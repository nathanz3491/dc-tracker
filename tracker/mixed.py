"""Rows whose citations describe more than one campus: the mirror of `tracker duplicates`.

A row's identity is one company in one place — `dedup.dedup_key` is
`company|locality|STATE` — so where a company builds several campuses in one county
or one town, every article about any of them is filed on the same row. That row then
carries several sites' capacities, dates and phases at once, and every figure it
shows is a mixture nobody chose. On the 2026-10-26 production snapshot AWS's #158 in
Spotsylvania County, named for the Carter's Store Tech Campus, also cited articles
about the Cosner, Orrock, Crossroads and Summit Crossing campuses; NTT's Mesa rows
mixed the Elliot Road campus, open since 2022, with the land at Pecos and Crismon
roads bought in 2025 for a campus due in 2028.

`tracker duplicates` asks whether two rows are one campus. This asks whether one row
is several. It is read-only: splitting stays a person's call, made with the tools
that already exist — `tracker logic rule-out` takes a citation's claims (or, with
`blocks`, its tranches) off the row, `tracker tranches rule … elsewhere` moves one
tranche out, `tracker rename` gives the row the name of the campus it keeps, and
`tracker unalias` stops a folded key routing another campus's articles here. The
pipeline is drawn in `docs/workflows/duplicates.md`.

**What it reads.** Every citation keeps the name it gave the site (`claims.name`)
and the sentences behind its claims (`quotes`). What identifies a site is what is
left of that name once everything that does not is taken out — industry words,
places, the company's own name and its brands, other companies' names, building
codes, ordinals — plus the roads the citation's location sentences put it on.
Citations whose sites share a word, a building code, or a sentence naming the other
are one site. A row is listed when two of its sites never meet that way, at least
one of them is a *name* rather than only a road, and each is named by two
publishers or by an address.

**What it does not read, and why.** Coordinates: every stored point is the town's
Census centre, one per row, so two campuses in one town have the same point.
Capacity: two buildings of one campus are routinely quoted at different sizes, and
two campuses can share a round figure — Prime's two Santa Clara buildings are 9 MW
each — so a figure neither joins sites nor separates them. Towns named inside a
county row: on the snapshot 15 county rows had citations naming two towns, and 13
were one campus seen from its town and from the next one.

**A ruling a person made stands.** A claim `tracker logic rule-out` decided against
is not evidence here, and a citation none of whose claims still stands — every
figure, date and phase ruled out, its tranches too — no longer mixes anything and is
left out. So a row leaves this report the way it is repaired, and a name a person
has ruled a misreading stops raising it.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker.confidence import registrable_domain
from tracker.dedup import _GENERIC_NAME_TOKENS, _slug, company_aliases, company_key
from tracker.dupresolve import km_apart
from tracker.models import Project
from tracker.normalize import state_name

#: Words that say what kind of thing a site is, how far along, how big or for whom —
#: never which one. `dedup._GENERIC_NAME_TOKENS` is the base; these are the words
#: that list did not need because no two *rows* were ever paired on them, measured
#: as the false sites they raised on the 2026-10-26 snapshot: "BorderPlex Digital
#: Infrastructure Campus" against Project Jupiter, "Cologix Johnstown AI-Ready
#: Campus" against COL8, a "High Performance Computing Center" against a code.
# fmt: off
_NOT_A_SITE: frozenset[str] = frozenset({
    *_GENERIC_NAME_TOKENS,
    # what kind of place
    "hub", "hubs", "district", "corridor", "zone", "complex", "complexes", "cluster", "clusters",
    "development", "developments", "megasite", "megacampus", "megaproject", "mega", "portfolio",
    "platform", "industrial", "business", "commerce", "logistics", "industry", "properties",
    "estate", "tract", "parcel", "parcels", "lot", "lots", "land", "lands", "property",
    "annex", "addition", "extension", "hall", "halls", "tower", "towers", "office", "offices",
    "headquarters", "hq", "mall", "hotel", "carrier", "exchange", "network", "telecom",
    "area", "areas", "location", "locations", "initiative", "program", "programme",
    # what it is for, how it is built and powered
    "hyperscale", "hyperscaler", "hyperscalers", "colocation", "colo", "enterprise", "edge",
    "access", "aggregation", "compute", "computing", "hpc", "gpu", "gpus", "inference",
    "training", "factory", "factories", "gigafactory", "superfactory", "supercomputer",
    "supercomputers", "supercluster", "high", "performance", "advanced", "intelligence",
    "artificial", "innovation", "infrastructure", "micro", "modular", "ready", "flagship",
    "energy", "power", "powered", "plant", "generating", "generation", "station", "solar",
    "gas", "nuclear", "wind", "battery", "substation", "cogeneration", "rooftop", "shell",
    "build", "suit", "fit", "out", "fitout",
    # how far along
    "proposal", "proposed", "planned", "plan", "plans", "future", "existing", "former",
    "conversion", "converted", "redevelopment", "reuse", "adaptive", "rezoning", "permit",
    "permits", "application", "approved", "operational", "shuttered", "next", "new",
    # who owns it, in words that are never a site
    "joint", "venture", "ventures", "jv", "partnership", "investment", "investments",
    "llc", "inc", "corp", "co", "ltd", "lp", "holdings", "group", "partners", "realty",
    "trust", "reit", "capital", "fund",
    # how big, and which one in a sequence
    "mw", "gw", "kw", "megawatt", "megawatts", "gigawatt", "gigawatts", "acre", "acres",
    "square", "feet", "sq", "ft", "million", "billion", "story", "storey", "stories",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
    "tenth", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "vi", "vii", "viii", "ix", "x",
    # where, relative to something else
    "northern", "southern", "eastern", "western", "northeast", "northwest", "southeast",
    "southwest", "central", "upper", "lower", "greater", "metro", "metropolitan",
    "downtown", "uptown", "midtown", "near", "for", "in", "on", "by", "with", "from", "to",
    "a", "an",
    # kinds of place and of road, which name no particular one
    "county", "counties", "parish", "parishes", "city", "town", "township", "twp", "village",
    "borough", "road", "roads", "rd", "street", "streets", "st", "avenue", "ave",
    "boulevard", "blvd", "drive", "dr", "parkway", "pkwy", "highway", "hwy", "lane", "ln",
    "way", "circle", "cir", "pike", "trail", "court", "ct", "place", "pl", "route",
    "turnpike", "tpke", "interstate", "loop",
    "dc", "dcs", "sites", "centres", "projects", "phases", "expansions", "parks",
})

#: Landforms. "Tuckahoe Creek" and the "Port of Muskogee" are the town, so the word
#: goes with the place beside it; and a location sentence that mentions a creek
#: ("discharging into Sedges Creek") is not naming the Northeast Creek campus.
_LANDFORMS: frozenset[str] = frozenset({
    "creek", "lake", "lakes", "river", "rivers", "valley", "hill", "hills", "spring",
    "springs", "ridge", "mountain", "mountains", "grove", "forest", "oak", "oaks", "pine",
    "pines", "rock", "falls", "point", "bay", "harbor", "harbour", "island", "prairie",
    "meadow", "meadows", "field", "fields", "crossing", "junction", "woods", "wood",
    "garden", "gardens", "heights", "glen", "hollow", "run", "branch", "fork", "bluff",
    "canyon", "mesa", "plain", "plains", "flat", "flats", "landing", "mill", "mills", "farm",
    "farms", "ranch", "estates", "square", "plaza", "corner", "corners", "cove", "shore",
    "shores", "beach", "bend", "gap", "pass", "port", "view", "vista",
})

#: A capitalised word before "Court" or "Street" that makes it an institution or a
#: newspaper, not an address: "Effingham County Superior Court", "Wall Street".
_NOT_A_ROAD: frozenset[str] = frozenset({
    "superior", "supreme", "district", "circuit", "appeals", "federal", "bankruptcy", "claims",
    "municipal", "tax", "juvenile", "probate", "family", "county", "state", "wall", "main",
    "food", "high", "the", "commissioners", "commissioner", "customer",
})
# fmt: on

#: An operator's building code: `PH1`, `DFW-02`, `IAD 327`, `SAT93`. Every colocation
#: operator numbers its buildings in a market this way, so two codes on one row are
#: two buildings, which one campus usually holds (NTT's VA1 to VA7 in Ashburn). A code
#: joins the citations that share it and never makes a site of its own.
_CODE = re.compile(r"\b([A-Z]{2,4})[ \-]?(\d{1,3})\b|\b([A-Za-z]{2,4})(\d{1,3})\b")

#: Capitals with no digit — `IQMP`, `VSTC`, `DOE`, `ATC`, `CHI IV`. An initialism of
#: some longer name the row's other citations spell out, or of an agency; matching it
#: against nothing it can be compared with only ever raised false sites.
_INITIALISM = re.compile(r"\b[A-Z]{2,5}s?\b")

#: "Project Camellia", "Project Potomac Promise". A codename is how a site is known
#: before it has a public name, so a codename beside a public name is the ordinary
#: shape of *one* campus — on the snapshot it was the whole of six false sites
#: (Helios at Gainesville Crossing, Matador, Excalibur, Morgan, Llano, Potomac Promise).
#: A codename still joins the citations that use it; it never stands as a site.
_CODENAME = re.compile(r"\bproject\b", re.IGNORECASE)

#: "(Former Sears HQ)", "near Liberty High School": a landmark the site replaced or
#: sits beside. The words after it are not the site's name.
_LANDMARK = re.compile(
    r"\b(?:former(?:ly)?|near|next to|adjacent to|beside)\b[^,;()\[\]]*", re.IGNORECASE
)

#: A capitalised word, apostrophes (straight or curly) and hyphens included.
_PROPER = r"[A-Z][A-Za-z'" + "\N{RIGHT SINGLE QUOTATION MARK}" + r"\-]+"
# fmt: off
_ROAD_TYPES: tuple[str, ...] = (
    "Road", "Rd", "Street", "St", "Avenue", "Ave", "Boulevard", "Blvd", "Drive", "Dr",
    "Parkway", "Pkwy", "Highway", "Hwy", "Lane", "Ln", "Way", "Circle", "Cir", "Pike",
    "Trail", "Court", "Ct", "Turnpike", "Tpke",
)
# fmt: on
_ROAD_TYPE = "(?:" + "|".join(_ROAD_TYPES) + ")"
_ROAD_WORDS: frozenset[str] = frozenset(t.lower() for t in _ROAD_TYPES)
_DIRECTION = r"(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West)\.?\s+"
#: "12899 Moores Chapel Rd", "NE Brookwood at Evergreen Road", "Elliot Road".
_ROAD = re.compile(
    rf"(?:\b\d{{1,6}}\s+)?(?:\b{_DIRECTION})?\b((?:{_PROPER}\s+){{0,2}}{_PROPER})\s+"
    rf"{_ROAD_TYPE}\b\.?"
)
#: "the northeast corner of Pecos and Crismon roads".
_TWO_ROADS = re.compile(
    rf"\b({_PROPER})\s+(?:and|&)\s+({_PROPER})\s+(?:roads|streets|avenues|drives|boulevards)\b"
)
#: A directory's way of placing a campus: "Mesa Campus (Pecos & Crismon)".
_CORNER = re.compile(rf"\(\s*({_PROPER})\s*(?:&|and|/)\s*({_PROPER})\s*\)")
#: A name that is an address: "5853 Rue Ferrari", "326 Charcot Avenue data center".
_ADDRESS = re.compile(
    rf"^\s*\d{{2,6}}\s+(?:{_DIRECTION})?((?:Rue\s+)?{_PROPER}(?:\s+{_PROPER}){{0,2}})"
)

#: The quotes that say where the site is. A citation's other sentences are about
#: money, power and dates, and their roads are not the site's.
_LOCATION_QUOTES: tuple[str, ...] = ("name", "city", "county", "state")

#: Claims that say which site and where, as against what it is. A citation whose
#: every other claim has been ruled out carries nothing into the row's figures.
_IDENTITY_CLAIMS: frozenset[str] = frozenset(
    {"name", "company", "city", "county", "state", "country", "lat", "lon"}
)

#: How far apart one company's rows must be for a word in both their names to be a
#: brand — Switch's SUPERNAP in Las Vegas and in Reno, 700 km — rather than one
#: campus stored twice, which `tracker duplicates` is for. Tract's "Tuckahoe" was
#: in two rows' names a few kilometres apart and is the campus, not a brand.
BRAND_KM: float = 100.0


@dataclass(frozen=True)
class Site:
    """One campus a row's citations describe, and what they say about it."""

    #: The name its citations give it most often — the words a reader recognises.
    label: str
    #: The words that identify it, after everything generic is taken out.
    words: tuple[str, ...]
    #: Roads its citations put it on, from the name or the location sentences.
    roads: tuple[str, ...]
    source_ids: tuple[int, ...]
    publishers: tuple[str, ...]
    #: What its citations claim, so a reader can see what the row is mixing.
    mw: tuple[float, ...]
    online: tuple[str, ...]
    phases: tuple[str, ...]
    #: True when the row is named after this one: the site the row has been showing.
    is_row_name: bool = False

    @property
    def says(self) -> str:
        """Its figures in a few words: "450 MW · online 2029-01 · permitting"."""
        parts: list[str] = []
        if self.mw:
            parts.append(" / ".join(f"{v:,.0f}" for v in self.mw) + " MW")
        if self.online:
            parts.append("online " + " / ".join(self.online))
        if self.phases:
            parts.append(" / ".join(self.phases))
        return " · ".join(parts)

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "words": list(self.words),
            "roads": list(self.roads),
            "source_ids": list(self.source_ids),
            "publishers": list(self.publishers),
            "mw_planned": list(self.mw),
            "expected_online": list(self.online),
            "phase": list(self.phases),
            "is_row_name": self.is_row_name,
        }


@dataclass(frozen=True)
class MixedRow:
    """A row whose citations describe two or more campuses. Plain values, no ORM."""

    project_id: int
    company: str
    name: str
    place: str
    state: str
    citations: int
    sites: tuple[Site, ...]

    @property
    def why(self) -> str:
        """One line a reader can check: which citations name which site."""
        named = "; ".join(
            f"{site.label} ({', '.join(str(i) for i in site.source_ids)})" for site in self.sites
        )
        return f"{len(self.sites)} sites named by citations that never name each other: {named}"

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "row": f"{self.company} — {self.name}",
            "place": f"{self.place}, {self.state}",
            "citations": self.citations,
            "why": self.why,
            "sites": [s.as_json() for s in self.sites],
        }


def mixed_rows(session: Session) -> list[MixedRow]:
    """Every row whose citations describe more than one campus, most sites first.

    Reads every row and every citation once; writes nothing.
    """
    projects = session.scalars(
        select(Project).options(selectinload(Project.sources), selectinload(Project.parties))
    ).all()
    lexicon = _Lexicon.build(projects)
    found: list[MixedRow] = []
    for project in projects:
        row = _mixed(project, lexicon)
        if row is not None:
            found.append(row)
    return sorted(found, key=lambda r: (-len(r.sites), r.project_id))


# --- what a word is ----------------------------------------------------------


@dataclass(frozen=True)
class _Lexicon:
    """Everything a citation's name is read against, built once from the database."""

    #: Every locality any row or citation in the state names, as words.
    places: dict[str, frozenset[str]]
    #: Per company key, the words it uses for several campuses of its own.
    brands: dict[str, frozenset[str]]
    #: Company keys of parties named at two or more places: companies in their own
    #: right, as against a project entity named for the one site it holds.
    wide: frozenset[str]
    #: Operators and end users, by every spelling the roster and the code know.
    firm_phrases: _Phrases

    @classmethod
    def build(cls, projects: Iterable[Project]) -> _Lexicon:
        projects = list(projects)
        places: dict[str, set[str]] = defaultdict(set)
        row_names: dict[tuple[str, str], list[Project]] = defaultdict(list)
        cited_states: dict[tuple[str, str], set[str]] = defaultdict(set)
        party_places: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for project in projects:
            key = company_key(project.company)
            for value in (project.city, project.county):
                places[project.state].update(_slug(value or "").split())
            for word in set(_slug(project.name or "").split()):
                row_names[(key, word)].append(project)
            for source in project.sources:
                claims = _json(source.claims, {})
                for field in ("city", "county"):
                    if isinstance(claims.get(field), str):
                        places[project.state].update(_slug(claims[field]).split())
                if isinstance(claims.get("name"), str):
                    for word in set(_slug(claims["name"]).split()):
                        cited_states[(key, word)].add(project.state)
            here = ((project.city or project.county or "").lower(), project.state)
            for party in project.parties:
                party_places[company_key(party.name)].add(here)

        # A landform is never a brand: AWS writing "Creek" in two states says nothing
        # about its Northeast Creek campus in Louisa County.
        brands: dict[str, set[str]] = defaultdict(set)
        for (key, word), states in cited_states.items():
            if len(states) >= 2 and word not in _LANDFORMS:
                brands[key].add(word)
        for (key, word), rows in row_names.items():
            if len(rows) >= 2 and word not in _LANDFORMS and _far_apart(rows):
                brands[key].add(word)

        return cls(
            places={state: frozenset(words) for state, words in places.items()},
            brands={key: frozenset(words) for key, words in brands.items()},
            wide=frozenset(k for k, where in party_places.items() if k and len(where) >= 2),
            firm_phrases=_Phrases(_firm_names()),
        )


def _far_apart(rows: list[Project]) -> bool:
    """True when two of these rows are further apart than one company's campus spans.

    Rows without coordinates cannot show it, so a word in their names stays a site's.
    """
    return any(
        (km_apart(a, b) or 0.0) > BRAND_KM for i, a in enumerate(rows) for b in rows[i + 1 :]
    )


@lru_cache(maxsize=1)
def _firm_names() -> frozenset[str]:
    """Operators and end users, as phrases: a tenant or a partner in a campus's name
    ("Anthropic-Amazon New Carlisle", "OpenAI/Oracle/Vantage Port Washington") says
    who, not where. A roster that cannot be read leaves the end users."""
    from tracker.capex import end_user_keys

    names: set[str] = set()
    try:
        from tracker.roster import load

        for operator in load():
            for spelling in operator.spellings:
                names.update({_slug(spelling), company_key(spelling)})
    except Exception:  # a broken seed file must not stop a read-only report
        pass
    for key, display in end_user_keys().items():
        names.update({key, _slug(display)})
    return frozenset(n for n in names if len(n) > 2)


def _json(raw: str | None, default: Any) -> Any:
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _initials(name: str) -> str:
    words = _slug(name).split()
    return "".join(w[0] for w in words) if len(words) >= 2 else ""


def _plural(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


@lru_cache(maxsize=200_000)
def _alike(a: str, b: str) -> bool:
    """One word or a spelling of it: "Camellia" and "Camelia", "Beaumeade" and "Beumeade"."""
    if a == b:
        return True
    shortest = min(len(a), len(b))
    if shortest < 5 or abs(len(a) - len(b)) > 2:
        return False
    edits = _edits(a, b)
    return edits <= 1 or (shortest >= 7 and edits <= 2)


def _edits(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def _one_letter_out(word: str) -> set[str]:
    """The word and every spelling of it with one letter dropped."""
    return {word} | {word[:i] + word[i + 1 :] for i in range(len(word))}


#: Every long vocabulary word with one letter dropped. Two words share an entry here
#: when one letter added, dropped or changed, or two neighbours swapped, turns one
#: into the other — the test `_misspelt_vocabulary` needs, without comparing every
#: word in every citation against every word in the list.
_VOCABULARY_SPELLINGS: frozenset[str] = frozenset(
    s for w in _NOT_A_SITE if len(w) >= 6 for s in _one_letter_out(w)
)


@lru_cache(maxsize=50_000)
def _misspelt_vocabulary(word: str) -> bool:
    """ "Techology Park" is a technology park."""
    return len(word) >= 6 and not _one_letter_out(word).isdisjoint(_VOCABULARY_SPELLINGS)


def _any_alike(xs: Iterable[str], ys: Iterable[str]) -> bool:
    ys = tuple(ys)
    return any(_alike(x, y) for x in xs for y in ys)


# --- one citation --------------------------------------------------------------


@dataclass(frozen=True)
class _Reading:
    """What one citation says about which site it describes."""

    source_id: int
    publisher: str
    name: str
    #: Words of the name it gives the site; a road in the name counts.
    words: frozenset[str]
    #: Words of a road or address the name itself is ("1231 Comstock Street").
    address: frozenset[str]
    #: Words of roads its location sentences put the site on.
    roads_words: frozenset[str]
    roads: frozenset[str]
    #: Every other site-like word in its location sentences, which can only join.
    mentions: frozenset[str]
    codes: frozenset[tuple[str, int]]
    codename: bool
    claims: dict[str, Any]
    #: Claims the evidence gate did not confirm: still evidence of which site a
    #: citation describes, never a figure to show for it. On the 10-26 copy ATL3's
    #: row showed "9,000 MW" and "40,000 MW", two unconfirmed misreadings no
    #: published number uses.
    unconfirmed: frozenset[str] = frozenset()

    @property
    def identity(self) -> frozenset[str]:
        return self.words | self.roads_words


class _Phrases:
    """Company names to take out of a name before its words are read.

    Indexed by first word, because the roster alone is a few hundred spellings and
    every location sentence of every citation is read against them.
    """

    def __init__(self, phrases: Iterable[str] = (), parent: _Phrases | None = None) -> None:
        self.parent = parent
        self.by_first: dict[str, list[tuple[str, ...]]] = defaultdict(list)
        for phrase in phrases:
            words = tuple(phrase.split())
            if words:
                self.by_first[words[0]].append(words)

    def starting(self, word: str) -> list[tuple[str, ...]]:
        found = list(self.by_first.get(word, ()))
        if self.parent is not None:
            found += self.parent.starting(word)
        return sorted(found, key=len, reverse=True)

    def strip(self, tokens: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(tokens):
            for phrase in self.starting(tokens[i]):
                if tuple(tokens[i : i + len(phrase)]) == phrase:
                    i += len(phrase)
                    break
            else:
                out.append(tokens[i])
                i += 1
        return out


class _Row:
    """The noise one row's names are read against."""

    def __init__(self, project: Project, lexicon: _Lexicon) -> None:
        self.lexicon = lexicon
        self.places = lexicon.places.get(project.state, frozenset())
        self.home = frozenset(_slug(f"{project.city or ''} {project.county or ''}").split())
        self.own = company_key(project.company)
        noise = set(_slug(state_name(project.state) or "").split())
        noise |= _company_words(project.company)
        noise |= lexicon.brands.get(self.own, frozenset())
        phrases: set[str] = set()
        for party in project.parties:
            self._party(party.name, noise, phrases)
        if project.customer:
            phrases |= {_slug(project.customer), company_key(project.customer)}
        self.noise = frozenset(noise)
        self.phrases = _Phrases(phrases, parent=lexicon.firm_phrases)

    def _party(self, name: str | None, noise: set[str], phrases: set[str]) -> None:
        """A party's name is noise only when it is a company in its own right.

        "Pacific Proving LLC" owned the land NTT bought at Pecos and Crismon roads, and
        taking it out of names took the site's own name with it. A project entity is
        named for the one site it holds; a company is named at several.
        """
        key = company_key(name)
        if not key or key not in self.lexicon.wide:
            return
        phrases |= {_slug(name or ""), key}
        noise |= self.lexicon.brands.get(key, frozenset())

    def read(self, source: Any) -> _Reading | None:
        """One citation's site, or None when nothing it says still stands."""
        claims = _json(source.claims, {})
        ruled = _ruled_out(source)
        live = {k: v for k, v in claims.items() if k not in ruled}
        if not _carries_anything(source, live):
            return None
        noise = set(self.noise)
        extra: set[str] = set()
        claimed = live.get("company") if isinstance(live.get("company"), str) else None
        if claimed and company_key(claimed) == self.own:
            noise |= _company_words(claimed)
        elif claimed:
            self._party(claimed, noise, extra)
        for party in _json(source.parties, []):
            if isinstance(party, dict) and isinstance(party.get("name"), str):
                self._party(party["name"], noise, extra)
        if isinstance(live.get("customer"), str):
            extra |= {_slug(live["customer"]), company_key(live["customer"])}
        phrases = _Phrases(extra, parent=self.phrases) if extra else self.phrases

        name = live.get("name") if isinstance(live.get("name"), str) else ""
        plain = _LANDMARK.sub(" ", name)
        codes = frozenset(
            ((m.group(1) or m.group(3) or "").lower(), int(m.group(2) or m.group(4)))
            for m in _CODE.finditer(name)
        )
        named_roads = _roads(plain) | _address(plain)
        address = frozenset(w for road in named_roads for w in self.words(road, noise, phrases))
        words = frozenset(self.words(plain, noise, phrases)) | address

        quotes = _json(source.quotes, {})
        sentences = dict.fromkeys(
            quotes[k] for k in _LOCATION_QUOTES if k not in ruled and isinstance(quotes.get(k), str)
        )
        roads: set[str] = set()
        mentions: set[str] = set()
        for sentence in sentences:
            roads |= _roads(sentence)
            mentions |= self.words(sentence, noise, phrases)
        roads_words = frozenset(w for road in roads for w in self.words(road, noise, phrases))

        return _Reading(
            source_id=source.id,
            publisher=registrable_domain(source.url),
            name=name,
            words=words,
            address=address,
            roads_words=roads_words - words,
            roads=frozenset(self.tidy(r) for r in roads | named_roads) - {""},
            mentions=frozenset(mentions),
            codes=codes,
            codename=bool(_CODENAME.search(name)),
            claims=live,
            unconfirmed=frozenset(
                f.strip() for f in (source.unconfirmed_fields or "").split(",") if f.strip()
            ),
        )

    def tidy(self, road: str) -> str:
        """A road as a reader writes it: "moores chapel", not "realty moores chapel rd".

        The pattern takes up to three capitalised words before "Road", and a name such
        as "Digital Realty Moores Chapel Road Charlotte Campus" hands it the company
        too. Only for showing; the words that identify the site are read separately.
        """
        words = road.split()
        while words and words[-1] in _ROAD_WORDS:
            words.pop()
        while words and (
            words[0] in _NOT_A_SITE or words[0] in self.noise or words[0] in self.home
        ):
            words.pop(0)
        return " ".join(words)

    def words(self, text: str, noise: Iterable[str], phrases: _Phrases) -> set[str]:
        """The words of `text` that could name a site here."""
        noise = noise if isinstance(noise, (set, frozenset)) else set(noise)
        text = _INITIALISM.sub(" ", _CODE.sub(" ", text))
        tokens = phrases.strip(_slug(text).split())
        out: set[str] = set()
        for i, word in enumerate(tokens):
            if (
                len(word) <= 2
                or any(ch.isdigit() for ch in word)
                or word in _NOT_A_SITE
                or word in noise
                or word in self.places
                or _misspelt_vocabulary(word)
            ):
                continue
            if _plural(word) in _LANDFORMS or word in _LANDFORMS:
                before = tokens[i - 1] if i else ""
                after = tokens[i + 2] if i + 2 < len(tokens) and tokens[i + 1] == "of" else ""
                if before in self.places or after in self.places:
                    continue
            word = _plural(word)
            if word not in _NOT_A_SITE and word not in noise:
                out.add(word)
        return out


def _company_words(company: str | None) -> set[str]:
    """Every word of a company's name, its key, its known spellings and its initials."""
    if not company:
        return set()
    key = company_key(company)
    words = set(_slug(company).split()) | set(key.split())
    for alias in company_aliases(key):
        words |= set(alias.split())
    initials = _initials(company)
    if len(initials) >= 2:
        words.add(initials)
    return words


def _ruled_out(source: Any) -> frozenset[str]:
    """Fields a person or the audit decided against on this citation, and `blocks`
    when its tranches were taken off the row (`upsert.DECIDED_REASONS`)."""
    from tracker.upsert import DECIDED_REASONS

    reasons = _json(getattr(source, "unconfirmed_reasons", None), {})
    return frozenset(k for k, v in reasons.items() if v in DECIDED_REASONS)


def _carries_anything(source: Any, live: dict[str, Any]) -> bool:
    """Whether the citation still puts a figure, a date, a phase or a tranche on the row.

    `live` has the decided-against claims taken out already, and a tranche ruling is
    recorded under `blocks.RULED_OUT`, which `_ruled_out` reads with the rest.
    """
    from tracker.blocks import RULED_OUT

    if any(k not in _IDENTITY_CLAIMS for k in live):
        return True
    return bool(_json(source.blocks, [])) and RULED_OUT not in _ruled_out(source)


def _roads(text: str) -> set[str]:
    out: set[str] = set()
    for match in _ROAD.finditer(text or ""):
        road = _slug(match.group(1))
        if road and not set(road.split()) & _NOT_A_ROAD:
            out.add(road)
    for match in _TWO_ROADS.finditer(text or ""):
        out.update({_slug(match.group(1)), _slug(match.group(2))})
    return {r for r in out if r}


def _address(name: str) -> set[str]:
    out: set[str] = set()
    for match in _CORNER.finditer(name or ""):
        out.update({_slug(match.group(1)), _slug(match.group(2))})
    match = _ADDRESS.match(name or "")
    if match:
        out.add(_slug(match.group(1)))
    return {a for a in out if a}


# --- one row -------------------------------------------------------------------


def _mixed(project: Project, lexicon: _Lexicon) -> MixedRow | None:
    row = _Row(project, lexicon)
    readings = [r for r in (row.read(s) for s in project.sources) if r is not None]
    groups = [g for g in _group(readings) if _named(g) or _placed(g)]
    apart: set[int] = set()
    for i, a in enumerate(groups):
        for j in range(i + 1, len(groups)):
            if _disagree(a, groups[j]):
                apart.update({i, j})
    if not apart:
        return None
    own = row.words(_LANDMARK.sub(" ", project.name or ""), row.noise, row.phrases)
    sites = tuple(_site(groups[i], own) for i in sorted(apart))
    return MixedRow(
        project_id=project.id,
        company=project.company,
        name=project.name,
        place=project.city or project.county or "",
        state=project.state,
        citations=len(project.sources),
        sites=tuple(sorted(sites, key=lambda s: (not s.is_row_name, -len(s.source_ids)))),
    )


def _group(readings: list[_Reading]) -> list[list[_Reading]]:
    """Citations that describe one site, joined by anything they share.

    A shared word, a shared building code, or a location sentence that names the
    other citation's site. Joining is the cautious direction — two sites joined by
    mistake hide a mixed row, two sites split by mistake list a sound one — so a
    sentence naming both is read as one site: Fermi's Matador articles say it is
    "officially named the President Donald J. Trump Advanced Energy and Intelligence
    Campus", and that sentence is what keeps the two names one campus.
    """
    parent = list(range(len(readings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(readings):
        for j in range(i + 1, len(readings)):
            b = readings[j]
            if (
                _any_alike(a.identity, b.identity)
                or a.codes & b.codes
                or _names_the_other(a, b)
                or _names_the_other(b, a)
            ):
                parent[find(i)] = find(j)
    groups: dict[int, list[_Reading]] = defaultdict(list)
    for i, reading in enumerate(readings):
        if reading.identity or reading.codes:
            groups[find(i)].append(reading)
    return list(groups.values())


def _names_the_other(a: _Reading, b: _Reading) -> bool:
    """Whether a's location sentences use a word of b's name — not a landform."""
    return _any_alike(
        (w for w in a.mentions if w not in _LANDFORMS),
        (w for w in b.words if w not in _LANDFORMS),
    )


def _named(group: list[_Reading]) -> frozenset[str]:
    """The words its citations *name* it by. A codename does not count."""
    return frozenset(w for r in group if not r.codename for w in r.words)


def _placed(group: list[_Reading]) -> frozenset[str]:
    return frozenset(w for r in group for w in r.roads_words)


def _disagree(a: list[_Reading], b: list[_Reading]) -> bool:
    """Two sites one row cannot both be.

    **One of them must be a name.** A campus is bounded by several roads, so two
    articles putting it on two different ones are often describing one parcel from
    two sides: DataBank's Red Oak campus is "along Stainback Road" in one and at
    "3080 Batchler Rd." in another. A site name is a claim about identity.

    **Each must be stated twice, or as an address.** Two publishers, so one
    article's nickname — RagingWire's "The Bolt", Aligned's "Deer Valley" — is not
    a second campus; or a single citation whose name *is* an address or a street
    corner, which is what a directory's page for a separate site looks like
    ("NTT Global Data Centers Mesa Campus (Pecos & Crismon)").
    """
    if not (_named(a) or _named(b)):
        return False
    return _stated(a) and _stated(b)


def _stated(group: list[_Reading]) -> bool:
    if len({r.publisher for r in group}) >= 2:
        return True
    return any(r.address and not r.codename for r in group)


def _label(group: list[_Reading]) -> str:
    """The name most publishers give it, earliest citation first on a tie.

    Counted once per publisher, so one directory's two pages do not outvote two
    newspapers: AWS's Cosner campus, not "Amazon AWS IAD - Massaponax Church Rd".
    """
    ordered = sorted(group, key=lambda r: r.source_id)
    for candidates in ([r for r in ordered if not r.codename], ordered):
        named = [r for r in candidates if r.name]
        if named:
            votes = Counter(name for name, _ in {(r.name, r.publisher) for r in named})
            first = {r.name: i for i, r in reversed(list(enumerate(named)))}
            return min(votes, key=lambda name: (-votes[name], first[name]))
    roads = sorted({road for r in group for road in r.roads})
    return roads[0].title() if roads else ""


def _site(group: list[_Reading], own: set[str]) -> Site:
    roads = sorted({road for r in group for road in r.roads})
    label = _label(group)
    words = sorted(_named(group) | _placed(group))

    def values(field: str) -> tuple:
        got = {
            r.claims.get(field)
            for r in group
            if r.claims.get(field) not in (None, "") and field not in r.unconfirmed
        }
        return tuple(sorted(got, key=str))

    online = tuple(str(v)[:7] for v in values("expected_online"))
    return Site(
        label=label,
        words=tuple(words),
        roads=tuple(roads),
        source_ids=tuple(sorted(r.source_id for r in group)),
        publishers=tuple(sorted({r.publisher for r in group})),
        mw=tuple(float(v) for v in values("mw_planned") if isinstance(v, (int, float))),
        online=tuple(dict.fromkeys(online)),
        phases=tuple(str(v) for v in values("phase")),
        is_row_name=bool(own) and _any_alike(own, _named(group)),
    )


__all__ = ["BRAND_KM", "MixedRow", "Site", "mixed_rows"]
