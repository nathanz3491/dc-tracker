"""What the next word could be. A pure function, so these are ordinary tests.

The rules worth pinning down are the ones a person notices immediately when they
are wrong: a flag offered twice, a value offered for a switch, a closed vocabulary
that is not offered at all, and `ingest cr` completing to something that cannot be
run.
"""

from __future__ import annotations

import pytest

from tracker.tui import completion
from tracker.webui import catalog


@pytest.fixture(scope="module")
def commands() -> dict[str, catalog.Command]:
    return catalog.by_name()


def texts(result: completion.Completions) -> list[str]:
    return [item.text for item in result.items]


def kinds(result: completion.Completions) -> set[str]:
    return {item.kind for item in result.items}


# --- Command names ----------------------------------------------------------


def test_an_empty_line_offers_every_command(commands):
    result = completion.complete("", commands)
    assert len(result.items) == min(len(commands), completion.MAX_CANDIDATES)
    assert kinds(result) == {"command"}


def test_a_prefix_narrows_to_matching_commands(commands):
    result = completion.complete("cov", commands)
    assert texts(result) == ["coverage"]
    assert result.prefix == "cov"


def test_a_two_word_command_is_offered_whole(commands):
    """`ingest` on its own is a group and cannot run, so completing to it is a trap."""
    result = completion.complete("ingest cr", commands)
    assert texts(result) == ["ingest crawl"]
    assert result.prefix == "ingest cr", "the prefix spans the space, or the insert is wrong"


def test_a_word_from_the_middle_still_finds_the_command(commands):
    """Nothing starts with "crawl", so containment is the fallback."""
    assert "ingest crawl" in texts(completion.complete("crawl", commands))


def test_applying_a_command_leaves_a_line_ready_for_flags(commands):
    result = completion.complete("ingest cr", commands)
    line = result.apply("ingest cr", result.items[0])
    assert line == "ingest crawl "
    # And from there the next completion is that command's flags.
    following = completion.complete(line, commands)
    assert following.command is not None
    assert following.command.name == "ingest crawl"


def test_a_command_name_offers_the_help_as_the_hint(commands):
    result = completion.complete("coverage", commands)
    assert "supposed to know about" in result.items[0].hint


# --- Flags ------------------------------------------------------------------


def test_a_dash_offers_that_commands_flags(commands):
    result = completion.complete("sync --", commands)
    assert "--prospect" in texts(result)
    assert "--limit" in texts(result)
    assert kinds(result) == {"flag"}
    assert result.command.name == "sync"


def test_a_flag_already_in_the_line_is_not_offered_again(commands):
    """`--limit 5 --limit 9` is refused by `build_argv`, so offering it is offering
    a mistake."""
    result = completion.complete("sync --limit 5 --", commands)
    assert "--limit" not in texts(result)
    assert "--prospect" in texts(result)


def test_flag_hints_carry_the_type_and_the_default(commands):
    result = completion.complete("sync --since", commands)
    assert result.items[0].text == "--since-days"
    assert result.items[0].hint == "int=45"


def test_a_default_of_zero_is_not_mistaken_for_no_default(commands):
    """`0 == False`, so a membership test against `(None, False, "")` swallowed it.

    `--prospect` and `--enrich` both default to 0 and their whole meaning is "off
    unless you pass a number", so rendering them as if they had no default hid the
    one fact a reader wanted.
    """
    result = completion.complete("sync --prospect", commands)
    assert result.items[0].hint == "int=0"
    flag = next(f for f in commands["sync"].flags if f.name == "--prospect")
    assert completion.default_text(flag) == "=0"


def test_a_switch_hint_says_switch(commands):
    result = completion.complete("sync --brow", commands)
    assert result.items[0].hint == "switch"


# --- Values -----------------------------------------------------------------


def test_a_closed_vocabulary_is_offered_after_its_flag(commands):
    result = completion.complete("coverage --kind ", commands)
    assert texts(result) == ["hyperscaler", "ai_lab", "neocloud", "landlord"]
    assert kinds(result) == {"choice"}
    assert result.context.name == "--kind"


def test_the_same_flag_name_on_two_commands_offers_two_vocabularies(commands):
    """`--kind` means operator classes on `coverage` and filer classes on EDGAR.

    The EDGAR list includes `utility` and `contractor`, which `coverage` refuses,
    so one shared vocabulary would have offered a value that fails.
    """
    operators = texts(completion.complete("coverage --kind ", commands))
    filers = texts(completion.complete("ingest edgar --kind ", commands))
    assert "utility" in filers
    assert "utility" not in operators


def test_a_partial_value_narrows_the_vocabulary(commands):
    assert texts(completion.complete("coverage --kind neo", commands)) == ["neocloud"]


def test_a_switch_takes_no_value_so_the_flags_come_back(commands):
    """After `--deep` the next word is another flag, not a value for it."""
    result = completion.complete("sync --deep ", commands)
    assert result.items == () or kinds(result) <= {"flag", "value"}
    result = completion.complete("sync --deep --", commands)
    assert "--limit" in texts(result)


def test_a_number_has_nothing_to_offer_but_still_explains_itself(commands):
    """No candidates, and the flag is reported so its help can be shown."""
    result = completion.complete("sync --limit ", commands)
    assert result.items == ()
    assert result.context.name == "--limit"
    assert "Max NEW candidates" in result.context.help


# --- Positional values out of the database ----------------------------------


def test_project_ids_are_offered_for_a_project_positional(commands):
    projects = [
        {"id": 7, "company": "Nebius", "name": "Kansas City", "mw_planned": 300},
        {"id": 9, "company": "Meta", "name": "Hyperion", "mw_planned": 2000},
    ]
    provider = completion.value_provider(projects, None)
    result = completion.complete("enrich ", commands, values_for=provider)
    # Biggest first: nobody remembers that 9 is Hyperion, and the label is what
    # makes an id selectable at all.
    assert texts(result) == ["9", "7"]
    assert result.items[0].hint == "Meta — Hyperion"
    assert kinds(result) == {"value"}


def test_a_typed_digit_narrows_the_project_list(commands):
    projects = [
        {"id": 7, "company": "A", "name": "One", "mw_planned": 1},
        {"id": 71, "company": "B", "name": "Two", "mw_planned": 2},
    ]
    provider = completion.value_provider(projects, None)
    assert texts(completion.complete("show 7", commands, values_for=provider)) == ["71", "7"]


def test_operator_names_are_offered_for_prospect(commands):
    class _Row:
        def __init__(self, name, status, projects):
            self.name, self.status, self.projects = name, status, projects

    class _Report:
        def __init__(self):
            self.rows = [
                _Row("Meta", "covered", 15),
                _Row("Nebius", "absent", 0),
                _Row("Compass Datacenters", "absent", 0),
            ]

    provider = completion.value_provider([], _Report())
    result = completion.complete("prospect ", commands, values_for=provider)
    # Absent first, for the same reason `roster.hunt_order` puts them first.
    assert texts(result)[0] == '"Compass Datacenters"'
    assert "Nebius" in texts(result)
    assert texts(result)[-1] == "Meta"


def test_a_name_with_a_space_is_offered_quoted(commands):
    """`parse_command_line` tokenises with shlex, so the quotes have to be there."""

    class _Row:
        name, status, projects = "Compass Datacenters", "absent", 0

    class _Report:
        def __init__(self):
            self.rows = [_Row()]

    provider = completion.value_provider([], _Report())
    result = completion.complete("prospect ", commands, values_for=provider)
    line = result.apply("prospect ", result.items[0])
    assert line == 'prospect "Compass Datacenters" '
    cmd, flags = catalog.parse_command_line(line)
    assert cmd == "prospect"
    assert flags == {"operator": "Compass Datacenters"}


def test_a_positional_nobody_can_suggest_for_offers_nothing(commands):
    """A URL is not something this database knows."""
    provider = completion.value_provider([], None)
    result = completion.complete("ingest crawl ", commands, values_for=provider)
    assert result.items == ()


def test_no_provider_means_no_positional_suggestions(commands):
    assert completion.complete("enrich ", commands).items == ()


# --- Applying ---------------------------------------------------------------


def test_applying_replaces_only_the_word_being_typed(commands):
    result = completion.complete("sync --limit 5 --pro", commands)
    line = result.apply("sync --limit 5 --pro", result.items[0])
    assert line == "sync --limit 5 --prospect "


def test_applying_to_an_empty_word_appends(commands):
    result = completion.complete("sync --", commands)
    line = result.apply("sync --", result.items[0])
    assert line.startswith("sync --")
    assert line.endswith(" ")


def test_an_unparseable_half_typed_line_does_not_raise(commands):
    """This runs on every keystroke, so an unbalanced quote is normal here."""
    for line in ('point "half open', "sync --limit '", 'merge --into "x'):
        completion.complete(line, commands)
