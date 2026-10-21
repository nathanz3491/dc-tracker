"""A party whose role on a campus has ended: a former tenant, a sold site.

Step 4.4 of the client plan (2026-10-21). The sources keep naming a tenant after it
has gone — Cyxtera at Prime's Comstock campus, whose lease was rejected in its 2023
bankruptcy, was still credited the campus — so the ending is recorded apart from the
rebuilt party rows and copied onto them. These pin that it survives a rebuild, that
no current-buyer reader counts it, and that it can be taken back.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import capex, parties
from tracker.models import PartyTenure, Project, Source


def _campus(session, *, customer="Cyxtera") -> Project:
    project = Project(
        name="Comstock Campus",
        company="Prime Data Centers",
        customer=customer,
        state="CA",
        city="Santa Clara",
        dedup_key="prime|city:santa clara|CA",
        phase="operational",
        confidence=2,
        mw_planned=63.0,
    )
    session.add(project)
    session.flush()
    session.add(
        Source(
            project_id=project.id,
            url="https://example.test/comstock",
            source_type="trade_press",
            fetched_at=dt.datetime(2026, 1, 1),
            parties=json.dumps(
                [
                    {
                        "name": "Prime Data Centers",
                        "role": "operator",
                        "quote": "Prime Data Centers operates the Comstock campus.",
                    },
                    {
                        "name": "Cyxtera",
                        "role": "customer",
                        "quote": "Cyxtera leases the Comstock campus from Prime.",
                    },
                ]
            ),
        )
    )
    session.flush()
    session.refresh(project)
    parties.rebuild(session, project)
    return project


def test_a_tenant_still_named_is_the_buyer_until_its_role_is_ended(session):
    project = _campus(session)
    assert [p.name for p in parties.named_buyers(project.parties)] == ["Cyxtera"]
    assert capex.attribute(project)[1] == "centersquare"

    parties.end_role(
        session,
        project,
        "Cyxtera",
        "customer",
        reason="lease rejected in Cyxtera's 2023 bankruptcy",
        by="nathan",
        ended_on=dt.date(2023, 6, 1),
    )

    assert parties.named_buyers(project.parties) == []
    assert capex.attribute(project)[1] != "centersquare", "a former tenant holds no position"
    (gone,) = parties.former(project.parties)
    assert (gone.name, gone.ended_on, gone.ended_reason) == (
        "Cyxtera",
        dt.date(2023, 6, 1),
        "lease rejected in Cyxtera's 2023 bankruptcy",
    )
    assert "centersquare" not in parties.buying_keys(project)
    assert "centersquare" in parties.keys_for(project), "still evidence of which campus this is"


def test_every_surface_reads_a_former_tenant_as_no_buyer_now(session):
    """Capex, the page, a watch, the obstacle scan and the AI briefing agree."""
    from tracker import overview, watchlist
    from tracker.models import CapacityBlock, Risk
    from tracker.webui.dataset import project_payload

    project = _campus(session)
    session.add(
        Risk(
            project_id=project.id,
            category="grid_capacity",
            severity="watch",
            status="open",
            summary="Power guarantees are hard to get.",
        )
    )
    # A tranche that still names the tenant: a company page and a watch match it too.
    session.add(CapacityBlock(project_id=project.id, block_key="c", label="C", customer="Cyxtera"))
    session.flush()
    session.refresh(project)
    assert "centersquare" in capex.blocking_risks(session)
    assert {via for k, via in watchlist._project_keys(project) if k == "centersquare"} == {
        watchlist.VIA_CUSTOMER,
        watchlist.VIA_BLOCK,
    }
    before = overview.fingerprint(project)

    parties.end_role(
        session,
        project,
        "Cyxtera",
        "customer",
        reason="lease rejected",
        by="test",
        ended_on=dt.date(2023, 6, 1),
    )

    assert parties.current_customer(project) is None
    assert project.customer, "the field keeps what its sources say"
    payload = project_payload(project)
    assert payload["customer_former"]["ended_on"] == "2023-06-01"
    assert payload["buyers"] == []
    assert "centersquare" not in {k for k, _via in watchlist._project_keys(project)}
    assert "centersquare" not in capex.blocking_risks(session)
    assert "prime" in capex.blocking_risks(session), "the obstacle stays the operator's"
    assert overview.build_context(project)["customer"].startswith("none now (formerly")
    assert overview.fingerprint(project) != before, "a cached briefing is written again"


def test_a_tranche_still_naming_a_former_tenant_credits_it_nothing(session):
    from tracker.models import CapacityBlock

    project = _campus(session)
    session.add(
        CapacityBlock(
            project_id=project.id, block_key="sfo4-c", label="SFO4-C", mw=9.0, customer="Cyxtera"
        )
    )
    session.flush()
    session.refresh(project)
    assert [s.key for s in capex.block_shares(project)[0]] == ["centersquare"]

    parties.end_role(session, project, "Cyxtera", "customer", reason="gone", by="test")
    shares, rest = capex.block_shares(project)
    assert shares == [] and rest == 63.0


def test_the_project_list_marks_a_former_tenant_without_loading_every_party(session):
    from tracker.webui.dataset import index_rows

    project = _campus(session)
    parties.end_role(session, project, "Cyxtera", "customer", reason="gone", by="test")
    (row,) = [r for r in index_rows(session) if r["id"] == project.id]
    assert row["customer_former"]["reason"] == "gone"


def test_an_ended_role_survives_the_rebuild_every_reread_runs(session):
    project = _campus(session)
    parties.end_role(session, project, "Cyxtera", "customer", reason="gone", by="nathan")
    parties.rebuild(session, project)
    (row,) = [p for p in project.parties if p.role == "customer"]
    assert row.ended_reason == "gone"


def test_an_ended_role_can_be_taken_back(session):
    project = _campus(session)
    parties.end_role(session, project, "Cyxtera", "customer", reason="gone", by="nathan")
    assert parties.resume_role(session, project, "Cyxtera", "customer") is True
    assert [p.name for p in parties.named_buyers(project.parties)] == ["Cyxtera"]
    assert session.query(PartyTenure).count() == 0
    assert parties.resume_role(session, project, "Cyxtera", "customer") is False


@pytest.mark.parametrize(
    ("party", "role", "reason", "by", "message"),
    [
        ("Cyxtera", "tenant", "gone", "n", "role must be one of"),
        ("Cyxtera", "customer", "", "n", "a reason is required"),
        ("Cyxtera", "customer", "gone", "", "who decided"),
        ("Equinix", "customer", "gone", "n", "names no customer"),
    ],
)
def test_an_ending_that_cannot_be_recorded_says_why(session, party, role, reason, by, message):
    project = _campus(session)
    with pytest.raises(parties.TenureError, match=message):
        parties.end_role(session, project, party, role, reason=reason, by=by)


def test_the_export_carries_when_and_why_a_role_ended(session):
    from tracker.export import to_json_object

    project = _campus(session)
    parties.end_role(
        session, project, "Cyxtera", "customer", reason="gone", by="n", ended_on=dt.date(2023, 1, 1)
    )
    rows = {p["key"]: p for p in to_json_object(project)["parties"]}
    assert (rows["centersquare"]["ended_on"], rows["centersquare"]["ended_reason"]) == (
        "2023-01-01",
        "gone",
    )
    assert rows["prime"]["ended_reason"] is None


def test_the_commands_list_end_and_take_back_a_role(tmp_path, monkeypatch):
    """The group takes subcommands only: a bare campus number would swallow `end`."""
    from typer.testing import CliRunner

    from tracker.cli import _shared, app
    from tracker.db import init_db, session_scope

    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        pid = str(_campus(session).id)
    engine.dispose()
    runner = CliRunner()
    # A writing command holds the lock until its process exits; here each
    # invocation is a process of its own, so let go of it when it returns.
    releases: list = []
    monkeypatch.setattr(_shared.atexit, "register", releases.append)

    def run(*args: str) -> str:
        result = runner.invoke(app, ["--db", str(path), "party", *args])
        while releases:
            releases.pop()()
        assert result.exit_code == 0, result.output
        return result.output

    assert "Cyxtera" in run("list", pid) and "ended" not in run("list", pid)
    run("end", pid, "Cyxtera", "--on", "2023-06", "--reason", "lease rejected", "--by", "test")
    assert "ended 2023-06-01" in run("list", pid)
    assert "resumed" in run("resume", pid, "Cyxtera")
    assert "ended" not in run("list", pid)


def test_a_merge_carries_an_ended_role_to_the_survivor(session):
    from tracker.merge import merge_projects

    keep = _campus(session)
    dupe = Project(
        name="Comstock",
        company="Prime",
        state="CA",
        city="Santa Clara",
        dedup_key="prime|city:santa clara 2|CA",
        phase="operational",
        confidence=1,
    )
    session.add(dupe)
    session.flush()
    session.add(
        PartyTenure(
            project_id=dupe.id,
            party_key="centersquare",
            role="customer",
            reason="gone",
            decided_by="n",
        )
    )
    session.flush()
    merge_projects(session, keep.id, [dupe.id])
    (row,) = session.query(PartyTenure).all()
    assert row.project_id == keep.id
