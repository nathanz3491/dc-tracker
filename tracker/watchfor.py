"""What somebody should watch for themselves, on each project they follow.

The daily email and the console's *Monitor* page both read this. The briefing
(`feed`) answers "what moved"; this answers the question a reader is left with on a
day when nothing did: **what is standing between each of my projects and done, and
what would the next piece of good news look like?**

**It is the project's open obstacles reported in the last two months.** The same
window as the Updates page (`feed.REPORT_WINDOW_DAYS`, on `feed.reported_on`'s
date), because the product's rule is that nothing older than that is current. It
used to list every open obstacle however old, on the argument that an unresolved
fight is a standing fact rather than news; measured on the live database, 231 of
339 open obstacles had not been reported in two months and 71 not in a year, so
"open" mostly meant "nobody has written that it closed". The older ones are
counted, never listed, so the page still says they exist. Each listed obstacle
carries how long it has been open.

**What to watch for is the next milestone on the blocked track.** `tracks` already
knows, per track, the rung after the furthest one reached and what that rung looks
like in the wild (`NEXT_SIGNAL`). A blocked track's next rung is exactly the
signal that the obstacle has moved, so it is listed beside the obstacles that hold
it. A project with nothing blocked gets the next step of its earliest unfinished
track instead, the same choice `ProjectStanding.watch_for` makes.

**Unconfirmed obstacles are kept apart**, never mixed in. The console's rule is
that a model's say-so is not a fact; the page shows them, labelled, and the email
leaves them out, exactly as it leaves out unconfirmed updates.

**Rules for a place are kept apart too** (`tracker.conditions`). A state's permit
pause or a town's ban reaches a followed project because of where it is, not because
an article about that campus reported an obstacle, so it is listed beside the
obstacles and never among them: it does not make a project "blocked", is not counted
by severity, and does not move the order. What would clear one that restricts is the
rule lifted or voted down — or, for a rule that covers only projects with no permit
yet, the project's permit approved first (:func:`clears`). A rule that eases building
(a tax exemption, by-right zoning) reaches projects the same way and has nothing to
clear; it is counted apart from the restricting ones, so help never reads as a threat.

Nothing here is stored. Like `tracks` and `feed`, it is a reading of rows that
already carry their own dates and citations.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker import watchlist
from tracker.conditions import EXPOSING, PLACED, where_words
from tracker.dedup import company_key, company_slug
from tracker.feed import REPORT_WINDOW_DAYS, reported_on
from tracker.models import Condition, ConditionSource, Exposure, Project, Source
from tracker.sources import host_of
from tracker.tracks import NEXT_SIGNAL, RISK_TRACK, TRACK_LABELS, TRACKS, standing
from tracker.vocab import OPEN_RISK_STATUS, severity_rank

#: How many projects the email's short version lists before it points at the page.
EMAIL_PROJECTS: Final[int] = 5

#: How many blockers the short version names per project.
EMAIL_BLOCKERS_PER_PROJECT: Final[int] = 2


def track_label(track: str | None) -> str | None:
    """ "power", not "power (电力接入)": the reader-facing half of the label."""
    return TRACK_LABELS[track].split(" (")[0] if track in TRACK_LABELS else None


#: A rule's status as the console prints it: `rejected` as what happened to it.
RULE_STATUS_LABEL: Final[dict[str, str]] = {
    "proposed": "proposed",
    "pledged": "pledged",
    "passed": "passed",
    "in_force": "in force",
    "blocked": "blocked",
    "lifted": "lifted",
    "rejected": "voted down",
}

#: The need a rule hits, in the words the console's obstacle list uses for the same
#: categories (`RISK_LABEL` in `static/app.js`), so a permitting rule and a
#: permitting obstacle read alike.
NEED_LABEL: Final[dict[str, str]] = {
    "grid_capacity": "Grid capacity",
    "transmission": "Transmission",
    "permitting": "Permitting",
    "environmental": "Environmental",
    "equipment_supply": "Equipment supply",
    "chip_supply": "Chip supply",
    "financing": "Financing",
    "offtake": "No committed buyer",
    "community_opposition": "Local opposition",
    "water": "Water",
    "unclassified": "Unclassified",
}

#: Which projects a rule covers, by `applies_to`, as a phrase.
COVERS: Final[dict[str, str]] = {
    "new": "projects with no permit yet",
    "unbuilt": "projects not yet running",
    "all": "every project not cancelled",
}

#: What a rule does to building, as the console prints it (migration 0052).
EFFECT_LABEL: Final[dict[str, str]] = {"restricts": "restricts", "eases": "eases"}

#: A rule that helps — an exemption, by-right zoning. Everything else restricts,
#: which is also what a rule the reader could not class is taken to do.
EASES: Final = "eases"


def rule_status_label(status: str) -> str:
    return RULE_STATUS_LABEL.get(status, status.replace("_", " "))


def need_label(channel: str) -> str:
    return NEED_LABEL.get(channel, channel.replace("_", " "))


def clears(condition: Condition) -> str:
    """What would take a rule off a project, as the end of "Would clear it: …".

    Only two things can, because which projects a rule reaches is code
    (`conditions.reach`): the rule stops standing — lifted, repealed or voted down,
    so it is no longer in `conditions.EXPOSING` — or the project stops being one it
    covers. The second is in a project's own hands only for a rule on projects with
    no permit yet: a permit approved first takes it out. A rule on every unbuilt
    project lasts until the campus runs, which is finishing, not clearing anything.

    "A project's", not "this project's": Monitor and the Rules view print it once
    for every project the rule reaches. Empty for a rule already lifted or voted
    down, which reaches nothing, and for a rule that eases building, which stands in
    nobody's way: there is nothing to clear.
    """
    if condition.status not in EXPOSING or condition.effect == EASES:
        return ""
    rule = {
        "proposed": "the proposal voted down or dropped",
        "pledged": "the pledge dropped, or voted down if it is put to a vote",
        "passed": "the rule repealed before it takes effect",
        "in_force": "the rule lifted or repealed",
        "blocked": "the court's block made final, or the rule repealed",
    }[condition.status]
    if condition.status == "passed" and condition.effective_on:
        rule += f" on {condition.effective_on.isoformat()}"
    if condition.status == "in_force" and condition.ends_on:
        rule += f" (it is set to end on {condition.ends_on.isoformat()})"
    if condition.applies_to == "new":
        rule += "; or a project's permit approved first, since it covers only projects with none"
    return rule


def _newest(condition: Condition) -> ConditionSource | None:
    """The article that set the rule's status (`conditions.settle` reads it the same way)."""
    return max(
        condition.sources,
        key=lambda s: (s.published_at or s.read_at, s.id or 0),
        default=None,
    )


@dataclass(frozen=True)
class RuleReach:
    """A rule for a place that reaches one followed project. Never an obstacle of it."""

    condition_id: int
    title: str
    where: str
    status: str
    need: str
    applies_to: str
    fit: str
    #: Why it reaches this project, in the reader's words ("TX · announced · no permit
    #: on record"); stored on the exposure by the code that matched it.
    why: str
    #: Empty for a rule that eases building: nothing to clear.
    clears: str
    effect: str = "restricts"
    reported: dt.date | None = None
    source_url: str | None = None
    publisher: str | None = None

    @property
    def restricts(self) -> bool:
        return self.effect != EASES

    def as_json(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "title": self.title,
            "where": self.where,
            "status": self.status,
            "status_label": rule_status_label(self.status),
            "effect": self.effect,
            "effect_label": EFFECT_LABEL.get(self.effect, self.effect),
            "need": self.need,
            "need_label": need_label(self.need),
            "applies_to": self.applies_to,
            "covers": COVERS.get(self.applies_to, self.applies_to),
            "fit": self.fit,
            "why": self.why,
            "clears": self.clears,
            "reported": self.reported.isoformat() if self.reported else None,
            "source_url": self.source_url,
            "publisher": self.publisher,
        }


def rules_reaching(
    session: Session, project_ids: Iterable[int] | None = None
) -> dict[int, tuple[RuleReach, ...]]:
    """project id -> the rules for its place that reach it, newest report first.

    `None` reads every exposure. Only standing rules for a place: the exposure table
    is rebuilt by `tracker conditions match`, so between a rule being read as lifted
    and the next rebuild its old exposures are still there, and a rule that reaches
    nothing must not be listed as reaching something.
    """
    query = (
        select(Condition, Exposure)
        .join(Exposure, Exposure.condition_id == Condition.id)
        .where(Condition.status.in_(sorted(EXPOSING)), Condition.scope.in_(sorted(PLACED)))
        .options(selectinload(Condition.sources))
        .order_by(Condition.last_reported.desc(), Condition.id.desc())
    )
    if project_ids is not None:
        wanted = list(project_ids)
        if not wanted:
            return {}
        query = query.where(Exposure.project_id.in_(wanted))
    out: dict[int, list[RuleReach]] = {}
    for condition, exposure in session.execute(query):
        newest = _newest(condition)
        url = newest.url if newest is not None else None
        out.setdefault(exposure.project_id, []).append(
            RuleReach(
                condition_id=condition.id,
                title=condition.title,
                where=where_words(condition),
                status=condition.status,
                need=condition.channel,
                applies_to=condition.applies_to,
                fit=exposure.fit,
                why=exposure.why,
                clears=clears(condition),
                effect=condition.effect,
                reported=condition.last_reported,
                source_url=url,
                publisher=host_of(url) if url else None,
            )
        )
    return {pid: tuple(rows) for pid, rows in out.items()}


@dataclass(frozen=True)
class Blocker:
    """One open obstacle on one project."""

    risk_id: int
    category: str
    severity: str
    summary: str
    track: str | None = None
    quote: str | None = None
    #: The date the source puts on it, or else the day we recorded it.
    since: dt.date | None = None
    recorded: dt.datetime | None = None
    #: When it was last reported: what the two-month window is on.
    reported: dt.date | None = None
    source_url: str | None = None
    publisher: str | None = None
    unconfirmed: str | None = None

    @property
    def days_open(self) -> int | None:
        return (dt.date.today() - self.since).days if self.since else None

    @property
    def label(self) -> str:
        return self.category.replace("_", " ")

    def as_json(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "category": self.category,
            "label": self.label,
            "severity": self.severity,
            "summary": self.summary,
            "track": self.track,
            "track_label": track_label(self.track),
            "quote": self.quote,
            "since": self.since.isoformat() if self.since else None,
            "days_open": self.days_open,
            "recorded": self.recorded.isoformat() if self.recorded else None,
            "reported": self.reported.isoformat() if self.reported else None,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "unconfirmed": self.unconfirmed,
        }


@dataclass(frozen=True)
class Signpost:
    """The next milestone worth watching for on one track, and what it looks like."""

    track: str
    milestone: str
    looks_like: str
    #: True when an open obstacle holds this track, so this milestone arriving is
    #: the obstacle moving.
    blocked: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "track_label": track_label(self.track),
            "milestone": self.milestone,
            "milestone_label": self.milestone.replace("_", " "),
            "looks_like": self.looks_like,
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class ProjectWatch:
    """One followed project: where it stands, what holds it, what to look for."""

    project_id: int
    company: str
    project: str
    location: str
    entry: str | None
    #: Furthest track reached, in words: "construction: groundbreaking".
    stage: str | None
    blockers: tuple[Blocker, ...] = ()
    unconfirmed: tuple[Blocker, ...] = ()
    signposts: tuple[Signpost, ...] = ()
    #: Per track, for a strip: (track, status, blocked, complete).
    tracks: tuple[tuple[str, str, bool, bool], ...] = ()
    #: Open obstacles left out because nobody has reported them in two months.
    older: int = 0
    #: Rules for its place that reach it. Beside the obstacles, never among them:
    #: nothing below reads this, so it moves no count and no order.
    rules: tuple[RuleReach, ...] = ()

    @property
    def worst(self) -> str | None:
        if not self.blockers:
            return None
        return max((b.severity for b in self.blockers), key=severity_rank)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)

    def sort_key(self) -> tuple:
        return (
            -severity_rank(self.worst) if self.worst else 1,
            -len(self.blockers),
            self.company.lower(),
            self.project.lower(),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "company": self.company,
            "company_slug": company_slug(company_key(self.company)),
            "project": self.project,
            "location": self.location,
            "entry": self.entry,
            "stage": self.stage,
            "worst": self.worst,
            "blockers": [b.as_json() for b in self.blockers],
            "unconfirmed": [b.as_json() for b in self.unconfirmed],
            "signposts": [s.as_json() for s in self.signposts],
            "older": self.older,
            "rules": [r.as_json() for r in self.rules],
            "tracks": [
                {
                    "track": track,
                    "label": track_label(track),
                    "status": status,
                    "blocked": blocked,
                    "complete": complete,
                }
                for track, status, blocked, complete in self.tracks
            ],
        }


@dataclass(frozen=True)
class WatchReport:
    """Every followed project, most obstructed first."""

    projects: tuple[ProjectWatch, ...] = ()

    @property
    def blocked(self) -> tuple[ProjectWatch, ...]:
        return tuple(p for p in self.projects if p.blocked)

    @property
    def blockers(self) -> int:
        return sum(len(p.blockers) for p in self.projects)

    def by_severity(self) -> dict[str, int]:
        out = {"blocking": 0, "material": 0, "watch": 0}
        for project in self.projects:
            for blocker in project.blockers:
                out[blocker.severity] = out.get(blocker.severity, 0) + 1
        return out

    def by_rule(self) -> list[dict[str, Any]]:
        """Each rule once, with the followed projects it reaches, newest report first.

        The Monitor page's own section for them. One Texas pause reaching twelve of
        a reader's projects is one thing to watch, not twelve, and what would clear
        it is the same for all twelve.
        """
        rules: dict[int, dict[str, Any]] = {}
        for project in self.projects:
            for reach in project.rules:
                entry = rules.get(reach.condition_id)
                if entry is None:
                    entry = rules[reach.condition_id] = {
                        **{k: v for k, v in reach.as_json().items() if k not in ("fit", "why")},
                        "projects": [],
                    }
                entry["projects"].append(
                    {
                        "project_id": project.project_id,
                        "project": project.project,
                        "company": project.company,
                        "location": project.location,
                        "fit": reach.fit,
                        "why": reach.why,
                    }
                )
        return sorted(
            rules.values(),
            key=lambda r: (r["reported"] or "", r["condition_id"]),
            reverse=True,
        )

    def as_json(self) -> dict[str, Any]:
        by_rule = self.by_rule()
        return {
            "projects": [p.as_json() for p in self.projects],
            "rules": by_rule,
            "counts": {
                "projects": len(self.projects),
                "blocked": len(self.blocked),
                "blockers": self.blockers,
                "unconfirmed": sum(len(p.unconfirmed) for p in self.projects),
                "older": sum(p.older for p in self.projects),
                "window_days": REPORT_WINDOW_DAYS,
                "severity": self.by_severity(),
                # Counted on their own: a project a rule reaches is not "blocked".
                # Restricting and easing apart, so help is never added to a threat.
                "rules": sum(1 for r in by_rule if r["effect"] != EASES),
                "easing": sum(1 for r in by_rule if r["effect"] == EASES),
                "exposed": sum(1 for p in self.projects if any(r.restricts for r in p.rules)),
                "helped": sum(1 for p in self.projects if any(not r.restricts for r in p.rules)),
            },
        }


def _as_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> dt.datetime | None:
    if value is None or isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _location(project: Project) -> str:
    return ", ".join(part for part in (project.city, project.state) if part)


def _blocker(risk, sources: dict[int, Source], *, today: dt.date) -> Blocker:
    recorded = _as_datetime(getattr(risk, "recorded_at", None)) or _as_datetime(risk.created_at)
    source = sources.get(risk.source_id) if risk.source_id else None
    return Blocker(
        risk_id=risk.id,
        category=risk.category,
        severity=risk.severity,
        summary=(risk.summary or "").strip(),
        track=RISK_TRACK.get(risk.category),
        quote=risk.quote,
        since=_as_date(risk.first_seen) or (recorded.date() if recorded else None),
        recorded=recorded,
        reported=reported_on(
            source.published_at if source else None, risk.first_seen, recorded, today=today
        ),
        source_url=source.url if source else None,
        publisher=host_of(source.url) if source else None,
        unconfirmed=risk.unconfirmed,
    )


def project_watch(
    project: Project,
    sources: dict[int, Source],
    *,
    entry: str | None = None,
    today: dt.date | None = None,
    company: str | None = None,
    rules: Sequence[RuleReach] = (),
) -> ProjectWatch:
    """One project's watch entry, from its rows.

    Only obstacles reported inside the window are listed, or block a track here;
    the rest are counted in `older`. See the module docstring.

    `company` is the operator as the console prints it (`company.display_names`);
    without it the row's own spelling is used. `rules` are the rules for its place
    that reach it (:func:`rules_reaching`), carried alongside and read by nothing
    here.
    """
    today = today or dt.date.today()
    stand = standing(project.id, project.events, project.risks)
    order = {"blocking": 0, "material": 1, "watch": 2}
    cutoff = today - dt.timedelta(days=REPORT_WINDOW_DAYS)

    def most_pressing(b: Blocker) -> tuple:
        return (order.get(b.severity, 3), b.since or dt.date.max, b.risk_id)

    every = [
        (risk, _blocker(risk, sources, today=today))
        for risk in project.risks
        if risk.status == OPEN_RISK_STATUS
    ]
    current = [(r, b) for r, b in every if b.reported is not None and b.reported >= cutoff]
    confirmed = sorted((b for r, b in current if r.unconfirmed is None), key=most_pressing)
    held = sorted((b for r, b in current if r.unconfirmed is not None), key=most_pressing)
    confirmed_tracks = {b.track for b in confirmed if b.track}

    signposts: list[Signpost] = []
    blocked_states = sorted(
        (t for t in stand.tracks if t.track in confirmed_tracks and t.next_milestone),
        key=lambda t: TRACKS.index(t.track),
    )
    binding = stand.binding_blocker
    if binding is not None and binding in blocked_states:
        blocked_states.remove(binding)
        blocked_states.insert(0, binding)
    for state in blocked_states:
        signposts.append(
            Signpost(
                track=state.track,
                milestone=state.next_milestone,
                looks_like=NEXT_SIGNAL.get(state.next_milestone, ""),
                blocked=True,
            )
        )
    if not signposts:
        nxt = next((t for t in stand.tracks if not t.complete and t.next_milestone), None)
        if nxt is not None:
            signposts.append(
                Signpost(
                    track=nxt.track,
                    milestone=nxt.next_milestone,
                    looks_like=NEXT_SIGNAL.get(nxt.next_milestone, ""),
                    blocked=False,
                )
            )

    furthest = stand.furthest_track
    stage = None
    if furthest is not None:
        stage = f"{track_label(furthest)}: {stand.track(furthest).status.replace('_', ' ')}"

    return ProjectWatch(
        project_id=project.id,
        company=company or project.company,
        project=project.name,
        location=_location(project),
        entry=entry,
        stage=stage,
        blockers=tuple(confirmed),
        unconfirmed=tuple(held),
        signposts=tuple(signposts),
        tracks=tuple(
            (t.track, t.status, t.track in confirmed_tracks, t.complete) for t in stand.tracks
        ),
        older=len(every) - len(current),
        rules=tuple(rules),
    )


def report(
    session: Session,
    *,
    account_id: int | None = None,
    entities: list[watchlist.Entity] | None = None,
    everything: bool | None = None,
    today: dt.date | None = None,
) -> WatchReport:
    """Every project the account follows, most obstructed first.

    `everything` follows `feed.digest`'s rule when not given: an account that
    turned on `watch_all` follows every project, and with no account (an open
    console) an empty watchlist means every project too.
    """
    from tracker.feed import watches_all

    if entities is None:
        entities = watchlist.watched(session, account_id=account_id)
    if everything is None:
        everything = not entities if account_id is None else watches_all(session, account_id)

    wanted: dict[int, str | None] = {}
    if everything:
        wanted = {}
    else:
        for entity in entities:
            for project_id in entity.matches:
                wanted.setdefault(project_id, entity.entry)
        if not wanted:
            return WatchReport()

    query = (
        select(Project)
        .options(selectinload(Project.events), selectinload(Project.risks))
        .order_by(Project.id.asc())
    )
    if not everything:
        query = query.where(Project.id.in_(list(wanted)))
    projects = list(session.scalars(query).all())

    source_ids = {r.source_id for p in projects for r in p.risks if r.source_id}
    sources = (
        {
            row.id: row
            for row in session.scalars(select(Source).where(Source.id.in_(source_ids))).all()
        }
        if source_ids
        else {}
    )
    from tracker.company import display_names

    names = display_names(session)
    reaching = rules_reaching(session, None if everything else [p.id for p in projects])
    rows = [
        project_watch(
            p,
            sources,
            entry=wanted.get(p.id),
            today=today,
            company=names.company(p.company),
            rules=reaching.get(p.id, ()),
        )
        for p in projects
    ]
    return WatchReport(projects=tuple(sorted(rows, key=ProjectWatch.sort_key)))


__all__ = [
    "COVERS",
    "EASES",
    "EFFECT_LABEL",
    "EMAIL_BLOCKERS_PER_PROJECT",
    "EMAIL_PROJECTS",
    "NEED_LABEL",
    "RULE_STATUS_LABEL",
    "Blocker",
    "ProjectWatch",
    "RuleReach",
    "Signpost",
    "WatchReport",
    "clears",
    "need_label",
    "project_watch",
    "report",
    "rule_status_label",
    "rules_reaching",
    "track_label",
]
