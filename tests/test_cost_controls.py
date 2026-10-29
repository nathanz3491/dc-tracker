"""What a paid call costs in money, when it costs double, and three things that stop
paying for the same answer twice: the peak-hours guard, the no-reasoning sibling a
starved call retries on, and the search-result cache.

No network and no API key beyond a placeholder: prices are arithmetic on ledger lines,
the guard takes its clock as an argument, and the search backend is a fake.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
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


HIT, MISS, OUT = spend.PRICES["deepseek-flash"]


def test_a_call_is_priced_by_what_was_cached_what_was_not_and_the_reply():
    """The reply is four times an uncached prompt token and 200 times a cached one,
    which is why a token count was no measure of a night."""
    call = spend.parse(_line(p=1_000_000, c=1_000_000, hit=900_000, miss=100_000))
    assert call.cost() == pytest.approx(0.9 * HIT + 0.1 * MISS + OUT)
    assert pytest.approx(4 * MISS) == OUT and pytest.approx(50 * HIT) == MISS


def test_the_prices_are_opencode_gos_in_yuan():
    """The plan's limit is dollars of OpenCode Go's metering, so the ledger prices at
    its rates: a ceiling in yuan is then an overcount of either bill, never under."""
    assert pytest.approx((0.003 * 7.1, 0.15 * 7.1, 0.60 * 7.1)) == (HIT, MISS, OUT)
    assert OUT > 4.0, "above DeepSeek's own ¥4 a million reply tokens"


def test_a_search_is_counted_and_never_priced_as_a_model_call(tmp_path):
    """Searches are prepaid credits, another bill: the model ceiling must not see them."""
    ledger = tmp_path / "night.spend"
    ledger.write_text(
        _line(c=1_000_000) + _line(model=spend.SEARCH_MODEL) + _line(model=spend.SEARCH_MODEL),
        encoding="utf-8",
    )
    calls = spend.read(ledger)
    assert spend.total(calls) == pytest.approx(OUT)
    assert spend.searches(calls) == 2
    assert spend.report(calls)[0] == (
        "    2 web search(es), about ¥0.01 of prepaid credit outside the ¥ ceiling: 2 enrich"
    )


def test_the_closing_line_prices_searches_beside_the_model_bill(tmp_path, capsys):
    """2026-10-19 closed on "¥9.89" while its 780 searches had spent about ¥5.54 more,
    over a third of the night's money. The ceiling still reads the model bill alone."""
    ledger = tmp_path / "night.spend"
    ledger.write_text(
        _line(p=500_000, c=1_000_000) + _line(model=spend.SEARCH_MODEL) * 780,
        encoding="utf-8",
    )
    calls = spend.read(ledger)
    assert pytest.approx(0.0071) == spend.SEARCH_CNY
    assert spend.summary(calls) == (
        f"¥{spend.total(calls):.2f} on models, ~1,500,000 tokens; "
        "780 web search(es), about ¥5.54 of prepaid credit"
    )
    assert spend.main(["total", str(ledger)]) == 0
    assert capsys.readouterr().out.strip() == f"{OUT + 0.5 * MISS:.2f}", "the ceiling's figure"

    assert spend.main(["summary", str(ledger)]) == 0
    assert "780 web search(es)" in capsys.readouterr().out

    quiet = tmp_path / "quiet.spend"
    quiet.write_text(_line(c=1_000_000), encoding="utf-8")
    assert spend.summary(spend.read(quiet)) == f"¥{OUT:.2f} on models, ~1,000,000 tokens"


def test_the_nights_search_cap_counts_the_ledger_and_refuses_past_it(tmp_path, monkeypatch):
    """The cap is the whole night's, not one process's: the first check reads what the
    ledger already holds, so the news watch and enrich share one allowance."""
    import httpx

    from tracker.ingest.search import QuotaExhausted, SerperProvider

    ledger = tmp_path / "night.spend"
    ledger.write_text(_line(model=spend.SEARCH_MODEL), encoding="utf-8")
    settings = Settings(_env_file=None, serper_api_key="k", spend_ledger=ledger, search_cap=3)

    def post(url, **_kw):
        return httpx.Response(200, json={"news": []}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    provider = SerperProvider(settings)
    provider.news("a")
    provider.news("b")
    with pytest.raises(QuotaExhausted, match="TRACKER_SEARCH_CAP"):
        provider.news("c")
    assert spend.searches(spend.read(ledger)) == 3, "one before, two now, none refused"


def test_a_peak_call_costs_double():
    off = spend.parse(_line(when="2026-09-29T10:00:00Z", c=1_000_000))
    peak = spend.parse(_line(when="2026-09-29T02:00:00Z", c=1_000_000))
    assert off.cost() == pytest.approx(OUT)
    assert peak.cost() == pytest.approx(2 * OUT)


def test_an_unknown_model_and_an_unsplit_prompt_are_priced_as_the_dearer_case():
    """So an unknown line can make a ceiling fire early, never late."""
    call = spend.parse(_line(model="some-new-model", p=1_000_000))
    assert call.cost() == pytest.approx(2 * MISS)


def test_the_report_names_phases_and_the_peak_surcharge(tmp_path):
    ledger = tmp_path / "night.spend"
    ledger.write_text(
        _line(c=1_000_000).rstrip("\n")
        + "\textract\n"
        + _line(when="2026-09-29T02:00:00Z", c=100_000),
        encoding="utf-8",
    )
    calls = spend.read(ledger)
    assert spend.total(calls) == pytest.approx(1.2 * OUT)
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
    """The local extractor asks its server for a version when built; mocked, so the
    test does not depend on one running on this machine (it failed on 2026-10-19
    after the machine woke with no Ollama up)."""
    import respx

    from tracker.llm import (
        DeepSeekExtractor,
        OllamaExtractor,
        agent_extractor,
        judgement_extractor,
    )

    settings = Settings(deepseek_api_key="k", judgement_provider="ollama")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{settings.ollama_base_url}/api/version").respond(200, json={"version": "0"})
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


# --- the month (2026-10-25) --------------------------------------------------------


def _night(day: dt.datetime, cny: float) -> str:
    """A ledger line costing `cny` yuan off-peak: flash replies at $0.60 per million."""
    reply = round(cny / (0.60 * spend.CNY_PER_USD) * 1e6)
    return f"{day:%Y-%m-%dT%H:%M:%SZ}\t1\tenrich\tdeepseek-v4.1-flash\t0\t{reply}\t0\t0\textract\n"


def test_the_tracker_keeps_to_85_percent_of_the_month():
    assert spend.monthly_budget() == pytest.approx(80 * 7.1 * 0.85)  # ¥482.80


def test_a_command_run_by_hand_records_its_calls_in_the_daytime_ledger(tmp_path, monkeypatch):
    """The console's AI panels and hand-run commands recorded nothing, so the month's
    share had to guess at them. Every CLI process now writes a ledger: the night's
    when the loop names one, else the month's daytime ledger beside the database."""
    from typer.testing import CliRunner

    from tracker.cli import app
    from tracker.config import get_settings

    (tmp_path / "runs").mkdir()
    db = tmp_path / "tracker.db"
    CliRunner().invoke(app, ["--db", str(db), "version"])
    ledger = os.environ.get("TRACKER_SPEND_LEDGER", "")
    assert ledger.startswith(str(tmp_path / "runs" / "daytime-")) and ledger.endswith(".spend")
    assert get_settings().spend_ledger == pathlib.Path(ledger)

    monkeypatch.setenv("TRACKER_SPEND_LEDGER", str(tmp_path / "night.spend"))
    CliRunner().invoke(app, ["--db", str(db), "version"])
    assert os.environ["TRACKER_SPEND_LEDGER"] == str(tmp_path / "night.spend"), "the loop's wins"

    monkeypatch.delenv("TRACKER_SPEND_LEDGER")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    CliRunner().invoke(app, ["--db", str(elsewhere / "tracker.db"), "version"])
    assert "TRACKER_SPEND_LEDGER" not in os.environ, "no runs directory, no ledger"
    get_settings.cache_clear()


def test_a_month_spent_at_its_share_leaves_tonight_its_share():
    now = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
    share = spend.monthly_budget() / 30
    lines = [_night(now - dt.timedelta(days=d), share) for d in range(1, 30)]
    calls = [spend.parse(line) for line in lines]
    assert spend.allowance(calls, now=now) == pytest.approx(share, abs=0.01)


def test_a_quiet_month_is_not_carried_over():
    """Carrying October's underspend over ran nights above the share for four weeks,
    then left two nights with nothing once the cheap ones left the window."""
    now = dt.datetime(2026, 10, 26, 10, 0, tzinfo=dt.UTC)
    quiet = [spend.parse(_night(now - dt.timedelta(days=d), 11.0)) for d in range(1, 30)]
    share = spend.monthly_budget() / 30
    assert spend.allowance(quiet, now=now) == pytest.approx(share)
    assert spend.allowance([], now=now) == pytest.approx(share)


def test_a_dear_month_pays_itself_back_over_a_week_not_in_one_night():
    now = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
    share = spend.monthly_budget() / 30
    dear = [spend.parse(_night(now - dt.timedelta(days=d), share + 3)) for d in range(1, 30)]
    tonight = spend.allowance(dear, now=now)
    assert tonight == pytest.approx(share - 29 * 3 / 7, abs=0.05)
    assert tonight > 0, "a night at nothing reads no news"
    gone = [spend.parse(_night(now - dt.timedelta(days=1), 600.0))]
    assert spend.allowance(gone, now=now) == 0.0


def test_nights_that_want_more_than_their_share_keep_every_month_inside_the_budget():
    now = dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.UTC)
    nights: list = []
    spent = []
    for day in range(90):
        when = now + dt.timedelta(days=day)
        cny = min(spend.allowance(nights, now=when), 25.0)
        nights.append(spend.parse(_night(when, cny)))
        spent.append(cny)
    worst = max(sum(spent[i : i + 30]) for i in range(61))
    assert worst <= spend.monthly_budget() + 0.05
    assert min(spent) > 0


def test_only_the_last_29_days_count_and_searches_never_do(tmp_path):
    now = dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.UTC)
    (tmp_path / "old.spend").write_text(
        _night(now - dt.timedelta(days=40), 500.0), encoding="utf-8"
    )
    (tmp_path / "night.spend").write_text(
        "2026-11-19T12:00:00Z\t1\tnews\tserper\t0\t0\t0\t0\tnews\n" * 1000, encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text(_night(now - dt.timedelta(days=1), 500.0), encoding="utf-8")
    assert spend.allowance(spend.read_all(tmp_path), now=now) == pytest.approx(
        spend.monthly_budget() / 30
    )


def test_the_allowance_is_a_command_the_loop_can_read(tmp_path, capsys):
    assert spend.main(["allowance", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == f"{spend.monthly_budget() / 30:.2f}"


# --- the ceiling a pass asks about mid-run --------------------------------------


def _ceiling(monkeypatch, ledger, *, cny=None, tokens=None):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_SPEND_LEDGER", str(ledger))
    if cny is not None:
        monkeypatch.setenv("TRACKER_SPEND_CEILING_CNY", str(cny))
    if tokens is not None:
        monkeypatch.setenv("TRACKER_SPEND_CEILING_TOKENS", str(tokens))
    get_settings.cache_clear()


def test_no_ceiling_set_is_never_reached(tmp_path, monkeypatch):
    ledger = tmp_path / "night.spend"
    ledger.write_text(_line(c=10_000_000), encoding="utf-8")
    _ceiling(monkeypatch, ledger)
    assert spend.ceiling_reached() == ""


def test_the_ceiling_prices_the_ledger_the_way_total_does(tmp_path, monkeypatch):
    """One million reply tokens is `OUT` yuan; a search beside it costs the ceiling nothing."""
    ledger = tmp_path / "night.spend"
    ledger.write_text(_line(c=1_000_000) + _line(model=spend.SEARCH_MODEL), encoding="utf-8")
    _ceiling(monkeypatch, ledger, cny=OUT + 0.01)
    assert spend.ceiling_reached() == ""
    assert spend.ledger_spent(ledger)[0] == pytest.approx(spend.total(spend.read(ledger)))

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(_line(c=100_000))
    why = spend.ceiling_reached()
    assert why and "ceiling" in why, "a line appended since the last look is counted"


def test_the_token_ceiling_stops_too(tmp_path, monkeypatch):
    ledger = tmp_path / "night.spend"
    ledger.write_text(_line(p=600, c=400), encoding="utf-8")
    _ceiling(monkeypatch, ledger, tokens=1_000)
    assert "token" in spend.ceiling_reached()


def test_the_ledger_is_read_incrementally_and_a_half_line_waits(tmp_path):
    """Asked before every item, so it must not re-read a night's ledger each time —
    and a line a writer is half way through is not counted until it is whole."""
    ledger = tmp_path / "night.spend"
    ledger.write_text(_line(c=1_000_000), encoding="utf-8")
    assert spend.ledger_spent(ledger)[0] == pytest.approx(OUT)
    whole = _line(c=1_000_000)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(whole[:10])
    assert spend.ledger_spent(ledger)[0] == pytest.approx(OUT)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(whole[10:])
    assert spend.ledger_spent(ledger)[0] == pytest.approx(2 * OUT)


def test_a_ledger_that_shrank_is_read_again_from_the_start(tmp_path):
    ledger = tmp_path / "night.spend"
    ledger.write_text(_line(c=1_000_000) * 3, encoding="utf-8")
    assert spend.ledger_spent(ledger)[0] == pytest.approx(3 * OUT)
    ledger.write_text(_line(c=1_000_000), encoding="utf-8")
    assert spend.ledger_spent(ledger)[0] == pytest.approx(OUT)


def test_a_missing_ledger_has_spent_nothing(tmp_path):
    assert spend.ledger_spent(tmp_path / "absent.spend") == (0.0, 0)
    assert spend.ledger_spent(None) == (0.0, 0)
