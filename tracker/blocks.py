"""Capacity blocks: the tranche of a campus that has its own state.

`project` carries one `phase`, one `mw_planned`, one `mw_built`, one `customer`.
That was adequate when a data center was either built and serving customers or
not built. A modern AI campus is several of those at once — 150 MW energised and
serving one buyer, 150 MW under construction pre-leased to another, 300 MW planned
with nobody named — and the row cannot say it.

`tracker/tracks.py` already made half this argument and answered it with five
independent tracks. But tracks are per *project*, so a campus with phase 1
energised and phase 2 unpermitted reports `power: energized, permits: approved` as
though the whole thing were done. A block is the missing dimension.

Three things about the design are load-bearing.

**A block is identified by a derived key, never by a name a source chose.**
`block_key` is a pure function; two sources writing "Phase 1" and "phase one"
converge, and a filing writing "AZP-3 Phase 3" converges with an article writing
"Phase 3" *of AZP-3*. What it will never do is decide that `phase-1` and `azp-2`
are the same thing on a similarity score — that is an operator's call, recorded in
`block_alias`. `tracker/dedup.py` makes the same argument for projects: a wrong
merge is invisible and destroys two facts, a flagged ambiguity is visible and
costs a click.

**A project row is not one campus.** `dedup_key` is `company|city|state`, so one
row holds every facility an operator has in one municipality — AZP-2 and AZP-3 are
two campuses, not two phases of one. So the likeliest way this module could
corrupt data is a generic "Phase 1" from two different campuses colliding on one
key and summing their megawatts. Hence `parent`, the `generic` flag, and excluding
an unplaceable block from the rollup rather than guessing where it belongs.

**The rollup fills, and never overwrites.** `reconcile` may fill a null scalar; it
may not change one that holds a value. A project with no blocks is untouched, so
this stayed safe to turn on over 227 existing rows and the "9 of 12" count can
only go up.

It used to *raise* `mw_planned` to the block sum as well, on the reasoning that a
block sum is a floor on the campus. **That holds only if the blocks partition the
campus, and they routinely do not** — generation belonging to the utility, a
whole-campus figure filed as a sibling tranche, and a milestone repeating capacity
already listed all break it, and Hyperion had all three at once. Summed, they made
`mw_planned` 14,462 MW against the 5,000 that seven sources cite and that the
merge engine had already resolved correctly. A sum above the cited figure is still
worth knowing, so it is disclosed rather than acted on: either something is
double-counted or the cited figure is stale, and arithmetic cannot say which.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

from tracker.vocab import (
    BLOCK_LIVE,
    BLOCK_PROGRESSION,
    BLOCK_STATUS_TO_PHASE,
    BLOCK_TERMINAL,
    DEFAULT_BLOCK_STATUS,
)

#: Words that describe *what kind of thing* a block is rather than which one it is.
#: Kept rather than deleted: a label made only of these is `generic` and cannot be
#: placed without a parent, which is the distinction the ambiguity rule turns on.
#:
#: Deliberately NOT `dedup._GENERIC_NAME_TOKENS` — that list strips `phase` and the
#: digits 1-5, which is exactly the information a block key is made of.
TYPE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "phase",
        "phases",
        "building",
        "buildings",
        "bldg",
        "hall",
        "halls",
        "datahall",
        "tranche",
        "stage",
        "expansion",
        "increment",
        "block",
    }
)

#: Words that carry no information at all in a block label.
#:
#: `a` is here as an article, but it is also the designator in "Building A" — so
#: `_segments` keeps a single letter that directly follows a type word, and only
#: this list's other members are dropped unconditionally.
_NOISE: Final[frozenset[str]] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "at",
        "data",
        "center",
        "centre",
        "datacenter",
        "dc",
        "campus",
        "facility",
        "site",
        "project",
        "and",
        "mw",
        "megawatt",
        "megawatts",
    }
)

#: Ordinal words and roman numerals to digits, so "first phase", "Phase I" and
#: "Phase 1" are one block. This is the single most valuable normalisation here —
#: filings write roman, trade press writes words, and both describe one tranche.
_ORDINALS: Final[dict[str, str]] = {
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
    "fifth": "5",
    "sixth": "6",
    "seventh": "7",
    "eighth": "8",
    "ninth": "9",
    "tenth": "10",
    "initial": "1",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}

#: A designator with a trailing number stuck to it: `AZP-2`, `VA-1`, `DC3`.
_TRAILING_NUM = re.compile(r"^(?P<stem>[a-z]+(?:-[a-z]+)*)-?(?P<num>\d+)$")


def _fold(text: str) -> str:
    """NFKC, lowercase, punctuation to spaces. Same folding `crawl` uses."""
    folded = unicodedata.normalize("NFKC", text or "")
    # Dash and apostrophe variants folded by codepoint, so the literals below stay
    # ASCII and cannot be confused with the characters they replace.
    for fancy, plain in ((chr(0x2013), "-"), (chr(0x2014), "-"), (chr(0x2019), "'")):
        folded = folded.replace(fancy, plain)
    folded = re.sub(r"[^\w\s-]", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


def _segments(label: str) -> list[str]:
    """A label as ordered `designator-ordinal` segments.

    "AZP-3 Phase 3" -> ["azp-3", "phase-3"];  "Building A" -> ["building-a"];
    "first phase"   -> ["phase-1"].
    """
    raw = _fold(label).replace("-", " ").split()
    # Drop noise, except a single letter directly after a type word — that is the
    # designator in "Building A", and `a` is otherwise an article.
    words: list[str] = []
    for index, word in enumerate(raw):
        if not word:
            continue
        after_type = index > 0 and raw[index - 1] in TYPE_WORDS
        if word in _NOISE and not (after_type and len(word) == 1):
            continue
        words.append(word)
    if not words:
        return []

    # Ordinal words are moved onto the token they qualify, so "first phase" and
    # "phase 1" produce the same segment. A literal digit is more specific than a
    # hoisted ordinal word, so it wins: "initial 8" is 8, not 8-then-1.
    normalised: list[str] = []
    pending: str | None = None
    for word in words:
        as_num = _ORDINALS.get(word)
        if as_num is not None and word not in TYPE_WORDS and not word.isdigit():
            pending = as_num
            continue
        normalised.append(word)
        if pending is not None:
            if not word.isdigit():
                normalised.append(pending)
            pending = None
    if pending is not None:
        normalised.append(pending)

    out: list[str] = []
    index = 0
    while index < len(normalised):
        word = normalised[index]
        stuck = _TRAILING_NUM.match(word)
        if stuck:
            out.append(f"{stuck['stem']}-{int(stuck['num'])}")
            index += 1
            continue
        ordinal = None
        if index + 1 < len(normalised):
            nxt = normalised[index + 1]
            if nxt.isdigit():
                ordinal = str(int(nxt))
            elif len(nxt) == 1 and nxt.isalpha() and word in TYPE_WORDS:
                ordinal = nxt  # "Building A"
            elif nxt in _ORDINALS and word in TYPE_WORDS:
                ordinal = _ORDINALS[nxt]
        if ordinal is not None:
            out.append(f"{word}-{ordinal}")
            index += 2
        else:
            out.append(word)
            index += 1
    return out


@dataclass(frozen=True)
class Key:
    """A block's identity, and whether it can stand on its own."""

    value: str
    #: True when every segment is a type word — "Phase 1", "Building A". Such a
    #: label says which tranche of *something* without saying of what, so it
    #: cannot be placed in a project row that holds more than one campus.
    generic: bool


def block_key(label: str, parent: str | None = None) -> Key:
    """The identity of one block. Pure, so re-ingest is idempotent.

    `parent` is what makes a filing's "AZP-3 Phase 3" and an article's "Phase 3"
    (of AZP-3) converge — the reason project 39's third facility can be recognised
    at all.

    **Segments are sorted, so word order cannot fork one tranche into two.** Found on
    Lake Mariner during the backfill: two articles wrote "La Lupa (Core42 Leases)"
    and "Core42 Leases (La Lupa)", and the row ended up holding the same 60 MW
    tranche twice under two keys. Order carries no meaning here — a block is a set of
    designators, not a phrase — and every case where order looked meaningful
    ("AZP-3 Phase 3") converges either way.
    """
    segments = _segments(label)
    if not segments:
        slug = re.sub(r"[^a-z0-9]+", "-", _fold(label)).strip("-")
        return Key(slug or "block", generic=True)

    # A segment names a *kind* of thing, not which campus, when its head is a type
    # word or a bare number — "8 MW expansion" says a size, not a place, and must
    # not be counted as a designator family that licenses summing.
    generic = all(
        seg.split("-")[0] in TYPE_WORDS or seg.split("-")[0].isdigit() for seg in segments
    )
    if generic and parent:
        prefix = _segments(parent)
        if prefix and not all(s.split("-")[0] in TYPE_WORDS for s in prefix):
            return Key(".".join(sorted({*prefix, *segments})), generic=False)
    return Key(".".join(sorted(set(segments))), generic=generic)


def label_tokens(label: str, parent: str | None = None) -> frozenset[str]:
    """Every token a quote could plausibly use to name this block.

    Feeds the per-block evidence check: a verified quote has to actually mention
    the block it is cited for. Includes each ordinal in *all* its forms, because a
    filing writes "Phase III" where an article writes "third phase".
    """
    out: set[str] = set()
    reverse = {v: k for k, v in _ORDINALS.items() if len(k) > 2}
    for source in (label, parent):
        for segment in _segments(source or ""):
            head, _, tail = segment.partition("-")
            out.add(head)
            if tail:
                out.add(tail)
                for word, digit in _ORDINALS.items():
                    if digit == tail:
                        out.add(word)
                if tail in reverse:
                    out.add(reverse[tail])
    return frozenset(out - _NOISE)


def segment_requirements(label: str, parent: str | None = None) -> list[tuple[str, frozenset[str]]]:
    """What a quote must contain to be talking about this block.

    One entry per segment: `(head, every form of its ordinal)`. A quote satisfies a
    segment when the head appears **and**, if the segment carries an ordinal, some
    form of that ordinal appears too.

    Requiring the ordinal is the whole point. `AZP-2` and `AZP-3` share the stem
    "azp", so a stem-only test accepts a sentence about AZP-3 as evidence for
    AZP-2 — which is project 39's failure reproduced inside the very check meant to
    prevent it. What distinguishes two tranches of one campus is nearly always the
    number.
    """
    reverse = {v: k for k, v in _ORDINALS.items() if len(k) > 2}
    out: list[tuple[str, frozenset[str]]] = []
    for source in (label, parent):
        for segment in _segments(source or ""):
            head, _, tail = segment.partition("-")
            if not tail:
                out.append((head, frozenset()))
                continue
            forms = {tail}
            forms.update(word for word, digit in _ORDINALS.items() if digit == tail)
            if tail in reverse:
                forms.add(reverse[tail])
            out.append((head, frozenset(forms)))
    return out


def is_type_word_only(label: str) -> bool:
    """Whether a label names a kind of thing without naming which campus."""
    return block_key(label).generic


#: Words that mark a named thing as GENERATING or DELIVERING power rather than
#: consuming it. Taken from the vocabulary `prompts/_industry.txt` §2 enumerates,
#: so the prompt that refuses to emit these as blocks and the rollup that refuses
#: to count them cite one list.
#:
#: These belong to the utility, not the operator, and they are measured in a
#: different kind of megawatt — a plant's nameplate output against a data center's
#: IT load. Generation to serve a site is normally LARGER than the load: gas runs
#: 1.1-1.5x for reserve margin, solar 3-4x because it produces about a quarter of
#: nameplate over a year.
_GENERATION_WORDS: Final[frozenset[str]] = frozenset(
    {
        "battery",
        "bess",
        "ccct",
        "ccgt",
        "generating",
        "generation",
        "grid",
        "interconnection",
        "nuclear",
        "plant",
        "plants",
        "powerplant",
        "ppa",
        "solar",
        "storage",
        "substation",
        "transmission",
        "turbine",
        "turbines",
        "uprate",
        "uprates",
        "wind",
    }
)


def is_generation(label: str | None, parent: str | None = None) -> bool:
    """Whether a block names generation or grid plant rather than data center.

    Hyperion (#10) recorded 5,962 MW of Entergy's gas plants, solar farm and
    generating facilities as tranches of the campus, and `rollup` summed them into
    `mw_planned` alongside the data halls. Two different quantities were added
    together and the result — 14,462 MW — was published as the size of a data
    center, roughly three times the largest campus announced anywhere.

    A predicate rather than a stored `kind` column, deliberately: a column needs a
    migration, cannot be backfilled without re-reading every article, and would be
    a derived judgement frozen into a cache — which is how caches drift. This reads
    the label the article itself used, every time.

    `parent` is checked too because a bare "Unit 1" under a parent named for a
    power station is the station's unit, not the campus's.
    """
    words = set()
    for text in (label, parent):
        if text:
            words.update(part.strip(".,()") for part in str(text).lower().split())
    return bool(words & _GENERATION_WORDS)


# --- merge policy ------------------------------------------------------------

#: How to pick one value when two sources describe the same block differently.
#: Mirrors `upsert.FIELD_POLICY` and reuses its engine, so blocks inherit the
#: confirmed-first discipline (a 待确认 value never displaces a quoted one) rather
#: than reimplementing it.
#:
#: `mw` is PREFER_WEIGHT, not MAX. A block's size is one design figure; MAX would
#: let a campus total quoted beside a phase name inflate that block permanently,
#: which is the mistake `crawl.MAX_USD_PER_MW` exists to catch elsewhere.
BLOCK_POLICY: Final[dict[str, str]] = {
    "label": "prefer_weight",
    "parent": "fill_only",
    "mw": "prefer_weight",
    "status": "ladder",
    "customer": "prefer_weight",
    "expected_online": "prefer_weight",
    "energized_on": "min",
    "investment_usd": "prefer_weight",
}

_RANK: Final[dict[str, int]] = {name: i for i, name in enumerate(BLOCK_PROGRESSION)}


def furthest_status(statuses: list[str]) -> str:
    """The status furthest along the ladder, unless something says it stopped."""
    present = [s for s in statuses if s]
    if not present:
        return DEFAULT_BLOCK_STATUS
    terminal = [s for s in present if s in BLOCK_TERMINAL]
    if terminal:
        return terminal[0]
    return max(present, key=lambda s: _RANK.get(s, -1))


# --- accounting: the parts must always sum to the whole -----------------------
#
# Measured on the live database after the backfill: on **70 of 118** itemised
# projects the tranches summed to *less* than the campus total, because capacity
# that could not be trusted was quietly left out. Every one of those reads on screen
# as 250 + 150 ≠ 750, and the conclusion a reader draws from that is not "one of
# those figures is unconfirmed" — it is that we cannot add up. A 待确认 badge and a
# footnote do not repair it; the arithmetic has to close.
#
# So nothing is silently excluded any more. Capacity we will not commit to is still
# *accounted for*, on a line that says where it went. The difference between
# excluding a number and naming it is the whole difference between a dataset that
# looks careless and one that looks careful.

#: Why a slice of a campus is not in the committed total. Ordered as displayed.
#:
#: The last three are set aside by a ruling on how the tranches nest (`structure`):
#: like `generation` and `out_of_scale` they are named beside the arithmetic and
#: kept out of it.
RESIDUAL_REASONS: Final[tuple[str, ...]] = (
    "unconfirmed",
    "unplaceable",
    "unitemised",
    "generation",
    "overlap",
    "out_of_scale",
    "same_as",
    "superseded",
    "elsewhere",
)

#: Residuals named beside the total rather than inside it. See `accounted_mw`.
OUTSIDE_THE_TOTAL: Final[frozenset[str]] = frozenset(
    {"overlap", "out_of_scale", "generation", "same_as", "superseded", "elsewhere"}
)

#: A tranche cannot be several times larger than the campus it is part of.
#:
#: Portland 3 came back with a "Hillsboro Phase 1" of 36,000 MW against a cited 144 MW
#: campus — a kilowatt figure read as megawatts. Left in the arithmetic it was
#: absorbed by the `overlap` line as "counted twice over -35,988 MW", which is a
#: sentence that means nothing: nothing was counted twice, one number is wrong by
#: three orders of magnitude. Naming it correctly is the difference between a dataset
#: that catches a bad reading and one that dresses it up.
OUT_OF_SCALE: Final = 3.0

_RESIDUAL_NOTES: Final[dict[str, str]] = {
    "unconfirmed": "stated, but no quote in the article names that tranche",
    "unplaceable": "names a phase without saying of which facility",
    "unitemised": "no source breaks this part of the campus down",
    "generation": "a utility's plant, not the data center — generation is a different megawatt",
    "overlap": "tranches sum past the cited campus total — probably one described twice",
    "out_of_scale": "larger than the whole campus — almost always kilowatts read as megawatts",
    "same_as": "the same tranche under another name — counted once, under that name",
    "superseded": "an earlier figure the sources have since replaced",
    "elsewhere": "describes another campus, not this one",
}


@dataclass(frozen=True)
class Residual:
    """One slice of a campus that is accounted for but not committed."""

    reason: str
    mw: float
    #: The tranches this line stands for, where it stands for specific ones.
    labels: tuple[str, ...] = ()

    @property
    def note(self) -> str:
        return _RESIDUAL_NOTES.get(self.reason, "")


@dataclass(frozen=True)
class Accounting:
    """Every megawatt of one campus, placed on exactly one line.

    `counted_mw` plus the residuals equal `total`. That identity is the point, and
    `test_the_accounting_closes_on_every_live_project` asserts it across the whole
    database rather than trusting it here.
    """

    total: float | None
    counted_mw: float
    residuals: tuple[Residual, ...] = ()
    #: True when no source states a campus figure, so the total shown is the sum of
    #: the parts — a floor, and labelled as one rather than presented as the total.
    total_is_floor: bool = False
    #: What the counted tranches marked live (serving, energized) add up to, as the
    #: sources describe them. Not the campus's running figure: sources describe one
    #: building under several names, and Abilene's live tranches summed to 1,600 MW
    #: against a 1,200 MW campus. See `delivering`.
    live_mw: float = 0.0
    #: `counted_mw` by tranche state, furthest first — what the panel's bar draws, so
    #: the browser never sums tranches itself (a nested building would count twice).
    by_state: tuple[tuple[str, float], ...] = ()

    @property
    def accounted_mw(self) -> float:
        """What the lines inside the total add up to. Equals `total` by construction.

        A utility's plant (`generation`) and a figure too large to be the campus's
        (`out_of_scale`) are named beside the arithmetic and kept out of it. Adding the
        plant in made seven campuses fail to close on the 10-21 copy — Newark's 279 MW
        cogeneration read as 558 MW accounted for a 279 MW campus — and nothing noticed,
        because no check ran this across the database until the invariants register did.
        """
        over = sum(r.mw for r in self.residuals if r.reason == "overlap")
        rest = sum(r.mw for r in self.residuals if r.reason not in OUTSIDE_THE_TOTAL)
        return self.counted_mw + rest - over

    @property
    def closes(self) -> bool:
        """Whether the lines add up. Vacuously true when there is no capacity at all.

        15 live projects name tranches without sizing any of them — a real state
        ("VA2 is under construction", no megawatts published). There is nothing to
        reconcile there, and reporting it as an open discrepancy would be inventing
        a fault to go with a number nobody stated.
        """
        if self.total is None:
            return abs(self.accounted_mw) < 0.51
        return abs(self.accounted_mw - self.total) < 0.51


def account(project: Any, nested: Structure | None = None) -> Accounting:
    """Break a campus into lines that sum to its total. Never writes.

    The residuals are not a rounding plug. Each names a *reason* capacity is not in
    the committed figure, and each reason implies different work: `unconfirmed` wants
    a citation, `unplaceable` wants an operator to say which facility, `unitemised`
    wants another article, `overlap` wants a ruling on which tranche is part of which.

    **The tranches are summed as the tree they form** (`structure`), not as siblings:
    a tranche counts what its parts add up to, or its own figure where that is larger
    — the remainder nobody broke down. A campus described once as a whole and again
    building by building used to count both, which is how Abilene's 1,200 MW read as
    14,729 MW of tranches. A tranche ruled the same as another, superseded, or
    describing another campus is set aside on a line of its own, beside the total.
    """
    blocks = list(getattr(project, "blocks", ()) or ())
    # Generation is split off first, exactly as `rollup` does, and then reported as
    # its own residual. Dropping it silently would leave the reader looking at a
    # campus figure with 5,962 MW of gas plants and solar unaccounted for and no
    # line saying where they went — and this module's whole discipline is that
    # nothing leaves the arithmetic without being named.
    generation = [b for b in blocks if is_generation(b.label, getattr(b, "parent", None))]
    blocks = [b for b in blocks if b not in generation]
    nested = nested if nested is not None else structure(project)

    def running(items: list[Any]) -> list[Any]:
        # A cancelled tranche is not part of anybody's campus total, and neither is
        # its capacity — the same reason `rollup` leaves terminal blocks out.
        return [b for b in items if b.mw and b.status not in BLOCK_TERMINAL]

    cited_total = getattr(project, "mw_planned", None)

    def implausible(block: Any) -> bool:
        return bool(cited_total) and block.mw is not None and block.mw > cited_total * OUT_OF_SCALE

    # Only tranches that are still live. A cancelled one is already outside the
    # campus total, so calling its figure implausible as well is noise about a
    # number nobody is counting.
    rejected = [b for b in running(blocks) if implausible(b)]
    tree = _sum_tree(blocks, nested, skip={b.block_key for b in rejected})

    counted_mw = tree.placed.confirmed
    live_mw = tree.placed.live
    itemised = tree.placed.confirmed + tree.placed.unconfirmed + tree.unplaced.total

    cited = cited_total
    total, floor = cited, False
    if cited is None:
        total = itemised or None
        floor = True

    residuals: list[Residual] = []
    for reason, mw, labels in (
        ("unconfirmed", tree.placed.unconfirmed, tree.unconfirmed_labels),
        ("unplaceable", tree.unplaced.total, tree.unplaced_labels),
        ("generation", sum(b.mw for b in running(generation)), _labels(running(generation))),
        ("out_of_scale", sum(b.mw for b in rejected), _labels(rejected)),
        *((reason, *tree.set_aside[reason]) for reason in SET_ASIDE),
    ):
        if mw > 0:
            residuals.append(Residual(reason, mw, labels))
    if total is not None:
        gap = total - itemised
        if gap > 0.5:
            residuals.append(Residual("unitemised", gap))
        elif gap < -0.5:
            residuals.append(Residual("overlap", -gap))

    by_state: dict[str, float] = {}
    for key in tree.in_placed:
        rest = tree.remainder.get(key)
        if rest is not None and rest.confirmed > 0:
            state = tree.status[key]
            by_state[state] = by_state.get(state, 0.0) + rest.confirmed

    return Accounting(
        total=total,
        counted_mw=counted_mw,
        residuals=tuple(residuals),
        total_is_floor=floor,
        live_mw=live_mw,
        by_state=tuple(sorted(by_state.items(), key=lambda kv: -_RANK.get(kv[0], -1))),
    )


def _labels(blocks: list[Any]) -> tuple[str, ...]:
    return tuple(sorted(b.label for b in blocks))


#: The tranche states that mean the capacity is running.
LIVE_STATUSES: Final = frozenset({"serving", "energized"})


def delivering(project: Any, got: Accounting) -> float | None:
    """The running capacity the tranche panel's headline shows: the campus's own.

    The client saw Abilene as "1,200 MW planned, 240 MW running" on the cards and
    "1,600 MW delivering of 1,200 MW" above the tranches, on one page. The headline
    summed every quoted tranche marked live, and the sources describe the campus as
    a whole-site tranche and again as the buildings inside it. So the headline is
    the row's reconciled running figure (`mw_built`), the number every other surface
    shows; only a campus with no running figure stated falls back to its live
    tranches, never more than the campus total. `Accounting.live_mw` keeps what the
    tranches themselves describe, for the line that says they overlap.
    """
    built = getattr(project, "mw_built", None)
    if built is not None:
        return float(built)
    if not got.live_mw:
        return 0.0 if got.total is not None else None
    return min(got.live_mw, got.total) if got.total is not None else got.live_mw


#: What we know about whether a campus has been broken into tranches.
#:
#: The point is to stop a bare row from looking like a lapse. Before this, a project
#: with no blocks and a project nobody had read looked identical on screen, so 88
#: bare rows beside 118 detailed ones read as uneven research rather than as what it
#: is: most of those campuses really are one undivided thing. Same argument
#: `gaps.py` makes about NULL — an empty value is not automatically a gap.
ITEMISED: Final = "itemised"
SINGLE_BLOCK: Final = "single_block"
UNREAD: Final = "unread"
NO_ARTICLE: Final = "no_article"

ITEMISATION_NOTES: Final[dict[str, str]] = {
    ITEMISED: "broken into tranches",
    SINGLE_BLOCK: "read, and no source breaks it into tranches",
    UNREAD: "not yet read for tranches",
    NO_ARTICLE: "no article to read — derived or queue data only",
}


def itemisation(project: Any) -> str:
    """Whether this campus has tranches, has been read, or is simply one thing.

    Depends on the backfill recording an empty read as `blocks = '[]'` rather than
    leaving the column NULL. Without that, "read it, it is one campus" and "never
    read it" are the same value, which is why the backfill could not converge and
    re-read 40% of its articles.
    """
    if getattr(project, "blocks", None):
        return ITEMISED
    crawled = [
        s
        for s in (getattr(project, "sources", ()) or ())
        if (s.extractor or "").startswith("crawl:")
    ]
    if not crawled:
        return NO_ARTICLE
    if all(s.blocks is not None for s in crawled):
        return SINGLE_BLOCK
    return UNREAD


# --- the rollup --------------------------------------------------------------


@dataclass(frozen=True)
class Rollup:
    """What the blocks say the project's scalars should be, at minimum."""

    mw_planned: float | None
    mw_built: float | None
    phase: str | None
    customer: str | None
    #: Blocks whose identity is too vague to place, so excluded from the sums.
    unplaceable: tuple[str, ...] = ()
    #: Blocks carrying a capacity no quote confirmed, so excluded from the sums.
    uncited: tuple[str, ...] = ()
    #: Every distinct customer the blocks name, largest first, for disclosure.
    customers: tuple[tuple[str, float], ...] = ()
    #: Blocks naming generation or grid plant, excluded from the sums because they
    #: are a utility's megawatts and not the data center's. Kept so `account` can
    #: name them on screen rather than dropping them silently.
    generation: tuple[str, ...] = ()
    #: The blocks each sum was taken over, by key. A campus figure `reconcile` filled
    #: from them has no citation of its own, and without these the morning report
    #: (`changes.backing`) could only say nothing stood behind it — which it said of
    #: #1608's 3 MW on 2026-10-19, a figure one quoted tranche states.
    planned_from: tuple[str, ...] = ()
    built_from: tuple[str, ...] = ()


def placeable(
    blocks: list[Any], *, families: int | None = None, nested: Structure | None = None
) -> list[Any]:
    """Blocks that can safely be summed.

    A generic block with no parent, in a project that holds more than one
    designator family, might belong to either campus. Its megawatts are excluded
    rather than guessed at — the same floor discipline the rest of the project
    uses, and the alternative is silently double-counting two campuses.

    A ruling places one (`nested`): "Phase 2" that a model or a person ruled part of
    a named building, or its own slice of the campus, is no longer a guess.
    """
    if families is None:
        families = len({b.block_key.split(".")[0] for b in blocks if not b.generic})
    if families <= 1:
        return list(blocks)
    ruled = set()
    if nested is not None:
        ruled = {
            key
            for key, link in nested.links.items()
            if link.relation in ("part_of", "separate") and not link.automatic
        }
    return [b for b in blocks if not b.generic or b.parent or b.block_key in ruled]


def mw_is_confirmed(block: Any) -> bool:
    """Whether a quote in the article actually named this block's capacity.

    Blocks keep an unconfirmed figure rather than dropping it — that is what the
    待确认 tier is for, and a number somebody can check beats a null. But keeping a
    value and *summing* it are different acts, and conflating them cost a real
    1000x error: the first backfill tranche raised Applied Digital Jamestown from
    7 MW to 7,500 MW on a single 待确认 block, and `reconcile` records no tier, so
    the campus then asserted that figure as though it were cited.

    So an unconfirmed capacity is shown and not counted. That is the same floor
    discipline as `placeable`, applied to the other way a block's megawatts can be
    untrustworthy: not "we cannot tell whose it is" but "nothing said it".
    """
    raw = getattr(block, "unconfirmed_fields", None)
    if not raw:
        return True
    return "mw" not in {part.strip() for part in raw.split(",")}


# --- how the tranches nest -----------------------------------------------------
#
# Sources describe one campus at several grains at once: the campus as a whole, its
# phases, the buildings inside them, one building under three names, an earlier plan
# the campus outgrew, now and then another site entirely. Summed as siblings they
# counted the same megawatts several times — 123 campuses on the 10-21 copy, 101,892
# MW — and the arithmetic could only report the excess as `overlap`.
#
# A `Structure` says how they relate: which tranche is part of which, which are one
# tranche under two names, which describe something no longer planned or somewhere
# else. Judgement comes from `block_nesting` (a model's or a person's ruling); what
# arithmetic settles alone is derived here on every read, and never stored, for the
# reason `is_generation` gives about a stored `kind`: a derived judgement frozen into
# a cache is how caches drift.

#: Rulings that take a tranche out of the sum, each named on its own residual line.
SET_ASIDE: Final[tuple[str, ...]] = ("same_as", "superseded", "elsewhere")

#: Who decided a link that no stored ruling made. The suffix says which rule.
AUTO_NAMES: Final = "auto:names"
AUTO_PARENT: Final = "auto:parent"
AUTO_CAMPUS: Final = "auto:campus"

#: Words that mark a tranche as an increment of a campus, never the campus itself,
#: whatever its size: "another 3.75 GW", "Option for another 380 MW".
_INCREMENT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "additional",
        "another",
        "expansion",
        "expansions",
        "extra",
        "first",
        "further",
        "future",
        "initial",
        "more",
        "next",
        "option",
        "remaining",
        "second",
        "third",
    }
)


#: Words that mark a tranche as added to what it names rather than part of it: an
#: "AMD Expansion" of "Initial AMD Capacity" is beside that capacity, not inside it.
_ADDED_WORDS: Final[frozenset[str]] = frozenset(
    {"additional", "another", "expansion", "expansions", "extra", "further", "more", "option"}
)


def adds_to(block: Any) -> bool:
    """Whether a tranche's label says it adds to its parent rather than being a part of it."""
    return bool(set(_fold_label(block.label).split()) & _ADDED_WORDS)


@dataclass(frozen=True)
class Link:
    """How one tranche relates to its campus's others."""

    key: str
    #: From `vocab.NEST_RELATIONS`.
    relation: str
    target: str | None = None
    #: 'model', a person's account name, or one of the `AUTO_*` rules.
    by: str = AUTO_NAMES
    reason: str = ""
    confidence: float | None = None

    @property
    def automatic(self) -> bool:
        return self.by.startswith("auto:")

    @property
    def by_model(self) -> bool:
        return self.by == "model"

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "relation": self.relation,
            "target": self.target,
            "by": self.by,
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Structure:
    """Every link in one campus, by tranche key. Tranches with none are top level."""

    links: dict[str, Link]
    #: Stored rulings naming a tranche no source states any more. Kept, not applied:
    #: the next re-read may state it again.
    dormant: tuple[Link, ...] = ()

    def link(self, key: str) -> Link | None:
        return self.links.get(key)

    def canonical(self, key: str) -> str:
        """The tranche this one is, following `same_as` to its end."""
        seen = {key}
        link = self.links.get(key)
        while link is not None and link.relation == "same_as" and link.target not in seen:
            key = link.target or key
            seen.add(key)
            link = self.links.get(key)
        return key

    def parent(self, key: str) -> str | None:
        link = self.links.get(key)
        if link is None or link.relation != "part_of" or not link.target:
            return None
        return self.canonical(link.target)


EMPTY_STRUCTURE: Final = Structure({})


def closes_cycle(links: dict[str, Link], key: str, target: str | None) -> bool:
    """Whether linking `key` to `target` would make a tranche part of itself."""
    seen: set[str] = set()
    while target is not None and target not in seen:
        if target == key:
            return True
        seen.add(target)
        link = links.get(target)
        target = (
            link.target if link is not None and link.relation in ("part_of", "same_as") else None
        )
    return False


def _fold_label(text: str | None) -> str:
    return " ".join(_fold(text or "").replace("-", " ").split())


def campus_like(block: Any) -> bool:
    """Whether a tranche's label could name the whole campus: no ordinal, no increment.

    "Abilene Campus", "Full buildout", "Sweetwater Hub" — and not "Building 1",
    "Phase 2" or "another 3.75 GW", whatever their size.
    """
    from tracker.blockcheck import Member, families

    if families(Member.of(block)):
        return False
    words = set(_fold_label(block.label).split())
    return not words & _INCREMENT_WORDS


def structure(project: Any, *, automatic: bool = True) -> Structure:
    """How a campus's tranches nest: stored rulings, then the automatic rules. Never writes.

    Stored rulings come first, a person's before a model's, and each is applied only
    while both tranches it names still exist. Then, for tranches nobody ruled on,
    three rules arithmetic and the sources' own words can settle:

    * **one tranche under several names** — the groups `blockcheck` finds with no
      confirmed figure in dispute ("Building 1", "Facility 1", "First facility")
      become one, under the name with the best-supported figure;
    * **a stated parent that is itself a tranche** — "Building 2" of "Colossus 2",
      where "Colossus 2" is a tranche of the same campus;
    * **a campus-wide tranche** — labelled for the whole campus and within 2% of its
      cited total — is the parent of every other top-level tranche, when they fit
      inside it. When they do not, something in them is described twice or belongs
      elsewhere, and that is a judgement left to the review.
    """
    blocks = [
        b
        for b in (getattr(project, "blocks", ()) or ())
        if not is_generation(b.label, getattr(b, "parent", None))
    ]
    keys = {b.block_key: b for b in blocks}
    links: dict[str, Link] = {}
    dormant: list[Link] = []

    stored = sorted(
        getattr(project, "nesting", ()) or (),
        key=lambda row: (row.decided_by == "model", row.block_key),
    )
    for row in stored:
        link = Link(
            row.block_key,
            row.relation,
            row.target_key,
            by=row.decided_by or "operator",
            reason=row.reason or "",
            confidence=row.confidence,
        )
        missing = row.block_key not in keys or (row.target_key and row.target_key not in keys)
        if missing or closes_cycle(links, row.block_key, row.target_key):
            dormant.append(link)
            continue
        links[row.block_key] = link

    if automatic and blocks:
        _auto_names(project, blocks, links)
        _auto_parents(blocks, keys, links)
        _auto_campus(project, blocks, links)
    return Structure(links, tuple(dormant))


def _auto_names(project: Any, blocks: list[Any], links: dict[str, Link]) -> None:
    from tracker.blockcheck import _canonical, group_blocks

    # `group_blocks` tells members apart by row id, so rows not yet flushed — no id,
    # or ids that collide — would be grouped as one. Nothing is inferred from them.
    ids = [getattr(b, "id", None) for b in blocks]
    if None in ids or len(set(ids)) != len(ids):
        return
    by_key = {b.block_key: b for b in blocks}
    stored = {k for k, link in links.items() if not link.automatic}
    for group in group_blocks(getattr(project, "id", 0), blocks):
        if group.verdict != "mergeable":
            continue
        # A label naming a range or a list — "DC4-DC6", "Buildings 1-7" — holds
        # several of the others; it is never the same as one of them. Two
        # designators of different kinds are one thing named twice: "Building 2
        # (ELN-02)".
        members = [
            by_key[m.block_key] for m in group.members if m.block_key in by_key and not _spans(m)
        ]
        # The same name under two different stated parents is two things: "Phase I"
        # of ACC9 and "Phase I" of ACC6 are the first phases of two buildings, and
        # `blockcheck` groups by the label alone.
        parents = {_fold_label(m.parent) for m in members if getattr(m, "parent", None)}
        if len(parents) > 1:
            continue
        # A tranche an earlier group already placed by rule stays where it was put.
        members = [m for m in members if m.block_key not in links or m.block_key in stored]
        if len(members) < 2 or all(m.block_key in links for m in members):
            continue
        head = _canonical(group.members).block_key
        ruled = [m for m in members if m.block_key in stored]

        def tier(block: Any, head: str = head) -> tuple[int, int, str]:
            figure = 0 if block.mw and mw_is_confirmed(block) else 1 if block.mw else 2
            return (figure, 0 if block.block_key == head else 1, block.block_key)

        target = sorted(ruled or members, key=tier)[0]
        for member in members:
            if member is target or member.block_key in links:
                continue
            if closes_cycle(links, member.block_key, target.block_key):
                continue
            links[member.block_key] = Link(
                member.block_key,
                "same_as",
                target.block_key,
                by=AUTO_NAMES,
                reason=f"another name for {target.label}",
            )


def _spans(member: Any) -> bool:
    """Whether a label names several of one kind: two ordinals of one class, or a bare
    count beside one ("Buildings 1-7", "Phase 1 (4 of 8 buildings)")."""
    from tracker.blockcheck import UNKNOWN_CLASS, families

    found = families(member)
    classes = [klass for klass, _ in found]
    return len(classes) != len(set(classes)) or (UNKNOWN_CLASS in classes and len(found) > 1)


def _auto_parents(blocks: list[Any], keys: dict[str, Any], links: dict[str, Link]) -> None:
    by_label = {}
    for block in blocks:
        by_label.setdefault(_fold_label(block.label), block)
    for block in sorted(blocks, key=lambda b: b.block_key):
        stated = getattr(block, "parent", None)
        if not stated or block.block_key in links or adds_to(block):
            continue
        target = keys.get(block_key(stated).value) or by_label.get(_fold_label(stated))
        if target is None or target is block:
            continue
        if closes_cycle(links, block.block_key, target.block_key):
            continue
        links[block.block_key] = Link(
            block.block_key,
            "part_of",
            target.block_key,
            by=AUTO_PARENT,
            reason=f"its source names {target.label} as what it is part of",
        )


def _auto_campus(project: Any, blocks: list[Any], links: dict[str, Link]) -> None:
    total = getattr(project, "mw_planned", None)
    if not total:
        return
    tolerance = max(1.0, total * 0.02)
    nested = Structure(links)
    tree = _sum_tree(blocks, nested)
    usable = {b.block_key for b in placeable(blocks, nested=nested)}
    top = [tree.nodes[k] for k in sorted(tree.nodes) if tree.parent.get(k) is None and k in usable]
    candidates = [
        b
        for b in top
        if b.block_key not in links
        and b.mw
        and b.status not in BLOCK_TERMINAL
        and abs(b.mw - total) <= tolerance
        and campus_like(b)
    ]
    if not candidates:
        return
    root = sorted(candidates, key=lambda b: (not mw_is_confirmed(b), b.block_key))[0]
    for twin in candidates:
        if twin is not root:
            links[twin.block_key] = Link(
                twin.block_key,
                "same_as",
                root.block_key,
                by=AUTO_CAMPUS,
                reason=f"both describe the whole {total:g} MW campus",
            )
    # An expansion or "another 380 MW" may be beyond the cited total rather than
    # inside it, so it is left beside the campus for the review to place.
    others = [b for b in top if b not in candidates and b.block_key not in links and not adds_to(b)]
    inside = sum(tree.eff[c].total for c in tree.children.get(root.block_key, ()))
    if (
        not others
        or inside + sum(tree.eff[b.block_key].total for b in others) > root.mw + tolerance
    ):
        return
    for other in others:
        links[other.block_key] = Link(
            other.block_key,
            "part_of",
            root.block_key,
            by=AUTO_CAMPUS,
            reason=f"fits inside {root.label}, the whole campus",
        )


@dataclass
class _Figures:
    confirmed: float = 0.0
    unconfirmed: float = 0.0
    live: float = 0.0

    @property
    def total(self) -> float:
        return self.confirmed + self.unconfirmed

    def add(self, other: _Figures) -> None:
        self.confirmed += other.confirmed
        self.unconfirmed += other.unconfirmed
        self.live += other.live


@dataclass
class _Tree:
    """One campus's tranches summed as the tree they form."""

    nodes: dict[str, Any]
    parent: dict[str, str | None]
    children: dict[str, list[str]]
    #: What each tranche counts: its parts, or its own figure where that is larger.
    eff: dict[str, _Figures]
    #: The part of each tranche's own figure its parts do not cover — what it adds.
    remainder: dict[str, _Figures]
    #: Tranches ruled the same as one in the tree, by the tranche they are.
    twins: dict[str, list[Any]]
    #: Whose figure each tranche counts: its own row, or one of its other names'.
    figure_from: dict[str, Any]
    #: Each tranche's own quoted figure (0 when unquoted), before its parts are summed.
    own_confirmed: dict[str, float]
    #: Each tranche's state, the furthest any of its names reports.
    status: dict[str, str]
    placed: _Figures
    unplaced: _Figures
    roots: tuple[str, ...]
    placed_roots: frozenset[str]
    #: Every tranche in a tree whose root is placed.
    in_placed: frozenset[str]
    unconfirmed_labels: tuple[str, ...]
    unplaced_labels: tuple[str, ...]
    set_aside: dict[str, tuple[float, tuple[str, ...]]]


def _sum_tree(blocks: list[Any], nested: Structure, *, skip: set[str] = frozenset()) -> _Tree:
    """Sum non-generation tranches as the tree `nested` makes of them.

    `skip` are tranches left out entirely (a figure too large to be the campus's); a
    tranche whose parent is skipped or set aside counts at the top level.
    """
    present = [b for b in blocks if b.block_key not in skip]
    aside: dict[str, list[Any]] = {reason: [] for reason in SET_ASIDE}
    nodes: dict[str, Any] = {}

    def gone_with(key: str) -> str | None:
        # A part goes where what holds it goes: Abilene's "second Abilene campus" is
        # part of the Microsoft expansion ruled another campus, so it is that
        # campus's too, not a top-level part of Abilene. Followed up the `part_of`
        # chain to the first tranche ruled superseded or elsewhere.
        seen: set[str] = set()
        above = nested.parent(key)
        while above is not None and above not in seen:
            seen.add(above)
            link = nested.link(above)
            if link is not None and link.relation in ("superseded", "elsewhere"):
                return link.relation
            above = nested.parent(above)
        return None

    for block in present:
        link = nested.link(block.block_key)
        if link is not None and link.relation in SET_ASIDE:
            aside[link.relation].append(block)
        elif (inherited := gone_with(block.block_key)) is not None:
            aside[inherited].append(block)
        else:
            nodes[block.block_key] = block

    twins: dict[str, list[Any]] = {}
    for block in aside["same_as"]:
        canonical = nested.canonical(block.block_key)
        if canonical in nodes:
            twins.setdefault(canonical, []).append(block)

    usable = {b.block_key for b in placeable(blocks, nested=nested)}
    parent: dict[str, str | None] = {}
    children: dict[str, list[str]] = {}
    for key in sorted(nodes):
        above = nested.parent(key)
        parent[key] = above if above in nodes and above != key else None
        if parent[key] is not None:
            children.setdefault(parent[key], []).append(key)

    figure_from: dict[str, Any] = {}

    def own(key: str) -> tuple[float, float]:
        # A tranche's own figure, or — when it states none, or only an unquoted one —
        # the best its other names state. `figure_from` records whose it was: the
        # buyer a statement names travels with that statement's figure and no other.
        block = nodes[key]
        figure_from[key] = block
        if block.status in BLOCK_TERMINAL:
            return 0.0, 0.0
        if block.mw and mw_is_confirmed(block):
            return float(block.mw), 0.0
        group = [t for t in twins.get(key, ()) if t.mw and t.status not in BLOCK_TERMINAL]
        confirmed = [t for t in group if mw_is_confirmed(t)]
        if confirmed:
            figure_from[key] = max(confirmed, key=lambda t: (t.mw, t.block_key))
            return float(figure_from[key].mw), 0.0
        if block.mw:
            return 0.0, float(block.mw)
        if group:
            figure_from[key] = max(group, key=lambda t: (t.mw, t.block_key))
            return 0.0, float(figure_from[key].mw)
        return 0.0, 0.0

    def state(key: str) -> str:
        if nodes[key].status in BLOCK_TERMINAL:
            return nodes[key].status
        return furthest_status(
            [nodes[key].status]
            + [t.status for t in twins.get(key, ()) if t.status not in BLOCK_TERMINAL]
        )

    def live(key: str) -> bool:
        return state(key) in BLOCK_LIVE

    eff: dict[str, _Figures] = {}
    remainder: dict[str, _Figures] = {}

    def total_of(key: str, depth: int = 0) -> _Figures:
        if key in eff:
            return eff[key]
        below = _Figures()
        if depth < 64:
            for child in children.get(key, ()):
                below.add(total_of(child, depth + 1))
        mine_confirmed, mine_unconfirmed = own(key)
        extra = (mine_confirmed + mine_unconfirmed) - below.total
        rest = _Figures()
        if extra > 1e-9:
            if mine_confirmed:
                rest.confirmed = extra
                rest.live = extra if live(key) else 0.0
            else:
                rest.unconfirmed = extra
        got = _Figures(below.confirmed, below.unconfirmed, below.live)
        got.add(rest)
        eff[key], remainder[key] = got, rest
        return got

    roots = tuple(k for k in sorted(nodes) if parent[k] is None)
    placed, unplaced = _Figures(), _Figures()
    placed_roots = frozenset(k for k in roots if k in usable)
    for root in roots:
        (placed if root in placed_roots else unplaced).add(total_of(root))

    def tree_of(root: str) -> list[str]:
        out, stack = [], [root]
        while stack:
            key = stack.pop()
            out.append(key)
            stack.extend(children.get(key, ()))
        return out

    unconfirmed_labels: set[str] = set()
    unplaced_labels: set[str] = set()
    in_placed: set[str] = set()
    for root in roots:
        for key in tree_of(root):
            if root in placed_roots:
                in_placed.add(key)
            rest = remainder.get(key) or _Figures()
            if root in placed_roots and rest.unconfirmed > 0:
                unconfirmed_labels.add(nodes[key].label)
            if root not in placed_roots and rest.total > 0:
                unplaced_labels.add(nodes[key].label)

    set_aside = {}
    for reason in SET_ASIDE:
        group = [b for b in aside[reason] if b.mw and b.status not in BLOCK_TERMINAL]
        set_aside[reason] = (float(sum(b.mw for b in group)), _labels(group))

    return _Tree(
        nodes=nodes,
        parent=parent,
        children=children,
        eff=eff,
        remainder=remainder,
        twins=twins,
        figure_from=figure_from,
        own_confirmed={k: own(k)[0] for k in nodes},
        status={k: state(k) for k in nodes},
        placed=placed,
        unplaced=unplaced,
        roots=roots,
        placed_roots=placed_roots,
        in_placed=frozenset(in_placed),
        unconfirmed_labels=tuple(sorted(unconfirmed_labels)),
        unplaced_labels=tuple(sorted(unplaced_labels)),
        set_aside=set_aside,
    )


@dataclass(frozen=True)
class TreeShare:
    """One customer's confirmed slice of a campus's tranche tree."""

    customer: str | None
    mw_planned: float
    mw_built: float
    online: Any


def tree_shares(tree: _Tree) -> list[TreeShare]:
    """Confirmed capacity by named buyer, read up the tree. Unnamed capacity is left out.

    A tranche naming a buyer holds its own quoted figure, less what the tranches inside
    it credit to buyers of their own; parts inside it that name nobody are its buyer's,
    up to that figure and no further. So a campus-wide lease and the buildings inside
    it are credited once between them — Abilene's 1,200 MW for OpenAI with a 200 MW
    building for Oracle is 1,000 and 200 — and a buyer named on a 210 MW "Colossus 2"
    does not take the 1,531 MW building later reported inside it.
    """
    out: dict[str, dict[str, Any]] = {}

    def customer_of(key: str) -> str | None:
        # Its own buyer, else the buyer of another name for it that states the same
        # figure, or none. Never a buyer stated with a different figure: Fermi's 222
        # MW lease naming AMD is another name for its 1 GW phase, and AMD did not
        # take the gigawatt.
        block = tree.nodes[key]
        if block.customer:
            return block.customer
        counted = tree.own_confirmed.get(key) or float(getattr(block, "mw", None) or 0.0)
        for twin in [tree.figure_from.get(key), *tree.twins.get(key, ())]:
            if twin is None or twin is block or not twin.customer:
                continue
            if twin.mw is None or abs(float(twin.mw) - counted) <= max(1.0, counted * 0.01):
                return twin.customer
        return None

    def walk(key: str, depth: int = 0) -> tuple[float, float]:
        named, named_live = 0.0, 0.0
        if depth < 64:
            for child in tree.children.get(key, ()):
                got, got_live = walk(child, depth + 1)
                named += got
                named_live += got_live
        buyer = customer_of(key)
        whole = tree.eff[key]
        own = tree.own_confirmed.get(key, 0.0)
        if not buyer or own <= 0:
            return named, named_live
        take = max(0.0, min(own, whole.confirmed) - named)
        take_live = max(0.0, min(take, whole.live - named_live))
        if take > 0:
            entry = out.setdefault(buyer, {"planned": 0.0, "built": 0.0, "online": None})
            entry["planned"] += take
            entry["built"] += take_live
            block = tree.nodes[key]
            when = getattr(block, "energized_on", None) or getattr(block, "expected_online", None)
            if when and (entry["online"] is None or when < entry["online"]):
                entry["online"] = when
        return named + take, named_live + take_live

    for root in tree.roots:
        if root in tree.placed_roots:
            walk(root)
    return [
        TreeShare(name, entry["planned"], entry["built"], entry["online"])
        for name, entry in out.items()
    ]


def tranche_tree(project: Any, nested: Structure | None = None) -> _Tree:
    """A campus's non-generation tranches summed as their tree. For Capex and the page."""
    blocks = [
        b
        for b in (getattr(project, "blocks", ()) or ())
        if not is_generation(b.label, getattr(b, "parent", None))
    ]
    return _sum_tree(blocks, nested if nested is not None else structure(project))


def rollup(blocks: list[Any], nested: Structure | None = None) -> Rollup:
    """Derive project-level values from a project's blocks. Never writes.

    Summed as the tree `nested` makes of them (see `structure`); without one, every
    tranche is its own top-level part, which is how this read before the hierarchy.
    """
    if not blocks:
        return Rollup(None, None, None, None)
    nested = nested if nested is not None else EMPTY_STRUCTURE

    usable = placeable(blocks, nested=nested)
    skipped = tuple(b.block_key for b in blocks if b not in usable)

    # Generation comes out before anything is summed. A gas plant built to serve
    # the campus is real, cited, and placeable — it fails none of the other floor
    # rules — and it is still not the data center's capacity.
    generation = tuple(
        b.block_key for b in usable if is_generation(b.label, getattr(b, "parent", None))
    )
    plant = set(generation)
    tree = _sum_tree([b for b in blocks if b.block_key not in plant], nested)
    usable = [b for b in usable if b.block_key not in plant and b.block_key in tree.nodes]

    uncited = tuple(b.block_key for b in usable if not mw_is_confirmed(b) and b.mw)
    contributing = [
        k for k in tree.in_placed if tree.remainder.get(k) and tree.remainder[k].confirmed > 0
    ]
    built_from = [k for k in contributing if tree.remainder[k].live > 0]

    # Which buyer the tranches name, largest first, for `reconcile`'s fill and the
    # disclosure. Every counted tranche that names one, as before the tree — a buyer
    # named on a tranche with no figure still names the buyer — less those set aside.
    by_customer: dict[str, float] = {}
    for block in usable:
        if block.customer and mw_is_confirmed(block):
            by_customer[block.customer] = by_customer.get(block.customer, 0.0) + (block.mw or 0.0)
    ranked = tuple(sorted(by_customer.items(), key=lambda kv: (-kv[1], kv[0])))

    # Generation excluded here too: a utility energising its own gas plant is not
    # the data center coming online, and letting it set the campus phase is one of
    # the ways Hyperion came to read `operational` while nothing was built. So is a
    # tranche ruled superseded or another campus's: neither says where this one is.
    gone = {k for k, link in nested.links.items() if link.relation in ("superseded", "elsewhere")}
    statuses = [b.status for b in blocks if b.block_key not in plant | gone]
    phase = None
    if statuses:
        # Terminal only when *every* block is terminal: a cancelled phase 3 must
        # not flip a live campus to cancelled, which today it can.
        running = [s for s in statuses if s not in BLOCK_TERMINAL]
        if running:
            # Something is still going, so the campus is. A cancelled Phase 3 must
            # not flip a live campus to cancelled, which today it can.
            phase = BLOCK_STATUS_TO_PHASE[furthest_status(running)]
        else:
            # Every tranche stopped. `furthest_status` returns the terminal state
            # rather than indexing the list, so which one wins does not depend on
            # the order the blocks happen to be in.
            phase = BLOCK_STATUS_TO_PHASE[furthest_status(statuses)]

    return Rollup(
        mw_planned=tree.placed.confirmed or None,
        mw_built=tree.placed.live or None,
        phase=phase,
        customer=ranked[0][0] if ranked else None,
        unplaceable=skipped,
        uncited=uncited,
        customers=ranked,
        generation=generation,
        planned_from=tuple(sorted(contributing)),
        built_from=tuple(sorted(built_from)),
    )


# --- rebuilding the block rows from the sources ------------------------------


def _policy(name: str):
    """The `upsert.Policy` member for a block field."""
    from tracker.upsert import Policy

    return {
        "prefer_weight": Policy.PREFER_WEIGHT,
        "fill_only": Policy.FILL_ONLY,
        "max": Policy.MAX,
        "min": Policy.MIN,
        "ladder": Policy.PHASE,
    }[BLOCK_POLICY[name]]


def aliases_for(session: Any, project_id: int) -> dict[str, str]:
    """Operator-recorded key equivalences, resolved transitively.

    Cycle-guarded: two aliases pointing at each other would otherwise hang the
    ingest path, and a bad pair is entered by hand.
    """
    from sqlalchemy import select

    from tracker.models import BlockAlias

    direct = {
        row.from_key: row.to_key
        for row in session.scalars(
            select(BlockAlias).where(BlockAlias.project_id == project_id)
        ).all()
    }
    resolved: dict[str, str] = {}
    for start in direct:
        seen = {start}
        target = direct[start]
        while target in direct and target not in seen:
            seen.add(target)
            target = direct[target]
        resolved[start] = target
    return resolved


#: The key in `source.unconfirmed_reasons` under which an operator rules a
#: citation's tranches out of a row (`tracker logic rule-out ID blocks`). Not a
#: claim field, so it never enters `unconfirmed_fields`; `upsert` carries it across a
#: re-read of the same article, as it carries any decision.
RULED_OUT: Final = "blocks"


def ruled_out(source: Any) -> bool:
    """Whether an operator has ruled this citation's tranches out of its row."""
    import json

    from tracker.upsert import DECIDED_REASONS

    try:
        reasons = json.loads(getattr(source, "unconfirmed_reasons", None) or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(reasons, dict) and reasons.get(RULED_OUT) in DECIDED_REASONS


def rule_out(source: Any, *, reason: str) -> bool:
    """Rule this citation's tranches out of its row. Idempotent; True if it changed.

    For an article about another site that names its buildings — NTT's 2025 land
    purchase for a 216 MW second Hillsboro campus, read onto the existing campus
    (#29) as two planned tranches. The article stays a citation; its tranches stop
    describing this row. `reason` is one of `upsert.DECIDED_REASONS`.
    """
    import json

    try:
        reasons = json.loads(source.unconfirmed_reasons or "{}")
    except (TypeError, ValueError):
        reasons = {}
    if not isinstance(reasons, dict):
        reasons = {}
    if reasons.get(RULED_OUT) == reason:
        return False
    reasons[RULED_OUT] = reason
    source.unconfirmed_reasons = json.dumps(reasons, sort_keys=True, ensure_ascii=False)
    return True


def blocks_by_key(sources: list[Any], aliases: dict[str, str] | None = None) -> dict[str, dict]:
    """Every source's assertions about every block, grouped by identity.

    The block-level sibling of `upsert.claims_by_field`, producing the same
    `_Claim` shape so one `resolve` engine settles both.
    """
    import json

    from tracker import confidence as conf
    from tracker.upsert import _Claim, is_placeholder, resolve

    aliases = aliases or {}
    grouped: dict[str, dict[str, list]] = {}
    labels: dict[str, tuple[str, str | None, bool]] = {}

    # Same demotion as `upsert.claims_by_field`: a placeholder's seed-file
    # `source_type` is `company_filing` on a URL that does not exist, so left alone
    # it would win every block field on weight. The sort is demoted too, not just
    # the `_Claim` weight below, because `label`/`parent` are not resolved by weight
    # at all — `labels.setdefault` gives them to whichever source is seen first.
    def _weight_of(source: Any) -> int:
        return 0 if is_placeholder(source) else conf.SOURCE_WEIGHTS.get(source.source_type, 1)

    # First-hand before tertiary, then by weight, for the same reason as the
    # demotion above: the first source seen supplies the label, the quotes and the
    # `source_id`, so a directory read first would be credited with a block whose
    # figures `resolve` took from a report.
    ordered = sorted(sources, key=lambda s: (conf.is_tertiary(s), -_weight_of(s), -(s.id or 0)))
    for source in ordered:
        if not source.blocks or ruled_out(source):
            continue
        placeholder = is_placeholder(source)
        try:
            entries = json.loads(source.blocks)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        weight = _weight_of(source)
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("label") or "").strip():
                continue
            key = block_key(entry["label"], entry.get("parent"))
            identity = aliases.get(key.value, key.value)
            unconfirmed = set(entry.get("unconfirmed") or ())
            labels.setdefault(identity, (entry["label"], entry.get("parent"), key.generic))
            fields = grouped.setdefault(identity, {})
            for name in BLOCK_POLICY:
                value = entry.get(name)
                if value is None or value == "":
                    continue
                fields.setdefault(name, []).append(
                    _Claim(
                        value,
                        weight,
                        source.fetched_at,
                        source.source_type,
                        source.url,
                        confirmed=not placeholder and name not in unconfirmed,
                        tertiary=conf.is_tertiary(source),
                    )
                )
            fields.setdefault("_quotes", []).append(entry.get("quotes") or {})
            fields.setdefault("_source_id", []).append(source.id)
            fields.setdefault("_unconfirmed", []).append(sorted(unconfirmed))

    out: dict[str, dict] = {}
    for identity, fields in grouped.items():
        label, parent, generic = labels[identity]
        resolved: dict[str, Any] = {
            "block_key": identity,
            "label": label,
            "parent": parent,
            "generic": generic,
        }
        for name in BLOCK_POLICY:
            claims = fields.get(name) or []
            resolved[name] = (
                resolve(
                    _policy(name),
                    claims,
                    None,
                    rank=_RANK,
                    terminal=BLOCK_TERMINAL,
                    default=DEFAULT_BLOCK_STATUS,
                )
                if claims
                else None
            )
        resolved["status"] = resolved.get("status") or DEFAULT_BLOCK_STATUS

        merged: dict[str, str] = {}
        for quotes in fields.get("_quotes", []):
            if isinstance(quotes, dict):
                for field_name, quote in quotes.items():
                    merged.setdefault(field_name, quote)
        resolved["quotes"] = merged
        resolved["unconfirmed"] = sorted(
            {name for group in fields.get("_unconfirmed", []) for name in group}
        )
        resolved["source_id"] = next(iter(fields.get("_source_id", [])), None)
        out[identity] = resolved
    return out


def rebuild(session: Any, project: Any) -> int:
    """Rebuild a project's blocks from its sources. Returns rows changed.

    Wholesale, not incremental — the same discipline `upsert` applies to fields,
    and what makes re-ingest idempotent. Blocks no source asserts any more are
    deleted: a block is a *description* of the site, so its absence means the
    description changed. That reasoning does not hold for `risk`, where absence
    from one article is no evidence the obstacle cleared, which is why risks are
    never dropped this way.
    """
    import datetime as dt
    import json

    from tracker.models import CapacityBlock

    wanted = blocks_by_key(list(project.sources), aliases_for(session, project.id))
    existing = {b.block_key: b for b in list(project.blocks)}
    changed = 0

    def as_date(value):
        if value is None or isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    def as_number(value):
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    for key, spec in wanted.items():
        row = existing.pop(key, None)
        money = spec.get("investment_usd")
        fresh = {
            "label": spec["label"],
            "parent": spec["parent"],
            "generic": bool(spec["generic"]),
            "mw": as_number(spec.get("mw")),
            "status": spec["status"],
            "customer": spec.get("customer"),
            "expected_online": as_date(spec.get("expected_online")),
            "energized_on": as_date(spec.get("energized_on")),
            "investment_usd": int(money) if as_number(money) is not None else None,
            "quotes": (
                json.dumps(spec["quotes"], ensure_ascii=False, sort_keys=True)
                if spec["quotes"]
                else None
            ),
            "unconfirmed_fields": ",".join(spec["unconfirmed"]) or None,
            "source_id": spec["source_id"],
        }
        if row is None:
            project.blocks.append(CapacityBlock(block_key=key, **fresh))
            changed += 1
            continue
        if any(getattr(row, name) != value for name, value in fresh.items()):
            for name, value in fresh.items():
                setattr(row, name, value)
            changed += 1

    for orphan in existing.values():
        project.blocks.remove(orphan)
        session.delete(orphan)
        changed += 1

    session.flush()
    return changed


def mark_mw_unconfirmed(session: Any, project: Any, keys: set[str]) -> int:
    """Mark named tranches' capacity 待确认 on every source that states it.

    The block-level counterpart to `conflicts.supersede`, and it has to live in the
    `source.blocks` JSON rather than on the source row: `mw` is not a
    `WRITABLE_FIELD`, so `unconfirmed_fields` cannot carry it — `supersede` would
    filter it out silently.

    `mw_is_confirmed` then keeps the figure visible and out of `rollup`, which is what
    "this tranche's number is wrong" actually means. The alternative — deleting the
    number — throws away something a person can check. It matters that `rollup` stops
    seeing it: `rollup` does *not* apply `account`'s out-of-scale filter, so a
    confirmed tranche larger than its own campus still feeds `reconcile`.

    **Not carried across a re-read.** `upsert_record` and `backfill blocks` overwrite
    `source.blocks` wholesale, and there is no block-level `DECIDED_REASONS`. That is
    the right default — re-reading the article is the real repair — but it means
    `audit.settled_codes` has to be able to see the mark go, which is what its
    `_BLOCK_EDIT` branch is for.

    Keyed on `block_key` through the project's aliases, never on `label`: two sources
    can spell one tranche differently, and the identity is the key.

    Marks the JSON *and* then the block row, because the two can disagree: a block
    row no source asserts any more still exists until the next `rebuild` deletes it,
    and it is the only record of that tranche in the meantime. Returns the number of
    citations whose JSON was rewritten, which is the durable part.
    """
    import json

    if not keys:
        return 0

    aliases = aliases_for(session, project.id)
    touched = 0
    for source in project.sources:
        if not source.blocks:
            continue
        try:
            entries = json.loads(source.blocks)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        changed = False
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("label") or "").strip():
                continue
            key = block_key(entry["label"], entry.get("parent"))
            if aliases.get(key.value, key.value) not in keys:
                continue
            unconfirmed = set(entry.get("unconfirmed") or ())
            if "mw" in unconfirmed:
                continue
            unconfirmed.add("mw")
            entry["unconfirmed"] = sorted(unconfirmed)
            changed = True
        if changed:
            source.blocks = json.dumps(entries, ensure_ascii=False, sort_keys=True)
            touched += 1

    if touched:
        session.flush()
        rebuild(session, project)

    # Whatever `rebuild` did not cover: a tranche no source states any more, whose
    # block row is the only place it is written down. Marking the row says the same
    # thing, and says it now — a `rebuild` that later deletes the row settles the
    # question a different way.
    for block in list(project.blocks):
        if block.block_key not in keys or mw_is_confirmed(block) is False:
            continue
        marks = {p.strip() for p in (block.unconfirmed_fields or "").split(",") if p.strip()}
        marks.add("mw")
        block.unconfirmed_fields = ",".join(sorted(marks))
    session.flush()
    return touched


def raise_phase(phase: str | None, from_blocks: str | None) -> str | None:
    """The campus phase once its tranches have had their say.

    The one rule `reconcile` applies to `phase`, as a pure function so the read path
    can ask it too. `logic.check_collisions` decides whether a row has drifted from
    its own citations by resolving the claims — and for `phase` the claims are not
    the last word, because a tranche further along raises the campus after the merge.
    Checking against the claims alone reported 48 rows as drifted on every night's
    `logic resolve --auto`, "repaired" all 48, and the recompute raised every one of
    them straight back.

    A terminal state has no rank on the ladder, so a live tranche lifts it: a
    campus with a serving building is not cancelled, whatever one article said.
    """
    from tracker.upsert import _PHASE_RANK

    if from_blocks is not None and _PHASE_RANK.get(from_blocks, -1) > _PHASE_RANK.get(phase, -1):
        return from_blocks
    return phase


def phase_after_blocks(
    phase: str | None, blocks: list[Any], nested: Structure | None = None
) -> str | None:
    """The campus phase once its *named* buildings have had their say.

    Only a block that names its building may lift the campus. A generic block —
    "Phase 1", "Building 2" — says nothing about which facility it is, and no block
    status carries a quote, so an unnamed one lifting the whole campus is the
    weakest evidence in the row overruling the quoted citations. Observed on Switch's
    The Rock (#44): a city page listing several operators' data centers put Sabey's
    finished building on Switch's campus as two unnamed "serving" phases, and the
    campus read `operational` over four quotes saying `announced`. Its megawatts
    were already left out as unplaceable; its status was not.

    One function for the write path (`reconcile`) and the read path
    (`logic.check_collisions`), so the nightly drift check expects exactly what a
    recompute stores. Both pass the campus's `structure`, so a tranche ruled
    another campus's or superseded lifts nothing.
    """
    named = [b for b in blocks if not getattr(b, "generic", False)]
    return raise_phase(phase, rollup(named, nested).phase if named else None)


def reconcile(project: Any) -> list[str]:
    """Fill the project's scalars from its blocks where they are empty. Disclosures.

    **It may fill a null, never overwrite a cited figure** — `mw_planned`, `mw_built`
    and `customer`. That is the guarantee which makes this safe over an existing
    database: a project with no blocks is untouched, so rows predating this are
    unaffected and the "9 of 12" count can only go up.

    `phase` is the one exception, on purpose. It is a ladder, not a sum: two tranches
    both `serving` cannot add up to something further along, so the double-counting
    that disqualifies a capacity sum cannot happen to a status.

    It used to *raise* a non-null `mw_planned` to the block sum as well, on the
    reasoning that "a block sum is a floor on the campus". **That reasoning holds
    only if the blocks partition the campus, and they routinely do not.** Three
    ways they break it, all three present on Hyperion (#10) at once:

    * generation and grid plant, which belong to the utility and are a different
      quantity — 5,962 MW of it (now excluded by `is_generation` upstream);
    * a whole-campus restatement stored as a sibling tranche, so the campus was
      counted once as itself and again as its own "Phase 3" (5,000 MW);
    * a milestone target repeating capacity already listed under another name
      (1,500 MW "by the end of 2027", which is Phase 1 again).

    Summed, those made `mw_planned` 14,462 MW against a figure seven sources cite
    as 5,000 — and the merge engine had already resolved 5,000 correctly from the
    claims. `reconcile` was the only thing overwriting it, and it did so *after*
    an `audit` action had cleared the same bad figure, which is how the repair came
    undone and the detector was muzzled.

    A block sum above a cited campus total is still worth knowing, so it is
    disclosed rather than acted on: something is either double-counted or the cited
    figure is stale, and neither is a question arithmetic can settle.

    `mw_built` carried the other half of that same mistake, found second. The fix
    above was applied to `mw_planned` and the next clause went on raising `mw_built`
    to the tranche sum three lines later — and a whole-campus restatement filed as a
    sibling tranche is `serving` as readily as it is `planned`, so the partition
    argument fails identically. Worse, it undid the merge: `resolve` had just lowered
    `mw_built` to what the claims support, and this raised it straight back, the same
    way it once undid an `audit` action on `mw_planned`.
    """
    if not project.blocks:
        return []

    nested = structure(project)
    got = rollup(list(project.blocks), nested)
    notes: list[str] = []

    if got.mw_planned is not None:
        if project.mw_planned is None:
            project.mw_planned = got.mw_planned
        elif got.mw_planned > project.mw_planned:
            notes.append(
                f"blocks total {got.mw_planned:g} MW, above the cited campus figure of "
                f"{project.mw_planned:g} MW; kept the cited figure — the tranches "
                "either double-count or the campus figure is stale"
            )

    if got.mw_built is not None:
        # `not project.mw_built` rather than `is None`, which keeps the old treatment
        # of a stored 0.0 as unfilled: a row carrying zero is filled from its tranches
        # instead of being told they are "above the cited built figure of 0 MW".
        if not project.mw_built:
            project.mw_built = got.mw_built
        elif got.mw_built > project.mw_built:
            notes.append(
                f"energised tranches total {got.mw_built:g} MW, above the cited built figure "
                f"of {project.mw_built:g} MW; kept the cited figure — the tranches either "
                "double-count or the campus figure is stale"
            )

    project.phase = phase_after_blocks(project.phase, list(project.blocks), nested)

    if project.customer is None and got.customer is not None:
        project.customer = got.customer

    notes.extend(reconcile_notes(got))
    return notes


def reconcile_notes(got: Rollup) -> list[str]:
    """What a reader has to be told about a rollup, given only the rollup.

    Separate from `reconcile` because `reconcile` writes and this does not, and the
    surfaces need the disclosures without the write: `tracker show` prints them under
    the block table, and printing "3 blocks are 待确认" is only half the fact — the
    other half is that their megawatts are therefore *not* in `MW planned` above.
    Every caller that shows a capacity owes the reader both.
    """
    notes: list[str] = []

    if got.generation:
        notes.append(
            f"{len(got.generation)} block(s) name generation or grid plant "
            f"({', '.join(got.generation)}); that is a utility's capacity, measured "
            "differently, and is not counted toward this campus"
        )

    if len(got.customers) > 1:
        named = ", ".join(f"{name} ({mw:g} MW)" for name, mw in got.customers)
        notes.append(f"blocks name {len(got.customers)} customers: {named}")

    if got.uncited:
        notes.append(
            f"{len(got.uncited)} block(s) carry a capacity no quote in the article "
            f"names ({', '.join(got.uncited)}); shown as 待确认 and left out of the "
            "campus total"
        )

    if got.unplaceable:
        notes.append(
            f"{len(got.unplaceable)} block(s) name a phase without saying of which "
            f"facility ({', '.join(got.unplaceable)}); their capacity is left out of "
            "the campus total until one is named"
        )

    return notes


__all__ = [
    "AUTO_CAMPUS",
    "AUTO_NAMES",
    "AUTO_PARENT",
    "BLOCK_POLICY",
    "EMPTY_STRUCTURE",
    "ITEMISATION_NOTES",
    "ITEMISED",
    "NO_ARTICLE",
    "OUTSIDE_THE_TOTAL",
    "RESIDUAL_REASONS",
    "SET_ASIDE",
    "SINGLE_BLOCK",
    "TYPE_WORDS",
    "UNREAD",
    "Accounting",
    "Key",
    "Link",
    "Residual",
    "Rollup",
    "Structure",
    "TreeShare",
    "account",
    "adds_to",
    "aliases_for",
    "block_key",
    "blocks_by_key",
    "campus_like",
    "closes_cycle",
    "furthest_status",
    "is_generation",
    "is_type_word_only",
    "itemisation",
    "label_tokens",
    "mw_is_confirmed",
    "placeable",
    "rebuild",
    "reconcile",
    "reconcile_notes",
    "rollup",
    "segment_requirements",
    "structure",
    "tranche_tree",
    "tree_shares",
]
