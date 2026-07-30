"""The `risk` table: vocabulary, constraints, and the 0004 backfill.

Behaviour that depends on extraction or the write path lives in
`test_ingest_crawl.py` and `test_upsert.py`; this module covers the schema itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tracker.db import discover_migrations, make_engine, run_migrations
from tracker.vocab import (
    DEFAULT_RISK_SEVERITY,
    OPEN_RISK_STATUS,
    RISK_CATEGORIES,
    RISK_SEVERITIES,
    RISK_STATUSES,
)

_PROJECT = (
    "INSERT INTO project (id, name, company, city, state, dedup_key) "
    "VALUES (1, 'n', 'c', 'ci', 'WI', 'k')"
)

_RISK = (
    "INSERT INTO risk (project_id, category, severity, status, summary) "
    "VALUES (1, :cat, :sev, :status, 's')"
)


# --- Vocabulary -------------------------------------------------------------


def test_severities_are_ordered_least_to_most_severe():
    """The order decides which risk becomes `project.blocker`, so pin it."""
    assert RISK_SEVERITIES == ("watch", "material", "blocking")


def test_the_default_severity_is_the_least_severe():
    """A source that names an obstacle without stating an effect must not be read
    as reporting a blocker: `blocker` is the field an operator acts on."""
    assert RISK_SEVERITIES[0] == DEFAULT_RISK_SEVERITY


def test_open_is_a_real_status():
    assert OPEN_RISK_STATUS in RISK_STATUSES


def test_every_prd_obstacle_kind_has_a_category():
    """The PRD names these seven; each must be expressible or the table cannot
    answer the question it exists for."""
    for category in (
        "grid_capacity",  # the local grid cannot supply the load
        "transmission",  # substation and line construction lags
        "permitting",  # government approval takes longer than planned
        "equipment_supply",  # transformers, cooling
        "financing",  # funding not secured
        "offtake",  # no committed customer
        "community_opposition",  # residents, noise
        "water",  # water use
        "environmental",  # environmental impact
    ):
        assert category in RISK_CATEGORIES


def test_categories_are_unique():
    assert len(RISK_CATEGORIES) == len(set(RISK_CATEGORIES))


# --- Constraints ------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "params"),
    [
        ("category", {"cat": "traffic", "sev": "watch", "status": "open"}),
        ("severity", {"cat": "water", "sev": "catastrophic", "status": "open"}),
        ("status", {"cat": "water", "sev": "watch", "status": "pending"}),
    ],
)
def test_closed_vocabularies_are_enforced(engine: Engine, column: str, params: dict):
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        with pytest.raises(IntegrityError, match=f"ck_risk_{column}"):
            conn.execute(text(_RISK), params)


def test_an_open_risk_cannot_carry_a_resolution_date(engine: Engine):
    """Otherwise a row reads as both current and settled, and `project.blocker`
    would report an obstacle that something already recorded as fixed."""
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO risk (project_id, category, severity, status, summary, "
                    "resolved_at) VALUES (1, 'water', 'watch', 'open', 's', '2026-01-01')"
                )
            )


def test_a_resolved_risk_may_carry_a_resolution_date(engine: Engine):
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        conn.execute(
            text(
                "INSERT INTO risk (project_id, category, severity, status, summary, "
                "resolved_at) VALUES (1, 'water', 'watch', 'resolved', 's', '2026-01-01')"
            )
        )
        assert conn.execute(text("SELECT count(*) FROM risk")).scalar_one() == 1


def test_quote_length_is_capped(engine: Engine):
    """Same reasoning as `source.excerpt`: unbounded scraped text is both a
    copyright and a database-size problem."""
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO risk (project_id, category, severity, summary, quote) "
                    "VALUES (1, 'water', 'watch', 's', :q)"
                ),
                {"q": "x" * 501},
            )


def test_delay_days_cannot_be_negative(engine: Engine):
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO risk (project_id, category, severity, summary, delay_days) "
                    "VALUES (1, 'water', 'watch', 's', -1)"
                )
            )


def test_status_defaults_to_open(engine: Engine):
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        conn.execute(
            text(
                "INSERT INTO risk (project_id, category, severity, summary) "
                "VALUES (1, 'water', 'watch', 's')"
            )
        )
        assert conn.execute(text("SELECT status FROM risk")).scalar_one() == OPEN_RISK_STATUS


def test_one_category_per_project_per_date(engine: Engine):
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        ins = text(
            "INSERT INTO risk (project_id, category, severity, summary, first_seen) "
            "VALUES (1, 'water', 'watch', 's', '2026-01-01')"
        )
        conn.execute(ins)
        with pytest.raises(IntegrityError):
            conn.execute(ins)


def test_deleting_a_project_deletes_its_risks(engine: Engine):
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        conn.execute(
            text(
                "INSERT INTO risk (project_id, category, severity, summary) "
                "VALUES (1, 'water', 'watch', 's')"
            )
        )
        conn.execute(text("DELETE FROM project WHERE id = 1"))
        assert conn.execute(text("SELECT count(*) FROM risk")).scalar_one() == 0


def test_deleting_a_source_keeps_the_risk(engine: Engine):
    """ON DELETE SET NULL, same as `event.source_id`: retiring a superseded
    citation must not erase the fact that an obstacle was reported."""
    with engine.begin() as conn:
        conn.execute(text(_PROJECT))
        conn.execute(
            text(
                "INSERT INTO source (id, project_id, url, source_type) "
                "VALUES (7, 1, 'https://a.test/x', 'trade_press')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO risk (project_id, category, severity, summary, source_id) "
                "VALUES (1, 'water', 'watch', 's', 7)"
            )
        )
        conn.execute(text("DELETE FROM source WHERE id = 7"))
        assert conn.execute(text("SELECT source_id FROM risk")).scalar_one() is None


# --- The 0004 backfill ------------------------------------------------------


def test_0004_backfills_an_existing_blocker_as_a_cited_risk(tmp_path: Path):
    """Upgrading must not drop what the old column already held.

    `unclassified` because the sentence does not say which category it is, and
    guessing from keywords inside a migration would be inventing a fact in the
    worst possible place. Re-crawling reclassifies it properly.
    """
    migrations = discover_migrations()
    engine = make_engine(tmp_path / "upgrade.db")
    run_migrations(engine, [m for m in migrations if m.version <= 3])

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO project (id, name, company, city, state, dedup_key, blocker) "
                "VALUES (1, 'n', 'c', 'ci', 'WI', 'k', 'Seeking anchor tenant')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO source (id, project_id, url, source_type, claims) "
                "VALUES (5, 1, 'https://a.test/x', 'trade_press', '{\"blocker\": \"x\"}')"
            )
        )

    assert 4 in run_migrations(engine, migrations)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT category, severity, status, summary, source_id FROM risk")
        ).one()
    assert row == ("unclassified", "material", "open", "Seeking anchor tenant", 5)


def test_0004_leaves_a_project_without_a_blocker_alone(tmp_path: Path):
    migrations = discover_migrations()
    engine = make_engine(tmp_path / "upgrade.db")
    run_migrations(engine, [m for m in migrations if m.version <= 3])

    with engine.begin() as conn:
        conn.execute(text(_PROJECT))

    run_migrations(engine, migrations)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM risk")).scalar_one() == 0


def test_0004_backfills_uncited_when_no_source_asserted_the_blocker(tmp_path: Path):
    """The subquery yields NULL rather than picking an arbitrary source, so
    `tracker review` can surface the row as an uncited obstacle."""
    migrations = discover_migrations()
    engine = make_engine(tmp_path / "upgrade.db")
    run_migrations(engine, [m for m in migrations if m.version <= 3])

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO project (id, name, company, city, state, dedup_key, blocker) "
                "VALUES (1, 'n', 'c', 'ci', 'WI', 'k', 'Something is wrong')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO source (id, project_id, url, source_type, claims) "
                "VALUES (5, 1, 'https://a.test/x', 'trade_press', '{\"mw_planned\": 10}')"
            )
        )

    run_migrations(engine, migrations)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT source_id FROM risk")).scalar_one() is None
