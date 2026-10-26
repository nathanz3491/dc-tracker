"""Rows whose citations describe more than one campus (`tracker duplicates mixed`).

The mirror of the duplicates report, measured on the 2026-10-26 production snapshot:
AWS's #158 in Spotsylvania County, named for the Carter's Store Tech Campus, also
cited the Cosner, Orrock and Crossroads campuses; NTT's Mesa rows held the Elliot Road
campus and the land at Pecos and Crismon roads. Each test below pins one rule, and
most of them pin a way a sound row was once listed by mistake.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from tracker.cli import app
from tracker.db import session_scope
from tracker.mixed import mixed_rows
from tracker.models import Project, Source


def _campus(session, **kw) -> Project:
    defaults = {
        "company": "Amazon Web Services",
        "name": "Carter's Store Tech Campus",
        "county": "Spotsylvania County",
        "state": "VA",
        "dedup_key": "amazon|county:spotsylvania|VA",
        "phase": "permitting",
        "confidence": 1,
    }
    project = Project(**{**defaults, **kw})
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url, name, *, quotes=None, ruled=None, **claims) -> Source:
    """One citation naming the site `name`, with a phase claim so it carries a value."""
    source = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        claims=json.dumps(
            {
                "name": name,
                "company": project.company,
                "state": project.state,
                "phase": "permitting",
                **claims,
            }
        ),
        quotes=json.dumps(quotes or {}),
        unconfirmed_reasons=json.dumps(ruled) if ruled else None,
    )
    session.add(source)
    session.flush()
    return source


def _listed(session) -> dict[int, list[str]]:
    return {row.project_id: [s.label for s in row.sites] for row in mixed_rows(session)}


def _spotsylvania(session) -> Project:
    """#158 as the snapshot held it: three campuses, each named by two publishers."""
    row = _campus(session)
    _cite(session, row, "https://dcd.example/cosner", "Cosner Tech Campus", mw_planned=450.0)
    _cite(session, row, "https://fxbg.example/cosner", "Cosner Tech")
    _cite(session, row, "https://parks.example/orrock", "Amazon Orrock Tech Campus")
    _cite(session, row, "https://dcd.example/orrock", "Orrock Tech Campus")
    _cite(session, row, "https://wjla.example/crossroads", "Crossroads Technology Campus")
    _cite(session, row, "https://potomac.example/crossroads", "Crossroads Technology Campus")
    _cite(session, row, "https://local.example/amazon", "Amazon Spotsylvania data centers")
    return row


def test_a_row_holding_three_campuses_is_listed_with_who_says_which(session):
    row = _spotsylvania(session)
    (found,) = mixed_rows(session)
    assert found.project_id == row.id
    assert sorted(s.label for s in found.sites) == [
        "Amazon Orrock Tech Campus",
        "Cosner Tech Campus",
        "Crossroads Technology Campus",
    ]
    cosner = next(s for s in found.sites if s.label == "Cosner Tech Campus")
    assert len(cosner.source_ids) == 2 and cosner.mw == (450.0,)
    # The generic citation names no site and is no site's evidence.
    assert sum(len(s.source_ids) for s in found.sites) == 6
    assert "never name each other" in found.why


def test_a_figure_the_gate_did_not_confirm_is_not_shown(session):
    """ATL3's row showed "9,000 MW" and "40,000 MW" on 10-26: misreadings stored
    unconfirmed, which no published number uses. The citation still tells the sites
    apart; only its figure is left out."""
    row = _spotsylvania(session)
    misread = _cite(session, row, "https://wrong.example/cosner", "Cosner Tech", mw_planned=9000.0)
    misread.unconfirmed_fields = "mw_planned"
    session.flush()
    (found,) = mixed_rows(session)
    cosner = next(s for s in found.sites if misread.id in s.source_ids)
    assert "Cosner" in cosner.label and len(cosner.source_ids) == 3
    assert cosner.mw == (450.0,)


def test_one_campus_with_several_building_codes_is_not_listed(session):
    """NTT's VA1 to VA7 are seven buildings on one Ashburn campus."""
    row = _campus(
        session,
        company="NTT Global Data Centers",
        name="NTT Ashburn Campus",
        city="Ashburn",
        county="Loudoun County",
        dedup_key="ntt|city:ashburn|VA",
    )
    for code in ("VA1", "VA2", "VA3", "VA4", "VA5"):
        _cite(session, row, f"https://ntt.example/{code}", f"NTT Ashburn {code}")
        _cite(session, row, f"https://press.example/{code}", f"{code} Data Center")
    assert _listed(session) == {}


def test_spelling_variants_and_aliases_are_one_campus(session):
    """A misspelling, a possessive, a plural, "Centre" for "Center", and the operator's
    own names in front: none of it is a second site. Each spelling is stated by two
    publishers, so only reading them as one name keeps the row off the list."""
    row = _campus(session)
    _cite(session, row, "https://a.example/1", "Carter's Store Tech Campus")
    _cite(session, row, "https://b.example/2", "Carters Store Technology Campus")
    _cite(session, row, "https://c.example/3", "Amazon Web Services Carter Store Campus")
    _cite(session, row, "https://d.example/4", "AWS Carters Store data center")
    camellia = _campus(
        session,
        company="OpenAI",
        name="Project Camellia",
        county="Effingham County",
        state="GA",
        dedup_key="openai|county:effingham|GA",
    )
    _cite(session, camellia, "https://e.example/5", "Camellia Tech Center")
    _cite(session, camellia, "https://f.example/6", "OpenAI Camellia Tech Centre")
    _cite(session, camellia, "https://g.example/7", "Camelia Technology Center")
    _cite(session, camellia, "https://h.example/8", "Camelia data center campus")
    assert _listed(session) == {}


def test_a_codename_beside_a_public_name_is_one_campus(session):
    """Apple's Waukee campus was "Project Morgan" before it had a public name."""
    row = _campus(
        session,
        company="Apple",
        name="Apple Waukee Data Center Campus",
        city="Waukee",
        state="IA",
        dedup_key="apple|city:waukee|IA",
    )
    _cite(session, row, "https://a.example/1", "Project Morgan")
    _cite(session, row, "https://b.example/2", "Project Morgan")
    _cite(session, row, "https://c.example/3", "Apple Hickman Campus")
    _cite(session, row, "https://d.example/4", "Apple Hickman Road data center")
    assert _listed(session) == {}


def test_two_roads_alone_are_one_parcel_seen_from_two_sides(session):
    """DataBank's Red Oak campus is "along Stainback Road" in one article and at
    "3080 Batchler Rd." in another. Neither names a site, so neither is listed."""
    row = _campus(
        session,
        company="DataBank",
        name="Red Oak Campus",
        city="Red Oak",
        county="Ellis County",
        state="TX",
        dedup_key="databank|city:red oak|TX",
    )
    stainback = {"county": "Located along Stainback Road in Ellis County"}
    batchler = {"city": "3080 Batchler Rd. - Red Oak, TX 75154"}
    _cite(session, row, "https://a.example/1", "Red Oak Campus", quotes=stainback)
    _cite(session, row, "https://b.example/2", "Red Oak campus", quotes=stainback)
    _cite(session, row, "https://c.example/3", "Red Oak Data Center", quotes=batchler)
    _cite(session, row, "https://d.example/4", "Red Oak Campus", quotes=batchler)
    assert _listed(session) == {}


def test_one_publishers_nickname_is_not_a_second_campus(session):
    """RagingWire's Ashburn campus was "The Bolt" in one 2017 article."""
    row = _campus(
        session,
        company="RagingWire Data Centers",
        name="RagingWire Ashburn Campus",
        city="Ashburn",
        county="Loudoun County",
        dedup_key="ragingwire|city:ashburn|VA",
    )
    at_guilford = {"city": "44664 Guilford Drive, Ashburn VA 20147"}
    _cite(session, row, "https://a.example/1", "RagingWire Ashburn", quotes=at_guilford)
    _cite(session, row, "https://b.example/2", "Ashburn campus", quotes=at_guilford)
    _cite(session, row, "https://c.example/3", "The Bolt", mw_planned=21.0)
    assert _listed(session) == {}


def test_a_directory_page_for_a_street_corner_counts_on_its_own(session):
    """NTT's Mesa row: one directory page per site, one of them only a corner."""
    row = _campus(
        session,
        company="NTT Global Data Centers",
        name="NTT Global Data Centers Mesa",
        city="Mesa",
        county="Maricopa County",
        state="AZ",
        dedup_key="ntt|city:mesa|AZ",
    )
    _cite(
        session,
        row,
        "https://servercountry.org/data/projects/ntt-mesa-elliot-road-az/",
        "NTT Global Data Centers Mesa Campus (Elliot Road)",
        mw_planned=240.0,
    )
    _cite(
        session,
        row,
        "https://servercountry.org/data/projects/ntt-mesa-pecos-crismon-az/",
        "NTT Global Data Centers Mesa Campus (Pecos & Crismon)",
        mw_planned=360.0,
    )
    (found,) = mixed_rows(session)
    assert found.project_id == row.id
    assert {r for s in found.sites for r in s.roads} == {"elliot", "pecos", "crismon"}


def test_a_road_its_sentences_give_joins_a_code_to_the_campus_named_there(session):
    """#112: PH1's pages say Elliot Road; the Pacific Proving pages say Pecos and
    Crismon roads, and one of them is PH10. Two campuses, both stated twice."""
    row = _campus(
        session,
        company="NTT Ltd.",
        name="NTT Phoenix Data Center Campus",
        city="Mesa",
        county="Maricopa County",
        state="AZ",
        dedup_key="ntt|city:mesa|AZ",
    )
    elliot = {"city": "The campus is located along Elliot Road in Mesa."}
    pecos = {"name": "planned PH10 development at the NEC of Pecos and Crismon roads"}
    bought = {"city": "The park sits at the northeast corner of Pecos and Crismon roads."}
    _cite(session, row, "https://ntt.example/ph1", "Phoenix PH1 Data Center", quotes=elliot)
    _cite(session, row, "https://dcf.example/ph1", "PH1", quotes=elliot)
    _cite(session, row, "https://dcd.example/pp", "Pacific Proving Technology Park", quotes=bought)
    _cite(session, row, "https://azbex.example/ph10", "PH10", quotes=pecos)
    _cite(session, row, "https://tribune.example/pp", "Pacific Proving Technology Campus")
    (found,) = mixed_rows(session)
    labels = {s.label for s in found.sites}
    assert "Pacific Proving Technology Park" in labels
    assert len(found.sites) == 2
    pacific = next(s for s in found.sites if s.label == "Pacific Proving Technology Park")
    assert len(pacific.source_ids) == 3, "PH10 sits with the site its sentence names"


def test_a_project_entity_named_for_the_site_does_not_erase_the_name(session):
    """The landowner "Pacific Proving LLC" sold NTT the land; its name is the site's."""
    row = _campus(
        session,
        company="NTT DATA",
        name="NTT DATA Mesa",
        city="Mesa",
        county="Maricopa County",
        state="AZ",
        dedup_key="ntt data|city:mesa|AZ",
    )
    elliot = {"city": "NTT Data broke ground on 102 acres in the Elliot Road Technology Corridor."}
    _cite(session, row, "https://a.example/1", "NTT Data Mesa Campus", quotes=elliot)
    _cite(session, row, "https://b.example/2", "NTT Data Mesa campus", quotes=elliot)
    for url in ("https://c.example/3", "https://d.example/4"):
        source = _cite(session, row, url, "Pacific Proving Technology Park")
        source.parties = json.dumps([{"name": "Pacific Proving LLC", "role": "owner"}])
    session.flush()
    (found,) = mixed_rows(session)
    assert "Pacific Proving Technology Park" in {s.label for s in found.sites}


def test_a_sentence_naming_both_keeps_them_one_campus(session):
    """Fermi's Matador articles say it is "officially named" the Trump campus."""
    row = _campus(
        session,
        company="Fermi America",
        name="Project Matador",
        city="Amarillo",
        state="TX",
        dedup_key="fermi america|city:amarillo|TX",
    )
    both = {
        "name": "HyperGrid, officially named the Donald J. Trump Advanced Energy and "
        "Intelligence Campus, is under construction."
    }
    _cite(session, row, "https://a.example/1", "HyperGrid campus", quotes=both)
    _cite(session, row, "https://b.example/2", "HyperGrid")
    formal = "Donald J. Trump Advanced Energy and Intelligence Campus"
    _cite(session, row, "https://c.example/3", formal)
    _cite(session, row, "https://d.example/4", formal)
    assert _listed(session) == {}


def test_a_brand_one_company_uses_far_apart_is_not_a_site(session):
    """Switch's SUPERNAP is in Las Vegas and in Reno; at Reno it is the Citadel."""
    vegas = _campus(
        session,
        company="Switch",
        name="SUPERNAP Las Vegas Campus",
        city="Las Vegas",
        state="NV",
        dedup_key="switch|city:las vegas|NV",
        lat=36.17,
        lon=-115.14,
    )
    reno = _campus(
        session,
        company="Switch",
        name="SUPERNAP Tahoe Reno Industrial Center",
        city="Reno",
        state="NV",
        dedup_key="switch|city:reno|NV",
        lat=39.53,
        lon=-119.81,
    )
    _cite(session, vegas, "https://a.example/1", "SuperNAP Las Vegas")
    _cite(session, reno, "https://b.example/2", "SUPERNAP Reno")
    _cite(session, reno, "https://c.example/3", "SuperNAP Tahoe Reno")
    _cite(session, reno, "https://d.example/4", "Citadel Campus")
    _cite(session, reno, "https://e.example/5", "The Citadel Campus")
    assert _listed(session) == {}


def test_a_row_leaves_the_list_once_the_other_campus_is_ruled_off_it(session):
    """`tracker logic rule-out` is the repair, and the report reads its rulings."""
    row = _campus(session)
    _cite(session, row, "https://a.example/1", "Cosner Tech Campus")
    _cite(session, row, "https://b.example/2", "Cosner Tech Campus")
    orrock = [
        _cite(session, row, "https://c.example/3", "Orrock Tech Campus", mw_planned=300.0),
        _cite(session, row, "https://d.example/4", "Orrock Tech Campus"),
    ]
    assert list(_listed(session)) == [row.id]

    # Its figure ruled out, one Orrock citation still puts a phase on the row.
    orrock[0].unconfirmed_reasons = json.dumps({"mw_planned": "misread"})
    session.flush()
    assert list(_listed(session)) == [row.id]

    # Every figure and phase ruled out: the name still stands, and mixes nothing.
    for source in orrock:
        source.unconfirmed_reasons = json.dumps({"mw_planned": "misread", "phase": "misread"})
    session.flush()
    assert _listed(session) == {}


def test_a_name_ruled_a_misreading_stops_raising_the_row(session):
    """The answer for a row that is one campus after all: rule the odd name out."""
    row = _campus(session)
    _cite(session, row, "https://a.example/1", "Cosner Tech Campus")
    _cite(session, row, "https://b.example/2", "Cosner Tech Campus")
    _cite(session, row, "https://c.example/3", "Orrock Tech Campus", ruled={"name": "misread"})
    _cite(session, row, "https://d.example/4", "Orrock Tech Campus", ruled={"name": "misread"})
    assert _listed(session) == {}


# --- the command ---------------------------------------------------------------


def _invoke(db_path, *args):
    return CliRunner().invoke(app, ["--db", str(db_path), *args])


def test_the_command_names_each_site_and_its_citations(engine, db_path):
    with session_scope(engine) as session:
        row = _spotsylvania(session)
        row_id = row.id
    done = _invoke(db_path, "duplicates", "mixed")
    assert done.exit_code == 0, done.output
    flat = " ".join(done.output.split())
    assert "1 row(s) whose citations describe more than one campus" in flat
    assert f"#{row_id}" in flat
    for site in ("Cosner Tech Campus", "Amazon Orrock Tech Campus", "Crossroads Technology"):
        assert site in flat
    assert "450 MW" in flat
    assert "tracker logic rule-out" in flat


def test_the_command_reports_json_and_an_empty_database(engine, db_path):
    empty = _invoke(db_path, "duplicates", "mixed")
    assert empty.exit_code == 0, empty.output
    assert "no row's citations describe more than one campus" in empty.output

    with session_scope(engine) as session:
        row_id = _spotsylvania(session).id
    done = _invoke(db_path, "--json", "duplicates", "mixed")
    assert done.exit_code == 0, done.output
    payload = json.loads(done.output)
    assert payload["count"] == 1
    (row,) = payload["rows"]
    assert row["project_id"] == row_id
    assert {s["label"] for s in row["sites"]} >= {"Cosner Tech Campus"}
    assert all(s["source_ids"] for s in row["sites"])
