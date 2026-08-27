"""`parallel.map_ordered`: the guarantees its docstring makes, one test each.

The interesting ones are the two that are easy to get wrong and impossible to
notice: that results come back in *input* order however they complete, and that a
failure stops paying for work nobody will read.
"""

from __future__ import annotations

import threading
import time

import pytest

from tracker.config import Settings
from tracker.parallel import map_ordered


def test_results_arrive_in_input_order_not_completion_order():
    """The guarantee the run report depends on.

    Item 0 is deliberately the slowest, so completion order is the reverse of input
    order and anything yielding as-completed would fail this.
    """
    delays = {0: 0.20, 1: 0.10, 2: 0.01}

    def slow(item: int) -> int:
        time.sleep(delays[item])
        return item * 10

    got = list(map_ordered([0, 1, 2], slow, limit=3))
    assert got == [(0, 0), (1, 10), (2, 20)]


def test_calls_actually_overlap():
    """Otherwise this module is an elaborate `for` loop.

    Each worker announces itself and then waits for the barrier, so the barrier can
    only clear if `limit` of them are genuinely in flight at once. A serial
    implementation deadlocks here and the timeout fails the test rather than hanging
    the suite.
    """
    barrier = threading.Barrier(4, timeout=10)
    peak: list[int] = []

    def wait(item: int) -> int:
        peak.append(item)
        barrier.wait()
        return item

    assert list(map_ordered(range(4), wait, limit=4)) == [(i, i) for i in range(4)]
    assert len(peak) == 4


def test_limit_of_one_never_starts_a_thread():
    """The serial path has to stay the serial path: it is what `--llm-provider
    ollama` takes, what the test suite takes, and what a failure under concurrency
    gets bisected against."""
    main = threading.current_thread().name
    seen: list[str] = []

    def note(item: int) -> str:
        seen.append(threading.current_thread().name)
        return str(item)

    assert list(map_ordered([1, 2, 3], note, limit=1)) == [(1, "1"), (2, "2"), (3, "3")]
    assert seen == [main, main, main], "limit=1 must run inline on the calling thread"


def test_a_single_item_runs_inline_whatever_the_limit():
    """One article is the common case for `tracker point --url` and for most of the
    suite; spinning up a pool for it is pure overhead."""
    main = threading.current_thread().name
    seen: list[str] = []

    def note(item: int) -> int:
        seen.append(threading.current_thread().name)
        return item

    assert list(map_ordered([7], note, limit=8)) == [(7, 7)]
    assert seen == [main]


def test_nothing_in_nothing_out():
    assert list(map_ordered([], lambda item: item, limit=4)) == []


def test_an_exception_surfaces_at_the_item_that_raised():
    """Earlier results are still delivered, because the caller has already acted on
    them — in `crawl.run` they are committed rows. The failure must land where the
    serial loop would have put it, not at the top."""

    def boom(item: int) -> int:
        if item == 2:
            raise ValueError("item 2 is bad")
        return item

    seen: list[int] = []
    with pytest.raises(ValueError, match="item 2 is bad"):
        for _item, result in map_ordered([0, 1, 2, 3], boom, limit=2):
            seen.append(result)
    assert seen == [0, 1], "everything before the failure is delivered"


def test_a_failure_stops_paying_for_work_nobody_will_read():
    """The bounded loss the docstring promises.

    Twenty items, two workers, and the first one raises. A pool that ran the queue
    to completion regardless would call `fn` twenty times; cancelling the unstarted
    tasks keeps it near the number in flight. The bound is `limit` plus the one that
    failed — asserted loosely because which task a worker has picked up at the
    moment of failure is genuinely a race, and pinning it exactly would be pinning
    the scheduler.
    """
    calls: list[int] = []
    lock = threading.Lock()

    def boom(item: int) -> int:
        with lock:
            calls.append(item)
        if item == 0:
            raise RuntimeError("first item fails")
        time.sleep(0.05)
        return item

    with pytest.raises(RuntimeError):
        list(map_ordered(range(20), boom, limit=2))
    assert len(calls) <= 4, f"expected the queue to be cancelled, {len(calls)} ran"


def test_abandoning_the_generator_cancels_the_rest():
    """A caller that breaks out of the loop — `--limit` reached, operator
    interrupted — must not leave a pool grinding through the remainder."""
    calls: list[int] = []
    lock = threading.Lock()

    def note(item: int) -> int:
        with lock:
            calls.append(item)
        time.sleep(0.05)
        return item

    stream = map_ordered(range(20), note, limit=2)
    assert next(stream) == (0, 0)
    stream.close()
    assert len(calls) <= 4, f"expected cancellation after close(), {len(calls)} ran"


# --- the setting that feeds it ------------------------------------------------


def test_llm_workers_gives_a_local_model_one_worker():
    """The whole point of having two fields. Local inference is compute-bound, so
    fanning out queues requests against one GPU and makes the run slower while
    looking like it should be faster."""
    settings = Settings(llm_provider="ollama", llm_concurrency=8, ollama_concurrency=1)
    assert settings.llm_workers() == 1
    assert settings.llm_workers("deepseek") == 8, "an explicit override still wins"


def test_llm_workers_gives_the_api_its_own_number():
    settings = Settings(llm_provider="deepseek", llm_concurrency=6, ollama_concurrency=1)
    assert settings.llm_workers() == 6
    assert settings.llm_workers("ollama") == 1
