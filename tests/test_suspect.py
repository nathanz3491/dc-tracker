"""The suspicion score: its model, its ranking, the suspects it hands the logic agent.

Each test is one promise: the plain-Python regression learns a signal and ranks by it,
a saved model trained on other features is refused rather than misread, the night's
suspects are the top of the ranking plus a reproducible random share, a refit with too
few labels keeps the old model, and an agent asking about a row it cannot find is told
so rather than failing its turn.
"""

from __future__ import annotations

import json
import random

import pytest

from tracker import suspect
from tracker.models import Project


def test_the_regression_learns_a_signal_and_ranks_by_it():
    rng = random.Random(1)
    X, y = [], []
    for _ in range(300):
        wrong = rng.random() < 0.4
        backers = rng.gauss(1.0 if wrong else 2.5, 0.6)
        X.append([backers, rng.random()])
        y.append(int(wrong))

    model = suspect.fit(X, y)

    assert model.weights[0] < 0, "more backing, less likely wrong"
    assert suspect.auc(y, [model.prob(x) for x in X]) > 0.85
    assert suspect.heldout_auc(X, y, [i % 30 for i in range(300)]) > 0.8


def test_auc_is_the_chance_a_wrong_value_outranks_a_right_one():
    assert suspect.auc([1, 0], [0.9, 0.1]) == 1.0
    assert suspect.auc([1, 0], [0.1, 0.9]) == 0.0
    assert suspect.auc([1, 0, 0], [0.5, 0.5, 0.1]) == 0.75
    assert suspect.auc([0, 0], [0.1, 0.2]) is None


def test_a_model_saved_on_other_features_is_refused(tmp_path):
    db = tmp_path / "tracker.db"
    (tmp_path / "models").mkdir()
    saved = suspect.fit([[1.0] * len(suspect.FEATURES), [0.0] * len(suspect.FEATURES)], [1, 0])
    data = saved.as_json()
    (tmp_path / "models" / suspect.MODEL).write_text(json.dumps(data), encoding="utf-8")
    assert suspect.load(db) is not None

    data["features"] = ["something", "else"]
    (tmp_path / "models" / suspect.MODEL).write_text(json.dumps(data), encoding="utf-8")
    assert suspect.load(db) is None


def test_the_signals_read_the_held_values_backing():
    entry = {
        "project_id": 1,
        "field": "mw_planned",
        "held": 300.0,
        "row": {"citations": 9},
        "claims": [
            {
                "value": "300",
                "number": 300.0,
                "tier": "directory",
                "held": True,
                "source_url": "https://a.example/x",
                "source_type": "general_media",
                "as_of": "2026-10-01",
                "quote": "",
            },
            {
                "value": "1200",
                "number": 1200.0,
                "tier": "confirmed",
                "held": False,
                "source_url": "https://b.example/y",
                "source_type": "company_filing",
                "as_of": "2026-10-20",
                "quote": "up to 1.2 GW",
            },
        ],
    }
    import datetime as dt

    got = suspect.signals(entry, dt.date(2026, 11, 1))

    assert got["directory_only"] == 1.0
    assert got["primary_backed"] == 0.0
    assert got["rival_claims"] > 0
    assert got["field_mw_planned"] == 1.0
    assert suspect.signals({**entry, "held": None}) is None


def _ranking(tmp_path, n=50):
    runs = tmp_path / "runs"
    runs.mkdir()
    rows = [
        {
            "night": "2026-11-01",
            "rank": i + 1,
            "project_id": i + 1,
            "field": "mw_planned",
            "held": 100 + i,
            "p_wrong": round(1 - i / n, 3),
            "why": ["rival_claims"],
        }
        for i in range(n)
    ]
    (runs / suspect.RANKED).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return tmp_path / "tracker.db"


def test_the_suspects_are_the_top_plus_a_reproducible_random_fifth(tmp_path):
    db = _ranking(tmp_path)

    first = suspect.suspects(db, 10)
    again = suspect.suspects(db, 10)

    ids = [f.project_id for f in first]
    assert ids[:8] == list(range(1, 9)), "eight from the top"
    assert all(i > 8 for i in ids[8:]) and len(ids) == 10, "two drawn from the rest"
    assert ids == [f.project_id for f in again], "a rerun the same night offers the same"
    assert {f.code for f in first} == {"suspect_mw_planned"}
    assert first[0].fields == ("mw_planned",)
    assert "likely wrong" in first[0].summary


def test_no_ranking_means_no_suspects(tmp_path):
    assert suspect.suspects(tmp_path / "tracker.db", 10) == []


def test_a_refit_with_too_few_labels_keeps_the_old_model(session, db_path):
    session.add(
        Project(
            name="Campus",
            company="Compass Datacenters",
            state="AZ",
            city="Mesa",
            phase="announced",
            dedup_key="suspect-1",
        )
    )
    session.commit()

    report = suspect.train_logistic(session, db_path)

    assert report["kept"].startswith("too few labels")
    assert not (db_path.parent / "models" / suspect.MODEL).exists()


def _row(session, n: int, mw: float, *, quote: str = "a 300 MW campus") -> Project:
    from tracker.models import Source

    project = Project(
        name=f"Campus {n}",
        company="Compass Datacenters",
        state="AZ",
        city="Mesa",
        phase="announced",
        dedup_key=f"suspect-sel-{n}",
        mw_planned=mw,
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url=f"https://e.test/{n}",
            source_type="trade_press",
            claims=json.dumps({"mw_planned": mw, "investment_usd": 1_000_000_000}),
            quotes=json.dumps({"mw_planned": quote, "investment_usd": "a $1 billion campus"}),
            fields="mw_planned,investment_usd",
        )
    )
    session.commit()
    session.refresh(project)
    return project


def _rank(db_path, projects):
    runs = db_path.parent / "runs"
    runs.mkdir(exist_ok=True)
    rows = [
        {
            "night": "2026-11-11",
            "rank": i + 1,
            "project_id": p.id,
            "field": "mw_planned",
            "held": p.mw_planned,
            "p_wrong": round(0.9 - i / 100, 3),
            "why": ["rival_claims"],
        }
        for i, p in enumerate(projects)
    ]
    (runs / suspect.RANKED).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def _left_alone(session, project, *, days_ago: int = 2):
    """A suspect re-read the agent left alone, recorded as `triage` records it."""
    import datetime as dt

    from tracker import declines
    from tracker.models import ModelDecline, utcnow

    declines.record(
        session,
        "logic",
        f"{project.id}:suspect_mw_planned",
        declines.value_evidence(project, "mw_planned"),
        outcome="left alone",
    )
    row = session.query(ModelDecline).filter_by(subject=f"{project.id}:suspect_mw_planned").one()
    row.decided_at = utcnow() - dt.timedelta(days=days_ago)
    session.commit()


def test_a_value_left_alone_is_not_offered_again_until_its_evidence_changes(session, db_path):
    """On 2026-11-11, 30 rows had taken 172 of 241 re-reads: the question was keyed on
    its wording, which carries the night's rank, so it was new every night."""
    first, second = _row(session, 1, 300.0), _row(session, 2, 400.0, quote="a 400 MW campus")
    _rank(db_path, [first, second])
    _left_alone(session, first)

    offered = [f.project_id for f in suspect.suspects(db_path, 1, session=session)]
    assert offered == [second.id], "the slot goes to the next value, not to a repeat"

    # Another field's evidence changing is not new evidence about this value.
    source = first.sources[0]
    source.quotes = json.dumps(
        {"mw_planned": "a 300 MW campus", "investment_usd": "now a $2 billion campus"}
    )
    session.commit()
    assert [f.project_id for f in suspect.suspects(db_path, 1, session=session)] == [second.id]

    # The sentence behind this value changing is.
    source.quotes = json.dumps(
        {"mw_planned": "a 300 MW first phase", "investment_usd": "now a $2 billion campus"}
    )
    session.commit()
    assert [f.project_id for f in suspect.suspects(db_path, 1, session=session)] == [first.id]

    assert suspect.suspects(db_path, 1, session=session, again=True)[0].project_id == first.id


def test_every_slot_is_filled_past_values_already_judged(session, db_path):
    rows = [_row(session, n, 100.0 + n, quote=f"a {100 + n} MW campus") for n in range(10)]
    _rank(db_path, rows)
    for held in rows[:3]:
        _left_alone(session, held)

    got = suspect.suspects(db_path, 5, session=session)

    ids = [f.project_id for f in got]
    assert len(ids) == 5, "every slot used"
    assert not {r.id for r in rows[:3]} & set(ids), "nothing already judged"
    assert ids[:4] == [r.id for r in rows[3:7]], "the top share from the top of what is open"


def test_a_suspect_names_the_citations_stating_its_value(session, db_path):
    row = _row(session, 1, 300.0)
    _rank(db_path, [row])

    (got,) = suspect.suspects(db_path, 1, session=session)

    assert f"The citation(s) stating it: {row.sources[0].id}" in got.summary
    assert "field=mw_planned" in got.summary


def test_a_logic_round_reads_the_likeliest_wrong_findings_first(engine, db_path, monkeypatch):
    """The round is capped (60 a night): ordered by project id, the cap fell wherever
    the ids fell. Ordered by the suspicion score, it falls on the least suspect."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from tracker import logic
    from tracker.cli import app

    findings = [
        logic.Finding(project_id=pid, code="built_exceeds_planned", severity="warning",
                      summary=f"row {pid}", fields=("mw_planned",))
        for pid in (1, 2, 3)
    ]  # fmt: skip
    monkeypatch.setattr(logic, "review", lambda session: SimpleNamespace(findings=findings))
    runs = db_path.parent / "runs"
    runs.mkdir(exist_ok=True)
    (runs / suspect.RANKED).write_text(
        json.dumps({"rank": 1, "project_id": 2, "field": "mw_planned", "p_wrong": 0.9}) + "\n"
        + json.dumps({"rank": 2, "project_id": 1, "field": "mw_planned", "p_wrong": 0.2}) + "\n",
        encoding="utf-8",
    )  # fmt: skip

    result = CliRunner().invoke(app, ["--db", str(db_path), "--json", "logic", "resolve", "--auto"])

    assert result.exit_code == 0, result.output
    order = [f["project_id"] for f in json.loads(result.output)["left_for_a_person"]]
    assert order == [2, 1, 3], "most suspect first; an unscored field last; nothing skipped"


@pytest.mark.parametrize("project_id", [999])
def test_an_agent_asking_about_a_missing_row_is_told_so(session, project_id):
    from tracker.agent import _check_suspicion

    assert _check_suspicion(session, project_id) == f"no project #{project_id}"
