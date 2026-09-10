"""Backfill: selection order, and the two guards against cross-facility writes.

The interesting part of this module is not the reading — it is deciding *whose*
blocks these are. Both guards here exist because the unguarded version wrote real
wrong numbers into a copy of the live database:

* `_match` once returned the only extracted project unconditionally, and a STACK
  Infrastructure article put an 80 MW "Portland Expansion" onto eight STACK rows.
* `_route` exists because a Core Scientific filing describing five campuses gave
  all six of its blocks to both the Denton row and the Dalton row, recording
  588 MW twice.

And the first attempt at `_route` — demand that every block name its own site —
was wrong in the opposite direction: it emptied Lake Mariner, whose blocks are
called "Akela" and "La Lupa". That case is pinned below, because it is the one
that a stricter rule silently breaks.
"""

from __future__ import annotations

from dataclasses import dataclass

from tracker import backfill


@dataclass
class FakeProject:
    id: int
    name: str
    city: str | None = None
    county: str | None = None


@dataclass
class FakeBlock:
    label: str
    parent: str | None = None


# --- _match: which extracted project is this row? ---------------------------


def test_a_lone_extracted_project_is_not_written_to_a_row_it_does_not_name():
    """The Portland bug. One project extracted, and it is not this campus."""
    extracted = [{"name": "STACK Portland Campus", "city": "Portland"}]
    row = FakeProject(1, "STACK Infrastructure San Jose", city="San Jose")
    assert backfill._match(extracted, row) is None


def test_the_operator_name_alone_never_carries_a_match():
    """Every one of the eight wrong rows shared "STACK Infrastructure"."""
    extracted = [{"name": "STACK Infrastructure", "city": "Portland"}]
    row = FakeProject(1, "STACK Infrastructure", city="Chicago")
    assert backfill._match(extracted, row) is None


def test_the_right_campus_is_chosen_out_of_several():
    extracted = [
        {"name": "Portland Campus", "city": "Portland"},
        {"name": "Chicago Data Center", "city": "Chicago"},
    ]
    row = FakeProject(1, "STACK Chicago Data Center", city="Chicago")
    assert backfill._match(extracted, row)["city"] == "Chicago"


def test_a_sole_cited_row_and_a_sole_extracted_project_are_paired():
    """The one exemption: ingest already decided these belong together."""
    extracted = [{"name": "Nameless Facility"}]
    row = FakeProject(1, "Lake Mariner Campus", city="Barker")
    assert backfill._match(extracted, row, sole_candidate=True) is extracted[0]
    assert backfill._match(extracted, row) is None


def test_the_exemption_does_not_apply_when_the_article_names_several_projects():
    """`sole_candidate` is about the row, but two projects still need choosing."""
    extracted = [{"name": "Alpha", "city": "Reno"}, {"name": "Beta", "city": "Mesa"}]
    row = FakeProject(1, "Gamma Campus", city="Tulsa")
    assert backfill._match(extracted, row, sole_candidate=True) is None


def test_a_partial_overlap_below_the_floor_is_refused():
    """One token of three is a coincidence, not an identification.

    Deliberately no locality on the extracted side, so the veto above cannot be
    what refuses this — the floor has to.
    """
    extracted = [{"name": "Vantage Ashburn"}]
    row = FakeProject(1, "Aligned Ashburn Reserve", city="Ashburn")
    assert backfill._match(extracted, row) is None


# --- _distinguishing: what tells sibling rows apart -------------------------


def test_what_every_sibling_shares_cannot_route_anything():
    rows = [
        FakeProject(1, "Core Scientific Denton", city="Denton"),
        FakeProject(2, "Core Scientific Dalton", city="Dalton"),
    ]
    distinct = backfill._distinguishing(rows)
    assert distinct[1] == {"denton"}
    assert distinct[2] == {"dalton"}


def test_a_single_row_keeps_all_of_its_tokens():
    """With nothing to be told apart from, nothing is shared away."""
    distinct = backfill._distinguishing([FakeProject(1, "Lake Mariner", city="Barker")])
    assert distinct[1] == {"lake", "mariner", "barker"}


# --- _route: splitting a portfolio article ----------------------------------


def test_a_portfolio_filing_sends_each_block_to_the_row_it_names():
    """The Core Scientific case: 588 MW recorded twice."""
    denton = FakeProject(1, "Core Scientific Denton", city="Denton")
    dalton = FakeProject(2, "Core Scientific Dalton", city="Dalton")
    found = [
        FakeBlock("Denton Campus"),
        FakeBlock("Dalton 1"),
        FakeBlock("Dalton 4"),
    ]
    kept, elsewhere = backfill._route(found, denton, [denton, dalton])
    assert [b.label for b in kept] == ["Denton Campus"]
    assert [b.label for b in elsewhere] == ["Dalton 1", "Dalton 4"]

    kept, elsewhere = backfill._route(found, dalton, [denton, dalton])
    assert [b.label for b in kept] == ["Dalton 1", "Dalton 4"]


def test_a_block_naming_no_row_is_dropped_from_a_portfolio_article():
    """ "Muskogee Campus" is a sixth site, not either of these two."""
    denton = FakeProject(1, "Core Scientific Denton", city="Denton")
    dalton = FakeProject(2, "Core Scientific Dalton", city="Dalton")
    found = [FakeBlock("Denton Campus"), FakeBlock("Muskogee Campus")]
    kept, elsewhere = backfill._route(found, denton, [denton, dalton])
    assert [b.label for b in kept] == ["Denton Campus"]
    assert [b.label for b in elsewhere] == ["Muskogee Campus"]


def test_ordinary_building_names_survive_a_multi_row_article():
    """Lake Mariner. A stricter rule empties this, and it is the common case.

    Nothing here tells the rows apart, so the article is not a portfolio split and
    every block stays — a building is usually named after nothing in particular.
    """
    mine = FakeProject(1, "Lake Mariner Campus", city="Barker")
    other = FakeProject(2, "Lake Mariner Campus", city="Barker")
    found = [FakeBlock("Akela (Fluidstack)"), FakeBlock("La Lupa (Core42)")]
    kept, elsewhere = backfill._route(found, mine, [mine, other])
    assert len(kept) == 2
    assert elsewhere == []


def test_unattributable_blocks_are_kept_when_no_block_names_a_row():
    """Portfolio mode is decided per article, not per block."""
    a = FakeProject(1, "Stargate", city="Abilene")
    b = FakeProject(2, "Prometheus", city="New Albany")
    found = [FakeBlock("Phase 1"), FakeBlock("Phase 2")]
    kept, elsewhere = backfill._route(found, a, [a, b])
    assert len(kept) == 2 and elsewhere == []


def test_a_generic_block_is_dropped_once_the_article_is_known_to_be_a_portfolio():
    """Measured: "Planned 600 MW Expansion" in a nine-campus article."""
    a = FakeProject(1, "Stargate", city="Abilene")
    b = FakeProject(2, "Prometheus", city="New Albany")
    found = [FakeBlock("Abilene Campus"), FakeBlock("Planned 600 MW Expansion")]
    kept, elsewhere = backfill._route(found, a, [a, b])
    assert [x.label for x in kept] == ["Abilene Campus"]
    assert [x.label for x in elsewhere] == ["Planned 600 MW Expansion"]


def test_a_single_cited_row_is_never_routed():
    """Routing splits blocks *between* rows. With one row there is nowhere to send
    them, so dropping would lose data with no beneficiary — even though one of
    these blocks does name a site and the other does not.
    """
    row = FakeProject(1, "Core Scientific Denton", city="Denton")
    found = [FakeBlock("Denton Campus"), FakeBlock("Muskogee Campus")]
    kept, elsewhere = backfill._route(found, row, [row])
    assert kept == found and elsewhere == []


def test_a_parent_can_place_a_block_its_own_label_does_not():
    """ "Phase 3" of "AZP-3" belongs where "AZP-3" does."""
    azp = FakeProject(1, "AZP-3", city="Goodyear")
    other = FakeProject(2, "Mesa Campus", city="Mesa")
    found = [FakeBlock("Phase 3", parent="AZP-3"), FakeBlock("Mesa Campus")]
    kept, _ = backfill._route(found, azp, [azp, other])
    assert [b.label for b in kept] == ["Phase 3"]


# --- selection order --------------------------------------------------------


def test_filings_outrank_press_because_that_is_where_phase_tables_live():
    filing = backfill._yield_score("company_filing", (1,), set())
    press = backfill._yield_score("trade_press", (1,), set())
    other = backfill._yield_score("blog", (1,), set())
    assert filing > press > other


def test_a_contested_project_and_a_shared_article_both_raise_the_score():
    base = backfill._yield_score("trade_press", (1,), set())
    assert backfill._yield_score("trade_press", (1,), {1}) > base
    assert backfill._yield_score("trade_press", (1, 2), set()) > base


# --- re-gating the claim envelope, for free ---------------------------------
#
# `axis_gate` is a pure function of the entry, the stored quote and the record's
# labels, and all three are already in the database. So 9,069 stored `this_site`
# labels can be rechecked without re-reading one article — where an agent doing the
# same judgement costs ~77,000 tokens a row. Measured on the live snapshot: 3,993 of
# 9,069 relabelled, 2,683 to `unnamed` and 1,310 to `block:*`.


def _enveloped(session, *, quote: str, scope: str = "this_site", label: str | None = None):
    """A project with one claim carrying a stored scope, and optionally a tranche."""
    import datetime as dt
    import json

    from tracker.models import CapacityBlock, Project, Source

    project = Project(
        name="Digital Ashburn Campus",
        company="Digital Realty",
        city="Ashburn",
        state="VA",
        dedup_key="digital realty|city:ashburn|VA",
        phase="construction",
    )
    session.add(project)
    session.flush()
    if label:
        session.add(
            CapacityBlock(
                project_id=project.id,
                label=label,
                block_key=label.lower().replace(" ", "-"),
                mw=19.2,
            )
        )
    source = Source(
        project_id=project.id,
        url="https://example.test/ashburn",
        source_type="trade_press",
        fetched_at=dt.datetime(2026, 1, 1),
        claims=json.dumps({"mw_planned": 19.2}),
        quotes=json.dumps({"mw_planned": quote}),
        claim_meta=json.dumps({"mw_planned": {"scope": scope}}),
    )
    session.add(source)
    session.flush()
    return project, source


def _scope_of(source, field="mw_planned"):
    import json

    return json.loads(source.claim_meta)[field]["scope"]


def test_a_this_site_label_the_sentence_does_not_support_is_relabelled(session):
    from tracker.backfill import regate_scope

    _project, source = _enveloped(
        session, quote="the buildout is expected to cost in the $10 billion range"
    )

    report = regate_scope(session, apply=True)

    assert report.changed == 1
    assert _scope_of(source) == "unnamed"


def test_a_sentence_naming_a_tranche_is_relabelled_to_that_tranche(session):
    """The #14 correction, done for free: source 2790's 19.2 MW is Building K's."""
    from tracker.backfill import regate_scope

    _project, source = _enveloped(
        session,
        quote="The new Building K itself is rated at 19.2 MW.",
        label="Building K",
    )

    report = regate_scope(session, apply=True)

    assert report.changed == 1
    assert _scope_of(source) == "block:building k"


def test_a_licensed_this_site_label_is_left_alone(session):
    from tracker.backfill import regate_scope

    _project, source = _enveloped(
        session, quote="the Ashburn campus will draw 19.2 megawatts at full build"
    )

    report = regate_scope(session, apply=True)

    assert report.changed == 0
    assert _scope_of(source) == "this_site"


def test_a_scope_that_was_already_licensed_is_never_touched(session):
    """`region` and `programme` were checked against wording when written, and the
    gate has not changed for them. Only `this_site` was unrefusable."""
    from tracker.backfill import regate_scope

    _project, source = _enveloped(
        session,
        quote="will bring more than $50B of investment to the region",
        scope="region",
    )

    report = regate_scope(session, apply=True)

    assert report.claims == 0, "a licensed scope was re-gated"
    assert _scope_of(source) == "region"


def test_without_apply_nothing_is_written(session):
    from tracker.backfill import regate_scope

    _project, source = _enveloped(
        session, quote="the buildout is expected to cost in the $10 billion range"
    )

    report = regate_scope(session, apply=False)

    assert report.changed == 1, "the report must still say what it would do"
    assert _scope_of(source) == "this_site", "a dry run wrote to the database"


# --- seeding the party table ------------------------------------------------
#
# Migration 0023 added the axis and wrote no rows, because the roles live in
# article text. This closes the gap using the two columns that already exist,
# and the interesting question is what it refuses to claim.


def _sourced_project(session, *, company="Crusoe", customer=None, quote=None):
    import datetime as dt
    import json

    from tracker.models import Project, Source

    project = Project(
        name="Stargate Abilene",
        company=company,
        city="Abilene",
        state="TX",
        dedup_key=f"{company.lower()}|city:abilene|TX",
        customer=customer,
    )
    session.add(project)
    session.flush()

    claims = {"company": company}
    quotes = {}
    if customer:
        claims["customer"] = customer
    if quote:
        quotes["company"] = quote
        if customer:
            quotes["customer"] = quote
    source = Source(
        project_id=project.id,
        url="https://example.test/abilene",
        source_type="trade_press",
        fetched_at=dt.datetime(2026, 1, 1),
        claims=json.dumps(claims),
        fields=",".join(sorted(claims)) if quote else None,
        quotes=json.dumps(quotes) if quotes else None,
    )
    session.add(source)
    session.flush()
    session.refresh(project)
    return project, source


def test_seeding_writes_the_source_column_not_the_table(session):
    """The trap the wholesale rebuild sets.

    `project_party` is a cache of `source.parties`, so rows written straight into
    the table are deleted by the next `recompute_parties` — silently, and only on
    rows re-derived since, which is the worst shape a bug can have.
    """
    import json

    from tracker.backfill import seed_parties

    project, source = _sourced_project(
        session, customer="OpenAI", quote="Crusoe is building it for OpenAI."
    )

    report = seed_parties(session, apply=True)

    assert report.sources == 1
    assert source.parties is not None
    got = {e["role"]: e for e in json.loads(source.parties)}
    assert set(got) == {"operator", "customer"}
    assert got["operator"]["name"] == "Crusoe"
    assert got["customer"]["name"] == "OpenAI"

    # And it survives the rebuild, which is the whole point of writing it there.
    from tracker.upsert import recompute_parties

    recompute_parties(session)
    session.refresh(project)
    assert {(p.party_key, p.role) for p in project.parties} == {
        ("crusoe", "operator"),
        ("openai", "customer"),
    }


def test_seeding_claims_operator_and_customer_and_invents_nothing_else(session):
    """The half of `company` that can be defended, and no more.

    `company` means "who builds AND operates the site". `operator` is what every
    reader has taken it for and what `capex.attribute` already treated it as.
    `developer`, `owner` and `utility` are recoverable only from the article text,
    so guessing one would manufacture a claim no source made.
    """
    from tracker.backfill import seed_parties

    _sourced_project(session, customer="OpenAI", quote="Crusoe is building it for OpenAI.")

    report = seed_parties(session, apply=True)

    assert set(report.written) == {"operator", "customer"}


def test_seeding_marks_an_unquoted_column_as_unconfirmed(session):
    """A value with no verified sentence must not be written as a quoted one."""
    import json

    from tracker.backfill import seed_parties

    _project, source = _sourced_project(session, customer="OpenAI", quote=None)

    seed_parties(session, apply=True)

    for entry in json.loads(source.parties):
        assert entry.get("unconfirmed") == "no_quote"
        assert "quote" not in entry


def test_seeding_writes_nothing_without_apply(session):
    from tracker.backfill import seed_parties

    _project, source = _sourced_project(session, quote="Crusoe is building it.")

    report = seed_parties(session, apply=False)

    assert report.sources == 1
    assert source.parties is None


def test_seeding_leaves_a_project_that_already_has_parties_alone(session):
    """A crawl's parties are richer than anything two columns can seed."""
    import json

    from tracker.backfill import seed_parties

    _project, source = _sourced_project(session, quote="Crusoe is building it.")
    source.parties = json.dumps([{"name": "Oracle", "role": "owner", "quote": "Oracle owns it."}])
    session.flush()

    report = seed_parties(session, apply=True)

    assert report.already == 1
    assert json.loads(source.parties)[0]["name"] == "Oracle"


def test_seeding_is_idempotent(session):
    from tracker.backfill import seed_parties

    _sourced_project(session, customer="OpenAI", quote="Crusoe is building it for OpenAI.")

    first = seed_parties(session, apply=True)
    second = seed_parties(session, apply=True)

    assert first.sources == 1
    # The second pass sees the parties it wrote and declines to redo them.
    assert second.sources == 0
    assert second.already == 1


# --- deriving the basis axis ------------------------------------------------
#
# Backfillable where 0015's axes were not, and the difference is one thing: this
# axis reads the stored *quote*, which is already on disk.


def _capacity_source(session, quote, *, mw=200.0, basis=None):
    import datetime as dt
    import json

    from tracker.models import Project, Source

    project = Project(
        name="Ashburn Campus",
        company="Digital Realty",
        city="Ashburn",
        state="VA",
        dedup_key="digital realty|city:ashburn|VA",
        mw_planned=mw,
    )
    session.add(project)
    session.flush()
    meta = {"mw_planned": {"basis": basis}} if basis else {}
    source = Source(
        project_id=project.id,
        url="https://example.test/ashburn",
        source_type="trade_press",
        fetched_at=dt.datetime(2026, 1, 1),
        claims=json.dumps({"mw_planned": mw}),
        fields="mw_planned",
        quotes=json.dumps({"mw_planned": quote}),
        claim_meta=json.dumps(meta) if meta else None,
    )
    session.add(source)
    session.flush()
    session.refresh(project)
    return project, source


def test_the_basis_is_read_out_of_the_stored_quote(session):
    import json

    from tracker.backfill import derive_basis

    project, source = _capacity_source(
        session, "The campus will draw 200 MW of critical IT load at full build."
    )

    report = derive_basis(session, apply=True)

    assert report.changed == 1
    assert json.loads(source.claim_meta)["mw_planned"]["basis"] == "it_load"
    # And the cached column follows, via the write path's own merge order.
    assert project.mw_planned_basis == "it_load"


def test_a_quote_that_says_nothing_is_counted_and_not_stored(session):
    """Not guessed from the number — that is the line migration 0015 drew — and
    not written down either.

    `unspecified` is the answer for most capacity figures, so storing it would
    attach an envelope to nearly every one and make the measured coverage of every
    *other* axis look near-total. That measurement is what decides whether an axis
    survives at all, so it must not be gamed. The share stays visible in the
    report instead.

    Nothing is lost: a quoted capacity claim with no stored basis means "the
    sentence was read and did not say", because both this path and the ingest path
    evaluate every one of them.
    """
    from tracker.backfill import derive_basis

    _project, source = _capacity_source(session, "Meta is building a 200 MW campus in Ohio.")

    report = derive_basis(session, apply=True)

    assert report.found.get("unspecified") == 1
    assert source.claim_meta in (None, "{}")


def test_a_default_basis_an_earlier_run_stored_is_cleared(session):
    """So the two paths cannot disagree permanently about one claim."""
    import json

    from tracker.backfill import derive_basis

    _project, source = _capacity_source(
        session, "Meta is building a 200 MW campus in Ohio.", basis="unspecified"
    )

    derive_basis(session, apply=True)

    assert "basis" not in json.loads(source.claim_meta or "{}").get("mw_planned", {})


def test_deriving_the_basis_writes_nothing_without_apply(session):
    from tracker.backfill import derive_basis

    _project, source = _capacity_source(session, "200 MW of critical IT load.")

    report = derive_basis(session, apply=False)

    assert report.changed == 1
    assert source.claim_meta is None


def test_deriving_the_basis_is_idempotent(session):
    from tracker.backfill import derive_basis

    _capacity_source(session, "The site's nameplate capacity is 200 MW.")

    first = derive_basis(session, apply=True)
    second = derive_basis(session, apply=True)

    assert first.changed == 1
    assert second.changed == 0


def test_deriving_the_basis_leaves_the_other_axes_alone(session):
    """It writes one key into an envelope that other axes share."""
    import json

    from tracker.backfill import derive_basis

    _project, source = _capacity_source(session, "200 MW of critical IT load.")
    source.claim_meta = json.dumps({"mw_planned": {"scope": "this_site", "bound": "at_least"}})
    session.flush()

    derive_basis(session, apply=True)

    entry = json.loads(source.claim_meta)["mw_planned"]
    assert entry["scope"] == "this_site"
    assert entry["bound"] == "at_least"
    assert entry["basis"] == "it_load"
