"""Census-derived county and coordinates.

Fixtures are written to tmp_path rather than committed: the real files are 4 MB
of national reference data, and what needs testing is the *folding and ambiguity*
logic, which a dozen hand-picked rows exercise far more legibly.

The assertions that matter:

* :func:`test_a_multi_county_place_yields_no_county` — the honesty rule. Kansas
  City touches four counties; choosing one would invent a fact.
* :func:`test_a_derived_source_does_not_change_confidence` — a Census centroid
  proves a city exists and says nothing about whether the data center does.
* :func:`test_an_article_sourced_county_is_never_overwritten`.
"""

from __future__ import annotations

import types
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from tracker.confidence import SourceView, compute, is_derived
from tracker.dedup import dedup_key
from tracker.ingest import geo
from tracker.models import Project, Source

COUNTY_ROWS = """\
STATE|STATEFP|COUNTYFP|COUNTYNAME|PLACEFP|PLACENS|PLACENAME|TYPE|CLASSFP|FUNCSTAT
WI|55|101|Racine County|53775|01584324|Mount Pleasant village|INCORPORATED PLACE|C1|A
TN|47|157|Shelby County|48000|02405177|Memphis city|INCORPORATED PLACE|C1|A
TX|48|441|Taylor County|01924|02409675|Abilene city|INCORPORATED PLACE|C1|A
TX|48|253|Jones County|01924|02409675|Abilene city|INCORPORATED PLACE|C1|A
GA|13|245|Richmond County|04204|02405078|Augusta-Richmond County consolidated government (balance)|INCORPORATED PLACE|C8|F
IN|18|097|Marion County|36003|02395424|Indianapolis city (balance)|INCORPORATED PLACE|C8|F
MO|29|095|Jackson County|38000|02395064|Kansas City city|INCORPORATED PLACE|C1|A
MO|29|047|Clay County|38000|02395064|Kansas City city|INCORPORATED PLACE|C1|A
IL|17|031|Cook County|14000|00428803|Chicago city|INCORPORATED PLACE|C1|A
MO|29|183|St. Charles County|64730|02397907|St. Charles city|INCORPORATED PLACE|C1|A
"""

GAZ_ROWS = (
    "USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\t"
    "ALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG          \n"
    "WI\t5553775\t01584324\tMount Pleasant village\t47\tA\t1\t1\t1\t1\t42.708832\t-87.884894   \n"
    "TN\t4748000\t02405177\tMemphis city\t25\tA\t1\t1\t1\t1\t35.109164\t-89.968511   \n"
    "TX\t4801924\t02409675\tAbilene city\t25\tA\t1\t1\t1\t1\t32.454514\t-99.738147   \n"
    "GA\t1304204\t02405078\tAugusta-Richmond County consolidated government (balance)"
    "\t00\tF\t1\t1\t1\t1\t33.383084\t-82.070778   \n"
    "MO\t2938000\t02395064\tKansas City city\t25\tA\t1\t1\t1\t1\t39.124228\t-94.550898   \n"
)


@pytest.fixture
def census_dir(tmp_path: Path) -> Path:
    root = tmp_path / "census"
    root.mkdir()
    (root / geo.COUNTY_FILE).write_text(COUNTY_ROWS, encoding="utf-8")
    with zipfile.ZipFile(root / geo.GAZETTEER_FILE, "w") as archive:
        archive.writestr("2024_Gaz_place_national.txt", GAZ_ROWS)
    return root


@pytest.fixture
def places(census_dir: Path):
    return geo.load_places(census_dir)


def projects(session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.id)))


def sources(session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.id)))


def add(session, **kwargs) -> Project:
    defaults = {
        "name": "Campus",
        "company": "Acme",
        "country": "US",
        "phase": "announced",
        "confidence": 1,
    }
    merged = {**defaults, **kwargs}
    merged.setdefault(
        "dedup_key",
        dedup_key(merged["company"], merged.get("city"), merged.get("county"), merged["state"]),
    )
    project = Project(**merged)
    session.add(project)
    session.flush()
    return project


# --- name folding -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mount Pleasant village", "mount pleasant"),
        ("Memphis city", "memphis"),
        ("Abanda CDP", "abanda"),
        ("Autaugaville town", "autaugaville"),
        ("St. Charles city", "st charles"),
        ("Indianapolis city (balance)", "indianapolis"),
        ("Mount Pleasant", "mount pleasant"),
        ("  MEMPHIS  ", "memphis"),
        (None, ""),
    ],
)
def test_place_key_folds_census_spellings_onto_article_spellings(raw, expected):
    assert geo.place_key(raw) == expected


def test_place_key_is_stable_across_apostrophe_styles():
    """Articles use the straight quote, Census data the typographic one."""
    curly = "O" + chr(0x2019) + "Fallon"  # right single quotation mark
    assert geo.place_key("O'Fallon city") == geo.place_key(curly) == "ofallon"


def test_only_a_consolidated_government_gets_an_alias():
    assert geo.place_aliases("Augusta-Richmond County consolidated government (balance)") == (
        "augusta",
    )
    assert geo.place_aliases("Nashville-Davidson metropolitan government") == ("nashville",)
    assert geo.place_aliases("Memphis city") == ()
    assert geo.place_aliases("Mount Pleasant village") == ()


# --- lookup -----------------------------------------------------------------


def test_lookup_resolves_county_and_point(places):
    place = geo.lookup("Mount Pleasant", "WI", places)
    assert place is not None
    assert place.county == "Racine County"
    assert (place.lat, place.lon) == (42.708832, -87.884894)


def test_a_multi_county_place_yields_no_county(places):
    """The honesty rule: Abilene straddles Taylor and Jones."""
    place = geo.lookup("Abilene", "TX", places)
    assert place is not None
    assert place.spans_counties
    assert place.counties == ("Taylor County", "Jones County")
    assert place.county is None, "picking one of two counties would invent a fact"
    # Coordinates are still perfectly good.
    assert place.lat == 32.454514


def test_a_consolidated_city_is_found_under_its_plain_name(places):
    """Regression: Augusta GA and Indianapolis IN were the only unresolved cities."""
    augusta = geo.lookup("Augusta", "GA", places)
    assert augusta is not None
    assert augusta.county == "Richmond County"
    assert augusta.lat == 33.383084

    indy = geo.lookup("Indianapolis", "IN", places)
    assert indy is not None
    assert indy.county == "Marion County"


def test_lookup_is_state_scoped(places):
    assert geo.lookup("Memphis", "WI", places) is None
    assert geo.lookup("Memphis", "TN", places) is not None


def test_lookup_needs_both_city_and_state(places):
    assert geo.lookup(None, "TN", places) is None
    assert geo.lookup("Memphis", None, places) is None


def test_missing_reference_files_name_the_download_urls(tmp_path):
    with pytest.raises(geo.GeoDataMissing) as exc:
        geo.load_places(tmp_path)
    message = str(exc.value)
    assert geo.COUNTY_FILE in message
    assert "www2.census.gov" in message, "the operator must be told where to get it"


# --- run() ------------------------------------------------------------------


def test_run_fills_county_and_coordinates(session, census_dir):
    add(session, city="Mount Pleasant", state="WI")
    report = geo.run(session, data_dir=census_dir)

    project = projects(session)
    assert report.county_filled == 1
    assert report.coords_filled == 1

    row = session.get(Project, project[0].id)
    assert row.county == "Racine County"
    assert (row.lat, row.lon) == (42.708832, -87.884894)


def test_run_records_a_citable_derived_source(session, census_dir):
    add(session, city="Memphis", state="TN")
    geo.run(session, data_dir=census_dir)

    source = sources(session)[0]
    assert source.url == geo.CITATION_URL
    assert source.extractor == geo.EXTRACTOR
    # The excerpt must quote the operator's own spelling, not the folded key --
    # earlier it read "mount pleasant (WI)".
    assert "Memphis, TN" in source.excerpt
    assert "NOT the project site" in source.excerpt, "centroid precision must be disclosed"


def test_an_article_sourced_county_is_never_overwritten(session, census_dir):
    """county/lat/lon are FILL_ONLY: a reported value beats a derived one."""
    add(session, city="Mount Pleasant", state="WI", county="Something Else County")
    geo.run(session, data_dir=census_dir)
    row = projects(session)[0]
    assert row.county == "Something Else County"


def test_run_leaves_county_unset_for_a_multi_county_city(session, census_dir):
    add(session, city="Abilene", state="TX")
    report = geo.run(session, data_dir=census_dir)

    row = projects(session)[0]
    assert row.county is None
    assert row.lat == 32.454514, "coordinates are still derivable"
    assert report.spans_counties == 1
    assert report.county_filled == 0
    assert any("Abilene" in name for name in report.multi_county_places)


def test_run_is_idempotent(session, census_dir):
    add(session, city="Memphis", state="TN")
    geo.run(session, data_dir=census_dir)
    second = geo.run(session, data_dir=census_dir)
    assert second.county_filled == 0
    assert second.coords_filled == 0
    assert second.already_complete == 1
    assert len(sources(session)) == 1


def test_run_reports_an_unknown_city_instead_of_guessing(session, census_dir):
    add(session, city="Nowheresville", state="WI")
    report = geo.run(session, data_dir=census_dir)
    assert report.unmatched == 1
    assert "Nowheresville, WI" in report.unmatched_places
    assert projects(session)[0].county is None


def test_run_skips_a_project_with_no_city(session, census_dir):
    add(session, city=None, county="Loudoun County", state="VA")
    report = geo.run(session, data_dir=census_dir)
    assert report.no_city == 1
    assert report.considered == 0


def test_dry_run_writes_nothing(session, census_dir):
    add(session, city="Memphis", state="TN")
    report = geo.run(session, data_dir=census_dir, dry_run=True)
    assert report.county_filled == 1, "it still reports what it would do"
    assert sources(session) == []
    assert projects(session)[0].county is None


# --- confidence -------------------------------------------------------------


def view(**kwargs) -> SourceView:
    base = {"source_type": "trade_press", "url": "https://example.com/a", "fields": "mw_planned"}
    return SourceView.from_row(types.SimpleNamespace(**{**base, "claims": None, **kwargs}))


def test_a_derived_source_is_recognised():
    assert is_derived(view(extractor=geo.EXTRACTOR))
    assert not is_derived(view(extractor="extract-v1@3f2a91c4"))
    assert not is_derived(view(extractor=None))


def test_a_derived_source_does_not_change_confidence():
    """The Census confirms a city exists. It corroborates nothing about a project.

    Without this, one press release plus a Census lookup would read as two
    independent domains and reach 3 -- the score reserved for corroborated facts.
    """
    reported = view(source_type="company_filing", url="https://news.acme.com/x")
    census = view(
        source_type="government_doc",
        url=geo.CITATION_URL,
        extractor=geo.EXTRACTOR,
        fields="county,lat,lon",
    )

    alone = compute([reported])
    with_census = compute([reported, census])
    assert with_census.value == alone.value
    assert any("do not corroborate" in r for r in with_census.reasons)


def test_a_project_cited_only_by_derived_data_scores_zero():
    census = view(source_type="government_doc", url=geo.CITATION_URL, extractor=geo.EXTRACTOR)
    assert compute([census]).value == 0
