"""`tracker answers apply`: correcting what the answer key proves wrong, through the claims.

Each test is one promise: a wrong figure is corrected by citing the answer's sentence
and ruling out the claim that refuted it, and the correction survives a recompute;
a "no figure" answer empties the field by ruling its claims out; a value the key
already holds right is left alone; and the decision is written into the row's notes.
"""

from __future__ import annotations

import datetime as dt

from tracker import answer_apply
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import AnswerKey, Project
from tracker.upsert import recompute_from_sources, upsert_record


def _cited(session, name, **claims_by_url):
    sources = [
        SourceRecord(
            url=url,
            source_type="trade_press",
            excerpt="excerpt",
            claims=dict(claims),
            quotes={k: f"the campus is {v}" for k, v in claims.items()},
            fetched_at=dt.datetime(2026, 1, 1),
        )
        for url, claims in claims_by_url.items()
    ]
    result = upsert_record(
        session,
        IngestRecord(
            project={"company": "Someone", "name": name, "city": "Englewood", "state": "CO"},
            sources=sources,
        ),
    )
    session.commit()
    return session.get(Project, result.project_id)


def _answer(session, project, field, value, **kw):
    session.add(
        AnswerKey(
            project_id=project.id, field=field, value=value, status="verified", method="test", **kw
        )
    )
    session.commit()


def test_a_wrong_figure_is_corrected_by_citing_the_answer_and_ruling_out_the_claim(session):
    project = _cited(session, "Campus A", **{"https://a.example/county": {"investment_usd": 11e9}})
    _answer(
        session,
        project,
        "investment_usd",
        "3000000000",
        source_url="https://b.example/release",
        quote="the campus will cost $3 billion",
    )

    fixes = answer_apply.plan(session)
    assert [(f.field, f.cite, len(f.refuted)) for f in fixes] == [("investment_usd", True, 1)]

    outcomes = answer_apply.apply(session, fixes)
    session.commit()

    assert outcomes[0].verdict == "right"
    assert project.investment_usd == 3e9
    recompute_from_sources(session, project)
    assert project.investment_usd == 3e9, "the correction is a claim, so it survives a recompute"
    assert "answer key" in (project.notes or "")
    assert answer_apply.plan(session) == [], "nothing left to correct"


def test_a_no_figure_answer_empties_the_field(session):
    project = _cited(session, "Campus B", **{"https://a.example/plant": {"mw_planned": 4500.0}})
    _answer(session, project, "mw_planned", None)

    outcomes = answer_apply.apply(session, answer_apply.plan(session))
    session.commit()

    assert outcomes[0].verdict == "right"
    assert project.mw_planned is None


def test_a_ruling_on_a_claim_the_key_holds_right_is_lifted(session):
    """The 2026-11-11 shape: the right figure stood ruled out from an earlier reading."""
    from tracker.conflicts import supersede
    from tracker.models import Source

    project = _cited(
        session,
        "Campus D",
        **{
            "https://a.example/old": {"mw_planned": 1500.0},
            "https://a.example/new": {"mw_planned": 1000.0},
        },
    )
    right = next(s for s in project.sources if s.url.endswith("/new"))
    supersede(right, "mw_planned", reason="misread")
    session.commit()
    recompute_from_sources(session, project)
    assert project.mw_planned == 1500.0
    _answer(session, project, "mw_planned", "1000")

    (fix,) = answer_apply.plan(session)
    assert fix.lifted == [right.id] and len(fix.refuted) == 1
    (outcome,) = answer_apply.apply(session, [fix])
    session.commit()

    assert outcome.verdict == "right"
    assert project.mw_planned == 1000.0
    assert "mw_planned" not in (session.get(Source, right.id).unconfirmed_fields or "")
    assert "ruling(s) on right claim(s) lifted" in (project.notes or "")


def test_a_value_the_key_holds_right_is_left_alone(session):
    project = _cited(session, "Campus C", **{"https://a.example/x": {"mw_planned": 300.0}})
    _answer(session, project, "mw_planned", "300")

    assert answer_apply.plan(session) == []


def test_the_dry_run_writes_nothing(session, monkeypatch, db_path):
    from typer.testing import CliRunner

    from tracker.cli import app

    project = _cited(session, "Campus D", **{"https://a.example/y": {"investment_usd": 9e9}})
    _answer(
        session,
        project,
        "investment_usd",
        "1000000000",
        source_url="https://b.example/z",
        quote="a $1 billion campus",
    )
    monkeypatch.setattr("tracker.cli._shared._db_path", lambda: db_path)

    result = CliRunner().invoke(app, ["answers", "apply"])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    session.expire_all()
    assert session.get(Project, project.id).investment_usd == 9e9


def test_the_keys_labels_are_frozen_before_it_corrects_the_rows(session, db_path):
    """After a correction the row reads "right"; training must still see it as wrong."""
    from tracker import suspect

    project = _cited(session, "Campus E", **{"https://a.example/county": {"investment_usd": 11e9}})
    _answer(
        session,
        project,
        "investment_usd",
        "3000000000",
        source_url="https://b.example/release",
        quote="the campus will cost $3 billion",
    )

    assert suspect.freeze(session, db_path) == 1, "the answered field only"
    assert suspect.freeze(session, db_path) == 0, "a campus is frozen once"
    answer_apply.apply(session, answer_apply.plan(session))
    session.commit()

    live = suspect.entries(session, db_path, [project.id])
    assert next(e for e in live if e["field"] == "investment_usd")["answer"]["verdict"] == "right"
    frozen = suspect.with_frozen(live, db_path)
    kept = next(e for e in frozen if e["field"] == "investment_usd")
    assert kept["answer"]["verdict"] == "wrong" and kept["held"] == 11e9
