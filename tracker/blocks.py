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

**The rollup only ever raises.** `reconcile` may lift a project scalar or fill a
null; it may never lower one or blank one. A block sum is a *floor* on the campus
and a cited campus total is a different, also-valid figure, so the larger wins.
That is what makes this safe to turn on over 227 existing rows: a project with no
blocks is untouched, and the "9 of 12" count can only go up.
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
            return Key(".".join([*prefix, *segments]), generic=False)
    return Key(".".join(segments), generic=generic)


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


def placeable(blocks: list[Any], *, families: int | None = None) -> list[Any]:
    """Blocks that can safely be summed.

    A generic block with no parent, in a project that holds more than one
    designator family, might belong to either campus. Its megawatts are excluded
    rather than guessed at — the same floor discipline the rest of the project
    uses, and the alternative is silently double-counting two campuses.
    """
    if families is None:
        families = len({b.block_key.split(".")[0] for b in blocks if not b.generic})
    if families <= 1:
        return list(blocks)
    return [b for b in blocks if not b.generic or b.parent]


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


def rollup(blocks: list[Any]) -> Rollup:
    """Derive project-level values from a project's blocks. Never writes."""
    if not blocks:
        return Rollup(None, None, None, None)

    usable = placeable(blocks)
    skipped = tuple(b.block_key for b in blocks if b not in usable)
    counted = [b for b in usable if mw_is_confirmed(b)]
    uncited = tuple(b.block_key for b in usable if not mw_is_confirmed(b) and b.mw)
    live = [b for b in counted if b.status in BLOCK_LIVE]

    sized = [b.mw for b in counted if b.mw and b.status not in BLOCK_TERMINAL]
    built = [b.mw for b in live if b.mw]

    by_customer: dict[str, float] = {}
    for block in counted:
        if block.customer:
            by_customer[block.customer] = by_customer.get(block.customer, 0.0) + (block.mw or 0.0)
    ranked = tuple(sorted(by_customer.items(), key=lambda kv: (-kv[1], kv[0])))

    statuses = [b.status for b in blocks]
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
        mw_planned=sum(sized) if sized else None,
        mw_built=sum(built) if built else None,
        phase=phase,
        customer=ranked[0][0] if ranked else None,
        unplaceable=skipped,
        uncited=uncited,
        customers=ranked,
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


def blocks_by_key(sources: list[Any], aliases: dict[str, str] | None = None) -> dict[str, dict]:
    """Every source's assertions about every block, grouped by identity.

    The block-level sibling of `upsert.claims_by_field`, producing the same
    `_Claim` shape so one `resolve` engine settles both.
    """
    import json

    from tracker import confidence as conf
    from tracker.upsert import _Claim, resolve

    aliases = aliases or {}
    grouped: dict[str, dict[str, list]] = {}
    labels: dict[str, tuple[str, str | None, bool]] = {}

    ordered = sorted(
        sources, key=lambda s: (-conf.SOURCE_WEIGHTS.get(s.source_type, 1), -(s.id or 0))
    )
    for source in ordered:
        if not source.blocks:
            continue
        try:
            entries = json.loads(source.blocks)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        weight = conf.SOURCE_WEIGHTS.get(source.source_type, 1)
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
                        confirmed=name not in unconfirmed,
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


def reconcile(project: Any) -> list[str]:
    """Raise the project's scalars to what its blocks support. Returns disclosures.

    **Monotone: it may raise a value or fill a null, never lower one or blank one.**
    That is the guarantee which makes this safe to turn on over an existing
    database. A project with no blocks is untouched, so the 227 rows that predate
    this are unaffected and the "9 of 12" count can only go up. A block sum is a
    *floor* on the campus; a cited campus total is a different, also-valid figure,
    so the larger of the two wins.
    """
    if not project.blocks:
        return []

    from tracker.upsert import _PHASE_RANK

    got = rollup(list(project.blocks))
    notes: list[str] = []

    if got.mw_planned is not None and (project.mw_planned or 0) < got.mw_planned:
        was = project.mw_planned
        project.mw_planned = got.mw_planned
        if was:
            notes.append(
                f"blocks total {got.mw_planned:g} MW, above the cited campus figure "
                f"of {was:g} MW; raised mw_planned"
            )

    if got.mw_built is not None and (project.mw_built or 0) < got.mw_built:
        project.mw_built = got.mw_built

    if got.phase is not None and _PHASE_RANK.get(got.phase, -1) > _PHASE_RANK.get(
        project.phase, -1
    ):
        project.phase = got.phase

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
    "BLOCK_POLICY",
    "TYPE_WORDS",
    "Key",
    "Rollup",
    "aliases_for",
    "block_key",
    "blocks_by_key",
    "furthest_status",
    "is_type_word_only",
    "label_tokens",
    "mw_is_confirmed",
    "placeable",
    "rebuild",
    "reconcile",
    "reconcile_notes",
    "rollup",
    "segment_requirements",
]
