"""Which publishers to read first, and which to stop reading.

`tracker sources` has measured publisher performance for a while and has never been
able to act on it — its last line says so: *"Nothing here changes a weight. This is
the evidence for doing so."* `tracker feeds` ends the same way, telling an operator
to go and comment a line out of a TOML file by hand. Two commands that reach a
verdict and then hand it to a text editor.

This closes that loop. The measurement writes `seed/sources.toml`, and the commands
that spend time and money read it.

**It changes what gets READ, never what a stored citation is WORTH.** Weight stays
per-`source_type` and hand-edited in `confidence.SOURCE_WEIGHTS`. An ignored
publisher's existing citations keep their values, their quotes and their weight —
nothing already recorded moves, and `test_ignoring_moves_no_stored_value` pins
that. This is a work queue policy, not a scoring policy.

**One host resolver, delegating to the one that already exists.** The codebase has
five different URL→host normalisations, and the ranking prints a different one
(`example.co.uk`, via `confidence.registrable_domain`) from the one the queue
reasons about (`news.example.co.uk`). A policy keyed on what the report prints
would silently never match what the queue sees — and silent non-matching is the
worst failure available here, because the run *looks* like it obeyed. So
:func:`decide` resolves through `registrable_domain`, which makes what
`tracker sources` prints directly pasteable into the file.

**Label boundaries, never substrings.** `search.is_useful_host` records the bug
this repeats otherwise: a substring test made ``x.com`` block every ``equinix.com``
URL. :func:`matches` uses the same ``host == d or host.endswith("." + d)`` rule, and
that regression has its own test.

**Absent means no opinion.** A domain not in the file is neither promoted nor
skipped. That is deliberately different from an explicit `keep`, and it is why the
analysis refuses to judge a publisher it has too little evidence about rather than
filling the file with 654 rows of noise.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from tracker.confidence import registrable_domain
from tracker.config import home, seed_path
from tracker.funnel import LOW_YIELD as IGNORE_BELOW

log = logging.getLogger(__name__)

#: Read this publisher before the others when a run is working to a budget.
PRIORITY: Final = "priority"
#: Do not queue, fetch or read it again. Existing citations are untouched.
IGNORE: Final = "ignore"

RANKS: Final[tuple[str, ...]] = (PRIORITY, IGNORE)


class PolicyError(ValueError):
    """The policy file is malformed. Message is operator-facing."""


def default_path() -> Path:
    """`seed/sources.toml`, beside `seed/feeds.toml`.

    `config.seed_path` rather than the CWD, for the reason `discover` uses it: the
    file ships with the code and `tracker` is routinely run from another directory.
    """
    return seed_path("sources.toml")


@dataclass(frozen=True)
class Entry:
    """One publisher's rank, and the evidence that was used to set it."""

    domain: str
    rank: str
    why: str = ""

    def as_toml(self) -> str:
        why = self.why.replace('"', "'")
        return f'[[source]]\ndomain = "{self.domain}"\nrank   = "{self.rank}"\nwhy    = "{why}"\n'


@dataclass(frozen=True)
class Policy:
    """The loaded file. Empty is a valid, meaningful state: no opinion about anything."""

    entries: tuple[Entry, ...] = ()

    @property
    def by_domain(self) -> dict[str, Entry]:
        return {e.domain: e for e in self.entries}

    def decide(self, url: str) -> str | None:
        """`priority`, `ignore`, or None for no opinion.

        The single entry point. Everything that filters or orders by policy comes
        through here, so there is one definition of "is this URL that publisher's".
        """
        host = registrable_domain(url)
        if not host:
            return None
        for entry in self.entries:
            if matches(host, entry.domain):
                return entry.rank
        return None

    def ignores(self, url: str) -> bool:
        return self.decide(url) == IGNORE

    def prioritises(self, url: str) -> bool:
        return self.decide(url) == PRIORITY

    def partition(self, urls: list[str]) -> tuple[list[str], list[str]]:
        """`(kept_in_priority_order, ignored)`.

        Stable within each band, so a caller's own ordering survives — the queue
        has already sorted by publication date and by whether a URL deepens a known
        project, and this must not undo that work.
        """
        first: list[str] = []
        rest: list[str] = []
        dropped: list[str] = []
        for url in urls:
            rank = self.decide(url)
            if rank == IGNORE:
                dropped.append(url)
            elif rank == PRIORITY:
                first.append(url)
            else:
                rest.append(url)
        return first + rest, dropped


#: The empty policy. Returned whenever the file is missing or unreadable, so every
#: caller can treat "no policy" and "a policy with no opinions" identically.
EMPTY: Final = Policy()


def matches(host: str, domain: str) -> bool:
    """Whether `host` belongs to `domain`, on label boundaries.

    `example.com` covers `example.com` and `news.example.com`, and must NOT cover
    `notexample.com`. Same rule as `search.is_useful_host`, and for the same
    reason — see that function's docstring for the bug a substring test caused.
    """
    return host == domain or host.endswith("." + domain)


@lru_cache(maxsize=4)
def load(path: Path | None = None) -> Policy:
    """Read the policy, or return `EMPTY`.

    Cached, like `crawl.operator_hosts`, because every URL filtered consults it.
    That makes it a trap in tests — a suite that writes a policy file passes alone
    and fails in company — so `write()` clears it and `conftest` clears it between
    tests.

    **Degrades rather than fails**, matching how `sync` already treats a broken
    feed config (`except DiscoverError: queue_spec = None`). A malformed policy
    file should not stop a crawl that would otherwise work — it should mean the
    run has no opinion about publishers, which is exactly where the tool was
    before this existed. The warning is logged so it is not silent.
    """
    path = path or default_path()
    if not path.is_file():
        return EMPTY
    try:
        return parse(path.read_text(encoding="utf-8"))
    except (PolicyError, OSError) as exc:
        log.warning("ignoring the source policy at %s: %s", path, exc)
        return EMPTY


def parse(text: str) -> Policy:
    """Parse the TOML. Raises `PolicyError` on anything malformed."""
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"not valid TOML: {exc}") from exc

    entries: list[Entry] = []
    seen: set[str] = set()
    for raw in data.get("source") or []:
        if not isinstance(raw, dict):
            continue
        domain = str(raw.get("domain") or "").strip().lower().removeprefix("www.")
        rank = str(raw.get("rank") or "").strip().lower()
        if not domain:
            continue
        if rank not in RANKS:
            raise PolicyError(
                f"{domain} has rank {rank!r}; expected one of {', '.join(RANKS)}. "
                "A domain with no opinion belongs out of the file entirely."
            )
        if domain in seen:
            raise PolicyError(f"{domain} appears twice; one rank per domain")
        seen.add(domain)
        entries.append(Entry(domain=domain, rank=rank, why=str(raw.get("why") or "")))
    return Policy(entries=tuple(entries))


# --- deriving a proposal from what was measured ------------------------------


@dataclass(frozen=True)
class Proposal:
    """One publisher the analysis has an opinion about, and why."""

    domain: str
    rank: str
    why: str
    #: What the file already said, when there is an entry to compare against.
    was: str | None = None

    @property
    def verb(self) -> str:
        if self.was is None:
            return "add"
        return "keep" if self.was == self.rank else "change"


@dataclass(frozen=True)
class Refusal:
    """A publisher the analysis deliberately has no opinion about.

    Reported rather than dropped, for the reason `_print_feed_verdicts` gives about
    its own classes: an operator who only sees the proposals will not know the
    other outcomes exist, and "cannot read" in particular is a finding.
    """

    domain: str
    #: `too few to judge` · `cannot read` · `still a feed` · `own newsroom` · `thin`
    why_class: str
    detail: str


@dataclass
class Analysis:
    proposals: list[Proposal] = dc_field(default_factory=list)
    refusals: list[Refusal] = dc_field(default_factory=list)
    #: Entries that were justified once and are not any more. Reported, never
    #: deleted — see `analyse`.
    stale: list[str] = dc_field(default_factory=list)

    def by_class(self) -> dict[str, list[Refusal]]:
        out: dict[str, list[Refusal]] = {}
        for r in self.refusals:
            out.setdefault(r.why_class, []).append(r)
        return out


def unread_by_publisher(rows: Any) -> dict[str, int]:
    """Re-key `discover.failure_summary` onto publisher identity.

    That function counts unread URLs per `netloc` minus `www.` — normalisation #3
    of the five in this codebase — while everything here is keyed on registrable
    domain. Passing its output in raw silently compares `news.example.com` against
    `example.com`, matches nothing, and the "cannot read" guard quietly stops
    guarding. That is the failure this module's docstring is about, and it caught
    the author of this function first time out. Converting in one named place is
    the fix; doing it inline at the call site would be the sixth normalisation.
    """
    out: dict[str, int] = {}
    pairs = rows.items() if hasattr(rows, "items") else rows
    for host, count in pairs:
        domain = registrable_domain(f"https://{host}")
        if domain:
            out[domain] = out.get(domain, 0) + int(count)
    return out


def analyse(
    survey: Any,
    existing: Policy,
    *,
    unread_hosts: dict[str, int] | None = None,
    feed_hosts: frozenset[str] = frozenset(),
    newsroom_hosts: frozenset[str] = frozenset(),
) -> Analysis:
    """Propose a rank for the publishers there is enough evidence to judge.

    **It refuses to judge nearly all of them, and that is the design.** On the live
    database 560 of 654 publishers are cited fewer than five times, where a
    per-citation ratio means nothing — a host cited once on a single-source project
    wins every field unopposed and outscores any real outlet.

    **`ignore` fires on zero, not on thin.** `funnel.LOW_YIELD` is documented there
    as reported and never proposed, and that discipline carries down: a thin
    publisher is a prompt to go and look, a publisher that has never once backed a
    stored value is a proposal. So `ignore` is `funnel`'s `retire` rule transposed
    from feed to publisher — read enough times to judge, and decisive on nothing.

    **The refusals matter more than the proposals.** In particular a publisher we
    mostly *cannot fetch* looks identical to a worthless one from the citation count
    alone, and silencing it would be the `datacenterdynamics` mistake with a file
    behind it.
    """
    from tracker.funnel import MIN_READ_TO_JUDGE

    unread = unread_by_publisher(unread_hosts or {})
    known = existing.by_domain
    out = Analysis()
    judged: set[str] = set()

    # Par is the fleet's own average decisions per citation — measured, not chosen.
    # A fixed bar was tried first and was useless: at `LOW_YIELD` (0.15) it promoted
    # 75 of the 94 judgeable publishers, and a priority list containing nearly
    # everything is not an ordering. Against par it promotes 16, and they are the
    # ones carrying the dataset. It also self-adjusts as the corpus grows, where a
    # constant would drift out of date silently.
    par = (survey.decisions / survey.sources_read) if survey.sources_read else 0.0

    for host in survey.hosts:
        domain, cited, decisive = host.host, host.cited, host.decisive
        judged.add(domain)
        measured = (
            f"{cited} citation(s), {decisive} value(s) decided, "
            f"{host.contested} against a disagreeing rival"
        )
        prior = known.get(domain)

        if cited < survey.MIN_CITED_FOR_RATIO:
            if prior is None:
                out.refusals.append(Refusal(domain, "too few to judge", f"{cited} citation(s)"))
            continue

        # Blocked, not worthless. Checked before anything else can propose `ignore`:
        # a host whose URLs mostly fail to fetch has few citations *because* of the
        # failures, and writing it into the file would make that permanent.
        blocked = unread.get(domain, 0)
        if blocked >= cited:
            out.refusals.append(
                Refusal(domain, "cannot read", f"{blocked} unread against {cited} cited")
            )
            continue

        rank: str | None = None
        if cited < MIN_READ_TO_JUDGE:
            # Enough to rank in a report, not enough to change what a run reads.
            pass
        elif decisive == 0:
            rank = IGNORE
        elif host.contested >= 1 and host.yield_per_citation >= par:
            rank = PRIORITY

        if rank == IGNORE and domain in feed_hosts:
            out.refusals.append(
                Refusal(domain, "still a feed", "retire it in seed/feeds.toml, not here")
            )
            continue
        if rank == IGNORE and domain in newsroom_hosts:
            out.refusals.append(
                Refusal(domain, "own newsroom", "primary evidence about its own operator")
            )
            continue

        # An `ignore` the evidence no longer supports, reported however the new
        # measurement lands. Never acted on: deleting a decision somebody made
        # deliberately is the one thing `--apply` must not do, so this says so and
        # leaves the entry alone.
        if prior is not None and prior.rank == IGNORE and decisive:
            out.stale.append(f"{domain} is marked ignore and now decides {decisive} value(s)")

        if rank is None:
            if prior is None and 0 < host.yield_per_citation < IGNORE_BELOW:
                out.refusals.append(
                    Refusal(domain, "thin", f"{host.yield_per_citation:.2f} per citation")
                )
            elif prior is not None:
                out.proposals.append(Proposal(domain, prior.rank, prior.why, was=prior.rank))
            continue

        if prior is not None:
            # The rank the file holds, and the operator's own sentence, both stand.
            # Re-deriving `why` would overwrite a human's note with a machine's.
            out.proposals.append(Proposal(domain, prior.rank, prior.why, was=prior.rank))
            continue
        out.proposals.append(Proposal(domain, rank, measured))

    # Entries about publishers the survey cannot see — cited too rarely, or not yet
    # at all. Carried through untouched, so applying never deletes a decision
    # somebody made deliberately.
    for entry in existing.entries:
        if entry.domain not in judged:
            out.proposals.append(Proposal(entry.domain, entry.rank, entry.why, was=entry.rank))

    out.proposals.sort(key=lambda p: (RANKS.index(p.rank), p.domain))
    out.refusals.sort(key=lambda r: (r.why_class, r.domain))
    return out


def to_policy(proposals: list[Proposal]) -> Policy:
    return Policy(entries=tuple(Entry(p.domain, p.rank, p.why) for p in proposals))


HEADER: Final = """# Which publishers to read first, and which to stop reading.
#
# Written by `tracker sources policy --apply`, and safe to edit by hand: a domain
# already here keeps its rank and its `why` on a re-run unless --refresh is passed.
#
# rank = "priority"  read it before the others when a run is working to a budget
# rank = "ignore"    do not queue, fetch or read it again
#
# A domain that is NOT listed has no opinion attached, which is different from
# being kept. Absence is the normal state: most publishers are cited too few times
# to judge.
#
# This changes what gets READ. It never changes what a stored citation is worth —
# weight stays per source_type in tracker/confidence.py, and citations already
# stored keep their values whatever is written here.
"""


def render(policy: Policy) -> str:
    """The file, in a stable order so two runs diff cleanly."""
    ordered = sorted(policy.entries, key=lambda e: (RANKS.index(e.rank), e.domain))
    return HEADER + "".join("\n" + e.as_toml() for e in ordered)


def write(policy: Policy, path: Path | None = None) -> Path:
    """Write the file atomically, with LF endings. Returns the path.

    `newline="\\n"` deliberately: this project is developed on Windows, and letting
    Python translate to CRLF would make the next diff look like a full rewrite of a
    file whose whole point is being reviewable.

    Written to a temporary file beside the target and then `os.replace`d, the same
    shape `webui/runs.py` uses — a half-written policy is one the loader would
    reject, and it would reject it on the next crawl rather than now.
    """
    import os
    import tempfile

    # Never into the package. `default_path` returns the packaged copy when this
    # installation has no override, and writing there would edit a file inside
    # site-packages that the next reinstall discards — and that every database on
    # the machine shares. An edited policy belongs to this installation.
    path = path or (home() / "seed" / "sources.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".toml")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render(policy))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    load.cache_clear()
    return path


__all__ = [
    "EMPTY",
    "IGNORE",
    "IGNORE_BELOW",
    "PRIORITY",
    "RANKS",
    "Analysis",
    "Entry",
    "Policy",
    "PolicyError",
    "Proposal",
    "Refusal",
    "analyse",
    "default_path",
    "load",
    "matches",
    "parse",
    "render",
    "to_policy",
    "unread_by_publisher",
    "write",
]
