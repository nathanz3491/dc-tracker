"""Contradictions inside a project row, and which side of one should win.

Every other check in this project asks "is this value supported?". This one asks a
different question: **do the supported values agree with each other?** A row can be
entirely well-cited and still be impossible — `phase = operational` on a campus
whose construction track has not reached a single milestone is either the wrong
phase or a missing milestone, and both citations behind it can be perfectly good.

Three layers, in order of what they cost.

**1. Rules.** Deterministic contradictions computable from the row, its milestones
and its obstacles. These need no model and must not have one: paying an LLM to
notice that `mw_built > mw_planned` is paying for arithmetic, and a rule states its
reasoning in a way anybody can check.

**2. Collisions.** Two sources claiming different values for one field. The winner
is *not* decided here and is not decided by a model — `upsert.resolve_field` is
asked, because that is the function the write path used, and this layer reports the
answer with the reason.

That reason is **not always "the better source won"**, and assuming it was is the
mistake this module was built on. Each field has a declared policy: built capacity
takes the largest figure because energised megawatts only go up, `first_announced`
the earliest because that is what "first" means, `phase` the furthest along unless
a source says it stopped, and the identity fields are never overwritten once set.
Only the remainder are settled by credibility and then recency. Re-deriving the
winner as though credibility always decided it reported 73 of 221 live rows as
having drifted from their sources; none of them had. A tool that invents 73 faults
is worse than one that finds none.

**3. Judgement.** Contradictions a rule cannot express — a blocker that describes a
resolved problem, a customer that is plainly the builder. That needs reading, so it
is the only layer that costs money, it is opt-in, and everything it returns is
tagged as a model's opinion. It is *not* allowed to pick a collision winner: which
of two cited numbers is right is a question about sources, and sources have weights
and dates precisely so that nobody has to guess.

Nothing here writes. A contradiction is a question for a person — `tracker review`
is where answers go, and `tracker merge` is where duplicate rows go. The one thing
this does that looks like a fix is telling you when a stored value no longer matches
what its own sources support, which `upsert.recompute_from_sources` resolves.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.dedup import looks_like_county
from tracker.models import Project
from tracker.normalize import is_blank
from tracker.tracks import RISK_TRACK, TRACK_MILESTONES, standing
from tracker.vocab import (
    BLOCK_LIVE,
    BLOCK_TERMINAL,
    OPEN_RISK_STATUS,
    PHASE_TERMINAL,
    TRACKED_FIELDS,
)

log = logging.getLogger(__name__)

#: A contradiction that cannot be explained away: the two values cannot both be
#: true of the same project at the same time.
ERROR: Final = "error"
#: Probably wrong, legitimately possible. A speculative build really can be under
#: construction with no customer; a plan really can be revised.
WARNING: Final = "warning"

#: Phases that assert the building exists and is running.
_LIVE_PHASES: Final[frozenset[str]] = frozenset({"operational"})
#: Phases that assert physical work has started.
_BUILDING_PHASES: Final[frozenset[str]] = frozenset({"construction", "operational"})

#: Event types that are milestones on a track. `delayed` and `expanded` are news
#: about a project rather than a rung on a ladder, and a future-dated `delayed` is
#: exactly what a slipped date looks like — flagging it would be wrong.
_MILESTONE_TYPES: Final[frozenset[str]] = frozenset(
    m for ladder in TRACK_MILESTONES.values() for m in ladder
)


@dataclass(frozen=True)
class Finding:
    """One contradiction, stated so a reader can check it without the code."""

    project_id: int
    code: str
    severity: str
    #: One line, naming both sides. This is what gets printed.
    summary: str
    #: The fields involved, for filtering and for the UI to highlight.
    fields: tuple[str, ...] = ()
    #: Where the reader should look, when there is a better answer than "both".
    remedy: str = ""
    #: True when a model produced it rather than a rule. Never presented as fact.
    inferred: bool = False
    #: What, specifically, this finding is about — `risk:water`, `track:permits`,
    #: `event:energized:2027-06-01`. Tokens, not prose.
    #:
    #: Added because the triage prompt was blind without it. A finding about an
    #: open `grid_capacity` obstacle on a finished power track declares
    #: `fields=("blocker",)`, so the evidence assembled for the model was one
    #: quote behind the derived `blocker` string — a sentence about something
    #: else entirely. The model read it and correctly answered "the provided
    #: quote addresses grid service approval, not the turbines", which looked
    #: like a stubborn model and was a prompt with the wrong page open. These
    #: tokens are what `_triage_context` uses to put the *right* evidence in
    #: front of it: the obstacle's own quote, and the milestone that is supposed
    #: to contradict it.
    subjects: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "fields": list(self.fields),
            "remedy": self.remedy,
            "inferred": self.inferred,
            "subjects": list(self.subjects),
        }

    @property
    def identity(self) -> tuple[Any, ...]:
        """What makes two findings the same finding.

        Duplicate `risk` rows produce duplicate findings — one campus had the
        same `community_opposition` obstacle stored twice under different
        `first_seen` dates, so `tracker logic resolve --llm` offered the identical
        question four times and the first answer had already closed all of them.
        Deduplicating on the sentence is enough: two findings that say the same
        thing about the same row are one question.
        """
        return (self.project_id, self.code, self.summary)


@dataclass(frozen=True)
class Collision:
    """One field, two sources, two different values, and who wins."""

    project_id: int
    field: str
    winner: Any
    loser: Any
    winner_url: str
    loser_url: str
    winner_type: str
    loser_type: str
    winner_weight: int
    loser_weight: int
    winner_fetched: dt.datetime | None
    loser_fetched: dt.datetime | None
    #: Which rule decided it: "confirmed", "credibility", "recency", "tiebreak",
    #: "largest", "earliest", "furthest along", "terminal" or "first seen".
    decided_by: str
    #: The field's declared merge policy, named so the reason is checkable.
    policy: str = "prefer_weight"
    #: True when the value on the project row is not the winner — meaning the row
    #: has drifted from its own sources and a recompute would move it.
    stored_disagrees: bool = False
    stored: Any = None

    @property
    def why(self) -> str:
        """The reason in the reader's terms, and it is not always credibility.

        Only fields on the `prefer_weight` policy are settled by how good the
        source is and how recent it is. Built capacity takes the largest figure
        because energised megawatts only go up; the first-announced date takes the
        earliest because that is what "first" means; the phase takes the furthest
        along unless somebody says it stopped. Reporting all of them as "the better
        source won" would be a plausible sentence and the wrong one.
        """
        return why_decided(
            self.decided_by,
            winner_type=self.winner_type,
            winner_weight=self.winner_weight,
            loser_type=self.loser_type,
            loser_weight=self.loser_weight,
            winner_fetched=self.winner_fetched,
            loser_fetched=self.loser_fetched,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "field": self.field,
            "winner": self.winner,
            "loser": self.loser,
            "winner_url": self.winner_url,
            "loser_url": self.loser_url,
            "decided_by": self.decided_by,
            "why": self.why,
            "stored_disagrees": self.stored_disagrees,
            "stored": self.stored,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)
    #: Projects examined, so a count of findings has a denominator.
    projects: int = 0
    #: Projects sent to a model, when the judgement layer ran.
    examined: int = 0
    #: Projects whose evidence was audited, when that layer ran.
    audited: int = 0
    #: Of those, the ones whose reply could not be used — reason -> count. Paid
    #: for and thrown away, so it belongs in the summary and not only in the log.
    unreadable: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("projects checked", self.projects),
            ("contradictions", len(self.findings)),
            ("  of which impossible", len(self.errors)),
            ("source collisions", len(self.collisions)),
            ("  row disagrees with sources", sum(1 for c in self.collisions if c.stored_disagrees)),
            ("read by a model", self.examined),
            ("evidence audited", self.audited),
            *[(f"  unreadable ({reason})", n) for reason, n in sorted(self.unreadable.items())],
        ]


def _day(when: dt.datetime | None) -> str:
    return str(when)[:10] if when else "undated"


# --- Layer 1: rules -----------------------------------------------------------


def _reached(state) -> set[str]:
    return set(state.reached)


def _nested_blocks(blocks: list[Any]) -> list[tuple[str, str]]:
    """Pairs where one block's label is contained in another's. `(inner, outer)`.

    Measured on Riot Rockdale, which came back as "AMD Lease" 25 MW, "AMD Lease
    Initial Deployment" 25 MW and "AMD Lease Expansion" 25 MW — one lease described
    at three grains. `block_key` correctly says those are three different strings;
    what it cannot know is that summing them counts the same megawatts three times.

    Deliberately a *report*, not a merge. Sometimes "Phase 1" and "Phase 1
    Remainder" really are the whole and a part; sometimes they are two tranches an
    operator named badly. Only a person can say which, and folding them silently is
    the failure mode `dedup.py` argues against at project grain.
    """
    # Compared as sets of segments rather than as prefixes, because the extra words
    # can arrive anywhere: "AMD Lease Expansion" appends, "Initial AMD Capacity"
    # prepends. Two distinct keys with *identical* segment sets would differ only in
    # order, which `block_key` builds deterministically, so the strict-subset test
    # below cannot be loosened by any input this codebase can produce.
    keyed = [(b.block_key, set(b.block_key.split("."))) for b in blocks]
    out: list[tuple[str, str]] = []
    for inner, inner_parts in keyed:
        for outer, outer_parts in keyed:
            if inner == outer:
                continue
            # Strict superset: every segment of the outer key appears in the inner
            # one, so the inner names the outer plus something more.
            if outer_parts < inner_parts:
                out.append((inner, outer))
    return sorted(set(out))


#: Scalars checked against the evidence beneath them. Money and megawatts only:
#: these are the fields that feed `tracker capex` and the national totals, so a
#: stored figure nothing supports is a figure that misstates a rollup.
_EVIDENCE_BACKED_FIELDS: Final[tuple[str, ...]] = ("mw_built", "mw_planned", "investment_usd")

_UNITS: Final[dict[str, str]] = {
    "mw_built": "MW built",
    "mw_planned": "MW planned",
    "investment_usd": "USD invested",
}


def _check_stored_against_evidence(project: Project) -> list[Finding]:
    """Stored scalars that the row's own citations cannot account for.

    `check_collisions` already asks a version of this — `stored_disagrees` means
    the row drifted from its sources — but only *inside a collision*, and a
    collision needs two or more claims on one field. So the cheapest shape of the
    error is invisible to it: **one claim, and the row does not match it.**

    Observed live on Stargate Abilene (#3), which read `mw_built = 1200` while the
    only `mw_built` claim on the row was a well-quoted 200. The 1.2 GW quotes had
    been re-extracted as `mw_planned`, correctly — "committed capacity" is not
    energised capacity — but `mw_built` is policy MAX and `_resolve` counts the
    stored value among the candidates, so **MAX can never come back down.** Once
    written, the figure outlived the claim that produced it. 1,000 MW, against
    HTI's ~0.4 GW satellite read and our own `phase-1` block of 200 MW serving.

    Two things can legitimately support a scalar, and both are consulted:

    * the field's own claims, resolved by the same policy the write path uses; and
    * the **block rollup**, because `blocks.reconcile` deliberately raises a campus
      scalar to the sum of its tranches. Ignoring it reported 28 false positives.

    Deliberately *not* an ERROR. Nothing here is arithmetically impossible — the
    row may be right and the extraction stale — so this is a question for a person,
    which is what WARNING means everywhere else in this module.
    """
    from tracker import blocks as blocks_mod
    from tracker.confidence import values_conflict
    from tracker.upsert import claims_by_field, resolve_field

    sources = list(getattr(project, "sources", ()) or ())
    if not sources:
        return []
    by_field = claims_by_field(sources)
    stored_blocks = list(getattr(project, "blocks", ()) or ())
    roll = blocks_mod.rollup(stored_blocks) if stored_blocks else None

    out: list[Finding] = []
    for name in _EVIDENCE_BACKED_FIELDS:
        stored = getattr(project, name, None)
        if not isinstance(stored, (int, float)):
            continue
        from_claims = resolve_field(name, by_field.get(name, []), None)
        from_blocks = getattr(roll, name, None) if roll is not None else None
        supported = [v for v in (from_claims, from_blocks) if isinstance(v, (int, float))]
        unit = _UNITS[name]

        if not supported:
            out.append(
                Finding(
                    project_id=project.id,
                    code="value_without_evidence",
                    severity=WARNING,
                    summary=(
                        f"{stored:g} {unit} is stored, and no source on this row claims "
                        f"{name} at all"
                    ),
                    fields=(name,),
                    remedy=(
                        "find the citation and record it, or clear the value — it is "
                        "counted in every total that reads this field"
                    ),
                )
            )
            continue

        ceiling = max(supported)
        # `values_conflict` rather than `!=`, so rounding between a quote and a
        # stored figure is not reported as drift.
        if stored > ceiling and values_conflict(stored, ceiling):
            out.append(
                Finding(
                    project_id=project.id,
                    code="value_above_its_evidence",
                    severity=WARNING,
                    summary=(
                        f"{stored:g} {unit} is stored, but the citations support at most "
                        f"{ceiling:g}"
                    ),
                    fields=(name,),
                    remedy=(
                        "usually a figure that outlived the claim behind it — MAX fields "
                        "never come back down on their own; confirm and correct in "
                        "`tracker review`"
                    ),
                )
            )
    return out


def check_rules(project: Project) -> list[Finding]:
    """Every deterministic contradiction on one row.

    Ordered roughly by how much a reader should care. Each rule names both sides,
    because "this project has a problem" is not actionable and "phase says
    operational, construction shows nothing" is.
    """
    out: list[Finding] = []
    events = list(getattr(project, "events", ()) or ())
    risks = list(getattr(project, "risks", ()) or ())
    stand = standing(project.id, events, risks)
    by_track = {s.track: s for s in stand.tracks}
    phase = (project.phase or "").lower()
    today_ = _today()

    # --- what the blocks say, which several rules below must ask before firing ---
    #
    # Four of these rules were written when a campus was either built and serving
    # customers or not built. On a modern AI campus — part energised and serving one
    # buyer, part still going up — they fire on the ordinary shape of the thing and
    # tell an operator to "fix" data that is correct. That was 18 of 144 findings
    # outright and a share of another 45.
    #
    # So each of them now asks the blocks first. A campus that is *partly* live is
    # not contradicting itself, and saying so is the whole reason blocks exist. A
    # campus with no blocks keeps exactly today's behaviour, because the absence of
    # blocks is a gap in what has been read and not evidence of coherence.
    blocks = list(getattr(project, "blocks", ()) or ())
    live_blocks = [b for b in blocks if b.status in BLOCK_LIVE]
    pending_blocks = [b for b in blocks if b.status not in BLOCK_LIVE | set(BLOCK_TERMINAL)]
    partly_live = bool(live_blocks and pending_blocks)

    def add(
        code: str,
        severity: str,
        summary: str,
        fields: tuple[str, ...],
        remedy: str = "",
        subjects: tuple[str, ...] = (),
    ) -> None:
        out.append(
            Finding(
                project_id=project.id,
                code=code,
                severity=severity,
                summary=summary,
                fields=fields,
                remedy=remedy,
                subjects=subjects,
            )
        )

    # --- the phase against the milestones underneath it ----------------------
    #
    # Only checked on projects that have *some* milestone recorded. On a row with
    # no events at all the construction track is empty because nothing has been
    # read, not because the project contradicts itself — that is a gap, and
    # `tracker gaps` is where gaps are counted. Applying the check to those rows
    # would flag most of the database and mean nothing.
    power = by_track.get("power")
    energized = bool(power and "energized" in _reached(power))

    # Skipped when something else already proves the building exists. A running
    # campus with an `energized` milestone and no `groundbreaking` is a gap in what
    # we read, not a contradiction — and `IMPLIED_BY` deliberately does not fill
    # construction backwards from power, so the empty track is expected rather than
    # suspicious. Without this exclusion the rule fired on 82 of 221 projects,
    # most of them running sites whose groundbreaking simply never made the news.
    if events and phase in _BUILDING_PHASES and not energized and project.mw_built is None:
        construction = by_track.get("construction")
        if construction and not _reached(construction):
            add(
                "phase_without_construction",
                WARNING,
                f"phase is {phase}, but nothing says the building exists — no "
                f"construction milestone, no built capacity, no energised power, "
                f"and {len(events)} milestone(s) recorded on other tracks",
                ("phase",),
                "confirm the phase against a source, or record the missing milestone",
            )

    # A milestone dated in the future is a plan, not an achievement.
    #
    # `tracks.standing` reads only the *type* of an event, never its date, so an
    # `energized` dated next December counts as reached today and pulls the whole
    # power track with it. That is a reasonable simplification for a milestone
    # somebody reported as done; it is wrong for one a source described as
    # scheduled. Reported here rather than fixed in `tracks.py`, because changing
    # what "reached" means would move every track strip in the product and that is
    # not a decision this command gets to take on its own.
    ahead = sorted(
        {
            (e.event_type, e.event_date)
            for e in events
            if e.event_date and e.event_date > today_ and e.event_type in _MILESTONE_TYPES
        }
    )
    if ahead:
        listed = ", ".join(f"{name} on {when}" for name, when in ahead[:3])
        add(
            "milestone_in_the_future",
            WARNING,
            f"{len(ahead)} milestone(s) are dated ahead of today — {listed}",
            ("phase",),
            "these count as reached on the track strip; a scheduled date belongs in "
            "expected_online, not in a milestone",
            subjects=tuple(f"event:{name}:{when}" for name, when in ahead),
        )

    if phase in _LIVE_PHASES and project.mw_built is None and project.mw_planned:
        if live_blocks:
            # Not a contradiction. Something *does* say part of it is running — the
            # block says so — and `mw_built` is empty because that block's capacity
            # arrived without a quote naming it, which `rollup` refuses to sum. The
            # remedy is a citation for a known tranche, not a phase to step back.
            named = ", ".join(sorted(b.label for b in live_blocks)[:3])
            add(
                "live_block_without_cited_capacity",
                WARNING,
                f"{len(live_blocks)} block(s) are running ({named}) but none carries a "
                "capacity any quote confirms, so mw_built stays empty",
                ("mw_built",),
                "find a source that states the megawatts of that tranche",
            )
        else:
            add(
                "operational_without_built_capacity",
                WARNING,
                f"phase is operational and {project.mw_planned:g} MW is planned, but no "
                "source says any of it is built",
                ("phase", "mw_built"),
                "a live campus has energised capacity; find it or step the phase back",
            )

    # The sibling gap, observed live on Fairwater (#1): `mw_built` IS set, so the
    # branch above stays silent — but every running tranche carries either no
    # capacity at all or a 待确认 one `rollup` refuses to sum. The console then
    # prints "0 MW delivering of 350 MW" directly above an energized 350, and the
    # events say the site came online in April. Nothing was wrong *inside* any one
    # of those statements; the contradiction only exists between the scalar, the
    # blocks and the events, which is precisely the kind this module exists to
    # name. The remedy is the same citation hunt as its sibling — never summing
    # the 待确认 figure to make the zero go away.
    if phase in _LIVE_PHASES and project.mw_built and live_blocks:
        from tracker.blocks import mw_is_confirmed

        if not any(b.mw is not None and mw_is_confirmed(b) for b in live_blocks):
            named = ", ".join(sorted(b.label for b in live_blocks)[:3])
            add(
                "built_capacity_uncited_in_blocks",
                WARNING,
                f"{project.mw_built:g} MW is recorded as built and {len(live_blocks)} "
                f"block(s) are running ({named}), but no running block carries a "
                "capacity any quote confirms — so the delivering total reads 0",
                ("mw_built",),
                "find a source that states the megawatts of a running tranche; the "
                "待确认 figure is deliberately never summed in its place",
            )

    # Only on an energisation that has actually happened. A future-dated one is
    # caught by `milestone_in_the_future` above, and calling it a contradiction
    # with the phase as well would report one data fault twice.
    energized_on = min(
        (e.event_date for e in events if e.event_type == "energized" and e.event_date),
        default=None,
    )
    if (
        energized_on
        and energized_on <= today_
        and phase
        and phase not in _LIVE_PHASES
        and phase not in PHASE_TERMINAL
        # A campus with one tranche energised and another still going up is
        # *correctly* described as under construction. This was every one of the 18
        # `energized_but_not_operational` findings: one enum asked to answer for a
        # campus that is two things at once.
        and not partly_live
    ):
        if blocks and not live_blocks:
            # The tranches this row has disagree with its power track. Measured on
            # SDC Quincy: one block, "Newest Phase", under construction, beside a
            # cited 2022 energisation with no tranche to hold it. Reporting that as
            # "the phase is behind the milestone" points at the wrong thing — the
            # phase is right and the blocks are incomplete, which is a crawl to do
            # rather than a value to change.
            add(
                "no_block_for_energisation",
                WARNING,
                f"power was energised on {energized_on}, but none of the "
                f"{len(blocks)} tranche(s) recorded here is running",
                ("mw_built",),
                "the energised part of this campus has no block yet; re-read a "
                "source that describes it",
            )
        else:
            add(
                "energized_but_not_operational",
                ERROR,
                f"power was energised on {energized_on} but the phase is still {phase}",
                ("phase",),
                "energised means running; the phase is behind the milestone",
            )

    # --- the numbers against each other --------------------------------------
    if (
        project.mw_built is not None
        and project.mw_planned is not None
        and project.mw_built > project.mw_planned
    ):
        add(
            "built_exceeds_planned",
            ERROR,
            f"{project.mw_built:g} MW built against {project.mw_planned:g} MW planned",
            ("mw_built", "mw_planned"),
            "either the plan was revised upward and nobody recorded it, or one "
            "figure is about a different phase of the campus",
        )

    # --- the numbers against the citations under them -------------------------
    out.extend(_check_stored_against_evidence(project))

    # --- the dates against each other ----------------------------------------
    if (
        project.first_announced
        and project.expected_online
        and project.expected_online < project.first_announced
    ):
        add(
            "online_before_announced",
            ERROR,
            f"expected online {project.expected_online} is before it was first "
            f"announced on {project.first_announced}",
            ("expected_online", "first_announced"),
            "usually a year misread from a source, or two campuses conflated",
        )

    today = _today()

    # Asked per block wherever there are blocks, because a campus whose phase-1 date
    # passed while phase 2 runs to a later schedule is not late — it is a campus. The
    # project-level question is only meaningful when there is nothing finer to ask.
    late_blocks = [
        b
        for b in blocks
        if b.expected_online
        and b.expected_online < today
        and b.status not in BLOCK_LIVE
        and b.status not in BLOCK_TERMINAL
    ]
    if late_blocks:
        listed = ", ".join(f"{b.label} ({b.expected_online})" for b in late_blocks[:3])
        add(
            "block_past_its_own_date",
            WARNING,
            f"{len(late_blocks)} tranche(s) are past their own online date — {listed}",
            ("expected_online",),
            "that tranche specifically slipped or is running; the campus around it "
            "may be perfectly on schedule",
        )
    elif (
        project.expected_online
        and project.expected_online < today
        and phase not in _LIVE_PHASES
        and phase not in PHASE_TERMINAL
        # A dated tranche still to come explains a campus date that has passed: the
        # date on the row is the first phase's, and the campus is not done.
        and not any(b.expected_online and b.expected_online >= today for b in blocks)
    ):
        overdue = (today - project.expected_online).days
        add(
            "past_its_own_date",
            WARNING,
            f"expected online {project.expected_online}, {overdue} days ago, and the "
            f"phase is still {phase or 'unset'}",
            ("expected_online", "phase"),
            "either it slipped and we missed the news, or it is running and we "
            "missed that — both are worth one crawl",
            subjects=("milestones:recent",),
        )

    # --- the blocks against themselves ----------------------------------------
    #
    # These two are this design's own instrumentation. `block_key` is asserted to be
    # an identity; if it is not, the symptom is a generic label that cannot be placed
    # or two tranches summing capacity they share. Better to report that than to let
    # a silent double-count sit inside a campus total.
    if blocks:
        from tracker import blocks as blocks_mod

        got = blocks_mod.rollup(blocks)
        if got.unplaceable:
            listed = ", ".join(got.unplaceable[:3])
            add(
                "block_label_ambiguous",
                WARNING,
                f"{len(got.unplaceable)} block(s) name a phase without saying of which "
                f"facility ({listed}), and this row holds more than one",
                ("mw_planned",),
                "name the parent facility; until then their capacity is left out of "
                "the campus total rather than guessed at",
            )

        nested = _nested_blocks(blocks)
        if nested:
            listed = ", ".join(f"{inner} within {outer}" for inner, outer in nested[:3])
            add(
                "blocks_may_double_count",
                WARNING,
                f"{len(nested)} block label(s) sit inside another's ({listed}), so "
                "summing them as separate tranches may count the same megawatts twice",
                ("mw_planned", "mw_built"),
                "if one describes part of the other, merge them or record only the finer grain",
            )

    # --- terminal states that are still moving --------------------------------
    if phase in PHASE_TERMINAL:
        construction = by_track.get("construction")
        live = construction and {"equipment_install"} & _reached(construction)
        if live:
            add(
                "cancelled_but_building",
                ERROR,
                f"phase is {phase}, but equipment installation is recorded as reached",
                ("phase",),
                "a cancelled project does not install equipment; one of the two is stale",
            )

    # --- an obstacle on a track that is already finished ----------------------
    for risk in risks:
        if getattr(risk, "status", OPEN_RISK_STATUS) != OPEN_RISK_STATUS:
            continue
        track = RISK_TRACK.get(risk.category)
        if not track:
            continue
        state = by_track.get(track)
        if not state:
            continue
        final = TRACK_MILESTONES[track][-1]
        if final in _reached(state):
            add(
                "obstacle_on_a_finished_track",
                WARNING,
                f"`{risk.category}` is still open, but the {track} track already reached `{final}`",
                ("blocker",),
                "resolve the obstacle in `tracker review`, or the milestone is wrong",
                subjects=(f"risk:{risk.category}", f"track:{track}", f"event:{final}"),
            )

    # --- placeholder values that reached storage --------------------------------
    #
    # `normalize.is_blank` turns "TBD" / "N/A" / "—" into NULL on every ingest
    # path, so a stored placeholder means a path bypassed the normalizer or the
    # token predates the list. It is an ERROR, not a warning: "TBD" is "the
    # source did not say" asserted as though it were a fact, and everything
    # downstream (coverage, confidence, capex) counts it as a populated field.
    for name in TRACKED_FIELDS:
        value = getattr(project, name, None)
        if isinstance(value, str) and value and is_blank(value):
            add(
                "placeholder_value",
                ERROR,
                f"{name} holds the placeholder {value!r} — a non-answer stored as a fact",
                (name,),
                "clear it: a null we can cite is worth more than a token that means nothing",
            )

    # --- evidence that is itself a placeholder ----------------------------------
    #
    # The per-field quote is the sentence a value stands on. A quote that is
    # empty or a placeholder means the gate verified the value against nothing —
    # the hover in the console would show "TBD" as the evidence for a number.
    for source in getattr(project, "sources", ()) or ():
        if not source.quotes:
            continue
        try:
            quotes = json.loads(source.quotes)
        except (TypeError, ValueError):
            continue
        if not isinstance(quotes, dict):
            continue
        for field_name, quote in quotes.items():
            text = str(quote or "").strip()
            if text and not is_blank(text):
                continue
            shown = "empty" if not text else repr(text)
            add(
                "placeholder_quote",
                WARNING,
                f"the quote recorded for {field_name} is {shown} — the value has no "
                "sentence behind it",
                (field_name,) if field_name in TRACKED_FIELDS else (),
                f"re-read {source.url}, or confirm the value in `tracker review`",
            )

    # A county or parish name sitting in `city`.
    #
    # `city` is FILL_ONLY and identity fields skip the evidence gate, so a wrong
    # one written at insert is permanent: no recompute, merge or re-extraction has
    # ever been able to correct it. Hyperion (#10) has carried city="Richland
    # Parish" since the day it was created. `crawl` now routes these at the
    # boundary, but that only helps rows read from here on.
    #
    # The repair is key-neutral, which is why this one gets an action:
    # `dedup.locality` already reads a county-suffixed `city` as county
    # granularity, so moving it does not move `dedup_key` and cannot fork the row.
    if project.city and looks_like_county(project.city):
        add(
            "city_holds_a_county_name",
            WARNING,
            f"city is {project.city!r}, which names a county or parish rather than a municipality",
            ("city", "county"),
            "move it to `county` — the dedup key already reads it that way, so nothing re-keys",
        )

    return out


def _today() -> dt.date:
    from tracker.models import utcnow

    return utcnow().date()


# --- Layer 2: source collisions ------------------------------------------------


def check_collisions(project: Project) -> list[Collision]:
    """Fields where two sources disagree, and who the merge policy keeps.

    The winner is asked for, never re-derived. `upsert._resolve` applies the
    declared per-field policy — and that policy is *not* "the best source wins"
    for every field, which is the trap this originally fell into. Built capacity
    takes the largest figure, `first_announced` the earliest, `phase` the furthest
    along unless a source says it stopped, and the identity fields are never
    overwritten at all. Only `prefer_weight` fields are settled by credibility and
    recency.

    Getting that wrong is not a cosmetic error. Reported against the live database
    the first version claimed 73 rows had "drifted from their sources" when they
    had done nothing of the kind — the resolution was simply a different rule than
    the one being checked against. A tool that invents 73 faults is worse than one
    that finds none.
    """
    from tracker.confidence import values_conflict
    from tracker.upsert import (
        DERIVED_FIELDS,
        FIELD_POLICY,
        Policy,
        claim_value,
        claims_by_field,
        resolve_field,
    )

    sources = list(getattr(project, "sources", ()) or ())
    if len(sources) < 2:
        return []

    out: list[Collision] = []
    for name, claims in claims_by_field(sources).items():
        # `blocker` and `notes` are not merged from claims at all — the first is
        # derived from the risk rows and the second is assembled. Sources record a
        # claim for `blocker` only to keep "every populated field is supported by
        # some citation" true, and that value is written and never read. Comparing
        # the stored blocker against those claims reported two projects as having
        # drifted from their sources when neither had.
        if name in DERIVED_FIELDS or name not in TRACKED_FIELDS or len(claims) < 2:
            continue

        stored = getattr(project, name, None)
        policy = FIELD_POLICY.get(name, Policy.PREFER_WEIGHT)
        # Resolve as the write path would, against the value already on the row —
        # FILL_ONLY, MAX and MIN all consult it.
        chosen = resolve_field(name, claims, stored)
        winner = next((c for c in claims if _same(c.value, chosen)), claims[0])
        rival = next((c for c in claims if values_conflict(chosen, c.value)), None)
        if rival is None:
            continue

        out.append(
            Collision(
                project_id=project.id,
                field=name,
                winner=chosen,
                loser=rival.value,
                winner_url=winner.url,
                loser_url=rival.url,
                winner_type=winner.source_type,
                loser_type=rival.source_type,
                winner_weight=winner.weight,
                loser_weight=rival.weight,
                winner_fetched=winner.fetched_at,
                loser_fetched=rival.fetched_at,
                decided_by=_decided_by(policy, winner, rival, claims),
                policy=policy.value,
                # The stored value should equal what the policy chooses. When it
                # does not, the row has drifted from its own citations — a hand
                # edit, or a source added since the last write — and
                # `recompute_from_sources` is what puts it back.
                stored_disagrees=stored is not None and not _same(claim_value(stored), chosen),
                stored=stored,
            )
        )
    return out


def why_decided(
    decided_by: str,
    *,
    winner_type: str = "",
    winner_weight: int = 0,
    loser_type: str = "",
    loser_weight: int = 0,
    winner_fetched: Any = None,
    loser_fetched: Any = None,
) -> str:
    """A rule name rendered in the reader's terms, and it is not always credibility.

    Only fields on the `prefer_weight` policy are settled by how good the source is
    and how recent it is. Built capacity takes the largest figure because energised
    megawatts only go up; the first-announced date takes the earliest because that
    is what "first" means; the phase takes the furthest along unless somebody says
    it stopped. Reporting all of them as "the better source won" would be a
    plausible sentence and the wrong one.

    Taken out of `Collision.why` so `upsert._conflict_notes` can say the same thing
    in the row's own notes. It used to hardcode "kept higher-weighted value" there,
    which was **false** whenever two equally-weighted sources disagreed — and that
    is the common case, because `government_doc` and `company_filing` are both
    weight 3. Hyperion's notes claimed credibility settled $10B over $50B when the
    two sources were the same weight and crawl order decided it.
    """
    if decided_by == "confirmed":
        return "the other value has no quote behind it"
    if decided_by == "credibility":
        return f"{winner_type} (weight {winner_weight}) beats {loser_type} (weight {loser_weight})"
    if decided_by == "recency":
        # Same-day fetches print the same date twice, which reads as a typo and
        # hides the real answer: the two were crawled minutes apart and the clock
        # decided a contested figure. Say that instead — it is the case an operator
        # most needs to distrust, and Hyperion's $10B is exactly it.
        if _day(winner_fetched) == _day(loser_fetched):
            return (
                "same credibility and both read on "
                f"{_day(winner_fetched)}; settled by which was crawled last, "
                "which is arbitrary with respect to the truth"
            )
        return (
            f"same credibility; kept the newer reading "
            f"({_day(winner_fetched)} over {_day(loser_fetched)})"
        )
    if decided_by == "largest":
        return "built capacity only grows, so the largest cited figure wins"
    if decided_by == "earliest":
        return "first announced means the earliest anybody saw, whatever the source"
    if decided_by == "furthest along":
        return "the phase furthest along the progression wins"
    if decided_by == "terminal":
        return "a source saying it stopped overrides one saying it progressed"
    if decided_by == "first seen":
        return "identity fields are never overwritten once set; churn is worse than staleness"
    return "identical credibility and date; settled on the source URL so the result is stable"


def decision(policy, winner, rival, claims) -> tuple[str, str]:
    """The rule that settled a field, and why, from the claims themselves.

    The public pair. Every surface that reports a contested field must ask this
    rather than re-derive it: the first version of `check_collisions` re-derived the
    ordering and reported 73 rows as drifted when none had.
    """
    code = _decided_by(policy, winner, rival, claims)
    return code, why_decided(
        code,
        winner_type=winner.source_type,
        winner_weight=winner.weight,
        loser_type=rival.source_type,
        loser_weight=rival.weight,
        winner_fetched=winner.fetched_at,
        loser_fetched=rival.fetched_at,
    )


def _decided_by(policy, winner, rival, claims) -> str:
    """Name the rule that actually settled this field."""
    from tracker.upsert import Policy
    from tracker.vocab import PHASE_TERMINAL

    if policy is Policy.FILL_ONLY:
        return "first seen"
    if policy is Policy.MAX:
        return "largest"
    if policy is Policy.MIN:
        return "earliest"
    if policy is Policy.PHASE:
        return "terminal" if winner.value in PHASE_TERMINAL else "furthest along"
    # PREFER_WEIGHT: the ordering itself is the answer, so say which step of it.
    if winner.confirmed != rival.confirmed:
        return "confirmed"
    if winner.weight != rival.weight:
        return "credibility"
    if (winner.fetched_at or _EPOCH) != (rival.fetched_at or _EPOCH):
        return "recency"
    return "tiebreak"


_EPOCH = dt.datetime(1970, 1, 1)


def _same(a: Any, b: Any) -> bool:
    """Loose equality, for a stored column against a JSON-round-tripped claim."""
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return a == b


# --- The report ----------------------------------------------------------------


# --- Layer 3: judgement --------------------------------------------------------

#: Below this the model is guessing, by its own account. Same floor `infer` uses.
MIN_CONFIDENCE: Final = 0.4
#: Per project. A row with five contradictions has one problem, not five.
MAX_PER_PROJECT: Final = 3


def build_context(project: Project, found: list[Finding]) -> dict[str, str]:
    """What the model is allowed to reason from.

    Includes the quote behind each value, because a contradiction between two
    *sourced* values is a different thing from one between two guesses, and the
    model cannot tell which it is looking at otherwise. Includes what the rules
    already found, so it does not spend its attention re-deriving them.
    """
    from tracker.gaps import provenance

    def show(value: Any) -> str:
        return "unknown" if value is None else str(value)

    quotes: list[str] = []
    for name in TRACKED_FIELDS:
        if getattr(project, name, None) is None:
            continue
        prov = provenance(project, name)
        if prov is None:
            continue
        text = (prov.quote or "").strip().replace("\n", " ")
        if not text:
            continue
        exact = "quoted for this field" if prov.quote_is_exact else "from the source excerpt"
        quotes.append(f'  {name} [{prov.tier}, {exact}]: "{text[:300]}"')

    milestones = sorted(
        {(e.event_type, str(e.event_date)) for e in getattr(project, "events", ()) or ()}
    )
    risks = [
        f"  - {r.category} ({r.severity}): {r.summary}"
        for r in getattr(project, "risks", ()) or ()
        if getattr(r, "status", OPEN_RISK_STATUS) == OPEN_RISK_STATUS
    ]

    return {
        "project_id": str(project.id),
        "name": show(project.name),
        "company": show(project.company),
        "customer": show(project.customer),
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "state": show(project.state),
        "mw_planned": show(project.mw_planned),
        "mw_built": show(project.mw_built),
        "investment_usd": show(project.investment_usd),
        "phase": show(project.phase),
        "first_announced": show(project.first_announced),
        "expected_online": show(project.expected_online),
        "blocker": show(project.blocker),
        "quotes": "\n".join(quotes) or "  (no quotes recorded)",
        "milestones": "\n".join(f"  - {t} on {d}" for t, d in milestones) or "  (none recorded)",
        "risks": "\n".join(risks) or "  (none open)",
        "already": "\n".join(f"  - {f.code}: {f.summary}" for f in found) or "  (nothing)",
        "today": str(_today()),
    }


def parse_contradictions(project: Project, payload: dict[str, Any]) -> list[Finding]:
    """Turn a reply into findings, dropping everything that is not checkable.

    Four ways a finding is dropped, and each has cost something to learn
    elsewhere in this project: fewer than two fields (an opinion, not a
    contradiction), a field name that does not exist (the model inventing a
    column), no evidence text (unverifiable), or confidence below the floor (the
    model saying so itself).
    """
    raw = payload.get("contradictions")
    if not isinstance(raw, list):
        return []

    out: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fields = tuple(
            str(f).strip() for f in (entry.get("fields") or []) if str(f).strip() in TRACKED_FIELDS
        )
        summary = str(entry.get("summary") or "").strip()
        evidence = str(entry.get("evidence") or "").strip()
        try:
            confidence = float(entry.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0

        if len(set(fields)) < 2 or not summary or not evidence:
            continue
        if confidence < MIN_CONFIDENCE:
            continue

        severity = ERROR if str(entry.get("severity", "")).lower() == "error" else WARNING
        out.append(
            Finding(
                project_id=project.id,
                code="model_contradiction",
                severity=severity,
                summary=f"{summary} [confidence {confidence:.1f}]",
                fields=tuple(dict.fromkeys(fields)),
                remedy=f"evidence given: {evidence[:200]}",
                inferred=True,
            )
        )
    return out[:MAX_PER_PROJECT]


#: Room for the reply.
#:
#: A reasoning model spends most of its budget inside `<think>` before writing a
#: line of JSON, and a reply cut off mid-thought has no closing `</think>` — so
#: the parser cannot strip the reasoning, finds no object, and the call is a total
#: loss that reads in the log as "the model returned nothing useful". Measured at
#: 2048 on a project with an obvious contradiction: truncated every time; raised
#: to 4096 to match `infer`. Then the first live evidence-audit sweep truncated
#: 5 of 20 rows at 4096 — a quarter of the spend paid for and thrown away — so
#: it is 8192 now. The rows that truncate are precisely the evidence-heavy ones
#: the audit most needs to finish reading.
MAX_TOKENS: Final = 8192


@dataclass(frozen=True)
class Read:
    """What one model call produced. A failure is not an absence of findings.

    The distinction is the whole reason this type exists. A sweep that reports
    "read by a model: 200" and shows nothing looks like two hundred clean rows,
    and is indistinguishable from two hundred calls that were paid for and threw
    their replies away. `failure` makes that visible in the summary instead of
    only in a log line nobody scrolls back to.
    """

    findings: list[Finding] = field(default_factory=list)
    #: None when the row was read. Otherwise "truncated", "unusable" or "error".
    failure: str | None = None


def examine(
    project: Project, found: list[Finding], *, extractor, prompt_name: str = "logic-v1"
) -> Read:
    """Ask a model for contradictions the rules cannot express. Costs one call.

    Never raises: one unreadable row is not a reason to abandon a sweep of two
    hundred. It is reported rather than swallowed — see :class:`Read`.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    prompt = load_prompt(prompt_name)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(**build_context(project, found)),
            max_tokens=MAX_TOKENS,
        )
    except LLMError as exc:
        log.warning("logic check failed for project %s: %s", project.id, exc)
        return Read(failure="error")
    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError) as exc:
        # Distinguish the two, because they call for opposite responses: a reply
        # that never left its reasoning block needs a bigger budget, and a reply
        # that produced malformed JSON needs a look at the prompt.
        if _ran_out_of_room(reply):
            log.warning(
                "logic check for project %s ran out of room while reasoning — it never "
                "reached the JSON. Raise MAX_TOKENS (currently %d); this is not a finding "
                "of 'no contradictions'.",
                project.id,
                MAX_TOKENS,
            )
            return Read(failure="truncated")
        log.warning("logic check for project %s returned unusable JSON: %s", project.id, exc)
        return Read(failure="unusable")
    return Read(findings=parse_contradictions(project, payload))


def _ran_out_of_room(reply) -> bool:
    """Did the budget cut this reply off, rather than the model finishing badly?

    `finish_reason == "length"` is the provider saying so, and it is the only
    signal that works wherever the cut landed. The first version of this sniffed
    for an unclosed `<think>` tag, which catches a reply severed mid-reasoning and
    misses one severed mid-JSON — and the second is indistinguishable from a model
    that simply returned prose. Observed live: a reply that had closed its
    reasoning and was then cut off got reported as "unusable JSON", sending the
    reader to look at the prompt when the answer was the budget.

    The tag check stays as a fallback for a provider that omits the field.

    Deliberately not retried at a larger budget. The first call is already paid
    for and a retry doubles it, which is worth doing only if truncation is rare —
    and there is no measurement of how often it happens at 4096, so a retry would
    be a guess spending somebody's money. Reported instead, with the number to
    raise.
    """
    if (getattr(reply, "finish_reason", None) or "").lower() in {"length", "max_tokens"}:
        return True
    lowered = (getattr(reply, "text", "") or "").lower()
    return "<think>" in lowered and "</think>" not in lowered


# --- Layer 3b: the evidence audit ------------------------------------------------
#
# `examine` asks whether the values agree with each other; this asks the prior
# question — whether each value's own evidence actually says it. The gate that
# stored a quote checked mechanically (the figure appears in the sentence); what
# survives that and still goes wrong is semantic: a programme-wide total quoted
# as one campus's money, a figure about the building next door, an aspiration
# recorded as a schedule. Reading is the only way to catch those, so this layer
# costs one call per row and is off by default.
#
# Nothing it returns is written. A confirmed mismatch is answered in `tracker
# review` by demoting the value to 待确认 — and an unconfirmed investment figure
# already stays out of the capex sums, so the audit plugs into an existing
# repair path rather than needing one of its own.

AUDIT_VERDICTS: Final[frozenset[str]] = frozenset({"unsupported", "misattributed", "hedged"})


def _evidence_lines(project: Project) -> tuple[list[str], list[str]]:
    """``(field names shown, rendered lines)``. Both empty when nothing is auditable."""
    from tracker.gaps import provenance

    names: list[str] = []
    lines: list[str] = []
    for name in TRACKED_FIELDS:
        value = getattr(project, name, None)
        if value is None:
            continue
        prov = provenance(project, name)
        if prov is None:
            continue
        text = (prov.quote or "").strip().replace("\n", " ")
        if not text:
            continue
        kind = (
            "exact quote for this field" if prov.quote_is_exact else "fallback: the source excerpt"
        )
        names.append(name)
        lines.append(f'  {name} = {value}\n    [{prov.tier}, {kind}] "{text[:400]}"')
    return names, lines


def auditable_fields(project: Project) -> list[str]:
    """Fields whose stored value carries a sentence the audit can judge."""
    names, _lines = _evidence_lines(project)
    return names


def parse_evidence_findings(
    project: Project, payload: dict[str, Any], shown: list[str]
) -> list[Finding]:
    """Findings from an audit reply, dropping everything unverifiable.

    Same discipline as `parse_contradictions`, with the two-field rule swapped
    for a shown-field rule: the audit's subject *is* one field, so a single
    field is correct here — but it must be one the model was actually shown,
    the verdict must be one of the three defined kinds, the reason must exist,
    and the confidence floor still applies.
    """
    raw = payload.get("mismatches")
    if not isinstance(raw, list):
        return []

    allowed = set(shown)
    out: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        field_name = str(entry.get("field") or "").strip()
        verdict = str(entry.get("verdict") or "").strip().lower()
        reason = str(entry.get("reason") or "").strip()
        try:
            confidence = float(entry.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if field_name not in allowed or verdict not in AUDIT_VERDICTS or not reason:
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        out.append(
            Finding(
                project_id=project.id,
                code="evidence_mismatch",
                severity=WARNING,
                summary=(
                    f"{field_name}: the evidence looks {verdict} — {reason} "
                    f"[confidence {confidence:.1f}]"
                ),
                fields=(field_name,),
                remedy=(
                    "if a person confirms this, demote the value in `tracker review`; "
                    "an unconfirmed investment figure already stays out of the capex sums"
                ),
                inferred=True,
            )
        )
    return out[:MAX_PER_PROJECT]


def audit_evidence(project: Project, *, extractor, prompt_name: str = "evidence-v1") -> Read:
    """Ask a model whether each value's evidence actually states it. One call.

    Never raises, for the same reason `examine` never does: one unreadable row
    is not a reason to abandon a sweep.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    shown, lines = _evidence_lines(project)
    if not shown:
        return Read()

    def show(value: Any) -> str:
        return "unknown" if value is None else str(value)

    prompt = load_prompt(prompt_name)
    context = {
        "project_id": str(project.id),
        "name": show(project.name),
        "company": show(project.company),
        "customer": show(project.customer),
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "evidence": "\n".join(lines),
        "today": str(_today()),
    }
    try:
        reply = extractor.complete(
            system=prompt.system, user=prompt.render_user(**context), max_tokens=MAX_TOKENS
        )
    except LLMError as exc:
        log.warning("evidence audit failed for project %s: %s", project.id, exc)
        return Read(failure="error")
    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError) as exc:
        if _ran_out_of_room(reply):
            log.warning(
                "evidence audit for project %s ran out of room while reasoning — "
                "raise MAX_TOKENS (currently %d); this is not a clean audit.",
                project.id,
                MAX_TOKENS,
            )
            return Read(failure="truncated")
        log.warning("evidence audit for project %s returned unusable JSON: %s", project.id, exc)
        return Read(failure="unusable")
    return Read(findings=parse_evidence_findings(project, payload, shown))


# --- What a person can do about one -------------------------------------------
#
# "0 of 149 findings are mechanically resolvable" is true about a *machine* and
# says nothing about a person. An operator looking at `100 MW built against 32 MW
# planned` with both quotes in front of them can settle it in two seconds; what
# they lacked was a way to record the answer without hand-editing SQLite.
#
# So each finding declares the concrete edits that answer it. Every one is a
# statement an operator is entitled to make and the model is not — which is the
# line this whole project draws, not a line between "writes" and "does not write".
#
# Each action is (key, label, apply) where `apply` returns what it changed, for
# the audit note.


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    apply: Any  # (session, project, finding) -> str


def _drop_future_milestones(session: Session, project: Project, _f: Finding) -> str:
    today = _today()
    doomed = [
        e
        for e in list(project.events)
        if e.event_date and e.event_date > today and e.event_type in _MILESTONE_TYPES
    ]
    for event in doomed:
        project.events.remove(event)
        session.delete(event)
    return f"removed {len(doomed)} milestone(s) dated in the future"


def _move_city_to_county(_s: Session, project: Project, _f: Finding) -> str:
    """Put a parish name where it belongs, without moving the dedup key.

    Safe precisely because `dedup.locality` already treats a county-suffixed
    `city` as county granularity — so the row's identity is unchanged and no
    lookup starts missing. `ck_project_locality` holds because the value lands in
    `county` rather than being dropped.
    """
    was = project.city
    if not project.county:
        project.county = was
    project.city = None
    return f"city {was!r} -> empty (moved to county {project.county!r})"


def _raise_planned_to_built(_s: Session, project: Project, _f: Finding) -> str:
    was, project.mw_planned = project.mw_planned, project.mw_built
    return f"mw_planned {was} -> {project.mw_planned} (plan revised up to what is built)"


def _clear_built(_s: Session, project: Project, _f: Finding) -> str:
    was, project.mw_built = project.mw_built, None
    return f"mw_built {was} -> empty"


def _clear_expected_online(_s: Session, project: Project, _f: Finding) -> str:
    was, project.expected_online = project.expected_online, None
    return f"expected_online {was} -> empty"


def _clear_first_announced(_s: Session, project: Project, _f: Finding) -> str:
    was, project.first_announced = project.first_announced, None
    return f"first_announced {was} -> empty"


def _resolve_finished_obstacles(session: Session, project: Project, _f: Finding) -> str:
    stand = standing(project.id, list(project.events), list(project.risks))
    by_track = {s.track: s for s in stand.tracks}
    closed = 0
    for risk in project.risks:
        if getattr(risk, "status", OPEN_RISK_STATUS) != OPEN_RISK_STATUS:
            continue
        track = RISK_TRACK.get(risk.category)
        state = by_track.get(track) if track else None
        if state and TRACK_MILESTONES[track][-1] in set(state.reached):
            risk.status = "resolved"
            risk.resolved_at = _today()
            closed += 1
    session.flush()
    return f"closed {closed} obstacle(s) on a finished track"


#: finding code -> what an operator can do about it. `skip` and `verify` are
#: offered everywhere and are not listed here.
#: Per-finding operator edits.
#:
#: **Three actions were removed, and the reason is the point of this table.**
#: `_set_phase_operational`, `_drop_energized` and `_built_equals_planned` all
#: "fixed" a finding by rewriting the row to match a `phase` enum that cannot
#: describe the row in the first place.
#:
#: A modern campus is several states at once: 150 MW energised and serving, 150 MW
#: under construction, 300 MW planned. Measured on the live database, 28 projects
#: are partly built, 15 are `construction` with megawatts already live, and 12 have
#: power energised while construction is mid-track. Those are not contradictions —
#: they are the ordinary shape of the thing, and the schema has no room for it.
#:
#: So the findings they hung off were largely artefacts, and the edits destroyed
#: correct information to silence them. `_drop_energized` deleted a real, cited
#: energisation milestone. `_built_equals_planned` asserted a whole campus was
#: energised because one phase was. `_set_phase_operational` marked a campus
#: finished while most of it was still being built. `tracker logic resolve --llm`
#: could apply any of them unattended.
#:
#: The findings stay — they are still worth a person's eye — but with no action
#: offered they can only be verified or skipped, which is the honest set of
#: choices until `capacity_block` lands and the rules are re-expressed per block.
#: See the plan: those rules are then either per-block or retired.
ACTIONS: Final[dict[str, tuple[Action, ...]]] = {
    # No action: the phase and the milestone disagree because one enum is being
    # asked to describe a campus that is partly live. Neither side is wrong.
    "energized_but_not_operational": (),
    "built_exceeds_planned": (
        Action("u", "the plan was revised — raise mw_planned to mw_built", _raise_planned_to_built),
        Action("c", "the built figure is wrong — clear it", _clear_built),
    ),
    "online_before_announced": (
        Action("c", "the online date is wrong — clear it", _clear_expected_online),
        Action("a", "the announced date is wrong — clear it", _clear_first_announced),
    ),
    # Clearing a stale date is still honest. Declaring the campus operational
    # because one phase's date passed is not.
    "past_its_own_date": (Action("c", "that date is stale — clear it", _clear_expected_online),),
    "city_holds_a_county_name": (
        Action("m", "move it to county — the dedup key does not change", _move_city_to_county),
    ),
    # No action: "no source says any of it is built" is a gap to fill, not a
    # licence to assert the whole plan is energised.
    "operational_without_built_capacity": (),
    "obstacle_on_a_finished_track": (
        Action("r", "the obstacle cleared — mark it resolved", _resolve_finished_obstacles),
    ),
    "milestone_in_the_future": (
        Action("d", "those are plans, not milestones — remove them", _drop_future_milestones),
    ),
    "phase_without_construction": (),  # nothing to edit; accept or skip
    # The block rules are all report-only, and stay that way. Each names something
    # only a person can settle: which facility an unplaceable tranche belongs to,
    # whether two labels describe one lease, whether a slipped tranche slipped or
    # simply went live unreported. An automatic edit here would be guessing at the
    # sub-site grain, which is the grain this whole design exists to stop guessing at.
    "block_past_its_own_date": (),
    "live_block_without_cited_capacity": (),
    # Same reasoning as its sibling above: the fix is a citation for a running
    # tranche, and no automatic edit can find one.
    "built_capacity_uncited_in_blocks": (),
    "block_label_ambiguous": (),
    "blocks_may_double_count": (),
    "no_block_for_energisation": (),
}


# --- Letting a model make the choice --------------------------------------------

#: Below this a triage answer is discarded and treated as a skip. Higher than
#: `infer`'s 0.35 on purpose: an inferred obstacle is a labelled opinion sitting
#: beside the facts, whereas a triage answer *edits* the row. The cost of being
#: wrong is not symmetrical, so neither is the bar.
TRIAGE_MIN_CONFIDENCE: Final = 0.6


@dataclass(frozen=True)
class Decision:
    """One model answer to one contradiction.

    `outcome` separates two things the first version conflated, and the confusion
    was not cosmetic: a model that *declined* is doing exactly what it was asked —
    "skip when the evidence does not clearly favour one option" is rule one of the
    prompt — while a model whose answer was *rejected* returned something unusable.
    Reporting a correct decline as "'s' is not one of the options" made a working
    model look broken in its own summary.
    """

    key: str
    confidence: float
    reason: str
    #: "applied" | "declined" (the model chose to skip) | "rejected" (unusable).
    outcome: str = "applied"
    #: Why, for anything but applied.
    note: str = ""

    @property
    def acted(self) -> bool:
        return self.outcome == "applied"


def decide(project: Project, finding: Finding, *, extractor, prompt_name: str = "triage-v1"):
    """Ask a model which of the offered fixes to apply. Costs one call.

    **Why this is allowed where `infer` bars 关键数字.** `infer` asks a model to
    produce a value from general knowledge, and a capacity or a date it invents is
    indistinguishable in the database from one somebody reported. This asks a
    different question: given two figures that are *both already in the row with
    citations behind them*, which of two hand-written edits applies. The model's
    entire output is one character from a closed set. It cannot type a number, so
    the worst it can do is pick the wrong one of two options a person wrote — a
    mistake a person could equally make, and one the audit note makes visible.

    Never returns "v". Marking a row verified means "an operator says this is
    right", it feeds `confidence`, and a model may not say it. The option is not
    offered and is rejected if volunteered.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    actions = ACTIONS.get(finding.code, ())
    if not actions:
        return Decision("s", 0.0, "", outcome="declined", note="nothing to choose between")

    prompt = load_prompt(prompt_name)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(**_triage_context(project, finding, actions)),
            max_tokens=MAX_TOKENS,
        )
    except LLMError as exc:
        log.warning("triage failed for project %s: %s", project.id, exc)
        return Decision("s", 0.0, "", outcome="rejected", note=f"call failed: {exc}")

    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError):
        return Decision("s", 0.0, "", outcome="rejected", note="unusable reply")

    key = str(payload.get("key") or "").strip().lower()[:1]
    reason = str(payload.get("reason") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    allowed = {a.key for a in actions}
    if key == "v":
        return Decision(
            "s", confidence, reason, outcome="rejected", note="a model may not verify a row"
        )
    # A deliberate skip is the answer rule one of the prompt asks for, not a
    # malformed reply. Checked before the membership test, which would
    # otherwise report every correct decline as an invalid option.
    if key == "s":
        return Decision(
            "s",
            confidence,
            reason,
            outcome="declined",
            note=reason or "the evidence did not clearly favour one option",
        )
    if key not in allowed:
        return Decision(
            "s", confidence, reason, outcome="rejected", note=f"{key!r} is not one of the options"
        )
    if confidence < TRIAGE_MIN_CONFIDENCE:
        return Decision(
            "s",
            confidence,
            reason,
            outcome="declined",
            note=f"confidence {confidence:.2f} is below the {TRIAGE_MIN_CONFIDENCE} floor",
        )
    if not reason:
        return Decision("s", confidence, reason, outcome="rejected", note="no reason given")
    return Decision(key, confidence, reason)


def _subject_evidence(project: Project, finding: Finding) -> list[str]:
    """The obstacles and milestones this finding is actually about, quoted.

    **This is the fix for a model that kept declining sensibly.** The old context
    showed one provenance quote per declared field, and half the rules declare
    `blocker` or `phase` — derived strings whose quote is about something else. So
    a question like "`grid_capacity` is still open but the power track is
    finished" arrived beside a sentence about a 150 MW grid service approval, and
    the model answered, correctly, that the quote did not address the question. It
    was reading the only page it had been given.

    Each subject token names a row: the obstacle with its own quote and source,
    the milestone with its date and the sentence that reported it.
    """
    lines: list[str] = []
    wanted_risks = {s.split(":", 1)[1] for s in finding.subjects if s.startswith("risk:")}
    wanted_events = {s.split(":", 1)[1] for s in finding.subjects if s.startswith("event:")}
    tracks = {s.split(":", 1)[1] for s in finding.subjects if s.startswith("track:")}
    if tracks:
        wanted_events |= {m for t in tracks for m in TRACK_MILESTONES.get(t, ())}

    sources = {s.id: s for s in getattr(project, "sources", ()) or ()}

    for risk in getattr(project, "risks", ()) or ():
        if risk.category not in wanted_risks:
            continue
        if getattr(risk, "status", OPEN_RISK_STATUS) != OPEN_RISK_STATUS:
            continue
        lines.append(f"  OBSTACLE {risk.category} ({risk.severity}), first seen {risk.first_seen}")
        lines.append(f"    what it says: {risk.summary}")
        quote = (risk.quote or "").strip().replace("\n", " ")
        if quote:
            lines.append(f'    quoted: "{quote[:280]}"')
        else:
            reason = risk.unconfirmed or "no quote recorded"
            lines.append(f"    NOT QUOTED ({reason}) — a source named it and quoted nothing")
        source = sources.get(risk.source_id)
        if source is not None:
            lines.append(f"    source: {source.url[:110]}")

    for event in sorted(
        getattr(project, "events", ()) or (), key=lambda e: (str(e.event_date), e.event_type)
    ):
        key = f"{event.event_type}:{event.event_date}"
        if event.event_type not in wanted_events and key not in wanted_events:
            continue
        lines.append(f"  MILESTONE {event.event_type} dated {event.event_date}")
        if event.description:
            lines.append(f"    reported as: {event.description[:280]}")
        source = sources.get(event.source_id)
        if source is not None:
            lines.append(f"    source: {source.url[:110]}")

    if "milestones:recent" in finding.subjects:
        recent = sorted(
            getattr(project, "events", ()) or (), key=lambda e: str(e.event_date), reverse=True
        )[:3]
        for event in recent:
            lines.append(f"  MOST RECENT MILESTONE {event.event_type} dated {event.event_date}")
            if event.description:
                lines.append(f"    reported as: {event.description[:200]}")
    return lines


def _triage_context(project: Project, finding: Finding, actions) -> dict[str, str]:
    from tracker.gaps import provenance

    def show(value: Any) -> str:
        return "unknown" if value is None else str(value)

    evidence: list[str] = []
    for name in finding.fields or ():
        value = getattr(project, name, None)
        evidence.append(f"  {name} = {show(value)}")
        prov = provenance(project, name) if value is not None else None
        quote = (prov.quote or "").strip().replace("\n", " ") if prov else ""
        if quote:
            exact = "for this field" if prov.quote_is_exact else "from the source excerpt"
            evidence.append(f'      [{prov.tier}, {exact}] "{quote[:280]}"')
    subject_lines = _subject_evidence(project, finding)
    if subject_lines:
        evidence.append("")
        evidence.append("  WHAT THIS FINDING IS ABOUT, IN THE ROW'S OWN WORDS:")
        evidence.extend(subject_lines)

    milestones = sorted(
        {(e.event_type, str(e.event_date)) for e in getattr(project, "events", ()) or ()}
    )
    risks = [
        f"  - {r.category} ({r.severity}): {r.summary}"
        for r in getattr(project, "risks", ()) or ()
        if getattr(r, "status", OPEN_RISK_STATUS) == OPEN_RISK_STATUS
    ]
    return {
        "project_id": str(project.id),
        "company": show(project.company),
        "name": show(project.name),
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "phase": show(project.phase),
        "mw_planned": show(project.mw_planned),
        "mw_built": show(project.mw_built),
        "investment_usd": show(project.investment_usd),
        "first_announced": show(project.first_announced),
        "expected_online": show(project.expected_online),
        "customer": show(project.customer),
        "summary": finding.summary,
        "remedy": finding.remedy,
        "evidence": "\n".join(evidence) or "  (no values recorded)",
        "milestones": "\n".join(f"  - {t} on {d}" for t, d in milestones) or "  (none)",
        "risks": "\n".join(risks) or "  (none open)",
        "options": "\n".join(f"  {a.key}  {a.label}" for a in actions),
        "today": str(_today()),
    }


def record_decision(
    project: Project, code: str, what: str, *, by: str = "operator", detail: str = ""
) -> None:
    """Write the decision into the row's notes, permanently, naming who made it.

    Plain prose with no marker, which is the one class of note `upsert._merge_notes`
    never regenerates or deletes. That matters: a `[tracker]` line would be wiped
    by the next ingest and the record of a human overruling the data would vanish
    exactly when somebody later asks why the phase says operational.

    `by` is load-bearing and not decoration. A row edited by a model and a row
    edited by a person who read the sources are different things, and six months
    later the note is the only place that difference survives. Writing "operator"
    over a model's choice would be the single most damaging lie this file could
    tell, because every other honesty mechanism here assumes the provenance
    string is true.
    """
    from tracker.models import utcnow

    line = f"{utcnow().date()} {by} resolved `{code}`: {what}"
    if detail:
        line += f" — {detail}"
    lines = [ln for ln in (project.notes or "").splitlines() if ln.strip()]
    if line not in lines:
        lines.append(line)
    project.notes = "\n".join(lines)


@dataclass
class Repair:
    """One row put back in step with its own sources."""

    project_id: int
    label: str
    #: field -> (what the row held, what its sources support)
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def resolve_drift(session: Session, *, apply: bool = False) -> list[Repair]:
    """Re-derive fields for rows whose stored values their sources do not support.

    **This is the only contradiction in the whole report that a machine may
    settle**, and it is worth being precise about why. It is not a judgement
    about which of two claims is true — that was decided when the row was
    written, by the declared per-field policy. It is the narrower observation
    that the row no longer matches the answer that policy gives, which happens
    after a hand edit or when a source is attached by a path that did not
    recompute. Re-running the policy is arithmetic, not an opinion.

    Everything else `tracker logic` reports needs a person. Whether 100 MW built
    against 32 MW planned means the plan was revised or the figure is about a
    different phase is not derivable from the row, and a tool that picked one
    would be inventing a fact — the exact thing the evidence tiers exist to
    prevent. Measured on the live database: 0 of 149 findings were mechanically
    resolvable.

    Args:
        apply: write. False returns the same list without touching anything, so
            the preview and the change are computed by identical code.
    """
    from tracker.upsert import recompute_from_sources

    repairs: list[Repair] = []
    for project in session.scalars(select(Project)).all():
        drifted = [c for c in check_collisions(project) if c.stored_disagrees]
        if not drifted:
            continue
        repair = Repair(
            project_id=project.id,
            label=f"{project.company} — {project.name}",
            changes={c.field: (c.stored, c.winner) for c in drifted},
        )
        if apply:
            repair.conflicts = recompute_from_sources(session, project)
        repairs.append(repair)
    return repairs


def dedupe(findings: list[Finding]) -> list[Finding]:
    """One question per question. Order preserved.

    Two `risk` rows for the same obstacle — same category, different `first_seen`,
    which the unique constraint permits — produce two identical findings, and the
    action offered for one closes both. `tracker logic resolve --llm` was paying
    for a model call per copy and printing the same line four times under one
    project, which is most of what made a run unreadable.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[Finding] = []
    for finding in findings:
        if finding.identity in seen:
            continue
        seen.add(finding.identity)
        out.append(finding)
    return out


def resolvable(finding: Finding) -> bool:
    """Whether any edit exists that answers this finding.

    Eleven of the sixteen rules offer no action, on purpose — each names something
    only a person can settle, or a contradiction between a phase enum and a campus
    that is two things at once. Those are worth a reader's eye and are not worth an
    LLM call: `decide` can only ever return "nothing to choose between" for them.
    Filtering on this before spending is the difference between a run that reads as
    twelve refusals and one that reads as three decisions.
    """
    return bool(ACTIONS.get(finding.code))


def review(
    session: Session,
    *,
    extractor=None,
    read_limit: int | None = None,
    audit_limit: int | None = None,
    only: int | None = None,
    on_examine=None,
    on_audit=None,
) -> Report:
    """Every free check over every project, and optionally the paid ones.

    Args:
        extractor: when given, rows may also be read by a model. One call each.
        read_limit: contradiction reads to pay for. **None means none**, not
            unlimited — the caller must always name a number, because "however
            many rows the database happens to hold" is not a spend anybody
            chose. (None meant unlimited once, and the first `--audit`-only run
            silently started reading every row in the database.)
        audit_limit: also audit the evidence behind this many rows' values.
            Separate dial, separate question — see `audit_evidence`.
        only: check a single project id.
        on_examine: called with each project before its contradiction read.
        on_audit: called with each project before its evidence audit.
    """
    query = select(Project)
    if only is not None:
        query = query.where(Project.id == only)
    projects = session.scalars(query).all()

    report = Report(projects=len(projects))
    for project in projects:
        found = dedupe(check_rules(project))
        report.findings.extend(found)
        report.collisions.extend(check_collisions(project))

        if extractor is None or not read_limit or report.examined >= read_limit:
            continue
        if on_examine is not None:
            on_examine(project)
        report.examined += 1
        read = examine(project, found, extractor=extractor)
        report.findings.extend(read.findings)
        if read.failure:
            report.unreadable[read.failure] = report.unreadable.get(read.failure, 0) + 1

    if extractor is not None and audit_limit:
        # Costliest rows first: the audit exists to protect the sums, so the
        # dollars at stake pick the reading order rather than the row id. Rows
        # with no quoted values are skipped outright — a call that could only
        # come back empty is not worth paying for.
        candidates = sorted(
            (p for p in projects if auditable_fields(p)),
            key=lambda p: (-(p.investment_usd or 0), -(p.mw_planned or 0.0), p.id),
        )
        for project in candidates[:audit_limit]:
            if on_audit is not None:
                on_audit(project)
            report.audited += 1
            read = audit_evidence(project, extractor=extractor)
            report.findings.extend(read.findings)
            if read.failure:
                report.unreadable[read.failure] = report.unreadable.get(read.failure, 0) + 1

    return report


__all__ = [
    "ACTIONS",
    "AUDIT_VERDICTS",
    "ERROR",
    "MAX_PER_PROJECT",
    "MAX_TOKENS",
    "MIN_CONFIDENCE",
    "TRIAGE_MIN_CONFIDENCE",
    "WARNING",
    "Action",
    "Collision",
    "Decision",
    "Finding",
    "Read",
    "Repair",
    "Report",
    "audit_evidence",
    "auditable_fields",
    "build_context",
    "check_collisions",
    "check_rules",
    "decide",
    "dedupe",
    "examine",
    "parse_contradictions",
    "parse_evidence_findings",
    "record_decision",
    "resolvable",
    "resolve_drift",
    "review",
]
