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

import json

from tracker import parties
from tracker.models import Project, ProjectParty, Source, utcnow
from tracker.vocab import (
    COMPANY_ROLE_ORDER,
    PARTY_ROLES,
    PARTY_ROLES_BUYING,
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
