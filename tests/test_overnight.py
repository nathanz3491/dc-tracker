"""The overnight loop's spend accounting: the ledger, and the tally that reads it.

`scripts/overnight.sh` ran no phase at all for twenty-one nights. Its token tally was
`grep -o '~N tokens'` over the log, piped onward, under `set -euo pipefail` — and the
tally was taken at the top of round one, before any phase had printed a line for it
to match. A grep that matches nothing exits 1, pipefail made that the pipeline's
status, and `set -e` ended the script straight after its snapshot, every night,
with nothing in the log to say so.

The script is bash and the suite is Python, so the functions under test are lifted
out of the script text and run in a real bash with the script's own shell options.
That is the only way to test the property that failed: not what the function
computes, but whether calling it can kill its caller.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "overnight.sh"


def _bash() -> str | None:
    """A bash that can actually run a command here, or None to skip.

    `shutil.which` alone is not enough on Windows, where it can find the WSL
    launcher on a machine with no distribution installed.
    """
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        done = subprocess.run(
            [bash, "-c", "echo ok"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bash if done.stdout.strip() == "ok" else None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no working bash on this machine")

#: The interpreter the script's `$PY` stands for, spelled so the bash found above can
#: run it, and the checkout it must import `tracker` from — not whichever copy happens
#: to be installed.
PY = Path(sys.executable).as_posix()
ENV = {**os.environ, "PYTHONPATH": ROOT.as_posix()}


def _python_from_bash() -> bool:
    if BASH is None:
        return False
    try:
        done = subprocess.run(
            [BASH, "-c", f'"{PY}" -c "import tracker.spend; print(1)"'],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.stdout.strip() == "1"


needs_python = pytest.mark.skipif(
    not _python_from_bash(), reason="the bash here cannot run this Python (WSL?)"
)


def _function(name: str) -> str:
    """One shell function's source, exactly as the script defines it."""
    text = SCRIPT.read_text(encoding="utf-8")
    found = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.MULTILINE | re.DOTALL)
    assert found, f"{name} is not defined in {SCRIPT.name}"
    return found.group(0)


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """`body` under the script's own shell options, with its tally functions defined.

    Relative paths and a working directory, because the bash found on a Windows
    machine may not read a Windows path.
    """
    script = (
        "set -euo pipefail\n"
        f'PY="{PY}"\n'
        "CNY_CAP=1000000\n"
        + _function("spent_so_far")
        + _function("spent_cny")
        + _function("over_ceiling")
        + body
    )
    return subprocess.run(
        [BASH, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=ENV,
    )


@needs_bash
def test_a_night_that_has_spent_nothing_survives_its_first_tally(tmp_path):
    """The regression. Before any paid phase has run there is nothing to count, and
    counting nothing must print 0 — not end the night."""
    done = _run(
        tmp_path,
        'export TRACKER_SPEND_LEDGER=none-yet.spend\nSPENT=$(spent_so_far)\necho "alive $SPENT"\n',
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "alive 0"


@needs_bash
def test_the_tally_is_prompt_plus_completion_across_every_call(tmp_path):
    (tmp_path / "night.spend").write_text(
        "2026-09-24T10:00:00Z\t1\tlogic resolve\tdeepseek-flash\t1000\t200\t900\t100\n"
        "2026-09-24T10:01:00Z\t2\tduplicates resolve\tdeepseek-flash\t40\t2\t0\t40\n",
        encoding="utf-8",
    )
    done = _run(tmp_path, "export TRACKER_SPEND_LEDGER=night.spend\nspent_so_far\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "1242"


@needs_bash
def test_the_ceiling_is_a_test_the_caller_can_branch_on(tmp_path):
    """`over_ceiling` is called inside `if`, so its non-zero answer must be an answer
    and never an exit."""
    (tmp_path / "night.spend").write_text("t\t1\tenrich\tm\t600\t0\t0\t0\n", encoding="utf-8")
    body = (
        "export TRACKER_SPEND_LEDGER=night.spend\n"
        "TOKEN_CAP=1000\n"
        'if over_ceiling; then echo over; else echo "under $SPENT"; fi\n'
        "TOKEN_CAP=500\n"
        'if over_ceiling; then echo "over $SPENT"; else echo under; fi\n'
    )
    done = _run(tmp_path, body)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:2] == ["under 600", "over 600"]


@needs_bash
@needs_python
def test_the_money_ceiling_fires_on_the_bill_not_the_token_count(tmp_path):
    """The token ceiling mostly counted cached prompt, a fiftieth of the price. Two
    nights with the same token count can differ tenfold in money, and money is what
    the ceiling is for."""
    # A Saturday, so off-peak: a million cached prompt tokens is ¥0.02; a hundred
    # thousand reply tokens is ¥0.43 (OpenCode Go's $0.60 a million, at ¥7.1).
    (tmp_path / "cached.spend").write_text(
        "2026-09-26T12:00:00Z\t1\tlogic resolve\tdeepseek-flash\t1000000\t0\t1000000\t0\n",
        encoding="utf-8",
    )
    (tmp_path / "reply.spend").write_text(
        "2026-09-26T12:00:00Z\t1\tenrich\tdeepseek-flash\t0\t100000\t0\t0\textract\n",
        encoding="utf-8",
    )
    body = (
        "TOKEN_CAP=999999999\n"
        "CNY_CAP=0.30\n"
        "export TRACKER_SPEND_LEDGER=cached.spend\n"
        'if over_ceiling; then echo "over $CNY"; else echo "under $CNY"; fi\n'
        "export TRACKER_SPEND_LEDGER=reply.spend\n"
        'if over_ceiling; then echo "over $CNY"; else echo "under $CNY"; fi\n'
    )
    done = _run(tmp_path, body)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:2] == ["under 0.02", "over 0.43"]


@needs_bash
@needs_python
def test_an_unreadable_ledger_prices_at_nothing_and_does_not_end_the_night(tmp_path):
    done = _run(tmp_path, 'export TRACKER_SPEND_LEDGER=no-such.spend\necho "alive $(spent_cny)"\n')
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "alive 0.00"


def _lock(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """`body` with the script's lock functions defined, the lock under `tmp_path`."""
    script = (
        "set -euo pipefail\nLOCK=lock\n" + _function("lock_holder") + _function("take_lock") + body
    )
    return subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )


@needs_bash
def test_a_lock_left_by_a_run_the_machine_killed_is_taken_over(tmp_path):
    """2026-10-16: a reset at 19:32 ended the run before its EXIT trap, and the lock it
    left refused every later night. A lock whose pid is not a live overnight run is
    stale, and the next run takes it over and says so."""
    (tmp_path / "lock").mkdir()
    (tmp_path / "lock" / "pid").write_text("999999\n")
    done = _lock(tmp_path, 'take_lock && echo "took $$" && cat lock/pid\n')
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert "stale lock" in lines[0]
    assert lines[1].startswith("took ")
    assert lines[2] == lines[1].removeprefix("took ")


@needs_bash
def test_a_lock_still_being_written_is_left_alone(tmp_path):
    """No pid yet means another run is between its `mkdir` and its `echo`."""
    (tmp_path / "lock").mkdir()
    done = _lock(tmp_path, "take_lock\n")
    assert done.returncode == 1
    assert "another overnight run holds" in done.stdout
    assert not (tmp_path / "lock" / "pid").exists()


@needs_bash
def test_a_live_run_keeps_its_lock(tmp_path):
    probe = subprocess.run(
        [BASH, "-c", "ps -p $$ -o command="], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        pytest.skip("this bash's ps cannot name a process (Git Bash)")
    (tmp_path / "overnight.sh").write_text("sleep 30\n")
    done = _lock(
        tmp_path,
        "bash ./overnight.sh & holder=$!\nmkdir lock && echo $holder > lock/pid\n"
        "take_lock; status=$?; kill $holder; exit $status\n",
    )
    assert done.returncode == 1
    assert "another overnight run holds" in done.stdout


def test_every_paid_phase_is_preceded_by_a_ceiling_check():
    """The header promised a check "between phases" and the loop made one per round,
    so a single round could overshoot the ceiling by a round's worth of agent runs."""
    text = SCRIPT.read_text(encoding="utf-8")
    for phase in ("audit", "risks", "logic", "duplicates", "enrich", "reread", "news"):
        assert f"if capped {phase}; then break; fi" in text, phase


def test_the_loop_adds_campuses_through_the_identity_check():
    """Nothing else in the loop creates a row since enrich reads for one row only.
    First round only, capped, and through the ordinary crawl — whose identity check
    is what keeps a campus held under another name from gaining a twin."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "DISCOVER=600\n" in text
    assert 'if [ "$DO_DISCOVER" -eq 1 ] && [ "$round" -eq 1 ]; then' in text
    assert "tracker discover --no-show < /dev/null || true" in text
    assert 'tracker news --baselines "$BASELINES"' in text
    assert '--max-queries "$((SEARCH_CAP > 500 ? SEARCH_CAP - 500 : SEARCH_CAP))"' in text
    assert 'tracker ingest crawl --from-queue --new-first --limit "$DISCOVER"' in text
    assert "--no-verify-identity" not in text


def test_the_news_comes_first_in_the_night():
    """Discovery ran last, so a reset at 19:32 on 2026-10-16 meant no discovery that
    day, and feeds that hold less than a day lost those stories for good."""
    text = SCRIPT.read_text(encoding="utf-8")
    loop = text[text.index('for round in $(seq 1 "$ROUNDS"); do') :]
    news = loop.index("tracker discover --no-show")
    for later in ("tracker backfill dates", "tracker audit resolve", "tracker enrich --select"):
        assert news < loop.index(later), later


def test_the_night_has_a_model_ceiling_and_a_separate_search_cap():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "CNY_CAP=15\n" in text, "OpenCode Go's $80 a month, about ¥18 a day; ¥15 is the plan"
    assert "SEARCH_CAP=2000\n" in text
    assert 'export TRACKER_SEARCH_CAP="$SEARCH_CAP"' in text
    assert 'tracker news report --since "$STARTED_UTC"' in text


def test_enrich_runs_once_a_night_on_rows_below_t2():
    """It was 86% of the bill and ran every round. What it reads changes when articles
    are ingested, not between rounds, and `--target 0` alone chose the fullest rows."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'if [ "$DO_ENRICH" -eq 1 ] && [ "$round" -le "$ENRICH_ROUNDS" ]; then' in text
    assert "ENRICH_ROUNDS=1\n" in text
    assert re.search(r"tracker enrich --select \"\$ENRICH\" --t2 ", text)


@needs_bash
def test_a_count_that_wobbles_back_down_is_not_progress(tmp_path):
    """The counts from 2026-09-29, round by round. Compared with the round before,
    every dip read as progress and the night ran seven rounds for nothing; against the
    night's lowest it stops after the third."""
    rounds = [(335, 12, 524), (334, 14, 527), (334, 12, 524), (335, 11, 528)]
    body = "MIN_F=334; MIN_D=12; MIN_B=525; STALE=0\n"
    for f, d, b in rounds:
        body += (
            f"if progressed {f} {d} {b}; then STALE=0; else STALE=$((STALE + 1)); fi\n"
            'echo "$STALE $MIN_F $MIN_D $MIN_B"\n'
        )
    script = "set -euo pipefail\n" + _function("progressed") + body
    done = subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:4] == [
        "0 334 12 524",  # below-T2 fell to a new low: progress
        "1 334 12 524",  # groups up, findings back to the low: nothing new
        "2 334 12 524",  # both back to where they were: still nothing — stop here
        "0 334 11 524",  # (a fourth round would have found one group)
    ]


def test_the_judgement_tier_runs_at_high_effort_overnight_unless_told_otherwise():
    """It ran at `low` for one night and two of three audit decisions were wrong in
    ways that moved published totals. The step costs tenths of a yuan."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "JUDGEMENT_EFFORT=high\n" in text
    assert 'export TRACKER_DEEPSEEK_JUDGEMENT_EFFORT="$JUDGEMENT_EFFORT"' in text
    assert "export TRACKER_JUDGEMENT_PROVIDER=ollama" in text


# --- the ledger itself -------------------------------------------------------


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    from tracker.config import get_settings

    monkeypatch.setenv("TRACKER_DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TRACKER_SPEND_LEDGER", str(tmp_path / "night.spend"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _completion(content: str = "{}") -> dict:
    return {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "prompt_cache_hit_tokens": 1000,
            "prompt_cache_miss_tokens": 200,
        },
    }


@respx.mock
def test_a_paid_call_appends_one_ledger_line(keyed, monkeypatch):
    from tracker.llm import DeepSeekExtractor

    monkeypatch.setattr("sys.argv", ["tracker", "risks", "confirm", "--limit", "40"])
    respx.post("https://api.deepseek.com/chat/completions").respond(200, json=_completion())
    DeepSeekExtractor(keyed).complete(system="s", user="u")

    lines = keyed.spend_ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    _when, _pid, command, model, prompt, completion, hit, miss, stage = lines[0].split("\t")
    assert (command, model) == ("risks confirm", "deepseek-flash")
    assert (prompt, completion, hit, miss) == ("1200", "80", "1000", "200")
    assert stage == "-"


@respx.mock
def test_a_stage_names_which_part_of_a_command_spent(keyed, monkeypatch):
    """`enrich` reads articles, settles disputes and runs an agent, and a ledger that
    only said "enrich" could not say which of the three cost ¥14."""
    from tracker.llm import DeepSeekExtractor, spend_stage

    monkeypatch.setattr("sys.argv", ["tracker", "enrich", "--select", "15"])
    respx.post("https://api.deepseek.com/chat/completions").respond(200, json=_completion())
    with spend_stage("settle"):
        DeepSeekExtractor(keyed).complete(system="s", user="u")
    DeepSeekExtractor(keyed).complete(system="s", user="u")

    stages = [line.split("\t")[8] for line in keyed.spend_ledger.read_text("utf-8").splitlines()]
    assert stages == ["settle", "-"]


@respx.mock
def test_a_refused_request_costs_nothing_and_writes_nothing(keyed):
    from tracker.llm import DeepSeekExtractor, LLMError

    respx.post("https://api.deepseek.com/chat/completions").respond(400, text="bad request")
    with pytest.raises(LLMError):
        DeepSeekExtractor(keyed).complete(system="s", user="u")
    assert not keyed.spend_ledger.exists()


def test_no_ledger_is_kept_unless_one_is_named(tmp_path, monkeypatch):
    from tracker.config import Settings
    from tracker.llm import record_spend

    monkeypatch.chdir(tmp_path)
    record_spend(Settings(spend_ledger=None), _completion(), "m")
    assert list(tmp_path.iterdir()) == []


def test_a_ledger_that_cannot_be_written_never_costs_the_answer(tmp_path):
    """The call is already paid for. Raising here would throw its answer away."""
    from tracker.config import Settings
    from tracker.llm import record_spend

    record_spend(Settings(spend_ledger=tmp_path / "missing-dir" / "x.spend"), _completion(), "m")


def test_the_command_column_names_the_subcommand_not_its_options(monkeypatch):
    from tracker.llm import _command

    monkeypatch.setattr("sys.argv", ["tracker", "duplicates", "resolve", "--merge"])
    assert _command() == "duplicates resolve"
    monkeypatch.setattr("sys.argv", ["tracker", "enrich", "--select", "15"])
    assert _command() == "enrich"
    monkeypatch.setattr("sys.argv", ["tracker"])
    assert _command() == "-"


def test_the_ledger_counts_agent_turns_too(keyed, monkeypatch):
    """`converse` is the agent loop's call and the most expensive one in the tool."""
    from tracker.llm import DeepSeekExtractor

    reply = {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50_000, "completion_tokens": 900},
    }
    with respx.mock:
        respx.post("https://api.deepseek.com/chat/completions").mock(
            return_value=httpx.Response(200, json=reply)
        )
        DeepSeekExtractor(keyed).converse(system="s", messages=[], tools=[])
    line = keyed.spend_ledger.read_text(encoding="utf-8").strip().split("\t")
    assert line[4:6] == ["50000", "900"]


@needs_bash
@needs_python
def test_the_morning_report_breaks_spend_down_by_phase_and_in_money(tmp_path):
    (tmp_path / "night.spend").write_text(
        "t\t1\tlogic resolve\tm\t9000\t1000\t8000\t1000\n"
        "t\t1\tlogic resolve\tm\t9000\t1000\t8000\t1000\n"
        "t\t2\trisks confirm\tm\t500\t100\t0\t0\n"
        "2026-09-26T12:00:00Z\t3\tenrich\tdeepseek-flash\t0\t1000000\t0\t0\textract\n",
        encoding="utf-8",
    )
    script = (
        "set -euo pipefail\n"
        f'PY="{PY}"\n'
        + _function("spend_by_command")
        + "export TRACKER_SPEND_LEDGER=night.spend\nspend_by_command\n"
    )
    done = subprocess.run(
        [BASH, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        timeout=60,
        check=False,
        env=ENV,
    )
    assert done.returncode == 0, done.stderr
    lines = [line.split() for line in done.stdout.decode("utf-8").strip().splitlines()]
    assert lines[0][0] == "enrich/extract" and "¥4.26" in lines[0]
    assert lines[1][:2] == ["logic", "resolve"] and "~20,000" in lines[1] and "89%" in lines[1]
    assert lines[2][:2] == ["risks", "confirm"] and "n/a" in lines[2]


def test_every_row_the_night_adds_gets_one_attempt_to_fold():
    """The regular pass only sees pairs within one town or one company, which is how
    a new 10 GW PORTS row sat beside its duplicate unpaired on 2026-10-05.

    Over a lookback rather than tonight alone (10-19 left about 16 twins a capped or
    blind pass never revisited), capped, and after the Census lookup has placed the
    new rows — before it, a row filed by town met a twin filed by county and saw
    nothing in common."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "STARTED_UTC=$(date -u '+%Y-%m-%d %H:%M:%S')" in text
    assert "TWINS=60\n" in text and "TWIN_DAYS=3\n" in text
    assert '--created-since "$TWIN_SINCE" --limit "$TWINS"' in text
    discover = text.index('tracker ingest crawl --from-queue --new-first --limit "$DISCOVER"')
    geo = text.index("tracker ingest geo", discover)
    twins = text.index('--created-since "$TWIN_SINCE"')
    assert discover < geo < twins, "after the rows exist, and after they are placed"


@needs_bash
def test_the_twin_lookback_is_days_before_now_in_utc(tmp_path):
    """The lookback is computed in Python because BSD and GNU `date` disagree."""
    import datetime as dt

    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("TWIN_SINCE=$(")
    line = text[start : text.index('"$TWIN_DAYS")', start) + len('"$TWIN_DAYS")')]
    script = f'PY="{PY}"\nTWIN_DAYS=3\n{line}\necho "$TWIN_SINCE"\n'
    done = subprocess.run(
        [BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    since = dt.datetime.strptime(done.stdout.strip(), "%Y-%m-%d %H:%M:%S")
    expected = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=3)
    assert abs((since - expected).total_seconds()) < 120


def test_the_first_round_asks_publishers_for_dates_and_rereads_old_prompts():
    """The two gaps the 2026-10-10 night showed: 1,836 citations undated because the
    loop never asked a publisher, and 2,320 last read by a superseded prompt
    because nothing re-read them."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "DATES=150\n" in text and "REREAD=60\n" in text
    assert 'tracker backfill dates --apply --refetch --yes --limit "$DATES"' in text
    assert "tracker ingest crawl --stale-prompt --cached-only --existing-only" in text
    reread = text.index('phase "reread')
    assert text.rfind('if [ "$round" -eq 1 ] && [ "$REREAD" -gt 0 ]', 0, reread) != -1
