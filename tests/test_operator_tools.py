"""The operator's tools for untangling two sites read as one (2026-10-21).

Three rows fixed by hand that night each needed something the CLI did not have: a
row named after one of its two buildings (Prime's #6, "1231 Comstock"), an article
about a second campus whose tranches sat on the first (NTT Hillsboro, #29), and a
phase claim that could not be ruled out at all, because the command emptied a
NOT NULL column before re-deriving it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from typer.testing import CliRunner

from tracker.cli import app
from tracker.db import session_scope
from tracker.ingest.records import BlockRecord, IngestRecord, SourceRecord
from tracker.models import CapacityBlock, Project, Source
from tracker.upsert import upsert_record

T0 = dt.datetime(2026, 5, 20, 12, 0, 0)
CAMPUS = "https://example.test/ntt-hillsboro-campus"
LAND = "https://example.test/ntt-buys-land"


def _invoke(db_path, *args):
    return CliRunner().invoke(app, ["--db", str(db_path), *args])


def _record(url: str, *, blocks=(), phase="operational", quote="It has been operating since 2021."):
    claims = {"name": "Hillsboro", "company": "NTT", "city": "Hillsboro", "state": "OR"}
    if phase:
        claims["phase"] = phase
    return IngestRecord(
        project={"name": "Hillsboro", "company": "NTT", "city": "Hillsboro", "state": "OR"},
        sources=[
            SourceRecord(
                url=url,
                source_type="trade_press",
                fetched_at=T0,
                excerpt=quote,
                claims=claims,
                quotes={"phase": quote} if phase else {},
                blocks=list(blocks),
            )
        ],
    )


def _seed(engine) -> int:
    with session_scope(engine) as session:
        first = upsert_record(
            session,
            _record(CAMPUS, blocks=[BlockRecord(label="HI1", mw=36.0, status="serving")]),
        )
        upsert_record(
            session,
            _record(
                LAND,
                phase="announced",
                quote="NTT announced plans to build on new land.",
                blocks=[BlockRecord(label="New Pacific Northwest site", mw=216.0)],
            ),
        )
        return first.project_id


def _labels(engine, project_id: int) -> set[str]:
    with session_scope(engine, commit=False) as session:
        return set(
            session.scalars(
                select(CapacityBlock.label).where(CapacityBlock.project_id == project_id)
            )
        )


def test_a_citations_tranches_come_off_the_row_and_stay_off(engine, db_path):
    pid = _seed(engine)
    assert _labels(engine, pid) == {"HI1", "New Pacific Northwest site"}

    done = _invoke(
        db_path,
        "logic", "rule-out", str(pid), "blocks", "--citation", LAND,
        "--why", "the 2025 land purchase, now its own row",
    )  # fmt: skip
    assert done.exit_code == 0, done.output
    assert "New Pacific Northwest site" in done.output
    assert _labels(engine, pid) == {"HI1"}

    # A re-read of the same article must not put the second campus back.
    with session_scope(engine) as session:
        upsert_record(
            session,
            _record(
                LAND,
                phase="announced",
                quote="NTT announced plans to build on new land.",
                blocks=[BlockRecord(label="New Pacific Northwest site", mw=216.0)],
            ),
        )
    assert _labels(engine, pid) == {"HI1"}
    with session_scope(engine, commit=False) as session:
        project = session.get(Project, pid)
        assert "operator resolved `operator_ruled_out`: blocks" in project.notes


def test_a_citation_without_tranches_is_refused(engine, db_path):
    pid = _seed(engine)
    with session_scope(engine) as session:
        bare = session.scalar(select(Source).where(Source.url == LAND))
        bare.blocks = None
    done = _invoke(
        db_path, "logic", "rule-out", str(pid), "blocks", "--citation", LAND, "--why", "x"
    )
    assert done.exit_code != 0
    assert "name no tranche" in done.output


def test_a_phase_claim_can_be_ruled_out_by_hand(engine, db_path):
    """Emptying `phase` before the re-derive failed the write: NOT NULL.

    The row's own serving tranche still lifts it, as a serving building should; with
    that citation's tranches ruled out as well — Cyxtera's #49, whose tranche was
    another company's building — the claim left standing decides.
    """
    pid = _seed(engine)
    done = _invoke(
        db_path,
        "logic", "rule-out", str(pid), "phase", "--citation", CAMPUS,
        "--why", "about another building",
    )  # fmt: skip
    assert done.exit_code == 0, done.output
    with session_scope(engine, commit=False) as session:
        assert session.get(Project, pid).phase == "operational"

    # In the library rather than a second command: one process holds the
    # single-writer lock until it exits, and a second writing command here would
    # meet it.
    from tracker import blocks as blocks_mod
    from tracker.upsert import recompute_from_sources

    with session_scope(engine) as session:
        project = session.get(Project, pid)
        source = session.scalar(select(Source).where(Source.url == CAMPUS))
        assert blocks_mod.rule_out(source, reason="misread")
        blocks_mod.rebuild(session, project)
        recompute_from_sources(session, project)
    with session_scope(engine, commit=False) as session:
        assert session.get(Project, pid).phase == "announced"


def test_a_row_takes_the_name_its_campus_goes_by(engine, db_path):
    pid = _seed(engine)
    with session_scope(engine, commit=False) as session:
        key = session.get(Project, pid).dedup_key

    done = _invoke(db_path, "rename", str(pid), "Hillsboro  Campus", "--why", "the campus, not HI1")
    assert done.exit_code == 0, done.output
    with session_scope(engine, commit=False) as session:
        project = session.get(Project, pid)
        assert project.name == "Hillsboro Campus"
        assert project.dedup_key == key
        assert "name 'Hillsboro' -> 'Hillsboro Campus'" in project.notes
        assert "the campus, not HI1" in project.notes


def test_a_rename_needs_a_name_and_writes_nothing_on_a_dry_run(engine, db_path):
    pid = _seed(engine)
    assert _invoke(db_path, "rename", str(pid), "   ", "--why", "x").exit_code != 0
    done = _invoke(db_path, "rename", str(pid), "Other", "--why", "x", "--dry-run")
    assert done.exit_code == 0, done.output
    with session_scope(engine, commit=False) as session:
        assert session.get(Project, pid).name == "Hillsboro"


def test_unalias_lets_a_wrongly_folded_key_make_its_own_row(engine, db_path):
    """Related Digital's South Works data center was folded into the quantum park on
    the same land, and its key kept routing every later reading there."""
    from tracker.models import ProjectAlias

    pid = _seed(engine)
    key = "related digital|city:hillsboro|OR"
    with session_scope(engine) as session:
        session.add(ProjectAlias(from_dedup_key=key, to_project_id=pid, decided_by="agent"))

    assert _invoke(db_path, "unalias", key, "--why", "x", "--dry-run").exit_code == 0
    with session_scope(engine, commit=False) as session:
        assert session.scalar(select(ProjectAlias).where(ProjectAlias.from_dedup_key == key))

    done = _invoke(db_path, "unalias", key, "--why", "a different campus on the same land")
    assert done.exit_code == 0, done.output
    with session_scope(engine, commit=False) as session:
        assert (
            session.scalar(select(ProjectAlias).where(ProjectAlias.from_dedup_key == key)) is None
        )
        notes = session.get(Project, pid).notes
        assert "no longer routes to this row" in notes
        assert "a different campus on the same land" in notes
    assert _invoke(db_path, "unalias", key, "--why", "x").exit_code != 0
