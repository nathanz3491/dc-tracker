"""One building, sixteen names: finding blocks that are the same tranche.

`blocks.block_key` already folds ordinals hard — "first phase", "Phase I" and
"Phase 1" converge, and word order cannot fork a tranche. What it does not fold,
and deliberately cannot, is **which noun a source chose for the thing**. Measured
on Fairwater (#1), 25 sources and 16 blocks:

    Building 1                  -> building-1     (`building` is a TYPE_WORD, kept as the head)
    Facility 1                  -> 1.fairwater    (`facility` is _NOISE, dropped entirely)
    First datacenter facility   -> 1              (same, and no parent to prefix)
    Building 2                  -> building-2
    Facility 2                  -> 2.fairwater
    Second facility             -> 2
    Area II                     -> area-2         (`area` is neither, so it reads as a place name)

Three keys for one building and four for the next, because `building`, `facility`
and `area` are synonyms that the key builder treats three different ways: one is
kept as the head, one is deleted as noise, and one is mistaken for a distinctive
name.

**Why this is not fixed inside `block_key`.** That function is load-bearing and
pure: `(project_id, block_key)` is the write path's identity, so changing it
re-keys all 286 stored blocks and moves every campus total. Worse, the only
folding that would collapse the seven labels above — dropping the type word
entirely — would also merge `Phase 1` into `Building 1`, and a phase is not a
building. A campus really can have a Building 1 containing a Hall 1 during
Phase 1.

So this module **proposes and never applies**, which is the shape `tracker
duplicates` already established for the same question one level up. It groups by
*designator family* — the ordinal plus the class of subdivision the label names —
and reports each group with what its members disagree about. Two rules keep it
honest:

* **A bare ordinal that could belong to two families is reported as ambiguous,
  not assigned.** "First datacenter facility" reduces to the single segment `1`,
  which is compatible with `Building 1` and equally with `Phase 1`. Guessing
  there is how 400 MW gets folded into a building.
* **Members whose confirmed capacities differ do not get a merge proposal.** They
  get a `collides` verdict instead. Picking a winner silently is exactly how
  `mw_built` MAX put 1,200 MW on Abilene and 13,620 on Hyperion; one level down
  it is harder to see, because nothing sums blocks in public yet.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Final

from tracker.blocks import _segments, mw_is_confirmed

log = logging.getLogger(__name__)

#: Nouns for a subdivision, grouped by what kind of subdivision they name.
#:
#: The classes matter more than the words. Two labels in the *same* class with the
#: same ordinal are two sources naming one thing; two labels in *different*
#: classes are a containment relationship, not a duplicate — a hall sits inside a
#: building, which is built during a phase.
#:
#: `area`, `facility` and `site` are here because `blocks.py` handles them
#: inconsistently: `facility` and `site` are in its `_NOISE` list and vanish,
#: `area` is in neither list and is mistaken for a campus name. That
#: inconsistency is the whole reason Fairwater holds three keys for one building.
SUBDIVISION_CLASSES: Final[dict[str, str]] = {
    # A physical structure.
    "building": "structure",
    "buildings": "structure",
    "bldg": "structure",
    "facility": "structure",
    "facilities": "structure",
    "area": "structure",
    "site": "structure",
    "datacenter": "structure",
    # A slice of the schedule.
    "phase": "phase",
    "phases": "phase",
    "stage": "phase",
    "tranche": "phase",
    "increment": "phase",
    "expansion": "phase",
    # A room inside a structure.
    "hall": "hall",
    "halls": "hall",
    "datahall": "hall",
    "room": "hall",
}

#: The class of a segment whose head is a bare number — "1", from a label like
#: "First datacenter facility" whose every other word was dropped as noise. It
#: names *which* one without saying which kind, so it is compatible with any
#: single class and ambiguous against two.
UNKNOWN_CLASS: Final = "unknown"

#: Fields worth reporting a disagreement on. `mw` first: it is the one that moves
#: a campus total, and the one a merge must refuse over.
COMPARED_FIELDS: Final[tuple[str, ...]] = (
    "mw",
    "status",
    "energized_on",
    "expected_online",
    "customer",
)


@dataclass(frozen=True)
class Member:
    """One block, reduced to what grouping and reporting need."""

    block_id: int
    block_key: str
    label: str
    parent: str | None
    status: str
    mw: float | None
    mw_confirmed: bool
    source_id: int | None
    energized_on: Any = None
    expected_online: Any = None
    customer: str | None = None
    #: `blocks.block_key` marked this label as naming a kind of thing without
    #: saying which campus — "Phase 1" with no parent.
    generic: bool = False

    @classmethod
    def of(cls, block: Any) -> Member:
        return cls(
            block_id=getattr(block, "id", 0),
            block_key=block.block_key or "",
            label=block.label or "",
            parent=getattr(block, "parent", None),
            status=getattr(block, "status", "") or "",
            mw=getattr(block, "mw", None),
            mw_confirmed=bool(getattr(block, "mw", None) is not None and mw_is_confirmed(block)),
            source_id=getattr(block, "source_id", None),
            energized_on=getattr(block, "energized_on", None),
            expected_online=getattr(block, "expected_online", None),
            customer=getattr(block, "customer", None),
            generic=bool(getattr(block, "generic", False)),
        )


@dataclass
class Conflict:
    """One field on which a group's members do not agree."""

    field: str
    values: list[tuple[int, Any]] = dc_field(default_factory=list)
    #: True when two members both *confirm* different values, which is the case a
    #: merge must refuse rather than resolve.
    confirmed_both_ways: bool = False


@dataclass
class Group:
    """Blocks that look like one tranche described more than once."""

    project_id: int
    #: The designator these share, e.g. `structure-1`, or `?-1` when ambiguous.
    family: str
    ordinal: str
    klass: str
    members: list[Member] = dc_field(default_factory=list)
    conflicts: list[Conflict] = dc_field(default_factory=list)
    #: Labels of blocks a bare ordinal could equally have joined. Non-empty means
    #: the group is a question, not a proposal.
    ambiguous_with: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        """`ambiguous` (needs judgement) · `collides` (numbers disagree) · `mergeable`."""
        if self.ambiguous_with:
            return "ambiguous"
        if any(c.confirmed_both_ways for c in self.conflicts):
            return "collides"
        return "mergeable"

    @property
    def evidence(self) -> str:
        """Why these were grouped, in a form a reader can check against the labels."""
        kind = "same ordinal" if self.klass == UNKNOWN_CLASS else f"same {self.klass}"
        return f"{kind} {self.ordinal}"


def segment_family(segment: str) -> tuple[str, str] | None:
    """`(class, ordinal)` for one segment, or None when it carries no ordinal.

    A segment without an ordinal names a place rather than a numbered slice of
    one — `fairwater`, `wisconsin`, `original` — and two of those being the same
    thing is a judgement no amount of string folding settles.
    """
    head, _, tail = segment.partition("-")
    if not tail or not tail.isdigit():
        return None
    if head.isdigit():
        return UNKNOWN_CLASS, tail
    klass = SUBDIVISION_CLASSES.get(head)
    if klass is None:
        # A named designator family: `mke-3`, `azp-2`, `va-1`. The stem *is* the
        # identity, so it forms its own class and never merges across stems.
        return f"named:{head}", tail
    return klass, tail


#: A capacity written into a label: "1.2 GW", "8 MW", "500kW".
#:
#: Stripped before any ordinal is read, because a size is not a designator and the
#: folding in `blocks._segments` cannot tell them apart: `_fold` turns "1.2" into
#: "1 2", so **"Initial 1.2 GW Phase" was claiming ordinal 2** and offering itself
#: as a reading of Building 2. `blocks.py` knows the hazard — its own comment says
#: "8 MW expansion says a size, not a place" — and handles it by marking such keys
#: generic, which is not enough here because a bare ordinal is exactly what this
#: module matches on.
_CAPACITY: Final = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:g|m|k)w\b", re.IGNORECASE)


def families(member: Member) -> set[tuple[str, str]]:
    """Every `(class, ordinal)` a block's own label claims.

    Read from the label and never from `block_key`, because the key has already
    had the parent folded into it — and a parent's ordinal is the *campus's*, not
    this tranche's.
    """
    label = _CAPACITY.sub(" ", member.label or "")
    out: set[tuple[str, str]] = set()
    for segment in _segments(label):
        family = segment_family(segment)
        if family is not None:
            out.add(family)
        # A bare ordinal that survived as its own segment: "1" from "Facility 1",
        # where `facility` was dropped as noise before a head could form.
        if segment.isdigit():
            out.add((UNKNOWN_CLASS, str(int(segment))))
    return out


def _conflicts(members: list[Member]) -> list[Conflict]:
    """Fields the members disagree on, marking the ones both sides confirm."""
    found: list[Conflict] = []
    for name in COMPARED_FIELDS:
        seen: list[tuple[int, Any]] = []
        for member in members:
            value = getattr(member, name, None)
            if value is None or value == "":
                continue
            seen.append((member.block_id, value))
        distinct = {str(v) for _, v in seen}
        if len(distinct) < 2:
            continue
        conflict = Conflict(field=name, values=seen)
        if name == "mw":
            confirmed = {m.mw for m in members if m.mw_confirmed and m.mw is not None}
            conflict.confirmed_both_ways = len(confirmed) > 1
        found.append(conflict)
    return found


def group_blocks(project_id: int, blocks: list[Any]) -> list[Group]:
    """Groups of blocks on one project that look like one tranche.

    Singletons are not reported: this asks which blocks are the *same*, and one
    block is not a question.
    """
    members = [Member.of(b) for b in blocks]
    claims = {m.block_id: families(m) for m in members}

    #: (class, ordinal) -> members, for classes that name a kind of subdivision.
    concrete: dict[tuple[str, str], list[Member]] = {}
    #: ordinal -> members whose label reduced to a bare number and nothing else.
    bare: dict[str, list[Member]] = {}
    for member in members:
        named = {f for f in claims[member.block_id] if f[0] != UNKNOWN_CLASS}
        for family in named:
            concrete.setdefault(family, []).append(member)
        if not named:
            for _, ordinal in claims[member.block_id]:
                bare.setdefault(ordinal, []).append(member)

    # Only true subdivision classes can claim a bare ordinal. A bare ordinal exists
    # *because* a type word was dropped — `facility` and `site` are in
    # `blocks._NOISE` — so the thing it names is a slice of this campus. "Campus One
    # (Durand Avenue)" is numbered 1 too, but "Facility 1" cannot mean Durand
    # Avenue, and letting a named family compete made two resolvable groups
    # ambiguous.
    subdivisions = set(SUBDIVISION_CLASSES.values())
    classes_by_ordinal: dict[str, set[str]] = {}
    for klass, ordinal in concrete:
        if klass in subdivisions:
            classes_by_ordinal.setdefault(ordinal, set()).add(klass)

    groups: list[Group] = []

    for (klass, ordinal), found in concrete.items():
        joined = list(found)
        # A bare ordinal joins a class only when it *is* a subdivision class and
        # exactly one such class holds that ordinal. Two candidates and it is a
        # question, never a coin toss: the two readings differ by a whole
        # building's capacity. Without the `in subdivisions` half, "Campus Two
        # (International Drive)" also claimed "Second facility".
        if klass in subdivisions and len(classes_by_ordinal.get(ordinal, ())) == 1:
            joined += bare.get(ordinal, [])
        if len(joined) < 2:
            continue
        ordered = sorted(dict.fromkeys(joined), key=lambda m: m.block_id)
        groups.append(
            Group(
                project_id=project_id,
                family=f"{klass}-{ordinal}",
                ordinal=ordinal,
                klass=klass,
                members=ordered,
                conflicts=_conflicts(ordered),
            )
        )

    for ordinal, found in bare.items():
        candidates = sorted(classes_by_ordinal.get(ordinal, ()))
        if len(candidates) > 1:
            # Reported rather than assigned: "First datacenter facility" reduces to
            # `1`, which fits Building 1 and fits Phase 1, and picking is how 400 MW
            # gets folded into a building.
            others = sorted({m.label for k in candidates for m in concrete[(k, ordinal)]})
            groups.append(
                Group(
                    project_id=project_id,
                    family=f"?-{ordinal}",
                    ordinal=ordinal,
                    klass=UNKNOWN_CLASS,
                    members=sorted(found, key=lambda m: m.block_id),
                    conflicts=_conflicts(found),
                    ambiguous_with=tuple(others),
                )
            )
        elif not candidates and len(found) > 1:
            ordered = sorted(found, key=lambda m: m.block_id)
            groups.append(
                Group(
                    project_id=project_id,
                    family=f"?-{ordinal}",
                    ordinal=ordinal,
                    klass=UNKNOWN_CLASS,
                    members=ordered,
                    conflicts=_conflicts(ordered),
                )
            )

    # One block belongs in one proposal, so a group wholly contained in a larger
    # one is dropped.
    kept: list[Group] = []
    for group in sorted(groups, key=lambda g: (-len(g.members), g.family)):
        ids = {m.block_id for m in group.members}
        if any(ids <= {m.block_id for m in other.members} for other in kept):
            continue
        kept.append(group)
    return sorted(kept, key=lambda g: (g.klass, g.ordinal))


def scan(session: Any, project_ids: list[int] | None = None) -> list[Group]:
    """Every suspected group in the database, or in the given projects."""
    from sqlalchemy import select

    from tracker.models import CapacityBlock

    query = select(CapacityBlock)
    if project_ids:
        query = query.where(CapacityBlock.project_id.in_(project_ids))
    by_project: dict[int, list[Any]] = {}
    for block in session.scalars(query):
        by_project.setdefault(block.project_id, []).append(block)

    out: list[Group] = []
    for project_id, blocks in sorted(by_project.items()):
        out.extend(group_blocks(project_id, blocks))
    return out


__all__ = [
    "COMPARED_FIELDS",
    "SUBDIVISION_CLASSES",
    "UNKNOWN_CLASS",
    "Conflict",
    "Group",
    "Member",
    "Section",
    "families",
    "group_blocks",
    "scan",
    "sections",
    "segment_family",
]


# --- the campus as sections, which is what a block actually is ---------------

#: Reading order for the classes, so a site plan reads structures then their
#: schedule then the rooms inside them. Within a class, by ordinal.
_CLASS_ORDER: Final[dict[str, int]] = {"structure": 0, "hall": 1, "phase": 2}


@dataclass
class Section:
    """One real subdivision of a campus, however many sources named it.

    **This is the unit a reader wants and the unit the table did not have.** The
    tranche list was ordered by `status` and its arithmetic grouped by evidence
    tier, which answers "what do we believe" — a provenance question. A block is a
    *section of a facility*, so identity is the primary key and state is one of its
    attributes: "Building 2, under construction, delivering 0 of 150 MW".
    """

    key: str
    label: str
    #: Every other name a source gave this same section, for the reader who wants
    #: to check the grouping rather than trust it.
    aliases: tuple[str, ...]
    klass: str
    ordinal: str | None
    status: str
    #: The section's capacity, and how much of it is actually delivering power.
    #: `delivering` is 0 until the section is live, which is the whole distinction
    #: `mw_planned` versus `mw_built` cannot make per building.
    capacity: float | None
    capacity_confirmed: bool
    delivering: float
    parent: str | None
    generic: bool
    source_ids: tuple[int, ...]
    #: Set when two sources confirm different capacities for this one section, in
    #: which case nothing here picks a winner — see `Group.verdict`.
    capacity_conflict: tuple[float, ...] = ()
    verdict: str = "single"
    customer: str | None = None
    energized_on: Any = None
    expected_online: Any = None

    @property
    def sort_key(self) -> tuple[int, int, str, str]:
        """Identity order: class, then ordinal, then name. Never status."""
        return (
            _CLASS_ORDER.get(self.klass, 3),
            int(self.ordinal) if self.ordinal and self.ordinal.isdigit() else 9_999,
            self.label.lower(),
            self.key,
        )


def _canonical(members: list[Member]) -> Member:
    """The member whose label names the section best.

    Prefers a label that says what kind of thing it is *and* numbers it in digits,
    so "Building 2" beats "Area II" and "Second facility". Deterministic, because
    the same campus must not rename itself between two page loads.
    """

    def score(member: Member) -> tuple[int, int, int, str]:
        segments = _segments(_CAPACITY.sub(" ", member.label or ""))
        heads = [s.partition("-")[0] for s in segments]
        named = any(h in SUBDIVISION_CLASSES for h in heads)
        digit = any(char.isdigit() for char in member.label or "")
        return (-int(named), -int(digit), len(member.label or ""), (member.label or "").lower())

    return sorted(members, key=score)[0]


def sections(project_id: int, blocks: list[Any]) -> list[Section]:
    """The campus as its real subdivisions, one entry each, in identity order.

    Duplicate namings collapse into one section — that is what `group_blocks`
    established — but a group whose members disagree on a *confirmed* capacity is
    reported with both figures rather than resolved, for the same reason a merge
    refuses there.
    """
    from tracker.blocks import BLOCK_LIVE, furthest_status

    members = [Member.of(b) for b in blocks]
    grouped: dict[int, Group] = {}
    for group in group_blocks(project_id, blocks):
        for member in group.members:
            grouped[member.block_id] = group

    seen: set[str] = set()
    out: list[Section] = []
    for member in members:
        group = grouped.get(member.block_id)
        family = group.family if group else member.block_key
        if family in seen:
            continue
        seen.add(family)

        crowd = group.members if group else [member]
        head = _canonical(crowd)

        confirmed = {m.mw for m in crowd if m.mw is not None and m.mw_confirmed}
        unconfirmed = {m.mw for m in crowd if m.mw is not None and not m.mw_confirmed}
        if len(confirmed) == 1:
            capacity, capacity_confirmed = next(iter(confirmed)), True
        elif confirmed:
            # Two sources confirm different figures. Show the larger so the bar has a
            # denominator, and carry both so the row can say they disagree.
            capacity, capacity_confirmed = max(confirmed), True
        elif unconfirmed:
            capacity, capacity_confirmed = max(unconfirmed), False
        else:
            capacity, capacity_confirmed = None, False

        status = furthest_status([m.status for m in crowd])
        ordinal = group.ordinal if group else None
        klass = group.klass if group else ""
        if not group:
            fam = next(iter(families(member)), None)
            if fam:
                klass, ordinal = fam
        out.append(
            Section(
                key=family,
                label=head.label,
                aliases=tuple(sorted({m.label for m in crowd} - {head.label})),
                klass=klass or UNKNOWN_CLASS,
                ordinal=ordinal,
                status=status,
                capacity=capacity,
                capacity_confirmed=capacity_confirmed,
                # Nothing is delivering until the section is live. This is the
                # distinction one `mw_built` per campus cannot draw.
                delivering=float(capacity) if capacity and status in BLOCK_LIVE else 0.0,
                parent=head.parent,
                generic=all(m.generic for m in crowd),
                source_ids=tuple(sorted({m.source_id for m in crowd if m.source_id})),
                capacity_conflict=tuple(sorted(confirmed)) if len(confirmed) > 1 else (),
                verdict=group.verdict if group else "single",
                # One value each from whichever member states one. A section named
                # four ways is one building, so its tenant and its dates are the
                # building's, not each reading's.
                customer=next((m.customer for m in crowd if m.customer), None),
                energized_on=next((m.energized_on for m in crowd if m.energized_on), None),
                expected_online=next((m.expected_online for m in crowd if m.expected_online), None),
            )
        )
    return sorted(out, key=lambda s: s.sort_key)
