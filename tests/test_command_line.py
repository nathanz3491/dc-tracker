"""The console's command box, and the fact that it is not a shell.

It looks like a terminal, which is the whole risk: the page is published on the
internet behind a password and runs commands. So the box is a shorthand for the
command *form*, not a second way in — the line is parsed against the catalog into
the same `(cmd, flags)` a form submission produces, and `build_argv` turns that
into the same validated argument list.

Nothing here ever reaches an interpreter. These tests are mostly about proving
that the things people try when they see a `$` are refused by name.
"""

from __future__ import annotations

import pytest

from tracker.webui import catalog
from tracker.webui.catalog import InvalidRequest, parse_command_line


def test_a_plain_command_parses():
    assert parse_command_line("stats") == ("stats", {})


def test_a_leading_tracker_is_allowed():
    """People will type it. Refusing would be pedantry about a prompt."""
    assert parse_command_line("tracker stats") == ("stats", {})


def test_several_positionals_reach_the_command():
    """The case the form could not express, and the reason this box exists.

    `merge 4 7 9 --into 2` in one line, against a picker where selecting several
    projects was possible but nobody found it.
    """
    cmd, flags = parse_command_line("merge 4 7 9 --into 2")
    assert cmd == "merge"
    assert flags == {"dupe_ids": ["4", "7", "9"], "--into": "2"}
    argv = catalog.build_argv(cmd, flags, db_path="x.db")
    assert argv[-4:] == ["--into", "2", "4", "7"] or argv[-3:] == ["4", "7", "9"]
    assert "9" in argv


def test_a_repeated_option_becomes_a_list():
    cmd, flags = parse_command_line("ingest crawl --url https://a.test --url https://b.test")
    assert cmd == "ingest crawl"
    assert flags["--url"] == ["https://a.test", "https://b.test"]


def test_a_two_word_command_is_not_read_as_one_plus_junk():
    """`logic` alone is not a command and `ingest` takes a subcommand."""
    assert parse_command_line("logic check")[0] == "logic check"
    assert parse_command_line("ingest geo")[0] == "ingest geo"


def test_the_longest_command_name_wins(monkeypatch):
    """Matters the day a group name is also a command in its own right.

    No command today is a prefix of another, so the current catalog would parse
    correctly either way — which is exactly why this needs a catalog where it
    does matter. Shortest-first would read `review verify` as `review` with a
    stray positional and run the wrong thing with no error.
    """
    real = catalog.by_name()
    both = {
        "review": real["review"],
        "review verify": catalog.Command(name="review verify", help="", cost="free", flags=()),
    }
    monkeypatch.setattr(catalog, "by_name", lambda: both)

    assert parse_command_line("review verify") == ("review verify", {})
    assert parse_command_line("review")[0] == "review"


def test_quotes_hold_a_value_together():
    cmd, flags = parse_command_line('point "Stargate Abilene" --dry-run')
    assert cmd == "point"
    assert flags == {"name": "Stargate Abilene", "--dry-run": True}


def test_equals_form_is_accepted():
    assert parse_command_line("sync --limit=20")[1] == {"--limit": "20"}


def test_a_switch_takes_no_value_and_does_not_eat_the_next_word():
    """`--dry-run 4` must be a switch and a positional, not a switch with a value."""
    cmd, flags = parse_command_line("merge --dry-run 4 --into 2")
    assert cmd == "merge"
    assert flags == {"--dry-run": True, "dupe_ids": "4", "--into": "2"}


def test_a_switch_given_a_value_with_equals_is_refused():
    with pytest.raises(InvalidRequest, match="takes no value"):
        parse_command_line("merge --dry-run=yes 4 --into 2")


@pytest.mark.parametrize(
    "line",
    [
        "cd /",
        "rm -rf /",
        "ls",
        "cat .env",
        "python -c 'print(1)'",
        "curl https://example.test",
        "echo hi > /tmp/x",
    ],
)
def test_shell_commands_are_refused_by_name(line):
    """The load-bearing test.

    Not "the shell rejects them" — there is no shell. The first word is not in the
    catalog, so there is nothing to run and the box says so.
    """
    with pytest.raises(InvalidRequest, match="no `"):
        parse_command_line(line)


@pytest.mark.parametrize(
    "line",
    ["list; rm -rf /", "list && rm -rf /", "list | tee out", "list `whoami`", "list $(whoami)"],
)
def test_shell_punctuation_never_chains_anything(line):
    """A metacharacter is an ordinary character here.

    It either lands in the command name — which is then unknown — or becomes a
    positional string that `build_argv` passes as one argument to a Python list.
    Either way nothing is interpreted, and no second command exists.
    """
    try:
        cmd, flags = parse_command_line(line)
    except InvalidRequest:
        return
    argv = catalog.build_argv(cmd, flags, db_path="x.db")
    assert all(part not in argv for part in ("rm", "-rf", "/", "whoami", "tee"))


def test_a_blocked_command_is_still_blocked():
    """Typing it is not a way past the blocklist.

    `cloudflare` parses — it is a real command — and `build_argv` is where it is
    refused, exactly as it would be from the form.
    """
    cmd, flags = parse_command_line("cloudflare")
    with pytest.raises(InvalidRequest, match="cannot be run from the console"):
        catalog.build_argv(cmd, flags, db_path="x.db")


def test_an_unknown_option_is_named_with_a_suggestion():
    with pytest.raises(InvalidRequest) as caught:
        parse_command_line("merge 4 --nto 2")
    assert "--nto" in str(caught.value)
    assert "--into" in str(caught.value), "a near miss should suggest the real flag"


def test_a_mistyped_command_suggests_the_real_one():
    with pytest.raises(InvalidRequest, match="list"):
        parse_command_line("lst")


def test_an_option_with_no_value_is_refused_rather_than_swallowing_the_next_word():
    with pytest.raises(InvalidRequest, match="needs a value"):
        parse_command_line("sync --limit")


def test_an_unbalanced_quote_is_a_message_not_a_traceback():
    with pytest.raises(InvalidRequest, match="unbalanced"):
        parse_command_line('point "Stargate')


@pytest.mark.parametrize("line", ["", "   ", "tracker", "tracker   "])
def test_an_empty_line_does_nothing(line):
    with pytest.raises(InvalidRequest, match="nothing to run"):
        parse_command_line(line)


def test_a_command_that_takes_no_arguments_says_so():
    with pytest.raises(InvalidRequest, match="takes no arguments"):
        parse_command_line("stats extra")


def test_every_catalog_command_round_trips_through_the_parser():
    """Whatever the CLI grows next is typeable here without further work."""
    for name, command in catalog.by_name().items():
        parsed, flags = parse_command_line(name)
        assert parsed == name, f"{name} did not parse back to itself"
        assert flags == {}
        assert command  # the catalog entry exists, which is what made it parseable
