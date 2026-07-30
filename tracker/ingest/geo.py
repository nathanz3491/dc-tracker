"""County and coordinates derived from US Census reference data.

These three fields are not a research problem, they are a lookup. An article
writes "Mount Pleasant, Wisconsin" and almost never adds "Racine County", so
asking an LLM to search harder cannot fix `county` — but the Census publishes the
place-to-county mapping, and `lat`/`lon` were 0% covered because articles do not
print coordinates at all, so the evidence gate correctly discarded every one the
model ever guessed. Derivation is the only honest route to either.

Two files, both free, no API key, no rate limit:

* ``national_place_by_county2020.txt`` — pipe-delimited place → county. A place
  appears once per county it touches.
* ``2024_Gaz_place_national.zip`` — tab-delimited gazetteer carrying each place's
  internal point (``INTPTLAT``/``INTPTLONG``).

Two honesty constraints shape the design:

1. **A place centroid is not the site.** "Abilene, TX" resolves to the middle of
   Abilene, several kilometres from the campus. Good enough to put a dot on a
   national map, wrong for anything else, so every derived coordinate records that
   its precision is the place and not the project.
2. **A place spanning several counties has no derivable county.** Kansas City
   touches four. Picking one would invent a fact, so `county` is left NULL and the
   ambiguity is reported.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Where `tracker enrich geo` expects the Census files.
COUNTY_FILE: Final = "place_by_county2020.txt"
GAZETTEER_FILE: Final = "gaz_place.zip"

#: Stable Census URLs, printed when a file is missing so the operator can fetch it.
SOURCE_URLS: Final[dict[str, str]] = {
    COUNTY_FILE: (
        "https://www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt"
    ),
    GAZETTEER_FILE: (
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
        "2024_Gazetteer/2024_Gaz_place_national.zip"
    ),
}

#: Citable URL recorded on a derived source row. Points at the reference data
#: itself, so a reviewer can check the mapping by hand.
CITATION_URL: Final = SOURCE_URLS[COUNTY_FILE]

#: Marks a source row as derived rather than reported. `confidence` excludes these
#: from scoring: the Census confirms a city exists, and says nothing whatever about
#: whether this data center is real or how large it is.
EXTRACTOR: Final = "derived:census-place-2020"

#: Census appends a legal-entity type to every place name ("Abbeville city",
#: "Autaugaville town", "Abanda CDP"). Articles never do, so both sides are folded
#: to a bare name before matching.
_PLACE_TYPES: Final[tuple[str, ...]] = (
    "consolidated government",
    "metropolitan government",
    "metro government",
    "unified government",
    "city and borough",
    "city and county",
    "municipality",
    "municipality of",
    "corporation",
    "urban county",
    "township",
    "borough",
    "village",
    "county",
    "city",
    "town",
    "cdp",
    "comunidad",
    "zona urbana",
    "plantation",
    "gore",
    "grant",
    "location",
    "reservation",
    "purchase",
)

_PLACE_TYPE_RE: Final = re.compile(
    r"\s+(?:" + "|".join(re.escape(t) for t in _PLACE_TYPES) + r")$", re.I
)


class GeoDataMissing(RuntimeError):
    """A required Census file is not on disk."""


#: Markers of a consolidated city-county, which Census names after *both* the city
#: and the county: "Augusta-Richmond County consolidated government (balance)",
#: "Nashville-Davidson metropolitan government", "Louisville/Jefferson County metro
#: government". An article says "Augusta". Without an alias on the leading
#: component these places are simply unfindable — Augusta GA and Indianapolis IN
#: were the only two project cities the first version failed to resolve.
_CONSOLIDATED_RE: Final = re.compile(
    r"consolidated government|metropolitan government|metro government"
    r"|unified government|city and county|city and borough",
    re.I,
)

#: "(balance)" and "(part)" qualify the Census entity, never the place name.
_PARENTHETICAL_RE: Final = re.compile(r"\s*\([^)]*\)")

#: Periods and both apostrophe forms (ASCII and typographic), dropped so every
#: spelling of "O'Fallon" folds together -- articles use the straight quote,
#: Census data the curly one. The curly form is an escape to keep this file ASCII.
_PUNCT_RE: Final = re.compile("[.'\u2019]")


def place_key(name: str | None) -> str:
    """Fold a place name so an article's spelling matches the Census spelling.

    Strips parentheticals, the trailing legal-entity type, punctuation and case.
    Applied to *both* sides, so "St. Charles" and "St Charles city" agree.
    """
    if not name:
        return ""
    folded = _PARENTHETICAL_RE.sub("", name).strip()
    # Repeat: "Athens-Clarke County unified government" carries two type words.
    for _ in range(3):
        stripped = _PLACE_TYPE_RE.sub("", folded)
        if stripped == folded:
            break
        folded = stripped
    folded = folded.lower().replace("&", "and")
    folded = _PUNCT_RE.sub("", folded)
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def place_aliases(name: str | None) -> tuple[str, ...]:
    """Extra keys a place should also be findable under. Never the primary key.

    Only consolidated city-counties get one, and only on the component before the
    separator — the part that is the city's actual name.
    """
    if not name or not _CONSOLIDATED_RE.search(name):
        return ()
    bare = _PARENTHETICAL_RE.sub("", name).strip()
    head = re.split(r"[-/]", bare, maxsplit=1)[0]
    alias = place_key(head)
    return (alias,) if alias and alias != place_key(name) else ()


@dataclass(frozen=True)
class Place:
    """One Census place: which counties it touches, and its internal point."""

    state: str
    #: The *folded* lookup key, not a display name — Census spellings like
    #: "Mount Pleasant village" are normalized away. Anything shown to an operator
    #: should use the project's own city spelling instead.
    key: str
    counties: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None

    @property
    def county(self) -> str | None:
        """The county, only when the place lies in exactly one.

        A place spanning several has no derivable county — choosing among them
        would be inventing the answer.
        """
        return self.counties[0] if len(self.counties) == 1 else None

    @property
    def spans_counties(self) -> bool:
        return len(self.counties) > 1


def load_counties(path: Path) -> dict[tuple[str, str], list[str]]:
    """(state, place_key) → every county the place touches, in file order.

    Aliases are applied in a second pass so a place whose real name matches can
    never be displaced by another place's alias.
    """
    out: dict[tuple[str, str], list[str]] = {}
    aliased: list[tuple[tuple[str, str], str]] = []

    with path.open(encoding="latin-1", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="|"):
            state = (row.get("STATE") or "").strip().upper()
            name = row.get("PLACENAME")
            key = place_key(name)
            county = (row.get("COUNTYNAME") or "").strip()
            if not state or not key or not county:
                continue
            counties = out.setdefault((state, key), [])
            if county not in counties:
                counties.append(county)
            for alias in place_aliases(name):
                aliased.append(((state, alias), county))

    for key, county in aliased:
        if key in out:
            continue
        out.setdefault(key, []).append(county)
    return out


def load_points(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """(state, place_key) → (lat, lon) internal point from the gazetteer."""
    raw = _read_gazetteer(path)
    out: dict[tuple[str, str], tuple[float, float]] = {}
    aliased: list[tuple[tuple[str, str], tuple[float, float]]] = []

    for row in csv.DictReader(io.StringIO(raw), delimiter="\t"):
        # The gazetteer pads every line, so both keys and values need stripping.
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        state = clean.get("USPS", "").upper()
        name = clean.get("NAME")
        key = place_key(name)
        try:
            point = (float(clean["INTPTLAT"]), float(clean["INTPTLONG"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not state or not key:
            continue
        out.setdefault((state, key), point)
        for alias in place_aliases(name):
            aliased.append(((state, alias), point))

    for key, point in aliased:
        out.setdefault(key, point)
    return out


def _read_gazetteer(path: Path) -> str:
    """Text of the gazetteer, whether it is the .zip or the extracted .txt."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".txt")]
            if not members:
                raise GeoDataMissing(f"{path} contains no .txt member")
            return archive.read(members[0]).decode("latin-1")
    return path.read_text(encoding="latin-1")


def load_places(data_dir: Path) -> dict[tuple[str, str], Place]:
    """Build the lookup table from both Census files in `data_dir`."""
    county_path = data_dir / COUNTY_FILE
    gaz_path = data_dir / GAZETTEER_FILE
    missing = [p for p in (county_path, gaz_path) if not p.exists()]
    if missing:
        lines = [f"missing Census reference data in {data_dir}:"]
        for path in missing:
            lines.append(f"  {path.name}  <-  {SOURCE_URLS[path.name]}")
        lines.append("Download both, then re-run. They are free and need no API key.")
        raise GeoDataMissing("\n".join(lines))

    counties = load_counties(county_path)
    points = load_points(gaz_path)

    places: dict[tuple[str, str], Place] = {}
    for key in counties.keys() | points.keys():
        state, folded = key
        lat_lon = points.get(key)
        places[key] = Place(
            state=state,
            key=folded,
            counties=tuple(counties.get(key, ())),
            lat=lat_lon[0] if lat_lon else None,
            lon=lat_lon[1] if lat_lon else None,
        )
    log.debug("loaded %d Census places", len(places))
    return places


def lookup(
    city: str | None, state: str | None, places: dict[tuple[str, str], Place]
) -> Place | None:
    """Find a place by city and two-letter state."""
    if not city or not state:
        return None
    return places.get((state.strip().upper(), place_key(city)))


@dataclass
class GeoReport:
    """What one `enrich geo` pass did, and what it deliberately did not do."""

    considered: int = 0
    county_filled: int = 0
    coords_filled: int = 0
    already_complete: int = 0
    spans_counties: int = 0
    unmatched: int = 0
    no_city: int = 0
    unmatched_places: list[str] = dc_field(default_factory=list)
    multi_county_places: list[str] = dc_field(default_factory=list)


def _excerpt(
    where: str, place: Place, county: str | None, point: tuple[float, float] | None
) -> str:
    """A human-checkable statement of what was derived, and from what.

    `where` is the project's own "City, ST" spelling. `Place.key` is folded for
    matching, so quoting it back produced excerpts reading "mount pleasant (WI)".
    """
    parts = []
    if county:
        parts.append(f"Census 2020 place-to-county: {where} is in {county}.")
    if point:
        parts.append(
            f"Census 2024 gazetteer internal point for {where}: "
            f"{point[0]}, {point[1]} — the centre of the place, NOT the project site."
        )
    if place.spans_counties:
        parts.append(
            f"County left unset: this place spans {len(place.counties)} counties "
            f"({', '.join(place.counties)}), so it cannot be derived from the city alone."
        )
    return " ".join(parts)


def run(
    session: Session,
    *,
    data_dir: Path,
    dry_run: bool = False,
    only_project_id: int | None = None,
) -> GeoReport:
    """Fill `county`, `lat` and `lon` from Census reference data.

    Goes through `upsert_record` like every other ingest path, so the derived
    values land as claims on a citable source row and `source.fields` stays
    derived rather than asserted. All three columns are FILL_ONLY, so a value an
    article actually stated is never overwritten by a lookup.

    `only_project_id` narrows the pass to one row, so a single-project command does
    not silently rewrite the whole database as a side effect.
    """
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.models import Project, utcnow
    from tracker.upsert import upsert_record

    places = load_places(data_dir)
    report = GeoReport()

    stmt = select(Project).order_by(Project.id)
    if only_project_id is not None:
        stmt = stmt.where(Project.id == only_project_id)
    projects = list(session.scalars(stmt))
    for project in projects:
        if project.county is not None and project.lat is not None:
            report.already_complete += 1
            continue
        if not project.city:
            # Only a county is known, so there is no place name to look up.
            report.no_city += 1
            continue

        report.considered += 1
        place = lookup(project.city, project.state, places)
        if place is None:
            report.unmatched += 1
            report.unmatched_places.append(f"{project.city}, {project.state}")
            continue

        claims: dict[str, object] = {}
        county = place.county if project.county is None else None
        point = (place.lat, place.lon) if project.lat is None and place.lat is not None else None

        if county:
            claims["county"] = county
        if point:
            claims["lat"], claims["lon"] = point
        if place.spans_counties and project.county is None:
            report.spans_counties += 1
            report.multi_county_places.append(
                f"{project.city}, {project.state} ({len(place.counties)} counties)"
            )
        if not claims:
            continue

        if county:
            report.county_filled += 1
        if point:
            report.coords_filled += 1
        if dry_run:
            continue

        record = IngestRecord(
            project={
                "company": project.company,
                "city": project.city,
                "county": project.county,
                "state": project.state,
                "country": project.country,
                **claims,
            },
            sources=[
                SourceRecord(
                    url=CITATION_URL,
                    source_type="government_doc",
                    fetched_at=utcnow(),
                    excerpt=_excerpt(f"{project.city}, {project.state}", place, county, point),
                    claims=dict(claims),
                    extractor=EXTRACTOR,
                )
            ],
        )
        upsert_record(session, record)

    return report


__all__ = [
    "CITATION_URL",
    "COUNTY_FILE",
    "EXTRACTOR",
    "GAZETTEER_FILE",
    "SOURCE_URLS",
    "GeoDataMissing",
    "GeoReport",
    "Place",
    "load_places",
    "lookup",
    "place_aliases",
    "place_key",
    "run",
]
