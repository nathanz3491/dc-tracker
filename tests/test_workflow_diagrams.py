"""The committed workflow diagrams still match their generator.

`CLAUDE.md` §7 asks that a change to a staged command update its page in
`docs/workflows/` — diagram included. Most of that rule is judgement and cannot be
tested. One half of it can: the `.svg` files are generated, so if somebody edits
`scripts/render_workflow_diagrams.py` and does not re-render, or hand-edits an SVG
that the next render will overwrite, the two fall out of step silently and the
repo ships a picture nobody produced.

So these tests do not check that a diagram is *correct* — nothing can. They check
that it is the one the committed generator makes, that it renders as XML, and that
each page actually shows its own diagram.
"""

from __future__ import annotations

import importlib.util
import sys
import xml.dom.minidom
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "workflows"
GENERATOR = ROOT / "scripts" / "render_workflow_diagrams.py"


def _generator():
    """Import the script by path: `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("render_workflow_diagrams", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _generator()


def test_every_diagram_has_a_page(generator):
    """A diagram nothing links to is a diagram nobody will maintain."""
    for name in generator.DIAGRAMS:
        page = DOCS / f"{name}.md"
        assert page.is_file(), f"{name}.svg has no {name}.md beside it"
        assert f"]({name}.svg)" in page.read_text(encoding="utf-8"), (
            f"docs/workflows/{name}.md does not embed {name}.svg"
        )


def test_every_page_names_the_render_command(generator):
    """The page has to say how to regenerate what it shows."""
    for name in generator.DIAGRAMS:
        text = (DOCS / f"{name}.md").read_text(encoding="utf-8")
        assert "scripts/render_workflow_diagrams.py" in text


def test_committed_svgs_match_the_generator(generator, tmp_path):
    """Re-render and compare bytes. Failing here means one of two things:

    the generator changed and the SVG was not re-rendered, or the SVG was edited
    by hand and the next render will throw that edit away. Both are fixed the same
    way — `python scripts/render_workflow_diagrams.py` — but read the diff first if
    it was a hand edit, because the change belongs in the generator.
    """
    stale = []
    for name, build in generator.DIAGRAMS.items():
        fresh = tmp_path / f"{name}.svg"
        build().save(fresh)
        committed = DOCS / f"{name}.svg"
        assert committed.is_file(), f"docs/workflows/{name}.svg is missing"
        if fresh.read_bytes() != committed.read_bytes():
            stale.append(name)
    assert not stale, (
        "these committed diagrams no longer match the generator: "
        f"{', '.join(stale)}. Run `python scripts/render_workflow_diagrams.py`."
    )


def test_diagrams_are_well_formed_xml():
    """A browser is forgiving; a broken SVG on GitHub renders as nothing at all."""
    svgs = sorted(DOCS.glob("*.svg"))
    assert svgs, "no diagrams found in docs/workflows/"
    for path in svgs:
        xml.dom.minidom.parse(str(path))


def test_diagrams_paint_their_own_background():
    """Without an opaque background the poster is unreadable in GitHub's dark mode.

    Every colour in the palette is chosen against white, and the text is near-black,
    so a transparent SVG inherits the reader's dark page and disappears.
    """
    for path in sorted(DOCS.glob("*.svg")):
        head = path.read_text(encoding="utf-8")[:1200]
        assert 'fill="#FFFFFF"' in head, f"{path.name} has no opaque background rect"


def test_render_is_deterministic(generator, tmp_path):
    """Two renders must be byte-identical, or the test above would flap."""
    name, build = next(iter(generator.DIAGRAMS.items()))
    first, second = tmp_path / "a.svg", tmp_path / "b.svg"
    build().save(first)
    build().save(second)
    assert first.read_bytes() == second.read_bytes(), f"{name} renders differently each time"
