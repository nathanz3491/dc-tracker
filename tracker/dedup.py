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
    # A rename, not a resemblance: Iris Energy became IREN in 2024 and both names
    # are still in circulation, so the Childress campus was stored twice. Caught
    # by the shared-tranche test only because that site happens to have four
    # labelled tranches; a renamed operator with none would have sat there.
    "iris energy": "iren",
    "iren limited": "iren",
}

#: County-equivalent suffixes to strip, so "Racine County" and "Racine" agree.
#: Louisiana uses Parish, Alaska uses Borough and Census Area.
_COUNTY_SUFFIXES = ("county", "parish", "borough", "census area", "municipality", "city and county")

#: Wording a source uses when it will not name the tenant.
#:
#: These are not identities and must not be treated as ones. Measured on the live
#: database, 4 of the 12 populated `customer` values were of this shape —
#: "Fortune 100 technology company", "Publicly-traded global enterprise
#: (technology company based in the San Francisco Bay Area)". Left as-is they
#: each become a distinct "customer" in any rollup, so a capacity-by-customer
#: table shows four one-project tenants that are probably two real ones, and
#: probably ones already named elsewhere in the table.
#:
#: Matched as substrings of the folded value, because the phrasing varies
#: endlessly and only the hedge is stable.
_UNDISCLOSED_MARKERS = (
    "fortune 100",
    "fortune 500",
    "undisclosed",
    "unnamed",
    "not disclosed",
    "confidential",
    "publicly traded",
    "publicly-traded",
    "global enterprise",
    "hyperscale customer",
    "hyperscaler customer",
    "major technology",
    "leading technology",
    "large technology",
    "investment grade",
    "investment-grade",
    "anchor tenant",
    "single tenant",
    "us-based technology",
    "a technology company",
)


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


def is_undisclosed(customer: str | None) -> bool:
    """True when a `customer` value hedges instead of naming anybody.

    A source that says "a Fortune 100 technology company" has declined to
    identify the tenant. That is worth recording — it tells you the capacity is
    committed — but it is not an identity, and the distinction matters the moment
    anything groups by customer.
    """
    if not customer:
        return False
    slug = _slug(customer)
    return any(marker in slug for marker in _UNDISCLOSED_MARKERS)


def customer_key(customer: str | None) -> str:
    """Normalized end-customer identity, or ``""`` when none is really named.

    Deliberately delegates to :func:`company_key`: a tenant is a company, and
    "Amazon Web Services" must fold to the same key whether it appears as the
    operator of its own campus or as somebody else's customer. Without that,
    a rollup by customer double-counts the same buyer under two spellings.

    Hedged values (see :func:`is_undisclosed`) return ``""`` rather than a key
    made out of the hedge.
    """
    if is_undisclosed(customer):
        return ""
    return company_key(customer)


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


#: Words that appear in half the project names in the industry and identify
#: nothing. A name token has to be rarer than these to suggest two rows are the
#: same site.
#:
#: **The plurals are not padding.** The singular list alone paired Aligned Data
#: *Centers* Phoenix with NTT Global Data *Centers* Americas Phoenix, on the shared
#: token "centers" — two unrelated operators, one city, and a word that appears in
#: a third of the names in the industry. Every generic word here is a word that
#: sends a false pair to the top of the duplicates report, and a false pair costs
#: more than a missed one: `capex.rollup` holds one row of each suspected group
#: out of the buyer table.
_GENERIC_NAME_TOKENS = frozenset(
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
        "campuses",
        "project",
        "site",
        "sites",
        "facility",
        "facilities",
        "expansion",
        "expansions",
        "phase",
        "phases",
        "building",
        "buildings",
        "park",
        "technology",
        "technologies",
        "tech",
        "digital",
        "cloud",
        "ai",
        "americas",
        "global",
        "international",
        "the",
        "and",
        "of",
        "at",
        "new",
        "north",
        "south",
        "east",
        "west",
        "i",
        "ii",
        "iii",
        "iv",
        "1",
        "2",
        "3",
        "4",
        "5",
    }
)

#: Separators a source uses when one campus has several parties behind it:
#: "OpenAI/Oracle", "OpenAI, Oracle", "Oracle & Crusoe".
_PARTY_SPLIT = re.compile(r"[/,&+]| and ")


def company_parts(company: str | None) -> set[str]:
    """Every operator named in a company string, as keys.

    ``"OpenAI/Oracle"`` → ``{"openai", "oracle"}``.

    A big campus routinely has three companies attached — one builds it, one
    leases it, one occupies it — and sources name whichever they care about. Each
    spelling produces its own `dedup_key`, so the same site lands in the database
    several times. Recovering the parts is what lets those rows recognise each
    other.
    """
    if not company:
        return set()
    # A legal suffix that survives the split is a fragment, not a party:
    # "Amazon Web Services, Inc." is one company, and comma-splitting it leaves
    # "Inc." behind. `company_key` cannot strip that on its own, because it only
    # removes a suffix that follows something.
    parts = {company_key(part) for part in _PARTY_SPLIT.split(company)}
    return {p for p in parts if p and p not in _COMPANY_SUFFIXES}


def shares_a_party(a: str | None, b: str | None) -> bool:
    """True when two company strings name at least one operator in common."""
    if not a or not b:
        return False
    common = company_parts(a) & company_parts(b)
    return bool(common)


def shared_parties_across_companies(a: str | None, b: str | None) -> set[str]:
    """Operators two *differently named* companies have in common.

    The distinction this draws is load-bearing, and conflating it is how a
    duplicate report turns into a data-loss bug. `shares_a_party` answers "do
    these strings name a common operator", which is trivially true when both
    strings are the same company — and `capex.suspected_duplicates` has a whole
    pass that buckets rows *by* company, so on that path every pair would report
    party evidence and none of it would mean anything. `dupresolve.HARD_EVIDENCE`
    trusts `party` enough to carry an unattended merge, so the vacuous form would
    have offered to fold NTT's Itasca campus into NTT's Chicago one, 31.7 km away.

    What `party` was built for is the opposite case: "OpenAI/Oracle" against
    "Oracle", where two different company strings name one campus because one
    builds it and the other occupies it. Same company is a bucketing artefact and
    is returned as nothing.
    """
    if not a or not b or company_key(a) == company_key(b):
        return set()
    return company_parts(a) & company_parts(b)


def exact_identity(
    a_name: str | None, a_company: str | None, b_name: str | None, b_company: str | None
) -> bool:
    """True when two rows carry the same company and the same name outright.

    The strongest identity claim available, and until this function nothing looked
    for it. Measured on the live database, six suspected pairs held a byte-identical
    company *and* name — `Flexential — Atlanta-Douglasville` twice,
    `STACK Infrastructure — Stafford Technology Campus` twice, `DataBank — Lithia
    Springs Campus` twice — and every one of them was reported under the weakest
    evidence class the report has, because none of them produced a `name` signal at
    all: :func:`distinctive_name_tokens` drops generic industry words and the
    locality, and "Stafford Technology Campus" in Stafford is nothing else. The
    strictness that stopped "centers" pairing Aligned with NTT is what hid these.

    Ranked above a shared tranche, and trusted for a merge. Two rows that agree on
    both of the fields a person reads first are not a resemblance.
    """
    company = company_key(a_company)
    name = _slug(a_name or "")
    if not company or not name:
        return False
    return company == company_key(b_company) and name == _slug(b_name or "")


#: A trailing ordinal, in the two shapes operators write one: "Forge 2", "SLC02".
_TRAILING_ORDINAL = re.compile(r"^(?P<stem>.*?)[ \-]?(?P<ordinal>\d{1,3})$")


def sibling_ordinals(a_name: str | None, b_name: str | None) -> bool:
    """True when two names are the same site's *neighbours*, not one site twice.

    A campus and the campus next to it share everything a duplicate detector looks
    at — operator, town, often a tranche key an article listed under both — and
    differ in one digit. `Polaris Forge 1` in Ellendale and `Polaris Forge 2` in
    Harwood are two real Applied Digital sites holding the same `forge-2.polaris`
    key, so once a shared tranche is allowed to carry a merge across localities,
    they are foldable and the fold destroys a campus. Same shape for Aligned's
    `SLC02` against `SLC-04`.

    The test is deliberately narrow: both names must reduce to the *same stem* and
    both must carry an ordinal, and the ordinals must differ. One name having a
    number and the other not is the ordinary case of a source being more specific —
    "Sweetwater Data Center" against "IREN Sweetwater 1" is one site written twice —
    and is not a sibling.
    """
    got = []
    for raw in (a_name, b_name):
        match = _TRAILING_ORDINAL.match(_slug(raw or ""))
        if not match:
            return False
        got.append((match.group("stem").strip(), int(match.group("ordinal"))))
    (a_stem, a_ordinal), (b_stem, b_ordinal) = got
    return bool(a_stem) and a_stem == b_stem and a_ordinal != b_ordinal


def distinctive_name_tokens(
    name: str | None, *, locality: str | None = None, company: str | None = None
) -> frozenset[str]:
    """Name words that could identify a specific site.

    Generic industry vocabulary is dropped, and so is the locality: every project
    in Ashburn has "Ashburn" in its name, and treating that as distinctive would
    make fourteen unrelated campuses look like one.

    **The operator's own name goes the same way** when `company` is given. Every
    STACK project is called "STACK something", so the word says which company and
    not which building — and on a pair already known to share an operator it is
    tautological. Measured: it was the entire `name` evidence pairing
    `STACK Portland Expansion` with `STACK Infrastructure Hillsboro Campus`, two
    towns apart.
    """
    if not name:
        return frozenset()
    drop = set(_GENERIC_NAME_TOKENS)
    if locality:
        drop.update(_slug(locality).split())
    if company:
        drop.update(_slug(company).split())
        drop.update(company_key(company).split())
    return frozenset(t for t in _slug(name).split() if t not in drop and len(t) > 2)


#: A metro code and a number: `IAD3`, `VA-2`, `ORD 1`, `PH-1`, `ACC-9`. Every
#: colocation operator in the country names facilities this way, off the nearest
#: airport.
_FACILITY_NUMBER = re.compile(r"^[a-z]{2,4}[ .\-]?\d{1,2}$")


def is_facility_number(key: str | None) -> bool:
    """True when a tranche key is a metro code and a sequence number.

    **Whether this is identity depends entirely on where the two rows are**, which
    is why it is its own predicate rather than a line in
    :func:`is_vocabulary_block_key`. Inside one market the code is the market and
    the number is the building, so two Ashburn rows both holding `va-4` are two
    readings of one building — that is the four-row RagingWire/NTT group, the
    largest on the live database, and treating the key as vocabulary loses it.
    Across two markets the code is *all* it says, and the number is a sequence
    every operator restarts from one: `iad-3` is held by DataBank in Ashburn and
    Aligned in Sterling, `ord-1` and `ord-2` by three operators around Chicago,
    and `va-2` by both RagingWire and Iron Mountain sixty kilometres apart.

    So `capex.shared_identity_keys` applies it only to pairs whose rows sit in
    different localities.
    """
    if not key:
        return False
    return bool(_FACILITY_NUMBER.match(_slug(key).replace(" ", "-")))


def is_market_sequence(
    key: str | None, *, localities: frozenset[str] | set[str] = frozenset()
) -> bool:
    """True when a tranche key is "the Nth building in this market", however spelled.

    Two spellings of one thing. `iad-3` uses the airport code; `hillsboro-1`,
    `chicago-2` and `sweetwater-1` use the town. Both name a market and a sequence
    number, and the number restarts at one for every operator — so two rows holding
    it are in the same market and nothing more is implied.

    **Evidence, but not merge authority**, and the split is what makes this usable.
    Discarding these keys outright would lose real duplicates: `sweetwater-1` is how
    IREN's Sweetwater campus, stored twice across a rename, is connected at all, and
    with the key gone that pair carries no signal and disappears from the report.
    Keeping them as merge evidence would fold Flexential's Hillsboro site into NTT's
    on `hillsboro-1`. So the report shows them and `dupresolve.merge_blocked` refuses
    to merge on them alone — a person answering `--ask` still can.
    """
    if not key:
        return False
    if is_facility_number(key):
        return True
    match = _TRAILING_ORDINAL.match(_slug(key))
    if not match:
        return False
    stem = match.group("stem").strip()
    return bool(stem) and stem in {_slug(name) for name in localities if name}


#: Words that name a *kind* of tranche rather than one. `blocks.TYPE_WORDS` cannot
#: catch these, because it reads a label's own words to decide whether the label is
#: generic, and "existing capacity" is a perfectly specific-looking label that
#: every operator writes. `capex.identifying_block_keys` records the damage:
#: `existing` alone paired Element Critical's Houston One with Switch's Houston
#: campus.
_BLOCK_VOCABULARY = frozenset(
    {
        "capacity",
        "existing",
        "planned",
        "hyperscale",
        "total",
        "new",
        "expansion",
        "phase",
        "building",
        "buildings",
        "hall",
        "halls",
        "site",
        "campus",
        "colocation",
        "retail",
        "wholesale",
        # The power words. A tranche labelled "temporary power" or "permanent plant
        # power" names a *supply arrangement* every large site has, and two rows
        # holding it share an engineering sequence rather than a building. Measured:
        # `permanent.plant.power` and `power.temporary` paired xAI's Colossus with a
        # separate SpaceX row in Memphis.
        "power",
        "plant",
        "permanent",
        "temporary",
        "substation",
        "generation",
        "utility",
        "interconnection",
    }
)


def is_vocabulary_block_key(
    key: str | None, *, localities: frozenset[str] | set[str] = frozenset()
) -> bool:
    """True when a shared tranche key is industry vocabulary, not a building.

    The counterpart to :func:`distinctive_name_tokens`, one level down. A tranche
    key is derived from what an article called a building, so two rows carrying the
    same key are usually two readings of one building — but only if the key names
    something. Two ways it can fail to, both true wherever the rows are:

    * a key made **only of type words and digits** — `capacity-1`, `existing`,
      `a-1.building` — names a kind of tranche. Bare single letters count as
      nothing here for the same reason;
    * a key that is exactly a **locality name** is the locality, and the locality is
      never distinctive. `austin` is a tranche label on Switch's Austin campus and
      on Sabey's in Round Rock, which is one metro and two buildings.

    `localities` is the row's own city and county, slugged. Passing them is what
    makes the second test possible at all: rarity cannot see it, because the key
    really is rare — it is just rare in the way a town's name is.

    A locality word inside a compound key counts the same way, which is what
    separates `expansion.portland` from `crossing.gainesville`. The first says an
    expansion happened in this town and the second names a development; measured,
    `expansion.houston` is the key that paired Element Critical's Houston One with
    Switch's Houston campus, and `expansion.portland` paired STACK's Portland site
    with its Hillsboro one.

    A facility number is *not* judged here, and neither is a market sequence like
    `sweetwater-1`. Whether those identify a building depends on where the two rows
    are, which is a fact about a pair — and they are worth showing a reader even
    when they are not worth merging on. See :func:`is_market_sequence`.
    """
    if not key:
        return True
    slug = _slug(key)
    if not slug:
        return True
    if is_market_sequence(key, localities=localities):
        return False
    places = {_slug(name) for name in localities if name}
    if slug in places:
        return True
    # `county_key` first, so the *word* "county" does not become a place word just
    # because the row's county field spells it out. It did, and it cost the flagship
    # case: `county.shackelford` — the key that ties Stargate's Abilene row to its
    # Shackelford County one — reduced to two place words and was discarded as
    # vocabulary. A tranche named after a county the campus reaches into is a fact
    # about that campus; the county's own suffix is not.
    place_words = {word for place in places for word in (county_key(place) or place).split()}
    parts = [p for p in re.split(r"[.\-_ ]+", slug) if p]
    return all(
        p in _BLOCK_VOCABULARY or p in place_words or p.isdigit() or len(p) < 2 for p in parts
    )


def looks_like_the_same_site(
    a_name: str | None,
    a_company: str | None,
    b_name: str | None,
    b_company: str | None,
    *,
    locality: str | None = None,
) -> bool:
    """Two rows in one locality that are probably one campus seen twice.

    **The narrow case this exists for**, measured on the live database: the
    Abilene Stargate campus was stored four times, as `crusoe|city:abilene|TX`,
    `openai|city:abilene|TX`, `oracle|city:abilene|TX` and
    `openai oracle|city:abilene|TX`. Crusoe builds it, Oracle leases it and OpenAI
    occupies it, so all three companies are real and the keys are all correct —
    the site is simply one building. Grouping by end customer then counted 1.2 GW
    four times, which is exactly the number `tracker capex` exists to report.

    Deliberately NOT "same locality means duplicate". Ashburn holds fourteen
    projects from fourteen genuinely different operators, and Santa Clara,
    Hillsboro, Chicago and Phoenix are all the same. Locality alone is evidence of
    nothing. What separates Abilene is that the *name* survives across the rows
    while the company changes, or that one company string names the other's
    operator.

    Like everything else in this module, a match proposes a review candidate and
    never merges anything.
    """
    if shares_a_party(a_company, b_company):
        return True
    shared = distinctive_name_tokens(a_name, locality=locality) & distinctive_name_tokens(
        b_name, locality=locality
    )
    return bool(shared)


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
    "company_parts",
    "county_key",
    "customer_key",
    "dedup_key",
    "distinctive_name_tokens",
    "is_cross_granularity_match",
    "is_undisclosed",
    "locality",
    "looks_like_county",
    "looks_like_the_same_site",
    "same_company_and_state",
    "shares_a_party",
]
