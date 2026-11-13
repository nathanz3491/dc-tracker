"""The menu: every kind of step a night can run, and the fixed schedule built from it.

A step is one `log.phase` line of the night and the `tracker` commands under it. The
commands, their order and the phase lines were byte-for-byte what `Night.run_round`
ran before steps existed, and the tests that pin every command of a fixed night pin
them still; what has changed since is changed there deliberately (2026-11-10: the rule
reader after the news, and the free phase matching rules to campuses). What the module
docstring of `tracker.overnight` argues about each phase's place still holds; this
module only makes each phase a value that something other than the fixed order can
choose.

Each kind also carries what the director needs to choose it well and what the referee
needs to stop it choosing badly: the unit its size is counted in, the range a size
may take, the night's floor, whether it rules on published values (and so may never
run at less than today's reasoning effort), and the count its result is read in.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Final

#: The twin pass: rows created in the last TWIN_DAYS days, at most TWINS of them a
#: night. See `tracker.overnight`, which re-exports both.
TWINS: Final = 60
TWIN_DAYS: Final = 3

#: Searches the fixed schedule's news watch leaves for the research after it.
SEARCHES_KEPT: Final = 500

EFFORTS: Final = ("low", "high", "max")
#: Effort order, for "never below".
EFFORT_RANK: Final = {name: i for i, name in enumerate(EFFORTS)}

#: The environment variable each effort tier is set through (`tracker.config`).
AGENT_EFFORT: Final = "TRACKER_DEEPSEEK_AGENT_EFFORT"
JUDGEMENT_EFFORT: Final = "TRACKER_DEEPSEEK_JUDGEMENT_EFFORT"
EXTRACTION_EFFORT: Final = "TRACKER_DEEPSEEK_EXTRACTION_EFFORT"

#: The director's note to the agents of one step (`tracker.agent.run` reads it).
NOTE_ENV: Final = "TRACKER_DIRECTOR_NOTE"
NOTE_CHARS: Final = 300


@dataclass(frozen=True)
class Kind:
    """One kind of step: what it is, and every limit on choosing it."""

    key: str
    #: What one unit of `size` is.
    unit: str
    #: One line, in plain words, for the director's manual.
    does: str
    paid: bool
    #: Decides a published value or deletes a row. Its effort never goes below today's.
    ruling: bool
    #: Which effort tier a step's `effort` sets, or None when it has none to set.
    effort_env: str | None = None
    #: Effort may go below today's: only where a model looks for pages.
    lowerable: bool = False
    #: At most once a night.
    once: bool = False
    #: The range a size may take, and the fixed schedule's size per round.
    lo: int = 0
    hi: int = 0
    default: int = 0
    #: The night's minimum, in units. 0: none.
    floor: int = 0
    #: Range for the article budget (enrich kinds only).
    budget_hi: int = 0
    #: The count its result is read in: values, findings, groups, below, rows, rules.
    result: str = "values"
    #: The director may choose it. The frame's steps (news, conditions, twins, free)
    #: are placed by the referee, never by the director.
    choosable: bool = True


KINDS: Final[dict[str, Kind]] = {
    k.key: k
    for k in (
        Kind(
            "news",
            "articles",
            "the morning's news: polls the feeds, walks the archives, runs the campus "
            "news watch (searches) and reads the queued articles. Always first, once.",
            paid=True,
            ruling=True,
            effort_env=EXTRACTION_EFFORT,
            once=True,
            lo=300,
            hi=2000,
            default=1350,
            result="rows",
            choosable=False,
        ),
        Kind(
            "conditions",
            "pages",
            "rules that apply to a place (a state's permit pause, a town's ban, a grid "
            "operator's large-load rule), read from pages the news saved in the last two "
            "days: one model call per page a free word test says may state one. Right "
            "after the news, once. Edits no campus's values; which campuses a rule "
            "reaches is matched by code.",
            paid=True,
            ruling=False,
            effort_env=JUDGEMENT_EFFORT,
            once=True,
            lo=0,
            hi=300,
            default=150,
            result="rules",
            choosable=False,
        ),
        Kind(
            "twins",
            "rows",
            "one fold attempt for each row created in the last three days, against its "
            "likeliest twin. Right after the news, once.",
            paid=True,
            ruling=True,
            effort_env=AGENT_EFFORT,
            once=True,
            lo=TWINS,
            hi=TWINS,
            default=TWINS,
            result="groups",
            choosable=False,
        ),
        Kind(
            "free",
            "-",
            "dates, Census places, scope re-gate, derive, which campuses each rule for a "
            "place reaches, mechanical logic repairs. No model, no cost. Starts every round.",
            paid=False,
            ruling=False,
            result="values",
            choosable=False,
        ),
        Kind(
            "duplicates",
            "pairs",
            "the duplicate agent reads both rows of a suspected pair and folds or parks "
            "them. The only step that deletes rows.",
            paid=True,
            ruling=True,
            effort_env=AGENT_EFFORT,
            lo=10,
            hi=80,
            default=40,
            floor=20,
            result="groups",
        ),
        Kind(
            "audit",
            "findings",
            "a model rules on implausible figures (a T1 gate). One call each; cheap.",
            paid=True,
            ruling=True,
            effort_env=JUDGEMENT_EFFORT,
            lo=10,
            hi=120,
            default=60,
            floor=30,
            result="below",
        ),
        Kind(
            "risks",
            "obstacles",
            "reads the article behind each unquoted obstacle and confirms or drops it. "
            "Cheap; moves T3.",
            paid=True,
            ruling=True,
            effort_env=JUDGEMENT_EFFORT,
            lo=10,
            hi=80,
            default=40,
            floor=20,
            result="below",
        ),
        Kind(
            "logic",
            "findings",
            "the logic agent reads the sources behind a contradiction and rules wrong "
            "claims out. About 290 open findings are tranche identity, which it cannot "
            "answer.",
            paid=True,
            ruling=True,
            effort_env=AGENT_EFFORT,
            lo=0,
            hi=120,
            default=60,
            # One fixed round's worth. On 2026-11-06, the first directed night, the
            # director ran six findings all night, blaming the rulings for an answer the
            # news read had moved, and open findings rose by 17 against 6 the night before.
            floor=60,
            result="findings",
        ),
        Kind(
            "suspects",
            "values",
            "the logic agent re-reads the values the suspicion score ranks likeliest "
            "wrong tonight. Once a night.",
            paid=True,
            ruling=True,
            effort_env=AGENT_EFFORT,
            once=True,
            lo=0,
            hi=50,
            default=25,
            # Skipped altogether on 2026-11-06. Its verdicts are also what the suspicion
            # score is retrained from each morning, so a night without them teaches it
            # nothing.
            floor=25,
            result="values",
        ),
        Kind(
            "tranches",
            "campuses",
            "a model nests the tranches of campuses whose megawatts are counted twice.",
            paid=True,
            ruling=True,
            effort_env=JUDGEMENT_EFFORT,
            lo=5,
            hi=40,
            default=20,
            floor=10,
            result="findings",
        ),
        Kind(
            "pipeline",
            "campuses",
            "enrich, on campuses being built or permitted with no online date or "
            "capacity, largest first, asking only those two fields. Searches the web.",
            paid=True,
            ruling=False,
            effort_env=AGENT_EFFORT,
            lowerable=True,
            lo=0,
            hi=90,
            default=45,
            budget_hi=360,
            result="values",
        ),
        Kind(
            "enrich",
            "rows",
            "enrich, on rows below T2 for missing fields, fewest missing first. "
            "Searches the web; the dearest step per row (~77,000 tokens).",
            paid=True,
            ruling=False,
            effort_env=AGENT_EFFORT,
            lowerable=True,
            lo=0,
            hi=150,
            default=75,
            budget_hi=600,
            result="values",
        ),
        Kind(
            "rows",
            "rows",
            "enrich on rows YOU name, for the fields you name, with its own article "
            "budget. How to put effort on particular rows.",
            paid=True,
            ruling=False,
            effort_env=AGENT_EFFORT,
            lowerable=True,
            lo=1,
            hi=40,
            default=0,
            budget_hi=200,
            result="values",
        ),
        Kind(
            "reread",
            "articles",
            "re-reads, from the cache, articles a superseded prompt last read. Once a "
            "night; nothing fetched.",
            paid=True,
            ruling=True,
            effort_env=EXTRACTION_EFFORT,
            once=True,
            lo=0,
            hi=360,
            default=180,
            floor=180,
            result="values",
        ),
        Kind(
            "recheck",
            "articles",
            "re-reads, from the cache, articles holding a value the gate refused before "
            "its last revision. Once a night.",
            paid=True,
            ruling=True,
            effort_env=EXTRACTION_EFFORT,
            once=True,
            lo=0,
            hi=360,
            default=180,
            floor=180,
            result="values",
        ),
    )
}

#: The kinds that read a row the duplicate pass may fold, so it goes before them in a
#: round; and the kinds the tranche review reads the result of.
AFTER_DUPLICATES: Final = frozenset(
    {"audit", "risks", "logic", "suspects", "tranches", "pipeline", "enrich", "rows"}
)
AFTER_LOGIC: Final = frozenset({"tranches"})
#: The enrich passes that pick their own rows: a second one the same night is `--fresh`.
SELECTING: Final = frozenset({"pipeline", "enrich"})


@dataclass
class Step:
    """One planned step: a kind, its settings, and who asked for it."""

    kind: str
    size: int = 0
    #: Articles, for the enrich kinds.
    budget: int = 0
    #: The news step only: the watch's searches and its first looks.
    searches: int = 0
    baselines: int = 0
    fresh: bool = False
    rows: tuple[int, ...] = ()
    fields: tuple[str, ...] = ()
    #: The logic step only: one kind of finding, or "".
    code: str = ""
    effort: str | None = None
    note: str = ""
    skip_search: bool = False
    #: Who asked: fixed, director, floor, frame, fallback, replay.
    origin: str = "fixed"
    why: str = ""
    #: The director's prediction: cny, searches, minutes, result.
    expect: dict[str, float] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["rows"] = list(self.rows)
        out["fields"] = list(self.fields)
        return {k: v for k, v in out.items() if v not in ((), [], "", None, {}) or k == "kind"}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Step:
        names = {f.name for f in dataclasses.fields(cls)}
        kept = {k: v for k, v in data.items() if k in names}
        kept["rows"] = tuple(int(r) for r in kept.get("rows") or ())
        kept["fields"] = tuple(str(f) for f in kept.get("fields") or ())
        return cls(**kept)


@dataclass(frozen=True)
class Command:
    """One `tracker` command of a step, and the ceiling check that must pass first."""

    args: tuple[str, ...]
    #: `Night.capped(gate)` before this command, or None.
    gate: str | None = None


@dataclass(frozen=True)
class Context:
    """What a step's commands depend on beyond the step itself."""

    options: Any
    number: int
    twin_since: str
    #: The news step placed the new rows this round, so `free` need not.
    placed: bool = False


def label(step: Step, c: Context) -> str:
    """The step's `log.phase` line, exactly as the fixed loop printed it."""
    n, k = step.size, step.kind
    if k == "news":
        return f"news — feeds and archives, the campus news watch, {n} queued article(s) read"
    if k == "conditions":
        return f"conditions — rules that apply to a place, in up to {n} saved page(s) the news read"
    if k == "twins":
        return f"duplicates — one attempt per row created in the last {TWIN_DAYS} day(s), {TWINS} at most"
    if k == "free":
        return "free — dates, geo, scope, derive, conditions match, logic --auto"
    if k == "duplicates":
        return f"duplicates — {n} pair(s)"
    if k == "audit":
        return f"audit — implausible figures, {n} at a time"
    if k == "risks":
        return f"risks — unquoted obstacles, {n} at a time"
    if k == "logic":
        return (
            f"logic — a model reads the sources, {n} at a time"
            + (f", {step.code} only" if step.code else "")
            + (f", {len(step.rows)} named row(s)" if step.rows else "")
        )
    if k == "suspects":
        return f"suspects — a model re-reads the {n} likeliest-wrong value(s)"
    if k == "tranches":
        return f"tranches — a model nests the tranches of {n} campus(es) that do not add up"
    if k == "pipeline":
        return (
            f"enrich — {n} campus(es) being built or permitted with no date or capacity, "
            f"{step.budget} article budget"
        )
    if k == "enrich":
        return f"enrich — {n} row(s) below T2, {step.budget} article budget"
    if k == "rows":
        asked = f", {', '.join(step.fields)}" if step.fields else ""
        return f"enrich — {len(step.rows)} row(s) the director chose{asked}, {step.budget} article budget"
    if k == "reread":
        return f"reread — {n} article(s) last read by a superseded prompt"
    if k == "recheck":
        return f"recheck — {n} article(s) holding a refusal from before the gate's last revision"
    raise KeyError(k)


def commands(step: Step, c: Context) -> list[Command]:
    """The `tracker` commands one step runs, in order."""
    o, n, k = c.options, step.size, step.kind
    fresh = ("--fresh",) if step.fresh else ()
    if k == "news":
        return [
            Command(("discover", "--no-show")),
            Command(
                ("news", "--baselines", str(step.baselines), "--max-queries", str(step.searches))
            ),
            Command(
                (
                    "ingest",
                    "queue",
                    "--new-first",
                    "--limit",
                    str(n),
                    "--screen" if o.screen else "--no-screen",
                ),
                gate="news",
            ),
            Command(("ingest", "geo")),
        ]
    if k == "conditions":
        # Gated at the step, like every other paid step (`gate`), and inside the
        # command too: `conditions read` asks the night's money ceiling before each
        # wave of pages (`judged.run`) and says "spend ceiling reached" if it stops.
        return [Command(("conditions", "read", "--limit", str(n)))]
    if k == "twins":
        if o.merge:
            return [
                Command(
                    (
                        "duplicates", "resolve", "--merge", "--min-confidence", str(o.min_confidence),
                        "--created-since", c.twin_since, "--limit", str(TWINS),
                    )
                )
            ]  # fmt: skip
        return [
            Command(
                ("duplicates", "resolve", "--created-since", c.twin_since, "--limit", str(TWINS))
            )
        ]
    if k == "free":
        out = [
            Command(("backfill", "dates", "--apply", "--refetch", "--yes", "--limit", str(o.dates)))
            if c.number == 1 and o.dates > 0
            else Command(("backfill", "dates", "--apply"))
        ]
        if not c.placed:
            out.append(Command(("ingest", "geo")))
        out += [
            Command(("backfill", "scope", "--apply")),
            # Before derive, which then re-derives what it reopened: answers a report
            # published since says changed, superseded rulings a report went back to.
            # Code only, free.
            Command(("logic", "expire", "--apply")),
            Command(("backfill", "derive")),
            # After derive, which settles each row's stage and county: a campus placed,
            # merged or moved on since the last match is matched to the rules for its
            # place now, not tomorrow. Code only, free.
            Command(("conditions", "match")),
            Command(("logic", "resolve", "--auto", "--apply")),
        ]
        return out
    if k == "duplicates":
        if o.merge:
            args = (
                "duplicates",
                "resolve",
                "--merge",
                "--limit",
                str(n),
                "--min-confidence",
                str(o.min_confidence),
            )
        else:
            args = ("duplicates", "resolve", "--limit", str(n))
        return [Command(args)]
    if k == "audit":
        return [Command(("audit", "resolve", "--no-ask", "--limit", str(n)))]
    if k == "risks":
        return [Command(("risks", "confirm", "--limit", str(n)))]
    if k == "logic":
        args: tuple[str, ...] = ("logic", "resolve", "--limit", str(n))
        if step.code:
            args += ("--code", step.code)
        for row in step.rows:
            args += ("--project", str(row))
        return [Command(args)]
    if k == "suspects":
        return [Command(("logic", "resolve", "--suspects", str(n), "--limit", str(n)))]
    if k == "tranches":
        return [Command(("tranches", "review", "--limit", str(n)))]
    if k == "pipeline":
        return [
            Command(
                ("enrich", "--select", str(n), "--pipeline", "--budget", str(step.budget), *fresh)
            )
        ]
    if k == "enrich":
        return [
            Command(
                (
                    "enrich",
                    "--select",
                    str(n),
                    "--t2",
                    "--target",
                    "0",
                    "--budget",
                    str(step.budget),
                    *fresh,
                )
            )
        ]
    if k == "rows":
        args = ("enrich", *map(str, step.rows), "--target", "0", "--budget", str(step.budget))
        if step.fields:
            args += ("--fields", ",".join(step.fields))
        if step.skip_search:
            args += ("--skip-search",)
        return [Command(args)]
    if k == "reread":
        return [
            Command(("ingest", "reread", "--cached-only", "--existing-only", "--limit", str(n)))
        ]
    if k == "recheck":
        return [
            Command(("ingest", "recheck", "--cached-only", "--existing-only", "--limit", str(n)))
        ]
    raise KeyError(k)


def gate(step: Step) -> str | None:
    """The ceiling check before the step's phase line, as the fixed loop made it."""
    return {
        "news": None,
        "free": None,
        "twins": "duplicates",
        "enrich": "enrich",
        "rows": "enrich",
    }.get(step.kind, step.kind)


def env(step: Step) -> dict[str, str]:
    """What a step adds to its commands' environment: its effort and its note."""
    out: dict[str, str] = {}
    kind = KINDS[step.kind]
    if step.effort and kind.effort_env:
        out[kind.effort_env] = step.effort
    if step.note:
        out[NOTE_ENV] = step.note[:NOTE_CHARS]
    return out


def fixed_round(o: Any, number: int) -> list[Step]:
    """The fixed schedule's round `number`, step by step — `Night.run_round` as it was.

    The order is the argument the `tracker.overnight` docstring makes: the news first
    (round 1), the rule reader right after it (round 1: it reads the pages the news
    just saved), the twin pass, the free phase, duplicates ahead of every phase that
    pays to read a row, audit, risks, logic, the suspects (round 1), tranches, the two
    enrich passes (the first `enrich_rounds` rounds, `--fresh` after the first), then
    reread and recheck (round 1).

    The rule reader has its own switch (`--conditions`), not the news's: it reads the
    pages saved over the last two days, so a night without discovery still has
    yesterday's to read.
    """
    out: list[Step] = []
    first = number == 1
    if o.do_discover and first:
        out.append(
            Step(
                "news",
                size=o.discover,
                searches=o.search_cap - SEARCHES_KEPT
                if o.search_cap > SEARCHES_KEPT
                else o.search_cap,
                baselines=o.baselines,
            )
        )
    if first and o.conditions > 0:
        out.append(Step("conditions", size=o.conditions))
    if o.do_discover and first:
        out.append(Step("twins", size=TWINS))
    out.append(Step("free"))
    out.append(Step("duplicates", size=o.pairs))
    out.append(Step("audit", size=o.audit))
    out.append(Step("risks", size=o.risks))
    out.append(Step("logic", size=o.findings))
    if number == 1 and o.suspects > 0:
        out.append(Step("suspects", size=o.suspects))
    out.append(Step("tranches", size=o.nest))
    fresh = number > 1
    if number <= o.enrich_rounds and o.pipeline > 0:
        out.append(Step("pipeline", size=o.pipeline, budget=o.pipeline_budget, fresh=fresh))
    if o.do_enrich and number <= o.enrich_rounds:
        out.append(Step("enrich", size=o.enrich, budget=o.enrich_budget, fresh=fresh))
    if number == 1 and o.reread > 0:
        out.append(Step("reread", size=o.reread))
    if number == 1 and o.recheck > 0:
        out.append(Step("recheck", size=o.recheck))
    return out


def defaults_per_night(o: Any) -> dict[str, int]:
    """What a fixed night asks of each kind, in units, when it runs its two busy rounds.

    The baseline the referee's drift rule measures against until the record holds
    fourteen fixed nights of its own.
    """
    rounds = max(1, o.enrich_rounds)
    return {
        "news": o.discover,
        "conditions": o.conditions,
        "twins": TWINS,
        "duplicates": o.pairs * 2,
        "audit": o.audit * 2,
        "risks": o.risks * 2,
        "logic": o.findings * 2,
        "suspects": o.suspects,
        "tranches": o.nest * 2,
        "pipeline": o.pipeline * rounds,
        "enrich": o.enrich * rounds,
        "rows": 40,
        "reread": o.reread,
        "recheck": o.recheck,
    }


def manual(o: Any) -> str:
    """The menu as the director reads it: one entry per kind it may choose."""
    lines = []
    for kind in KINDS.values():
        flags = []
        if kind.once:
            flags.append("once a night")
        if kind.floor:
            flags.append(f"night floor {kind.floor} {kind.unit}")
        if kind.ruling:
            flags.append("rules on published values: effort never below today's")
        elif kind.lowerable:
            flags.append("effort may be lowered")
        size = f"size {kind.lo}-{kind.hi} {kind.unit}" if kind.hi else "no size"
        if kind.budget_hi:
            size += f", budget 0-{kind.budget_hi} articles"
        who = "" if kind.choosable else " [placed by the referee, not chosen]"
        lines.append(
            f"- {kind.key}{who}: {kind.does} ({size}; result read in {kind.result}"
            + (f"; {'; '.join(flags)}" if flags else "")
            + ")"
        )
    return "\n".join(lines)


__all__ = [
    "AFTER_DUPLICATES",
    "AFTER_LOGIC",
    "EFFORTS",
    "EFFORT_RANK",
    "KINDS",
    "NOTE_CHARS",
    "NOTE_ENV",
    "SEARCHES_KEPT",
    "SELECTING",
    "TWINS",
    "TWIN_DAYS",
    "Command",
    "Context",
    "Kind",
    "Step",
    "commands",
    "defaults_per_night",
    "env",
    "fixed_round",
    "gate",
    "label",
    "manual",
]
