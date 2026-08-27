"""Bounded concurrency for the blocking calls that dominate a run's wall clock.

`ingest/fetch.py` has had this discipline since it was written — one semaphore for
a global cap, one per host, a politeness delay between requests — and the LLM layer
beside it had none. Every model call went out on its own, blocking, while the
fetches that preceded it ran four at a time. On a crawl the model is most of the
elapsed time, so the serial half was the whole cost.

This module is that discipline for the *synchronous* side. `Extractor.complete` is
blocking by design and there are two implementations of it; rewriting them around
asyncio would mean a second HTTP client, a second retry policy and a second set of
timeout semantics for no gain, because threads block on a socket perfectly well.

**Results come back in input order, never completion order.** Two reasons, and the
first is the one that decided it:

* A run's report is read by a person, and ordering it by whichever call happened to
  return first makes two runs over one queue print their lines in different orders.
  A test that pins that output then fails intermittently, which is the worst kind
  of failing test. The *stored* data is safe either way — `upsert` recomputes from
  the full claim set and is order-independent by construction — but a log is not
  data and has no such property.
* It lets the caller keep doing its own work serially while the calls overlap,
  which is what allows `crawl.run` to hold one writer and its commit-per-article
  checkpoint unchanged. See `crawl._checkpoint` for why that checkpoint is not
  negotiable.

Ordering costs nothing in throughput. Every task is submitted up front and the pool
runs `limit` of them at a time regardless of which one the consumer happens to be
waiting for, so head-of-line blocking delays a *write* — milliseconds — and never a
call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


def map_ordered(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    limit: int,
    label: str = "task",
) -> Iterator[tuple[T, R]]:
    """Apply `fn` to every item with at most `limit` calls in flight.

    Yields ``(item, result)`` in the order of `items`, so a caller consumes it the
    way it consumed the `for` loop this replaces.

    `limit <= 1` runs inline, with no executor and no threads at all. That is not a
    micro-optimisation but a guarantee: it keeps the serial path — which is what
    `--llm-provider ollama` and the whole test suite take — identical to what it was
    before this module existed, so any behaviour that changes under concurrency can
    be bisected against a serial run of the same input.

    An exception propagates from the `yield` that would have delivered its result,
    after every task that has not yet started is cancelled. Tasks already running
    are not interrupted, because a blocking HTTP request cannot be, so a failure
    costs at most `limit` calls in flight. That bounded loss is most of why the cap
    is worth having: it is the same reasoning as the write lock's, which exists so a
    second run cannot die partway through having already paid for its calls.

    `fn` must not touch a SQLAlchemy `Session`. A session is not thread-safe, and a
    lazy load from a worker is a race that presents as corrupt data rather than as
    an error. Callers pass plain values — `crawl.extract_one` takes a `FetchResult`
    and returns an `ExtractionOutcome`, neither of which knows a database exists.
    """
    ordered = list(items)
    if limit <= 1 or len(ordered) <= 1:
        for item in ordered:
            yield item, fn(item)
        return

    workers = min(limit, len(ordered))
    log.info("%s: %d item(s), %d at a time", label, len(ordered), workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"tracker-{label}") as pool:
        futures = [pool.submit(fn, item) for item in ordered]
        try:
            for item, future in zip(ordered, futures, strict=True):
                yield item, future.result()
        finally:
            # Before the executor's own shutdown, which waits. Cancelling the
            # queue first means an abandoned or failed consumer stops paying for
            # results nobody will read; the ones already running still have to be
            # waited for, and are the bounded loss documented above.
            cancelled = sum(1 for future in futures if future.cancel())
            if cancelled:
                log.info("%s: cancelled %d task(s) that had not started", label, cancelled)


__all__ = ["map_ordered"]
