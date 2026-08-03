"""Named sequences of commands, run as one job.

The console could always run any single command, which left the operator doing
the sequencing by hand: run `sync`, wait, read the output, run `ingest geo`,
wait, run `logic check`. That is three visits to the page for one routine, and
the order matters in ways nothing on the page explained — deriving geography
before reading articles wastes the derivation, and checking logic before either
reports contradictions that the run was about to fix.

So the sequences are here, named, with the reason each step follows the last.

**Not a node editor.** A general graph would need branching, per-node arguments
and a way to save one, and the four routines below cover what this database
actually needs doing. A fifth is eight lines in this file, which is cheaper than
a builder nobody asked for.

**One run, not several.** A workflow occupies the same single slot a command
does, and the whole sequence is one entry in the run history with one log. SQLite
takes one writer, so this is not a limitation being worked around — it is the
same constraint stated once instead of per step.

**Stops at the first failure.** Continuing past a failed `sync` means `enrich`
runs against data that did not arrive, and the operator reads a green finish over
a broken run. `duplicates` is the one step allowed to "fail": it exits non-zero
when it finds duplicates, which is a finding rather than an error, so it is
marked `tolerate_failure`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tracker.webui import catalog


@dataclass(frozen=True)
class Step:
    cmd: str
    flags: dict[str, Any] = field(default_factory=dict)
    #: Why this step comes after the one before it. Shown in the log and the UI,
    #: because a sequence whose order is unexplained is a sequence people reorder.
    because: str = ""
    #: A non-zero exit is a finding, not a breakage, so the run continues.
    tolerate_failure: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "flags": self.flags,
            "because": self.because,
            "tolerates_failure": self.tolerate_failure,
        }


@dataclass(frozen=True)
class Workflow:
    name: str
    title: str
    summary: str
    steps: tuple[Step, ...]

    @property
    def cost(self) -> str:
        commands = catalog.by_name()
        return (
            "llm"
            if any(getattr(commands.get(s.cmd), "cost", "free") == "llm" for s in self.steps)
            else "free"
        )

    @property
    def destroys(self) -> str | None:
        commands = catalog.by_name()
        for step in self.steps:
            destroys = getattr(commands.get(step.cmd), "destroys", None)
            if destroys:
                return destroys
        return None

    @property
    def needs_confirmation(self) -> bool:
        return self.cost == "llm" or self.destroys is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "summary": self.summary,
            "cost": self.cost,
            "destroys": self.destroys,
            "steps": [s.as_json() for s in self.steps],
        }


WORKFLOWS: tuple[Workflow, ...] = (
    Workflow(
        name="catch-up",
        title="Catch up on the news",
        summary=(
            "The routine most days want: poll the feeds, read what they turned up, "
            "then fill in the geography and check nothing contradicts itself."
        ),
        steps=(
            Step(
                "sync",
                {"--limit": 15, "--refresh-limit": 10},
                because="Finds new candidates and refreshes known rows in one pass.",
            ),
            Step(
                "ingest geo",
                because=(
                    "County and coordinates are a lookup, not research. Running it after "
                    "the read means the rows that just arrived get located too, at no "
                    "LLM cost."
                ),
            ),
            Step(
                "logic check",
                because=(
                    "New values are where contradictions come from — a row can gain a "
                    "perfectly cited phase that its own construction track disagrees with."
                ),
                tolerate_failure=True,
            ),
        ),
    ),
    Workflow(
        name="deepen",
        title="Deepen what we already have",
        summary=(
            "Grows the rows already tracked rather than finding new ones: drains the "
            "queue, retries what failed, then reports what is still missing."
        ),
        steps=(
            Step(
                "enrich",
                {"--select": 5, "--budget": 60},
                because="Throws every retrieval method at the five thinnest rows.",
            ),
            Step(
                "ingest geo",
                because="The same free lookup, over whatever enrich just created.",
            ),
            Step(
                "gaps",
                because="Says what is still missing, so the next run has a target.",
            ),
        ),
    ),
    Workflow(
        name="tidy",
        title="Tidy the database",
        summary=(
            "Writes nothing. Finds the same-campus-twice rows that make the capex "
            "table overcount, and the values that disagree with their own sources — "
            "then leaves both for you to decide on."
        ),
        steps=(
            Step(
                "duplicates",
                because="One campus stored twice double-counts its capacity by customer.",
                tolerate_failure=True,
            ),
            Step(
                "logic check",
                because="Contradictions between cited values, and which source wins.",
                tolerate_failure=True,
            ),
            Step(
                "stats",
                because="The coverage picture after both, as the number to report.",
            ),
        ),
    ),
    Workflow(
        name="report",
        title="Prepare a report",
        summary=(
            "Everything a number gets quoted from, in the order it should be read: "
            "coverage, then capacity by customer, then progress against the required list."
        ),
        steps=(
            Step("stats", because="Coverage first — it sets how much the rest is worth."),
            Step("capex", because="Capacity and investment by end customer."),
            Step(
                "verify",
                because="Progress against the required project list.",
                tolerate_failure=True,
            ),
        ),
    ),
)


def by_name() -> dict[str, Workflow]:
    return {w.name: w for w in WORKFLOWS}


def as_json() -> list[dict[str, Any]]:
    return [w.as_json() for w in WORKFLOWS]


def resolve(name: str) -> Workflow:
    """The workflow, with every step checked against the catalog.

    Validated here rather than trusted, because a step naming a command that was
    renamed would otherwise fail halfway through a paid run. The same rules the
    single-command path enforces apply: a blocked command cannot be reached by
    putting it in a sequence.
    """
    workflow = by_name().get(name)
    if workflow is None:
        raise catalog.InvalidRequest(f"unknown workflow {name!r}")

    commands = catalog.by_name()
    for step in workflow.steps:
        command = commands.get(step.cmd)
        if command is None:
            raise catalog.InvalidRequest(
                f"workflow {name!r} refers to unknown command {step.cmd!r}"
            )
        if command.blocked:
            raise catalog.InvalidRequest(
                f"workflow {name!r} contains {step.cmd!r}, which cannot run from the "
                f"console: {command.blocked}"
            )
    return workflow


__all__ = ["WORKFLOWS", "Step", "Workflow", "as_json", "by_name", "resolve"]
