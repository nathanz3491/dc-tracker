"""What a fixed night runs once its two busy rounds are done.

It keeps enriching rows not yet asked that night while a pass pays, and re-reads a
second batch of the likeliest-wrong values in round 2: the fixed nights before
2026-11-14 stopped with hours and yuan unspent after second passes that had filled 5,
17 and 34 values.
"""

from __future__ import annotations

import types

from tracker.night import steps

# --- the fixed schedule ------------------------------------------------------------------


def _options(**over):
    base = {
        "do_discover": False,
        "discover": 0,
        "search_cap": 0,
        "baselines": 0,
        "conditions": 0,
        "pairs": 40,
        "audit": 60,
        "risks": 40,
        "findings": 60,
        "suspects": 25,
        "nest": 20,
        "enrich_rounds": 2,
        "pipeline": 45,
        "pipeline_budget": 180,
        "do_enrich": True,
        "enrich": 75,
        "enrich_budget": 300,
        "reread": 0,
        "recheck": 0,
    }
    base.update(over)
    return types.SimpleNamespace(**base)


def _kinds(plan):
    return [s.kind for s in plan]


def test_a_fixed_night_rereads_a_second_batch_of_suspects_in_round_two():
    assert "suspects" in _kinds(steps.fixed_round(_options(), 1))
    assert "suspects" in _kinds(steps.fixed_round(_options(), steps.SECOND_SUSPECTS))
    assert "suspects" not in _kinds(steps.fixed_round(_options(), 3))
    assert "suspects" not in _kinds(steps.fixed_round(_options(suspects=0), 2))


def test_a_fixed_night_keeps_enriching_fresh_rows_while_a_pass_pays():
    round_three = steps.fixed_round(_options(), 3)
    assert "enrich" not in _kinds(round_three) and "pipeline" not in _kinds(round_three)

    paid = steps.fixed_round(_options(), 3, paid=frozenset({"enrich"}))
    (enrich,) = [s for s in paid if s.kind == "enrich"]
    assert enrich.fresh, "a later pass only takes rows not yet asked tonight"
    assert "pipeline" not in _kinds(paid)


def _record(kind, result, exits=(0,)):
    from tracker.night.record import StepRecord

    return StepRecord(
        night="n", mode="fixed", round=1, index=1, kind=kind, origin="fixed", size=75,
        started="", result=result, exits=list(exits),
    )  # fmt: skip


def test_a_pass_earns_another_only_by_what_it_filled():
    from tracker.overnight import Night

    def paying(records):
        night = types.SimpleNamespace(records=records)
        return Night.paying(night)

    assert paying([_record("enrich", 5)]) == {"enrich"}
    assert paying([_record("enrich", 34), _record("enrich", 4)]) == set()
    assert paying([_record("enrich", 9, exits=(1,))]) == set(), "a failed pass found nothing"
    assert paying([_record("pipeline", 9), _record("pipeline", 0)]) == set()
    six = [_record("enrich", 20) for _ in range(steps.FIXED_PASSES)]
    assert paying(six) == set(), "a ceiling on passes, whatever they fill"
