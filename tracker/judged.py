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
* **The money ceiling is asked about before each item starts**
  (`spend.ceiling_reached`). Once reached, nothing new starts and everything done is
  kept, so the overshoot is the calls already in flight, at most `workers`.
* **The calls overlap, each worker taking the next item the moment it is free.** At
  most `workers` questions are out at once, and never two on one project — both rows
  of a duplicate pair count — until the earlier one is applied (:func:`startable`).
  Items are prepared in input order as they start, and applied and committed one by
  one in the order they started. So each item's prompt is built after every earlier
  item on the same project was applied — what the serial loop guaranteed. With one
  worker this is exactly the serial loop.

  It used to go in waves: six asked together, the next six prepared only when all
  six were applied. Every wave waited for its slowest answer, so the other five
  workers sat idle for most of it — measured on three nights' ledgers, waves took
  1.5 to 2.3 times as long as the same calls taken as workers came free, and a
  connection that hung for ten minutes held its whole wave for ten minutes.

**Why an agent pass reads through a session of its own.** The agent's evidence
tools (`agent.evidence_toolkit`) look rows up while the model works, so they cannot
be handed plain values in advance: `find_projects` searches the whole table. They
only ever read, though, so with several workers each ask gets a fresh session on
the same engine, closed when it ends. SQLite in WAL mode lets readers run beside the
one writer, and because every earlier item on the same rows is committed before an
item is prepared, such a session sees the rows its item is about exactly as the
serial loop's would.
With one worker the ask reads through the caller's own session, unchanged — which
is also what a dry run needs, since its writes are never committed for another
session to see.
"""

from __future__ import annotations

import collections
import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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


#: How many workers' worth of answers may wait, received but not yet applied, behind
#: an earlier answer that is slow to arrive. Answers are applied in the order their
#: items started, so one slow call would otherwise idle every other worker until it
#: returned; this many let the rest keep going, and bound what a crash in the
#: meantime could lose to answers paid for and not yet committed.
BACKLOG: int = 3

#: Seconds to wait before a pass asks its failed calls again (`run(retry_errors=True)`).
#: A provider answering "overloaded" (HTTP 429) usually recovers within a minute; 17
#: of 241 suspect re-reads in ten nights were lost to it, each a slot the night had
#: chosen and then spent on nothing.
RETRY_WAIT: float = 60.0


@dataclass
class Run:
    """What a pass did."""

    results: list[Any] = field(default_factory=list)
    #: Items whose call failed and were asked once more at the end of the pass.
    retried: int = 0
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


@dataclass
class _Started:
    """An item whose question is out: on a worker, or answered and waiting its turn."""

    item: Any
    key: tuple[str, str]
    prepared: Any
    touched: list[int]
    future: Future


def _touched(projects: Callable[[Any], Iterable[int]], item: Any) -> list[int]:
    return sorted({int(p) for p in projects(item) if p is not None})


def startable(
    queue: Sequence[Any], projects: Callable[[Any], Iterable[int]], busy: Iterable[int]
) -> int | None:
    """Where in `queue` the next item to start is, or None while every one must wait.

    An item may start when none of its projects is `busy` — asked about and not yet
    applied. One that must wait blocks its projects for every later item too, so no
    later item on one of its projects jumps ahead of it: per project, the order of
    `queue` is kept, and each item's question is built after every earlier item on
    the same rows was applied.
    """
    blocked = set(busy)
    for at, item in enumerate(queue):
        touched = set(_touched(projects, item))
        if not touched & blocked:
            return at
        blocked |= touched
    return None


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
    fields: Callable[[Any], Iterable[tuple[int, str]]] | None = None,
    retry_errors: bool = False,
    retry_wait: float | None = None,
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
        fields: the (project, field) pairs an item is about, when the pass knows. Every
            committed answer's effect on its rows is written to the verdict log
            (`tracker.labels`); with this, the fields it left standing are too.
        retry_errors: an item whose call raised, or whose answer came back as a failed
            look (`declines.ERROR`), goes to the back of the queue and is asked once
            more after every other item, `retry_wait` seconds (default
            :data:`RETRY_WAIT`) after the first pass ends. Only its second failure is
            reported and held for the night.
    """
    import time

    from tracker import declines as _declines

    wait_s = RETRY_WAIT if retry_wait is None else retry_wait
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

    from tracker import labels
    from tracker.llm import spend_stage

    def record(item: Any, before: labels.Held, done: Done) -> None:
        """The answer's effect on its rows, for the verdict log. Never raises."""
        try:
            looked = list(fields(item)) if fields is not None else []
        except Exception:
            looked = []
        labels.record_verdicts(
            session,
            by=kind or label,
            before=before,
            after=labels.held(session, before),
            looked_at=looked,
            declined=bool(done.decline),
        )

    # Failed looks put back for one more try: (item, key, the failure as it came).
    # `second` holds the items already on their second try.
    put_back: list[tuple[Any, tuple[str, str], Any]] = []
    second: list[Any] = []

    def retrying(item: Any) -> bool:
        return any(other is item for other in second)

    def settle_put_back() -> None:
        """Report what was put back and never retried, as the first pass would have."""
        for item, key, failure in put_back:
            if isinstance(failure, _Raised):
                fail(item, key, failure.error, "ask")
            else:
                finish(key, failure)
        put_back.clear()

    queue = list(chosen)
    cap = max(1, workers)
    if cap > 1 and len(queue) > 1:
        log.info("%s: %d item(s), %d at a time", label, len(queue), cap)
    pool = (
        ThreadPoolExecutor(max_workers=cap, thread_name_prefix=f"tracker-{label}")
        if cap > 1
        else None
    )

    def launch(prepared: Any) -> Future:
        """Start one ask: on a worker, or here and now when there is one worker."""
        if pool is not None:
            return pool.submit(asked, prepared)
        future: Future = Future()
        future.set_result(asked(prepared))
        return future

    def settle(started: _Started) -> None:
        """Apply one answered item, on this thread, exactly as the serial loop did."""
        item, key, prepared = started.item, started.key, started.prepared
        answer = started.future.result()
        if isinstance(answer, _Raised):
            if retry_errors and not retrying(item):
                put_back.append((item, key, answer))
                return
            fail(item, key, answer.error, "ask")
            return
        before = labels.held(session, projects(item)) if commit else {}
        try:
            done = apply(item, prepared, answer)
        except Exception as exc:
            fail(item, key, exc, "apply")
            return
        if retry_errors and done.decline == _declines.ERROR and not retrying(item):
            put_back.append((item, key, done))
            return
        finish(key, done)
        if commit:
            record(item, before, done)

    try:
        with spend_stage(stage) if stage else contextlib.nullcontext():
            while True:
                stop = False
                #: Asked and not yet applied, in the order they started. Applied in
                #: that order, so a run's report reads the same however the calls
                #: happened to finish.
                pending: list[_Started] = []
                #: The projects of everything in `pending`: none may start another.
                busy: collections.Counter[int] = collections.Counter()
                while queue or pending:
                    # Fill every free worker. An answer that arrived but waits behind
                    # an earlier one is not running, so it does not hold a worker —
                    # but no more than BACKLOG workers' worth may wait, which bounds
                    # what a crash could lose to answers paid for and not yet saved.
                    # One worker: nothing starts until the last item is applied.
                    while not stop and queue:
                        running = sum(1 for p in pending if not p.future.done())
                        if running >= cap or len(pending) >= (BACKLOG * cap if pool else 1):
                            break
                        why = ceiling_reached()
                        if why:
                            out.ceiling, out.unreached = why, list(queue)
                            queue, stop = [], True
                            break
                        left = room() if room is not None else None
                        # What is asked but not applied has not been counted against
                        # the budget yet: wait for it before deciding.
                        if left is not None and left - len(pending) <= 0:
                            if not pending:
                                out.stopped, out.unreached = "no room left", list(queue)
                                queue, stop = [], True
                            break
                        at = startable(queue, projects, busy)
                        if at is None:
                            break
                        item = queue.pop(at)
                        # The key is taken here, after every earlier item on the same
                        # rows was applied, so a decline names the question as it was
                        # asked and the evidence it was shown — a merge earlier in the
                        # run may have moved a pair to other rows.
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
                        touched = _touched(projects, item)
                        pending.append(_Started(item, key, prepared, touched, launch(prepared)))
                        busy.update(touched)
                    if not pending:
                        if stop or not queue:
                            break
                        continue
                    if not pending[0].future.done():
                        # On the calls still out only: an answer already in would make
                        # this return at once, and the loop would spin until the head's.
                        wait(
                            [p.future for p in pending if not p.future.done()],
                            return_when=FIRST_COMPLETED,
                        )
                    while pending and pending[0].future.done():
                        started = pending.pop(0)
                        for project in started.touched:
                            busy[project] -= 1
                            if busy[project] <= 0:
                                del busy[project]
                        settle(started)
                if stop or not put_back:
                    settle_put_back()
                    break
                # Every other item has been asked; the ones whose call failed get one
                # more try, after a pause long enough for an overloaded provider to
                # recover.
                out.retried += len(put_back)
                second.extend(item for item, _key, _failure in put_back)
                queue = [item for item, _key, _failure in put_back]
                put_back.clear()
                if wait_s > 0:
                    time.sleep(wait_s)
    finally:
        if pool is not None:
            # Answers still being worked on are paid for either way; waiting for them
            # is the bounded loss `parallel.map_ordered` documents.
            pool.shutdown(wait=True, cancel_futures=True)
    return out


__all__ = ["Done", "Run", "Tokens", "run", "startable"]
