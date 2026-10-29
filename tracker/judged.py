"""One way to put a batch of questions to a model and keep every answer it paid for.

Six paid passes — `duplicates resolve`, `audit resolve`, `risks confirm`, `logic
resolve`, `tranches review` and `enrich`'s settle and agent steps — each had written
the same loop by hand: choose the work, skip what was already answered, ask, write
the answer, remember a refusal, save, print. They had drifted apart in exactly the
places that cost money:

* **Two held one transaction across the whole batch.** `risks confirm` and `tranches
  review` flushed every answer and committed at the end, so a run killed after
  thirty-nine of forty calls lost all thirty-nine answers *and* every refusal, and
  the next night paid for them again — with SQLite's write lock held the whole time.
* **Four let one bad item end the batch.** A database error while writing one pair,
  one finding or one obstacle took every item after it with it.
* **A failed call was held back in two passes and re-asked at once in four.**
* **Nothing could stop a pass part way.** The money ceiling was read between
  commands, so a pass that started under it spent its whole batch past it.

This module is that loop, once. A pass supplies what is particular to it and
nothing else:

``prepare(item)``
    Main thread, may read the session; builds everything the question needs as
    plain values. May return :class:`Done` to settle an item without asking.
``ask(prepared, db)``
    The model call. Runs on a worker thread when there is more than one worker, so
    it must not touch the caller's session — see `parallel.map_ordered`. ``db`` is
    None unless the pass declares ``reads_db``; then it is a session of the ask's
    own (see below).
``apply(item, prepared, answer)``
    Main thread; writes the answer and returns :class:`Done`, which says what to
    remember about the question.

and this module owns the rest, the same way for every pass:

* **Each item is committed as it is applied.** A crash keeps everything before it.
* **One item's failure is rolled back, logged, and the batch goes on.** The failure
  is recorded as `declines.ERROR`, held for the rest of the night only: a failed
  call says nothing about the question, so tomorrow it is worth asking again, but
  asking again tonight would pay to fail the same way.
* **The money ceiling is asked about before each wave** (`spend.ceiling_reached`).
  Once reached, nothing new starts and everything done is kept.
* **The calls overlap, in waves.** A wave holds at most one item per project — both
  rows of a duplicate pair count — and at most `workers` items. Its items are
  prepared in input order, asked together, then applied and committed one by one in
  input order, before the next wave is prepared. So each item's prompt is built
  after every earlier item on the same project was applied — what the serial loop
  guaranteed — and the ceiling can be overshot by one wave at most. With one worker
  a wave is one item and this is exactly the serial loop.

**Why an agent pass reads through a session of its own.** The agent's evidence
tools (`agent.evidence_toolkit`) look rows up while the model works, so they cannot
be handed plain values in advance: `find_projects` searches the whole table. They
only ever read, though, so with several workers each ask gets a fresh session on
the same engine, closed when it ends. SQLite in WAL mode lets readers run beside the
one writer, and because every earlier wave is committed before the next is prepared,
such a session sees the rows its item is about exactly as the serial loop's would.
With one worker the ask reads through the caller's own session, unchanged — which
is also what a dry run needs, since its writes are never committed for another
session to see.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


@dataclass
class Done:
    """What one item came to, and what to remember about its question.

    `result` is whatever the pass collects and prints. `decline` is the outcome to
    record in `model_decline` against the evidence the item was prepared on — an
    inability to decide, or `declines.ERROR` for a failed look — and "" records
    nothing. `forget` drops an earlier decline, for a question now decided.
    """

    result: Any
    decline: str = ""
    reason: str = ""
    forget: bool = False


@dataclass
class Tokens:
    """What a pass's model calls used, added up from each answer's counters."""

    prompt: int = 0
    completion: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    calls: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, got: Any) -> None:
        """Add one answer's `prompt_tokens`, `completion_tokens`, `cache_*_tokens`."""
        self.prompt += int(getattr(got, "prompt_tokens", 0) or 0)
        self.completion += int(getattr(got, "completion_tokens", 0) or 0)
        self.cache_hit += int(getattr(got, "cache_hit_tokens", 0) or 0)
        self.cache_miss += int(getattr(got, "cache_miss_tokens", 0) or 0)
        self.calls += 1


@dataclass
class Run:
    """What a pass did."""

    results: list[Any] = field(default_factory=list)
    #: Items already put to the model on the same evidence, passed over to fill the
    #: limit. See `declines.pick` for why a lazy walk counts only what it passed.
    held: int = 0
    #: Fresh items beyond the limit (or, on a lazy walk, items never examined).
    beyond: int = 0
    #: Items whose prepare, ask or apply raised; each was rolled back.
    failed: int = 0
    #: Why the spend ceiling stopped the pass, or "".
    ceiling: str = ""
    #: Why the pass's own `room` stopped it (a token budget), or "".
    stopped: str = ""
    #: The chosen items never started because the pass stopped.
    unreached: list[Any] = field(default_factory=list)


@dataclass
class _Raised:
    """An ask that raised, carried back to the main thread to be handled there."""

    error: BaseException


def waves(
    queue: Sequence[Any], projects: Callable[[Any], Iterable[int]], cap: int
) -> tuple[list[Any], list[Any]]:
    """The next wave from `queue`, and what is left: at most `cap` items, no project twice.

    An item that cannot join — a project already in the wave, or the wave full —
    blocks its projects for every later item too, so no later item on one of its
    projects jumps ahead of it. Per project, the order of `queue` is kept.
    """
    wave: list[Any] = []
    rest: list[Any] = []
    busy: set[int] = set()
    for item in queue:
        touched = {int(p) for p in projects(item) if p is not None}
        if len(wave) < max(1, cap) and not touched & busy:
            wave.append(item)
        else:
            rest.append(item)
        busy |= touched
    return wave, rest


def run(
    session: Session,
    items: Iterable[Any],
    *,
    kind: str | None,
    subject: Callable[[Any], str],
    fingerprint: Callable[[Any], str],
    projects: Callable[[Any], Iterable[int]],
    prepare: Callable[[Any], Any],
    ask: Callable[[Any, Session | None], Any],
    apply: Callable[[Any, Any, Any], Done],
    failed: Callable[[Any, BaseException], Any] | None = None,
    limit: int | None = None,
    again: bool = False,
    lazy: bool = False,
    known: dict[str, Any] | None = None,
    workers: int = 1,
    commit: bool = True,
    remember: bool | None = None,
    by: str = "model",
    reads_db: bool = False,
    stage: str | None = None,
    room: Callable[[], int | None] | None = None,
    on_each: Callable[[Any], None] | None = None,
    on_start: Callable[[Run, list[Any]], None] | None = None,
    label: str = "judge",
) -> Run:
    """Put `items` to a model, in order, keeping each answer as it lands.

    Args:
        kind: the `declines` kind questions are remembered under; None remembers
            nothing and holds nothing back.
        subject, fingerprint: an item's key in `model_decline` and a hash of what it
            is asked on. The fingerprint is taken again just before the item is
            prepared, after every earlier item was applied, so a decline describes
            what the model was actually shown.
        projects: the project ids an item reads or writes. Asked when each wave is
            formed, so a pass whose items move between rows (a merge) answers with
            where they are now.
        failed: builds the result reported for an item that raised; None reports
            nothing beyond the log line.
        limit, again, lazy: how many fresh items to ask; `--again`; whether to walk
            the candidates lazily (`declines.pick`) because a fingerprint is dear.
        known: the declines to hold back against, when the pass filters them first.
        workers: asks in flight at once; 1 is the serial loop, exactly.
        commit: False for a dry run — nothing is committed and nothing remembered.
        remember: record declines even so, in the caller's open transaction (None
            follows `commit`). For a caller that commits on its own terms.
        reads_db: `ask` reads the database through the session it is handed.
        stage: the spend ledger's stage label for the asks.
        room: how many more items may start, asked before each wave; None is no
            bound and 0 stops the pass (a token budget).
        on_each: called with each item's result as it is applied, for progress.
        on_start: called once the work is chosen, with this run (its `held` and
            `beyond` already counted) and the chosen items, before anything is asked.
    """
    from tracker import declines
    from tracker.spend import ceiling_reached

    out = Run()
    if kind is not None and known is None:
        known = declines.load(session, kind)
    candidates = list(items)
    if kind is None:
        chosen = candidates[:limit] if limit is not None else candidates
        out.beyond = len(candidates) - len(chosen)
    elif lazy:
        chosen, out.held, examined = declines.pick(
            candidates, known or {}, subject, fingerprint, limit=limit, again=again
        )
        out.beyond = len(candidates) - examined
    else:
        fresh, out.held = declines.split(
            candidates, known or {}, lambda item: (subject(item), fingerprint(item)), again=again
        )
        chosen = fresh[:limit] if limit is not None else fresh
        out.beyond = len(fresh) - len(chosen)

    if commit:
        # Whatever the caller wrote before the pass is its own, and a failing item's
        # rollback below must not take it along.
        session.commit()
    if on_start is not None:
        on_start(out, chosen)

    keep = commit if remember is None else remember

    def note(key: tuple[str, str], done: Done) -> None:
        if not keep or kind is None:
            return
        if done.decline:
            declines.record(
                session, kind, key[0], key[1], outcome=done.decline, reason=done.reason, by=by
            )
        elif done.forget:
            declines.forget(session, kind, key[0])

    def finish(key: tuple[str, str], done: Done) -> None:
        note(key, done)
        if commit:
            session.commit()
        if done.result is not None:
            out.results.append(done.result)
            if on_each is not None:
                on_each(done.result)

    def fail(item: Any, key: tuple[str, str], error: BaseException, where: str) -> None:
        # The failed item's writes go; everything committed before it stays.
        session.rollback()
        out.failed += 1
        log.warning("%s: %s failed on %s: %s", label, where, key[0] or item, error)
        if keep and kind is not None and key[0]:
            try:
                declines.record(
                    session,
                    kind,
                    key[0],
                    key[1],
                    outcome=declines.ERROR,
                    reason=f"{where} failed: {error}",
                    by=by,
                )
                if commit:
                    session.commit()
            except Exception as exc:  # the database itself is the problem
                session.rollback()
                log.warning("%s: could not record the failure: %s", label, exc)
        result = failed(item, error) if failed is not None else None
        if result is not None:
            out.results.append(result)
            if on_each is not None:
                on_each(result)

    def asked(prepared: Any) -> Any:
        """One ask, on whichever thread runs it; an exception is carried back."""
        try:
            if not reads_db:
                return ask(prepared, None)
            if workers <= 1:
                return ask(prepared, session)
            own = Session(bind=session.get_bind(), future=True)
            try:
                return ask(prepared, own)
            finally:
                own.close()
        except Exception as exc:
            return _Raised(exc)

    from tracker.llm import spend_stage
    from tracker.parallel import map_ordered

    queue = chosen
    while queue:
        why = ceiling_reached()
        if why:
            out.ceiling, out.unreached = why, list(queue)
            break
        cap = max(1, workers)
        if room is not None:
            left = room()
            if left is not None and left <= 0:
                out.stopped, out.unreached = "no room left", list(queue)
                break
            if left is not None:
                cap = min(cap, left)
        wave, queue = waves(queue, projects, cap)

        ready: list[tuple[Any, tuple[str, str], Any]] = []
        for item in wave:
            # The key is taken here, after every earlier item was applied, so a
            # decline names the question as it was asked and the evidence it was
            # shown — a merge earlier in the run may have moved a pair to other rows.
            key = ("", "")
            try:
                if kind is not None:
                    key = (subject(item), "")
                    key = (key[0], fingerprint(item))
                prepared = prepare(item)
            except Exception as exc:
                fail(item, key, exc, "prepare")
                continue
            if isinstance(prepared, Done):
                finish(key, prepared)
                continue
            ready.append((item, key, prepared))

        if not ready:
            continue
        with spend_stage(stage) if stage else contextlib.nullcontext():
            for (item, key, prepared), answer in map_ordered(
                ready, lambda entry: asked(entry[2]), limit=min(workers, len(ready)), label=label
            ):
                if isinstance(answer, _Raised):
                    fail(item, key, answer.error, "ask")
                    continue
                try:
                    done = apply(item, prepared, answer)
                except Exception as exc:
                    fail(item, key, exc, "apply")
                    continue
                finish(key, done)
    return out


__all__ = ["Done", "Run", "Tokens", "run", "waves"]
