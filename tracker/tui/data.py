"""One read of the database, and the formatting every pane shares.

The payload comes from `webui.dataset.build` — the same function the console's
`/api/dataset` returns — rather than from queries written here. Two reasons, and
the second is the one that matters: it is one round trip for everything a pane
needs, and it means the TUI and the console cannot disagree about a number. A
third implementation of "what is in the campus total" would eventually differ from
the other two and nothing would say when.

Coverage is fetched separately because `dataset.build` does not carry it: the
console has no coverage page yet, and adding a key to that payload to serve this
one would ship 73 operators to every browser that asks for projects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.text import Text

#: Provenance markers, matching what the CLI prints. A value is not just a number:
#: `derived` came from a Census lookup nobody said out loud, `unconfirmed` was
#: extracted and could not be quoted, `inferred` is a model's judgement. Rendering
#: them identically is how a spreadsheet ends up quoting an inference.
TIER_STYLE: dict[str, str] = {
    "reported": "green",
    "derived": "cyan",
    "unconfirmed": "yellow",
    "inferred": "magenta",
    "defaulted": "dim",
}

#: Phase colours, warm as a project gets closer to serving traffic.
PHASE_STYLE: dict[str, str] = {
    "announced": "blue",
    "permitting": "cyan",
    "construction": "yellow",
    "operational": "green",
    "cancelled": "red",
    "paused": "red",
}

SEVERITY_STYLE: dict[str, str] = {
    "blocking": "bold red",
    "material": "yellow",
    "watch": "dim",
}

STATUS_STYLE: dict[str, str] = {
    "absent": "red",
    "thin": "yellow",
    "covered": "green",
}

NA = Text("-", style="dim")


def fmt_mw(value: float | None) -> Text:
    if not value:
        return NA
    return Text(f"{value:,.0f}")


def fmt_usd(value: int | float | None) -> Text:
    """Trillions, billions and millions. Never thousands: nothing here is small.

    The trillion arm is not hypothetical — announced investment across the tracked
    fleet passes $2.9T, and rendering that as "2,992.1B" makes a reader do the
    division to notice.
    """
    if not value:
        return NA
    if value >= 1_000_000_000_000:
        return Text(f"${value / 1_000_000_000_000:.2f}T")
    if value >= 1_000_000_000:
        return Text(f"${value / 1_000_000_000:.1f}B")
    if value >= 1_000_000:
        return Text(f"${value / 1_000_000:.0f}M")
    return Text(f"${value:,.0f}")


def fmt_count(value: int | None) -> Text:
    return Text(f"{value:,}") if value else NA


def bar(value: float, total: float, width: int = 14, style: str = "green") -> Text:
    """A proportion, drawn. Empty is drawn as empty rather than omitted.

    Bars are the whole argument for this interface over the CLI's tables: "9 of 12
    fields" is a number you have to compare by hand across thirty rows, and a bar
    is a shape you compare at a glance.
    """
    if total <= 0:
        return Text("·" * width, style="dim")
    filled = max(0, min(width, round(width * value / total)))
    out = Text("█" * filled, style=style)
    out.append("·" * (width - filled), style="dim")
    return out


def confidence_cell(value: int | None) -> Text:
    """1..3, coloured. One source can never reach 3 however authoritative."""
    score = value or 0
    style = "green" if score >= 3 else "yellow" if score == 2 else "red"
    return Text("●" * score + "○" * (3 - score), style=style)


def tier_of(project: dict[str, Any], field_name: str) -> str:
    """The tier behind one field, from the payload's `basis` map."""
    basis = project.get("basis") or {}
    return str(basis.get(field_name) or "")


def value_with_tier(project: dict[str, Any], field_name: str, rendered: Text) -> Text:
    """A value, marked with what it rests on. Absence stays absent."""
    tier = tier_of(project, field_name)
    if not tier or tier == "reported":
        return rendered
    out = rendered.copy()
    out.append(f" {tier}", style=TIER_STYLE.get(tier, "dim"))
    return out


@dataclass
class Snapshot:
    """Everything the read panes render, as of one moment."""

    payload: dict[str, Any] = field(default_factory=dict)
    coverage: Any = None
    db_path: str = ""
    schema_version: int = 0
    version: str = ""
    #: Anything that went wrong loading. Rendered rather than raised: a coverage
    #: file with a typo in it must not take the projects table down with it.
    problems: list[str] = field(default_factory=list)

    @property
    def projects(self) -> list[dict[str, Any]]:
        return self.payload.get("projects") or []

    @property
    def totals(self) -> dict[str, Any]:
        return self.payload.get("totals") or {}

    @property
    def gaps(self) -> dict[str, Any]:
        return self.payload.get("gaps") or {}

    @property
    def capex(self) -> dict[str, Any]:
        return self.payload.get("capex") or {}

    @property
    def queue(self) -> list[dict[str, Any]]:
        return self.payload.get("queue") or []

    @property
    def failed(self) -> list[dict[str, Any]]:
        return self.payload.get("failed") or []

    @property
    def exposure(self) -> list[dict[str, Any]]:
        return self.payload.get("exposure") or []

    def project(self, project_id: int) -> dict[str, Any] | None:
        return next((p for p in self.projects if p.get("id") == project_id), None)

    @classmethod
    def load(cls, db_path: Path) -> Snapshot:
        """Read once, in a worker thread. Never on the event loop."""
        from tracker import __version__
        from tracker.db import open_db, schema_version, session_scope
        from tracker.webui import dataset

        snapshot = cls(db_path=str(db_path), version=__version__)
        engine = open_db(db_path)
        with session_scope(engine, commit=False) as session:
            snapshot.schema_version = schema_version(engine)
            snapshot.payload = dataset.build(
                session, db_path=str(db_path), schema_version=snapshot.schema_version
            )
            # Separately, and tolerantly: the roster is a hand-edited file, so a
            # bad entry is a message in the coverage pane rather than a TUI that
            # will not start.
            try:
                from tracker import roster as roster_mod

                snapshot.coverage = roster_mod.measure(session)
            except Exception as exc:  # reported in the pane, not swallowed
                snapshot.problems.append(f"coverage unavailable: {exc}")
        return snapshot


__all__ = [
    "NA",
    "PHASE_STYLE",
    "SEVERITY_STYLE",
    "STATUS_STYLE",
    "TIER_STYLE",
    "Snapshot",
    "bar",
    "confidence_cell",
    "fmt_count",
    "fmt_mw",
    "fmt_usd",
    "tier_of",
    "value_with_tier",
]
