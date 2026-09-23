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

import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "overnight.sh"


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
    script = "set -euo pipefail\n" + _function("spent_so_far") + _function("over_ceiling") + body
    return subprocess.run(
        [BASH, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
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


def test_every_paid_phase_is_preceded_by_a_ceiling_check():
    """The header promised a check "between phases" and the loop made one per round,
    so a single round could overshoot the ceiling by a round's worth of agent runs."""
    text = SCRIPT.read_text(encoding="utf-8")
    for phase in ("audit", "risks", "logic", "duplicates", "enrich"):
        assert f"if capped {phase}; then break; fi" in text, phase


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
    _when, _pid, command, model, prompt, completion, hit, miss = lines[0].split("\t")
    assert (command, model) == ("risks confirm", "deepseek-flash")
    assert (prompt, completion, hit, miss) == ("1200", "80", "1000", "200")


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
