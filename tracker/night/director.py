"""The director: a model that chooses the night's steps, in one conversation a night.

It checks in at the start of each round and after every three of its steps have run
(`tracker.overnight`). Each check-in is a user turn with what ran, what it cost and
what it changed; the model may call the read-only lookups (`briefing.Lookups`) and
then answers by calling `submit_plan` or `stop_night`. The referee's verdict on the
plan is the tool result for that call, so the model reads every clamp and refusal at
its next turn. One conversation, so it remembers what it planned and why:

* **append-only**, like `tracker.agent.run`: DeepSeek caches on the message prefix,
  so the repeated part of a growing conversation bills at about a fiftieth;
* **folded** when the prompt passes :data:`FOLD_AT` tokens: the next check-in starts
  a fresh conversation with the briefing and the night so far as a table;
* **bounded**: its own spend (ledger command `director`) against :data:`BUDGET_CNY`,
  :data:`MAX_TURNS` turns and :data:`LOOKUPS` lookups per check-in, and a check-in
  that runs past :data:`CHECK_IN_MINUTES`. Any of them, or two invalid plans in a row,
  raises :class:`DirectorFailed`, and the night runs the fixed schedule from there. A
  provider that fails is asked again after :data:`PATIENCE` waits, inside those minutes;
* **a stop is asked once**: when `confirm_stop` says much of the night is left, the
  first `stop_night` of a check-in is answered with what a stop gives up, and only a
  second one stops. On 2026-11-08 the director stopped at 21:21 with 361 searches left,
  having read two values set back to a week ago as answers lost.

Everything said and decided goes to the night's plan file
(`data/runs/director-<night>.jsonl`): the briefing, every check-in, the raw replies,
each plan, the referee's notes, every step as it ran. :class:`ReplayDirector` runs a
night again from that file, without a model.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from tracker.night.steps import EFFORTS, KINDS, Step

#: The instructions, beside this module.
SYSTEM_FILE: Final = Path(__file__).with_name("director.txt")
#: The director's own money a night, inside the night's ceiling.
BUDGET_CNY: Final = 5.0
#: Its steps between check-ins.
CHECK_IN_EVERY: Final = 3
MAX_TURNS: Final = 8
LOOKUPS: Final = 6
CHECK_IN_MINUTES: Final = 15
#: Seconds to wait before asking again when the provider fails, inside the check-in's
#: minutes. The client's own retries give up within seconds; on 2026-11-07 OpenCode Go
#: answered "the cell is at capacity; retry shortly" 224 times in one night, and one of
#: them ended the background predictions at the first check-in.
PATIENCE: Final = (30, 60, 120, 240)
#: Prompt tokens past which the conversation is folded into a table.
FOLD_AT: Final = 100_000
MAX_TOKENS: Final = 20_000
#: Characters of any lookup result handed back.
RESULT_CHARS: Final = 12_000

#: The operator's note for the next night, and the plan files, beside the log.
NOTE_FILE: Final = "director-note.txt"
PLAN_PREFIX: Final = "director-"
#: The ledger's command column for the director's calls.
LEDGER_COMMAND: Final = "director"

CHOOSABLE: Final = tuple(k for k, kind in KINDS.items() if kind.choosable)


class DirectorFailed(RuntimeError):
    """The director cannot go on tonight; the night runs the fixed schedule."""


def system_prompt(options: Any) -> str:
    from tracker.night import briefing, steps

    return (
        SYSTEM_FILE.read_text(encoding="utf-8").strip()
        + "\n\n=== THE MENU ===\n"
        + steps.manual(options)
        + "\n\n=== LESSONS FROM PAST NIGHTS ===\n"
        + briefing.lessons()
    )


# --- the plan ---------------------------------------------------------------------------


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    #: The news step's settings: searches, queue, baselines, effort.
    news: dict[str, Any] | None = None
    end_round: bool = True
    stop: bool = False
    why: str = ""
    #: Shadow nights: predictions for the fixed steps about to run, by index.
    predictions: list[dict[str, Any]] = field(default_factory=list)


_EXPECT = {
    "type": "object",
    "properties": {
        "cny": {"type": "number"},
        "searches": {"type": "number"},
        "minutes": {"type": "number"},
        "result": {"type": "number"},
    },
}

PLAN_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "why": {
            "type": "string",
            "description": "one or two plain sentences: what this plan is for",
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string", "enum": list(CHOOSABLE)},
                    "size": {"type": "integer"},
                    "budget": {"type": "integer", "description": "articles, enrich kinds only"},
                    "rows": {"type": "array", "items": {"type": "integer"}},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "code": {"type": "string", "description": "logic only: one finding code"},
                    "effort": {"type": "string", "enum": list(EFFORTS)},
                    "skip_search": {"type": "boolean", "description": "rows only"},
                    "note": {
                        "type": "string",
                        "description": "300 characters for the step's agents",
                    },
                    "why": {"type": "string"},
                    "expect": _EXPECT,
                },
                "required": ["step", "expect"],
            },
        },
        "news": {
            "type": "object",
            "description": "round 1 only: the news step's searches, queue and first looks",
            "properties": {
                "searches": {"type": "integer"},
                "queue": {"type": "integer"},
                "baselines": {"type": "integer"},
                "effort": {"type": "string", "enum": list(EFFORTS)},
                "expect": _EXPECT,
            },
        },
        "end_round": {"type": "boolean", "description": "settle this round after these steps"},
        "predictions": {
            "type": "array",
            "description": "shadow nights only: your prediction for each fixed step listed, by index",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, **_EXPECT["properties"]},
                "required": ["index"],
            },
        },
    },
    "required": ["why", "steps"],
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def expect_of(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k in ("cny", "searches", "minutes", "result")
        if (v := _number(raw.get(k))) is not None
    }


def parse_plan(args: Any) -> tuple[Plan | None, list[str]]:
    """A plan from `submit_plan`'s arguments, or the reasons it is not one.

    Shape only: whether a step is *allowed* is the referee's question. A step with an
    unknown kind is kept for the referee to refuse in words the model will read.
    """
    if not isinstance(args, dict):
        return None, ["the arguments were not an object"]
    errors: list[str] = []
    raw_steps = args.get("steps")
    if not isinstance(raw_steps, list):
        return None, ["`steps` must be a list (it may be empty)"]
    steps: list[Step] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict) or not isinstance(raw.get("step"), str):
            errors.append(f"step {i}: needs a `step` name")
            continue
        try:
            rows = tuple(int(r) for r in raw.get("rows") or ())
            fields = tuple(str(f) for f in raw.get("fields") or ())
            size = int(raw.get("size") or (len(rows) if raw["step"] == "rows" else 0))
            budget = int(raw.get("budget") or 0)
        except (TypeError, ValueError):
            errors.append(f"step {i} ({raw['step']}): size, budget and rows must be integers")
            continue
        effort = raw.get("effort")
        steps.append(
            Step(
                raw["step"],
                size=size,
                budget=budget,
                rows=rows,
                fields=fields,
                code=str(raw.get("code") or ""),
                effort=effort if isinstance(effort, str) else None,
                note=str(raw.get("note") or ""),
                skip_search=bool(raw.get("skip_search")),
                origin="director",
                why=str(raw.get("why") or "")[:400],
                expect=expect_of(raw.get("expect")),
            )
        )
    if errors:
        return None, errors
    news = args.get("news") if isinstance(args.get("news"), dict) else None
    predictions = [p for p in args.get("predictions") or () if isinstance(p, dict) and "index" in p]
    return (
        Plan(
            steps=steps,
            news=news,
            end_round=bool(args.get("end_round", True)),
            why=str(args.get("why") or "")[:600],
            predictions=predictions,
        ),
        [],
    )


# --- the plan file ----------------------------------------------------------------------


class PlanFile:
    """The night's record of everything the director was told and decided. Append-only."""

    def __init__(self, path: Path | None, clock: Callable[[], dt.datetime]):
        self.path = path
        self.clock = clock

    def write(self, kind: str, **data: Any) -> None:
        if self.path is None:
            return
        line = {"at": self.clock().isoformat(timespec="seconds"), "kind": kind, **data}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def plan_path(directory: Path, night: str) -> Path:
    return directory / f"{PLAN_PREFIX}{night}.jsonl"


# --- the operator's note ----------------------------------------------------------------


def read_note(directory: Path) -> str:
    try:
        return (directory / NOTE_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_note(directory: Path, text: str) -> Path:
    path = directory / NOTE_FILE
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def clear_note(directory: Path) -> bool:
    path = directory / NOTE_FILE
    if path.is_file():
        path.unlink()
        return True
    return False


# --- the model director -----------------------------------------------------------------


def _tools(lookups: Sequence[Any]) -> list[Any]:
    from tracker.agent import Tool

    return [
        *lookups,
        Tool(
            "submit_plan",
            "The steps for the rest of this round, in order; they replace whatever of your "
            "earlier plan has not run. The referee's verdict comes back as this call's result.",
            PLAN_SCHEMA,
            terminal=True,
        ),
        Tool(
            "stop_night",
            "End the night after this round settles. Nothing runs after it — the fixed "
            "schedule does not take over — and the searches and hours left go unused. Say "
            "why, in plain words.",
            {"type": "object", "properties": {"why": {"type": "string"}}, "required": ["why"]},
            terminal=True,
        ),
    ]


class ModelDirector:
    """The model's side of the night. See the module docstring."""

    def __init__(
        self,
        *,
        extractor: Any,
        system: str,
        briefing: str,
        lookups: Sequence[Any],
        plan_file: PlanFile,
        spent: Callable[[], float],
        clock: Callable[[], dt.datetime],
        history: Callable[[], str],
        mode: str = "director",
        budget: float = BUDGET_CNY,
        confirm_stop: Callable[[], str | None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.extractor = extractor
        self.confirm_stop = confirm_stop
        self.sleep = sleep or time.sleep
        self.system = system
        self.briefing = briefing
        self.tools = _tools(lookups)
        self.by_name = {t.name: t for t in self.tools}
        self.schemas = [t.schema() for t in self.tools]
        self.plan_file = plan_file
        self.spent = spent
        self.clock = clock
        self.history = history
        self.mode = mode
        self.budget = budget
        self.messages: list[dict[str, Any]] = []
        self.held: str | None = None
        self.prompt_tokens = 0
        self.check_ins = 0
        self.fresh = True
        plan_file.write("briefing", mode=mode, system=system, briefing=briefing)

    # The referee's verdict is the tool result of the call that submitted the plan.
    def answer(self, text: str) -> None:
        if self.held is None:
            return
        self.messages.append({"role": "tool", "tool_call_id": self.held, "content": text})
        self.plan_file.write("verdict", text=text)
        self.held = None

    def _fold(self) -> str:
        """Start the conversation again when it has grown past FOLD_AT tokens."""
        if self.fresh:
            self.fresh = False
            return self.briefing + "\n\n"
        if self.prompt_tokens < FOLD_AT:
            return ""
        self.messages = []
        self.plan_file.write("folded", prompt_tokens=self.prompt_tokens)
        return (
            self.briefing
            + "\n\n=== THE NIGHT SO FAR (earlier check-ins folded into this table) ===\n"
            + self.history()
            + "\n\n"
        )

    def _call(self, started: dt.datetime) -> Any:
        """One model turn. A provider error is asked again after each of PATIENCE's waits
        that still ends inside the check-in's minutes; a missing key or an empty balance
        (`LLMUnavailable`) is not, since waiting does not fix it."""
        from tracker.llm import LLMError, LLMUnavailable, spend_command, spend_stage

        limit = dt.timedelta(minutes=CHECK_IN_MINUTES)
        waits = list(PATIENCE)
        while True:
            if self.spent() >= self.budget:
                raise DirectorFailed(f"its ¥{self.budget:g} for the night is spent")
            try:
                with spend_command(LEDGER_COMMAND), spend_stage(self.mode):
                    return self.extractor.converse(
                        system=self.system,
                        messages=self.messages,
                        tools=self.schemas,
                        max_tokens=MAX_TOKENS,
                    )
            except LLMUnavailable as exc:
                raise DirectorFailed(f"the model failed: {exc}") from exc
            except LLMError as exc:
                wait = waits.pop(0) if waits else None
                if wait is None or self.clock() - started + dt.timedelta(seconds=wait) > limit:
                    raise DirectorFailed(f"the model failed: {exc}") from exc
                self.plan_file.write("retry", wait=wait, why=str(exc)[:300])
                self.sleep(wait)

    def check_in(self, message: str, **_where: Any) -> Plan:
        """One check-in: the night's news in, a plan (or a stop) out."""
        if self.held is not None:
            self.answer("accepted as proposed")
        self.check_ins += 1
        text = self._fold() + message
        self.messages.append({"role": "user", "content": text})
        self.plan_file.write("checkin", number=self.check_ins, message=message)
        started = self.clock()
        lookups = invalid = 0
        nudged = stop_asked = False
        for _turn in range(MAX_TURNS):
            if self.clock() - started > dt.timedelta(minutes=CHECK_IN_MINUTES):
                raise DirectorFailed(f"a check-in ran past {CHECK_IN_MINUTES} minutes")
            reply = self._call(started)
            self.prompt_tokens = reply.prompt_tokens or self.prompt_tokens
            if not reply.tool_calls:
                self.messages.append({"role": "assistant", "content": reply.text or ""})
                self.plan_file.write("prose", text=(reply.text or "")[:2000])
                if nudged:
                    raise DirectorFailed("it answered in prose twice instead of with a plan")
                self.messages.append(
                    {"role": "user", "content": "Answer by calling submit_plan or stop_night."}
                )
                nudged = True
                continue
            turn: dict[str, Any] = {
                "role": "assistant",
                "content": reply.text or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments or "{}"},
                    }
                    for c in reply.tool_calls
                ],
            }
            if getattr(reply, "reasoning", ""):
                turn["reasoning_content"] = reply.reasoning
            self.messages.append(turn)
            terminal = None
            for call in reply.tool_calls:
                tool = self.by_name.get(call.name)
                if tool is not None and tool.terminal and terminal is None:
                    terminal = call
                    continue
                if tool is not None and tool.terminal:
                    payload = "one plan per turn: this second call was ignored"
                elif tool is None:
                    payload = f"no such tool: {call.name}"
                else:
                    lookups += 1
                    if lookups > LOOKUPS:
                        payload = f"lookups are used up for this check-in ({LOOKUPS}); decide now"
                    elif call.parse_failed:
                        payload = "your arguments were not a JSON object"
                    else:
                        try:
                            payload = str(tool.run(**call.arguments))[:RESULT_CHARS]
                        except Exception as exc:  # a lookup that fails is an answer, not a crash
                            payload = f"{call.name} failed: {exc}"
                    self.plan_file.write("lookup", tool=call.name, arguments=call.arguments)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})
            if terminal is None:
                continue
            self.plan_file.write(
                "reply",
                tool=terminal.name,
                arguments=terminal.arguments,
                reasoning=(getattr(reply, "reasoning", "") or "")[:4000],
            )
            if terminal.name == "stop_night" and not stop_asked and self.confirm_stop:
                stop_asked = True
                question = self.confirm_stop()
                if question:
                    self.messages.append(
                        {"role": "tool", "tool_call_id": terminal.id, "content": question}
                    )
                    self.plan_file.write("stop_check", text=question)
                    continue
            if terminal.parse_failed:
                plan, errors = None, ["the arguments were not a JSON object"]
            elif terminal.name == "stop_night":
                plan, errors = (
                    Plan(stop=True, why=str(terminal.arguments.get("why") or "")[:600]),
                    [],
                )
            else:
                plan, errors = parse_plan(terminal.arguments)
            if errors or plan is None:
                invalid += 1
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": terminal.id,
                        "content": "not a valid plan: "
                        + "; ".join(errors)
                        + ". Call submit_plan again.",
                    }
                )
                self.plan_file.write("invalid", errors=errors)
                if invalid >= 2:
                    raise DirectorFailed("two invalid plans in one check-in")
                continue
            self.held = terminal.id
            return plan
        raise DirectorFailed(f"no plan after {MAX_TURNS} turns")


class ReplayDirector:
    """A night's recorded steps, run again without a model."""

    mode = "replay"

    def __init__(self, path: Path):
        self.rounds: dict[int, list[Step]] = {}
        self.news: dict[str, Any] | None = None
        for entry in PlanFile.read(path):
            if entry.get("kind") != "step":
                continue
            step = Step.from_json(entry.get("step") or {})
            number = int(entry.get("round") or 0)
            if step.kind == "news":
                self.news = {
                    "searches": step.searches,
                    "queue": step.size,
                    "baselines": step.baselines,
                }
            elif KINDS.get(step.kind) is not None and KINDS[step.kind].choosable:
                step.origin = "replay"
                self.rounds.setdefault(number, []).append(step)
        self.last = max(self.rounds, default=0)

    def answer(self, text: str) -> None:
        pass

    def check_in(
        self, message: str, *, round_number: int = 0, at_start: bool = False, **_: Any
    ) -> Plan:
        if round_number > self.last:
            return Plan(stop=True, why="the recorded night ends here")
        if not at_start:
            return Plan(steps=[], end_round=True, why="replay: the round as recorded")
        return Plan(
            steps=list(self.rounds.get(round_number, [])),
            news=self.news if round_number == 1 else None,
            end_round=True,
            why="replay",
        )


__all__ = [
    "BUDGET_CNY",
    "CHECK_IN_EVERY",
    "CHOOSABLE",
    "FOLD_AT",
    "LEDGER_COMMAND",
    "LOOKUPS",
    "MAX_TURNS",
    "NOTE_FILE",
    "PLAN_SCHEMA",
    "DirectorFailed",
    "ModelDirector",
    "Plan",
    "PlanFile",
    "ReplayDirector",
    "clear_note",
    "expect_of",
    "parse_plan",
    "plan_path",
    "read_note",
    "system_prompt",
    "write_note",
]
