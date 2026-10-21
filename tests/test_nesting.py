"""Ruling how a campus's tranches nest: the nightly review, and a person's hand.

Batch 4.2 of the client plan (2026-10-21). What arithmetic settles is derived in
`blocks.structure` and tested there; these pin the judgement half — that a model's
answer is checked before a word of it is written, that a person's ruling is never
overwritten, and that a campus left unsettled is not paid for twice.
"""

from __future__ import annotations

import json

import pytest

from tracker import declines, nesting
from tracker.models import BlockNesting, CapacityBlock, Project


def _campus(session, name="Sweetwater", **kwargs) -> Project:
    defaults = {
        "name": name,
        "company": "IREN",
        "state": "TX",
        "city": "Sweetwater",
        "dedup_key": f"k-{name}",
        "phase": "construction",
        "confidence": 2,
        "mw_planned": 2000.0,
    }
    defaults.update(kwargs)
    project = Project(**defaults)
    session.add(project)
    session.flush()
    return project


def _tranche(session, project, key, label, mw, **kwargs) -> CapacityBlock:
    block = CapacityBlock(
        project_id=project.id,
        block_key=key,
        label=label,
        mw=mw,
        status=kwargs.pop("status", "planned"),
        generic=False,
        **kwargs,
    )
    session.add(block)
    session.flush()
    return block


@pytest.fixture
def sweetwater(session):
    """IREN's Sweetwater: the hub, its two phases, and the Childress campus besides."""
    project = _campus(session)
    _tranche(session, project, "hub.sweetwater", "Sweetwater Hub", 2000.0)
    _tranche(session, project, "sweetwater-1", "Sweetwater 1", 1400.0, status="energized")
    _tranche(session, project, "childress", "Childress", 750.0)
    _tranche(session, project, "sweetwater-2", "Sweetwater 2", 600.0)
    session.refresh(project)
    return project


class _Model:
    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.prompts: list[str] = []

    def complete(self, *, system, user, max_tokens):
        self.prompts.append(user)

        class R:
            text = self.reply
            model = "test-model"

        return R()


def _ids(project) -> dict[str, str]:
    return {b.block_key: tid for tid, b in nesting.ids(project).items()}


def _answer(project, *rulings) -> dict:
    t = _ids(project)
    out = []
    for key, relation, target, confidence in rulings:
        item = {"id": t[key], "relation": relation, "confidence": confidence, "reason": "quoted"}
        if target:
            item["of"] = t[target]
        out.append(item)
    return {"tranches": out}


def test_the_campus_is_open_before_and_settled_by_a_good_answer(session, sweetwater):
    (only,) = nesting.candidates(session)
    assert only[0].id == sweetwater.id and only[1].overlap == 2750.0

    model = _Model(
        _answer(
            sweetwater,
            ("childress", "elsewhere", None, 0.9),
            ("sweetwater-1", "part_of", "hub.sweetwater", 0.85),
            ("sweetwater-2", "part_of", "hub.sweetwater", 0.85),
        )
    )
    (outcome,) = nesting.run(session, [only], extractor=model)

    assert outcome.result == "settled" and outcome.written == 3
    assert "Childress" in model.prompts[0] and "2000 MW planned" in model.prompts[0]
    rows = {r.block_key: (r.relation, r.target_key, r.decided_by) for r in sweetwater.nesting}
    assert rows["childress"] == ("elsewhere", None, "model")
    assert nesting.candidates(session) == []


def test_a_ruling_is_checked_before_a_word_of_it_is_written(session, sweetwater):
    t = _ids(sweetwater)
    found = nesting.parse(
        sweetwater,
        {
            "tranches": [
                {"id": "T99", "relation": "elsewhere", "confidence": 0.9},
                {"id": t["childress"], "relation": "moved", "confidence": 0.9},
                {"id": t["sweetwater-1"], "relation": "part_of", "confidence": 0.9},
                {
                    "id": t["sweetwater-2"],
                    "relation": "same_as",
                    "of": t["sweetwater-2"],
                    "confidence": 0.9,
                },
                {"id": t["hub.sweetwater"], "relation": "separate", "confidence": 0.4},
            ]
        },
    )
    assert found.proposals == []
    assert [why for _, why in found.refused] == [
        "no such tranche",
        "'moved' is not a relation",
        "names no tranche it is part of or the same as",
        "a tranche cannot relate to itself",
        "confidence 0.40 under 0.65",
    ]


def test_a_person_s_ruling_is_never_overwritten_by_the_review(session, sweetwater):
    nesting.rule(session, sweetwater, "childress", "separate", by="nathan", reason="IREN says so")
    session.refresh(sweetwater)
    model = _Model(_answer(sweetwater, ("childress", "elsewhere", None, 0.95)))
    (outcome,) = nesting.run(session, [(sweetwater, nesting.stake(sweetwater))], extractor=model)

    assert outcome.written == 0
    assert outcome.refused == [("T3 elsewhere", "a person ruled on it")]
    (row,) = sweetwater.nesting
    assert (row.relation, row.decided_by) == ("separate", "nathan")
    assert "PERSON ruled: separate" in model.prompts[0]


def test_a_review_replaces_the_model_s_earlier_rulings_on_the_campus(session, sweetwater):
    first = _Model(_answer(sweetwater, ("childress", "superseded", None, 0.9)))
    nesting.run(session, [(sweetwater, nesting.stake(sweetwater))], extractor=first, write=True)
    second = _Model(_answer(sweetwater, ("childress", "elsewhere", None, 0.9)))
    nesting.run(session, [(sweetwater, nesting.stake(sweetwater))], extractor=second)
    session.refresh(sweetwater)
    assert [(r.block_key, r.relation) for r in sweetwater.nesting] == [("childress", "elsewhere")]


def test_a_loop_the_model_proposes_is_broken_in_the_order_it_gave(session, sweetwater):
    model = _Model(
        _answer(
            sweetwater,
            ("sweetwater-1", "part_of", "sweetwater-2", 0.9),
            ("sweetwater-2", "part_of", "sweetwater-1", 0.9),
        )
    )
    (outcome,) = nesting.run(session, [(sweetwater, nesting.stake(sweetwater))], extractor=model)
    assert outcome.written == 1
    assert ("sweetwater-2", "would make a tranche part of itself") in outcome.refused


def test_a_dry_run_measures_the_answer_and_writes_nothing(session, sweetwater):
    model = _Model(_answer(sweetwater, ("childress", "elsewhere", None, 0.9)))
    (outcome,) = nesting.run(
        session, [(sweetwater, nesting.stake(sweetwater))], extractor=model, write=False
    )
    # With Childress elsewhere, the two phases fit inside the hub and the automatic
    # rule nests them: one ruling settles the campus.
    assert outcome.after.settled and outcome.result == "settled"
    session.refresh(sweetwater)
    assert sweetwater.nesting == []
    assert declines.load(session, nesting.KIND) == {}


def test_a_campus_left_open_is_not_reviewed_again_on_the_same_tranches(session, sweetwater):
    model = _Model({"tranches": []})
    pairs = nesting.candidates(session)
    (outcome,) = nesting.run(session, pairs, extractor=model)
    assert outcome.result == "unchanged"

    kept, held = nesting.fresh(session, nesting.candidates(session))
    assert (kept, held) == ([], 1)
    kept, held = nesting.fresh(session, nesting.candidates(session), again=True)
    assert len(kept) == 1


def test_an_unusable_reply_writes_nothing(session, sweetwater):
    (outcome,) = nesting.run(
        session, [(sweetwater, nesting.stake(sweetwater))], extractor=_Model("not json")
    )
    assert outcome.result == "error" and sweetwater.nesting == []


def test_a_person_rules_and_takes_it_back(session, sweetwater):
    row = nesting.rule(
        session, sweetwater, "sweetwater-2", "part_of", "hub.sweetwater", by="nathan"
    )
    assert (row.relation, row.target_key, row.confidence) == ("part_of", "hub.sweetwater", None)
    assert nesting.undo(session, sweetwater, "sweetwater-2") is True
    assert nesting.undo(session, sweetwater, "sweetwater-2") is False
    assert session.query(BlockNesting).count() == 0


@pytest.mark.parametrize(
    ("key", "relation", "target", "message"),
    [
        ("nowhere", "elsewhere", None, "has no tranche 'nowhere'"),
        ("childress", "beside", None, "relation must be one of"),
        ("childress", "part_of", None, "part_of needs the tranche it relates to"),
        ("childress", "same_as", "childress", "cannot relate to itself"),
    ],
)
def test_a_ruling_that_cannot_be_recorded_says_why(
    session, sweetwater, key, relation, target, message
):
    with pytest.raises(nesting.RulingError, match=message):
        nesting.rule(session, sweetwater, key, relation, target, by="nathan")


def test_a_person_cannot_rule_a_tranche_part_of_its_own_part(session, sweetwater):
    nesting.rule(session, sweetwater, "sweetwater-1", "part_of", "hub.sweetwater", by="nathan")
    with pytest.raises(nesting.RulingError, match="part of itself"):
        nesting.rule(session, sweetwater, "hub.sweetwater", "part_of", "sweetwater-1", by="nathan")


def test_a_tranche_is_found_by_its_label_as_well_as_its_key(session, sweetwater):
    assert nesting.find_key(sweetwater, "childress") == "childress"
    assert nesting.find_key(sweetwater, "sweetwater hub") == "hub.sweetwater"
    assert nesting.find_key(sweetwater, "Sweetwater") is None


# --- the project page's control (admins) ---------------------------------------


@pytest.fixture
def page(tmp_path, migrated_copy):
    """A console over a copy holding Sweetwater, an admin and a reader."""
    from tests.test_user_admin import _make
    from tracker.db import open_db, session_scope

    path = migrated_copy(tmp_path / "t.db")
    _make(path, "boss@example.com", admin=True, name="Boss")
    _make(path, "reader@example.com")
    with session_scope(open_db(path, readonly=False)) as session:
        project = _campus(session)
        _tranche(session, project, "hub.sweetwater", "Sweetwater Hub", 2000.0)
        _tranche(session, project, "childress", "Childress", 750.0)
        project_id = project.id
    return path, project_id


def test_an_admin_rules_from_the_page_and_a_reader_cannot(page):
    import threading
    from http.server import ThreadingHTTPServer

    from tests.test_user_admin import call, sign_in
    from tracker.db import open_db, session_scope
    from tracker.webui.server import Console, Handler

    path, project_id = page
    console = Console(path)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        address = httpd.server_address
        body = {"project_id": project_id, "key": "childress", "relation": "elsewhere"}
        _, _, reader = sign_in(address, "reader@example.com")
        status, _, _ = call(address, "/api/admin/tranches/rule", "POST", body, cookie=reader)
        assert status == 403

        _, _, boss = sign_in(address, "boss@example.com")
        status, bad, _ = call(
            address,
            "/api/admin/tranches/rule",
            "POST",
            {**body, "relation": "part_of"},
            cookie=boss,
        )
        assert status == 400 and "needs the tranche" in bad["error"]
        status, done, _ = call(address, "/api/admin/tranches/rule", "POST", body, cookie=boss)
        assert status == 200 and done["done"] == "elsewhere"

        status, payload, _ = call(address, f"/api/project?id={project_id}", cookie=boss)
        (childress,) = [s for s in payload["project"]["sections"] if s["label"] == "Childress"]
        assert (childress["relation"], childress["ruled_by"]) == ("elsewhere", "Boss")

        status, _, _ = call(
            address,
            "/api/admin/tranches/undo",
            "POST",
            {"project_id": project_id, "key": "childress"},
            cookie=boss,
        )
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()
    with session_scope(open_db(path, readonly=True), commit=False) as session:
        assert session.query(BlockNesting).count() == 0
