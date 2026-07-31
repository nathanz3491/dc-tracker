"""Export determinism, format contracts, and the traceability guarantee.

The renderers are pure functions, so most of this needs no database. What does
need one is the ordering and the eager-loading query.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re

import pytest
from sqlalchemy import select

from tracker.export import (
    CSV_COLUMNS,
    FORMATS,
    JSON_SCHEMA_TAG,
    ExportFilter,
    fetch_projects,
    render,
    render_csv,
    render_html,
    render_json,
    render_md,
    to_row,
    write_export,
)
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.models import Project
from tracker.upsert import SOURCE_NOTE_PREFIX, upsert_record
from tracker.vocab import TRACKED_FIELDS

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)


def sample(session, *, risks=None, **overrides):
    """A fully-populated project, so exports exercise every column.

    The obstacle comes in as a `RiskRecord`, not as a `blocker` claim: `blocker` is
    derived from the risk rows, so a claim alone would leave the column NULL and
    the export tests would silently stop covering it.
    """
    claims = {
        "name": "Fairwater",
        "company": "Microsoft",
        "customer": "Microsoft",
        "city": "Mount Pleasant",
        "county": "Racine County",
        "state": "WI",
        "mw_planned": 900.0,
        "mw_built": 150.0,
        "investment_usd": 3_300_000_000,
        "phase": "construction",
        "first_announced": dt.date(2023, 3, 1),
        "expected_online": dt.date(2027, 7, 1),
        "blocker": "Transmission upgrades are pending.",
        **overrides,
    }
    upsert_record(
        session,
        IngestRecord(
            project={
                "company": claims["company"],
                "name": claims["name"],
                "city": claims.get("city"),
                "county": claims.get("county"),
                "state": claims["state"],
            },
            sources=[
                SourceRecord(
                    url="https://news.microsoft.com/fairwater/",
                    source_type="company_filing",
                    fetched_at=T0,
                    excerpt="Microsoft said the campus will draw 900 MW.",
                    claims=claims,
                )
            ],
            risks=risks
            if risks is not None
            else [
                RiskRecord(
                    category="transmission",
                    severity="material",
                    summary="Transmission upgrades are pending.",
                    quote="two 345-kilovolt upgrades",
                    first_seen=dt.date(2026, 2, 1),
                    source_url="https://news.microsoft.com/fairwater/",
                )
            ],
        ),
    )
    return session.scalar(select(Project))


# --- CSV --------------------------------------------------------------------


def test_csv_column_tuple_is_frozen():
    """Downstream consumers index by position; reordering silently breaks them.

    New columns go on the END. `risks` reads better beside `blocker`, and putting
    it there would have moved `confidence` and everything after it along by one.
    """
    assert CSV_COLUMNS == (
        "id",
        "company",
        "name",
        "customer",
        "city",
        "county",
        "state",
        "country",
        "phase",
        "mw_planned",
        "mw_built",
        "investment_usd",
        "first_announced",
        "expected_online",
        "blocker",
        "confidence",
        "sources",
        "source_urls",
        "last_verified_at",
        "risks",
    )


def test_csv_uses_lf_not_crlf(session):
    """Python's csv module writes \\r\\n by default; on Windows that becomes \\r\\r\\n."""
    sample(session)
    text = render_csv(fetch_projects(session))
    assert "\r" not in text


def test_csv_round_trips(session):
    sample(session)
    rows = list(csv.DictReader(io.StringIO(render_csv(fetch_projects(session)))))
    assert len(rows) == 1
    assert rows[0]["company"] == "Microsoft"
    assert rows[0]["mw_planned"] == "900.0"
    assert rows[0]["source_urls"] == "https://news.microsoft.com/fairwater/"


def test_csv_writes_empty_string_for_null(session):
    sample(session, mw_built=None)
    rows = list(csv.DictReader(io.StringIO(render_csv(fetch_projects(session)))))
    assert rows[0]["mw_built"] == ""


# --- JSON -------------------------------------------------------------------


def test_json_carries_a_schema_tag_and_nested_citations(session):
    sample(session)
    payload = json.loads(render_json(fetch_projects(session)))
    assert payload["schema"] == JSON_SCHEMA_TAG
    assert payload["count"] == 1
    project = payload["projects"][0]
    assert project["name"] == "Fairwater"
    assert len(project["sources"]) == 1
    assert project["sources"][0]["claims"]["mw_planned"] == 900.0
    assert project["sources"][0]["fields"]


def test_json_carries_risks_with_their_evidence(session):
    sample(session)
    project = json.loads(render_json(fetch_projects(session)))["projects"][0]
    assert len(project["risks"]) == 1
    risk = project["risks"][0]
    assert risk["category"] == "transmission"
    assert risk["severity"] == "material"
    assert risk["status"] == "open"
    assert risk["first_seen"] == "2026-02-01"
    # Both, and they are different: the summary paraphrases, the quote is verbatim.
    assert risk["summary"] == "Transmission upgrades are pending."
    assert risk["quote"] == "two 345-kilovolt upgrades"
    # `blocker` is derived from this row, so its citation has to be exportable.
    assert risk["source_id"] is not None
    assert project["blocker"] == "Transmission upgrades are pending."


def test_csv_lists_open_risks_as_category_severity_pairs(session):
    sample(
        session,
        risks=[
            RiskRecord(category="water", severity="watch", summary="a"),
            RiskRecord(category="financing", severity="blocking", summary="b"),
        ],
    )
    rows = list(csv.DictReader(io.StringIO(render_csv(fetch_projects(session)))))
    assert rows[0]["risks"] == "financing:blocking;water:watch"


def test_csv_omits_a_resolved_risk(session):
    """A flat cell has no room to say "settled", so listing one would read as
    though the project were still blocked."""
    from tracker.models import Risk

    sample(session)
    session.scalar(select(Risk)).status = "resolved"
    session.flush()
    rows = list(csv.DictReader(io.StringIO(render_csv(fetch_projects(session)))))
    assert rows[0]["risks"] == ""


def test_json_keeps_a_resolved_risk(session):
    """Unlike CSV: the nested form has a `status` field to say so, and the history
    is worth exporting."""
    from tracker.models import Risk

    sample(session)
    session.scalar(select(Risk)).status = "resolved"
    session.flush()
    project = json.loads(render_json(fetch_projects(session)))["projects"][0]
    assert [r["status"] for r in project["risks"]] == ["resolved"]


def test_json_omits_a_timestamp_unless_asked(session):
    """A timestamp in the payload would make every export differ."""
    sample(session)
    projects = fetch_projects(session)
    assert "generated_at" not in json.loads(render_json(projects))
    assert "generated_at" in json.loads(render_json(projects, generated_at="2026-01-01 00:00:00"))


def test_json_keys_are_sorted(session):
    sample(session)
    text = render_json(fetch_projects(session))
    top = [line.strip().split('"')[1] for line in text.splitlines() if line.startswith('  "')]
    assert top == sorted(top)


# --- Markdown ---------------------------------------------------------------


def test_md_has_a_table_and_a_detail_section(session):
    sample(session)
    text = render_md(fetch_projects(session))
    assert "| # | Company | Project |" in text
    assert "| Microsoft | Fairwater |" in text
    assert "## Detail" in text
    assert "### Microsoft — Fairwater (WI)" in text
    assert "https://news.microsoft.com/fairwater/" in text
    assert "Microsoft said the campus will draw 900 MW." in text


def test_md_qualifies_its_totals(session):
    """A sum over partially-cited data is a floor, and must say so."""
    sample(session)
    assert "floor" in render_md(fetch_projects(session))


def test_md_escapes_pipes_so_a_value_cannot_break_the_table(session):
    sample(session, name="Phase 1 | Phase 2")
    text = render_md(fetch_projects(session))
    assert "Phase 1 \\| Phase 2" in text

    # Split on *unescaped* pipes only: an escaped pipe is still the character
    # "|", so counting raw pipes cannot show that it stopped being a delimiter.
    data_row = text.split("| # | Company | Project |")[1].splitlines()[2]
    cells = re.split(r"(?<!\\)\|", data_row)
    assert len(cells) == 11, f"9 columns plus the empty ends, got {cells}"


def test_md_separates_operator_prose_from_machine_notes(session):
    project = sample(session)
    project.notes = (
        f"Spoke to the county planner.\n{SOURCE_NOTE_PREFIX} queue MW is generator nameplate\n"
    )
    session.flush()
    text = render_md(fetch_projects(session))
    assert "> Spoke to the county planner." in text
    assert "<details><summary>Data-quality notes</summary>" in text
    assert "queue MW is generator nameplate" in text


def test_md_has_no_details_block_when_there_are_no_machine_notes(session):
    sample(session)
    assert "<details>" not in render_md(fetch_projects(session))


def test_md_on_an_empty_database(session):
    assert "_No projects match._" in render_md([])


# --- Determinism ------------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS)
def test_export_is_byte_stable_across_runs(session, fmt):
    """The whole point: re-exporting unchanged data must produce no diff."""
    sample(session)
    projects = fetch_projects(session)
    assert render(fmt, projects) == render(fmt, projects)


@pytest.mark.parametrize("fmt", FORMATS)
def test_ordering_is_by_content_not_insertion(session, fmt):
    """Inserting a project must not reshuffle the whole export."""
    sample(session)
    upsert_record(
        session,
        IngestRecord(
            project={"company": "Google", "name": "Aardvark", "city": "Ames", "state": "IA"},
            sources=[
                SourceRecord(
                    url="https://blog.google/a",
                    source_type="company_filing",
                    fetched_at=T0,
                    claims={"company": "Google", "city": "Ames", "state": "IA"},
                )
            ],
        ),
    )
    ordered = [p.state for p in fetch_projects(session)]
    assert ordered == ["IA", "WI"], "sorted by state, then company, then name"


def test_render_rejects_an_unknown_format():
    with pytest.raises(ValueError, match="unknown format"):
        render("yaml", [])


# --- Filtering --------------------------------------------------------------


def test_export_filter_narrows_the_result(session):
    sample(session)
    upsert_record(
        session,
        IngestRecord(
            project={"company": "Google", "name": "Ames", "city": "Ames", "state": "IA"},
            sources=[
                SourceRecord(
                    url="https://blog.google/a",
                    source_type="general_media",
                    fetched_at=T0,
                    claims={"company": "Google", "city": "Ames", "state": "IA"},
                )
            ],
        ),
    )
    assert len(fetch_projects(session, ExportFilter(company="microsoft"))) == 1
    assert len(fetch_projects(session, ExportFilter(state="ia"))) == 1
    assert len(fetch_projects(session, ExportFilter(phase="construction"))) == 1
    assert len(fetch_projects(session, ExportFilter(min_confidence=2))) == 1
    assert len(fetch_projects(session, ExportFilter(min_confidence=3))) == 0


# --- Traceability -----------------------------------------------------------


def test_every_exported_fact_is_cited(session):
    """The PRD's central premise, checked at the export boundary.

    Every non-null tracked field on an exported project must appear in at least
    one of its sources' `fields` lists.
    """
    sample(session)
    payload = json.loads(render_json(fetch_projects(session)))
    for project in payload["projects"]:
        cited: set[str] = set()
        for source in project["sources"]:
            if source["fields"]:
                cited.update(f.strip() for f in source["fields"].split(","))
        populated = {f for f in TRACKED_FIELDS if project.get(f) is not None}
        assert populated <= cited, f"uncited: {populated - cited}"


def test_source_urls_are_present_for_every_row(session):
    """An exported project with no citation would violate the core promise."""
    sample(session)
    for project in fetch_projects(session):
        assert to_row(project)["source_urls"], f"project {project.id} has no source URL"


# --- File writing -----------------------------------------------------------


def test_write_export_creates_parents_and_uses_lf(tmp_path):
    target = tmp_path / "nested" / "out.md"
    write_export("a\nb\n", target)
    assert target.read_bytes() == b"a\nb\n", "no CRLF translation on Windows"


def test_write_export_with_no_path_is_a_noop():
    write_export("x", None)  # must not raise


@pytest.fixture
def html_projects(session):
    """One fully-populated project, fetched the way the exporter fetches."""
    sample(session)
    return fetch_projects(session)


# --- HTML dashboard ----------------------------------------------------------


def test_html_inlines_the_dataset_rather_than_fetching_it(html_projects):
    """It has to open by double-click.

    `fetch()` from a `file://` page is blocked as cross-origin, so a build that
    loaded a sibling .json would show an empty table unless the reader happened to
    be running a web server.
    """
    html = render_html(html_projects)
    assert '<script type="application/json" id="data">' in html
    assert html_projects[0].company in html
    assert "__DATA__" not in html, "the placeholder must be consumed"


def test_html_makes_no_external_requests(html_projects):
    """Offline and self-contained: no CDN, no webfont, no tile server.

    Anything loaded over the network would make the file useless on a plane and
    would leak the reader's interest in these projects to a third party.
    """
    html = render_html(html_projects)
    for attr in ('src="http', "src='http", 'href="http', "href='http"):
        # Citation links in the DATA payload are fine — they are content, not
        # subresources — so only markup outside the payload is checked.
        markup = html.split('id="data">')[0] + html.split("</script>")[-1]
        assert attr not in markup, f"template pulls a subresource: {attr}"
    assert "@import" not in html
    assert "fonts.googleapis" not in html


def test_html_escapes_a_script_tag_hiding_in_the_data(html_projects):
    """The HTML tokenizer ends a script element at `</script>` regardless of
    JSON or JavaScript context, so a project whose notes quote one would truncate
    the whole dataset and leave a page that cannot parse."""
    html_projects[0].notes = "trouble: </script><script>alert(1)</script>"
    html = render_html(html_projects)
    payload = html.split('id="data">')[1].split("</script>")[0]
    assert "</script" not in payload
    assert r"<\/script" in payload
    # And the payload is still valid JSON once unescaped the way a browser does.
    assert json.loads(payload.replace(r"<\/", "</"))["count"] == len(html_projects)


def test_html_is_byte_stable_across_runs(html_projects):
    assert render_html(html_projects) == render_html(html_projects)


def test_html_carries_the_tier_of_every_value(html_projects):
    """The page colours values by provenance, so it needs `basis` per project."""
    html = render_html(html_projects)
    payload = json.loads(html.split('id="data">')[1].split("</script>")[0].replace(r"<\/", "</"))
    assert "basis" in payload["projects"][0]
    assert "standing" in payload["projects"][0]


def test_html_is_a_registered_format():
    from tracker.export import FORMATS, RENDERERS

    assert "html" in FORMATS
    assert RENDERERS["html"] is render_html
