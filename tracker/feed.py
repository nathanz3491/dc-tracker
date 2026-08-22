"""What changed, on the projects somebody asked about, and whether it was good.

The console's landing page and `tracker digest` both read this. The question it
answers is deliberately narrower than the rest of the tool: not "what do we know
about this campus" — Projects answers that, and answers it better — but *what
moved since I last looked, and do I need to worry about it.*

**Nothing here is stored**, exactly as in `tracks.py` and for the same reason:
every signal is a reading of `event`, `risk` and `project` rows that already carry
their own dates and citations, so there is no second copy to drift out of
agreement with the evidence.

Three ideas do the work.

**1. "New" means new to us, not new in the world.** Both clocks are reported, and
they are wildly different on this dataset: a crawl reads one article and imports a
project's whole back-history, so `event_date` spans 1997 to 2040 while the rows
themselves arrived last night. `created_at` (migration 0018) is what the window
filters on, because "tell me what changed since Friday" is a question about our
knowledge. `happened` rides along beside it so a 2022 milestone we only learned
yesterday reads as what it is, rather than as this morning's news.

**2. Good and bad are properties of the vocabulary, not of a model's opinion.**
An `energized` event is good news, a `delayed` event is bad, an obstacle opening
is bad and the same obstacle clearing is good. Every one of those is already a
closed enum in `vocab`, so the sign is a lookup rather than a judgement, and it
cannot say something different tomorrow.

**3. Material means it moved a track.** `SCALE` weights the milestone types the
PRD treats as decisive above the ones that merely restate an intention, and a
signal that reaches the awaited next milestone on a *blocked* track scores higher
still — that is `tracks.ProjectStanding.watch_for` arriving, which is the one
thing this tool exists to notice. Unconfirmed signals are ranked but held back in
their own list: the console's standing rule is that a model's say-so is not a
fact, and a digest is the last place to abandon it.

**4. Notifying is a higher bar than showing.** The page carries everything; a
notification interrupts a person, and a channel that interrupts too often gets
muted, at which point it protects nobody. :func:`notable` is that bar — checkable,
already happened, and material — and it admits five things: the blocker moving, a
decisive milestone, a dated slip, and an obstacle of `material` severity or worse
opening or clearing. An announcement is not one of them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from tracker import watchlist
from tracker.models import Project, Source
from tracker.sources import host_of
from tracker.tracks import RISK_TRACK, TRACK_LABELS, TRACK_MILESTONES, standing
from tracker.vocab import severity_rank

#: Which way a milestone type cuts. Complete over `vocab.EVENT_TYPES`.
#:
#: `announced` and `permit_filed` are neutral on purpose. Both are somebody
#: stating an intention — an announcement is a commitment to a location and
#: nothing more (`tracks` puts it one rung below buying the land), and a filed
#: permit is a queue position, not an approval. Calling either "good news" is how
#: a press release becomes progress.
EVENT_SIGN: Final[dict[str, str]] = {
    "announced": "neutral",
    "land_acquired": "good",
    "permit_filed": "neutral",
    "permit_approved": "good",
    "interconnection_agreement": "good",
    "site_work": "good",
    "groundbreaking": "good",
    "equipment_install": "good",
    "energized": "good",
    "first_customer": "good",
    "delayed": "bad",
    "expanded": "good",
}

#: How much a milestone type says about whether the project is real, 1 to 3.
#:
#: The threes are the PRD's own decisive signals: a signed interconnection
#: agreement is what separates a project that will be energised from one queued
#: behind a substation upgrade; energisation and a first customer are the two ends
#: of actually operating; a dated slip is the bad news nobody volunteers. The ones
#: are visible but cheap — earthworks, an application, a press release.
SCALE: Final[dict[str, int]] = {
    "announced": 1,
    "land_acquired": 2,
    "permit_filed": 1,
    "permit_approved": 2,
    "interconnection_agreement": 3,
    "site_work": 1,
    "groundbreaking": 2,
    "equipment_install": 1,
    "energized": 3,
    "first_customer": 3,
    "delayed": 3,
    "expanded": 2,
}

#: Milestone -> the track it advances, inverted from `tracks.TRACK_MILESTONES` so
#: the two cannot disagree about which track owns which milestone.
EVENT_TRACK: Final[dict[str, str]] = {
    milestone: track for track, ladder in TRACK_MILESTONES.items() for milestone in ladder
}

#: Signal kinds, in the order they are explained to a reader.
KINDS: Final[tuple[str, ...]] = (
    "milestone",
    "obstacle_opened",
    "obstacle_cleared",
    "new_project",
)

#: Default window when a caller names none. A week, because the crawl runs nightly
#: and a reader who was away for a weekend should not have to reconstruct it.
DEFAULT_DAYS: Final[int] = 7

#: For ordering only. `upsert` has its own for the same reason: naive datetimes
#: cannot be turned into a POSIX timestamp before 1970 on every platform.
_EPOCH: Final = dt.datetime(1970, 1, 1)

#: Added to a signal's weight when it is the milestone a blocked track was waiting
#: for. Deliberately large: "the thing that was stuck has moved" is the strongest
#: statement this dataset can make, and it should outrank a bigger milestone on a
#: track nobody was worried about.
UNBLOCKS_BONUS: Final[int] = 3

#: An obstacle's weight is its severity rank plus this. The offset is what puts a
#: *material* obstacle level with a decisive milestone, and it is deliberate: an
#: obstacle appearing is actionable and a milestone is not. Somebody can do
#: something about a permit fight; nobody needs to do anything about a
#: groundbreaking. Without the offset, only `blocking` obstacles ever crossed the
#: notification bar below — which would have dropped exactly the case this feature
#: was asked for, a local group opposing a site, since those are recorded
#: `material` far more often than `blocking`.
OBSTACLE_OFFSET: Final[int] = 2

#: What is worth interrupting somebody for. See :func:`notable`.
NOTIFY_WEIGHT: Final[int] = 3


@dataclass(frozen=True)
class Signal:
    """One thing that changed, on one project, with its evidence."""

    kind: str
    sign: str
    project_id: int
    company: str
    project: str
    #: One line, in the vocabulary's terms: "energized", "community_opposition".
    label: str
    #: The sentence a reader actually reads. The stored description or summary.
    detail: str
    #: When WE learned it (`created_at`), which is what the window filters on.
    at: dt.datetime | None = None
    #: When it happened, or the date the source puts on it. Not the same question.
    happened: dt.date | None = None
    #: What it did to the project's five tracks, in words, or None when it says
    #: nothing about them (a new project, an unclassified obstacle).
    effect: str | None = None
    track: str | None = None
    #: True when this is the milestone a *blocked* track was waiting for.
    unblocks: bool = False
    #: True when the milestone's own date is in the future — an intention, not an
    #: achievement. `tracks.standing` filters these out of a project's standing
    #: for exactly this reason, and the same reasoning applies here: "full Phase 1
    #: **expected** online 2028" is a schedule, and counting it as good news is how
    #: a campus that has never drawn power reads as energised.
    expected: bool = False
    #: How many further rows in this window said the same thing about the same
    #: project. The same real-world moment is recorded once per article, so a
    #: digest that lists each one is a log rather than a digest. See `fold`.
    restatements: int = 0
    quote: str | None = None
    #: The `vocab.UNCONFIRMED_REASONS` code, when the evidence gate did not
    #: confirm this. Not None means the signal is held out of the headline list.
    unconfirmed: str | None = None
    source_url: str | None = None
    publisher: str | None = None
    published_at: dt.datetime | None = None
    weight: int = 1
    #: Which watchlist entry brought this project in, and how it matched.
    entry: str | None = None
    via: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.unconfirmed is None

    @property
    def notify(self) -> bool:
        """Worth interrupting somebody for. See :func:`notable`."""
        return notable(self)

    @property
    def headline(self) -> str:
        """The label, plus what actually happened to it.

        `label` alone is the *category* — "permitting", "community opposition" —
        and for an obstacle that is only half the fact. A resolved risk rendered as
        "permitting" over its own summary sentence read as a live violation on the
        live database: the summary describes the obstacle, because that is what a
        risk row stores. `kind` carries the other half, so the title states it.
        """
        label = self.label.replace("_", " ")
        if self.kind == "obstacle_opened":
            return f"{label} — obstacle"
        if self.kind == "obstacle_cleared":
            return f"{label} — cleared"
        return label

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sign": self.sign,
            "project_id": self.project_id,
            "company": self.company,
            "project": self.project,
            "label": self.label,
            "headline": self.headline,
            "detail": self.detail,
            "at": self.at.isoformat() if self.at else None,
            "happened": self.happened.isoformat() if self.happened else None,
            "effect": self.effect,
            "track": self.track,
            "unblocks": self.unblocks,
            "expected": self.expected,
            "restatements": self.restatements,
            "quote": self.quote,
            "unconfirmed": self.unconfirmed,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "weight": self.weight,
            "notify": self.notify,
            "entry": self.entry,
            "via": self.via,
        }


@dataclass(frozen=True)
class EntityDigest:
    """One watched entity's line at the top of the digest."""

    entry: str
    projects: int
    good: int = 0
    bad: int = 0
    neutral: int = 0
    held: int = 0

    @property
    def total(self) -> int:
        return self.good + self.bad + self.neutral

    def as_json(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "projects": self.projects,
            "good": self.good,
            "bad": self.bad,
            "neutral": self.neutral,
            "held": self.held,
            "total": self.total,
        }


@dataclass(frozen=True)
class Digest:
    """Everything the landing page renders, and what `tracker digest` prints."""

    since: dt.datetime
    #: Quote-backed signals, most material first.
    signals: tuple[Signal, ...] = ()
    #: Signals whose evidence the gate did not confirm. Ranked the same way and
    #: shown separately, never mixed into the list above.
    held: tuple[Signal, ...] = ()
    entities: tuple[EntityDigest, ...] = ()
    #: The most recent citation fetch in the database. A digest with nothing in
    #: it means one of two very different things — a quiet week, or a crawler that
    #: died on Tuesday — and this is what tells them apart.
    last_crawl: dt.datetime | None = None
    #: True when no watchlist exists and the whole database is being read.
    watching_everything: bool = False
    projects_watched: int = 0

    @property
    def notifying(self) -> tuple[Signal, ...]:
        """The subset worth sending. Already ranked, because `signals` is."""
        return tuple(s for s in self.signals if s.notify)

    @property
    def good(self) -> int:
        return sum(1 for s in self.signals if s.sign == "good")

    @property
    def bad(self) -> int:
        return sum(1 for s in self.signals if s.sign == "bad")

    def as_json(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "signals": [s.as_json() for s in self.signals],
            "held": [s.as_json() for s in self.held],
            "entities": [e.as_json() for e in self.entities],
            "last_crawl": self.last_crawl.isoformat() if self.last_crawl else None,
            "watching_everything": self.watching_everything,
            "projects_watched": self.projects_watched,
            "counts": {
                "good": self.good,
                "bad": self.bad,
                "total": len(self.signals),
                "notify": len(self.notifying),
            },
        }


def _as_datetime(value: Any) -> dt.datetime | None:
    """Best effort, because these columns are read from JSON as well as from SQL."""
    if value is None or isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _as_date(value: Any) -> dt.date | None:
    if value is None or (isinstance(value, dt.date) and not isinstance(value, dt.datetime)):
        return value
    if isinstance(value, dt.datetime):
        return value.date()
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _citation(
    sources: dict[int, Source], source_id: int | None
) -> tuple[str | None, str | None, dt.datetime | None]:
    row = sources.get(source_id) if source_id else None
    if row is None:
        return None, None, None
    return row.url, host_of(row.url), _as_datetime(row.published_at)


def _milestone_effect(
    event_type: str, awaited: set[str], *, expected: bool
) -> tuple[str | None, str | None, bool]:
    """What a milestone did to the five tracks, in a reader's words.

    A milestone still in the future did nothing to any track yet, so it says when
    it is due rather than claiming the track moved.
    """
    track = EVENT_TRACK.get(event_type)
    if track is None:
        return None, None, False
    label = TRACK_LABELS[track].split(" (")[0]
    milestone = event_type.replace("_", " ")
    if expected:
        return f"{label} is scheduled to reach {milestone}, not there yet", track, False
    if event_type in awaited:
        return f"{label} was the blocker, and this is what it was waiting for", track, True
    return f"{label} advanced to {milestone}", track, False


def _obstacle_effect(
    category: str, severity: str, *, cleared: bool
) -> tuple[str | None, str | None]:
    track = RISK_TRACK.get(category)
    if track is None:
        # `unclassified` by definition cannot be placed on a track. Saying so is
        # better than picking one.
        return None, None
    label = TRACK_LABELS[track].split(" (")[0]
    if cleared:
        return f"{label} is clear of this obstacle", track
    return f"{label} obstructed ({severity})", track


def _awaited(project: Project, since: dt.datetime) -> set[str]:
    """The milestones this project's blocked tracks were waiting for, before the window.

    Computed `as_of` the start of the window rather than today, so a signal is
    judged against the standing it changed rather than against the standing it
    produced. Without that, an interconnection agreement signed on Monday is
    compared with a Friday reading that already includes it, and the single most
    interesting thing in the dataset — the blocker moving — never fires.
    """
    stand = standing(project.id, project.events, project.risks, as_of=since.date())
    return {t.next_milestone for t in stand.blocked if t.next_milestone}


def signals_for(
    project: Project,
    *,
    since: dt.datetime,
    sources: dict[int, Source],
    as_of: dt.date | None = None,
    entry: str | None = None,
    via: str | None = None,
) -> list[Signal]:
    """Every signal one project produced since `since`.

    `as_of` decides which milestones have actually happened, defaulting to today
    and matching `tracks.standing`'s parameter of the same name.
    """
    out: list[Signal] = []
    as_of = as_of or dt.date.today()
    awaited = _awaited(project, since)

    created = _as_datetime(project.created_at)
    if created is not None and created >= since:
        out.append(
            Signal(
                kind="new_project",
                sign="neutral",
                project_id=project.id,
                company=project.company,
                project=project.name,
                label="new to the tracker",
                detail=(
                    f"{project.company} — {project.name} entered the database"
                    + (f", {project.mw_planned:,.0f} MW planned" if project.mw_planned else "")
                ),
                at=created,
                happened=_as_date(project.first_announced),
                weight=2,
                entry=entry,
                via=via,
            )
        )

    for event in project.events:
        at = _as_datetime(event.created_at)
        if at is None or at < since:
            continue
        happened = _as_date(event.event_date)
        # An undated milestone is kept: somebody recorded it without saying when,
        # which is a different problem from one scheduled for next year. Same call
        # `tracks.standing` makes.
        expected = happened is not None and happened > as_of
        effect, track, unblocks = _milestone_effect(event.event_type, awaited, expected=expected)
        url, publisher, published = _citation(sources, event.source_id)
        out.append(
            Signal(
                kind="milestone",
                sign="neutral" if expected else EVENT_SIGN.get(event.event_type, "neutral"),
                project_id=project.id,
                company=project.company,
                project=project.name,
                label=event.event_type,
                detail=event.description,
                at=at,
                happened=happened,
                effect=effect,
                track=track,
                unblocks=unblocks,
                expected=expected,
                quote=event.quote,
                unconfirmed=event.unconfirmed,
                source_url=url,
                publisher=publisher,
                published_at=published,
                weight=(
                    1
                    if expected
                    else SCALE.get(event.event_type, 1) + (UNBLOCKS_BONUS if unblocks else 0)
                ),
                entry=entry,
                via=via,
            )
        )

    for risk in project.risks:
        url, publisher, published = _citation(sources, risk.source_id)
        resolved = _as_date(risk.resolved_at)
        # An obstacle clears on the date the source dates the resolution: unlike a
        # new row, nothing records when we *read* that it had cleared. So the
        # cleared branch filters on `resolved_at` and says so, rather than
        # pretending `created_at` — the date the obstacle first appeared — is the
        # date it went away.
        if risk.status != "open" and resolved is not None and resolved >= since.date():
            effect, track = _obstacle_effect(risk.category, risk.severity, cleared=True)
            out.append(
                Signal(
                    kind="obstacle_cleared",
                    sign="good",
                    project_id=project.id,
                    company=project.company,
                    project=project.name,
                    label=risk.category,
                    detail=risk.summary,
                    at=None,
                    happened=resolved,
                    effect=effect,
                    track=track,
                    quote=risk.quote,
                    unconfirmed=risk.unconfirmed,
                    source_url=url,
                    publisher=publisher,
                    published_at=published,
                    weight=severity_rank(risk.severity) + OBSTACLE_OFFSET,
                    entry=entry,
                    via=via,
                )
            )
            continue

        at = _as_datetime(risk.created_at)
        if risk.status == "open" and at is not None and at >= since:
            effect, track = _obstacle_effect(risk.category, risk.severity, cleared=False)
            out.append(
                Signal(
                    kind="obstacle_opened",
                    sign="bad",
                    project_id=project.id,
                    company=project.company,
                    project=project.name,
                    label=risk.category,
                    detail=risk.summary,
                    at=at,
                    happened=_as_date(risk.first_seen),
                    effect=effect,
                    track=track,
                    quote=risk.quote,
                    unconfirmed=risk.unconfirmed,
                    source_url=url,
                    publisher=publisher,
                    published_at=published,
                    weight=severity_rank(risk.severity) + OBSTACLE_OFFSET,
                    entry=entry,
                    via=via,
                )
            )

    return out


def notable(signal: Signal) -> bool:
    """Whether this is worth a notification, as against a line on the page.

    The page shows everything; a notification interrupts somebody, and a channel
    that interrupts too often gets muted — at which point it protects nobody. So
    three gates, and a signal has to clear all of them.

    **It has to be checkable.** An unconfirmed signal never notifies, whatever it
    says. Waking somebody at seven in the morning over a sentence no quote stood up
    for is the fastest way to make the whole channel ignorable, and `tracker risks
    confirm` exists precisely to settle those before they are worth acting on.

    **It has to have happened.** A future-dated milestone — "full Phase 1
    *expected* online 2028" — is a schedule. Schedules do not page anybody, and
    treating them as achievements is the fault the live database caught on
    Hyperion.

    **It has to be material**, at `NOTIFY_WEIGHT` or above, which admits exactly
    five things and no others:

    * the awaited milestone on a **blocked** track arriving — the blocker moved,
      which is the strongest statement in this dataset (weight 4+);
    * a decisive milestone: an interconnection agreement signed, a site energised,
      a first customer named (3);
    * a **dated slip** (3) — the bad news nobody volunteers;
    * an obstacle **opening** at `material` or worse (3+): the community, the
      permit, the utility, the money;
    * an obstacle **clearing** at `material` or worse (3+).

    What that deliberately excludes: an announcement, a filed permit, earthworks,
    an equipment delivery, land bought, a new row appearing in the tracker. All of
    those are on the page, and none of them is a reason to look up from something
    else. A watch-severity obstacle is a heads-up, not an alarm.

    **Nothing here remembers what it already sent, and it does not need to.** The
    window is on `created_at`, so a row falls inside exactly one `--days 1` window
    and a nightly job notifies about it exactly once. State would only be needed if
    the schedule overlapped itself, and the fix for that is the schedule.
    """
    if not signal.confirmed or signal.expected:
        return False
    return signal.weight >= NOTIFY_WEIGHT


def fold(signals: list[Signal]) -> list[Signal]:
    """Collapse rows in this window that say the same thing about the same project.

    Two articles reporting one withdrawal produce two `delayed` rows with two
    dates, and `export._timeline_json` already met this on the stored side: "the
    same real-world moment recorded once per article... a flat list of all 72 is
    not a timeline, it is a log". A digest has the same problem and needs the
    same discipline, so signals are grouped by (project, kind, label) and the most
    material one carries the others as `restatements`.

    Nothing is deleted — the count says how many were folded, and every row is
    still in the database — which is what makes this curation rather than
    suppression.
    """
    groups: dict[tuple[int, str, str], list[Signal]] = {}
    for signal in signals:
        groups.setdefault((signal.project_id, signal.kind, signal.label), []).append(signal)

    out: list[Signal] = []
    for group in groups.values():
        best, *rest = rank(group)
        out.append(best if not rest else replace(best, restatements=len(rest)))
    return out


def rank(signals: list[Signal]) -> list[Signal]:
    """Most material first, then most recently learned.

    Bad news sorts above good news of the same weight. Not a moral judgement: an
    obstacle is actionable and a milestone is not, and the whole point of the page
    is that nothing important is below the fold.
    """
    sign_order = {"bad": 0, "good": 1, "neutral": 2}

    def newest_first(when: dt.datetime | None) -> float:
        # Subtraction rather than `.timestamp()`: a naive datetime before the
        # epoch — which `datetime.min` is, and which is the stand-in for an
        # undated signal — raises OSError on Windows rather than returning a
        # negative number.
        return -((when or dt.datetime.min) - _EPOCH).total_seconds()

    return sorted(
        signals,
        key=lambda s: (
            -s.weight,
            sign_order.get(s.sign, 3),
            newest_first(s.at),
            -(s.happened or dt.date.min).toordinal(),
            s.project_id,
        ),
    )


def digest(
    session: Session,
    *,
    since: dt.datetime | None = None,
    days: int | None = None,
    limit: int | None = None,
) -> Digest:
    """The whole page, for the watchlist as it stands.

    With no watchlist the whole database is read and `watching_everything` says so.
    That is the useful default rather than an empty page: a digest that shows
    nothing until somebody configures it teaches nobody what it is for.
    """
    if since is None:
        since = dt.datetime.combine(
            dt.date.today() - dt.timedelta(days=days or DEFAULT_DAYS), dt.time.min
        )

    projects = list(
        session.scalars(
            select(Project)
            .options(
                selectinload(Project.events),
                selectinload(Project.risks),
                selectinload(Project.blocks),
            )
            .order_by(Project.id.asc())
        ).all()
    )
    by_id = {p.id: p for p in projects}

    # Every citation any signal might point at, in one query. Loading
    # `Project.sources` instead would pull every claim envelope on 300 projects to
    # render a publisher name.
    sources = {
        row.id: row for row in session.scalars(select(Source).order_by(Source.id.asc())).all()
    }
    last_crawl = _as_datetime(session.scalar(select(func.max(Source.fetched_at))))

    entities = watchlist.watched(session)
    collected: list[Signal] = []
    digests: list[EntityDigest] = []
    watched_ids: set[int] = set()

    if not entities:
        for project in projects:
            collected.extend(signals_for(project, since=since, sources=sources))
        watched_ids = set(by_id)
    else:
        for entity in entities:
            for project_id, via in entity.matches.items():
                project = by_id.get(project_id)
                if project is None:  # pragma: no cover - resolved from the same query
                    continue
                collected.extend(
                    signals_for(project, since=since, sources=sources, entry=entity.entry, via=via)
                )
            watched_ids |= set(entity.matches)

    # Folded after the confirmed/unconfirmed split, so a quote-backed signal is
    # never hidden behind an unconfirmed restatement of itself.
    shown = rank(fold([s for s in collected if s.confirmed]))
    held = rank(fold([s for s in collected if not s.confirmed]))

    # Counted from the FOLDED lists, and that is the whole point of doing this
    # after the fold rather than before it. Tallying `found` counted one moment
    # once per article that reported it, so a chip read "134 updates" over a list
    # that had 41 cards for the same watch — the same double-count the card list
    # was fixed for, left behind in the number above it.
    #
    # Counted before `limit` is applied, because the chip describes the window and
    # the limit describes the page.
    for entity in entities:
        mine = [s for s in shown if s.entry == entity.entry]
        digests.append(
            EntityDigest(
                entry=entity.entry,
                projects=len(entity.matches),
                good=sum(1 for s in mine if s.sign == "good"),
                bad=sum(1 for s in mine if s.sign == "bad"),
                neutral=sum(1 for s in mine if s.sign == "neutral"),
                held=sum(1 for s in held if s.entry == entity.entry),
            )
        )

    if limit is not None:
        shown, held = shown[:limit], held[:limit]

    return Digest(
        since=since,
        signals=tuple(shown),
        held=tuple(held),
        entities=tuple(digests),
        last_crawl=last_crawl,
        watching_everything=not entities,
        projects_watched=len(watched_ids),
    )


__all__ = [
    "DEFAULT_DAYS",
    "EVENT_SIGN",
    "EVENT_TRACK",
    "KINDS",
    "NOTIFY_WEIGHT",
    "OBSTACLE_OFFSET",
    "SCALE",
    "Digest",
    "EntityDigest",
    "Signal",
    "digest",
    "fold",
    "notable",
    "rank",
    "signals_for",
]
