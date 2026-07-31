"""Where a project really stands, and what would prove it is still moving.

The PRD calls this the most important judgement in the exercise:

    最重要的不是简单收集新闻，而是判断一个项目究竟走到了哪一步

A single `phase` enum cannot make it. The PRD's own ladder — 刚宣布 → 已经买地 →
申请政府许可和电力接入 → 平整土地、建设厂房 → 安装设备并投入运营 — is not one ladder
but progress along **five independent tracks**. A campus can own its land outright
and be stuck four years deep in an interconnection queue; another can be fully
permitted with no customer signed. Both are "under construction" to a single enum.

The insight that makes this cheap to build: **stage and obstacle are one structure
viewed twice.** The PRD's obstacle list maps onto the same five tracks, and the
eleven `RISK_CATEGORIES` already partition across them with nothing left over:

    电网没有足够电力 / 变电站输电线路慢   -> power        grid_capacity, transmission
    政府审批时间较长                      -> permits      permitting
    居民担心噪音、用水、环境              -> permits      community_opposition, water,
                                                          environmental
    变压器或冷却设备不能按时交付          -> construction equipment_supply, chip_supply
    资金不足                              -> commercial   financing
    客户尚未确定                          -> commercial   offtake

So a project's *stage* is how far each track has reached, and its *obstacle* is
which track is stuck. That in turn answers the PRD's hardest question —
接下来出现什么信号，才可以证明项目正在继续推进 — structurally rather than by opinion:
**the next unreached milestone on the blocked track.** If power is the blocker the
signal is an interconnection agreement; if permits are, it is a planning-commission
approval.

Everything here is derived from `event` and `risk` rows that already carry their own
dates and citations. Nothing is stored, so nothing can drift out of agreement with
the evidence.
"""  # noqa: RUF002 - the PRD is quoted verbatim, fullwidth punctuation included

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tracker.vocab import severity_rank

#: The five tracks, in the order a project normally advances along them. Order is
#: for display and for choosing which blocker to lead with, not a dependency graph:
#: real projects run several in parallel.
TRACKS: Final[tuple[str, ...]] = (
    "site_control",
    "permits",
    "power",
    "construction",
    "commercial",
)

#: Human labels, and the PRD's own wording for each.
TRACK_LABELS: Final[dict[str, str]] = {
    "site_control": "site control (买地)",
    "permits": "permits (审批)",
    "power": "power (电力接入)",
    "construction": "construction (施工)",
    "commercial": "customer & finance (客户/资金)",
}

#: Milestones per track, earliest first. A track's status is its latest reached
#: milestone; the next entry after that is the signal to watch for.
#:
#: `announced` sits on site_control because an announcement is a commitment to a
#: location and nothing more — it is the PRD's 刚刚宣布, one step below 已经买地.
TRACK_MILESTONES: Final[dict[str, tuple[str, ...]]] = {
    "site_control": ("announced", "land_acquired"),
    "permits": ("permit_filed", "permit_approved"),
    "power": ("interconnection_agreement", "energized"),
    "construction": ("site_work", "groundbreaking", "equipment_install"),
    "commercial": ("first_customer",),
}

#: Risk category to the track it blocks. Complete over RISK_CATEGORIES except
#: `unclassified`, which by definition cannot be placed.
RISK_TRACK: Final[dict[str, str]] = {
    "grid_capacity": "power",
    "transmission": "power",
    "permitting": "permits",
    "community_opposition": "permits",
    "environmental": "permits",
    "water": "permits",
    "equipment_supply": "construction",
    "chip_supply": "construction",
    "financing": "commercial",
    "offtake": "commercial",
}

#: What each milestone would look like in the wild, for the "watch for" line. Kept
#: here rather than in the vocab because it is advice to a human, not a constraint.
NEXT_SIGNAL: Final[dict[str, str]] = {
    "land_acquired": "a recorded deed or an announced land purchase/option",
    "permit_filed": "a rezoning or special-exception application on a county agenda",
    "permit_approved": "a planning-commission or board approval vote",
    "interconnection_agreement": "a signed interconnection agreement, or a utility "
    "filing naming the substation serving the site",
    "site_work": "grading permits, or photos of earthworks on site",
    "groundbreaking": "a groundbreaking event or a building permit issued",
    "equipment_install": "generator/transformer deliveries, or a commissioning notice",
    "energized": "an energization or service-start announcement from the utility",
    "first_customer": "a signed lease or a named anchor tenant",
}

#: A track with no milestone reached and no evidence either way.
UNKNOWN: Final = "unknown"


@dataclass(frozen=True)
class TrackState:
    """One track of one project: how far it got, and what is holding it."""

    track: str
    reached: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    blocker_severity: str | None = None

    @property
    def status(self) -> str:
        """The latest milestone reached on this track."""
        return self.reached[-1] if self.reached else UNKNOWN

    @property
    def next_milestone(self) -> str | None:
        """The next milestone after the furthest one reached, if any remain.

        Milestones on a track are cumulative: a site that is `energized` plainly
        has an interconnection agreement, whether or not any article we read said
        so. Returning the first *unreached* milestone instead asked an energized
        project to go and get its interconnection agreement — caught by reading the
        real output for project 1.
        """
        ladder = TRACK_MILESTONES[self.track]
        furthest = max((ladder.index(m) for m in self.reached), default=-1)
        return ladder[furthest + 1] if furthest + 1 < len(ladder) else None

    @property
    def next_signal(self) -> str | None:
        """What to watch for to prove this track is advancing."""
        nxt = self.next_milestone
        return NEXT_SIGNAL.get(nxt) if nxt else None

    @property
    def is_blocked(self) -> bool:
        return bool(self.blockers)

    @property
    def complete(self) -> bool:
        return self.next_milestone is None


@dataclass(frozen=True)
class ProjectStanding:
    """Every track for one project, plus the read-through an analyst wants."""

    project_id: int
    tracks: tuple[TrackState, ...] = ()

    def track(self, name: str) -> TrackState:
        return next(t for t in self.tracks if t.track == name)

    @property
    def blocked(self) -> tuple[TrackState, ...]:
        return tuple(t for t in self.tracks if t.is_blocked)

    @property
    def binding_blocker(self) -> TrackState | None:
        """The blocked track that matters most.

        Ranked by risk severity first — a `blocking` risk outranks a `watch` — then
        by track order, which puts the earliest unresolved stage first. A project
        stuck on permits *and* short of a customer is really stuck on permits: it
        cannot reach the later problem until the earlier one clears.
        """
        blocked = self.blocked
        if not blocked:
            return None
        return max(
            blocked,
            key=lambda t: (severity_rank(t.blocker_severity or "watch"), -TRACKS.index(t.track)),
        )

    @property
    def watch_for(self) -> str | None:
        """The single next signal worth watching for this project.

        Taken from the binding blocker when there is one, because that is where
        movement actually has to happen. Otherwise from the earliest incomplete
        track — the project is progressing normally and the question is simply
        what comes next.
        """
        target = self.binding_blocker
        if target is None:
            target = next((t for t in self.tracks if not t.complete), None)
        return target.next_signal if target else None

    @property
    def furthest_track(self) -> str | None:
        """The latest track with any milestone reached, for a one-word summary."""
        reached = [t for t in self.tracks if t.reached]
        return reached[-1].track if reached else None


def standing(project_id: int, events, risks) -> ProjectStanding:
    """Derive every track for one project from its events and open risks.

    `events` and `risks` are any iterables of rows carrying `event_type`, and
    `category`/`severity`/`status` respectively. Kept structural rather than
    ORM-typed so this module stays importable and testable without a database.
    """
    reached_types = {getattr(e, "event_type", None) for e in events}

    by_track: dict[str, list] = {t: [] for t in TRACKS}
    severity: dict[str, str] = {}
    for risk in risks:
        # A resolved obstacle is history, not a blocker.
        if getattr(risk, "status", "open") != "open":
            continue
        track = RISK_TRACK.get(getattr(risk, "category", "") or "")
        if track is None:
            continue
        by_track[track].append(risk.category)
        current = severity.get(track)
        this = getattr(risk, "severity", None) or "watch"
        if current is None or severity_rank(this) > severity_rank(current):
            severity[track] = this

    states = []
    for name in TRACKS:
        ladder = TRACK_MILESTONES[name]
        states.append(
            TrackState(
                track=name,
                reached=tuple(m for m in ladder if m in reached_types),
                blockers=tuple(dict.fromkeys(by_track[name])),
                blocker_severity=severity.get(name),
            )
        )
    return ProjectStanding(project_id=project_id, tracks=tuple(states))


__all__ = [
    "NEXT_SIGNAL",
    "RISK_TRACK",
    "TRACKS",
    "TRACK_LABELS",
    "TRACK_MILESTONES",
    "UNKNOWN",
    "ProjectStanding",
    "TrackState",
    "standing",
]
