"""CLI behaviour, including the PRD's Phase 0 exit criterion as an assertion.

Uses typer's CliRunner so no subprocess is spawned and exit codes are checked
directly. Every invocation passes an explicit `--db` under tmp_path, so a test
run can never touch the operator's real database.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracker.cli import app

runner = CliRunner()

SEED = Path(__file__).resolve().parent.parent / "seed" / "sample-projects.json"


def invoke(db: Path, *args: str):
    return runner.invoke(app, ["--db", str(db), *args])


@pytest.fixture
def initialized(tmp_path: Path) -> Path:
    db = tmp_path / "t.db"
    assert invoke(db, "init").exit_code == 0
    return db


@pytest.fixture
def seeded(initialized: Path) -> Path:
    result = invoke(initialized, "ingest", "manual", "--json", str(SEED), "--allow-placeholders")
    assert result.exit_code == 0, result.output
    return initialized


# --- init -------------------------------------------------------------------


def test_init_creates_the_database(tmp_path: Path):
    db = tmp_path / "nested" / "t.db"
    result = invoke(db, "init")
    assert result.exit_code == 0
    assert db.exists(), "init must create parent directories"
    # Derived, so adding a migration does not require editing this test.
    from tracker.db import discover_migrations

    latest = max(m.version for m in discover_migrations())
    assert f"schema version: {latest}" in result.output


def test_init_is_idempotent(initialized: Path):
    result = invoke(initialized, "init")
    assert result.exit_code == 0
    assert "already current" in result.output


def test_version_prints_a_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "dc-tracker" in result.output


# --- read commands against a missing database -------------------------------


@pytest.mark.parametrize("command", [["list"], ["stats"], ["show", "1"]])
def test_read_commands_say_run_init(tmp_path: Path, command):
    result = invoke(tmp_path / "absent.db", *command)
    assert result.exit_code == 2
    assert "tracker init" in result.output


# --- The PRD Phase 0 exit criterion -----------------------------------------


def test_prd_phase_0_exit_criterion(tmp_path: Path):
    """init -> ingest manual -> list produces a non-empty table.

    Verbatim from the PRD's definition of done, minus the `pip install -e .`
    step which the test environment has already performed.
    """
    db = tmp_path / "t.db"
    assert invoke(db, "init").exit_code == 0
    ingest = invoke(db, "ingest", "manual", "--json", str(SEED), "--allow-placeholders")
    assert ingest.exit_code == 0, ingest.output

    listing = invoke(db, "list")
    assert listing.exit_code == 0
    for company in ("Microsoft", "xAI", "Crusoe"):
        assert company in listing.output
    assert "3 project(s)" in listing.output


# --- The placeholder guard --------------------------------------------------


def test_seed_file_is_refused_while_it_holds_placeholders(initialized: Path):
    """The shipped sample must not be able to become data by accident."""
    result = invoke(initialized, "ingest", "manual", "--json", str(SEED))
    assert result.exit_code == 2
    assert "PLACEHOLDER" in result.output
    assert "--allow-placeholders" in result.output


def test_shipped_seed_still_has_placeholders():
    """If this fails, someone filled in the sample with unverified numbers.

    The seed file's contract is that its names are real and its figures are not.
    """
    assert "PLACEHOLDER" in SEED.read_text(encoding="utf-8")


def test_placeholders_become_null_not_zero(seeded: Path):
    """A placeholder is an absence of data, so it must store as NULL."""
    result = invoke(seeded, "show", "1")
    assert result.exit_code == 0
    assert "MW planned       -" in result.output


# --- ingest reporting -------------------------------------------------------


def test_reingest_reports_unchanged(seeded: Path):
    result = invoke(seeded, "ingest", "manual", "--json", str(SEED), "--allow-placeholders")
    assert result.exit_code == 0
    assert "| unchanged          |     3 |" in result.output


def test_invalid_seed_file_is_rejected_with_the_field_named(tmp_path: Path, initialized: Path):
    """A typo'd key must be an error, not a silently dropped value."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "X",
                        "company": "Y",
                        "city": "Z",
                        "state": "WI",
                        "mw_plannned": 900,
                        "sources": [{"url": "https://example.com/a"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = invoke(initialized, "ingest", "manual", "--json", str(bad))
    assert result.exit_code == 2
    assert "mw_plannned" in result.output


def test_event_citing_an_unlisted_source_is_rejected(tmp_path: Path, initialized: Path):
    bad = tmp_path / "dangling.json"
    bad.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "X",
                        "company": "Y",
                        "city": "Z",
                        "state": "WI",
                        "sources": [{"url": "https://example.com/a"}],
                        "events": [
                            {
                                "event_date": "2025-01-01",
                                "event_type": "announced",
                                "description": "d",
                                "source_url": "https://elsewhere.example/b",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = invoke(initialized, "ingest", "manual", "--json", str(bad))
    assert result.exit_code == 2
    assert "not listed in this record's sources" in result.output


def test_missing_seed_file_is_a_usage_error(initialized: Path):
    result = invoke(initialized, "ingest", "manual", "--json", "no-such-file.json")
    assert result.exit_code != 0


# --- list filters and sorting ----------------------------------------------


def test_list_filters_by_company(seeded: Path):
    result = invoke(seeded, "list", "--company", "microsoft")
    assert result.exit_code == 0
    assert "Microsoft" in result.output
    assert "xAI" not in result.output


def test_list_filters_by_state(seeded: Path):
    result = invoke(seeded, "list", "--state", "tx")
    assert "Crusoe" in result.output
    assert "Microsoft" not in result.output


def test_list_filters_by_phase(seeded: Path):
    result = invoke(seeded, "list", "--phase", "operational")
    assert "xAI" in result.output
    assert "Microsoft" not in result.output


def test_list_filters_by_min_confidence(seeded: Path):
    assert "no projects match" in invoke(seeded, "list", "--min-confidence", "3").output
    assert "3 project(s)" in invoke(seeded, "list", "--min-confidence", "2").output


def test_list_rejects_an_unknown_phase(seeded: Path):
    result = invoke(seeded, "list", "--phase", "bogus")
    assert result.exit_code == 2
    assert "must be one of" in result.output


def test_list_rejects_an_unknown_sort(seeded: Path):
    result = invoke(seeded, "list", "--sort", "sideways")
    assert result.exit_code == 2


@pytest.mark.parametrize("key", ["mw", "investment", "date", "confidence", "name"])
def test_every_documented_sort_key_works(seeded: Path, key):
    assert invoke(seeded, "list", "--sort", key).exit_code == 0


# --- show -------------------------------------------------------------------


def test_show_displays_citations_and_the_confidence_rationale(seeded: Path):
    result = invoke(seeded, "show", "3")
    assert result.exit_code == 0
    assert "Stargate Abilene" in result.output
    assert "OpenAI" in result.output, "customer must be distinguished from company"
    assert "why confidence" in result.output
    assert "datacenterfrontier.com" in result.output
    assert "supports:" in result.output


def test_show_reports_the_dedup_key(seeded: Path):
    """The dedup key is how an operator reasons about a suspected duplicate."""
    assert "crusoe|city:abilene|TX" in invoke(seeded, "show", "3").output


def test_show_missing_project_exits_one(seeded: Path):
    result = invoke(seeded, "show", "9999")
    assert result.exit_code == 1
    assert "no project with id" in result.output


# --- stats ------------------------------------------------------------------


def test_stats_on_an_empty_database_says_so(initialized: Path):
    result = invoke(initialized, "stats")
    assert result.exit_code == 0
    assert "empty" in result.output


def test_stats_summarizes_and_qualifies_its_sums(seeded: Path):
    result = invoke(seeded, "stats")
    assert result.exit_code == 0
    assert "3" in result.output
    assert "by phase" in result.output
    assert "by state" in result.output
    # Sums over partially-cited data are a floor, and must say so rather than
    # implying they are a total.
    assert "floor, not a total" in result.output


# --- the read-only guarantee -----------------------------------------------


# --- discovery queue --------------------------------------------------------


def queue_one(db: Path, url: str = "https://a.test/one/", status: str = "discovered") -> None:
    """Insert a queued candidate directly, without polling any feed."""
    from tracker.db import init_db, session_scope
    from tracker.models import IngestUrl

    engine, _ = init_db(db)
    with session_scope(engine) as session:
        session.add(
            IngestUrl(
                url=url,
                run_id="t",
                status=status,
                title="A 900MW data center campus",
                feed="dcd",
                published_at=dt.datetime(2026, 7, 20),
            )
        )


def test_queue_is_empty_on_a_fresh_database(initialized: Path):
    result = invoke(initialized, "queue")
    assert result.exit_code == 0
    assert "queue is empty" in result.output
    assert "tracker discover" in result.output, "must say how to fill it"


def test_queue_lists_a_candidate_with_its_headline(initialized: Path):
    queue_one(initialized)
    result = invoke(initialized, "queue")
    assert result.exit_code == 0
    assert "900MW data center campus" in result.output, "the headline is what makes it triageable"
    assert "dcd" in result.output
    assert "tracker ingest crawl --from-queue" in result.output


def test_queue_excludes_already_crawled_urls(initialized: Path):
    queue_one(initialized, status="ok")
    assert "queue is empty" in invoke(initialized, "queue").output


def test_queue_drop_removes_a_candidate(initialized: Path):
    queue_one(initialized)
    result = invoke(initialized, "queue", "--drop", "--url", "https://a.test/one/")
    assert result.exit_code == 0
    assert "dropped 1" in result.output
    assert "queue is empty" in invoke(initialized, "queue").output


def test_crawl_from_queue_and_urls_together_is_rejected(initialized: Path, tmp_path: Path):
    urls = tmp_path / "u.txt"
    urls.write_text("https://a.test/x\n", encoding="utf-8")
    result = invoke(initialized, "ingest", "crawl", "--from-queue", "--urls", str(urls))
    assert result.exit_code == 2
    assert "not both" in result.output


def test_crawl_with_neither_source_explains_the_options(initialized: Path):
    result = invoke(initialized, "ingest", "crawl")
    assert result.exit_code == 2
    assert "--from-queue" in result.output
    assert "--check" in result.output


def test_crawl_from_an_empty_queue_exits_cleanly(initialized: Path):
    """Nothing to do is not an error, and must not need an API key to discover that."""
    result = invoke(initialized, "ingest", "crawl", "--from-queue")
    assert result.exit_code == 0
    assert "queue is empty" in result.output


def test_crawl_from_queue_needs_a_key_and_says_so(initialized: Path):
    """The key is checked before any fetching, so a missing one costs nothing."""
    queue_one(initialized)
    result = invoke(initialized, "ingest", "crawl", "--from-queue")
    assert result.exit_code == 2
    assert "MINIMAX_API_KEY" in result.output
    assert "api.minimaxi.com" in result.output, "must mention the CN/global key split"


def test_discover_reports_a_broken_feed_config(initialized: Path, tmp_path: Path):
    bad = tmp_path / "feeds.toml"
    bad.write_text('[[feed]]\nurl = "https://a.test/rss"\n', encoding="utf-8")
    result = invoke(initialized, "discover", "--feeds", str(bad))
    assert result.exit_code == 2
    assert "topic" in result.output and "signal" in result.output


def test_discover_missing_feed_config_is_actionable(initialized: Path, tmp_path: Path):
    result = invoke(initialized, "discover", "--feeds", str(tmp_path / "absent.toml"))
    assert result.exit_code == 2
    assert "feeds.toml" in result.output


@pytest.mark.network
def test_discover_against_the_real_feeds(initialized: Path):
    """Deselected by default. Run with `-m network` to check the feeds still work."""
    result = invoke(initialized, "discover", "--since-days", "45")
    assert result.exit_code == 0
    assert "| feeds failed  |     0 |" in result.output, "a feed URL has gone stale"


# --- export -----------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["md", "csv", "json"])
def test_export_to_stdout(seeded: Path, fmt):
    result = invoke(seeded, "export", fmt)
    assert result.exit_code == 0
    assert "Microsoft" in result.output


def test_export_rejects_an_unknown_format(seeded: Path):
    result = invoke(seeded, "export", "yaml")
    assert result.exit_code == 2
    assert "format must be one of" in result.output


def test_export_to_file_is_byte_stable(seeded: Path, tmp_path: Path):
    """The PRD wants a Markdown table to paste; regenerating must not churn."""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    assert invoke(seeded, "export", "md", "--out", str(a)).exit_code == 0
    assert invoke(seeded, "export", "md", "--out", str(b)).exit_code == 0
    assert a.read_bytes() == b.read_bytes()


def test_export_stamp_adds_a_timestamp(seeded: Path):
    assert "_Generated" not in invoke(seeded, "export", "md").output
    assert "_Generated" in invoke(seeded, "export", "md", "--stamp").output


def test_export_respects_filters(seeded: Path):
    out = invoke(seeded, "export", "csv", "--company", "microsoft").output
    assert "Microsoft" in out
    assert "xAI" not in out


# --- review -----------------------------------------------------------------


def test_review_lists_nothing_when_all_rows_are_confident(seeded: Path):
    """The seed lands at confidence 2, which is the auto-approval threshold."""
    result = invoke(seeded, "review")
    assert result.exit_code == 0
    assert "nothing to review" in result.output


def test_review_lists_low_confidence_rows_with_reasons(seeded: Path):
    result = invoke(seeded, "review", "--max-confidence", "2")
    assert result.exit_code == 0
    assert "need review" in result.output
    assert "why:" in result.output
    assert "tracker review --verify" in result.output, "must say how to act on it"


def test_verify_sets_last_verified_at_and_lifts_confidence(seeded: Path):
    before = invoke(seeded, "show", "1").output
    assert "last verified    never" in before

    result = invoke(seeded, "review", "--verify", "1")
    assert result.exit_code == 0
    assert "is verified" in result.output
    assert "confidence is now 3" in result.output

    after = invoke(seeded, "show", "1").output
    assert "last verified    never" not in after
    assert "operator verified" in after


def test_unverify_reverses_it(seeded: Path):
    invoke(seeded, "review", "--verify", "1")
    result = invoke(seeded, "review", "--unverify", "1")
    assert result.exit_code == 0
    assert "no longer verified" in result.output
    assert "last verified    never" in invoke(seeded, "show", "1").output


def test_verify_rejects_a_missing_project(seeded: Path):
    result = invoke(seeded, "review", "--verify", "9999")
    assert result.exit_code == 1


def test_verify_and_unverify_are_mutually_exclusive(seeded: Path):
    result = invoke(seeded, "review", "--verify", "1", "--unverify", "2")
    assert result.exit_code == 2


# --- verify coverage --------------------------------------------------------


def test_verify_reports_progress_toward_the_target(seeded: Path):
    result = invoke(seeded, "verify")
    assert result.exit_code == 0
    assert "projects in database: 3" in result.output
    assert "target 30" in result.output
    assert "27 short" in result.output


def test_verify_reports_present_and_missing_against_a_list(seeded: Path, tmp_path: Path):
    """Turns an unmeasurable definition-of-done item into a measurable one."""
    listing = tmp_path / "required.txt"
    listing.write_text(
        "# comment\nMicrosoft | Fairwater\nxAI | Colossus\nSomeone | Nonexistent Site\n",
        encoding="utf-8",
    )
    result = invoke(seeded, "verify", "--required", str(listing))
    assert result.exit_code == 0
    assert "2/3 present" in result.output
    assert "missing Someone | Nonexistent Site" in result.output


def test_verify_explains_itself_when_no_list_exists(seeded: Path, tmp_path: Path):
    result = invoke(seeded, "verify", "--required", str(tmp_path / "absent.txt"))
    assert result.exit_code == 0
    assert "no required-project list" in result.output


def test_shipped_required_list_is_a_template_not_invented_data():
    """A fabricated list of 30 projects would look exactly like the deliverable."""
    path = SEED.parent / "required-projects.txt"
    active = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert active == [], "the required list must ship empty for the operator to fill in"


def _logical_snapshot(db: Path) -> dict[str, list[tuple]]:
    """Every row of every table, for comparing data rather than bytes.

    Raw file bytes are the wrong instrument here: SQLite in WAL mode checkpoints
    the write-ahead log into the main file at times of its own choosing, so the
    file legitimately changes without any data changing. That made a byte
    comparison pass or fail depending on when garbage collection closed the
    previous connection.
    """
    import sqlite3
    from contextlib import closing

    # `sqlite3.connect` as a context manager commits but does NOT close, which
    # leaks the handle and raises ResourceWarning under pytest.
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: list(conn.execute(f"SELECT * FROM {t} ORDER BY rowid")) for t in sorted(tables)}


def test_read_commands_do_not_modify_the_database(seeded: Path):
    """PRD: only init, ingest and review may write."""
    before = _logical_snapshot(seeded)
    for command in (["list"], ["stats"], ["show", "1"]):
        assert invoke(seeded, *command).exit_code == 0
    assert _logical_snapshot(seeded) == before
