"""The shared industry block, and the version stamp that has to cover it.

Twelve prompts read the same industry, so the background they need is written
once and prepended to all of them. That creates one hazard worth a test file:
a file outside the prompt can now change what a prompt says, and
`source.extractor` claims to identify what produced every row.
"""

from __future__ import annotations

import pytest

from tracker import prompts
from tracker.prompts import PromptError, available, load_prompt


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """`load_prompt` is lru_cached, and these tests change what it would read."""
    load_prompt.cache_clear()
    yield
    load_prompt.cache_clear()


def test_the_partial_is_not_a_prompt():
    """`_industry.txt` has no sections and must never be offered as loadable.

    It would fail `_split_sections` with a confusing error, and `tracker crawl
    --prompt _industry` is not a thing anyone should be able to ask for.
    """
    assert "_industry" not in available()
    assert not any(n.startswith("_") for n in available())
    with pytest.raises(PromptError):
        load_prompt("_industry")


def test_every_prompt_carries_the_industry_context():
    """All twelve, not just extraction.

    The failures that motivated it were spread across extraction, the evidence
    audit and the contradiction checker, and a block only some prompts get is one
    somebody has to remember to add.
    """
    for name in available():
        system = load_prompt(name).system
        assert system.startswith("HOW US DATA CENTER PROJECTS ACTUALLY WORK"), name
        # The three facts each prompt is worst without.
        assert "generating capacity" in system, name
        assert "$8-15M per MW" in system, name
        assert "broke ground" in system, name


def test_the_context_comes_first_so_the_prompt_wins():
    """Background first, task last.

    Where the shared block and a prompt's own rules touch, the prompt's rules are
    the ones that must win — extraction forbids outside knowledge, and the block
    has to arrive as background to that rule rather than as a licence over it.
    """
    p = load_prompt("extract-v1")
    context_at = p.system.index("HOW US DATA CENTER PROJECTS ACTUALLY WORK")
    task_at = p.system.index("You are a precise data-extraction engine")
    assert context_at < task_at


def test_editing_the_shared_block_moves_every_stamp(monkeypatch):
    """The stamp identifies the whole system message, or it identifies nothing.

    Without this, `_industry.txt` could be edited and every row produced
    afterwards would carry a version string claiming to be the old prompt — which
    is precisely the failure the sha1 exists to prevent, reintroduced by a file
    the hash did not cover.
    """
    before = {n: load_prompt(n).stamp for n in available()}

    load_prompt.cache_clear()
    monkeypatch.setattr(prompts, "_shared_bytes", lambda: b"DIFFERENT BACKGROUND")
    after = {n: load_prompt(n).stamp for n in available()}

    assert before.keys() == after.keys()
    for name in before:
        assert before[name] != after[name], f"{name} stamp did not move"


def test_a_missing_shared_block_degrades_rather_than_breaks(monkeypatch):
    """Absence is tolerated on purpose.

    Every prompt is complete without it — it adds background, not instructions —
    and a partial that failed to ship should cost extraction quality, not take
    down every command that loads a prompt.
    """
    monkeypatch.setattr(prompts, "_shared_bytes", lambda: b"")
    p = load_prompt("extract-v1")
    assert p.system.startswith("You are a precise data-extraction engine")
    assert "$schema" not in p.system
    assert p.user_template  # templating is untouched by any of this


def test_the_context_forbids_itself_as_a_source_of_values():
    """The one thing that would make this block worse than nothing.

    It is background for judging what a sentence means. A model that treated it
    as knowledge to answer *from* would start filling megawatts and dollars with
    plausible industry averages wearing a citation, which is the exact failure
    the evidence gate exists to prevent — and it would arrive through the gate,
    because the model would have a quote for the field it was inventing around.
    """
    system = load_prompt("extract-v1").system
    assert "It is not a source of values." in system
    assert "THE DOCUMENT WINS" in system


def test_one_question_is_asked_at_all_three_moments():
    """The same pair question is put by the agent judge, the one-call judge and the
    ingest-time arbiter. Three copies of a prompt are three prompts, and they drift:
    that is how v1's "answer unclear on granularity" outlived the change that made
    granularity answerable. `triage.CONTRADICTIONS` is the single copy.
    """
    from tracker import gatekeeper, triage

    checklist = triage.CONTRADICTIONS
    assert checklist in triage.PAIR_SYSTEM, "the agent judge"
    assert checklist in gatekeeper.RULES, "the ingest-time arbiter"
    assert checklist in gatekeeper.SYSTEM and checklist in gatekeeper.SYSTEM_WARM
    assert checklist in load_prompt("duplicates-resolve-v3").system, "the one-call judge"


def test_the_superseded_pair_prompt_is_kept_and_is_not_the_default():
    """v2 stays so the two can be run against each other on the same pairs. It must
    not be what a run reaches for."""
    import inspect

    from tracker import dupresolve

    assert load_prompt("duplicates-resolve-v2").system, "v2 must still load"
    assert "duplicates-resolve-v2" not in load_prompt("duplicates-resolve-v3").system
    default = inspect.signature(dupresolve.ask_model).parameters["prompt_name"].default
    assert default == "duplicates-resolve-v3"
