"""A read-only sweep works each row's answers out once; a writing path never reuses one."""

from __future__ import annotations

import json

from tracker import sweep
from tracker.models import Project, Source
from tracker.upsert import claims_by_field


def _source(mw: float) -> Source:
    return Source(
        url=f"https://example.com/{mw}",
        source_type="press_release",
        claims=json.dumps({"mw_planned": mw}),
        quotes=json.dumps({"mw_planned": f"a {mw} MW campus"}),
        fields="mw_planned",
    )


def test_outside_a_sweep_every_call_works_the_answer_out_again():
    """The merge recomputes a row it has just changed: it must see the change."""
    sources = [_source(100.0)]
    first = claims_by_field(sources)
    sources[0].claims = json.dumps({"mw_planned": 250.0})
    assert first["mw_planned"][0].value == 100.0
    assert claims_by_field(sources)["mw_planned"][0].value == 250.0


def test_inside_a_sweep_one_list_of_citations_is_worked_out_once():
    sources = [_source(100.0)]
    calls = []
    with sweep.memo():
        sweep.remembered("probe", (sources,), lambda: calls.append(1) or "answer")
        assert sweep.remembered("probe", (sources,), lambda: calls.append(1) or "other") == "answer"
        first = claims_by_field(sources)
        sources[0].claims = json.dumps({"mw_planned": 250.0})  # never happens mid-sweep
        assert claims_by_field(sources)["mw_planned"][0].value == 100.0, "kept for the block"
    assert calls == [1]
    assert claims_by_field(sources)["mw_planned"][0].value == 250.0, "and only for the block"
    assert first["mw_planned"][0].value == 100.0


def test_each_asker_gets_its_own_copy():
    sources = [_source(100.0)]
    with sweep.memo():
        mine = claims_by_field(sources)
        mine["mw_planned"].clear()
        mine["invented"] = []
        theirs = claims_by_field(sources)
    assert theirs["mw_planned"] and "invented" not in theirs


def test_different_citations_are_different_questions():
    a, b = [_source(100.0)], [_source(300.0)]
    with sweep.memo():
        assert claims_by_field(a)["mw_planned"][0].value == 100.0
        assert claims_by_field(b)["mw_planned"][0].value == 300.0


def test_a_rows_rule_check_is_kept_for_the_sweep_only():
    from tracker import logic

    project = Project(
        id=1, name="Campus", company="Co", state="TX", phase="operational", mw_planned=15.0
    )
    project.mw_built = 18.0
    with sweep.memo():
        inside = logic.check_rules(project)
        assert [f.code for f in inside] == ["built_exceeds_planned"]
        project.mw_built = 10.0
        assert logic.check_rules(project) == inside, "kept for the block"
        logic.check_rules(project).clear()
        assert logic.check_rules(project) == inside, "a caller's copy is its own"
    assert logic.check_rules(project) == [], "outside it, the row as it is now"


def test_nested_blocks_share_the_outer_memory():
    sources = [_source(100.0)]
    with sweep.memo():
        with sweep.memo():
            claims_by_field(sources)
        sources[0].claims = json.dumps({"mw_planned": 250.0})
        assert claims_by_field(sources)["mw_planned"][0].value == 100.0
