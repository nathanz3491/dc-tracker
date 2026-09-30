"""What a paid call costs in money, when it costs double, and three things that stop
paying for the same answer twice: the peak-hours guard, the no-reasoning sibling a
starved call retries on, and the search-result cache.

No network and no API key beyond a placeholder: prices are arithmetic on ledger lines,
the guard takes its clock as an argument, and the search backend is a fake.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import pytest

from tracker import spend
from tracker.config import Settings

UTC = dt.UTC


def _beijing(year, month, day, hour, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=spend.BEIJING)


# --- prices ------------------------------------------------------------------------


def test_peak_is_weekday_mornings_and_afternoons_beijing_time():
    tuesday = (2026, 9, 29)
    assert spend.is_peak(_beijing(*tuesday, 9))
    assert spend.is_peak(_beijing(*tuesday, 11, 59))
    assert not spend.is_peak(_beijing(*tuesday, 12)), "lunch is off-peak"
    assert spend.is_peak(_beijing(*tuesday, 14))
    assert not spend.is_peak(_beijing(*tuesday, 18)), "the overnight loop starts here"
    assert not spend.is_peak(_beijing(2026, 9, 26, 10)), "a Saturday"
    # The ledger writes UTC: 10:00Z is 18:00 in Beijing.
    assert not spend.is_peak(dt.datetime(2026, 9, 29, 10, 0))
    assert spend.is_peak(dt.datetime(2026, 9, 29, 9, 59))


def test_the_first_off_peak_moment_after_a_peak_one():
    assert spend.next_off_peak(_beijing(2026, 9, 29, 10, 30)) == _beijing(2026, 9, 29, 12)
    assert spend.next_off_peak(_beijing(2026, 9, 29, 15)) == _beijing(2026, 9, 29, 18)
    assert spend.next_off_peak(_beijing(2026, 9, 29, 20)) == _beijing(2026, 9, 29, 20)


def _line(when="2026-09-26T12:00:00Z", model="deepseek-flash", p=0, c=0, hit=0, miss=0):
    return f"{when}\t1\tenrich\t{model}\t{p}\t{c}\t{hit}\t{miss}\n"


def test_a_call_is_priced_by_what_was_cached_what_was_not_and_the_reply():
    """The reply is four times an uncached prompt token and 200 times a cached one,
    which is why a token count was no measure of a night."""
    call = spend.parse(_line(p=1_000_000, c=1_000_000, hit=900_000, miss=100_000))
    assert call.cost() == pytest.approx(0.018 + 0.1 + 4.0)


def test_a_peak_call_costs_double():
    off = spend.parse(_line(when="2026-09-29T10:00:00Z", c=1_000_000))
    peak = spend.parse(_line(when="2026-09-29T02:00:00Z", c=1_000_000))
    assert off.cost() == pytest.approx(4.0)
    assert peak.cost() == pytest.approx(8.0)


def test_an_unknown_model_and_an_unsplit_prompt_are_priced_as_the_dearer_case():
    """So an unknown line can make a ceiling fire early, never late."""
    call = spend.parse(_line(model="some-new-model", p=1_000_000))
    assert call.cost() == pytest.approx(2.0)


def test_the_report_names_phases_and_the_peak_surcharge(tmp_path):
    ledger = tmp_path / "night.spend"
    ledger.write_text(
        _line(c=1_000_000).rstrip("\n")
        + "\textract\n"
        + _line(when="2026-09-29T02:00:00Z", c=100_000),
        encoding="utf-8",
    )
    calls = spend.read(ledger)
    assert spend.total(calls) == pytest.approx(4.8)
    lines = spend.report(calls)
    assert lines[0].split()[0] == "enrich/extract"
    assert "at peak rates" in lines[-1]
    assert spend.read(tmp_path / "missing.spend") == []


# --- the peak-hours guard ------------------------------------------------------------


@pytest.fixture
def armed():
    from tracker import llm

    llm.arm_peak_guard()
    yield
    llm._PEAK_GUARD.clear()
    llm._PEAK_WARNED.clear()


def test_a_command_will_not_start_paying_at_peak(armed):
    from tracker.llm import LLMUnavailable, PeakHours, check_peak

    settings = Settings(deepseek_api_key="k", peak_guard="refuse")
    with pytest.raises(PeakHours) as refused:
        check_peak(settings, now=_beijing(2026, 9, 29, 15))
    assert isinstance(refused.value, LLMUnavailable), "handled wherever a missing key is"
    assert "18:00" in str(refused.value) and "TRACKER_PEAK_GUARD=off" in str(refused.value)

    check_peak(settings, now=_beijing(2026, 9, 29, 18, 1))
    check_peak(Settings(peak_guard="warn"), now=_beijing(2026, 9, 29, 15))
    check_peak(Settings(peak_guard="off"), now=_beijing(2026, 9, 29, 15))


def test_the_guard_is_off_until_a_command_arms_it():
    """The console's briefing panels never arm it: a reader waiting on a row is not a
    batch job that could have waited until the evening."""
    from tracker import llm

    llm._PEAK_GUARD.clear()
    llm.check_peak(Settings(peak_guard="refuse"), now=_beijing(2026, 9, 29, 15))


def test_every_command_that_spends_arms_the_guard():
    from tracker import llm
    from tracker.cli._shared import _use_llm

    llm._PEAK_GUARD.clear()
    try:
        _use_llm(None)
        assert llm._PEAK_GUARD.is_set()
    finally:
        llm._PEAK_GUARD.clear()


def test_a_deepseek_extractor_built_at_peak_is_refused_like_a_missing_key(armed, monkeypatch):
    from tracker import llm

    monkeypatch.setattr(spend, "is_peak", lambda when: True)
    with pytest.raises(llm.PeakHours):
        llm.DeepSeekExtractor(Settings(deepseek_api_key="k", peak_guard="refuse"))


# --- reasoning off, and the local judgement tier --------------------------------------


def test_the_no_reasoning_sibling_is_a_copy_with_only_the_effort_changed():
    from tracker.llm import DeepSeekExtractor, without_thinking

    thinking = DeepSeekExtractor(Settings(deepseek_api_key="k"), effort="high")
    quiet = without_thinking(thinking)
    assert quiet.effort is None and quiet.thinking is False
    assert thinking.effort == "high"
    assert quiet.model == thinking.model and quiet.settings is thinking.settings
    assert without_thinking(quiet) is None, "nothing left to switch off"
    assert without_thinking(object()) is None


def test_the_judgement_tier_alone_can_go_to_the_local_model():
    from tracker.llm import (
        DeepSeekExtractor,
        OllamaExtractor,
        agent_extractor,
        judgement_extractor,
    )

    settings = Settings(deepseek_api_key="k", judgement_provider="ollama")
    assert isinstance(judgement_extractor(settings), OllamaExtractor)
    assert isinstance(agent_extractor(settings), DeepSeekExtractor), "everything else stays"
    assert isinstance(judgement_extractor(Settings(deepseek_api_key="k")), DeepSeekExtractor)


# --- the search cache ------------------------------------------------------------------


class _Backend:
    NAME = "fake"

    def __init__(self):
        self.queries: list[str] = []

    def search(self, query, *, limit=10):
        from tracker.ingest.search import SearchHit

        self.queries.append(query)
        return [SearchHit(url=f"https://news.test/{len(self.queries)}", title=query)]


def test_a_query_asked_again_within_the_week_is_answered_from_disk(tmp_path):
    """Each overnight round is a new process, so the in-run memo forgot every query:
    487 were sent on one night, most of them the same ones round after round."""
    from tracker.ingest.search import CachedProvider

    backend = _Backend()
    cached = CachedProvider(backend, root=tmp_path, days=7)
    first = cached.search("STACK Hillsboro megawatts", limit=10)
    again = CachedProvider(backend, root=tmp_path, days=7).search(
        "STACK Hillsboro megawatts", limit=10
    )
    assert backend.queries == ["STACK Hillsboro megawatts"]
    assert [h.url for h in again] == [h.url for h in first]

    cached.search("STACK Hillsboro megawatts", limit=5)
    assert len(backend.queries) == 2, "a different ask is a different question"


def test_a_stale_answer_is_asked_again_and_a_failure_is_never_cached(tmp_path):
    from tracker.ingest.search import CachedProvider, SearchError

    backend = _Backend()
    cached = CachedProvider(backend, root=tmp_path, days=7)
    cached.search("q")
    for path in tmp_path.iterdir():
        old = time.time() - 8 * 86_400
        os.utime(path, (old, old))
    cached.search("q")
    assert backend.queries == ["q", "q"]

    class Refusing(_Backend):
        def search(self, query, *, limit=10):
            raise SearchError("quota")

    refusing = CachedProvider(Refusing(), root=tmp_path / "r", days=7)
    (tmp_path / "r").mkdir()
    with pytest.raises(SearchError):
        refusing.search("q2")
    assert list((tmp_path / "r").iterdir()) == []


def test_the_cache_can_be_turned_off():
    from tracker.ingest.search import CachedProvider, cached

    backend = _Backend()
    assert cached(backend, Settings(search_cache_days=0)) is backend
    assert isinstance(cached(backend, Settings(search_cache_days=7)), CachedProvider)
