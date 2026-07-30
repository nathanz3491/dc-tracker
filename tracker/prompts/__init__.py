"""Versioned extraction prompts, loaded from the `.txt` files beside this module.

The PRD wants prompts in files rather than Python strings so they can be iterated
without redeploying. Two decisions make that actually work:

**Version identity is filename + SHA-1 of the file bytes.** A filename alone
starts lying the moment you edit the file, which is exactly what iterating means.
``extract-v1@3f2a91c4`` is a distinct, reproducible identity for every edit, and
it is stamped into ``source.extractor`` so a bad row can be traced to the prompt
that produced it.

**Templating uses ``string.Template`` (``$var``), not ``str.format`` (``{var}``).**
The prompt contains a JSON schema block full of literal braces; ``str.format``
would raise on the first one. This is not a stylistic preference — it is the
difference between working and not.
"""

from __future__ import annotations

import hashlib
import re
import string
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: The prompt files live in this package directory, so they ship with the wheel.
#:
#: This is a package rather than the PRD's flat `prompts.py` for a mundane
#: reason: a module and a directory cannot share a name inside one package, and
#: the PRD asks for both `tracker/prompts.py` and `tracker/prompts/*.txt`. As a
#: package, `tracker.prompts.load_prompt` imports identically either way.
PROMPT_DIR = Path(__file__).resolve().parent

_SECTION = re.compile(r"^===\s*([A-Z][A-Z ]*[A-Z]|[A-Z])\s*===\s*$", re.MULTILINE)

#: Sections concatenated into the system message, in order.
SYSTEM_SECTIONS = ("SYSTEM", "SCHEMA", "FIELD NOTES")
#: The only templated section.
USER_SECTION = "USER"


class PromptError(ValueError):
    """The prompt file is missing or malformed."""


@dataclass(frozen=True)
class Prompt:
    name: str
    path: Path
    sha1: str
    system: str
    user_template: str

    @property
    def stamp(self) -> str:
        """Identity recorded on every row this prompt produced."""
        return f"{self.name}@{self.sha1[:8]}"

    def render_user(self, **values: object) -> str:
        """Fill the USER section.

        ``safe_substitute`` rather than ``substitute``: a stray ``$`` in scraped
        article text must not blow up the run.
        """
        return string.Template(self.user_template).safe_substitute(**values)


def _split_sections(text: str, path: Path) -> dict[str, str]:
    parts = _SECTION.split(text)
    if len(parts) < 3:
        raise PromptError(
            f"{path.name} has no `=== SECTION ===` headers. Expected: "
            f"{', '.join([*SYSTEM_SECTIONS, USER_SECTION])}"
        )
    # parts[0] is the leading comment block, then (name, body) pairs.
    sections = {
        name.strip(): body.strip() for name, body in zip(parts[1::2], parts[2::2], strict=True)
    }
    missing = [s for s in (*SYSTEM_SECTIONS, USER_SECTION) if s not in sections]
    if missing:
        raise PromptError(
            f"{path.name} is missing section(s): {', '.join(missing)}. "
            f"Found: {', '.join(sections) or 'none'}"
        )
    return sections


def available() -> list[str]:
    return sorted(p.stem for p in PROMPT_DIR.glob("*.txt"))


@lru_cache(maxsize=8)
def load_prompt(name_or_path: str = "extract-v1") -> Prompt:
    """Load a prompt by name (`extract-v1`) or by path."""
    path = Path(name_or_path)
    if not path.is_file():
        path = PROMPT_DIR / f"{name_or_path}.txt"
    if not path.is_file():
        raise PromptError(
            f"no prompt found at {name_or_path!r}. Available: {', '.join(available()) or 'none'}"
        )

    raw = path.read_bytes()
    # Hash the raw bytes so any edit at all changes the stamp, but normalize line
    # endings first so a CRLF checkout is not a different "version".
    normalized = raw.replace(b"\r\n", b"\n")
    sections = _split_sections(normalized.decode("utf-8"), path)
    return Prompt(
        name=path.stem,
        path=path,
        sha1=hashlib.sha1(normalized, usedforsecurity=False).hexdigest(),
        system="\n\n".join(sections[s] for s in SYSTEM_SECTIONS),
        user_template=sections[USER_SECTION],
    )


__all__ = [
    "PROMPT_DIR",
    "SYSTEM_SECTIONS",
    "USER_SECTION",
    "Prompt",
    "PromptError",
    "available",
    "load_prompt",
]
