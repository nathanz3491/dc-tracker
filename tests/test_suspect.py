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


@pytest.mark.parametrize("project_id", [999])
def test_an_agent_asking_about_a_missing_row_is_told_so(session, project_id):
    from tracker.agent import _check_suspicion

    assert _check_suspicion(session, project_id) == f"no project #{project_id}"
