"""One company, one party: lists split, spellings of one company folded.

Measured on the production copy of 2026-10-22, Stargate Abilene (#3) showed "DPR"
and "DPR Construction" as two contractors, "Blue Owl" and "Blue Owl Capital" as two
owners, and one customer named "Oracle, OpenAI". Both rules are narrow, and the
cases they must leave alone are pinned here as firmly as the ones they fix: a party
key is what the duplicate passes compare, so a wrong fold joins two companies
everywhere at once.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select

from tracker import companies, parties
from tracker.dedup import company_key, use_person_aliases
from tracker.models import Company, Project, Source, utcnow


def _project(session, company="Crusoe", city="Abilene", state="TX"):
    row = Project(
        name=f"{company} {city}",
        company=company,
        city=city,
        state=state,
        dedup_key=f"{company_key(company)}|city:{city.lower()}|{state}",
    )
    session.add(row)
    session.flush()
    return row


def _source(session, project, entries, *, url, claims=None, published=None):
    row = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        fetched_at=utcnow(),
        published_at=published,
        parties=json.dumps(entries) if entries is not None else None,
        claims=json.dumps(claims or {}),
    )
    session.add(row)
    session.flush()
    session.refresh(project)
    return row


def _slots(project, role):
    return sorted(p.party_key for p in project.parties if p.role == role)


def _abilene(session):
    project = _project(session)
    _source(
        session,
        project,
        [
            {"name": "DPR", "role": "contractor", "quote": "DPR is the general contractor."},
            {"name": "Blue Owl", "role": "owner", "quote": "Blue Owl owns the campus."},
            {"name": "Oracle", "role": "customer", "quote": "Oracle leases the site."},
        ],
        url="https://example.test/1",
    )
    _source(
        session,
        project,
        [
            {"name": "DPR Construction", "role": "contractor", "quote": "DPR Construction builds."},
            {"name": "Blue Owl Capital", "role": "owner", "quote": "Blue Owl Capital financed it."},
            {"name": "OpenAI", "role": "customer", "quote": "OpenAI will use it."},
        ],
        url="https://example.test/2",
    )
    # A citation listing no parties: its customer claim is a list of two companies.
    _source(
        session, project, None, url="https://example.test/3", claims={"customer": "Oracle, OpenAI"}
    )
    return project


# --- the Abilene cases -----------------------------------------------------------


def test_abilene_shows_each_company_once_in_each_role(session):
    project = _abilene(session)
    parties.rebuild(session, project)

    assert _slots(project, "contractor") == ["dpr"]
    assert _slots(project, "owner") == ["blue owl"]
    # "Oracle, OpenAI" is two customers, both already named on their own.
    assert _slots(project, "customer") == ["openai", "oracle"]


def test_the_folded_party_keeps_the_best_evidence_of_either_spelling(session):
    project = _abilene(session)
    parties.rebuild(session, project)
    (dpr,) = [p for p in project.parties if p.role == "contractor"]
    assert dpr.unconfirmed is None
    assert dpr.quote in {"DPR is the general contractor.", "DPR Construction builds."}


def test_splitting_and_folding_is_idempotent(session):
    project = _abilene(session)
    assert parties.rebuild(session, project) > 0
    assert parties.rebuild(session, project) == 0


def test_a_list_is_split_on_what_the_registry_knows(session):
    """No standalone mention on this campus: the parts are companies other rows name."""
    _project(session, company="Prologis", city="Elsewhere")
    _project(session, company="Skybox Data Centers", city="Dallas")
    project = _project(session, company="Prologis", city="Plano")
    _source(
        session,
        project,
        [{"name": "Prologis and Skybox Data Centers", "role": "operator", "quote": "q"}],
        url="https://example.test/p",
    )
    parties.forget_known_keys(session)
    parties.rebuild(session, project)
    assert _slots(project, "operator") == ["prologis", "skybox"]
    assert {p.quote for p in project.parties} == {"q"}


def test_the_parts_of_a_list_are_dated_under_their_own_keys(session):
    project = _abilene(session)
    sources = sorted(project.sources, key=lambda s: s.url)
    sources[2].published_at = dt.datetime(2025, 1, 2)  # the "Oracle, OpenAI" claim
    sources[1].published_at = dt.datetime(2025, 6, 1)  # "DPR Construction"
    session.flush()
    got = parties.first_cited(sources)
    assert got["oracle"] == dt.date(2025, 1, 2)
    assert got["openai"] == dt.date(2025, 1, 2)
    # Folded: DPR Construction's earlier date is DPR's.
    assert got["dpr"] == dt.date(2025, 6, 1)
    assert "dpr construction" not in got


# --- what must not be split ------------------------------------------------------


def test_one_company_whose_name_holds_a_separator_is_not_split():
    known = {company_key(n) for n in ("Johnson", "Oracle", "OpenAI", "Prime Data Centers")}
    # One key twice, however well known: Johnson & Johnson is not "Johnson".
    assert parties.split_names("Johnson & Johnson", known) == ["Johnson & Johnson"]
    # No part is a company anybody names.
    for name in ("Black & Veatch", "Burns & McDonnell", "Procter & Gamble"):
        assert parties.split_names(name, known) == [name]
    # A single letter is never a company.
    assert parties.split_names("AT&T", known | {"at"}) == ["AT&T"]
    # A legal form after a comma belongs to the name before it.
    assert parties.split_names("Oracle, Inc.", known) == ["Oracle, Inc."]
    # Every part has to be known, not just the first.
    assert parties.split_names("Prime Group and Capri Investment Group", known) == [
        "Prime Group and Capri Investment Group"
    ]


def test_the_listed_separators_all_split():
    known = {"oracle", "openai", "softbank"}
    for name in ("Oracle, OpenAI", "Oracle and OpenAI", "Oracle & OpenAI", "Oracle / OpenAI"):
        assert parties.split_names(name, known) == ["Oracle", "OpenAI"]
    assert parties.split_names("OpenAI / SoftBank / Oracle", known) == [
        "OpenAI",
        "SoftBank",
        "Oracle",
    ]


# --- what must not be folded ------------------------------------------------------


def test_different_companies_sharing_a_first_word_are_not_folded():
    assert not parties.same_company_names("Applied Digital", "Applied Partners LLC")
    assert not parties.same_company_names("Prime Data Centers", "Prime Group")
    # A word that can be the name is not a corporate suffix: Siemens Energy is not Siemens.
    assert not parties.same_company_names("Siemens", "Siemens Energy")
    assert not parties.same_company_names("Entergy", "Entergy Louisiana")


def test_corporate_suffixes_alone_fold():
    assert parties.same_company_names("DPR", "DPR Construction")
    assert parties.same_company_names("Blue Owl Capital", "Blue Owl")
    assert parties.same_company_names("Turner", "Turner Construction Company")
    assert parties.same_company_names("Cerberus", "Cerberus Capital Management")


def test_prime_group_is_not_prime_data_centers():
    """A legal suffix ("Group") made the key read Prime Group as Prime Data Centers."""
    assert company_key("Prime Group") != company_key("Prime Data Centers")
    assert company_key("Prime Group LLC") == company_key("Prime Group")


def test_two_companies_on_one_campus_in_one_role_stay_two(session):
    project = _project(session)
    _source(
        session,
        project,
        [
            {"name": "Applied Digital", "role": "developer", "quote": "a"},
            {"name": "Applied Partners LLC", "role": "developer", "quote": "b"},
            {"name": "Prime Data Centers", "role": "owner", "quote": "c"},
            {"name": "Prime Group", "role": "owner", "quote": "d"},
        ],
        url="https://example.test/x",
    )
    parties.rebuild(session, project)
    assert len(_slots(project, "developer")) == 2
    assert len(_slots(project, "owner")) == 2


def test_the_fold_is_scoped_to_one_role():
    rows = parties.parties_by_key(
        [
            type(
                "S",
                (),
                {
                    "id": 1,
                    "source_type": "trade_press",
                    "parties": json.dumps(
                        [
                            {"name": "Blue Owl", "role": "owner", "quote": "a"},
                            {"name": "Blue Owl Capital", "role": "customer", "quote": "b"},
                        ]
                    ),
                },
            )()
        ]
    )
    assert set(rows) == {("blue owl", "owner"), ("blue owl capital", "customer")}


def test_an_alias_decides_which_company_a_folded_pair_is():
    """The roster says DuPont Fabros Technology is Digital Realty; so is plain DuPont Fabros."""
    assert company_key("DuPont Fabros Technology") == "digital realty"
    rows = parties.parties_by_key(
        [
            type(
                "S",
                (),
                {
                    "id": 1,
                    "source_type": "trade_press",
                    "parties": json.dumps(
                        [
                            {"name": "DuPont Fabros", "role": "developer", "quote": "a"},
                            {"name": "DuPont Fabros Technology", "role": "developer", "quote": "b"},
                        ]
                    ),
                },
            )()
        ]
    )
    assert set(rows) == {("digital realty", "developer")}


def test_two_spellings_aliased_to_different_companies_are_never_joined():
    use_person_aliases({"acme": "alpha", "acme construction": "beta"})
    rows = parties.parties_by_key(
        [
            type(
                "S",
                (),
                {
                    "id": 1,
                    "source_type": "trade_press",
                    "parties": json.dumps(
                        [
                            {"name": "Acme", "role": "contractor", "quote": "a"},
                            {"name": "Acme Construction", "role": "contractor", "quote": "b"},
                        ]
                    ),
                },
            )()
        ]
    )
    assert set(rows) == {("alpha", "contractor"), ("beta", "contractor")}


# --- what a reader sees --------------------------------------------------------------


def test_the_page_prints_each_party_under_the_registrys_name(session):
    from tracker.webui.dataset import project_payload

    project = _abilene(session)
    parties.rebuild(session, project)
    companies.sync(session)
    blue_owl = session.scalar(select(Company).where(Company.key == "blue owl"))
    companies.rename(session, blue_owl, "Blue Owl (registry)", by="test")
    (stored,) = [p.name for p in project.parties if p.role == "owner"]

    payload = project_payload(project)
    (owner,) = [p for p in payload["parties"] if p["role"] == "owner"]
    assert owner["name"] == "Blue Owl (registry)"
    # The source's own words ride along for the popover.
    assert owner["spelling"] == stored
    customers = sorted(p["name"] for p in payload["parties"] if p["role"] == "customer")
    assert customers == ["OpenAI", "Oracle"]


# --- joint-venture company strings -----------------------------------------------------


def test_a_joint_venture_is_filed_under_its_partner_only_where_measured_safe():
    assert company_key("Yondr Group and Cerberus") == "yondr"
    # Not by rule: this one would take the key of EdgeConneX's own, different, Atlanta row.
    assert company_key("EdgeConneX and TA Realty") != company_key("EdgeConneX")
    assert company_key("Prime Group and Capri Investment Group") != company_key(
        "Prime Data Centers"
    )
