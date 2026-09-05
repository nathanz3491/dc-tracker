"""Who we are supposed to know about, against who we actually have.

Every other discovery path here is source-driven: poll the feeds, read what turns
up, see what accumulates. That measures publication, not coverage — an operator
nobody wrote about last month looks exactly like an operator that does not exist.
So the database could hold 300 projects and no Nebius row, and nothing in it was
capable of saying so.

This module supplies the missing side of that comparison: `seed/operators.toml`
names the operators worth having, and `measure` diffs it against the companies
actually present. The output is a list of names to go looking for, which is what
`tracker prospect` consumes.

**Nothing here writes a project.** A rostered name is a reason to search, never a
fact. Rows are still created only by the ordinary path — a fetched article, an
evidence-gated extraction — so a wrong entry in the roster costs one fruitless
round of searching and can never put a fictional campus in the database.

**Matching is deliberately loose, and says so.** A row filed as "Nebius Group
N.V." and a roster entry reading "Nebius" are the same operator, and so are
"Aligned" and "Aligned DataCenters". Both sides are normalized through
`dedup.company_key`, stripped of the words every data center company shares, and
then compared as token subsets. Where that is not enough — renames, acquisitions,
single-site LLCs — the roster carries explicit aliases. Every match is reported
with the spelling it matched, and loose matches are marked, because a matching
rule nobody can see is a rule nobody can correct.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.config import seed_path
from tracker.dedup import company_key
from tracker.models import Project

#: What a rostered company *is*. Validated, so a typo becomes an error at load
#: rather than an operator silently absent from every `--kind` report.
#:
#: Utilities and contractors are deliberately not among them. They do not own
#: campuses, so "no rows for Dominion" is not a coverage gap. They belong in
#: `seed/edgar-companies.toml`, which reads them as *sources*.
KINDS: Final[tuple[str, ...]] = ("hyperscaler", "ai_lab", "neocloud", "landlord")

#: Rows below this many projects count as `thin` rather than `covered`.
#:
#: Arbitrary, and only used for ordering: an operator running twenty campuses with
#: one row in here is as much a gap as one with none, and nothing in the data can
#: tell us how many campuses they really run. Two is the point at which a
#: second source has usually corroborated the first.
THIN_PROJECTS: Final = 2

#: Words shared by every company in this industry, which therefore identify none
#: of them. Applied *after* `company_key`, which has already removed the legal
#: suffixes ("Inc", "Ltd", "Holdings", "Group").
#:
#: Not shared with `point._NOISE`, which does the same job for project *names*:
#: that list omits the plurals because a campus is rarely called "Centers", while
#: a company very often is. Folding "Datacenters" and "Data Centers" together is
#: the entire reason "Compass Datacenters" finds a row filed as "Compass Data
#: Centers".
_GENERIC: Final[frozenset[str]] = frozenset(
    {
        "data",
        "center",
        "centers",
        "centre",
        "centres",
        "datacenter",
        "datacenters",
        "datacentre",
        "datacentres",
        "campus",
        "facility",
        "infrastructure",
        "site",
        "project",
        "the",
        "and",
        "of",
        "dc",
    }
)


class RosterError(RuntimeError):
    """The roster file is missing or malformed."""


def identity(name: str) -> frozenset[str]:
    """The words in a company name that actually identify the company.

    ``"NTT Global Data Centers Americas"`` -> ``{"ntt", "global", "americas"}``,
    ``"Aligned DataCenters"`` -> ``{"aligned"}``. Empty for a name made entirely
    of generic words, which is why every caller checks for empty before matching:
    ``"The Data Centers LLC"`` must not match everything.
    """
    return frozenset(w for w in company_key(name).split() if w and w not in _GENERIC)


@dataclass(frozen=True)
class Operator:
    """One rostered company, with every spelling we know it files under."""

    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    note: str = ""

    @property
    def spellings(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def keys(self) -> frozenset[str]:
        """Normalized forms that count as an exact match."""
        return frozenset(company_key(s) for s in self.spellings if company_key(s))

    @property
    def token_sets(self) -> tuple[frozenset[str], ...]:
        """Identity words per spelling, for the loose match. Empty sets dropped."""
        return tuple(t for t in (identity(s) for s in self.spellings) if t)

    def matches(self, company: str) -> str | None:
        """``"exact"``, ``"loose"`` or None for one stored company string.

        Exact is a normalized string equality — the roster's own spelling, or one
        of its aliases, as `company_key` renders it. Loose is a token subset: every
        identifying word of some rostered spelling appears in the stored one, which
        is what makes "Nebius" find "Nebius Group N.V." without an alias for it.

        The subset runs in one direction only. "Cipher Mining" must find "Cipher
        Mining Inc." and must NOT find "Cipher", because the shorter name could be
        anybody — and an operator folded into the wrong row is the mistake nothing
        downstream detects.
        """
        key = company_key(company)
        if key and key in self.keys:
            return "exact"
        words = identity(company)
        if words and any(tokens <= words for tokens in self.token_sets):
            return "loose"
        return None


def default_path() -> Path:
    return seed_path("operators.toml")


def load(path: Path | None = None) -> list[Operator]:
    """Read the roster. Order is the file's order, and that is load-bearing.

    `tracker prospect` works down the list, so the file itself is the priority
    queue: move an operator up and it gets the budget first.
    """
    path = path or default_path()
    if not path.is_file():
        raise RosterError(
            f"no operator roster at {path}.\n"
            "Expected a TOML file with [[operator]] entries; see seed/operators.toml "
            "in the repository for the format."
        )
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RosterError(f"{path.name} is not valid TOML: {exc}") from exc

    entries = data.get("operator") or []
    if not entries:
        raise RosterError(f"{path.name} defines no [[operator]] entries")

    operators: list[Operator] = []
    seen: dict[str, str] = {}
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name:
            raise RosterError(f"{path.name}: an [[operator]] entry has no name")
        kind = str(entry.get("kind") or "").strip()
        if kind not in KINDS:
            raise RosterError(
                f"{path.name}: {name!r} has kind {kind!r}; expected one of {', '.join(KINDS)}"
            )
        raw_aliases = entry.get("aliases") or []
        if not isinstance(raw_aliases, list):
            raise RosterError(f"{path.name}: {name!r} aliases must be a list of strings")
        aliases = tuple(str(a).strip() for a in raw_aliases if str(a).strip())
        key = company_key(name)
        # A duplicate is not harmless: both entries would be prospected, so the
        # same operator would consume the budget twice and appear twice in a report
        # that is meant to be a checklist.
        if key in seen:
            raise RosterError(
                f"{path.name}: {name!r} and {seen[key]!r} normalize to the same operator "
                f"({key!r}). Keep one and make the other an alias."
            )
        seen[key] = name
        operators.append(
            Operator(name=name, kind=kind, aliases=aliases, note=str(entry.get("note") or ""))
        )
    return operators


@dataclass(frozen=True)
class Presence:
    """What the database holds for one rostered operator."""

    operator: Operator
    projects: int
    with_capacity: int
    mw_planned: float
    states: tuple[str, ...]
    #: Stored company strings that matched, worst-case first: exact before loose.
    matched: tuple[tuple[str, int, str], ...] = ()

    @property
    def name(self) -> str:
        return self.operator.name

    @property
    def kind(self) -> str:
        return self.operator.kind

    @property
    def status(self) -> str:
        """``absent``, ``thin`` or ``covered``.

        `thin` means we have the operator but barely: one row, or rows with no
        capacity figure anywhere among them. Both are worth another look, and
        neither is worth as much as an operator we have nothing at all for — which
        is exactly the ordering `hunt_order` applies.
        """
        if not self.projects:
            return "absent"
        if self.projects < THIN_PROJECTS or not self.with_capacity:
            return "thin"
        return "covered"

    @property
    def loose_only(self) -> tuple[str, ...]:
        """Spellings matched by token subset alone — the ones worth eyeballing."""
        return tuple(name for name, _, how in self.matched if how == "loose")


@dataclass
class CoverageReport:
    rows: list[Presence] = field(default_factory=list)
    #: Companies with projects that no roster entry claims: ``(company, rows, mw)``.
    #:
    #: The reverse direction, and it earns its place. The roster is hand-written and
    #: the database is not, so the names in here are how the file gets *grown* —
    #: every one is either an operator to add or a spelling to alias.
    unrostered: list[tuple[str, int, float]] = field(default_factory=list)
    projects_total: int = 0

    def of_status(self, status: str) -> list[Presence]:
        return [row for row in self.rows if row.status == status]

    @property
    def absent(self) -> list[Presence]:
        return self.of_status("absent")

    @property
    def thin(self) -> list[Presence]:
        return self.of_status("thin")

    @property
    def covered(self) -> list[Presence]:
        return self.of_status("covered")

    @property
    def rostered_projects(self) -> int:
        """Projects claimed by at least one roster entry.

        Counted from the stored company strings rather than by summing `rows`,
        because a joint venture legitimately matches two operators — "TA Realty /
        EdgeConneX" is one project and both of them — and adding the per-operator
        counts would report more projects than exist.
        """
        return self.projects_total - sum(rows for _, rows, _ in self.unrostered)


def measure(session: Session, roster: list[Operator] | None = None) -> CoverageReport:
    """Diff the roster against the companies actually in the database.

    One pass over `(company, count, mw)` rather than a query per operator: the
    roster is dozens of entries long and the grouped counts are a single scan.
    """
    roster = roster if roster is not None else load()

    grouped = session.execute(
        select(
            Project.company,
            func.count(Project.id),
            func.coalesce(func.sum(Project.mw_planned), 0.0),
            func.count(Project.mw_planned),
        ).group_by(Project.company)
    ).all()
    states = session.execute(select(Project.company, Project.state).distinct()).all()
    by_company: dict[str, list[str]] = {}
    for company, state in states:
        by_company.setdefault(company, []).append(state)

    report = CoverageReport(projects_total=sum(count for _, count, _, _ in grouped))
    claimed: set[str] = set()

    for operator in roster:
        matched: list[tuple[str, int, str]] = []
        projects = capacity = 0
        megawatts = 0.0
        seen_states: set[str] = set()
        for company, count, mw, mw_rows in grouped:
            how = operator.matches(company)
            if how is None:
                continue
            claimed.add(company)
            matched.append((company, count, how))
            projects += count
            capacity += mw_rows
            megawatts += float(mw or 0.0)
            seen_states.update(by_company.get(company, ()))
        matched.sort(key=lambda item: (item[2] != "exact", -item[1], item[0]))
        report.rows.append(
            Presence(
                operator=operator,
                projects=projects,
                with_capacity=capacity,
                mw_planned=megawatts,
                states=tuple(sorted(seen_states)),
                matched=tuple(matched),
            )
        )

    report.unrostered = sorted(
        (
            (company, count, float(mw or 0.0))
            for company, count, mw, _ in grouped
            if company not in claimed
        ),
        key=lambda item: (-item[1], -item[2], item[0]),
    )
    return report


def hunt_order(report: CoverageReport, *, include_thin: bool = True) -> list[Presence]:
    """Operators worth prospecting, in the order the budget should be spent.

    Absent before thin, and within each group the roster's own order — so the file
    is the priority list and moving an entry up in it moves it up here. Nothing
    else sorts this: ranking by kind or by market size would be a second opinion
    about importance, and the roster is already that opinion, written down where an
    operator can edit it.
    """
    order = {row.name: i for i, row in enumerate(report.rows)}
    absent = sorted(report.absent, key=lambda r: order[r.name])
    if not include_thin:
        return absent
    return absent + sorted(report.thin, key=lambda r: order[r.name])


__all__ = [
    "KINDS",
    "THIN_PROJECTS",
    "CoverageReport",
    "Operator",
    "Presence",
    "RosterError",
    "default_path",
    "hunt_order",
    "identity",
    "load",
    "measure",
]
