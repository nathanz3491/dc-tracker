"""What "clean" means, asserted one condition at a time.

The definition is the product here, not the code, so these tests are written as
statements about the definition. Two of them exist to stop it drifting back to
something unusable: `test_not_applicable_counts_as_complete` (12-of-12 was the
original bar and reported 1,101 of 1,171 rows as broken, a target nothing reaches)
and `test_the_reference_row_reaches_the_top_tier` (the calibration rule — if a
hand-cleaned row cannot score T3, the definition is wrong, not the row).
"""

from __future__ import annotations

import datetime as dt
import json

from tracker import clean
from tracker.models import Project, Risk, Source


def _project(session, **kwargs) -> Project:
    """A row that passes every condition, so each test can break exactly one.

    City and company are keyed off the name so two rows in one test are never
    mistaken for one campus — `capex.suspected_duplicates` buckets by locality, and
    two "Someone" rows in Englewood are a genuine duplicate pair, which would fail
    `duplicates_answered` for reasons the test is not about.
    """
    slug = str(kwargs.get("name", "Reference")).lower().replace(" ", "-")
    defaults = {
        "name": "Reference",
        "company": f"Operator {slug}",
        "city": f"Town {slug}",
        "county": "Arapahoe County",
        "state": "CO",
        "dedup_key": f"clean|{slug}",
        "phase": "construction",
        "confidence": 2,
        "mw_planned": 300.0,
        "mw_built": 100.0,
        "investment_usd": 2_000_000_000,
        "customer": "A Tenant",
        "first_announced": dt.date(2024, 1, 15),
        "expected_online": dt.date(2027, 6, 1),
        "blocker": "transmission upgrades pending",
        "lat": 39.6,
        "lon": -104.9,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _current_stamp() -> str:
    from tracker.prompts import load_prompt

    return load_prompt("extract-v1").stamp


def _source(session, project: Project, **kwargs) -> Source:
    """A citation carrying the CURRENT prompt stamp, so `vintage_current` passes.

    Claims are taken from the row's own values rather than invented: `logic`'s
    `value_without_evidence` rule asks whether a stored figure has a claim behind
    it, so a source that lists a field in `fields` while claiming nothing is a row
    asserting a number no citation supports — which is a real defect, and would
    fail every test here for the wrong reason.
    """
    fields = kwargs.pop(
        "fields",
        "mw_planned,mw_built,investment_usd,phase,customer,first_announced,expected_online",
    )
    names = [f for f in fields.split(",") if f]
    claims = {}
    for name in names:
        value = getattr(project, name, None)
        if value is not None:
            claims[name] = value.isoformat() if hasattr(value, "isoformat") else value
    defaults = {
        "url": kwargs.pop("url", f"https://ref.test/{project.id}"),
        "source_type": "trade_press",
        "fetched_at": dt.datetime(2026, 1, 1),
        "excerpt": "an excerpt",
        "fields": ",".join(claims),
        "claims": json.dumps(claims),
        "quotes": json.dumps({name: f"the article states {name}" for name in claims}),
        "extractor": f"crawl:{_current_stamp()}:model:httpx",
    }
    defaults.update(kwargs)
    source = Source(project=project, **defaults)
    session.add(source)
    session.flush()
    return source


def _card(session, project) -> clean.CleanCard:
    return clean.card(session, project)


# --- the tiers themselves ----------------------------------------------------


def test_the_reference_row_reaches_the_top_tier(session):
    """The calibration rule. If a fully-answered row cannot reach T3, the
    definition is wrong and `tracker/clean.py` changes — not the row.

    Without this the tiers can quietly become unreachable, which is exactly what
    12-of-12 was: a bar 97% of the database failed and nobody could use.
    """
    project = _project(session)
    _source(session, project)
    got = _card(session, project)

    assert got.failed == (), f"unexpected failures: {[c.key for c in got.failed]}"
    assert got.tier == 3
    assert got.label == "SETTLED"
    assert got.blocking == ()


def test_tiers_are_cumulative(session):
    """A row failing a T0 condition can never report T2, however much else passes.

    The histogram is only readable if the tiers nest; a row that scored 2 on the
    strength of late conditions while failing an early one would make "at or above
    T1" a lie.
    """
    project = _project(session)
    _source(session, project)
    project.mw_built = 400.0  # built exceeds planned: a logic ERROR
    session.flush()

    got = _card(session, project)
    assert got.by_key["no_errors"].ok is False
    assert got.tier == -1, "failing T0 means no tier at all"
    assert got.label == "UNSOURCED"


def test_a_row_nothing_cites_is_not_even_sourced(session):
    project = _project(session)
    got = _card(session, project)
    assert got.by_key["has_source"].ok is False
    assert got.tier == -1


def test_reference_data_alone_does_not_count_as_a_citation(session):
    """A Census geocode confirms a county; it says nothing about a campus.

    `confidence.compute` already refuses to let a `derived:` row corroborate
    anything, and this is the same rule applied to the question "is this row
    sourced at all".
    """
    project = _project(session)
    _source(session, project, extractor="derived:census-place-2020", fields="county")
    got = _card(session, project)
    assert got.by_key["has_source"].ok is False


# --- the condition that was nearly defined wrong -----------------------------


def test_not_applicable_counts_as_complete(session):
    """`mw_built` on an announced project is correctly null.

    Treating every one of the twelve fields as required made 1,101 of 1,171 rows
    fail, and a target nothing reaches is a target nobody uses. `gaps.for_project`
    already distinguishes NOT_APPLICABLE from MISSING; this honours it.
    """
    project = _project(session, name="Announced", phase="announced", mw_built=None)
    _source(session, project)

    got = _card(session, project)
    assert got.by_key["fields_present"].ok is True, got.by_key["fields_present"].detail
    assert got.tier == 3


def test_fields_that_are_usually_absent_are_not_counted_against_a_row(session):
    """`blocker` and `customer` are `gaps.UNMEASURABLE` — absence is usually the
    truth, so a null must not read as a backlog item."""
    project = _project(session, name="No tenant", customer=None, blocker=None)
    _source(session, project, fields="mw_planned,mw_built,investment_usd,phase")

    got = _card(session, project)
    assert got.by_key["fields_present"].ok is True


def test_a_missing_measurable_field_blocks_completeness_only(session):
    """It stops T2, not T1: an incomplete row makes a total smaller, and that is a
    different kind of wrong from a row carrying a bad figure."""
    project = _project(session, name="No capex", investment_usd=None)
    _source(session, project, fields="mw_planned,mw_built,phase,customer")

    got = _card(session, project)
    assert got.by_key["fields_present"].ok is False
    assert "investment_usd" in got.by_key["fields_present"].detail
    assert got.tier == 1, "still SOUND — nothing it does assert is a lie"


# --- one test per remaining condition ----------------------------------------


def test_an_implausible_capacity_stops_soundness(session):
    """The Hyperion case. 14,462 MW is beyond any announced campus, and a row
    carrying it makes every total that includes it wrong."""
    project = _project(session, name="Too big", mw_planned=14_462.0)
    _source(session, project)

    got = _card(session, project)
    assert got.by_key["audit_clear"].ok is False
    assert "campus_exceeds_worlds_largest" in got.by_key["audit_clear"].detail
    assert got.tier == 0


def test_a_settled_audit_finding_no_longer_blocks(session):
    """Answering the question is what raises the tier — the point of the campaign."""
    from tracker import audit

    # Capex scaled to the capacity, or `usd_per_mw_out_of_band` fires too and the
    # test would be asserting about a different finding than it names.
    project = _project(
        session, name="Genuinely big", mw_planned=9_000.0, investment_usd=90_000_000_000
    )
    _source(session, project)
    assert _card(session, project).by_key["audit_clear"].ok is False

    audit.record(project, "campus_exceeds_worlds_largest", "left as it stands", by="operator")
    session.flush()
    assert _card(session, project).by_key["audit_clear"].ok is True


def test_an_unconfirmed_obstacle_stops_the_top_tier(session):
    project = _project(session)
    source = _source(session, project)
    session.add(
        Risk(
            project=project,
            category="water",
            severity="watch",
            status="open",
            summary="cooling water may be constrained",
            unconfirmed="quote_off_target",
            source_id=source.id,
        )
    )
    session.flush()

    got = _card(session, project)
    assert got.by_key["risks_confirmed"].ok is False
    assert got.tier == 2
    assert [c.key for c in got.blocking] == ["risks_confirmed"]


def test_a_resolved_obstacle_does_not_count(session):
    """Only open obstacles are questions. A superseded one is history."""
    project = _project(session)
    source = _source(session, project)
    session.add(
        Risk(
            project=project,
            category="water",
            severity="watch",
            status="superseded",
            summary="was never really an obstacle",
            unconfirmed="quote_off_target",
            resolved_at=dt.datetime(2026, 2, 1),
            source_id=source.id,
        )
    )
    session.flush()
    assert _card(session, project).by_key["risks_confirmed"].ok is True


def test_a_superseded_prompt_stops_the_top_tier(session):
    """Every gate improvement applies only to rows written after it landed, so a
    row read by an older prompt is a row nobody has re-checked."""
    project = _project(session, name="Old read")
    _source(session, project, extractor="crawl:extract-v1@deadbeef:model:httpx")

    got = _card(session, project)
    assert got.by_key["vintage_current"].ok is False
    assert "extract-v1@deadbeef" in got.by_key["vintage_current"].detail


# --- the worklist ------------------------------------------------------------


def test_the_worklist_puts_the_closest_rows_first(session):
    """One command away beats eight, for the reason `enrich.select_projects` gives:
    the run is judged on how many rows clear the bar."""
    near = _project(session, name="Near", mw_planned=14_462.0)
    _source(session, near)
    far = _project(session, name="Far", mw_planned=14_462.0, mw_built=99_999.0, customer=None)

    sweep = clean.scan(session)
    ordered = [c.project_id for c in clean.worklist(sweep, tier=1)]
    assert ordered.index(near.id) < ordered.index(far.id)


def test_the_sweep_counts_every_failure_and_every_tier(session):
    good = _project(session, name="Good")
    _source(session, good)
    bad = _project(session, name="Bad", mw_planned=14_462.0)
    _source(session, bad)

    sweep = clean.scan(session)
    assert len(sweep.cards) == 2
    assert sweep.failures.get("audit_clear") == 1
    assert sweep.at_or_above(3) == 1
    assert sweep.as_json()["projects"] == 2


def test_blocking_names_only_the_next_tier(session):
    """A row at T0 with eight failures needs the three that reach T1, not eight
    commands. Ordering the work is most of what makes 1,171 rows tractable."""
    project = _project(session, name="Several problems", mw_planned=14_462.0, investment_usd=None)
    _source(session, project, fields="mw_planned,phase")

    got = _card(session, project)
    assert {c.key for c in got.failed} > {c.key for c in got.blocking}
    assert [c.key for c in got.blocking] == ["audit_clear"]


def test_every_condition_has_a_remedy(session):
    """A scorecard naming a failure an operator cannot act on is a complaint."""
    project = _project(session)
    _source(session, project)
    keys = {c.key for c in _card(session, project).conditions}
    assert keys == set(clean.REMEDIES), "a condition without a remedy, or the reverse"
    for _level, _label, tier_keys in clean.TIERS:
        assert set(tier_keys) <= keys


# --- basics: the fields that say what a project IS ----------------------------
#
# Reported, but deliberately not a tier condition — `capex.suspected_duplicates`
# set that precedent, because a new tier condition moves every row's tier in one
# commit and buries the signal it exists to raise. The tests below assert both
# halves: that the condition fires on the right rows, and that it moves nobody.


def test_basics_passes_on_a_row_that_knows_what_it_is(session):
    project = _project(session, name="Basics Complete")
    _source(session, project)
    assert _card(session, project).by_key["basics_defined"].ok


def test_basics_catches_a_capacity_nobody_has_stated(session):
    """The presence half. Three of the eight basic fields can actually be null."""
    project = _project(session, name="No Capacity", mw_planned=None)
    _source(session, project)

    condition = _card(session, project).by_key["basics_defined"]
    assert not condition.ok
    assert "mw_planned" in condition.detail


def test_basics_catches_a_phase_nobody_ever_sourced(session):
    """The provenance half, and the reason presence alone cannot be the check.

    `phase` is NOT NULL with a server default, so a row nobody has ever described
    still reads `announced` — and presents it as though a source had said so.
    `gaps.provenance` already calls this DEFAULTED; nothing failed a row for it.
    """
    project = _project(session, name="Defaulted Phase", phase="announced")
    # A citation that asserts everything EXCEPT the phase, so the value standing in
    # the column came from the schema rather than from anybody.
    _source(session, project, fields="mw_planned,mw_built,investment_usd")

    condition = _card(session, project).by_key["basics_defined"]
    assert not condition.ok
    assert "never sourced" in condition.detail
    assert "phase" in condition.detail


def test_a_defaulted_country_is_not_a_defect(session):
    """`US` arrives by default on essentially every row and is correct.

    No trade-press article about a campus in Ohio says it is in the United States,
    so demanding a citation would fail the whole database for being right. The
    hand-cleaned reference row caught this the first time it was asked.
    """
    project = _project(session, name="Default Country", country="US")
    _source(session, project)

    condition = _card(session, project).by_key["basics_defined"]
    assert condition.ok, condition.detail


def test_basics_is_not_a_tier_condition(session):
    """Structural, so the guarantee cannot be lost by editing TIERS.

    `capex.suspected_duplicates` gives the reason it must stay out: adding one
    "would move every row's tier in a single commit and bury the signal it exists
    to raise".
    """
    assert "basics_defined" not in {key for _l, _n, keys in clean.TIERS for key in keys}


def test_a_row_failing_only_basics_keeps_its_tier(session):
    """A defaulted phase is the case that isolates it: the value is *present*, so
    `fields_present` passes, and only `basics_defined` objects."""
    project = _project(session, name="Tier Unmoved", phase="announced", mw_built=None)
    _source(
        session,
        project,
        fields="mw_planned,investment_usd,customer,first_announced,expected_online",
    )
    card = _card(session, project)

    assert not card.by_key["basics_defined"].ok, "the row must be failing basics"
    demoted_by = [c.key for c in card.failed if c.key != "basics_defined"]
    assert card.tier == 3, (
        f"a non-tier condition must not demote anything; also failed {demoted_by}"
    )
    assert "basics_defined" not in {c.key for c in card.blocking}


def test_a_null_that_is_correct_is_not_a_basics_gap(session):
    """`mw_built` on an announced project is right, so it is not a missing basic."""
    project = _project(session, name="Nothing Built", phase="announced", mw_built=None)
    _source(session, project, fields="mw_planned,phase,investment_usd")

    condition = _card(session, project).by_key["basics_defined"]
    assert "mw_built" not in condition.detail


def test_the_worklist_puts_the_cheapest_rows_first(session):
    """Fewest gaps first, for the reason `select_projects` gives: a bounded budget
    is judged on how many rows clear the bar."""
    far = _project(session, name="Far", mw_planned=None, mw_built=None, expected_online=None)
    near = _project(session, name="Near", expected_online=None)
    done = _project(session, name="Done")
    for row in (far, near, done):
        _source(session, row)

    order = clean.basics_worklist(session)
    assert order.index(near.id) < order.index(far.id), "one gap before three"
    assert done.id not in order, "a row that knows what it is needs nothing"


def test_the_worklist_and_the_condition_cannot_disagree(session):
    """One implementation, two callers. A second copy would drift."""
    short = _project(session, name="Short", mw_planned=None)
    _source(session, short)
    whole = _project(session, name="Whole")
    _source(session, whole)

    listed = set(clean.basics_worklist(session))
    for project in (short, whole):
        fails = not _card(session, project).by_key["basics_defined"].ok
        assert (project.id in listed) == fails, f"#{project.id} disagrees"
