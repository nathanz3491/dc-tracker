"""Parties: the axis `project.company` was collapsing.

Three things carry the design and all three are tested here rather than trusted.

**`project.company` must not move.** `dedup_key` is `company|locality|state`, it
is UNIQUE, and nothing recomputes it — so a `company` that changes after insert
leaves the key naming a company the row does not. The party table is allowed to
fill a null `customer` and nothing else.

**The cache contract.** Parties are rebuilt wholesale from `source.parties`, so a
second pass must change nothing. Same obligation as `blocks`, `confidence` and
`h200_equivalent`.

**A role has to be quotable.** `docs/plan-claim-envelope.md` records two axes that
rotted for want of a check — `severity`, at `watch` on every risk in the database,
and `scope`, at 96.9% `this_site`. An unchecked label does not stay neutral; it
drifts to whatever is cheapest to say.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import parties
from tracker.models import Project, ProjectParty, Source, utcnow
from tracker.vocab import (
    COMPANY_ROLE_ORDER,
    PARTY_ROLES,
    PARTY_ROLES_BUYING,
    PARTY_ROLES_SUPPLYING,
)

# --- vocabulary -------------------------------------------------------------


def test_the_roles_that_buy_are_a_subset_and_exclude_the_two_that_do_not():
    """A utility connects capacity and a contractor pours concrete."""
    assert set(PARTY_ROLES) >= PARTY_ROLES_BUYING
    assert "utility" not in PARTY_ROLES_BUYING
    assert "contractor" not in PARTY_ROLES_BUYING


def test_the_company_ladder_is_a_subset_of_the_roles_and_leads_with_operator():
    """`project.company` has always meant "who runs it"."""
    assert set(COMPANY_ROLE_ORDER) <= set(PARTY_ROLES)
    assert COMPANY_ROLE_ORDER[0] == "operator"


# --- helpers ----------------------------------------------------------------


def _project(session, **kwargs):
    row = Project(
        name=kwargs.pop("name", "Test Campus"),
        company=kwargs.pop("company", "Crusoe"),
        city=kwargs.pop("city", "Abilene"),
        state=kwargs.pop("state", "TX"),
        dedup_key=kwargs.pop("dedup_key", "crusoe|city:abilene|TX"),
        **kwargs,
    )
    session.add(row)
    session.flush()
    return row


def _source(session, project, entries, *, url="https://example.test/a", source_type="trade_press"):
    row = Source(
        project_id=project.id,
        url=url,
        source_type=source_type,
        fetched_at=utcnow(),
        parties=json.dumps(entries, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    session.refresh(project)
    return row


# --- parsing ----------------------------------------------------------------


def test_a_malformed_payload_is_read_as_no_parties_rather_than_raising():
    """A bad extraction must not fail a read of the rest of the row."""
    assert parties.parse(None) == []
    assert parties.parse("") == []
    assert parties.parse("{not json") == []
    assert parties.parse('{"name": "x"}') == []  # an object, not an array
    assert parties.parse('["a string", {"name": "Oracle"}]') == [{"name": "Oracle"}]


def test_an_entry_with_an_unknown_role_or_no_name_is_dropped():
    rows = parties.parties_by_key(
        [
            type(
                "S",
                (),
                {
                    "source_type": "trade_press",
                    "id": 1,
                    "parties": json.dumps(
                        [
                            {"name": "Oracle", "role": "landlord"},
                            {"name": "", "role": "customer"},
                            {"name": "OpenAI", "role": "customer"},
                        ]
                    ),
                },
            )()
        ]
    )
    assert list(rows) == [("openai", "customer")]


# --- merging across sources -------------------------------------------------


def _fake_source(sid, source_type, entries):
    return type(
        "S",
        (),
        {"id": sid, "source_type": source_type, "parties": json.dumps(entries)},
    )()


def test_a_confirmed_party_beats_an_unconfirmed_one_whatever_the_source_weight():
    """Same discipline as `upsert._resolve`: 待确认 is a last resort, not a rival."""
    rows = parties.parties_by_key(
        [
            _fake_source(
                1,
                "company_filing",
                [{"name": "Oracle", "role": "customer", "unconfirmed": "no_quote"}],
            ),
            _fake_source(
                2,
                "general_media",
                [{"name": "Oracle", "role": "customer", "quote": "Oracle will lease it."}],
            ),
        ]
    )
    got = rows[("oracle", "customer")]
    assert got.confirmed
    assert got.source_id == 2


def test_between_two_confirmed_parties_the_heavier_source_supplies_the_words():
    rows = parties.parties_by_key(
        [
            _fake_source(
                1, "general_media", [{"name": "ORACLE CORP", "role": "customer", "quote": "a"}]
            ),
            _fake_source(
                2, "company_filing", [{"name": "Oracle", "role": "customer", "quote": "bb"}]
            ),
        ]
    )
    assert rows[("oracle", "customer")].name == "Oracle"


def test_one_company_can_hold_two_roles():
    """Meta both owns and operates its own campuses; collapsing that loses a fact."""
    rows = parties.parties_by_key(
        [
            _fake_source(
                1,
                "trade_press",
                [
                    {"name": "Meta", "role": "owner", "quote": "Meta owns the site."},
                    {"name": "Meta", "role": "operator", "quote": "Meta will operate it."},
                ],
            )
        ]
    )
    assert set(rows) == {("meta", "owner"), ("meta", "operator")}


# --- the cache contract -----------------------------------------------------


def test_the_party_cache_is_consistent_after_a_recompute(session):
    """`recompute_parties` must be a no-op on a database already current."""
    from tracker.upsert import recompute_parties

    assert recompute_parties(session) == 0
    assert recompute_parties(session) == 0


def test_rebuild_is_idempotent_and_deletes_what_no_source_asserts(session):
    """Wholesale, like blocks: a party's absence means the description changed."""
    project = _project(session)
    source = _source(
        session,
        project,
        [
            {"name": "Oracle", "role": "customer", "quote": "Oracle will lease it."},
            {"name": "Crusoe", "role": "operator", "quote": "Crusoe will operate it."},
        ],
    )

    assert parties.rebuild(session, project) == 2
    assert parties.rebuild(session, project) == 0
    assert {(p.party_key, p.role) for p in project.parties} == {
        ("oracle", "customer"),
        ("crusoe", "operator"),
    }

    source.parties = json.dumps([{"name": "Crusoe", "role": "operator", "quote": "q"}])
    session.flush()
    assert parties.rebuild(session, project) == 2  # one updated, one deleted
    assert {p.party_key for p in project.parties} == {"crusoe"}


# --- what it may and may not write ------------------------------------------


def test_reconcile_never_touches_company_even_when_a_party_disagrees(session):
    """The guarantee that keeps `dedup_key` honest.

    `dedup_key` is computed once at insert and nothing re-keys it, so a `company`
    that moves afterwards leaves the UNIQUE index naming a company the row does
    not. `company` is FILL_ONLY upstream for the same reason.
    """
    project = _project(session, company="Crusoe")
    _source(
        session,
        project,
        [{"name": "Oracle", "role": "operator", "quote": "Oracle will operate the campus."}],
    )
    parties.rebuild(session, project)
    parties.reconcile(project)

    assert project.company == "Crusoe"
    assert project.dedup_key == "crusoe|city:abilene|TX"


def test_reconcile_fills_a_null_customer_from_a_confirmed_party(session):
    project = _project(session, customer=None)
    _source(
        session,
        project,
        [{"name": "OpenAI", "role": "customer", "quote": "OpenAI will occupy the site."}],
    )
    parties.rebuild(session, project)
    parties.reconcile(project)
    assert project.customer == "OpenAI"


def test_reconcile_will_not_fill_customer_from_an_unconfirmed_party(session):
    """待确认 means no sentence licenses the role; it must not become a fact."""
    project = _project(session, customer=None)
    _source(
        session,
        project,
        [{"name": "OpenAI", "role": "customer", "unconfirmed": "no_quote"}],
    )
    parties.rebuild(session, project)
    parties.reconcile(project)
    assert project.customer is None


def test_reconcile_never_overwrites_a_customer_that_is_already_set(session):
    project = _project(session, customer="Core42")
    _source(
        session,
        project,
        [{"name": "OpenAI", "role": "customer", "quote": "OpenAI will occupy the site."}],
    )
    parties.rebuild(session, project)
    parties.reconcile(project)
    assert project.customer == "Core42"


def test_a_project_with_no_parties_is_left_exactly_as_it_was(session):
    """What lets migration 0023 land on a live database without moving a figure."""
    project = _project(session, customer=None)
    before = (project.company, project.customer, project.dedup_key)
    assert parties.reconcile(project) == []
    assert (project.company, project.customer, project.dedup_key) == before


# --- the duplicate signal ---------------------------------------------------


def test_a_shared_party_is_found_when_each_row_names_only_one(session):
    """The 48-of-90 shape, which no comparison of company strings can reach."""
    crusoe = _project(session, company="Crusoe", dedup_key="crusoe|city:abilene|TX")
    oracle = _project(
        session,
        company="Oracle",
        name="Stargate Abilene",
        dedup_key="oracle|city:abilene|TX",
    )
    _source(
        session,
        crusoe,
        [{"name": "OpenAI", "role": "customer", "quote": "OpenAI will occupy it."}],
        url="https://example.test/one",
    )
    _source(
        session,
        oracle,
        [{"name": "OpenAI", "role": "customer", "quote": "OpenAI is the tenant."}],
        url="https://example.test/two",
    )
    parties.rebuild(session, crusoe)
    parties.rebuild(session, oracle)

    from tracker.dedup import shared_parties_across_companies

    # The string comparison sees nothing: neither company string names the other.
    assert shared_parties_across_companies(crusoe.company, oracle.company) == set()
    # The party rows do.
    assert parties.shared_across_companies(crusoe, oracle) == {"openai"}


def test_two_rows_of_one_company_share_no_party_however_many_they_hold(session):
    """The guard that stops NTT's Itasca campus folding into NTT's Chicago one.

    `dupresolve.HARD_EVIDENCE` trusts `party` for an unattended merge, and
    `capex.suspected_duplicates` has a pass that buckets rows *by* company — so
    without this every pair on that pass would carry vacuous party evidence.
    """
    a = _project(session, company="NTT", city="Itasca", state="IL", dedup_key="ntt|city:itasca|IL")
    b = _project(
        session, company="NTT", city="Chicago", state="IL", dedup_key="ntt|city:chicago|IL"
    )
    for i, row in enumerate((a, b)):
        _source(
            session,
            row,
            [{"name": "NTT", "role": "operator", "quote": "NTT will operate it."}],
            url=f"https://example.test/{i}",
        )
        parties.rebuild(session, row)

    assert parties.shared_across_companies(a, b) == set()


def test_buying_keys_leaves_out_the_utility(session):
    """Entergy Louisiana is a party on Meta's campus and buys none of it."""
    project = _project(session, company="Meta", dedup_key="meta|city:abilene|TX")
    _source(
        session,
        project,
        [
            {"name": "Meta", "role": "operator", "quote": "Meta will operate it."},
            {"name": "Entergy Louisiana", "role": "utility", "quote": "Entergy will supply it."},
        ],
    )
    parties.rebuild(session, project)
    assert parties.buying_keys(project) == {"meta"}


# --- what capex does with them ----------------------------------------------


def test_a_confirmed_customer_party_decides_the_attribution(session):
    from tracker.capex import attribute

    project = _project(session, company="Crusoe", customer=None)
    _source(
        session,
        project,
        [{"name": "OpenAI", "role": "customer", "quote": "OpenAI will occupy it."}],
    )
    parties.rebuild(session, project)
    name, key, self_built = attribute(project)
    assert (name, key, self_built) == ("OpenAI", "openai", False)


def test_an_unconfirmed_customer_party_does_not_decide_the_attribution(session):
    """A buyer's whole position must not rest on a role no sentence licenses.

    The operator here is a wholesale developer rather than an end user, so the
    ladder has nowhere else to go and `unattributed` is the honest answer — which
    is what makes this test able to see the party being refused. With an operator
    that *is* an end user (Crusoe, say) the row would correctly attribute to it by
    the rule that predates this table, and the refusal would be invisible.
    """
    from tracker.capex import attribute

    project = _project(
        session, company="Vantage", customer=None, dedup_key="vantage|city:abilene|TX"
    )
    _source(
        session,
        project,
        [{"name": "OpenAI", "role": "customer", "unconfirmed": "quote_off_target"}],
    )
    parties.rebuild(session, project)
    _name, key, _self_built = attribute(project)
    assert key == ""  # unattributed, exactly as before the party table existed


def test_attribution_is_unchanged_on_a_row_with_no_parties(session):
    """A row nothing has re-crawled must attribute exactly as it did before."""
    from tracker.capex import attribute

    project = _project(session, company="Crusoe", customer="OpenAI")
    assert attribute(project) == ("OpenAI", "openai", False)


def test_a_party_row_written_by_hand_survives_only_if_a_source_asserts_it(session):
    """The trap the wholesale rebuild sets, pinned so nobody falls into it twice.

    A party written straight into `project_party` is deleted by the next rebuild,
    because the table is a cache of `source.parties`. That is why
    `backfill.seed_parties` writes the source column instead.
    """
    project = _project(session)
    project.parties.append(ProjectParty(party_key="oracle", role="customer", name="Oracle"))
    session.flush()
    assert parties.rebuild(session, project) == 1  # the orphan is removed
    assert list(project.parties) == []


# --- a citation that lists no parties still has some -------------------------
#
# Five places create a citation and only one reads an article. Teaching four of
# them to emit parties would be four places to forget, so it is derived in one.


def _claiming_source(session, project, claims, *, url="https://example.test/claim"):
    row = Source(
        project_id=project.id,
        url=url,
        source_type="company_filing",
        fetched_at=utcnow(),
        claims=json.dumps(claims),
    )
    session.add(row)
    session.flush()
    session.refresh(project)
    return row


def test_a_company_claim_is_an_operator_claim(session):
    """What every path other than the article reader gets for free."""
    project = _project(session, company="Meta", dedup_key="meta|city:abilene|TX")
    _claiming_source(session, project, {"company": "Meta", "customer": "Meta"})
    parties.rebuild(session, project)

    got = {(p.party_key, p.role): p for p in project.parties}
    assert set(got) == {("meta", "operator"), ("meta", "customer")}


def test_an_inferred_role_carries_no_quote_and_cannot_attribute(session):
    """Nobody asserted the role, so nothing may present it as evidenced."""
    from tracker.capex import attribute

    project = _project(
        session, company="Vantage", customer=None, dedup_key="vantage|city:abilene|TX"
    )
    _claiming_source(session, project, {"company": "Vantage", "customer": "OpenAI"})
    parties.rebuild(session, project)

    tenant = parties.customer_party(list(project.parties))
    assert tenant.unconfirmed == parties.INFERRED_ROLE
    assert tenant.quote is None
    # `customer` is still NULL on the row, so the ladder has nothing to fall back
    # to and the inferred party is refused rather than deciding the buyer.
    _name, key, _self = attribute(project)
    assert key == ""


def test_an_inferred_role_still_raises_a_duplicate(session):
    """It is the name that makes two rows recognise each other, not the role."""
    a = _project(session, company="Crusoe", dedup_key="crusoe|city:abilene|TX")
    b = _project(session, company="Oracle", name="Stargate", dedup_key="oracle|city:abilene|TX")
    _claiming_source(session, a, {"company": "Crusoe", "customer": "OpenAI"}, url="https://a.test")
    _claiming_source(session, b, {"company": "Oracle", "customer": "OpenAI"}, url="https://b.test")
    for row in (a, b):
        parties.rebuild(session, row)

    assert parties.shared_across_companies(a, b) == {"openai"}


def test_a_citation_that_listed_parties_is_not_second_guessed(session):
    """It has said what it knows; deriving more would re-add what it left out."""
    project = _project(session, company="Crusoe")
    row = _source(
        session,
        project,
        [{"name": "Oracle", "role": "customer", "quote": "Oracle will lease it."}],
    )
    row.claims = json.dumps({"company": "Crusoe", "customer": "Someone Else"})
    session.flush()
    parties.rebuild(session, project)

    assert {(p.party_key, p.role) for p in project.parties} == {("oracle", "customer")}


def test_an_inferred_role_is_not_disclosed_as_a_refusal(session):
    """Every project has a company, so counting these would note every row."""
    project = _project(session, company="Meta", dedup_key="meta|city:abilene|TX")
    _claiming_source(session, project, {"company": "Meta"})
    parties.rebuild(session, project)

    assert not [n for n in parties.reconcile(project) if "待确认" in n]


def test_a_refused_role_is_still_disclosed(session):
    """The distinction the separate reason code buys."""
    project = _project(session, company="Crusoe")
    _source(
        session,
        project,
        [{"name": "Oracle", "role": "customer", "unconfirmed": "quote_off_target"}],
    )
    parties.rebuild(session, project)

    assert [n for n in parties.reconcile(project) if "待确认" in n]


def test_a_party_derived_customer_reads_as_derived_not_unconfirmed(session):
    """Observed on screen, which is why this is pinned.

    `reconcile` fills `customer` from a confirmed party, and no *claim* carries
    that value — so provenance found no winning source and reported 待确认, whose
    own wording is "a source gave this figure and we could not find a sentence
    proving it". A sentence was right there, on the party.
    """
    from tracker.gaps import DERIVED, provenance

    project = _project(session, company="Crusoe", customer=None)
    _source(
        session,
        project,
        [{"name": "Oracle", "role": "customer", "quote": "Oracle will lease the facility."}],
    )
    parties.rebuild(session, project)
    parties.reconcile(project)
    session.flush()

    got = provenance(project, "customer")
    assert project.customer == "Oracle"
    assert got.tier == DERIVED
    assert got.quote == "Oracle will lease the facility."


# --- when a party was first named --------------------------------------------


def test_a_party_is_dated_by_the_first_citation_that_named_it():
    """The project page marks a party first cited in the last thirty days as new.

    Publication date when recorded, the day it was read otherwise; inferred parties
    count, so a party is never dated later than the citation that put it on the row.
    """
    import datetime as dt

    def src(when, entries=None, *, published=None, claims=None):
        return type(
            "S",
            (),
            {
                "id": 1,
                "source_type": "trade_press",
                "fetched_at": when,
                "published_at": published,
                "parties": json.dumps(entries) if entries is not None else None,
                "claims": json.dumps(claims or {}),
            },
        )()

    got = parties.first_cited(
        [
            src(
                dt.datetime(2026, 10, 15, 9),
                [{"name": "Turner Construction", "role": "contractor"}],
            ),
            src(
                dt.datetime(2026, 10, 20, 9),
                [{"name": "Turner Construction", "role": "contractor"}],
                published=dt.datetime(2026, 3, 2, 12),
            ),
            src(dt.datetime(2026, 10, 18, 9), claims={"company": "Crusoe"}),
            src(None, [{"name": "Oracle", "role": "customer"}]),
        ]
    )
    assert got == {
        parties.party_key("Turner Construction"): dt.date(2026, 3, 2),
        parties.party_key("Crusoe"): dt.date(2026, 10, 18),
    }


def test_the_export_carries_when_each_party_was_first_named(session):
    from tracker.export import to_json_object

    project = _project(session)
    _source(
        session,
        project,
        [{"name": "Oracle", "role": "customer", "quote": "Oracle will lease the facility."}],
    )
    parties.rebuild(session, project)
    session.flush()

    (party,) = to_json_object(project)["parties"]
    assert party["name"] == "Oracle"
    assert party["first_cited"] == project.sources[0].fetched_at.date().isoformat()


# --- newly named across campuses ---------------------------------------------


def _dated(session, project, entries, *, days_ago, url, published_days_ago=None):
    """A citation read `days_ago` days ago, optionally published earlier."""
    now = utcnow()
    row = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        fetched_at=now - dt.timedelta(days=days_ago),
        published_at=(
            now - dt.timedelta(days=published_days_ago) if published_days_ago is not None else None
        ),
        parties=json.dumps(entries, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    session.refresh(project)
    return row


def _campus(session, n):
    return _project(
        session, name=f"Campus {n}", city=f"Town {n}", dedup_key=f"crusoe|city:town {n}|TX"
    )


def _named(name, role, quote=None, **extra):
    return {"name": name, "role": role, "quote": quote or f"{name} is the {role}.", **extra}


def test_the_supplying_roles_sell_to_a_campus_and_never_buy_it():
    assert set(PARTY_ROLES) >= PARTY_ROLES_SUPPLYING
    assert not PARTY_ROLES_SUPPLYING & PARTY_ROLES_BUYING


def test_a_company_named_on_several_campuses_is_one_entry(session):
    """Turner on two campuses is one line listing both, newest first, under the
    registry's one name — and each campus keeps its own sentence and source."""
    from tracker.models import Company

    a, b = _campus(session, 1), _campus(session, 2)
    _dated(session, a, [], days_ago=200, url="https://example.test/a-old")
    _dated(
        session,
        a,
        [_named("Turner Construction", "contractor")],
        days_ago=12,
        url="https://example.test/a-new",
    )
    _dated(
        session,
        b,
        [_named("Turner Construction Co.", "contractor", "Turner Construction Co. will build it.")],
        days_ago=3,
        url="https://example.test/b",
    )
    for project in (a, b):
        parties.rebuild(session, project)
    session.add(Company(key=parties.party_key("Turner Construction"), name="Turner"))
    session.flush()

    got = parties.newly_named(session, days=30)
    assert got["days"] == 30
    (turner,) = [p for p in got["parties"] if p["key"] == "turner construction"]
    assert turner["name"] == "Turner"
    assert turner["roles"] == ["contractor"] and turner["supplier"] is True
    assert [c["name"] for c in turner["campuses"]] == ["Campus 2", "Campus 1"]
    assert turner["latest"] == turner["campuses"][0]["first_cited"]
    newest, older = turner["campuses"]
    assert newest["quote"] == "Turner Construction Co. will build it."
    assert newest["url"] == "https://example.test/b"
    assert newest["new_campus"] is True, "campus 2's only citation is in the window"
    assert older["new_campus"] is False, "campus 1 was first cited 200 days ago"


def test_only_a_first_mention_inside_the_window_is_new(session):
    """Named again last week is not named for the first time, and an old article the
    crawler only just read is dated by its publication — as the project page does."""
    campus = _campus(session, 1)
    turner = _named("Turner Construction", "contractor")
    _dated(session, campus, [turner], days_ago=90, url="https://example.test/old")
    _dated(session, campus, [turner], days_ago=2, url="https://example.test/again")
    _dated(
        session,
        campus,
        [_named("Entergy Louisiana", "utility")],
        days_ago=1,
        published_days_ago=400,
        url="https://example.test/archive",
    )
    _dated(
        session,
        campus,
        [_named("Oncor", "utility")],
        days_ago=40,
        url="https://example.test/month-ago",
    )
    parties.rebuild(session, campus)

    assert parties.newly_named(session, days=30)["parties"] == []
    assert [p["name"] for p in parties.newly_named(session, days=60)["parties"]] == ["Oncor"]


def test_a_role_that_has_ended_is_not_newly_named(session):
    """A former tenant is not a new one, however recent the article naming it."""
    campus = _campus(session, 1)
    _dated(
        session,
        campus,
        [_named("Oracle", "customer"), _named("Turner Construction", "contractor")],
        days_ago=5,
        url="https://example.test/a",
    )
    parties.rebuild(session, campus)
    parties.end_role(session, campus, "Oracle", "customer", reason="lease ended", by="test")

    keys = {p["key"] for p in parties.newly_named(session)["parties"]}
    assert "oracle" not in keys
    assert "turner construction" in keys


def test_the_role_filter_and_suppliers_first(session):
    campus = _campus(session, 1)
    _dated(
        session,
        campus,
        [_named("Oracle", "customer"), _named("Turner Construction", "contractor")],
        days_ago=5,
        url="https://example.test/a",
    )
    _dated(session, campus, [_named("Oncor", "utility")], days_ago=20, url="https://example.test/b")
    parties.rebuild(session, campus)

    every = [p["name"] for p in parties.newly_named(session)["parties"]]
    # Suppliers first, newest first among them; then everybody else.
    assert every[:2] == ["Turner Construction", "Oncor"]
    assert "Oracle" in every[2:]
    only = parties.newly_named(session, roles=["customer"])["parties"]
    assert [p["name"] for p in only] == ["Oracle"]
    assert parties.newly_named(session, roles=[])["parties"] == []
    with pytest.raises(ValueError, match="unknown role"):
        parties.newly_named(session, roles=["landlord"])
    with pytest.raises(ValueError, match="at least 1"):
        parties.newly_named(session, days=0)


def test_a_refused_role_is_kept_and_says_so(session):
    """The name is in the article even where its wording did not license the role."""
    campus = _campus(session, 1)
    _dated(
        session,
        campus,
        [
            _named(
                "MUFG",
                "contractor",
                "MUFG served as administrative agent.",
                unconfirmed="quote_off_target",
            )
        ],
        days_ago=4,
        url="https://example.test/a",
    )
    parties.rebuild(session, campus)

    (mufg,) = parties.newly_named(session, roles=["contractor"])["parties"]
    assert mufg["confirmed"] is False
    assert mufg["campuses"][0]["unconfirmed"] == "quote_off_target"


def test_newly_named_is_scoped_to_campuses_and_reads_in_constant_queries(session):
    """The company page asks for its own campuses; and no campus costs a query of
    its own — the trap `capex.working_set` records."""
    from sqlalchemy import event

    from tracker.company import Names

    campuses = [_campus(session, n) for n in range(1, 5)]
    for n, campus in enumerate(campuses, 1):
        _dated(
            session,
            campus,
            [_named(f"Builder {n}", "contractor")],
            days_ago=n,
            url=f"https://example.test/{n}",
        )
        parties.rebuild(session, campus)
    session.flush()

    scoped = parties.newly_named(session, project_ids=[campuses[0].id], roles=["contractor"])
    assert [p["name"] for p in scoped["parties"]] == ["Builder 1"]
    assert parties.newly_named(session, project_ids=[])["parties"] == []

    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        one = parties.newly_named(session, project_ids=[campuses[0].id], names=Names())
        n_one = len(statements)
        statements.clear()
        four = parties.newly_named(session, names=Names())
        n_four = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(one["parties"]) < len(four["parties"])
    assert n_one == n_four == 2
