"""The night's steps, who chooses them, and what each one cost and did.

`tracker.overnight` runs the night. Before 2026-11-05 its schedule was fixed in code;
now a night is a sequence of steps, and either the fixed schedule or a model (the
director) chooses them. Whichever chooses, the same code checks the choice, runs it,
and measures it:

* :mod:`tracker.night.steps` — the menu: every kind of step, its commands, its sizes,
  its floor, and the fixed schedule built from it;
* :mod:`tracker.night.referee` — what a director may ask for, enforced: order,
  ranges, floors, drift, effort, the rows it may name;
* :mod:`tracker.night.record` — one line per step run, on every night: cost (from the
  spend ledger), time, exit status, and the counts before and after;
* :mod:`tracker.night.guard` — the accuracy check: answers in the key that the night
  turned wrong, values flipped back within a week, values with nothing behind them;
* :mod:`tracker.night.briefing` — what the director is told, and the read-only
  lookups it may call;
* :mod:`tracker.night.director` — the model's side: one conversation a night, a
  check-in at each round and after every three steps, and the fallback to the fixed
  schedule when it fails.

The principle the package follows: the model chooses; code decides whether the choice
is allowed, carries it out, and measures it. The worst night a director can produce is
the worst sequence of allowed steps, and every limit on that sequence is here, in code.
"""
