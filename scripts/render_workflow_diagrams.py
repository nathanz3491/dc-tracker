#!/usr/bin/env python3
"""Render the workflow posters in `docs/workflows/`.

    python scripts/render_workflow_diagrams.py

**Why a generator and not hand-drawn SVG.** `CLAUDE.md` §7 makes updating the
matching diagram part of the same change as the command. That rule only holds if
editing a diagram is cheap, and editing 400 lines of absolute SVG coordinates is
not — a moved box means re-typing every arrow around it. Here a diagram is a
declarative block at the foot of this file: boxes with names, arrows between their
edges, one line each. The SVG is committed beside the Markdown because GitHub
renders `![](x.svg)` and does not render a build step.

**Why not Mermaid.** Mermaid lays itself out and cannot express the composition
these posters use — sectioned bands, a dashed group around a sub-loop, annotation
callouts, and a palette where colour carries meaning. `docs/workflows/README.md`
states what each colour means; that contract is the reason these are drawn rather
than generated from a graph description.

Standard library only, deliberately: this repo is Python with no Node toolchain,
and a diagram nobody can re-render is a diagram that goes stale.

The palette is lifted from `work/Extraction-Pipeline-Diagram.pdf`, which is the
house style for a diagram in this project.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from xml.sax.saxutils import escape

# --- the house palette, measured out of the reference PDF ---------------------

INK = "#16242E"  # titles, box headings
SLATE = "#5C6B73"  # arrows, secondary text
RULE = "#B9C6CC"  # hairlines, cool box borders
COOL = "#EDF1F2"  # stored data, inputs
COOL_2 = "#E7EDEE"  # a second cool tone, for stacks
PANEL = "#F7F9F9"  # grouping panels
TEAL = "#0F6E75"  # free and deterministic: no model
TEAL_FILL = "#E7F0F0"
ORANGE = "#D9891F"  # costs a model call
ORANGE_FILL = "#FBF2E4"
ORANGE_FILL_2 = "#FDF6E9"
RED = "#A83E33"  # a refusal, a rail, a stop
RED_FILL = "#F7EBE9"
WHITE = "#FFFFFF"

FONT = "system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue','DejaVu Sans',Arial,sans-serif"

#: fill, stroke, heading colour
ROLES: dict[str, tuple[str, str, str]] = {
    "cool": (COOL, RULE, INK),
    "cool2": (COOL_2, RULE, INK),
    "panel": (PANEL, RULE, INK),
    "plain": (WHITE, RULE, INK),
    "teal": (TEAL_FILL, TEAL, TEAL),
    "orange": (ORANGE_FILL, ORANGE, INK),
    "orange2": (ORANGE_FILL_2, ORANGE, INK),
    "red": (RED_FILL, RED, RED),
}

LEGEND = [
    (TEAL, "free · deterministic"),
    (RULE, "one fetch or query"),
    (ORANGE, "costs a model call"),
    (RED, "a refusal, rail or stop"),
]


#: Letter-spacing on the small-caps section labels.
SECTION_TRACKING = 2.4


def _w(text: str, size: float, bold: bool = False) -> float:
    """Rough rendered width. Only ever used to back a label with white."""
    return len(text) * size * (0.565 if bold else 0.515)


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def left(self, dy: float = 0) -> tuple[float, float]:
        return (self.x, self.cy + dy)

    def right(self, dy: float = 0) -> tuple[float, float]:
        return (self.x + self.w, self.cy + dy)

    def top(self, dx: float = 0) -> tuple[float, float]:
        return (self.cx + dx, self.y)

    def bottom(self, dx: float = 0) -> tuple[float, float]:
        return (self.cx + dx, self.y + self.h)


@dataclass
class Canvas:
    width: float
    height: float
    parts: list[str] = field(default_factory=list)
    markers: set[str] = field(default_factory=set)

    # --- text ---------------------------------------------------------------

    def text(
        self,
        x: float,
        y: float,
        s: str,
        *,
        size: float = 11,
        colour: str = INK,
        bold: bool = False,
        anchor: str = "start",
        spacing: float | None = None,
        italic: bool = False,
    ) -> None:
        attrs = [
            f'x="{x:.1f}"',
            f'y="{y:.1f}"',
            f'font-size="{size}"',
            f'fill="{colour}"',
            f'text-anchor="{anchor}"',
        ]
        if bold:
            attrs.append('font-weight="700"')
        if italic:
            attrs.append('font-style="italic"')
        if spacing is not None:
            attrs.append(f'letter-spacing="{spacing}"')
        self.parts.append(f"<text {' '.join(attrs)}>{escape(s)}</text>")

    def title(self, s: str, sub: str = "") -> None:
        self.text(48, 54, s, size=25, bold=True)
        if sub:
            self.text(48, 82, sub, size=13, colour=SLATE)

    def section(self, x: float, y: float, s: str, note: str = "") -> None:
        caps = s.upper()
        self.text(x, y, caps, size=10.5, colour=SLATE, bold=True, spacing=SECTION_TRACKING)
        if note:
            # Capitals plus tracking, measured per character: the generic estimate
            # in `_w` is tuned for mixed case and ran the note into the label.
            width = len(caps) * (10.5 * 0.72 + SECTION_TRACKING)
            self.text(x + width + 16, y, note, size=11, colour=SLATE)

    def rule(self, y: float, x0: float = 48, x1: float | None = None) -> None:
        x1 = self.width - 48 if x1 is None else x1
        self.parts.append(
            f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{RULE}" stroke-width="1"/>'
        )

    def footer(self, s: str) -> None:
        self.rule(self.height - 62)
        self.text(48, self.height - 34, s, size=12.5, colour=INK)

    def legend(self, x: float, y: float) -> None:
        for colour, label in LEGEND:
            self.parts.append(
                f'<rect x="{x:.1f}" y="{y - 8:.1f}" width="11" height="11" rx="2.5" '
                f'fill="{colour}"/>'
            )
            self.text(x + 17, y + 1, label, size=10, colour=SLATE)
            x += 25 + _w(label, 10) + 22

    # --- shapes -------------------------------------------------------------

    def box(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        title: str = "",
        subs: tuple[str, ...] | list[str] = (),
        *,
        role: str = "cool",
        title_size: float = 12.5,
        sub_size: float = 10,
        dashed: bool = False,
        align: str = "middle",
    ) -> Box:
        fill, stroke, heading = ROLES[role]
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="7" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.3"{dash}/>'
        )
        tx = x + w / 2 if align == "middle" else x + 14
        block = (17 if title else 0) + 13.6 * len(subs)
        cursor = y + (h - block) / 2 + (13 if title else 11)
        if title:
            self.text(tx, cursor, title, size=title_size, colour=heading, bold=True, anchor=align)
            cursor += 17
        for line in subs:
            self.text(tx, cursor, line, size=sub_size, colour=SLATE, anchor=align)
            cursor += 13.6
        return Box(x, y, w, h)

    def frame(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str = "",
        *,
        colour: str = ORANGE,
        fill: str = "none",
    ) -> Box:
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="9" '
            f'fill="{fill}" stroke="{colour}" stroke-width="1.4" stroke-dasharray="6 4"/>'
        )
        if label:
            self.text(x + 14, y + 19, label, size=10.5, colour=colour, bold=True, spacing=1.4)
        return Box(x, y, w, h)

    def chip(self, x: float, y: float, s: str, *, colour: str, fill: str) -> float:
        """A small pill. Returns the x to place the next one at."""
        w = _w(s, 10.5, True) + 22
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="25" rx="12.5" '
            f'fill="{fill}" stroke="{colour}" stroke-width="1.2"/>'
        )
        self.text(x + w / 2, y + 17, s, size=10.5, colour=colour, bold=True, anchor="middle")
        return x + w + 9

    # --- arrows -------------------------------------------------------------

    def arrow(
        self,
        points: list[tuple[float, float]],
        *,
        colour: str = SLATE,
        label: str = "",
        label_at: float = 0.5,
        label_dy: float = -8,
        dashed: bool = False,
        head: bool = True,
    ) -> None:
        self.markers.add(colour)
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        marker = f' marker-end="url(#a{colour.lstrip("#")})"' if head else ""
        self.parts.append(
            f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="1.8" '
            f'stroke-linejoin="round"{dash}{marker}/>'
        )
        if label:
            i = max(0, min(len(points) - 2, int(label_at * (len(points) - 1))))
            (x0, y0), (x1, y1) = points[i], points[i + 1]
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2 + label_dy
            self._label(mx, my, label, colour)

    def _label(self, x: float, y: float, s: str, colour: str) -> None:
        w = _w(s, 10, True)
        self.parts.append(
            f'<rect x="{x - w / 2 - 5:.1f}" y="{y - 11:.1f}" width="{w + 10:.1f}" '
            f'height="15" fill="{WHITE}" opacity="0.94"/>'
        )
        self.text(x, y, s, size=10, colour=colour, bold=True, anchor="middle")

    # --- output -------------------------------------------------------------

    def save(self, path: Path) -> None:
        defs = "".join(
            f'<marker id="a{c.lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
            f'<path d="M 0 1 L 10 5 L 0 9 z" fill="{c}"/></marker>'
            for c in sorted(self.markers)
        )
        body = "\n".join(self.parts)
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width:.0f}" '
            f'height="{self.height:.0f}" viewBox="0 0 {self.width:.0f} {self.height:.0f}" '
            f'font-family="{FONT}">\n'
            f"<defs>{defs}</defs>\n"
            f'<rect width="{self.width:.0f}" height="{self.height:.0f}" fill="{WHITE}"/>\n'
            # xml:space, because several panels align a continuation line under
            # its parent with leading spaces. SVG collapses runs of whitespace
            # by default, which silently flattened every indented list here.
            f'<g font-family="{FONT}" xml:space="preserve">\n{body}\n</g>\n</svg>\n',
            encoding="utf-8",
        )


# The four diagrams. `fmt: off` because the box and arrow arguments are laid
# out to be read as a diagram — one line per box, coordinates in columns — and
# the formatter would put each string on its own line, tripling the length of
# the thing a person has to edit when a command changes.
# fmt: off

# --- tracker enrich -----------------------------------------------------------


def enrich() -> Canvas:
    c = Canvas(1500, 1010)
    c.title(
        "tracker enrich — every method at one row, cheapest first",
        "Six harvesters in cost order. A round that fills nothing ends the run, and the two "
        "model rungs go last, on whatever is left.",
    )
    c.legend(48, 108)
    c.rule(128)

    c.section(48, 162, "the run", "one project at a time, under one shared article budget")

    ids = c.box(48, 196, 186, 44, "tracker enrich 90 93", ["no field target — work the row"], role="cool", title_size=11.5)
    sel = c.box(48, 250, 186, 44, "--select 30", ["the 30 closest to nine fields"], role="cool", title_size=11.5)
    allr = c.box(48, 304, 186, 44, "--all", ["every row still below nine"], role="cool", title_size=11.5)

    order = c.box(
        278, 196, 178, 152, "Rows, ordered",
        ["closest to the target first,", "capacity breaks the tie,", "finished rows excluded", "", "--budget is the real ceiling"],
        role="cool2",
    )
    for b in (ids, sel, allr):
        c.arrow([b.right(), (278, b.cy)])

    derive = c.box(
        278, 386, 178, 78, "1 · derive",
        ["county, lat, lon from Census.", "Free and certain, so these", "fields are never searched for"],
        role="teal",
    )
    c.arrow([order.bottom(), (order.cx, 386)])

    frame = c.frame(492, 176, 314, 400, "HARVEST · repeated, up to six rounds")
    y = 210
    for title, subs, role in [
        ("2 · queue", ["candidates already naming this row"], "teal"),
        ("3 · retry", ["its URLs that failed to fetch"], "teal"),
        ("4 · archive", ["sitemap sweep, filtered to this row", "round 1 · key-free · swept once a batch"], "cool"),
        ("5 · search", ["queries built from this row's own gaps", "needs a key · at most twelve"], "orange"),
        ("6 · refresh", ["its own citations, re-read", "round 1 only"], "cool"),
    ]:
        h = 56 if len(subs) > 1 else 44
        c.box(514, y, 270, h, title, subs, role=role, title_size=11.5)
        y += h + 10
    # Up the outside of the frame and into the first harvester, rather than into
    # the frame's mid-height: derive runs once, before round 1 opens, and an
    # arrowhead landing beside `archive` read as though it fed that harvester.
    c.arrow([derive.right(), (474, derive.cy), (474, 232), (514, 232)])

    pool = c.box(
        844, 232, 186, 86, "Pooled, then filtered",
        ["de-duplicated across harvesters;", "publishers seed/sources.toml", "ignores are dropped first"],
        role="panel",
    )
    read = c.box(
        844, 362, 186, 78, "Read and extract",
        ["crawl.run, force=True — a cited", "page may support a field the", "gate dropped last time"],
        role="cool",
    )
    c.arrow([frame.right(-24), (825, frame.cy - 24), (825, 275), (844, 275)])
    c.arrow([pool.bottom(), (pool.cx, 362)], label="unread only", label_dy=-6)

    gate = c.box(
        1076, 344, 190, 92, "Did the round fill",
        ["a field that was empty?"],
        role="orange2", title_size=13,
    )
    c.arrow([read.right(), (1053, read.cy), (1053, gate.cy), (1076, gate.cy)])

    c.box(
        1310, 344, 142, 92, "no — stop",
        ["the real stop", "condition"],
        role="red",
    )
    c.arrow([gate.right(), (1310, gate.cy)], colour=RED)
    c.arrow(
        [gate.bottom(), (gate.cx, 552), (649, 552), (649, 576)],
        colour=TEAL, label="yes — go round again", label_at=0.45, label_dy=14,
    )

    c.text(
        48, 612,
        "The other four stops, each reported by name: every field is filled · the row is already at the target, which is a refusal "
        "phrased so it cannot read as success · no harvester found an unread article · the six-round ceiling.",
        size=10.5, colour=SLATE,
    )

    c.rule(636)
    c.section(48, 676, "after the rounds", "two model rungs, in this order, on what the cheap passes could not reach")

    contested = c.box(
        48, 716, 190, 84, "Contested fields",
        ["two quote-backed claims", "that genuinely disagree —", "which harvesting creates"],
        role="cool",
    )
    settle = c.box(
        286, 706, 208, 104, "7 · settle",
        ["one reasoning call reads every", "claim about one field at once,", "with dates. Picks one, or refuses.", "Not the extractor that read the pages"],
        role="orange",
    )
    c.arrow([contested.right(), (286, contested.cy)])

    supersede = c.box(
        554, 716, 200, 84, "Supersede the losers",
        ["the field is never assigned;", "the row is re-derived, so it still", "equals what its citations imply"],
        role="teal",
    )
    c.arrow([settle.right(), (554, 758)], colour=TEAL, label="resolved", label_dy=-6)
    c.box(
        286, 838, 208, 62, "refused",
        ["the disagreement stays in the notes,", "and is asked again next run"],
        role="red", title_size=11.5,
    )
    c.arrow([settle.bottom(), (settle.cx, 838)], colour=RED)

    agent = c.box(
        802, 706, 208, 104, "8 · agent pass",
        ["a model picks its own searches", "for what the query templates", "could not reach, and cites it.", "~77,000 tokens a row, so it is last"],
        role="orange",
    )
    c.arrow([supersede.right(), (802, 758)])

    c.box(
        1058, 700, 224, 116, "The row",
        ["nine of twelve tracked fields is the bar", "", "citations up, confidence rescored,", "and every stop reason on screen"],
        role="cool2",
    )
    c.arrow([agent.right(), (1058, 758)])

    c.box(
        1310, 694, 142, 126, "--dry-run",
        ["harvests and", "reports only.", "", "Extraction is skipped", "outright, so a", "preview cannot", "bill you"],
        role="red", title_size=12.5,
    )

    c.footer(
        "Cost order is the whole design: an expensive rung never runs for a field a free one would have filled, "
        "and the run stops on diminishing returns rather than on a fixed article count."
    )
    return c


# --- tracker sync -------------------------------------------------------------


def sync() -> Canvas:
    c = Canvas(1500, 920)
    c.title(
        "tracker sync — the whole loop, in seven phases",
        "Five phases run by default. The two that spend the most are off until asked for, and every "
        "phase that costs calls has its own cap.",
    )
    c.legend(48, 108)
    c.rule(128)

    c.section(48, 162, "the phases", "numbered within the plan this run actually chose — a phase not asked for is absent, not 'skipped'")

    #: title, sub-lines, role, cap, and whether the phase is off until asked for
    phases = [
        ("1 · discover", ["poll the feeds, sweep", "archives, run searches"], "cool", "--since-days 45", False),
        ("2 · prospect", ["chase operators the", "roster says we lack"], "orange", "--prospect · off", True),
        ("3 · extract", ["crawl the queue into", "new project rows"], "orange", "--limit 15", False),
        ("4 · refresh", ["re-read sources nobody", "has looked at lately"], "orange", "--refresh-limit 15", False),
        ("5 · enrich", ["every method at the", "thinnest rows we hold"], "orange", "--enrich · off", True),
        ("6 · settle", ["re-derive, then", "rescore confidence"], "teal", "free · pure functions", False),
        ("7 · projects", ["list the result, and", "what is still unread"], "cool", "--rows 30", False),
    ]
    boxes = []
    x = 48
    for title, subs, role, cap, optional in phases:
        boxes.append(c.box(x, 200, 178, 106, title, subs, role=role, dashed=optional))
        c.text(x + 89, 328, cap, size=10, colour=ORANGE if role == "orange" else SLATE, bold=True, anchor="middle")
        x += 198
    for a, b in pairwise(boxes):
        c.arrow([a.right(), (b.x, b.cy)])

    c.text(
        48, 366,
        "--full turns on both dashed phases (prospect 5, enrich 10) and adds --deep and --retry-failed. It never overrides a number you gave: "
        "--full --prospect 1 means one operator.",
        size=10.5, colour=SLATE,
    )
    c.text(
        48, 388,
        "Four caps and not one budget, because they buy different things: --limit buys breadth, --refresh-limit buys currency, "
        "--prospect buys coverage of operators we are blind to, --enrich buys depth.",
        size=10.5, colour=SLATE,
    )

    c.rule(414)
    c.section(48, 454, "where the rows come from", "three phases end in one queue, and one gate stands between the queue and a new row")

    feeds = c.box(48, 500, 176, 62, "Feeds", ["what was published lately"], role="cool", title_size=11.5)
    search = c.box(48, 574, 176, 62, "Search", ["reaches back past the feeds"], role="orange", title_size=11.5)
    roster = c.box(48, 648, 176, 62, "Roster", ["who we hold no rows for"], role="cool", title_size=11.5)
    archives = c.box(48, 722, 176, 62, "Archives (--deep)", ["sitemaps, no key needed"], role="cool", title_size=11.5)

    queue = c.box(
        280, 500, 190, 284, "The queue",
        ["ingest_url rows,", "status = discovered", "", "Ordered before the limit", "bites, never after:", "", "· rows covering a project", "  we already track go first", "· among those, the ones", "  reporting an obstacle", "· prospect finds jump the", "  whole queue"],
        role="panel",
    )
    for b in (feeds, search, roster, archives):
        c.arrow([b.right(), (280, b.cy)])

    gate = c.box(
        520, 560, 206, 164, "Identity arbiter",
        ["--verify-identity, on by default", "", "A row resembling one we already hold", "is rejected back to a model with the", "article extraction just read, and the", "suspected row's own details.", "One call, and nothing re-fetched.", "", "Unsure, erroring or short of 0.9?", "The row is created — it fails open"],
        role="red", title_size=13,
    )
    c.arrow([queue.right(), (520, 642)], label="extract", label_dy=-6)

    rows = c.box(776, 560, 190, 76, "New rows", ["with their citations", "and a confidence score"], role="cool2")
    routed = c.box(776, 656, 190, 68, "Routed instead", ["citations land on the row", "we already had"], role="teal")
    c.arrow([gate.right(-34), (776, 598)])
    c.arrow([gate.right(56), (776, 690)], colour=TEAL)

    c.box(
        1016, 560, 200, 164, "6 · settle",
        ["derive reapplies every derived", "value — county, coordinates,", "capacity rollups — because those", "only move when something", "writes to the row.", "", "confidence is a cache of a pure", "function of the citations, so it is", "stale the moment one lands"],
        role="teal", title_size=13,
    )
    c.arrow([rows.right(), (1016, 598)])
    c.arrow([routed.right(), (1016, 690)])

    c.box(
        1266, 560, 186, 164, "Refresh, separately",
        ["phase 4 re-reads stale sources", "with the cache turned off.", "", "The point is finding out whether", "the article changed, and serving", "it from the local cache would", "guarantee the answer is no"],
        role="orange2", title_size=12,
    )

    c.text(
        48, 826,
        "Not this command: scripts/sync_db.py moves the database file between machines. It shares only the word. See docs/workflows/sync.md.",
        size=10.5, colour=RED, bold=True,
    )

    c.footer(
        "One write lock is held for the whole run, because SQLite takes one writer and two overlapping syncs fail partway — "
        "after the second has already paid for its model calls."
    )
    return c


# --- tracker duplicates -------------------------------------------------------


def duplicates() -> Canvas:
    c = Canvas(1500, 1000)
    c.title(
        "tracker duplicates — raise a pair, rank it, answer it",
        "One site often has a builder, a landlord and an occupier. Every dedup key is correct; the "
        "building is one. Nothing here merges by itself.",
    )
    c.legend(48, 108)
    c.rule(128)

    c.section(48, 162, "how a pair is raised", "three passes, unioned — each reaches duplicates the others structurally cannot")

    p1 = c.box(
        48, 196, 236, 108, "1 · same locality",
        ["rows sharing (city or county, state).", "Finds what made capex need this:", "one campus, four companies"],
        role="cool", title_size=12,
    )
    p2 = c.box(
        48, 318, 236, 108, "2 · dedup keys",
        ["bucketed on company, not locality —", "locality is the axis that disagrees.", "Pairs county:richland with city:richland"],
        role="cool", title_size=12,
    )
    p3 = c.box(
        48, 440, 236, 108, "3 · shared tranche key",
        ["starts from the key, not a place.", "Reaches Crusoe's Abilene row and", "Oracle's Shackelford County row"],
        role="cool", title_size=12,
    )

    pair = c.box(
        330, 274, 200, 200, "A suspected pair",
        ["carrying every signal that", "holds for it, not only the", "one that raised it.", "", "Recording only the latter left", "31 live pairs with a single", "evidence class and no route", "to any decision"],
        role="panel", title_size=13,
    )
    for b in (p1, p2, p3):
        c.arrow([b.right(), (307, b.cy), (307, pair.cy), (330, pair.cy)])

    c.text(576, 258, "RANKED, STRONGEST FIRST", size=10.5, colour=SLATE, bold=True, spacing=SECTION_TRACKING)
    x = 576
    for label, colour, fill in [
        ("same name", TEAL, TEAL_FILL),
        ("same tranche", TEAL, TEAL_FILL),
        ("shared operator", SLATE, COOL),
        ("city vs county", SLATE, COOL),
        ("name overlap", RED, RED_FILL),
    ]:
        x = c.chip(x, 276, label, colour=colour, fill=fill)
    c.text(
        576, 330,
        "The order is what a reader can act on. 'city vs county' led it on the strength of being structural, so the report opened",
        size=10.5, colour=SLATE,
    )
    c.text(
        576, 346,
        "with 31 of 49 pairs of the one class no automated path can settle. A shared name word is a word: it sorts last, and it",
        size=10.5, colour=SLATE,
    )
    c.text(576, 362, "can never carry a merge on its own.", size=10.5, colour=SLATE)

    groups = c.box(
        576, 386, 216, 104, "Grouped",
        ["pairs sharing an id are the", "same building, so they are", "unioned. Four rows make six", "pairs and one decision, not six"],
        role="cool2", title_size=12,
    )
    c.arrow([pair.right(), (553, pair.cy), (553, groups.cy), (576, groups.cy)])

    c.box(
        832, 386, 216, 104, "Read by capex",
        ["rollup counts one row per", "group and discloses the rest,", "so a false pair holds a real", "campus out of a quoted number"],
        role="orange2", title_size=12,
    )
    c.arrow([groups.right(), (832, groups.cy)])

    c.box(
        1096, 386, 356, 104, "Which is why the answer matters",
        ["Parking is not cosmetic: it puts a real campus's", "capacity back into the buyer table. Merging is the", "only repair for a real duplicate, and it deletes rows.", "Leaving it keeps one row out of every capex total."],
        role="panel", title_size=12.5, align="left", sub_size=10.2,
    )

    c.rule(574)
    c.section(48, 608, "the three answers", "duplicates proposes; a person, a model or an agent disposes")

    who = c.box(
        48, 640, 226, 200, "Who decides",
        ["--agent   default. Reads both rows'", "          articles, searches, then rules", "", "--ask     a person at the keyboard,", "          trusted for a merge outright", "", "--no-agent  the older one-call path,", "          shown two rows and nothing else", "", "all three are asked one question:", "what would rule this match OUT?"],
        role="cool", title_size=13, align="left", sub_size=10.2,
    )

    diff = c.box(320, 630, 190, 58, "different sites", ["parks the pair, at 0.6 or above"], role="teal", title_size=12.5)
    same = c.box(320, 706, 190, 58, "same site", ["merges — behind --merge"], role="red", title_size=12.5)
    unclear = c.box(
        320, 782, 190, 84, "unclear",
        ["stays in the report, and", "capex keeps one row of the", "group out of the total"],
        role="cool", title_size=12.5,
    )
    c.arrow([who.right(-40), (297, 700), (297, diff.cy), (320, diff.cy)], colour=TEAL)
    c.arrow([who.right(-5), (320, same.cy)], colour=RED)
    c.arrow([who.right(40), (297, 780), (297, unclear.cy), (320, unclear.cy)])

    notdup = c.box(
        936, 630, 200, 104, "not_duplicate",
        ["decided_by records 'operator',", "'model (0.87)' or 'agent (0.91)':", "a reader must be able to tell", "which of them read the sources"],
        role="teal", title_size=12.5,
    )
    c.arrow([diff.right(), (700, diff.cy), (700, notdup.cy), (936, notdup.cy)], colour=TEAL)

    rails = c.box(
        556, 696, 356, 198, "Rails · what --merge still refuses, one function for both judges",
        ["· confidence under the floor — 0.85 for the agent, 0.9 for", "   the one-call path, far above what a park needs", "· evidence is only a shared name word, even with --weak", "· evidence is only a cross-granularity key match — the old", "   path refuses outright; the agent may rule, because it", "   can read the articles and be a person with a map", "· the only shared tranche names a market and a sequence:", "   iad-3, hillsboro-1, held by two operators 60 km apart", "· the names differ by an ordinal — Polaris Forge 1 and 2", "· the coordinates are over 25 km apart. Geography wins", "· the agent quoted no sentence from an article it read"],
        role="red", title_size=13, align="left", sub_size=10.2,
    )
    c.arrow([same.right(), (533, same.cy), (533, rails.cy), (556, rails.cy)], colour=RED)

    merge = c.box(
        936, 756, 200, 110, "tracker merge",
        ["every rail cleared, then: the", "survivor is the row with more", "citations, then more fields,", "then the lower id — never the", "model's choice, and nearly", "consequence-free"],
        role="cool2", title_size=12.5,
    )
    c.arrow([rails.right(), (924, rails.cy), (924, merge.cy), (936, merge.cy)], colour=RED)

    c.box(
        1160, 630, 292, 236, "The group, and what is outside it",
        ["duplicates · list the suspected groups", "duplicates park · these are different sites", "duplicates unpark · reopen that decision", "duplicates parked · who ruled what, and why", "duplicates resolve · work through them", "", "merge lives outside the group on purpose:", "it deletes rows, and that deserves its own", "name.", "", "A wrong merge destroys two rows and no", "re-crawl recovers them; a wrong split is", "visible and recoverable. That asymmetry is", "why every rail above refuses rather than asks."],
        role="panel", title_size=13, align="left", sub_size=10.2,
    )

    c.footer(
        "Prevention is cheaper than all of this: sync's identity arbiter makes the same judgement one call before the row exists. "
        "Clearing 47 stored groups took a ten-hour agent run."
    )
    return c


# --- tracker logic ------------------------------------------------------------


def logic() -> Canvas:
    c = Canvas(1500, 1090)
    c.title(
        "tracker logic — do the supported values agree?",
        "Every other check asks whether a value is cited. These ask whether the cited values contradict "
        "each other, which a perfectly sourced row can still do.",
    )
    c.legend(48, 108)
    c.rule(128)

    c.section(48, 162, "logic check", "four layers, and only the last two cost anything. Nothing here writes")

    rules = c.box(
        48, 196, 250, 118, "Rules · always, free",
        ["energised before operational, built above", "planned, online before announced, a", "milestone dated next year counted as", "already reached. Each states its reasoning,", "so you can disagree without reading code"],
        role="teal", title_size=12.5, align="left", sub_size=10.2,
    )
    coll = c.box(
        48, 330, 250, 104, "Collisions · always, free",
        ["two sources, one field, two values. The", "winner is read back from the same", "per-field policy the write path used, with", "its reason — not re-decided here"],
        role="teal", title_size=12.5, align="left", sub_size=10.2,
    )
    judge = c.box(
        48, 450, 250, 104, "Judgement · --read N",
        ["one call per row, for what no rule can", "phrase: a blocker describing a problem", "the milestones say is solved. Off unless", "you name a number"],
        role="orange", title_size=12.5, align="left", sub_size=10.2,
    )
    audit = c.box(
        48, 570, 250, 104, "Evidence audit · --audit N",
        ["the prior question: does the value's own", "sentence state it, or is it a programme", "total quoted as one campus's money?", "Costliest rows read first"],
        role="orange", title_size=12.5, align="left", sub_size=10.2,
    )

    c.box(
        344, 196, 200, 478, "Findings",
        ["error or warning,", "each with a remedy", "line naming what", "to look at", "", "plus every collision:", "the kept value, the", "loser, and which", "rule settled it", "", "A model finding is", "tagged as one, and", "must name two fields", "and quote its", "evidence or it is", "dropped"],
        role="panel", title_size=13,
    )
    for b in (rules, coll, judge, audit):
        c.arrow([b.right(), (344, b.cy)])

    c.box(
        576, 196, 432, 232, "Five fields do not use credibility",
        ["Assuming the better source always won is the mistake this", "module was built on: re-deriving that way reported 73 of 221", "live rows as drifted, and none had.", "", "· mw_built takes the largest figure — energised megawatts", "   only go up, and a better source describing an earlier", "   state must not walk it back", "· first_announced takes the earliest — that is what 'first' means", "· phase takes the furthest along, unless a source says it stopped", "· name, company and location are never overwritten once set:", "   churn in an identity field is worse than staleness"],
        role="teal", title_size=13, align="left", sub_size=10.2,
    )

    c.box(
        576, 442, 432, 232, "What the paid layers may never do",
        ["A model is not allowed to pick a collision winner. Which of two", "cited numbers is right is a question about sources, and sources", "carry weights and dates precisely so that nobody has to guess.", "", "So --read looks only for contradictions no rule can phrase, and", "--audit asks only whether a value's own sentence supports it.", "Its verdicts are a closed set: unsupported, misattributed, hedged.", "", "A truncated reply is said out loud rather than left in a table row:", "that call was paid for and returned nothing, which is not the", "same as finding no contradictions."],
        role="orange", title_size=13, align="left", sub_size=10.2,
    )

    c.box(
        1032, 196, 420, 478, "Nothing in check is written",
        ["A contradiction is a question for a person. The", "report says so in its last line, and names where", "an answer goes.", "", "That is not timidity. Whether 100 MW built against", "32 MW planned means the plan was revised, or that", "the two figures describe different phases of one", "campus, is not in the row — and a tool that picked", "one would be inventing a fact.", "", "Measured on the live database: 0 of 149 findings", "were mechanically resolvable.", "", "Where answers go:", "", "tracker review · confirm or demote a value", "tracker merge · fold rows that are one campus", "logic conflicts · settle a contested field", "logic resolve · work through the findings", "", "An unconfirmed investment figure already stays out", "of the capex sums, so the repair path exists before", "the audit ever runs."],
        role="panel", title_size=13, align="left", sub_size=10.2,
    )

    c.rule(716)
    c.section(48, 752, "the two that can settle something", "and the sharp difference between what each of them writes")

    c.box(
        48, 792, 340, 204, "logic conflicts · proposes",
        ["A field with two quote-backed claims that genuinely", "disagree. Narrow on purpose: identity fields are excluded,", "and 174 of 666 contested fields were name or company.", "", "One reasoning call sees every claim at once — value, stored", "quote, publisher, date — and picks a key from a closed list.", "It cannot type a value: every option is a figure a publisher", "actually printed, shown with the quote already stored.", "", "Then one adversarial call tries to knock the answer down.", "Two calls a field, hard: an unbounded argument is", "unbounded spend, and a refusal carrying the objection is a", "better outcome than a third call arguing with itself."],
        role="orange", title_size=13, align="left", sub_size=10.2,
    )

    c.box(
        412, 792, 300, 204, "--apply writes claims, not fields",
        ["It marks the losing citations superseded on their", "own source rows, then re-derives the project.", "", "The field is never assigned. That is what keeps the", "one guarantee this database rests on: every value", "equals what its citations imply. An assignment", "would make the row a thing somebody typed, and", "the next backfill derive would put it back.", "", "A refusal writes nothing and is a recorded answer:", "the disagreement stays disclosed in the notes with", "both citations intact."],
        role="teal", title_size=13, align="left", sub_size=10.2,
    )
    c.box(
        736, 792, 380, 204, "logic resolve · settles what it can, then asks",
        ["1  drift repair, free. A row whose stored value its own sources no", "   longer support: re-running the declared policy is arithmetic, not", "   an opinion. The only mechanical fix in the whole report.", "", "2  answered by comparison, free. A read of data already stored,", "   never a judgement between two sourced figures. Two codes clear", "   that bar, and they were 448 of 536 resolvable findings.", "", "3  what is left goes to the agent (default), the older fixed menu", "   (--llm), or to you at the keyboard. --auto stops after step 2,", "   for scripts and for the console, which has no keyboard.", "", "A finding already answered on its row is not offered again."],
        role="orange", title_size=13, align="left", sub_size=10.2,
    )
    c.box(
        1140, 792, 312, 204, "Why an agent, and its two limits",
        ["The fixed menu could only answer with a key from", "ACTIONS[code], and 11 of 16 codes have none — a", "property of the menu, not of the finding. It declined", "432 of 526 findings before calling a model at all.", "", "An agent rules claims out of the merge instead, which", "is available on every code. That is how 334 tranche", "findings reached a model for the first time, and why a", "ruling survives the next backfill derive.", "", "It may never mark a row verified: that means an", "operator says so, and it feeds confidence. And its", "edits are recorded as 'agent', never as 'operator'."],
        role="red", title_size=13, align="left", sub_size=10.2,
    )

    c.footer(
        "The layers are ordered by cost, and the free ones are not a warm-up: rules and collisions settle most of what is wrong, "
        "and only what survives them is worth a model call."
    )
    return c


# fmt: on

DIAGRAMS = {
    "enrich": enrich,
    "sync": sync,
    "duplicates": duplicates,
    "logic": logic,
}


def main(argv: list[str]) -> int:
    out = Path(__file__).resolve().parent.parent / "docs" / "workflows"
    out.mkdir(parents=True, exist_ok=True)
    wanted = argv or list(DIAGRAMS)
    unknown = [name for name in wanted if name not in DIAGRAMS]
    if unknown:
        print(f"unknown diagram(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(DIAGRAMS)}", file=sys.stderr)
        return 2
    for name in wanted:
        path = out / f"{name}.svg"
        DIAGRAMS[name]().save(path)
        print(f"wrote {path.relative_to(path.parents[2])}  ({path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
