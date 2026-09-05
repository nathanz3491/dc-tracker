"""Progress against the PRD's named project list.

The PRD's definition of done names 30 specific projects, but the list itself is
not in the PRD text. `seed/required-projects.txt` is where an operator pastes it,
one per line, as ``Company | Project name`` or just a name.

Extracted from `tracker verify` so the console and the command agree on what
"present" means. Two implementations of a fuzzy match would drift, and the whole
point of the file is to turn an unmeasurable requirement into a measurable one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tracker.config import seed_path
from tracker.dedup import company_key


def default_path() -> Path:
    return seed_path("required-projects.txt")


def load(path: Path | None = None) -> list[str]:
    """The wanted entries, comments and blanks stripped. Empty if there is no file."""
    path = path or default_path()
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@dataclass(frozen=True)
class RequiredMatch:
    entry: str
    project_id: int | None

    @property
    def met(self) -> bool:
        return self.project_id is not None


def match(projects, wanted: list[str]) -> list[RequiredMatch]:
    """Pair each wanted entry with a project id, or None.

    Matching is deliberately loose in both directions — ``name in row`` or ``row in
    name`` — because the list is typed by hand and the database holds whatever the
    first article called the campus. A false positive here costs an operator one
    glance; a false negative sends them hunting for a project already tracked.
    """
    haystack = [(company_key(p.company), (p.name or "").lower(), p.id) for p in projects]
    out: list[RequiredMatch] = []
    for entry in wanted:
        needle_company, _, needle_name = entry.partition("|") if "|" in entry else ("", "", entry)
        key = company_key(needle_company.strip()) if needle_company.strip() else ""
        name = needle_name.strip().lower()
        hit = next(
            (
                pid
                for ck, row_name, pid in haystack
                if (not key or key == ck) and (not name or name in row_name or row_name in name)
            ),
            None,
        )
        out.append(RequiredMatch(entry, hit))
    return out


__all__ = ["RequiredMatch", "default_path", "load", "match"]
