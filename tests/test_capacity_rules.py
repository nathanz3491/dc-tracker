"""Planned and running capacity that agree everywhere they are shown (2026-10-21).

The client saw Englewood at "18 MW planned, 6,750 MW running". The digest's
kilowatts-as-megawatts were unquoted, and since 2026-11-17 an unquoted figure never
sets a campus's capacity (`upsert.unsupported`); running capacity never exceeds
planned. Both are applied when a row is derived, and the drift check expects exactly
what they store.
"""

from __future__ import annotations

import datetime as dt

from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.logic import check_collisions
from tracker.upsert import (
    _Claim,
    recompute_from_sources,
    settle_capacity,
    upsert_record,
)

T0 = dt.datetime(2026, 5, 1, 12, 0, 0)


def _claim(value, *, confirmed=True, url="https://example.test/a", tertiary=False):
    return _Claim(value, 2, T0, "trade_press", url, confirmed=confirmed, tertiary=tertiary)


# --- running never exceeds planned ------------------------------------------------


def test_a_planned_figure_below_what_runs_gives_way_to_the_smallest_that_covers_it():
    """IREN Childress: 600 picked by weight, 650 running, 750 quoted beside it."""
    by_field = {
        "mw_planned": [_claim(600.0), _claim(750.0), _claim(1400.0), _claim(75.0)],
        "mw_built": [_claim(650.0)],
    }
    planned, built, notes = settle_capacity(600.0, 650.0, by_field)
    assert (planned, built) == (750.0, 650.0)
    assert notes == [
        "mw_planned 600 -> 750: below the 650 MW running, so it described a phase; "
        "the smallest stated figure covering what runs is used"
    ]


def test_with_nothing_covering_it_planned_is_what_runs():
    by_field = {"mw_planned": [_claim(20.0)], "mw_built": [_claim(152.0)]}
    assert settle_capacity(20.0, 152.0, by_field)[:2] == (152.0, 152.0)


def test_an_unquoted_running_figure_never_overrules_a_quoted_plan():
    by_field = {
        "mw_planned": [_claim(18.0)],
        "mw_built": [_claim(40.0, confirmed=False), _claim(12.0, confirmed=False)],
    }
    planned, built, notes = settle_capacity(18.0, 40.0, by_field)
    assert (planned, built) == (18.0, 12.0)
    assert "it rests on an unquoted report" in notes[0]
    assert settle_capacity(18.0, 40.0, {"mw_planned": [_claim(18.0)], "mw_built": []})[:2] == (
        18.0,
        None,
    )


def test_a_directory_running_figure_gives_way_to_a_first_hand_plan():
    """NVA05: 420 MW running from a directory page about the whole operator, against
    the 200 MW STACK's releases plan, with another campus's 420 on the operator's site."""
    by_field = {
        "mw_planned": [_claim(200.0), _claim(250.0), _claim(420.0, url="https://op.test/nva02")],
        "mw_built": [_claim(420.0, tertiary=True, url="https://directory.test/stack")],
    }
    planned, built, notes = settle_capacity(200.0, 420.0, by_field)
    assert (planned, built) == (200.0, None)
    assert notes == [
        "mw_built 420 -> empty: it rests on a directory's quote, and exceeds the 200 MW "
        "planned that a quoted first-hand source states, so it describes a bigger site"
    ]


def test_figures_equally_supported_mean_the_plan_described_a_phase():
    """Two directories, one planned and one running: the campus has what it runs."""
    by_field = {
        "mw_planned": [_claim(20.0, tertiary=True)],
        "mw_built": [_claim(152.0, tertiary=True)],
    }
    assert settle_capacity(20.0, 152.0, by_field)[:2] == (152.0, 152.0)


def test_a_plan_stated_as_a_minimum_is_not_contradicted_by_more_running():
    """Kuna: "a data center of more than 20 megawatts", and a directory's 152 MW."""
    floor = _Claim(20.0, 2, T0, "trade_press", "https://dck.test/kuna", floor=True)
    by_field = {
        "mw_planned": [floor, _claim(152.0, tertiary=True)],
        "mw_built": [_claim(152.0, tertiary=True)],
    }
    assert settle_capacity(20.0, 152.0, by_field)[:2] == (152.0, 152.0)


def test_a_bare_directory_line_never_raises_a_plan():
    """Covering figures must carry a quote or come first-hand; otherwise what runs."""
    by_field = {
        "mw_planned": [_claim(600.0), _claim(5000.0, confirmed=False, tertiary=True)],
        "mw_built": [_claim(650.0)],
    }
    assert settle_capacity(600.0, 650.0, by_field)[:2] == (650.0, 650.0)


def test_agreeing_figures_are_untouched():
    assert settle_capacity(300.0, 120.0, {}) == (300.0, 120.0, [])
    assert settle_capacity(None, 120.0, {}) == (None, 120.0, [])


# --- applied where rows are derived, and expected by the drift check ----------------


def _record(url: str, claims: dict, unconfirmed=frozenset(), quotes=None):
    base = {"name": "Englewood", "company": "Flexential", "city": "Englewood", "state": "CO"}
    return IngestRecord(
        project={**base, "country": "US"},
        sources=[
            SourceRecord(
                url=url,
                source_type="trade_press",
                fetched_at=T0,
                excerpt="A.",
                claims={**base, **claims},
                unconfirmed=frozenset(unconfirmed),
                quotes=quotes or {},
            )
        ],
    )


def test_englewood_reads_its_quoted_capacity_and_nothing_running(session):
    pid = upsert_record(
        session,
        _record(
            "https://dcd.test/englewood",
            {"mw_planned": 18.0},
            quotes={"mw_planned": "The Englewood site will total 18 MW."},
        ),
    ).project_id
    upsert_record(
        session,
        _record(
            "https://digest.test/flexential",
            {"mw_planned": 11250.0, "mw_built": 6750.0},
            unconfirmed={"mw_planned", "mw_built"},
        ),
    )
    from tracker.models import Project

    project = session.get(Project, pid)
    recompute_from_sources(session, project)
    assert (project.mw_planned, project.mw_built) == (18.0, None)
    drift = [c for c in check_collisions(project) if c.stored_disagrees]
    assert drift == []


def test_a_raised_plan_is_what_the_drift_check_expects(session):
    pid = upsert_record(
        session,
        _record(
            "https://a.test/1",
            {"mw_planned": 600.0, "mw_built": 650.0},
            quotes={"mw_planned": "600 MW planned", "mw_built": "650 MW running"},
        ),
    ).project_id
    upsert_record(
        session,
        _record("https://b.test/2", {"mw_planned": 750.0}, quotes={"mw_planned": "750 MW"}),
    )
    from tracker.models import Project

    project = session.get(Project, pid)
    assert project.mw_planned >= project.mw_built
    assert [c for c in check_collisions(project) if c.stored_disagrees] == []


# --- one running figure on the page ----------------------------------------------


def _tranches(session, built=None):
    """A campus described once whole and again as the building inside it."""
    from tracker.ingest.records import BlockRecord

    claims = {"mw_planned": 1200.0}
    quotes = {"mw_planned": "The campus will total 1,200 MW."}
    if built is not None:
        claims["mw_built"] = built
        quotes["mw_built"] = f"About {built:g} MW is running today."
    base = {"name": "Abilene", "company": "Crusoe", "city": "Abilene", "state": "TX"}
    record = IngestRecord(
        project={**base, "country": "US"},
        sources=[
            SourceRecord(
                url="https://example.test/abilene",
                source_type="trade_press",
                fetched_at=T0,
                excerpt="A.",
                claims={**base, **claims},
                quotes=quotes,
                blocks=[
                    BlockRecord(
                        label="Abilene Campus",
                        mw=1200.0,
                        status="energized",
                        quotes={"mw": "the 1,200 MW Abilene campus is energized"},
                    ),
                    BlockRecord(
                        label="Building 1",
                        mw=200.0,
                        status="serving",
                        quotes={"mw": "Building 1, 200 MW, is serving"},
                    ),
                ],
            )
        ],
    )
    from tracker.models import Project

    return session.get(Project, upsert_record(session, record).project_id)


def test_the_tranche_headline_is_the_campus_running_figure(session):
    """Abilene: cards said 240 MW running, the headline "1,600 MW delivering"."""
    from tracker.export import _accounting_json

    project = _tranches(session, built=240.0)
    got = _accounting_json(project)
    assert got["delivering"] == 240.0
    # What the live tranches describe, kept for the note: the building nests inside the
    # campus-wide tranche (`blocks.structure`), so 1,200 MW once — it read 1,400.
    assert got["live_mw"] == 1200.0


def test_with_no_running_figure_the_live_tranches_count_up_to_the_campus(session):
    from tracker.export import _accounting_json

    project = _tranches(session)
    got = _accounting_json(project)
    assert got["delivering"] == 1200.0


def test_a_plan_only_the_tranches_supply_is_never_below_what_runs(session):
    """Flexential Plano (#26): no plan stated, 18 MW running quoted, and the two
    tranches read summing to 15 MW. It broke "running never exceeds planned" at
    every re-derive; a campus has at least what it runs."""
    from tracker import invariants
    from tracker.ingest.records import BlockRecord
    from tracker.models import Project

    base = {"name": "Plano", "company": "Flexential", "city": "Plano", "state": "TX"}
    record = IngestRecord(
        project={**base, "country": "US"},
        sources=[
            SourceRecord(
                url="https://example.test/plano",
                source_type="trade_press",
                fetched_at=T0,
                excerpt="A.",
                claims={**base, "mw_built": 18.0},
                quotes={"mw_built": "an 18 MW critical load capacity"},
                blocks=[
                    BlockRecord(
                        label="CoreWeave deployment",
                        mw=13.0,
                        status="planned",
                        quotes={"mw": "a 13-megawatt deployment"},
                    ),
                    BlockRecord(
                        label="first phase",
                        mw=2.0,
                        status="planned",
                        quotes={"mw": "a first phase of 2 MW"},
                    ),
                ],
            )
        ],
    )
    project = session.get(Project, upsert_record(session, record).project_id)
    assert (project.mw_planned, project.mw_built) == (18.0, 18.0)
    assert "a campus has at least what it runs" in (project.notes or "")
    assert list(invariants.campus_breaks(project, kind=invariants.HELD)) == []
