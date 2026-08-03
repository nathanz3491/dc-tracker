"""Named command sequences, and the rules that keep them from being a back door.

A workflow is a way to run several commands with one click, so every guarantee
the single-command path enforces has to hold here too — otherwise "put it in a
sequence" becomes the way around confirmation and the blocklist.
"""

from __future__ import annotations

import pytest

from tracker.db import init_db
from tracker.webui import catalog, workflows
from tracker.webui import runner as runner_mod


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "tracker.db"
    init_db(path)
    return path


@pytest.fixture
def idle_runner(db, monkeypatch):
    """A Runner that accepts a start but never spawns anything.

    These tests are about the rules in front of execution. Letting them through
    would fork `tracker stats` for real, three times, to learn nothing the
    subprocess tests do not already cover.
    """
    monkeypatch.setattr(runner_mod.Runner, "_execute_workflow", lambda *a, **k: None)
    return runner_mod.Runner(db)


def test_every_step_names_a_real_runnable_command():
    """The whole catalog is derived from Typer, so a renamed command breaks these.

    Caught here rather than halfway through a paid run: `catch-up` spends money
    in step one and the operator would find out about the typo in step three.
    """
    commands = catalog.by_name()
    for workflow in workflows.WORKFLOWS:
        assert workflow.steps, f"{workflow.name} has no steps"
        for step in workflow.steps:
            assert step.cmd in commands, f"{workflow.name} names unknown command {step.cmd!r}"
            assert not commands[step.cmd].blocked, f"{workflow.name} contains a blocked command"
            assert step.because, f"{workflow.name}/{step.cmd} does not say why it follows"


def test_every_step_builds_a_valid_argv():
    """Flags are hard-coded in this file, so a wrong one is a bug nobody types."""
    for workflow in workflows.WORKFLOWS:
        for step in workflow.steps:
            argv = catalog.build_argv(step.cmd, step.flags, db_path="x.db")
            assert argv[-len(step.cmd.split()) :] or step.flags
            assert "--db" in argv


def test_a_workflow_costs_what_its_most_expensive_step_costs():
    """A sequence cannot be cheaper than what is in it.

    Reporting `free` for a sequence containing `sync` is the failure that matters:
    the badge is what an operator reads before clicking, and the confirmation
    prompt is derived from the same property.
    """
    commands = catalog.by_name()
    for workflow in workflows.WORKFLOWS:
        spends = any(commands[s.cmd].cost == "llm" for s in workflow.steps)
        assert workflow.cost == ("llm" if spends else "free"), workflow.name
        assert workflow.needs_confirmation == (spends or workflow.destroys is not None)


def test_an_unknown_workflow_is_refused():
    with pytest.raises(catalog.InvalidRequest):
        workflows.resolve("no-such-routine")


def test_a_step_naming_a_command_that_no_longer_exists_is_refused(monkeypatch):
    """The catalog is generated from Typer, so a rename silently orphans a step.

    Caught at resolve rather than at the step, because by then the run is half
    done and, for `catch-up`, half paid for.
    """
    stale = workflows.Workflow(
        name="stale",
        title="Stale",
        summary="",
        steps=(workflows.Step("ingest census", because="renamed years ago"),),
    )
    monkeypatch.setattr(workflows, "by_name", lambda: {"stale": stale})
    with pytest.raises(catalog.InvalidRequest, match="unknown command"):
        workflows.resolve("stale")


def test_a_sequence_cannot_reach_a_blocked_command(monkeypatch):
    """Otherwise "put it in a workflow" is the way around the blocklist.

    `cloudflare` publishes the console to a public URL. It is kept out of the
    console deliberately — publishing this page is not a click — and wrapping it
    in a routine must not be the loophole.
    """
    blocked = next(name for name, c in catalog.by_name().items() if c.blocked)
    sneaky = workflows.Workflow(
        name="sneaky",
        title="Sneaky",
        summary="",
        steps=(workflows.Step(blocked, because="should never be reachable"),),
    )
    monkeypatch.setattr(workflows, "by_name", lambda: {"sneaky": sneaky})
    with pytest.raises(catalog.InvalidRequest, match="cannot run from the console"):
        workflows.resolve("sneaky")


def test_a_sequence_is_not_a_way_past_confirmation(idle_runner):
    """The rule the single-command path enforces, enforced here too.

    Without this, wrapping `sync` in a workflow would be the way to spend money
    without confirming — and the whole point of the confirmation is that no single
    click can do it.
    """
    run = idle_runner
    costly = next(w for w in workflows.WORKFLOWS if w.needs_confirmation)

    with pytest.raises(catalog.InvalidRequest) as caught:
        run.start_workflow(costly.name)
    assert costly.name in str(caught.value)

    with pytest.raises(catalog.InvalidRequest):
        run.start_workflow(costly.name, confirm="something else")


def test_a_free_workflow_runs_without_ceremony(idle_runner):
    free = next((w for w in workflows.WORKFLOWS if not w.needs_confirmation), None)
    assert free is not None, "at least one routine should be runnable without confirming"
    started = idle_runner.start_workflow(free.name)
    assert started.cmd == f"workflow {free.name}"
    assert started.cost == "free"


def test_the_whole_plan_is_built_before_anything_executes(idle_runner, monkeypatch):
    """A bad last step must not be discovered after the first two spent money."""
    broken = workflows.Workflow(
        name="broken",
        title="Broken",
        summary="",
        steps=(
            workflows.Step("stats", because="fine"),
            workflows.Step("stats", {"--nonsense": 1}, because="not fine"),
        ),
    )
    monkeypatch.setattr(workflows, "by_name", lambda: {"broken": broken})

    with pytest.raises(catalog.InvalidRequest):
        idle_runner.start_workflow("broken")
    assert idle_runner.current is None, "nothing may start when a later step will not build"
