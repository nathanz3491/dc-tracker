"""Contradiction checks, and the two ways this feature can be worse than useless.

It can miss a real fault, which is the ordinary failure. Or it can invent one —
and that is the expensive failure, because every finding costs somebody the time
to go and check it. So most of this file is near-misses: cases that look like a
contradiction and are not, which must stay silent.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from tracker import logic
from tracker.models import Event, Project, Risk, Source

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)
TODAY = dt.date(2026, 8, 2)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    """`past_its_own_date` and `milestone_in_the_future` both read the clock."""
    monkeypatch.setattr(logic, "_today", lambda: TODAY)


def _project(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Someone",
        "state": "TX",
        "city": "Abilene",
        "dedup_key": f"k{kwargs.get('name', 'x')}{kwargs.get('company', 'y')}",
        "phase": "construction",
        "confidence": 2,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _event(session, project, event_type: str, when: dt.date) -> None:
    session.add(
        Event(
            project_id=project.id,
            event_type=event_type,
            event_date=when,
            description=f"{event_type} on {when}",
        )
    )
    session.flush()


def _codes(project) -> set[str]:
    return {f.code for f in logic.check_rules(project)}


# --- the numbers ---------------------------------------------------------------


def test_built_capacity_cannot_exceed_planned(session):
    over = _project(session, name="A", mw_planned=32, mw_built=100)
    assert "built_exceeds_planned" in _codes(over)

    ok = _project(session, name="B", mw_planned=100, mw_built=100)
    assert "built_exceeds_planned" not in _codes(ok)


def test_a_date_cannot_precede_the_announcement(session):
    backwards = _project(
        session,
        name="A",
        first_announced=dt.date(2026, 1, 1),
        expected_online=dt.date(2025, 1, 1),
    )
    assert "online_before_announced" in _codes(backwards)

    forwards = _project(
        session,
        name="B",
        first_announced=dt.date(2025, 1, 1),
        expected_online=dt.date(2026, 1, 1),
    )
    assert "online_before_announced" not in _codes(forwards)


# --- the phase against what is underneath it ------------------------------------


def test_a_phase_with_nothing_to_support_it_is_flagged(session):
    """Milestones on other tracks, none on construction, nothing else built."""
    project = _project(session, name="A", phase="construction")
    _event(session, project, "permit_approved", dt.date(2025, 1, 1))
    assert "phase_without_construction" in _codes(project)


def test_a_row_with_no_milestones_at_all_is_a_gap_not_a_contradiction(session):
    """Otherwise the rule fires on every thinly-read project and means nothing.

    Absence of a construction milestone on a row nobody has read anything about is
    not evidence the phase is wrong. `tracker gaps` counts that; this must not.
    """
    project = _project(session, name="A", phase="construction")
    assert "phase_without_construction" not in _codes(project)


@pytest.mark.parametrize("proof", ["energized", "built"])
def test_proof_the_building_exists_silences_the_phase_rule(session, proof: str):
    """A running campus with no groundbreaking on file is unread, not impossible.

    `IMPLIED_BY` deliberately does not fill construction backwards from power, so
    the empty track is expected. Before this exclusion the rule fired on 82 of 221
    live projects — mostly sites whose groundbreaking never made the news.
    """
    project = _project(
        session,
        name="A",
        phase="operational",
        mw_planned=100,
        mw_built=100 if proof == "built" else None,
    )
    _event(session, project, "announced", dt.date(2024, 1, 1))
    if proof == "energized":
        _event(session, project, "energized", dt.date(2025, 1, 1))
    assert "phase_without_construction" not in _codes(project)


def test_energised_in_the_past_contradicts_a_pre_operational_phase(session):
    project = _project(session, name="A", phase="construction")
    _event(session, project, "energized", dt.date(2025, 6, 1))
    findings = {f.code: f for f in logic.check_rules(project)}
    assert findings["energized_but_not_operational"].severity == logic.ERROR


def test_a_cancelled_project_is_not_expected_to_be_operational(session):
    """Terminal phases are a statement about stopping, not about progress."""
    project = _project(session, name="A", phase="cancelled")
    _event(session, project, "energized", dt.date(2025, 6, 1))
    assert "energized_but_not_operational" not in _codes(project)


def test_a_future_milestone_is_a_plan_not_an_achievement(session):
    """`tracks.standing` reads the event type and never its date.

    So an `energized` dated next December counts as reached today and drags the
    whole power track with it. Reported here rather than changed there: what
    "reached" means moves every track strip in the product.
    """
    project = _project(session, name="A", phase="construction")
    _event(session, project, "energized", dt.date(2026, 12, 31))
    codes = _codes(project)
    assert "milestone_in_the_future" in codes
    # And not also reported as contradicting the phase — one fault, one finding.
    assert "energized_but_not_operational" not in codes


def test_a_future_delay_is_not_a_future_milestone(session):
    """`delayed` is news about a project, and a future date is what a slip is."""
    project = _project(session, name="A", phase="construction")
    _event(session, project, "delayed", dt.date(2027, 1, 1))
    assert "milestone_in_the_future" not in _codes(project)


def test_a_date_that_has_passed_without_the_project_arriving(session):
    late = _project(session, name="A", phase="construction", expected_online=dt.date(2024, 1, 1))
    assert "past_its_own_date" in _codes(late)

    # Not flagged once it is running, nor once it is cancelled.
    running = _project(session, name="B", phase="operational", expected_online=dt.date(2024, 1, 1))
    assert "past_its_own_date" not in _codes(running)
    dead = _project(session, name="C", phase="cancelled", expected_online=dt.date(2024, 1, 1))
    assert "past_its_own_date" not in _codes(dead)


def test_an_open_obstacle_on_a_finished_track(session):
    project = _project(session, name="A", phase="construction")
    _event(session, project, "permit_approved", dt.date(2025, 1, 1))
    session.add(
        Risk(
            project_id=project.id,
            category="permitting",
            severity="material",
            summary="zoning still outstanding",
            status="open",
            first_seen=dt.date(2025, 1, 1),
        )
    )
    session.flush()
    assert "obstacle_on_a_finished_track" in _codes(project)


# --- collisions ------------------------------------------------------------------


def _source(
    session,
    project,
    url: str,
    *,
    source_type: str,
    claims: str,
    fetched: dt.datetime,
    fields: str = "",
) -> None:
    session.add(
        Source(
            project_id=project.id,
            url=url,
            source_type=source_type,
            fetched_at=fetched,
            claims=claims,
            fields=fields,
        )
    )
    session.flush()


def test_the_winner_is_reported_with_the_rule_that_chose_it(session):
    """Credibility, and only where credibility is the declared policy.

    `mw_planned` is `prefer_weight`, so a company filing beats trade press.
    """
    project = _project(session, name="A", mw_planned=900)
    _source(
        session,
        project,
        "https://a.test/1",
        source_type="company_filing",
        claims='{"mw_planned": 900}',
        fetched=T0,
        fields="mw_planned",
    )
    _source(
        session,
        project,
        "https://b.test/2",
        source_type="trade_press",
        claims='{"mw_planned": 300}',
        fetched=T0,
        fields="mw_planned",
    )

    collision = next(c for c in logic.check_collisions(project) if c.field == "mw_planned")
    assert collision.winner == 900
    assert collision.loser == 300
    assert collision.decided_by == "credibility"
    assert "weight 3" in collision.why and "weight 2" in collision.why
    assert not collision.stored_disagrees


def test_built_capacity_takes_the_largest_not_the_best_sourced(session):
    """The trap this feature originally fell into.

    `mw_built` is on the MAX policy — energised megawatts only grow, so a weaker
    source reporting more is reporting a later state. Calling this "the better
    source won" would be a plausible sentence and the wrong one, and reporting the
    wrong winner made 73 healthy rows on the live database look like they had
    drifted from their sources.
    """
    project = _project(session, name="A", mw_built=300)
    _source(
        session,
        project,
        "https://a.test/1",
        source_type="company_filing",
        claims='{"mw_built": 100}',
        fetched=T0,
        fields="mw_built",
    )
    _source(
        session,
        project,
        "https://b.test/2",
        source_type="general_media",
        claims='{"mw_built": 300}',
        fetched=T0,
        fields="mw_built",
    )

    collision = next(c for c in logic.check_collisions(project) if c.field == "mw_built")
    assert collision.winner == 300, "the largest wins, even from the weaker source"
    assert collision.decided_by == "largest"
    assert not collision.stored_disagrees


def test_first_announced_takes_the_earliest(session):
    project = _project(session, name="A", first_announced=dt.date(2023, 1, 1))
    _source(
        session,
        project,
        "https://a.test/1",
        source_type="company_filing",
        claims='{"first_announced": "2025-01-01"}',
        fetched=T0,
        fields="first_announced",
    )
    _source(
        session,
        project,
        "https://b.test/2",
        source_type="general_media",
        claims='{"first_announced": "2023-01-01"}',
        fetched=T0,
        fields="first_announced",
    )

    collision = next(c for c in logic.check_collisions(project) if c.field == "first_announced")
    assert collision.decided_by == "earliest"


def test_a_derived_field_is_not_a_collision(session):
    """`blocker` comes from the risk rows; sources claim it only for bookkeeping.

    Comparing the stored blocker against those claims reported two live projects
    as having drifted from their sources when neither had.
    """
    project = _project(session, name="A", blocker="water rights outstanding")
    _source(
        session,
        project,
        "https://a.test/1",
        source_type="company_filing",
        claims='{"blocker": "grid capacity"}',
        fetched=T0,
        fields="blocker",
    )
    _source(
        session,
        project,
        "https://b.test/2",
        source_type="trade_press",
        claims='{"blocker": "permits"}',
        fetched=T0,
        fields="blocker",
    )

    assert [c.field for c in logic.check_collisions(project)] == []


def test_a_row_out_of_step_with_its_own_sources_is_flagged(session):
    project = _project(session, name="A", mw_planned=5)  # nothing claims 5
    _source(
        session,
        project,
        "https://a.test/1",
        source_type="company_filing",
        claims='{"mw_planned": 900}',
        fetched=T0,
        fields="mw_planned",
    )
    _source(
        session,
        project,
        "https://b.test/2",
        source_type="trade_press",
        claims='{"mw_planned": 300}',
        fetched=T0,
        fields="mw_planned",
    )

    collision = next(c for c in logic.check_collisions(project) if c.field == "mw_planned")
    assert collision.stored_disagrees
    assert collision.stored == 5


# --- the model layer --------------------------------------------------------------


def _payload(**over):
    entry = {
        "fields": ["phase", "expected_online"],
        "severity": "error",
        "summary": "phase is operational but expected_online is two years away",
        "evidence": "phase: operational / expected_online: 2028-06-01",
        "confidence": 0.8,
    }
    entry.update(over)
    return {"contradictions": [entry]}


def test_a_grounded_finding_is_kept(session):
    project = _project(session, name="A")
    found = logic.parse_contradictions(project, _payload())
    assert len(found) == 1
    assert found[0].inferred is True
    assert found[0].severity == logic.ERROR
    assert found[0].fields == ("phase", "expected_online")
    assert "confidence 0.8" in found[0].summary


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"fields": ["phase"]}, "one field is an opinion, not a contradiction"),
        ({"fields": ["phase", "phase"]}, "the same field twice is still one field"),
        ({"fields": ["phase", "vibes"]}, "an invented column cannot be checked"),
        ({"evidence": ""}, "unverifiable without the text it relies on"),
        ({"summary": ""}, "nothing to show the reader"),
        ({"confidence": 0.2}, "below the floor, by the model's own account"),
        ({"confidence": "high"}, "unparseable confidence is not a high one"),
    ],
)
def test_an_ungrounded_finding_is_dropped(session, override, why):
    project = _project(session, name="A")
    assert logic.parse_contradictions(project, _payload(**override)) == [], why


def test_a_flood_of_findings_is_capped(session):
    """A row with five contradictions has one problem, not five."""
    project = _project(session, name="A")
    payload = {"contradictions": [_payload()["contradictions"][0] for _ in range(9)]}
    assert len(logic.parse_contradictions(project, payload)) == logic.MAX_PER_PROJECT


@pytest.mark.parametrize("payload", [{}, {"contradictions": None}, {"contradictions": ["x"]}])
def test_a_malformed_reply_yields_nothing_rather_than_raising(session, payload):
    project = _project(session, name="A")
    assert logic.parse_contradictions(project, payload) == []


def test_the_model_is_told_what_the_rules_already_found(session):
    """Otherwise it spends its one call re-deriving them and the reader reads twice."""
    project = _project(session, name="A", mw_planned=32, mw_built=100)
    found = logic.check_rules(project)
    context = logic.build_context(project, found)
    assert "built_exceeds_planned" in context["already"]
    assert "100" in context["mw_built"] and "32" in context["mw_planned"]


class _Reply:
    def __init__(self, text: str, finish_reason: str | None = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.model = "test"


class _Extractor:
    """Returns a canned reply, and records the budget it was asked for."""

    def __init__(self, text: str, finish_reason: str | None = "stop") -> None:
        self._text = text
        self._finish = finish_reason
        self.max_tokens: int | None = None

    def complete(self, *, system, user, max_tokens):
        self.max_tokens = max_tokens
        return _Reply(self._text, self._finish)


def test_the_provider_saying_length_is_believed_over_the_text(session):
    """A reply cut off *after* it closed its reasoning still ran out of room.

    The first version sniffed for an unclosed `<think>` tag, which catches a cut
    mid-reasoning and misses one mid-JSON. Observed live: a reply that had
    finished thinking and was then severed got reported as "unusable JSON", which
    sends the reader to look at the prompt when the answer was the budget.
    """
    project = _project(session, name="A")
    severed = _Extractor(
        '<think>all done</think>{"contradictions": [{"fields": ["phase",',
        finish_reason="length",
    )
    assert logic.examine(project, [], extractor=severed).failure == "truncated"

    # And the tag heuristic still covers a provider that reports nothing.
    silent = _Extractor("<think>cut off mid thought", finish_reason=None)
    assert logic.examine(project, [], extractor=silent).failure == "truncated"


def test_a_reply_cut_off_mid_thought_is_a_failure_not_a_clean_row(session):
    """The expensive confusion this whole distinction exists to prevent.

    A reasoning model spends most of its budget inside `<think>` before writing
    any JSON. Cut it off there and the reply has no closing tag, so the parser
    cannot strip the reasoning, finds no object, and returns nothing — which in a
    sweep is indistinguishable from a row with no contradictions. It is not: the
    call was paid for and thrown away.
    """
    project = _project(session, name="A")
    extractor = _Extractor("<think>Let me look at mw_built versus mw_planned and")
    read = logic.examine(project, [], extractor=extractor)
    assert read.findings == []
    assert read.failure == "truncated"


def test_malformed_json_is_reported_differently_from_truncation(session):
    """They call for opposite responses: a bigger budget, or a look at the prompt."""
    project = _project(session, name="A")
    read = logic.examine(project, [], extractor=_Extractor("<think>done</think> not json at all"))
    assert read.failure == "unusable"


def test_a_complete_reply_is_read(session):
    project = _project(session, name="A")
    body = (
        '<think>reasoning</think>{"contradictions": [{"fields": ["phase", "mw_built"], '
        '"severity": "error", "summary": "x", "evidence": "y", "confidence": 0.9}]}'
    )
    read = logic.examine(project, [], extractor=_Extractor(body))
    assert read.failure is None
    assert len(read.findings) == 1


def test_the_budget_is_large_enough_for_a_reasoning_model(session):
    """2048 truncated every time on a real project; `infer` had already found this."""
    project = _project(session, name="A")
    extractor = _Extractor('{"contradictions": []}')
    logic.examine(project, [], extractor=extractor)
    assert extractor.max_tokens == logic.MAX_TOKENS
    assert logic.MAX_TOKENS >= 4096


def test_a_sweep_counts_what_it_could_not_read(session):
    """Otherwise "read by a model: 200" with no findings reads as 200 clean rows."""
    _project(session, name="A")
    _project(session, name="B")
    report = logic.review(session, extractor=_Extractor("<think>cut off here"))
    assert report.examined == 2
    assert report.unreadable == {"truncated": 2}
    assert ("  unreadable (truncated)", 2) in report.as_rows()


def test_a_drifted_row_is_put_back_and_nothing_else_is_touched(session):
    """The one contradiction a machine may settle, and the proof it settles only that.

    A row drifts when its stored value is no longer what its own citations
    support — a hand edit, or a source attached by a path that did not re-derive.
    Re-running the declared policy is arithmetic. Deciding whether 100 MW built
    against 32 MW planned means a revised plan or a mixed-up phase is not, and
    this must leave that alone.
    """
    drifted = _project(session, name="A", mw_planned=5)  # nothing claims 5
    _source(
        session,
        drifted,
        "https://a.test/1",
        source_type="company_filing",
        claims='{"mw_planned": 900}',
        fetched=T0,
        fields="mw_planned",
    )
    _source(
        session,
        drifted,
        "https://b.test/2",
        source_type="trade_press",
        claims='{"mw_planned": 300}',
        fetched=T0,
        fields="mw_planned",
    )
    # A rule finding with no mechanical answer, which must survive untouched.
    impossible = _project(session, name="B", mw_planned=32, mw_built=100)

    preview = logic.resolve_drift(session, apply=False)
    assert [r.project_id for r in preview] == [drifted.id]
    assert preview[0].changes["mw_planned"] == (5, 900)
    assert drifted.mw_planned == 5, "a preview must not write"

    applied = logic.resolve_drift(session, apply=True)
    assert [r.project_id for r in applied] == [drifted.id]
    assert drifted.mw_planned == 900

    # Idempotent, and the judgement call is still there for a person.
    assert logic.resolve_drift(session, apply=True) == []
    assert "built_exceeds_planned" in _codes(impossible)
    assert impossible.mw_built == 100 and impossible.mw_planned == 32


# --- what a person can do about a finding ---------------------------------------


def test_every_finding_offers_a_way_out(session):
    """A finding with no answer is a complaint, not a tool.

    Each rule must appear in `ACTIONS`, even if the entry is empty — an empty
    tuple is the deliberate statement that accept-or-skip is all there is (the
    phase rule means a milestone was never read, and there is nothing to edit).
    A code missing from the table entirely is an oversight, and this is what
    catches the next rule somebody adds without one.
    """
    project = _project(session, name="A", mw_planned=32, mw_built=100, phase="operational")
    _event(session, project, "energized", dt.date(2025, 1, 1))
    _event(session, project, "energized", dt.date(2027, 1, 1))

    codes = {
        "built_exceeds_planned",
        "online_before_announced",
        "past_its_own_date",
        "operational_without_built_capacity",
        "obstacle_on_a_finished_track",
        "milestone_in_the_future",
        "energized_but_not_operational",
        "phase_without_construction",
    }
    assert codes <= set(logic.ACTIONS), f"no answers offered for {codes - set(logic.ACTIONS)}"

    keys = [a.key for actions in logic.ACTIONS.values() for a in actions]
    assert not ({"v", "s", "q"} & set(keys)), "v/s/q are reserved for accept, skip and quit"
    for code, actions in logic.ACTIONS.items():
        assert len({a.key for a in actions}) == len(actions), f"duplicate key in {code}"


def test_no_action_rewrites_a_row_to_satisfy_a_coarse_phase_enum(session):
    """The three actions that were removed, and why they must not come back.

    A modern campus is several states at once — measured on the live database, 28
    projects are partly built, 15 are `construction` with megawatts already live,
    12 have power energised while construction is mid-track. The `phase` enum
    cannot say that, so the findings it produces there are largely artefacts of
    the schema rather than faults in the data.

    `_set_phase_operational`, `_drop_energized` and `_built_equals_planned`
    silenced those findings by rewriting the row: deleting a real, cited
    energisation milestone; asserting a whole campus was energised because one
    phase was; marking a campus finished while most of it was still being built.
    `tracker logic resolve --llm` could apply any of them unattended.

    The two findings they hung off now offer no action at all, which is the honest
    set of choices until `capacity_block` lands. This test is what stops a future
    reader restoring a convenient one-liner.
    """
    for code in ("energized_but_not_operational", "operational_without_built_capacity"):
        assert logic.ACTIONS[code] == (), (
            f"`{code}` offers an edit again. It is a contradiction between a coarse "
            "phase enum and a correct milestone; rewriting the row to agree with the "
            "enum destroys the milestone."
        )

    # `past_its_own_date` keeps only the honest half: a stale date can be cleared,
    # but a passed phase-1 date is not evidence the whole campus is running.
    assert [a.key for a in logic.ACTIONS["past_its_own_date"]] == ["c"]

    # And nothing anywhere may delete an energisation. It is the single most
    # informative milestone in the dataset and it always carries a citation.
    project = _project(session, name="A", mw_planned=100, mw_built=100, phase="construction")
    _event(session, project, "energized", dt.date(2025, 6, 1))
    for code, actions in logic.ACTIONS.items():
        for action in actions:
            finding = next((f for f in logic.check_rules(project) if f.code == code), None)
            if finding is None:
                continue
            action.apply(session, project, finding)
            assert any(e.event_type == "energized" for e in project.events), (
                f"{code}/{action.key} deleted an energisation milestone"
            )


def test_raising_the_plan_clears_the_contradiction(session):
    project = _project(session, name="A", mw_planned=32, mw_built=100)
    finding = next(f for f in logic.check_rules(project) if f.code == "built_exceeds_planned")
    action = next(a for a in logic.ACTIONS[finding.code] if a.key == "u")

    changed = action.apply(session, project, finding)
    assert project.mw_planned == 100
    assert "32" in changed and "100" in changed
    assert "built_exceeds_planned" not in _codes(project)


def test_dropping_a_future_milestone_clears_the_contradiction(session):
    project = _project(session, name="A", phase="construction")
    _event(session, project, "groundbreaking", dt.date(2025, 1, 1))
    _event(session, project, "energized", dt.date(2027, 6, 1))
    finding = next(f for f in logic.check_rules(project) if f.code == "milestone_in_the_future")

    action = next(a for a in logic.ACTIONS[finding.code] if a.key == "d")
    action.apply(session, project, finding)
    session.flush()

    assert "milestone_in_the_future" not in _codes(project)
    # And it took only the future one.
    assert {e.event_type for e in project.events} == {"groundbreaking"}


def test_closing_an_obstacle_leaves_the_others_open(session):
    project = _project(session, name="A", phase="construction")
    _event(session, project, "permit_approved", dt.date(2025, 1, 1))
    for category in ("permitting", "grid_capacity"):
        session.add(
            Risk(
                project_id=project.id,
                category=category,
                severity="material",
                summary=f"{category} outstanding",
                status="open",
                first_seen=dt.date(2025, 1, 1),
            )
        )
    session.flush()

    finding = next(
        f for f in logic.check_rules(project) if f.code == "obstacle_on_a_finished_track"
    )
    action = next(a for a in logic.ACTIONS[finding.code] if a.key == "r")
    action.apply(session, project, finding)

    states = {r.category: r.status for r in project.risks}
    assert states["permitting"] == "resolved", "the permits track is finished"
    assert states["grid_capacity"] == "open", "power is not, and must be left alone"


def test_a_decision_is_recorded_where_re_ingesting_cannot_erase_it(session):
    """`[tracker]` lines are regenerated on every upsert; operator prose is not.

    Without that distinction the record of a human overruling the data would
    vanish at the next ingest, exactly when somebody asks why the phase says
    operational.
    """
    from tracker.upsert import NOTE_PREFIX, SOURCE_NOTE_PREFIX, _merge_notes

    project = _project(session, name="A")
    logic.record_decision(project, "built_exceeds_planned", "mw_planned 32.0 -> 100.0")
    line = project.notes.splitlines()[-1]
    assert "operator resolved" in line
    assert not line.startswith((NOTE_PREFIX, SOURCE_NOTE_PREFIX))

    survived = _merge_notes(project.notes, derived=["[tracker] fresh"], contributed=[], tag="abc")
    assert line in survived, "an ingest must not erase an operator's decision"

    # Recording the same decision twice does not duplicate the line.
    logic.record_decision(project, "built_exceeds_planned", "mw_planned 32.0 -> 100.0")
    assert project.notes.count("operator resolved") == 1


# --- letting a model choose ------------------------------------------------------


class _Chooser:
    """Returns a canned triage reply."""

    def __init__(self, body: str) -> None:
        self._body = body

    def complete(self, *, system, user, max_tokens):
        self.user = user
        return _Reply(self._body)


def _contradiction(session):
    project = _project(session, name="A", mw_planned=32, mw_built=100)
    finding = next(f for f in logic.check_rules(project) if f.code == "built_exceeds_planned")
    return project, finding


def test_a_confident_grounded_choice_is_taken(session):
    project, finding = _contradiction(session)
    reply = '{"key": "u", "confidence": 0.85, "reason": "the 100 MW figure is quoted"}'
    decision = logic.decide(project, finding, extractor=_Chooser(reply))
    assert decision.acted and decision.key == "u"
    assert decision.confidence == 0.85


def test_declining_is_the_expected_answer_not_a_failure(session):
    """Rule one of the prompt asks the model to skip when unsure.

    The first version reported that correct behaviour as "'s' is not one of the
    options", which made a working model look broken in its own summary — and,
    worse, hid genuinely unusable replies among the sensible declines.
    """
    project, finding = _contradiction(session)
    reply = '{"key": "s", "confidence": 0.9, "reason": "neither figure is quoted"}'
    decision = logic.decide(project, finding, extractor=_Chooser(reply))
    assert not decision.acted
    assert decision.outcome == "declined"
    assert "neither figure" in decision.note


def test_a_model_may_not_mark_a_row_verified(session):
    """ "An operator says this is right" feeds confidence and is not a model's to say."""
    project, finding = _contradiction(session)
    reply = '{"key": "v", "confidence": 0.99, "reason": "looks fine"}'
    decision = logic.decide(project, finding, extractor=_Chooser(reply))
    assert not decision.acted
    assert decision.outcome == "rejected"
    assert "may not verify" in decision.note


@pytest.mark.parametrize(
    ("reply", "outcome", "why"),
    [
        ('{"key": "u", "confidence": 0.4, "reason": "maybe"}', "declined", "below the floor"),
        ('{"key": "z", "confidence": 0.9, "reason": "x"}', "rejected", "not an offered option"),
        ('{"key": "u", "confidence": 0.9, "reason": ""}', "rejected", "no reason to record"),
        ("not json at all", "rejected", "unusable reply"),
    ],
)
def test_an_answer_that_cannot_be_trusted_is_not_applied(session, reply, outcome, why):
    project, finding = _contradiction(session)
    decision = logic.decide(project, finding, extractor=_Chooser(reply))
    assert not decision.acted, why
    assert decision.outcome == outcome, why
    assert project.mw_planned == 32, "nothing may be written on a rejected answer"


def test_the_model_is_shown_the_quotes_and_only_the_real_options(session):
    project, finding = _contradiction(session)
    chooser = _Chooser('{"key": "s", "confidence": 0.9, "reason": "x"}')
    logic.decide(project, finding, extractor=chooser)

    assert re.search(r"mw_built = 100(\.0)?", chooser.user)
    assert re.search(r"mw_planned = 32(\.0)?", chooser.user)
    for action in logic.ACTIONS["built_exceeds_planned"]:
        assert f"  {action.key}  {action.label}" in chooser.user
    assert "\n  v " not in chooser.user, "verify must not be offered to a model"


def test_a_model_decision_is_recorded_as_the_models(session):
    """A row a model edited and a row a person read are different things.

    Six months later the note is the only place that difference survives, so
    writing "operator" over a model's choice would be the most damaging lie this
    file could tell.
    """
    project = _project(session, name="A")
    logic.record_decision(
        project,
        "built_exceeds_planned",
        "mw_planned 32 -> 100",
        by="model (0.72)",
        detail="the 100 MW figure is quoted",
    )
    line = project.notes.splitlines()[-1]
    assert "model (0.72) resolved" in line
    assert "operator" not in line
    assert "the 100 MW figure is quoted" in line


def test_nothing_is_read_unless_an_extractor_is_given(session):
    """The free layers must never make a network call."""
    _project(session, name="A", mw_planned=32, mw_built=100)
    report = logic.review(session)
    assert report.examined == 0
    assert report.findings and report.projects == 1


# --- Rules asked per block ---------------------------------------------------
#
# Four rules were written when a campus was either built and serving customers or
# not built. On a partly-live AI campus they fire on the ordinary shape of the
# thing. Each case below is one that used to be reported as a contradiction and is
# not one.


def _blk(session, project, **kwargs):
    from tracker.models import CapacityBlock

    defaults = {
        "block_key": kwargs.get("label", "b").lower().replace(" ", "-"),
        "label": "Block",
        "status": "planned",
        "generic": False,
    }
    defaults.update(kwargs)
    session.add(CapacityBlock(project_id=project.id, **defaults))
    session.flush()


def _codes(project) -> set[str]:
    return {f.code for f in logic.check_rules(project)}


def test_a_partly_energised_campus_is_not_contradicting_itself(session):
    """All 18 `energized_but_not_operational` findings were this shape."""
    project = _project(session, phase="construction", mw_planned=400.0)
    _event(session, project, "energized", dt.date(2025, 6, 1))
    _blk(session, project, label="Phase 1", mw=100.0, status="energized")
    _blk(session, project, label="Phase 2", mw=300.0, status="under_construction")
    session.refresh(project)
    assert "energized_but_not_operational" not in _codes(project)


def test_a_fully_energised_campus_whose_phase_lags_is_still_reported(session):
    """The rule is narrowed, not deleted: with nothing still going up it is real."""
    project = _project(session, phase="construction", mw_planned=100.0)
    _event(session, project, "energized", dt.date(2025, 6, 1))
    _blk(session, project, label="Phase 1", mw=100.0, status="energized")
    session.refresh(project)
    assert "energized_but_not_operational" in _codes(project)


def test_a_campus_with_no_blocks_keeps_the_old_behaviour(session):
    """No blocks means nothing has been read, not that the row is coherent."""
    project = _project(session, phase="construction", mw_planned=100.0)
    _event(session, project, "energized", dt.date(2025, 6, 1))
    session.refresh(project)
    assert "energized_but_not_operational" in _codes(project)


def test_a_slipped_tranche_is_named_rather_than_the_whole_campus(session):
    project = _project(session, phase="construction", expected_online=dt.date(2024, 1, 1))
    _blk(
        session,
        project,
        label="Phase 1",
        mw=50.0,
        status="under_construction",
        expected_online=dt.date(2024, 1, 1),
    )
    session.refresh(project)
    codes = _codes(project)
    assert "block_past_its_own_date" in codes
    assert "past_its_own_date" not in codes


def test_a_still_future_tranche_explains_a_campus_date_that_passed(session):
    """Phase 1's date is on the row; phase 2 has not happened yet."""
    project = _project(session, phase="construction", expected_online=dt.date(2024, 1, 1))
    _blk(session, project, label="Phase 1", mw=50.0, status="energized")
    _blk(
        session,
        project,
        label="Phase 2",
        mw=50.0,
        status="planned",
        expected_online=dt.date(2099, 1, 1),
    )
    session.refresh(project)
    assert "past_its_own_date" not in _codes(project)


def test_a_running_tranche_with_no_cited_capacity_is_a_missing_citation(session):
    project = _project(session, phase="operational", mw_planned=200.0, mw_built=None)
    _blk(session, project, label="Phase 1", mw=200.0, status="serving", unconfirmed_fields="mw")
    session.refresh(project)
    codes = _codes(project)
    assert "live_block_without_cited_capacity" in codes
    assert "operational_without_built_capacity" not in codes


def test_nested_tranche_labels_are_reported_as_possible_double_counting(session):
    """Riot Rockdale: one AMD lease described at three grains, summed three times."""
    project = _project(session, phase="construction", mw_planned=700.0)
    _blk(session, project, block_key="amd.lease", label="AMD Lease", mw=25.0)
    _blk(session, project, block_key="amd.lease.expansion", label="AMD Lease Expansion", mw=25.0)
    session.refresh(project)
    assert "blocks_may_double_count" in _codes(project)


def test_two_genuinely_separate_tranches_are_not_called_double_counting(session):
    project = _project(session, phase="construction", mw_planned=100.0)
    _blk(session, project, block_key="azp-2", label="AZP-2", mw=48.0)
    _blk(session, project, block_key="azp-3.phase-3", label="AZP-3 Phase 3", mw=8.0)
    session.refresh(project)
    assert "blocks_may_double_count" not in _codes(project)


def test_every_block_rule_is_report_only(session):
    """An automatic edit here would be guessing at the sub-site grain."""
    for code in (
        "block_past_its_own_date",
        "live_block_without_cited_capacity",
        "block_label_ambiguous",
        "blocks_may_double_count",
    ):
        assert logic.ACTIONS[code] == ()


def test_the_same_phase_of_two_campuses_is_not_double_counting(session):
    """`dedup_key` is company|city|state, so one row holds several campuses.

    "AZP-2 Phase 1" and "AZP-3 Phase 1" share a segment and neither contains the
    other. They are two tranches of two facilities, and their megawatts genuinely
    add — reporting them as double-counted would train an operator to ignore the
    rule on exactly the rows it matters for.
    """
    project = _project(session, phase="construction", mw_planned=100.0)
    _blk(session, project, block_key="azp-2.phase-1", label="AZP-2 Phase 1", mw=48.0)
    _blk(session, project, block_key="azp-3.phase-1", label="AZP-3 Phase 1", mw=8.0)
    session.refresh(project)
    assert "blocks_may_double_count" not in _codes(project)


def test_an_energisation_no_tranche_holds_is_reported_as_a_gap_in_the_blocks(session):
    """Measured on SDC Quincy: one block under construction, a cited 2022
    energisation, and nothing to hold it.

    Calling that "the phase is behind the milestone" points at the wrong thing. The
    phase is right; the blocks are incomplete, which is a crawl to do rather than a
    value to change.
    """
    project = _project(session, phase="construction", mw_planned=85.0)
    _event(session, project, "energized", dt.date(2022, 7, 1))
    _blk(session, project, label="Newest Phase", mw=85.0, status="under_construction")
    session.refresh(project)
    codes = _codes(project)
    assert "no_block_for_energisation" in codes
    assert "energized_but_not_operational" not in codes
