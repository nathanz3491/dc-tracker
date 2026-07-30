"""Export the database to Markdown, CSV or JSON.

Determinism is the design constraint. These files get diffed, re-generated and
pasted into chat, so running the same export twice on unchanged data must produce
byte-identical output:

* a fixed ``ORDER BY``, never the database's natural order;
* a frozen CSV header tuple, because downstream consumers depend on column order;
* ``lineterminator="\\n"`` — Python's csv module writes ``\\r\\n`` by default, and
  on Windows that becomes ``\\r\\r\\n``;
* ``sort_keys=True`` for JSON;
* no timestamp inside the payload unless the caller passes one in.

The renderers are pure functions of rows to strings, so the golden-file tests do
not need a database at all.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker.models import Project
from tracker.upsert import NOTE_PREFIX, SOURCE_NOTE_PREFIX

#: CSV columns, frozen. Appending is safe for consumers; reordering or removing
#: is not, so a test asserts this tuple exactly.
CSV_COLUMNS: tuple[str, ...] = (
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
    # Appended, not slotted in beside `blocker` where it reads better: this tuple
    # is a positional contract, and moving `confidence` along by one would break a
    # consumer indexing into the row.
    "risks",
)

#: Schema tag on JSON exports, so a downstream consumer can detect a change.
#: Bumped to 2 when `risks` was added to both formats — appending a CSV column is
#: safe for consumers, but a reader keying on the tag should still be able to tell.
JSON_SCHEMA_TAG = "tracker/2"

FORMATS = ("md", "csv", "json")


@dataclass(frozen=True)
class ExportFilter:
    company: str | None = None
    state: str | None = None
    phase: str | None = None
    min_confidence: int | None = None

    def apply(self, stmt):
        from sqlalchemy import func

        if self.company:
            stmt = stmt.where(func.lower(Project.company).like(f"%{self.company.lower()}%"))
        if self.state:
            stmt = stmt.where(Project.state == self.state.upper())
        if self.phase:
            stmt = stmt.where(Project.phase == self.phase)
        if self.min_confidence is not None:
            stmt = stmt.where(Project.confidence >= self.min_confidence)
        return stmt


def fetch_projects(session: Session, flt: ExportFilter | None = None) -> list[Project]:
    """Projects in a stable order, with sources, events and risks eagerly loaded.

    The ordering is by content, not by id, so inserting a project does not
    reshuffle the whole export and produce a noisy diff.
    """
    stmt = select(Project).options(
        selectinload(Project.sources),
        selectinload(Project.events),
        selectinload(Project.risks),
    )
    if flt is not None:
        stmt = flt.apply(stmt)
    stmt = stmt.order_by(Project.state, Project.company, Project.name, Project.id)
    return list(session.scalars(stmt))


# --- Row shaping ------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return str(value)


def _sorted_risks(project: Project) -> list:
    """Risks in a content-stable order, so exports stay byte-identical."""
    return sorted(project.risks, key=lambda r: (r.category, str(r.first_seen or ""), r.status))


def _risk_cell(project: Project) -> str:
    """Open risks as ``category:severity`` pairs, for the flat formats.

    Only open ones: a resolved obstacle is history, and a flat cell has no room to
    say so without reading as though the project were still blocked.
    """
    return ";".join(
        f"{r.category}:{r.severity}" for r in _sorted_risks(project) if r.status == "open"
    )


def to_row(project: Project) -> dict[str, Any]:
    """One flat dict per project, for CSV and Markdown."""
    urls = sorted(s.url for s in project.sources)
    return {
        "id": project.id,
        "company": project.company,
        "name": project.name,
        "customer": project.customer,
        "city": project.city,
        "county": project.county,
        "state": project.state,
        "country": project.country,
        "phase": project.phase,
        "mw_planned": project.mw_planned,
        "mw_built": project.mw_built,
        "investment_usd": project.investment_usd,
        "first_announced": _iso(project.first_announced),
        "expected_online": _iso(project.expected_online),
        "blocker": project.blocker,
        "risks": _risk_cell(project),
        "confidence": project.confidence,
        "sources": len(urls),
        "source_urls": " ".join(urls),
        "last_verified_at": _iso(project.last_verified_at),
    }


def to_json_object(project: Project) -> dict[str, Any]:
    """Nested dict per project, preserving the citation structure."""
    return {
        "id": project.id,
        "name": project.name,
        "company": project.company,
        "customer": project.customer,
        "city": project.city,
        "county": project.county,
        "state": project.state,
        "country": project.country,
        "lat": project.lat,
        "lon": project.lon,
        "mw_planned": project.mw_planned,
        "mw_built": project.mw_built,
        "investment_usd": project.investment_usd,
        "phase": project.phase,
        "first_announced": _iso(project.first_announced),
        "expected_online": _iso(project.expected_online),
        "blocker": project.blocker,
        "notes": project.notes,
        "confidence": project.confidence,
        "dedup_key": project.dedup_key,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "last_verified_at": _iso(project.last_verified_at),
        "sources": [
            {
                "url": s.url,
                "source_type": s.source_type,
                "fetched_at": _iso(s.fetched_at),
                "excerpt": s.excerpt,
                "fields": s.fields,
                "claims": json.loads(s.claims) if s.claims else None,
                "extractor": s.extractor,
            }
            for s in sorted(project.sources, key=lambda s: s.url)
        ],
        "events": [
            {
                "event_date": _iso(e.event_date),
                "event_type": e.event_type,
                "description": e.description,
                "source_id": e.source_id,
            }
            for e in sorted(project.events, key=lambda e: (e.event_date, e.event_type))
        ],
        "risks": [
            {
                "category": r.category,
                "severity": r.severity,
                "status": r.status,
                # `summary` may be a paraphrase; `quote` is the verified verbatim
                # sentence. A consumer that needs evidence wants the quote.
                "summary": r.summary,
                "quote": r.quote,
                "first_seen": _iso(r.first_seen),
                "resolved_at": _iso(r.resolved_at),
                "delay_days": r.delay_days,
                "source_id": r.source_id,
            }
            for r in _sorted_risks(project)
        ],
    }


# --- Renderers --------------------------------------------------------------


def _fmt_number(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return f"{value:,}"


def _fmt_usd(value: int | None) -> str:
    if value is None:
        return ""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B".replace(".00B", "B")
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    return f"${value:,}"


def _md_escape(text: str | None) -> str:
    """Escape pipes so a value containing one cannot break the table."""
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_csv(projects: Sequence[Project]) -> str:
    """CSV with a frozen header and LF line endings."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for project in projects:
        row = to_row(project)
        writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in CSV_COLUMNS})
    return buffer.getvalue()


def render_json(projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    """JSON with sorted keys and full nested citations.

    ``generated_at`` is omitted unless supplied, so the default output is
    reproducible; the CLI passes a timestamp when writing to a file.
    """
    payload: dict[str, Any] = {
        "schema": JSON_SCHEMA_TAG,
        "count": len(projects),
        "projects": [to_json_object(p) for p in projects],
    }
    if generated_at:
        payload["generated_at"] = generated_at
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_md(projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    """Markdown: a summary table, then one section per project with its citations.

    The table is designed to be pasted somewhere with limited width, so it carries
    the headline facts only and the per-project sections carry the evidence.
    """
    lines: list[str] = ["# US data center projects", ""]
    if generated_at:
        lines += [f"_Generated {generated_at}._", ""]

    if not projects:
        lines += ["_No projects match._", ""]
        return "\n".join(lines)

    cited = sum(1 for p in projects if p.mw_planned is not None)
    total_mw = sum(p.mw_planned or 0 for p in projects)
    total_usd = sum(p.investment_usd or 0 for p in projects)
    lines += [
        f"{len(projects)} project(s). "
        f"Planned capacity {_fmt_number(total_mw)} MW across {cited} project(s) that cite one; "
        f"announced investment {_fmt_usd(total_usd) or '$0'}.",
        "",
        "Totals cover only projects where the figure is cited, so they are a floor, "
        "not a sum of the industry.",
        "",
        "| # | Company | Project | Location | Phase | MW | Investment | Online | Conf |",
        "|---|---------|---------|----------|-------|---:|-----------:|--------|:----:|",
    ]
    for p in projects:
        location = _md_escape(
            f"{p.city}, {p.state}"
            if p.city
            else (f"{p.county}, {p.state}" if p.county else p.state)
        )
        lines.append(
            f"| {p.id} | {_md_escape(p.company)} | {_md_escape(p.name)} | {location} "
            f"| {p.phase} | {_fmt_number(p.mw_planned)} | {_fmt_usd(p.investment_usd)} "
            f"| {_iso(p.expected_online) or ''} | {p.confidence} |"
        )

    lines += ["", "## Detail", ""]
    for p in projects:
        lines += [f"### {p.company} — {p.name} ({p.state})", ""]
        facts = [
            ("Customer", p.customer),
            ("Location", f"{p.city}, {p.state}" if p.city else f"{p.county} (county), {p.state}"),
            ("Phase", p.phase),
            ("Planned MW", _fmt_number(p.mw_planned) or None),
            ("Built MW", _fmt_number(p.mw_built) or None),
            ("Investment", _fmt_usd(p.investment_usd) or None),
            ("First announced", _iso(p.first_announced)),
            ("Expected online", _iso(p.expected_online)),
            ("Blocker", p.blocker),
            ("Confidence", f"{p.confidence}/3"),
        ]
        lines += [f"- **{label}:** {value}" for label, value in facts if value]

        auto = [
            line
            for line in (p.notes or "").splitlines()
            if line.startswith((NOTE_PREFIX, SOURCE_NOTE_PREFIX))
        ]
        human = [
            line
            for line in (p.notes or "").splitlines()
            if line.strip() and not line.startswith((NOTE_PREFIX, SOURCE_NOTE_PREFIX))
        ]
        if human:
            lines += ["", "Notes:", ""] + [f"> {line}" for line in human]
        if auto:
            lines += ["", "<details><summary>Data-quality notes</summary>", ""]
            lines += [f"- {line}" for line in auto]
            lines += ["", "</details>"]

        lines += ["", "Sources:", ""]
        for s in sorted(p.sources, key=lambda s: s.url):
            lines.append(f"- [{s.source_type}] <{s.url}> — supports: {s.fields or 'nothing'}")
            if s.excerpt:
                lines.append(f"  > {_md_escape(s.excerpt)}")

        if p.events:
            lines += ["", "Timeline:", ""]
            for e in sorted(p.events, key=lambda e: (e.event_date, e.event_type)):
                lines.append(f"- {_iso(e.event_date)} — **{e.event_type}** — {e.description}")
        lines.append("")

    return "\n".join(lines)


RENDERERS = {"md": render_md, "csv": render_csv, "json": render_json}


def render(fmt: str, projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    if fmt not in RENDERERS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    if fmt == "csv":
        return render_csv(projects)
    return RENDERERS[fmt](projects, generated_at=generated_at)


def write_export(text: str, out: Path | None) -> None:
    """Write to a file, or leave it to the caller to print to stdout.

    Always UTF-8 with LF endings, so a Windows export is byte-identical to one
    produced anywhere else.
    """
    if out is None:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")


__all__ = [
    "CSV_COLUMNS",
    "FORMATS",
    "JSON_SCHEMA_TAG",
    "RENDERERS",
    "ExportFilter",
    "fetch_projects",
    "render",
    "render_csv",
    "render_json",
    "render_md",
    "to_json_object",
    "to_row",
    "write_export",
]
