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

from tracker import blocks as blocks_mod
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
#: 4 adds per-field `prov` (tier + the sentence behind the value) and `quotes` on
#: each source. Both are additive; `basis` is unchanged for readers keying on it.
JSON_SCHEMA_TAG = "tracker/4"

FORMATS = ("md", "csv", "json", "html")


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
        selectinload(Project.blocks),
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
        "h200_equivalent": project.h200_equivalent,
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


def _standing_json(project: Project) -> dict[str, Any]:
    """Per-track position, the binding constraint, and the signal to watch.

    Derived, not stored, so it is computed here rather than read from a column. A
    consumer — a web page, a spreadsheet, another script — should not have to
    reimplement the reasoning in `tracker.tracks` to know where a project stands.
    """
    from tracker.tracks import standing

    stand = standing(project.id, project.events, project.risks)
    binding = stand.binding_blocker
    return {
        "tracks": [
            {
                "track": state.track,
                "status": state.status,
                "reached": list(state.reached),
                "implied": sorted(state.implied),
                "complete": state.complete,
                "blockers": list(state.blockers),
                "blocker_severity": state.blocker_severity,
                "next_milestone": state.next_milestone,
            }
            for state in stand.tracks
        ],
        "binding_blocker": binding.track if binding else None,
        "watch_for": stand.watch_for,
    }


def _provenance_json(project: Project) -> tuple[dict[str, str], dict[str, Any]]:
    """Per-field tier, and the evidence behind it.

    The PRD's central rule is that a model's answer must not be taken as fact, so a
    machine-readable consumer needs this as much as the terminal does. Without it a
    web page would render a 待确认 guess identically to a quoted figure.

    Two shapes, because they answer different questions and cost different amounts:
    ``basis`` is the flat field -> tier map consumers have keyed on since schema 2
    and is kept unchanged; ``prov`` adds the sentence, whether that sentence is the
    one recorded for this field or the source's whole excerpt, and which citation
    it came from.
    """
    from tracker.gaps import provenance
    from tracker.vocab import WRITABLE_FIELDS

    basis_out: dict[str, str] = {}
    prov_out: dict[str, Any] = {}
    for field in WRITABLE_FIELDS:
        result = provenance(project, field)
        if result is None:
            continue
        basis_out[field] = result.tier
        prov_out[field] = {
            "tier": result.tier,
            "quote": result.quote,
            "quote_is_exact": result.quote_is_exact,
            "source_url": result.source_url,
            "source_index": result.source_index,
        }
        # The claim envelope, only when it says something. Emitted inside `prov`
        # rather than as a sibling map because the axes qualify the value the way
        # the quote does, and every consumer that wants one wants the other —
        # keeping them together is what stops a figure and its hedge being
        # rendered from two different places and drifting apart.
        if result.axes:
            prov_out[field]["axes"] = dict(result.axes)
    return basis_out, prov_out


def to_json_object(project: Project) -> dict[str, Any]:
    """Nested dict per project, preserving the citation structure."""
    basis_map, prov_map = _provenance_json(project)
    return {
        "basis": basis_map,
        "prov": prov_map,
        "standing": _standing_json(project),
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
        "h200_equivalent": project.h200_equivalent,
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
                "unconfirmed_fields": s.unconfirmed_fields,
                "claims": json.loads(s.claims) if s.claims else None,
                # Parallel to `claims`: the same field names, mapped to the
                # sentence that got each value through the evidence gate. NULL for
                # every citation stored before migration 0007.
                "quotes": json.loads(s.quotes) if s.quotes else None,
                "extractor": s.extractor,
            }
            for s in sorted(project.sources, key=lambda s: s.url)
        ],
        "events": [
            {
                "event_date": _iso(e.event_date),
                "event_type": e.event_type,
                "description": e.description,
                # The sentence the milestone stands on, and why there is none.
                # NULL `unconfirmed` means the gate verified the quote — a claim
                # that is only ever true for rows written after migration 0017,
                # because the backfill marked every older one `no_quote`.
                "quote": e.quote,
                "unconfirmed": e.unconfirmed,
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
        # A consumer asking "what state is this campus in" cannot answer from
        # `phase` and `mw_planned` alone once a campus is partly live, which most
        # now are. `mw_counted` says whether this tranche's capacity is inside
        # `mw_planned` above or excluded as 待确认 — without it the numbers appear
        # not to add up.
        "blocks": [
            {
                "block_key": b.block_key,
                "label": b.label,
                "parent": b.parent,
                "generic": bool(b.generic),
                "mw": b.mw,
                "mw_counted": blocks_mod.mw_is_confirmed(b),
                "status": b.status,
                "customer": b.customer,
                "expected_online": _iso(b.expected_online),
                "energized_on": _iso(b.energized_on),
                "investment_usd": b.investment_usd,
                "quotes": json.loads(b.quotes) if b.quotes else None,
                "unconfirmed_fields": b.unconfirmed_fields,
                "source_id": b.source_id,
            }
            for b in sorted(project.blocks, key=lambda b: b.block_key)
        ],
        # Sent rather than recomputed in the page. The console could add these up
        # itself, and then there would be two definitions of "what is in the campus
        # total" free to disagree — the objection `webui/dataset.py` opens with.
        "accounting": _accounting_json(project),
    }


def _accounting_json(project: Project) -> dict[str, Any] | None:
    """Every megawatt of the campus on one line each, or None with no tranches."""
    if not project.blocks:
        return None
    got = blocks_mod.account(project)
    return {
        "total": got.total,
        "total_is_floor": got.total_is_floor,
        "counted_mw": got.counted_mw,
        "closes": got.closes,
        "residuals": [
            {"reason": r.reason, "mw": r.mw, "labels": list(r.labels), "note": r.note}
            for r in got.residuals
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


#: Where the dataset is spliced into the template.
_HTML_DATA_TOKEN = "__DATA__"


def template_path() -> Path:
    """The dashboard template, resolved next to the installed package."""
    from tracker.config import install_root

    return install_root() / "tracker" / "templates" / "dashboard.html"


def render_html(projects: Sequence[Project], *, generated_at: str | None = None) -> str:
    """One self-contained HTML file: the dataset inlined into the template.

    Inlined rather than fetched from a sibling `.json` deliberately. `fetch()` from
    a `file://` page is blocked as cross-origin, so a two-file build would open to
    an empty table unless the reader happened to be running a web server — which
    defeats the point of a deliverable someone can double-click.

    `</script>` inside the payload has to be broken up: the HTML tokenizer ends a
    script element at that byte sequence regardless of JSON or JavaScript context,
    so a project whose notes quoted a script tag would truncate the whole dataset
    and produce a page that fails to parse.
    """
    payload = json.dumps(
        {
            "schema": JSON_SCHEMA_TAG,
            "count": len(projects),
            "projects": [to_json_object(p) for p in projects],
            **({"generated_at": generated_at} if generated_at else {}),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = payload.replace("</", "<\\/")

    template = template_path().read_text(encoding="utf-8")
    if _HTML_DATA_TOKEN not in template:
        raise ValueError(f"{template_path().name} has no {_HTML_DATA_TOKEN} placeholder")
    return template.replace(_HTML_DATA_TOKEN, payload, 1)


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
            ("H200-equivalent", _fmt_number(p.h200_equivalent) or None),
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


RENDERERS = {"md": render_md, "csv": render_csv, "json": render_json, "html": render_html}


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
    "render_html",
    "render_json",
    "render_md",
    "to_json_object",
    "to_row",
    "write_export",
]
