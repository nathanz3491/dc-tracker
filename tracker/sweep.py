"""Memory for one read-only sweep over the whole table.

The tier scan (`clean.scan`) and the nightly counts (`quality.night_counts`) ask the
same questions of every row from several places: each row's claim map from five
(the stored-value check, the evidence census, the silent defects, the recency
inversions, each field's provenance), and each row's contradictions from two (the
findings count and the row's own card). Every asker worked its answer out again —
9,003 claim maps and 2,934 rule checks for 1,467 rows — which was most of a sweep's
twenty seconds, paid again at every step of a night that measures itself after each.

Nothing is written during a sweep, so one row gives one answer, and inside
:func:`memo` the first is kept for the rest of the block. Outside one nothing is
kept, so a writing path — the merge recomputing a row it has just changed — always
works its answer out afresh.

Answers are keyed on the objects that asked (a row, a list of its citations) and
those objects are held with the answer, so an id cannot be reused by another object
while the block lasts. Callers that hand out a kept answer give each asker its own
copy of anything mutable.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

T = TypeVar("T")

_MEMO: contextvars.ContextVar[dict[tuple[Any, ...], tuple[Any, Any]] | None] = (
    contextvars.ContextVar("sweep_memo", default=None)
)


@contextmanager
def memo() -> Iterator[None]:
    """Keep each answer worked out inside this block. Read-only sweeps only.

    Nested blocks share the outermost one's memory.
    """
    if _MEMO.get() is not None:
        yield
        return
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def remembered(kind: str, holders: tuple[Any, ...], compute: Callable[[], T]) -> T:
    """`compute()`, or what it returned for the same `kind` and objects in this block.

    `holders` are the objects the answer is about, compared by identity. Outside a
    :func:`memo` block this is just `compute()`.
    """
    store = _MEMO.get()
    if store is None:
        return compute()
    key = (kind, *(id(h) for h in holders))
    kept = store.get(key)
    if kept is None:
        kept = (holders, compute())
        store[key] = kept
    return kept[1]


__all__ = ["memo", "remembered"]
