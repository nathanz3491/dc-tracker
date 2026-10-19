"""Every name a campus goes by reaches the one row.

The 2026-10-19 night created 96 rows and about 16 of them were campuses the database
already held — 21% of the capacity it added. The causes were all identity, not
judgement: a company spelled a second way ("Novva Data Centers", "Amazon (AWS)",
"RagingWire"), a row whose key described a county it had since been placed in a
town of, an arbiter's routing nobody remembered, and a distance rule reading two
towns' centres as two sites. Each test below pins one of those shut.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from tracker import keys
from tracker.dedup import company_key, dedup_key, same_operator
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project, ProjectAlias, Source


def _row(session, company, *, city=None, county=None, state="TX", name=None, key=None, **kw):
    project = Project(
        name=name or f"{company} {city or county}",
        company=company,
        city=city,
        county=county,
        state=state,
        dedup_key=key or dedup_key(company, city, county, state),
        phase=kw.pop("phase", "announced"),
        **kw,
    )
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url, *, extractor=None, claims=None):
    session.add(
        Source(
            project_id=project.id,
            url=url,
            source_type="trade_press",
            fetched_at=dt.datetime(2026, 10, 1),
            extractor=extractor,
            claims=json.dumps(claims) if claims is not None else None,
        )
    )
    session.flush()


def _aliases(session) -> dict[str, int]:
    return {a.from_dedup_key: a.to_project_id for a in session.scalars(select(ProjectAlias))}


# --- one company, one key --------------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "key"),
    [
        ("Amazon (AWS)", "amazon"),
        ("Prologis, L.P.", "prologis"),
        ("Amazon.com, Inc.", "amazon"),
        ("Compass Datacenters", "compass"),
        ("Novva Data Centers", "novva"),
        ("EdgeCore Digital Infrastructure", "edgecore"),
        ("CoreSite (American Tower)", "coresite"),
        # The roster's renames and acquisitions, which only the coverage report read.
        ("RagingWire Data Centers", "ntt"),
        ("NTT Global Data Centers Americas", "ntt"),
        ("DuPont Fabros Technology", "digital realty"),
        ("Crusoe Energy", "crusoe"),
        ("Cyxtera Technologies", "centersquare"),
    ],
)
def test_a_known_company_has_one_key_however_it_is_spelled(spelling, key):
    assert company_key(spelling) == key


@pytest.mark.parametrize(
    "name", ["Digital Realty", "Applied Digital", "X.AI", "Data Centers Inc", "Meta"]
)
def test_the_new_rules_leave_a_name_that_is_the_company_alone(name):
    """Only trailing industry words go: "Digital Realty" keeps the word that is its
    name, ".ai" is part of xAI's, and a name made only of the words survives."""
    assert (
        company_key(name)
        == {
            "Digital Realty": "digital realty",
            "Applied Digital": "applied digital",
            "X.AI": "xai",
            "Data Centers Inc": "data centers",
            "Meta": "meta",
        }[name]
    )


def test_one_operator_at_two_lengths_is_evidence_and_never_a_key():
    """A holding company named for the operator plus the site is the operator; a bare
    prefix of an unrelated name is not."""
    assert same_operator("AMP Z Champion Data Center Holdings LLC", "Amp Z")
    assert same_operator("Novva", "Novva Data Centers")
    assert not same_operator("Copia Power", "Copia Digital")
    assert company_key("AMP Z Champion Data Center Holdings LLC") != company_key("Amp Z")


# --- every row reachable by the key it would be filed under today -----------------


def test_a_row_that_learned_its_town_is_reached_by_the_town_key(session):
    """Hyperion's shape: born under its parish, since placed in Rayville."""
    row = _row(session, "Meta", county="Richland Parish", state="LA")
    row.city = "Rayville"
    session.flush()

    report = keys.sync(session)

    assert ("meta|city:rayville|LA", row.id) in report.added
    assert _aliases(session)["meta|city:rayville|LA"] == row.id


def test_a_row_born_under_an_old_spelling_is_reached_by_the_new_key(session):
    row = _row(
        session, "RagingWire", city="Sacramento", state="CA", key="ragingwire|city:sacramento|CA"
    )
    keys.sync(session)
    assert _aliases(session)["ntt|city:sacramento|CA"] == row.id


def test_a_merge_alias_written_under_old_rules_gains_its_new_spelling(session):
    keep = _row(session, "NTT", city="Hillsboro", state="OR")
    session.add(ProjectAlias(from_dedup_key="ragingwire|city:hillsboro|OR", to_project_id=keep.id))
    session.flush()

    keys.sync(session)

    # The new key is the survivor's own, so nothing needs remembering for it...
    assert "ntt|city:hillsboro|OR" not in _aliases(session)
    # ...and an old alias whose renormalised key is free gains one.
    other = _row(session, "Vantage", city="Hillsboro", state="OR")
    session.add(
        ProjectAlias(from_dedup_key="vantage data centers|city:portland|OR", to_project_id=other.id)
    )
    session.flush()
    keys.sync(session)
    assert _aliases(session)["vantage|city:portland|OR"] == other.id


def test_a_key_another_row_holds_is_left_to_that_row(session):
    """Choosing between two rows is the duplicate passes' question, not this one's."""
    holder = _row(session, "NTT", city="Phoenix", state="AZ")
    old = _row(
        session,
        "NTT Global Data Centers Americas",
        city="Phoenix",
        state="AZ",
        key="ntt global data centers americas|city:phoenix|AZ",
    )

    report = keys.sync(session)

    assert "ntt|city:phoenix|AZ" not in _aliases(session)
    assert ("ntt|city:phoenix|AZ", old.id, holder.id) in report.held_elsewhere


def test_a_key_several_rows_share_goes_to_the_one_a_merge_would_keep(session):
    small = _row(
        session, "RagingWire", city="Sacramento", state="CA", key="ragingwire|city:sacramento|CA"
    )
    big = _row(session, "NTT Ltd.", city="Sacramento", state="CA", key="ntt ltd|city:sacramento|CA")
    for n in range(3):
        _cite(session, big, f"https://news.test/{n}")
    _cite(session, small, "https://news.test/only")

    report = keys.sync(session)

    assert _aliases(session)["ntt|city:sacramento|CA"] == big.id
    assert report.shared["ntt|city:sacramento|CA"] == sorted([small.id, big.id])


def test_sync_is_idempotent(session):
    row = _row(session, "Meta", county="Richland Parish", state="LA")
    row.city = "Rayville"
    session.flush()
    assert keys.sync(session).added
    assert keys.sync(session).added == []


def test_the_identity_checks_routing_is_remembered(session):
    """Routed at insert, then refused two hours later by a re-read with no alias to
    follow — 10-19, "poolside ai". Now the second article goes straight there."""
    from tracker.upsert import upsert_record

    stored = _row(
        session, "Microsoft", city="Mount Pleasant", county="Racine", state="WI", name="Fairwater"
    )
    asked: list[int] = []

    def arbiter(*, candidate, **_):
        asked.append(candidate.id)
        return candidate.id

    def arriving(url):
        return IngestRecord(
            project={
                "company": "Microsoft",
                "county": "Racine",
                "state": "WI",
                "name": "Racine DC",
            },
            sources=[
                SourceRecord(
                    url=url,
                    source_type="trade_press",
                    fetched_at=dt.datetime(2026, 6, 1),
                    claims={},
                )
            ],
        )

    first = upsert_record(session, arriving("https://a.test/1"), arbiter=arbiter)
    second = upsert_record(session, arriving("https://a.test/2"), arbiter=arbiter)

    assert first.project_id == second.project_id == stored.id
    assert asked == [stored.id], "the second article followed the alias, unasked"
    alias = session.scalar(
        select(ProjectAlias).where(ProjectAlias.from_dedup_key == "microsoft|county:racine|WI")
    )
    assert alias is not None and alias.decided_by == keys.ARBITER


def test_a_fold_remembers_the_folded_rows_natural_key(session):
    from tracker.merge import merge_projects

    keep = _row(session, "NTT", city="Ashburn", state="VA")
    dupe = _row(
        session, "RagingWire", city="Sterling", state="VA", key="ragingwire|city:sterling|VA"
    )
    merge_projects(session, keep.id, [dupe.id])
    aliases = _aliases(session)
    assert aliases["ragingwire|city:sterling|VA"] == keep.id
    assert aliases["ntt|city:sterling|VA"] == keep.id


# --- a town's centre is not a site -------------------------------------------------


def test_town_centres_widen_how_far_apart_one_campus_may_read(session):
    from tracker.dupresolve import FAR_APART_KM, TOWN_CENTRE_KM, distance_limit
    from tracker.ingest.geo import EXTRACTOR

    a = _row(session, "Crusoe", city="Amarillo", county="Armstrong County", lat=35.2, lon=-101.8)
    b = _row(session, "Crusoe", city="Claude", county="Armstrong County", lat=35.1, lon=-101.4)
    assert distance_limit(a, b) == FAR_APART_KM
    _cite(session, a, "https://census.test/a", extractor=EXTRACTOR, claims={"lat": 35.2})
    _cite(session, b, "https://census.test/b", extractor=EXTRACTOR, claims={"lat": 35.1})
    session.refresh(a)
    session.refresh(b)
    assert distance_limit(a, b) == FAR_APART_KM + 2 * TOWN_CENTRE_KM


def test_a_judged_twin_between_two_towns_is_no_longer_refused_for_the_distance(session):
    """Crusoe under Amarillo and under Claude: 'same' at 0.95, refused '44 km apart'."""
    from tracker.capex import DuplicatePair
    from tracker.dupresolve import evidence_blocks_merge
    from tracker.ingest.geo import EXTRACTOR

    a = _row(
        session, "Crusoe", city="Amarillo", county="Armstrong County", lat=35.222, lon=-101.831
    )
    b = _row(session, "Crusoe", city="Claude", county="Armstrong County", lat=35.112, lon=-101.364)
    for row in (a, b):
        _cite(session, row, f"https://census.test/{row.id}", extractor=EXTRACTOR, claims={"lat": 1})
        session.refresh(row)
    pair = DuplicatePair(
        a.id,
        a.company,
        a.name,
        b.id,
        b.company,
        b.name,
        "Armstrong",
        "TX",
        0.0,
        shared_keys=("crusoe|county:armstrong county|TX",),
    )
    assert evidence_blocks_merge(pair, a, b, judge_read_the_sources=True) is None


def test_one_operators_shared_project_name_may_be_folded_by_a_reading_judge(session):
    """TeraWulf's Muskie filed under Ashland and Grayson; Galaxy's Helios under Afton
    and Dickens County. A shared word is still not enough for anyone else."""
    from tracker.capex import DuplicatePair
    from tracker.dupresolve import evidence_blocks_merge

    a = _row(session, "TeraWulf", city="Ashland", state="KY", name="TeraWulf Muskie Data Campus")
    b = _row(session, "TeraWulf", city="Grayson", state="KY", name="Muskie Data campus")
    pair = DuplicatePair(
        a.id,
        a.company,
        a.name,
        b.id,
        b.company,
        b.name,
        "Ashland",
        "KY",
        0.0,
        shared_tokens=("muskie",),
    )
    assert evidence_blocks_merge(pair, a, b, judge_read_the_sources=True) is None
    assert evidence_blocks_merge(pair, a, b, judge_read_the_sources=False) is not None

    c = _row(session, "Copia Power", city="Las Vegas", state="NV", name="Bergen Data Center")
    d = _row(session, "Copia Digital", city="Henderson", state="NV", name="Bergen DC")
    other = DuplicatePair(
        c.id,
        c.company,
        c.name,
        d.id,
        d.company,
        d.name,
        "Las Vegas",
        "NV",
        0.0,
        shared_tokens=("bergen",),
    )
    assert evidence_blocks_merge(other, c, d, judge_read_the_sources=True) is not None


def test_the_twin_pass_asks_about_one_operators_rows_within_reach(session):
    """Menlo Digital under Phoenix and under Ahwatukee: nothing in common but the
    operator and twenty kilometres, which is what a twin shares."""
    from tracker.capex import twin_pairs

    old = _row(
        session,
        "Menlo Digital",
        city="Ahwatukee",
        state="AZ",
        name="Menlo hyperscale",
        lat=33.33,
        lon=-111.98,
    )
    new = _row(
        session,
        "Menlo Digital",
        city="Phoenix",
        state="AZ",
        name="Menlo Digital",
        lat=33.45,
        lon=-112.07,
    )
    _row(session, "Someone Else", city="Phoenix", state="AZ", name="Other", lat=33.45, lon=-112.07)

    pairs = twin_pairs(session, [new.id])

    assert [(p.a_id, p.b_id) for p in pairs] == [(old.id, new.id)]
    assert pairs[0].kinds == ("identity",)
    assert pairs[0].shared_keys[0].startswith("one operator")


def test_census_text_is_read_as_utf8():
    from tracker.ingest.geo import _decode

    assert _decode("Doña Ana County".encode()) == "Doña Ana County"
    assert _decode(b"Do\xf1a Ana County") == "Doña Ana County", "Latin-1 still reads"


def test_an_unplaced_twin_is_found_by_one_operators_project_name(session):
    """Helios under "Afton" (a town the Census file does not list) and under Dickens
    County: no shared place, no distance, one operator and one project name."""
    from tracker.capex import twin_pairs

    old = _row(
        session,
        "Galaxy Digital Inc.",
        county="Dickens County",
        name="Galaxy Helios Data Centers II",
    )
    new = _row(session, "Galaxy Digital", city="Afton", name="Galaxy Helios")
    _row(session, "Galaxy Digital", city="Afton", state="OK", name="Galaxy Helios Oklahoma")

    pairs = twin_pairs(session, [new.id])

    assert [(p.a_id, p.b_id) for p in pairs] == [(old.id, new.id)]
    assert pairs[0].shared_tokens == ("helios",)


def test_a_twin_pair_is_asked_once_when_both_rows_are_new(session):
    from tracker.capex import twin_pairs

    a = _row(
        session, "Novva", city="North Las Vegas", state="NV", name="Nicco Way", lat=36.2, lon=-115.1
    )
    b = _row(
        session,
        "Novva Data Centers",
        county="Clark County",
        state="NV",
        name="Novva NLV",
        lat=36.2,
        lon=-115.1,
    )
    assert len(twin_pairs(session, [a.id, b.id])) == 1
