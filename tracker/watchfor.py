"""What somebody should watch for themselves, on each project they follow.

The daily email and the console's *Watch for* page both read this. The briefing
(`feed`) answers "what moved"; this answers the question a reader is left with on a
day when nothing did: **what is standing between each of my projects and done, and
what would the next piece of good news look like?**

**It is the project's open obstacles, every one, however old.** A permit fight
first reported four months ago that is still unresolved is still the thing most
worth watching, so there is no age gate here — the 45-day rule is about *news*
(`feed.NOTIFY_MAX_AGE_DAYS`), and a blocker is not news, it is a standing fact.
Each carries how long it has been open so an old one reads as what it is.

**What to watch for is the next milestone on the blocked track.** `tracks` already
knows, per track, the rung after the furthest one reached and what that rung looks
like in the wild (`NEXT_SIGNAL`). A blocked track's next rung is exactly the
signal that the obstacle has moved, so it is listed beside the obstacles that hold
it. A project with nothing blocked gets the next step of its earliest unfinished
track instead, the same choice `ProjectStanding.watch_for` makes.

**Unconfirmed obstacles are kept apart**, never mixed in. The console's rule is
that a model's say-so is not a fact; the page shows them, labelled, and the email
leaves them out, exactly as it leaves out unconfirmed updates.

Nothing here is stored. Like `tracks` and `feed`, it is a reading of rows that
already carry their own dates and citations.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker import watchlist
from tracker.models import Project, Source
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
            "project": self.project,
            "location": self.location,
            "entry": self.entry,
            "stage": self.stage,
            "worst": self.worst,
            "blockers": [b.as_json() for b in self.blockers],
            "unconfirmed": [b.as_json() for b in self.unconfirmed],
            "signposts": [s.as_json() for s in self.signposts],
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

    def as_json(self) -> dict[str, Any]:
        return {
            "projects": [p.as_json() for p in self.projects],
            "counts": {
                "projects": len(self.projects),
                "blocked": len(self.blocked),
                "blockers": self.blockers,
                "unconfirmed": sum(len(p.unconfirmed) for p in self.projects),
                "severity": self.by_severity(),
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


def _blocker(risk, sources: dict[int, Source]) -> Blocker:
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
        source_url=source.url if source else None,
        publisher=host_of(source.url) if source else None,
        unconfirmed=risk.unconfirmed,
    )


def project_watch(
    project: Project, sources: dict[int, Source], *, entry: str | None = None
) -> ProjectWatch:
    """One project's watch entry, from its rows."""
    stand = standing(project.id, project.events, project.risks)
    open_risks = [r for r in project.risks if r.status == OPEN_RISK_STATUS]
    order = {"blocking": 0, "material": 1, "watch": 2}

    def most_pressing(b: Blocker) -> tuple:
        return (order.get(b.severity, 3), b.since or dt.date.max, b.risk_id)

    confirmed = sorted(
        (_blocker(r, sources) for r in open_risks if r.unconfirmed is None), key=most_pressing
    )
    held = sorted(
        (_blocker(r, sources) for r in open_risks if r.unconfirmed is not None),
        key=most_pressing,
    )
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
        company=project.company,
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
    )


def report(
    session: Session,
    *,
    account_id: int | None = None,
    entities: list[watchlist.Entity] | None = None,
    everything: bool | None = None,
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
    rows = [project_watch(p, sources, entry=wanted.get(p.id)) for p in projects]
    return WatchReport(projects=tuple(sorted(rows, key=ProjectWatch.sort_key)))


__all__ = [
    "EMAIL_BLOCKERS_PER_PROJECT",
    "EMAIL_PROJECTS",
    "Blocker",
    "ProjectWatch",
    "Signpost",
    "WatchReport",
    "project_watch",
    "report",
    "track_label",
]
