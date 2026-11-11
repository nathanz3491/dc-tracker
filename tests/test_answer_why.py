"""`tracker answers why` and `answers try`: why a stored value is wrong, and what a
proposed check would change before it is built.

Measured on 2026-11-11: 10 of the 13 wrong investment figures should have been empty
— real figures for two campuses, a power plant, an estimate — and a blunt check that
looked right on the answer key would have emptied 86 good and bad figures across the
database. Each test pins one of the readings that showed it.
"""

from __future__ import annotations

import datetime as dt
import json

from tracker import answer_why
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import AnswerKey, Project
from tracker.upsert import upsert_record


def _campus(
    session, name, quote, value, *, extractor="crawl:extract-v1@test", field="investment_usd"
):
    result = upsert_record(
        session,
        IngestRecord(
            project={"company": "Someone", "name": name, "city": f"{name} Town", "state": "CO"},
            sources=[
                SourceRecord(
                    url=f"https://news.example/{name.replace(' ', '-').lower()}",
                    source_type="trade_press",
                    excerpt=quote[:200],
                    claims={field: value},
                    quotes={field: quote},
                    fetched_at=dt.datetime(2026, 1, 1),
                    extractor=extractor,
                )
            ],
        ),
    )
    session.commit()
    return session.get(Project, result.project_id)


def _answer(session, project, value, field="investment_usd"):
    session.add(
        AnswerKey(project_id=project.id, field=field, value=value, status="verified", method="test")
    )
    session.commit()


def test_a_guess_stored_where_the_field_should_be_empty_says_so(session):
    p = _campus(session, "Matador", "The full build-out could see a pricetag of $70 billion.", 7e10)
    _answer(session, p, None)

    [case] = answer_why.diagnose(session)
    assert case.cause == "the figure's sentence is a guess, a condition or a ceiling"
    assert case.should_be_empty and case.steps == ("article reader",)


def test_a_figure_for_more_than_the_campus_and_a_right_figure_never_read(session):
    wide = _campus(
        session, "Louisa", "Amazon will invest $11 billion in two data center campuses.", 11e9
    )
    _answer(session, wide, None)
    other = _campus(session, "Plain", "The campus will cost $3 billion, the county said.", 3e9)
    _answer(session, other, "5000000000")

    causes = {d.project_id: d.cause for d in answer_why.diagnose(session)}
    assert causes[wide.id] == "the figure's sentence covers more than the campus"
    assert causes[other.id] == "another figure, the right one never read"


def test_right_values_are_scored_by_the_step_that_wrote_them(session):
    right = _campus(session, "Right", "The campus will cost $3 billion, the county said.", 3e9)
    _answer(session, right, "3000000000")
    agent = _campus(
        session, "Agent", "Total investment is estimated to exceed $25 billion.", 25e9,
        extractor="gapfill-agent-v1", field="mw_planned",
    )  # fmt: skip
    _answer(session, agent, "100", field="mw_planned")

    steps = answer_why.by_step(session)
    assert (steps["article reader"].right, steps["article reader"].judged) == (1, 1)
    assert (steps["research agent"].right, steps["research agent"].judged) == (0, 1)


def test_a_trial_counts_what_it_fixes_and_loses_and_what_it_would_empty_elsewhere(session):
    """The answer key alone is not enough: the blunt check looked harmless on it."""
    guess = _campus(session, "Guess", "Costs could reach $20 billion over time.", 2e10)
    _answer(session, guess, None)
    filing = _campus(session, "Filing", "The filing gives an estimated cost of $80 million.", 8e7)
    _answer(session, filing, "80000000")
    _campus(session, "Unkeyed", "The project would cost $1 billion if completed.", 1e9)

    got = answer_why.trial(session, "hedge")
    assert (got.fixed, got.lost) == (1, 1), "fixes the guess, loses the filing's own estimate"
    assert got.held == 3 and len(got.emptied) == 3


def test_the_commands_answer_in_json(engine, db_path):
    from typer.testing import CliRunner

    from tracker.cli import app
    from tracker.db import session_scope

    with session_scope(engine) as session:
        p = _campus(
            session, "Matador", "The full build-out could see a pricetag of $70 billion.", 7e10
        )
        _answer(session, p, None)
    runner = CliRunner()
    why = runner.invoke(app, ["--db", str(db_path), "--json", "answers", "why"])
    assert why.exit_code == 0, why.output
    assert json.loads(why.stdout)["cases"][0]["should_be_empty"] is True
    tried = runner.invoke(app, ["--db", str(db_path), "--json", "answers", "try", "hedge"])
    assert tried.exit_code == 0, tried.output
    assert json.loads(tried.stdout)["answer_key"]["fixed"] == 1
    refused = runner.invoke(app, ["--db", str(db_path), "answers", "try", "nonsense"])
    assert refused.exit_code != 0
