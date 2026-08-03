"""Execute one CLI command at a time and stream its output.

Three properties this has to hold, in order of how much they cost when broken:

1. **No shell, ever.** `argv` comes from `catalog.build_argv`, which builds a list
   from a validated flag dict. Nothing here concatenates request text into a
   command line, so shell metacharacters are inert.
2. **Spending and destruction are confirmed.** A command in
   `catalog.LLM_COMMANDS` or `catalog.DESTRUCTIVE` needs a `confirm` string equal
   to the command name. A misplaced click cannot start a `sync`, and it cannot
   delete a project row either. Two different losses, one ritual — and the check
   is on the command *name*, never on its flags, so no combination of arguments
   talks its way past the gate.
3. **One at a time.** SQLite takes one writer. Two overlapping runs produce a raw
   "database is locked" partway through the second — after it has already paid for
   its LLM calls. The subprocess takes the real file lock; this keeps a slot as
   well, so the second request is refused immediately with an explanation instead
   of dying eight articles in.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from tracker.webui import catalog, runs

log = logging.getLogger(__name__)

#: Stop a wedged command rather than holding the single slot forever. Long enough
#: for a real `sync --limit 25`, which takes about four minutes.
TIMEOUT_S = 60 * 45


class Busy(RuntimeError):
    """Another run holds the slot."""


class Runner:
    """The single-slot executor behind /api/run."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._current: runs.Run | None = None
        self._process: subprocess.Popen | None = None
        #: One queue per connected SSE client. A slow reader cannot block the
        #: subprocess: `put_nowait` drops rather than waits.
        self._listeners: list[queue.Queue] = []

    # --- state ------------------------------------------------------------

    @property
    def current(self) -> runs.Run | None:
        return self._current

    def snapshot(self) -> dict[str, Any] | None:
        run = self._current
        return None if run is None else {**run.summary(), "lines": list(run.lines)}

    # --- starting ---------------------------------------------------------

    def start(self, cmd: str, flags: dict[str, Any], *, confirm: str | None = None) -> runs.Run:
        command = catalog.by_name().get(cmd)
        if command is None:
            raise catalog.InvalidRequest(f"unknown command {cmd!r}")
        if command.needs_confirmation and (confirm or "").strip() != cmd:
            why = (
                f"`{cmd}` {command.destroys}" if command.destroys else f"`{cmd}` spends LLM tokens."
            )
            raise catalog.InvalidRequest(f'{why} Re-send with confirm="{cmd}" to run it.')
        argv = catalog.build_argv(cmd, flags, db_path=self.db_path)

        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise Busy(
                    f"`{self._current.cmd}` is still running. "
                    "SQLite takes one writer, so a second run would fail partway through."
                )
            run = runs.Run(
                id=runs.new_id(self.db_path),
                cmd=cmd,
                argv=argv,
                started_at=_now(),
                cost=command.cost,
            )
            self._current = run
            self._listeners = []

        runs.begin(self.db_path, run)
        threading.Thread(target=self._execute, args=(run, argv), daemon=True).start()
        return run

    def start_workflow(self, name: str, *, confirm: str | None = None) -> runs.Run:
        """Run a named sequence as a single job.

        One `Run`, one log, one entry in the history — not N runs chained by the
        browser. Chaining client-side would mean a closed tab abandons the
        sequence halfway, and the history would show three unrelated commands with
        nothing recording that they were one intention.
        """
        from tracker.webui import workflows as workflow_mod

        workflow = workflow_mod.resolve(name)
        if workflow.needs_confirmation and (confirm or "").strip() != name:
            why = (
                f"`{name}` {workflow.destroys}"
                if workflow.destroys
                else f"`{name}` spends LLM tokens."
            )
            raise catalog.InvalidRequest(f'{why} Re-send with confirm="{name}" to run it.')

        # Every step's argv is built before anything executes. A typo in the last
        # step should not be discovered after the first two have spent money.
        plan = [
            (step, catalog.build_argv(step.cmd, step.flags, db_path=self.db_path))
            for step in workflow.steps
        ]

        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise Busy(
                    f"`{self._current.cmd}` is still running. "
                    "SQLite takes one writer, so a second run would fail partway through."
                )
            run = runs.Run(
                id=runs.new_id(self.db_path),
                cmd=f"workflow {name}",
                argv=[a for _, argv in plan for a in argv],
                started_at=_now(),
                cost=workflow.cost,
            )
            self._current = run
            self._listeners = []

        runs.begin(self.db_path, run)
        threading.Thread(
            target=self._execute_workflow, args=(run, workflow, plan), daemon=True
        ).start()
        return run

    def cancel(self) -> bool:
        process, run = self._process, self._current
        if process is None or run is None or run.status != "running":
            return False
        run.status = "cancelled"
        process.terminate()
        return True

    # --- streaming --------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2048)
        with self._lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _emit(self, event: dict[str, Any]) -> None:
        for q in list(self._listeners):
            # A browser tab that stopped reading must not stall the run.
            with contextlib.suppress(queue.Full):
                q.put_nowait(event)

    # --- execution --------------------------------------------------------

    def _execute(self, run: runs.Run, argv: list[str]) -> None:
        started = time.monotonic()
        before = _project_stamps(self.db_path)
        exit_code, error = self._spawn(run, argv)
        self._close(run, exit_code=exit_code, started=started, before=before, error=error)

    def _execute_workflow(self, run: runs.Run, workflow: Any, plan: list[Any]) -> None:
        """Each step in turn, into one log, stopping at the first real failure.

        The before/after stamps span the whole sequence, so `projects_touched`
        counts rows the workflow changed rather than rows the last step changed.
        """
        started = time.monotonic()
        before = _project_stamps(self.db_path)
        total = len(plan)
        self._push(run, f"[console] {workflow.title} — {total} step(s)")

        exit_code: int | None = 0
        for index, (step, argv) in enumerate(plan, start=1):
            if run.status == "cancelled":
                self._push(run, "[console] cancelled; remaining steps skipped")
                break
            self._push(run, "")
            self._push(run, f"[console] step {index}/{total}: {step.cmd}")
            if step.because:
                self._push(run, f"[console] {step.because}")

            code, error = self._spawn(run, argv)
            if error:
                self._push(run, f"[console] could not start: {error}")
                exit_code = 127
                break
            if code == 0:
                continue
            if step.tolerate_failure:
                # `duplicates` and `logic check` exit non-zero when they find
                # something. That is the answer, not a breakage.
                self._push(run, f"[console] {step.cmd} exited {code} — a finding, continuing")
                continue
            self._push(run, f"[console] {step.cmd} failed ({code}); stopping here")
            self._push(run, f"[console] {total - index} step(s) not run")
            exit_code = code
            break

        self._close(run, exit_code=exit_code, started=started, before=before)

    def _spawn(self, run: runs.Run, argv: list[str]) -> tuple[int | None, str | None]:
        """Run one argv to completion, streaming its output. Returns (code, error)."""
        env = _child_env()
        try:
            # argv came from catalog.build_argv: a validated list, never a shell
            # string. No shell=True anywhere, so metacharacters are inert.
            # No `cwd` override. The child is told its database explicitly, and
            # everything else it reads (migrations, prompts, feeds.toml, the
            # article cache) resolves package-relative. Guessing a directory from
            # the database path only made relative flag values resolve somewhere
            # the operator did not type.
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            return 127, str(exc)

        self._process = process
        self._push(run, f"$ {' '.join(argv[2:])}")
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self._push(run, line.rstrip("\n"))
            process.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            self._push(run, f"[console] killed after {TIMEOUT_S}s")
        finally:
            self._process = None

        return process.returncode, None

    def _push(self, run: runs.Run, line: str) -> None:
        runs.append(self.db_path, run, line)
        self._emit({"type": "line", "line": line})

    def _close(
        self,
        run: runs.Run,
        *,
        exit_code: int | None,
        started: float,
        before: dict[int, str],
        error: str | None = None,
    ) -> None:
        if error:
            self._push(run, f"[console] could not start: {error}")
        run.exit_code = exit_code
        if run.status != "cancelled":
            run.status = "ok" if exit_code == 0 else "failed"
        run.finished_at = _now()
        run.duration_s = round(time.monotonic() - started, 2)
        after = _project_stamps(self.db_path)
        run.projects_touched = sum(1 for pid, stamp in after.items() if before.get(pid) != stamp)
        runs.finish(self.db_path, run)
        self._emit({"type": "end", "run": run.summary()})


def _child_env() -> dict[str, str]:
    """The environment every run's subprocess gets.

    Keep the colour. Rich's output is not decorated text — red means a rejection,
    amber means 待确认, dim means a hint — and stripping it threw away the CLI's
    own signalling at exactly the moment someone is reading the output. The
    browser parses the escapes back out.

    Three variables, and all three are load-bearing. `FORCE_COLOR` makes Rich
    treat a pipe as a terminal. `NO_COLOR` has to be *removed* rather than left
    alone: it strips colour even from a forced terminal, so inheriting one from
    the operator's shell would silently undo this. And `COLORTERM` pins the SGR
    dialect, because without it the same command emits 8-bit codes on POSIX and
    truecolor on Windows, which would make the parser's job platform-dependent.

    Safe because no command uses a live widget — a Progress bar or a status
    spinner would start redrawing with \\r once Rich believes it is interactive,
    and the log would fill with half-drawn frames.
    """
    env = {
        **os.environ,
        "COLUMNS": "160",
        "FORCE_COLOR": "1",
        "COLORTERM": "truecolor",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("NO_COLOR", None)
    env.pop("TTY_COMPATIBLE", None)  # newer Rich checks this before FORCE_COLOR
    return env


def _now() -> str:
    from tracker.models import utcnow

    return utcnow().isoformat(sep=" ")


def _project_stamps(db_path: str) -> dict[int, str]:
    """id -> updated_at for every project, for the before/after comparison.

    Read through a fresh read-only connection each time, because the point is to
    observe what the *subprocess* wrote. Returns empty rather than raising if the
    database is not there yet: a first `tracker init` is a legitimate run.
    """
    from sqlalchemy import select

    from tracker.db import open_db, session_scope
    from tracker.models import Project

    try:
        engine = open_db(db_path)
        with session_scope(engine, commit=False) as session:
            rows = session.execute(select(Project.id, Project.updated_at)).all()
        return {int(pid): str(stamp) for pid, stamp in rows}
    except Exception:
        return {}


__all__ = ["TIMEOUT_S", "Busy", "Runner"]
