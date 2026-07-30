"""Project identity: deciding when two records describe the same site.

The PRD names two different dedup keys in two places — ``(company, city,
state)`` and ``(company, location, name)``. We use the former on *normalized*
values, because `name` is the least stable attribute a project has: PJM calls it
"MS Mt Pleasant", a press release calls it "Fairwater", and an article calls it
"Microsoft's Racine County campus". Name is display text; location is identity.

The hard case the PRD flags but cannot solve with string matching: ISO queues
report **County**, news reports a **municipality**. "Racine County, WI" and
"Mount Pleasant, WI" may or may not be the same project and no amount of string
comparison can tell. So granularity is part of the key — a county-level row and
a city-level row are *never* automatically merged. Instead the county row is
flagged as a possible duplicate and routed to ``tracker review``, where a human
decides. Silently merging would corrupt data; silently splitting is visible and
recoverable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Legal and structural suffixes that carry no identity. Order matters only in
#: that longer forms must be tried before their prefixes.
# fmt: off
_COMPANY_SUFFIXES = (
    "incorporated", "corporation", "holdings", "technologies", "platforms",
    "properties", "partners", "ventures", "international", "industries",
    "enterprises", "solutions", "services", "systems", "group", "company",
    "limited", "inc", "corp", "llc", "lllp", "llp", "lp", "ltd", "plc",
    "co", "sa", "nv", "ag", "gmbh", "pte", "pty",
)
# fmt: on

#: Distinct legal names for one operator. Without these, "Amazon Web Services"
#: and "Amazon" produce two projects for one site — the exact failure the PRD
#: lists as a High risk.
_COMPANY_ALIASES: dict[str, str] = {
    "amazon web services": "amazon",
    "aws": "amazon",
    "amazon data services": "amazon",
    "alphabet": "google",
    "google cloud": "google",
    "facebook": "meta",
    "meta platforms": "meta",
    "x ai": "xai",
    "x.ai": "xai",
    "microsoft": "microsoft",
    "msft": "microsoft",
    "ms": "microsoft",
    "open ai": "openai",
    "core weave": "coreweave",
    "qts realty trust": "qts",
    "qts data centers": "qts",
    "digital realty trust": "digital realty",
    "switch inc": "switch",
    "stack infrastructure": "stack",
    "vantage data centers": "vantage",
    "aligned data centers": "aligned",
    "crusoe energy systems": "crusoe",
    "applied digital corporation": "applied digital",
}

#: County-equivalent suffixes to strip, so "Racine County" and "Racine" agree.
#: Louisiana uses Parish, Alaska uses Borough and Census Area.
_COUNTY_SUFFIXES = ("county", "parish", "borough", "census area", "municipality", "city and county")


@dataclass(frozen=True)
class Locality:
    """Where a project is, and how precisely we know it."""

    kind: str  # "city" | "county"
    key: str
    display: str

    @property
    def is_precise(self) -> bool:
        return self.kind == "city"


def _slug(raw: str) -> str:
    """Lowercase, unaccented, punctuation-free, single-spaced."""
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def company_key(company: str | None) -> str:
    """Normalized company identity.

    ``"Microsoft Corporation"`` → ``"microsoft"``,
    ``"Amazon Web Services, Inc."`` → ``"amazon"``.
    """
    if not company:
        return ""
    slug = _slug(company)

    # Strip trailing legal suffixes repeatedly ("Foo Holdings LLC" -> "foo"),
    # re-checking the alias table after every strip.
    #
    # The alias check has to happen INSIDE the loop, not just before and after:
    # "Amazon Web Services, Inc." loses "inc" to leave "amazon web services",
    # which is an alias — but "services" is itself a strippable suffix, so a
    # loop that ran to completion first would reduce it to "amazon web" and miss
    # the alias entirely. Same trap for "Crusoe Energy Systems LLC".
    while True:
        if slug in _COMPANY_ALIASES:
            return _COMPANY_ALIASES[slug]
        for suffix in _COMPANY_SUFFIXES:
            if slug.endswith(" " + suffix):
                slug = slug[: -(len(suffix) + 1)].strip()
                break
        else:
            return slug


def county_key(county: str | None) -> str:
    """Normalized county identity, with the County/Parish/Borough word removed."""
    if not county:
        return ""
    slug = _slug(county)
    for suffix in _COUNTY_SUFFIXES:
        if slug.endswith(" " + suffix):
            return slug[: -(len(suffix) + 1)].strip()
    return slug


def city_key(city: str | None) -> str:
    """Normalized municipality identity.

    A "city" that is really a county (news sometimes writes "Racine County" into
    a city field) is detected and returned bare, so it can be recognized as
    county-granularity by :func:`locality`.
    """
    if not city:
        return ""
    return _slug(city)


def looks_like_county(value: str | None) -> bool:
    """True when a value names a county rather than a municipality."""
    if not value:
        return False
    slug = _slug(value)
    return any(slug.endswith(" " + suffix) for suffix in _COUNTY_SUFFIXES)


def locality(city: str | None, county: str | None) -> Locality:
    """Pick the location that identifies the project, preferring precision.

    A city field holding "Racine County" is treated as county-granularity, not
    as a municipality named "Racine County" — otherwise an ISO row whose county
    was written into `city` would silently merge with a real city row.
    """
    if city and not looks_like_county(city):
        return Locality("city", city_key(city), city)
    if city and looks_like_county(city):
        return Locality("county", county_key(city), city)
    if county:
        return Locality("county", county_key(county), county)
    return Locality("city", "", "")


def dedup_key(company: str | None, city: str | None, county: str | None, state: str | None) -> str:
    """The value stored in ``project.dedup_key`` and enforced UNIQUE.

    Format: ``"<company>|<kind>:<locality>|<STATE>"``. Because the granularity
    kind is part of the key, a county-level row and a city-level row for the
    same company and state occupy different keys and cannot collide — making
    "never auto-merge across a county/city boundary" a database invariant rather
    than something two ingest paths have to remember.
    """
    loc = locality(city, county)
    return f"{company_key(company)}|{loc.kind}:{loc.key}|{(state or '').upper()}"


def all_keys(
    company: str | None, city: str | None, county: str | None, state: str | None
) -> set[str]:
    """Every key this project could legitimately be filed under.

    A record that knows *both* its municipality and its county has two identities:
    the city-granular one it is stored under, and the county-granular one an ISO
    queue would produce for the same site. Returning both is what lets duplicate
    detection connect "Mount Pleasant, WI (Racine County)" with a queue row that
    only ever says "Racine" — the PRD's flagship duplicate case, which comparing
    locality names alone cannot catch because "mount pleasant" != "racine".

    Never used for storage or merging, only to propose a candidate for review.
    """
    keys = {dedup_key(company, city, county, state)}
    if city and county:
        keys.add(dedup_key(company, None, county, state))
    return keys


def is_cross_granularity_match(a: str, b: str) -> bool:
    """True when two dedup keys differ *only* by city-vs-county granularity.

    These are the pairs a human must adjudicate: same company, same state, and a
    locality name that matches once the County/Parish word is discounted.
    """
    if a == b:
        return False
    a_company, a_loc, a_state = a.split("|", 2)
    b_company, b_loc, b_state = b.split("|", 2)
    if (a_company, a_state) != (b_company, b_state):
        return False
    a_kind, a_name = a_loc.split(":", 1)
    b_kind, b_name = b_loc.split(":", 1)
    if a_kind == b_kind:
        return False
    return bool(a_name) and bool(b_name) and a_name == b_name


def same_company_and_state(a: str, b: str) -> bool:
    """True when two dedup keys share company and state but not locality.

    Weaker than :func:`is_cross_granularity_match` — used only to surface
    candidates in `review`, never to merge.
    """
    if a == b:
        return False
    a_company, _, a_state = a.split("|", 2)
    b_company, _, b_state = b.split("|", 2)
    return bool(a_company) and (a_company, a_state) == (b_company, b_state)


__all__ = [
    "Locality",
    "city_key",
    "company_key",
    "county_key",
    "dedup_key",
    "is_cross_granularity_match",
    "locality",
    "looks_like_county",
    "same_company_and_state",
]
