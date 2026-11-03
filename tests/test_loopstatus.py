"""`tracker loop`: the nightly log parsed into rounds and phases, and drawn."""

from __future__ import annotations

import datetime as dt
import io

from rich.console import Console
from typer.testing import CliRunner

from tracker import loopstatus

LOG = """\
[2026-11-01 18:01:00] overnight starting  (pid 11)
[2026-11-01 21:00:00] round 1 of 20  (¥0.00, ~0 tokens, 599m left)
  round 1: findings 10 -> 9 (-1)  groups 3 -> 3 (+0)  below-T2 50 -> 49 (-1)
[2026-11-01 21:30:00] 2 rounds with no reduction — this is the fixed point, stopping
[2026-11-01 21:31:00] overnight complete — 90m, ¥8.50 on models, ~1,000 tokens

[2026-11-02 18:01:06] overnight starting  (pid 24806)
    ceilings  10h, 20 rounds, ¥45.00, 88524590 tokens, 2000 searches
    spend     /Users/x/repo/data/runs/overnight-20261102-180105.spend
    at start  385 finding(s), 32 duplicate group(s), 842 row(s) below T2

[2026-11-02 18:01:12] snapshot before anything is deleted
[2026-11-02 18:01:16] round 1 of 20  (¥0.00, ~0 tokens, 599m left)

--- 18:01:16  news — feeds and archives, 1350 queued article(s) read
INFO     HTTP Request: GET https://example.com/robots.txt "HTTP/1.1 200 OK"
--- 18:59:24  duplicates — 40 pair(s)
--- 23:40:00  enrich — 75 row(s) below T2, 300 article budget
--- 00:10:00  settle — re-derive and score
  round 1: findings 385 -> 397 (+12)  groups 32 -> 34 (+2)  below-T2 842 -> 785 (-57)
[2026-11-03 00:10:30] round 2 of 20  (¥10.49, ~17524207 tokens, 359m left)
--- 00:10:30  free — dates, geo, scope, derive, logic --auto
"""


def _nights():
    return loopstatus.parse(line for line in io.StringIO(LOG) if loopstatus.wanted(line))


def test_parse_reads_the_settings_the_rounds_and_the_phases():
    first, night = _nights()
    assert first.complete and first.stop == "fixed point" and first.cny_closing == 8.50
    assert night.pid == 24806
    assert (night.hours, night.rounds_cap, night.cny_cap) == (10.0, 20, 45.0)
    assert (night.tokens_cap, night.searches_cap) == (88524590, 2000)
    assert night.ledger is not None and night.ledger.name == "overnight-20261102-180105.spend"
    assert vars(night.at_start) == {"findings": 385, "groups": 32, "below": 842}
    assert [r.number for r in night.rounds] == [1, 2]
    one = night.rounds[0]
    assert [p.name for p in one.phases] == ["news", "duplicates", "enrich", "settle"]
    assert one.phases[0].ended == one.phases[1].started
    assert vars(one.after) == {"findings": 397, "groups": 34, "below": 785}
    assert night.rounds[1].cny_at_start == 10.49
    assert not night.complete


def test_a_phase_after_midnight_is_dated_the_next_day():
    night = _nights()[-1]
    settle = night.rounds[0].phases[-1]
    assert settle.started == dt.datetime(2026, 11, 3, 0, 10)
    assert night.rounds[0].phases[2].ended == settle.started


def test_the_phase_under_way_is_the_last_one_open():
    night = _nights()[-1]
    phase = night.current_phase()
    assert phase is not None and phase.name == "free" and phase.ended is None


def _draw(nights, state, now):
    out = Console(width=120, file=io.StringIO(), color_system=None, legacy_windows=False)
    out.print(loopstatus.render(nights, state, now=now))
    return out.file.getvalue()


def test_render_a_running_night_shows_its_bars_phase_and_history():
    state = loopstatus.Live(running=True, cny=11.25, tokens=18_000_000, searches=500)
    page = _draw(_nights(), state, dt.datetime(2026, 11, 3, 0, 20))
    assert "RUNNING" in page
    assert "Now in free for 9m" in page
    assert "¥11.25 of tonight's ¥45.00; ¥33.75 still available" in page
    assert "25%" in page and "18.0M of 88.5M" in page
    assert "Where the time went" in page and "■ enrich" in page
    assert "12 more" in page and "57 fewer" in page
    assert "Round 2 so far" in page
    assert "The last 2 nights" in page and "fixed point" in page and "still running" in page
    assert "━" in page and "─" in page


def test_render_a_night_cut_off_says_so():
    nights = _nights()
    page = _draw(nights, loopstatus.Live(running=False), dt.datetime(2026, 11, 3, 9))
    assert "CUT OFF" in page and "cut off" in page
    assert "so far" not in page  # the phase list is for a night under way


def test_a_spend_line_says_which_ceiling_stopped_the_night():
    tokens = "spend ceiling reached (¥16.06 of ¥43.18, ~25280576 of 25000000 tokens) before reread"
    money = "spend ceiling reached (¥15.16 of ¥15.09, ~23208740 of 25000000 tokens) before tranches"
    assert loopstatus._which_ceiling(tokens) == "token ceiling"
    assert loopstatus._which_ceiling(money) == "money ceiling"


def test_ascii_bars_for_a_console_that_cannot_draw_lines():
    assert loopstatus.bar(0.5, 10, ascii_only=True).plain == "=====-----"
    assert loopstatus.bar(0.001, 10).plain == "━─────────"  # a sliver still shows


def test_cli_reads_the_log_beside_the_database(tmp_path, monkeypatch):
    from tracker.cli import app

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "overnight.log").write_text(LOG, encoding="utf-8")
    monkeypatch.setattr("tracker.cli.loop._db_path", lambda: tmp_path / "tracker.db")
    result = CliRunner().invoke(app, ["--json", "loop"])
    assert result.exit_code == 0, result.output
    assert '"running": false' in result.output
    assert '"phase": null' in result.output
    result = CliRunner().invoke(app, ["loop"])
    assert result.exit_code == 0, result.output
    assert "nightly loop" in result.output


def test_cli_without_a_log_points_at_the_host(tmp_path, monkeypatch):
    from tracker.cli import app

    monkeypatch.setattr("tracker.cli.loop._db_path", lambda: tmp_path / "tracker.db")
    result = CliRunner().invoke(app, ["loop"])
    assert result.exit_code == 1
    assert "no nightly log" in result.output
