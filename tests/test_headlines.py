"""The headline screen, offline: a fake model answers by keyword.

What the design rests on:

* :func:`test_a_screen_that_cannot_answer_keeps_every_headline` — a failed call or
  an answer in the wrong shape costs a read, never a campus.
* :func:`test_the_crawl_still_reads_its_limit_after_the_screen_sets_pages_aside` —
  the screen replaces what it sets aside from further down the queue, in order.
* :func:`test_a_page_kept_once_is_not_asked_again` — a model's verdict is a paid
  call and a chance to lose the page; a backlog page is asked once per prompt.
* :func:`test_measuring_counts_every_campus_the_screen_would_have_lost`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from tracker.cli import app
from tracker.ingest import discover, headlines
from tracker.llm import LLMError, LLMReply
from tracker.models import IngestUrl, Project, Source
from tracker.vocab import PENDING_URL_STATUS

#: Words the fake model sets a headline aside for, and the kind it gives.
RULES = {"Madrid": "abroad", "appoints": "people", "webinar": "event"}


class KeywordLLM:
    """Sets aside a numbered line whose headline holds one of `RULES`' words."""

    def __init__(self, rules: dict[str, str] | None = None, *, reply: str | None = None) -> None:
        self.rules = RULES if rules is None else rules
        self.reply = reply
        self.calls: list[str] = []

    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        self.calls.append(user)
        if self.reply is not None:
            return LLMReply(self.reply, "stop", 100, 10, "fake-model")
        aside = {}
        for line in user.splitlines():
            number, _, rest = line.partition(". ")
            headline = rest.rsplit(" | ", 1)[0]
            for word, kind in self.rules.items():
                if word.lower() in headline.lower():
                    aside[number] = kind
        return LLMReply(json.dumps({"aside": aside}), "stop", 100, 10, "fake-model")

    def lines_asked(self) -> int:
        return sum(len(user.splitlines()) for user in self.calls)


class FailingLLM:
    def complete(self, *, system: str, user: str, max_tokens: int | None = None) -> LLMReply:
        raise LLMError("the provider fell over")


def queued(session, title: str | None, *, n: int, **kwargs) -> IngestUrl:
    row = IngestUrl(
        url=f"https://news.example/{n}-{re.sub(r'[^a-z]+', '-', (title or 'none').lower())}",
        title=title,
        status=kwargs.pop("status", PENDING_URL_STATUS),
        run_id="test",
        published_at=dt.datetime(2026, 10, 1) + dt.timedelta(hours=n),
        **kwargs,
    )
    session.add(row)
    session.flush()
    return row


# --- judging ------------------------------------------------------------------------


def test_the_model_names_the_lines_to_set_aside_and_every_other_is_kept():
    lines = [
        headlines.Line(1, "CyrusOne appoints a managing director in Japan", "https://x.example/a"),
        headlines.Line(2, "County approves 300 MW campus near Abilene", "https://x.example/b"),
        headlines.Line(3, "A Madrid campus opens", "https://x.example/c"),
    ]
    assert headlines.judge(KeywordLLM(), lines) == {1: "people", 3: "abroad"}


def test_a_screen_that_cannot_answer_keeps_every_headline():
    lines = [headlines.Line(1, "A webinar on cooling", "https://x.example/a")]
    assert headlines.judge(FailingLLM(), lines) == {}
    assert headlines.judge(KeywordLLM(reply="not json at all"), lines) == {}
    assert headlines.judge(KeywordLLM(reply='{"keep": [1]}'), lines) == {}


def test_a_kind_not_on_the_list_or_a_line_not_asked_about_is_ignored():
    """The model may only set a page aside for a listed reason, and only a page it
    was shown: an invented kind or line number keeps the page."""
    lines = [headlines.Line(7, "Quarterly results", "https://x.example/a")]
    reply = json.dumps({"aside": {"1": "boring", "2": "people", "x": "abroad"}})
    assert headlines.judge(KeywordLLM(reply=reply), lines) == {}


def test_a_headline_naming_a_us_place_is_never_asked_about():
    """The first production measurement lost "CloudHQ LC11 - Loudoun, Virginia" and
    "Grimes County gives SpaceX Terafab project…" to a model told to keep them. A rule
    the code can apply is not left to the model."""
    from tracker.ingest import screen

    places = screen.us_places(["Abilene", "Dallas"])
    lines = [
        headlines.Line(1, "CloudHQ LC11 - Loudoun, Virginia", "https://x.example/a"),
        headlines.Line(
            2, "Grimes County gives SpaceX Terafab project massive tax break", "https://x.example/b"
        ),
        headlines.Line(3, "Dallas", "https://edged.example/dallas"),
        headlines.Line(4, "Campus update", "https://x.example/news/abilene-campus-update"),
        headlines.Line(5, "Operator appoints a new CFO", "https://x.example/e"),
    ]
    everything = KeywordLLM(dict.fromkeys(("a", "e", "o", "u"), "unrelated"))
    assert headlines.judge(everything, lines, places=places) == {5: "unrelated"}
    assert [
        line.split(". ", 1)[1].split(" | ")[0] for line in everything.calls[0].splitlines()
    ] == ["Operator appoints a new CFO"]


def test_headlines_are_asked_fifty_at_a_time():
    llm = KeywordLLM()
    lines = [
        headlines.Line(i, f"Campus {i} approved", f"https://x.example/{i}") for i in range(120)
    ]
    headlines.judge(llm, lines)
    assert [len(user.splitlines()) for user in llm.calls] == [50, 50, 20]


# --- the queue ------------------------------------------------------------------------


def test_the_crawl_still_reads_its_limit_after_the_screen_sets_pages_aside(session):
    rows = [
        queued(session, "Developer files plans for Mesa campus", n=1),
        queued(session, "Operator appoints a new CFO", n=2),
        queued(session, "Webinar: cooling at scale", n=3),
        queued(session, "Utility signs 500 MW deal for Ohio data center", n=4),
        queued(session, "Rezoning approved for Prince William campus", n=5),
    ]
    result = headlines.screen_queue(KeywordLLM(), rows, want=3, apply=True)

    assert [r.id for r in result.kept] == [rows[0].id, rows[3].id, rows[4].id]
    assert [(r.id, kind) for r, kind in result.aside] == [
        (rows[1].id, "people"),
        (rows[2].id, "event"),
    ]
    assert rows[1].status == "skipped"
    assert rows[1].error == f"{headlines.ASIDE}people"
    assert rows[0].screened_by == headlines.STAMP
    assert rows[0].status == PENDING_URL_STATUS


def test_it_asks_about_little_more_than_the_crawl_will_read(session):
    rows = [queued(session, f"Campus {n} approved", n=n) for n in range(200)]
    llm = KeywordLLM()
    result = headlines.screen_queue(llm, rows, want=10, apply=False)
    assert len(result.kept) == 10
    assert llm.lines_asked() == 10


def test_a_page_kept_once_is_not_asked_again(session):
    rows = [queued(session, f"Campus {n} approved", n=n) for n in range(3)]
    headlines.screen_queue(KeywordLLM(), rows, want=None, apply=True)
    again = KeywordLLM()
    result = headlines.screen_queue(again, rows, want=None, apply=True)
    assert again.calls == []
    assert len(result.kept) == 3


def test_go_live_results_and_untitled_pages_are_read_whatever_they_say(session):
    """A go-live search's result is read because a delay flag waits on it, and a page
    with no headline gives the screen nothing to judge."""
    golive = queued(session, "Madrid webinar recap", n=1)
    untitled = queued(session, None, n=2)
    llm = KeywordLLM()
    result = headlines.screen_queue(
        llm, [golive, untitled], want=None, exempt={golive.id}, apply=True
    )
    assert [r.id for r in result.kept] == [golive.id, untitled.id]
    assert llm.calls == []
    assert golive.screened_by is None, "kept without being judged, so not stamped"


def test_a_crawl_reads_unscreened_when_the_model_cannot_answer(session, monkeypatch):
    """No key, or peak hours: the crawl reads its limit as it did before the screen."""
    from tracker import llm as llm_mod
    from tracker.cli.ingest import _screen_headlines

    def unavailable(*args, **kwargs):
        raise llm_mod.MissingApiKey("no key")

    monkeypatch.setattr(llm_mod, "build_extractor", unavailable)
    rows = [queued(session, "Operator appoints a new CFO", n=n) for n in range(3)]
    assert _screen_headlines(session, rows, want=2, apply=True) == rows[:2]
    assert all(r.status == PENDING_URL_STATUS for r in rows)

    monkeypatch.setattr(llm_mod, "build_extractor", lambda *a, **k: KeywordLLM())
    assert _screen_headlines(session, rows, want=2, apply=True) == []
    assert all(r.status == "skipped" for r in rows)


def test_a_dry_run_changes_nothing(session):
    row = queued(session, "Operator appoints a new CFO", n=1)
    result = headlines.screen_queue(KeywordLLM(), [row], want=None, apply=False)
    assert [kind for _, kind in result.aside] == ["people"]
    assert row.status == PENDING_URL_STATUS and row.error is None and row.screened_by is None


# --- measuring ------------------------------------------------------------------------


def test_measuring_counts_every_campus_the_screen_would_have_lost(session):
    project = Project(
        name="Example Madrid Road Campus",
        company="Example",
        city="Leesburg",
        state="VA",
        country="US",
        phase="construction",
        confidence=1,
        dedup_key="example|city:leesburg|VA",
    )
    session.add(project)
    session.flush()
    lost = queued(session, "Madrid Road campus breaks ground", n=1, status="ok")
    session.add(Source(project_id=project.id, url=lost.url, source_type="trade_press"))
    queued(session, "County approves campus", n=2, status="ok")
    queued(session, "Operator appoints a new CFO", n=3, status="no_project")
    queued(session, "Webinar: cooling at scale", n=4, status="no_project")
    queued(session, "Industry outlook", n=5, status="no_project")
    queued(session, "Never read yet", n=6)
    session.flush()

    result = headlines.measure(session, KeywordLLM())

    assert (result.found, result.empty) == (2, 3)
    assert [(r.id, kind, cited) for r, kind, cited in result.lost] == [(lost.id, "abroad", True)]
    assert sorted(kind for _, kind in result.spared) == ["event", "people"]


# --- the command ------------------------------------------------------------------------

runner = CliRunner()


def _invoke(db: Path, *args: str):
    return runner.invoke(app, ["--db", str(db), *args])


def test_undo_puts_back_what_the_screen_set_aside_and_keeps_it_back(tmp_path, migrated_copy):
    db = migrated_copy(tmp_path / "t.db")
    from tracker.db import make_engine, session_scope

    with session_scope(make_engine(db)) as session:
        row = queued(session, "Operator appoints a new CFO", n=1)
        headlines.screen_queue(KeywordLLM(), [row], want=None, apply=True)
        assert row.status == "skipped"
        other = queued(
            session, "Old page", n=2, status="skipped", error="set aside before reading: a PDF"
        )

    result = _invoke(db, "queue", "screen", "--undo")
    assert result.exit_code == 0, result.output
    assert "1 page(s)" in result.output

    with session_scope(make_engine(db)) as session:
        back = session.scalars(
            select(IngestUrl).where(IngestUrl.title == "Operator appoints a new CFO")
        ).one()
        assert (back.status, back.error, back.screened_by) == (
            PENDING_URL_STATUS,
            None,
            headlines.STAMP,
        )
        untouched = session.get(IngestUrl, other.id)
        assert untouched.status == "skipped", "a page the address rules set aside is theirs"


def test_the_queue_command_reports_without_writing_unless_told(
    tmp_path, migrated_copy, monkeypatch
):
    db = migrated_copy(tmp_path / "t.db")
    from tracker import llm as llm_mod
    from tracker.db import make_engine, session_scope

    monkeypatch.setattr(llm_mod, "build_extractor", lambda *a, **k: KeywordLLM())
    with session_scope(make_engine(db)) as session:
        queued(session, "Operator appoints a new CFO", n=1)
        queued(session, "County approves campus", n=2)

    report = _invoke(db, "queue", "screen", "--limit", "0")
    assert report.exit_code == 0, report.output
    assert "1 would be set aside" in report.output and "people" in report.output

    with session_scope(make_engine(db)) as session:
        assert len(discover.pending(session)) == 2

    applied = _invoke(db, "queue", "screen", "--limit", "0", "--drop")
    assert applied.exit_code == 0, applied.output
    with session_scope(make_engine(db)) as session:
        assert [r.title for r in discover.pending(session)] == ["County approves campus"]


def test_measure_lists_each_read_it_would_have_lost(tmp_path, migrated_copy, monkeypatch):
    db = migrated_copy(tmp_path / "t.db")
    from tracker import llm as llm_mod
    from tracker.db import make_engine, session_scope

    monkeypatch.setattr(llm_mod, "build_extractor", lambda *a, **k: KeywordLLM())
    with session_scope(make_engine(db)) as session:
        queued(session, "Madrid campus breaks ground", n=1, status="ok")
        queued(session, "Operator appoints a new CFO", n=2, status="no_project")

    result = _invoke(db, "queue", "screen", "--measure")
    assert result.exit_code == 0, result.output
    assert "1 would have been set aside" in result.output
    assert "Madrid campus breaks ground" in result.output
    assert "[abroad]" in result.output, "the kind is text, not console markup"
    with session_scope(make_engine(db)) as session:
        assert session.scalars(select(IngestUrl).where(IngestUrl.status == "skipped")).all() == []
