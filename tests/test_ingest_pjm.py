"""ISO queue ingest.

The fixture uses PJM's real column headers. Its most important assertions are
the ones that pin down what this path must *not* do: claim generator nameplate
as data-center load, exceed confidence 1, or merge a county-granular row into a
city-granular one.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from tracker.ingest import pjm
from tracker.ingest.iso_maps import ISO_MAPS, PJM, get_map
from tracker.models import Project, Source
from tracker.upsert import upsert_record

FIXTURE = Path(__file__).parent / "fixtures" / "pjm_queue_sample.csv"


@pytest.fixture
def report(session):
    return pjm.run(session, FIXTURE, iso="pjm")


def projects(session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.id)))


def by_name(session, fragment: str) -> Project:
    matches = [p for p in projects(session) if fragment.lower() in p.name.lower()]
    assert matches, f"no project matching {fragment!r}"
    return matches[0]


# --- Mapping registry -------------------------------------------------------


def test_every_iso_has_required_columns_and_search_columns():
    for iso, iso_map in ISO_MAPS.items():
        assert iso_map.provenance_url.startswith("https://"), iso
        assert any(spec.required for spec in iso_map.columns.values()), iso
        assert iso_map.dc_search_cols, iso
        # No public generator queue identifies load type. If one ever does, that
        # is a deliberate change, not an accident.
        assert iso_map.load_type_col is None, iso


def test_map_override_replaces_a_column_name():
    overridden = get_map("pjm", {"gen_mw": ["Max Summer MW"]})
    assert overridden.columns["gen_mw"].src == ("Max Summer MW",)
    assert PJM.columns["gen_mw"].src[0] == "MW Capacity", "base map must not mutate"


def test_map_override_rejects_an_unknown_field():
    with pytest.raises(KeyError, match="unknown field"):
        get_map("pjm", {"nonsense": ["X"]})


def test_unknown_iso_is_rejected():
    with pytest.raises(KeyError, match="unknown iso"):
        get_map("nyiso")


# --- Header assertion -------------------------------------------------------


def test_assert_headers_accepts_the_fixture():
    assert "Project ID" in pjm.assert_headers(FIXTURE, PJM)


def test_renamed_column_aborts_before_reading_rows(tmp_path: Path, session):
    """The failure mode this guards: a rename yields 0 matches and exit 0."""
    rows = FIXTURE.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace("Project ID", "Queue Ident")
    renamed = tmp_path / "renamed.csv"
    renamed.write_text("\n".join(rows), encoding="utf-8")

    with pytest.raises(pjm.HeaderError) as exc:
        pjm.run(session, renamed, iso="pjm")
    assert "missing required column" in str(exc.value)
    assert "--map-override" in str(exc.value), "the error must say how to fix it"


def test_a_renamed_column_can_be_recovered_with_map_override(tmp_path: Path, session):
    rows = FIXTURE.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace("Project ID", "Queue Ident")
    renamed = tmp_path / "renamed.csv"
    renamed.write_text("\n".join(rows), encoding="utf-8")

    result = pjm.run(session, renamed, iso="pjm", map_override={"ext_id": ["Queue Ident"]})
    assert result.inserted > 0


# --- Filtering --------------------------------------------------------------


def test_generators_without_a_data_center_signal_are_filtered_out(report, session):
    names = [p.name for p in projects(session)]
    assert not any("Nova Solar" in n for n in names)
    assert not any("Prairie Wind" in n for n in names)
    assert not any("Green Storage" in n for n in names)


def test_phrase_and_operator_matches_are_distinguished():
    phrase_row = {"Name": "Cardinal Data Center Load", "Commercial Name": "Cardinal LLC"}
    keep, reason, cap = pjm.match_data_center(phrase_row, PJM, "heuristic")
    assert keep and "phrase" in reason and cap == 1

    operator_row = {"Name": "Microsoft Mount Pleasant", "Commercial Name": "MS Mt Pleasant"}
    keep, reason, cap = pjm.match_data_center(operator_row, PJM, "heuristic")
    assert keep and "operator" in reason and cap == 1


def test_filter_none_keeps_everything():
    keep, reason, _ = pjm.match_data_center({"Name": "Prairie Wind Farm"}, PJM, "none")
    assert keep and reason == "unfiltered"


def test_a_real_load_type_column_raises_the_confidence_cap():
    """The seam for the day a genuine large-load export exists."""
    row = {"Load Type": "Data Center", "Name": "Whatever"}
    keep, reason, cap = pjm.match_data_center(row, PJM, "column:Load Type=(?i)data")
    assert keep and cap == 2, "an authoritative load-type column is not a guess"
    assert reason == "column:Load Type"


def test_column_filter_naming_a_missing_column_is_an_error():
    with pytest.raises(pjm.IsoIngestError, match="not in this file"):
        pjm.match_data_center({"Name": "X"}, PJM, "column:Load Type=(?i)data")


def test_malformed_column_filter_is_an_error():
    with pytest.raises(pjm.IsoIngestError, match="column:Load Type"):
        pjm.match_data_center({"Name": "X"}, PJM, "column:LoadType")


# --- The honesty guard ------------------------------------------------------


def test_generator_mw_is_not_written_to_mw_planned(report, session):
    """The single most consequential rule in this module.

    Queue MW is a power plant's nameplate rating. Writing it to a data center's
    `mw_planned` would fabricate the headline number the whole tracker exists to
    report.
    """
    for project in projects(session):
        assert project.mw_planned is None, f"{project.name} got generator MW as load"


def test_generator_mw_is_disclosed_in_notes(report, session):
    cardinal = by_name(session, "Cardinal")
    assert "gen_queue_mw=300" in cardinal.notes
    assert "not data-center load" in cardinal.notes
    assert "generation interconnection request" in cardinal.notes


def test_trust_gen_mw_opts_in_and_still_discloses(session):
    pjm.run(session, FIXTURE, iso="pjm", trust_gen_mw=True)
    cardinal = by_name(session, "Cardinal")
    assert cardinal.mw_planned == 300.0
    assert "--trust-gen-mw" in cardinal.notes
    assert "NOT confirmed data-center load" in cardinal.notes


def test_confidence_never_exceeds_one_from_a_queue_match(report, session):
    for project in projects(session):
        assert project.confidence <= 1, f"{project.name} scored {project.confidence}"


# --- Row-level normalization ------------------------------------------------


def _record_for(queue_id: str, **kwargs):
    """Build the IngestRecord for one fixture row, without upserting.

    Tested at this level rather than through the database because several fixture
    rows deliberately merge into one project, which would confound assertions
    about a single row's mapping.
    """
    with FIXTURE.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            if row["Project ID"] == queue_id:
                return pjm.to_record(
                    row,
                    PJM,
                    fetched_at=datetime(2026, 1, 1),
                    reason="test",
                    confidence_cap=1,
                    file_digest="deadbeef",
                    lineno=lineno,
                    **{"trust_gen_mw": True, **kwargs},
                )
    raise AssertionError(f"fixture has no row {queue_id}")


@pytest.mark.parametrize(
    ("queue_id", "expected", "which_column"),
    [
        ("AG1-001", 300.0, "MW Capacity"),
        ("AG1-002", 250.0, "MW Energy (MW Capacity is blank)"),
        ("AG1-012", 180.0, "MFO only"),
        ("AG1-005", 1000.0, "MW Capacity with a thousands separator"),
    ],
)
def test_capacity_falls_back_through_candidate_columns(queue_id, expected, which_column):
    """PJM populates MW Capacity, MW Energy and MFO inconsistently."""
    record = _record_for(queue_id)
    assert record.sources[0].claims["mw_planned"] == expected, which_column


def test_capacity_is_absent_from_claims_without_trust_gen_mw():
    """The honesty guard, asserted at the record level."""
    record = _record_for("AG1-001", trust_gen_mw=False)
    assert "mw_planned" not in record.sources[0].claims
    assert any("gen_queue_mw=300" in n for n in record.notes)


def test_thousands_separator_is_parsed(session):
    pjm.run(session, FIXTURE, iso="pjm", trust_gen_mw=True)
    assert by_name(session, "QTS").mw_planned == 1000.0


def test_full_state_name_is_normalized(report, session):
    """Row AG1-004 spells the state 'Virginia'."""
    aligned = by_name(session, "Aligned")
    assert aligned.state == "VA"


def test_county_populates_county_not_city(report, session):
    cardinal = by_name(session, "Cardinal")
    assert cardinal.county == "Licking"
    assert cardinal.city is None, "a queue never tells us the municipality"


def test_county_suffix_is_handled(report, session):
    """Row AG1-008 writes 'Racine County' where others write 'Racine'."""
    campus = by_name(session, "Microsoft Mount Pleasant")
    # AG1-002 wins the FILL_ONLY county ("Racine"); AG1-008 says "Racine
    # County". Both normalize to the same key, which is the point.
    assert campus.county == "Racine"
    assert campus.dedup_key == "microsoft|county:racine|WI"


def test_unparseable_capacity_rejects_only_that_row(session):
    """Row AG1-006 has MW Capacity 'TBD'.

    'TBD' is a documented null sentinel, so the row survives with no capacity
    rather than being rejected -- losing a whole project over an unfilled cell
    would be worse than recording it without a number.
    """
    result = pjm.run(session, FIXTURE, iso="pjm")
    assert result.rejected == 0
    vantage = by_name(session, "Vantage")
    assert vantage.mw_planned is None


def test_unknown_status_leaves_phase_defaulted_and_says_so(report, session):
    """Row AG1-007 has Status 'Unknown Blah'."""
    meta = by_name(session, "Meta Platforms")
    assert meta.phase == "announced"
    assert "did not map to a known phase" in meta.notes


def test_unmapped_phase_is_not_claimed_as_a_cited_fact(report, session):
    """A defaulted phase must be absent from source.fields.

    Otherwise the coverage rule in confidence.py counts a value nobody stated.
    """
    meta = by_name(session, "Meta Platforms")
    for source in meta.sources:
        assert "phase" not in (source.fields or "").split(",")


def test_withdrawal_date_means_cancelled(report, session):
    assert by_name(session, "CoreWeave").phase == "cancelled"


def test_actual_in_service_date_means_operational(report, session):
    assert by_name(session, "Equinix").phase == "operational"


def test_queue_status_maps_to_phase(report, session):
    assert by_name(session, "QTS").phase == "construction"  # Engineering and Procurement
    assert by_name(session, "Cardinal").phase == "permitting"  # Under Study
    assert by_name(session, "Applied Digital").phase == "paused"  # Suspended


def test_submitted_and_projected_dates_are_parsed(report, session):
    cardinal = by_name(session, "Cardinal")
    assert str(cardinal.first_announced) == "2025-04-11"
    assert str(cardinal.expected_online) == "2027-12-01"


# --- Citations --------------------------------------------------------------


def test_source_url_is_row_unique_and_resolvable(report, session):
    sources = list(session.scalars(select(Source)))
    assert sources
    for source in sources:
        assert source.url.startswith(PJM.provenance_url + "#")
        assert source.source_type == "iso_queue"
    # Row-unique, so the (project_id, url) constraint does not collapse rows.
    assert len({s.url for s in sources}) == len(sources)


def test_excerpt_renders_the_row_deterministically(report, session):
    """A CSV row has no prose, so the citation quotes the row itself."""
    cardinal = by_name(session, "Cardinal")
    excerpt = cardinal.sources[0].excerpt
    assert "PJM queue AG1-001" in excerpt
    assert "MW=300" in excerpt
    assert "fuel=Natural Gas" in excerpt
    assert len(excerpt) <= 500


def test_extractor_records_the_file_and_row(report, session):
    """So "which file and line produced this?" is answerable later."""
    extractor = by_name(session, "Cardinal").sources[0].extractor
    assert extractor.startswith("pjm:iso_maps-v1:sha256=")
    assert extractor.endswith(":row=2")


def test_source_fields_are_derived_from_claims(report, session):
    for source in session.scalars(select(Source)):
        claimed = set(json.loads(source.claims))
        assert set(source.fields.split(",")) == claimed


# --- Dedup ------------------------------------------------------------------


def test_duplicate_queue_id_does_not_duplicate_the_project(report, session):
    """AG1-002 appears twice in the fixture."""
    matches = [p for p in projects(session) if p.dedup_key == "microsoft|county:racine|WI"]
    assert len(matches) == 1


def test_two_spellings_of_one_site_collapse_to_one_project(report, session):
    """AG1-002 'MS Mt Pleasant' and AG1-008 'Microsoft Racine County'.

    The PRD names this exact pair as a High risk. Neither commercial name is
    usable as an operator, so `_infer_company` recovers "microsoft" from the
    keyword match and both rows land on microsoft|county:racine|WI as one project
    with two citations.
    """
    racine = [p for p in projects(session) if p.dedup_key == "microsoft|county:racine|WI"]
    assert len(racine) == 1, f"expected one Racine project, got {[p.name for p in racine]}"
    assert len(racine[0].sources) == 2, "both queue rows must survive as citations"
    assert {s.url.rsplit("#", 1)[1] for s in racine[0].sources} == {"AG1-002", "AG1-008"}


def test_inferred_company_is_disclosed(report, session):
    """A heuristic that changes project identity has to say it did."""
    racine = next(p for p in projects(session) if p.dedup_key == "microsoft|county:racine|WI")
    assert "company inferred as 'Microsoft'" in racine.notes


def test_company_falls_back_when_no_operator_is_recognized(session):
    """A phrase-matched row with an unknown operator keeps the queue's own name."""
    pjm.run(session, FIXTURE, iso="pjm")
    cardinal = by_name(session, "Cardinal")
    assert cardinal.company == "Cardinal Data Center LLC"
    assert "company inferred" not in (cardinal.notes or "")


def test_county_row_does_not_merge_with_a_city_row(session):
    """A news-sourced city row and a queue-sourced county row stay separate."""
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.models import utcnow

    claims = {"name": "Fairwater", "company": "Microsoft", "city": "Racine", "state": "WI"}
    upsert_record(
        session,
        IngestRecord(
            project={"company": "Microsoft", "name": "Fairwater", "city": "Racine", "state": "WI"},
            sources=[
                SourceRecord(
                    url="https://news.microsoft.com/x",
                    source_type="company_filing",
                    fetched_at=utcnow(),
                    claims=claims,
                )
            ],
        ),
    )
    before = len(projects(session))

    pjm.run(session, FIXTURE, iso="pjm")
    keys = {p.dedup_key for p in projects(session)}
    assert "microsoft|city:racine|WI" in keys
    assert "microsoft|county:racine|WI" in keys
    assert len(projects(session)) > before


# --- Run mechanics ----------------------------------------------------------


def test_report_counts_are_exact(report):
    assert report.read == 14
    assert report.filtered == 3  # Nova Solar, Prairie Wind, Green Storage
    assert report.rejected == 0
    # 11 matched rows, of which AG1-002 appears twice and AG1-008 shares its
    # dedup key, so 9 distinct projects.
    assert report.inserted == 9
    assert report.unchanged + report.updated == 2


def test_reingest_changes_nothing(session):
    pjm.run(session, FIXTURE, iso="pjm")
    second = pjm.run(session, FIXTURE, iso="pjm")
    assert second.inserted == 0
    assert second.updated == 0


def test_limit_stops_early(session):
    result = pjm.run(session, FIXTURE, iso="pjm", limit=3)
    assert result.read == 3


def test_dry_run_writes_nothing(session):
    result = pjm.run(session, FIXTURE, iso="pjm", dry_run=True)
    assert result.inserted > 0, "the report still describes what would happen"
    assert projects(session) == []


def test_batching_commits_in_chunks(session, monkeypatch):
    """Proves the chunking actually chunks rather than accumulating everything."""
    monkeypatch.setattr(pjm, "CHUNK", 2)
    commits = {"n": 0}
    original = session.commit

    def counting_commit():
        commits["n"] += 1
        original()

    monkeypatch.setattr(session, "commit", counting_commit)
    pjm.run(session, FIXTURE, iso="pjm")
    # 11 matched rows in batches of 2 -> 5 full flushes plus a final partial.
    assert commits["n"] == 6


def test_zero_matches_is_a_loud_failure(tmp_path: Path, session):
    """A run that matches nothing must not look like a successful run."""
    only_solar = tmp_path / "solar.csv"
    with (
        FIXTURE.open(encoding="utf-8") as src,
        only_solar.open("w", encoding="utf-8", newline="") as dst,
    ):
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerow(next(r for r in reader if "Nova Solar" in r["Name"]))

    with pytest.raises(pjm.IsoIngestError) as exc:
        pjm.run(session, only_solar, iso="pjm")
    assert "matched none" in str(exc.value)


def test_high_reject_rate_is_a_loud_failure(tmp_path: Path, session):
    """A mostly-unparseable file means the mapping is wrong."""
    bad = tmp_path / "bad.csv"
    with FIXTURE.open(encoding="utf-8") as src:
        reader = csv.DictReader(src)
        rows = list(reader)
        fieldnames = reader.fieldnames
    for row in rows:
        row["State"] = "Xanadu"
    with bad.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(pjm.IsoIngestError) as exc:
        pjm.run(session, bad, iso="pjm")
    assert "failed normalization" in str(exc.value)


def test_rejects_are_written_as_replayable_jsonl(tmp_path: Path, session, monkeypatch):
    # Raise the ceiling: this test is about the reject file, not the ceiling,
    # and one bad row out of eleven already exceeds the 5% default.
    monkeypatch.setattr(pjm, "MAX_REJECT_RATE", 0.5)

    bad = tmp_path / "one_bad.csv"
    with FIXTURE.open(encoding="utf-8") as src:
        reader = csv.DictReader(src)
        rows = list(reader)
        fieldnames = reader.fieldnames
    rows[0]["State"] = "Xanadu"
    with bad.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    out = tmp_path / "rejects.jsonl"
    result = pjm.run(session, bad, iso="pjm", rejects_out=out)
    assert result.rejected == 1
    payload = json.loads(out.read_text(encoding="utf-8").strip())
    assert payload["field"] == "state"
    assert payload["value"] == "Xanadu"
    assert payload["row"]["Project ID"] == "AG1-001"


def test_utf8_bom_is_tolerated(tmp_path: Path, session):
    """ISO exports routinely ship with a BOM."""
    with_bom = tmp_path / "bom.csv"
    with_bom.write_bytes(b"\xef\xbb\xbf" + FIXTURE.read_bytes())
    assert pjm.run(session, with_bom, iso="pjm").inserted == 9


def test_empty_file_is_reported_clearly(tmp_path: Path, session):
    empty = tmp_path / "empty.csv"
    empty.write_text("Project ID,Name,State\n", encoding="utf-8")
    with pytest.raises(pjm.HeaderError, match="no data rows"):
        pjm.run(session, empty, iso="pjm")


def test_json_payload_is_read(tmp_path: Path, session):
    """MISO publishes an API response rather than a file export."""
    payload = {
        "projects": [
            {
                "projectNumber": "M-1",
                "projectName": "Meta Data Center",
                "county": "Jasper",
                "state": "IA",
                "summerNetMW": 200,
                "fuelType": "Solar",
                "applicationStatus": "Active",
                "queueDate": "2025-01-15",
            }
        ]
    }
    path = tmp_path / "miso.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = pjm.run(session, path, iso="miso")
    assert result.inserted == 1
    project = projects(session)[0]
    assert project.county == "Jasper"
    assert project.state == "IA"
    assert project.mw_planned is None, "generator MW must stay out of mw_planned"
