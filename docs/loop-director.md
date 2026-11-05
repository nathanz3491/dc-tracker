# The loop director

Since 2026-11-05 the nightly loop's steps can be chosen by a model instead of by the
fixed schedule in `tracker/overnight.py`. This page says what the director may and may
not do, how a directed night runs, what is recorded about every night, and why each
rule is there. The code is the `tracker/night/` package and `Night` in
`tracker/overnight.py`.

## Why

The fixed schedule runs the same steps at the same sizes every night. It cannot move
effort to what is working that night. On 2026-11-04 round 1 spent 481 searches on rows
below T2 and gave seven of them a new fact; round 2 ran after the search cap with no
searches at all, only re-reading articles already attached, and gave eighteen rows a
fact. The news watch used 30% more searches than forecast and left the second enrich
round short. A director that sees each step's cost and result can react to that; a
fixed order cannot.

## The principle

The loop was already non-deterministic: enrich, logic, duplicates, audit and the
queue read are all model judgements. What was fixed was only the schedule. A director
moves non-determinism into that one layer, and that layer is the easiest to bound:

- **the model chooses** from a closed menu of steps (`tracker/night/steps.py`);
- **code decides** whether each choice is allowed (`tracker/night/referee.py`), runs
  it as the same tested `tracker` commands, and measures it
  (`tracker/night/record.py`);
- **nothing the model writes reaches a command line** except through a step's fields;
- **every limit is code**: the money ceiling, the search cap, the wall clock, the
  rounds, the snapshot before a merge, the lock, the deploy hold — all unchanged.

The worst night a director can produce is the worst sequence of allowed steps.

## Who chooses, which night

`--mode auto` (the default) alternates by the date's day count: a directed night,
then a fixed one. A bad directed night is always followed by a normal one, and the
database never drifts far on the director alone. `--mode fixed` and `--mode director`
force one or the other; `--replay data/runs/director-<night>.jsonl` runs a recorded
directed night's steps again without a model.

On a fixed night the director still runs, in the background: at the same check-in
points it says what it would run instead and predicts the cost and result of the next
fixed steps. Nothing it says runs. Its predictions land on the steps' records, so
whether its estimates can be trusted is visible within a week.

## A directed night

1. The frame, which the director cannot change: counts, the snapshot, the suspicion
   score. Round 1 always starts with the **news** (the director sets its searches,
   queue size and first looks, inside a floor of 400 searches), then the **twin
   pass**, then the **free phase**. Every later round starts with the free phase.
2. **Check-ins.** One conversation for the whole night. The director is briefed once
   (`tracker/night/briefing.py`), then checks in at the start of each round and after
   every three of its steps. Each check-in tells it what ran, what it cost, what the
   counts did, and what is left of the limits. It answers with `submit_plan` (the
   steps for the rest of the round, replacing whatever has not run) or `stop_night`.
   Before answering it may call read-only lookups: one row, rows matching a filter, a
   step's price history, the night so far.
3. **The referee** reviews every plan. Its verdict — every clamp, refusal, move and
   added floor, in one sentence each — is the result of the `submit_plan` call, so the
   director reads it at its next turn, and it is printed in the log as `referee:` lines.
4. Each round ends with the settle and the counts, as on a fixed night; two rounds that
   lower nothing still end the night. After each directed round the **accuracy check**
   runs; a net loss of two answers hands the rest of the night to the fixed schedule.
5. The morning report gains a section, **how the night was chosen**: who chose, each
   step's cost and result against what was predicted, the director's own spend, and
   the accuracy check.

## What the director may do

| May | May not |
| --- | --- |
| choose which steps run and their order, within the order rules | write to the database except through the steps |
| set each step's size and article budget, within its range | change a ceiling, a floor, a prompt, a model or the quote gate |
| split the searches between the news watch and enrich, above the news floor | run a command outside the menu |
| run a second, fresh enrich pass | run a third, or a second that is not fresh |
| name up to 40 rows, and the fields to look for, for an enrich step | name rows that merged away or were asked about today |
| name a finding code and rows for a logic step | lower effort on a step that rules on published values |
| raise effort anywhere; lower it on enrich (it searches for pages) | send an agent more than a 300-character note |
| stop the night, with a reason | see article text, titles or URLs |

## The referee's rules

- **The menu only.** Unknown steps and the frame's steps are refused.
- **Once a night**: the news, the twins, the suspects, reread, recheck.
- **Ranges.** Every size and budget is clamped to its kind's range
  (`steps.KINDS`). An enrich step with no budget gets four articles a row, the fixed
  schedule's ratio.
- **Drift.** A kind's units over the night may not pass 1.5 times what a fixed night
  asks of it: the median over the record's fixed nights once there are three, the
  fixed schedule's sizes until then. One night's odd result cannot swing the plan.
- **Floors**, added to round 1 when the plan leaves a kind short: duplicates 20
  pairs, audit 30, risks 20, tranches 10, reread and recheck 180 each (their backlogs
  at the fixed pace until empty). A floor the night's options turned off is not
  added. A revised plan that drops a floor step gets it back.
- **Enrich passes.** A second selecting pass of `pipeline` or `enrich` the same night
  is `--fresh`; there is no third. On 2026-09-24 to 09-29 a second pass over the same
  rows was 86% of the bill for one new fact.
- **Named rows** must exist and must not have been asked about today. At most 40 a
  step and six such steps a night. The attempt limit (`tracker/attempts.py`) is not a
  knob: a field tried twice with nothing found stays closed.
- **Effort.** Raised anywhere; lowered only on the enrich steps, whose agent looks for
  pages. Steps that rule on published values — audit, risks, tranches, logic and the
  suspects, duplicates, the news read, reread and recheck — never go below today's
  setting. On 2026-09-30 the audit at `low` replaced a campus's $600M with a statewide
  "$20 billion+ in Ohio".
- **Notes** are cut to 300 characters and reach only agent steps, at the end of the
  agent's task (`tracker/agent.py`, `_director_note`). The quote gate and the
  confidence floors are code, so a note can steer where an agent looks but never what
  counts as evidence.
- **Order** within a round: duplicates before every step that pays to read a row the
  duplicate pass may fold; logic and the suspects before the tranche review.
- **Size of a night**: at most 60 steps.

## When the director fails

Any of these ends the director's part of the night, and the fixed schedule runs the
rest — the fixed round's steps not yet run this round, then fixed rounds (no
once-a-night step twice, no third enrich pass):

- the model or the provider fails, or a check-in takes more than 15 minutes;
- two invalid plans, or two prose answers, in one check-in;
- no plan after eight turns;
- its own ¥5 for the night is spent (ledger command `director`, inside the night's
  ceiling);
- the accuracy check finds two answers lost net.

The conversation is append-only, so the provider's prefix cache bills the repeated
part at about a fiftieth. Past 100,000 prompt tokens it is folded: the next check-in
starts a new conversation with the briefing and the night so far as a table.

## What it is told

The briefing is built by code: the limits and what is left; known conditions (an
empty DeepSeek balance, the Serper credit); the operator's note; the database (rows by
tier, rows below T2 by missing field, findings by kind and how many the agent can
repair, duplicate groups, the queue); the last nights; the price list; and a shortlist
of the 150 rows below T2 most worth effort. Row names come from articles, so they are
cut to 60 characters, kept to one line and set inside a `<data>` block.

The system prompt (`tracker/night/director.txt`) states the goal in order — make
nothing worse, read today's news, find values on rows below T2, answer findings,
settle duplicates — and that a field marked "searched, nothing found" is not a value
found. The menu and the lessons of past nights are appended from code
(`steps.manual`, `briefing.lessons`).

## The operator's note

```bash
tracker director note "tonight, duplicates first"   # set
tracker director note                                # show
tracker director note --clear                        # clear
```

Read into the briefing at the start of the night and cleared after a directed night.
It steers within the rules; it cannot lift a ceiling or a floor.

## What every night records

`data/runs/steps.jsonl`, one line per step on every night, fixed or directed:

- **cost**, sliced from the night's spend ledger by byte offset (the ledger is
  append-only and steps run one at a time, so the lines a step's commands wrote are
  exactly its own);
- **time**, and the **exit status** of each command — a crash is not a finding of
  nothing, and the director is told the difference;
- **counts before and after**: findings, duplicate groups, rows below T2, stored
  values and rows. A step's **result** is the change in its kind's count, signed so
  that more is better;
- the **prediction**, when there was one;
- the code version.

`data/runs/director-<night>.jsonl` holds what a night's director was told and decided:
the briefing, every check-in, lookups, replies, plans, the referee's verdicts, every
step as it ran, fallbacks.

```bash
tracker director steps [--night STAMP]   # one night's steps, cost and result
tracker director prices                  # the price list the director is briefed with
tracker director plan [--night STAMP]    # a night's check-ins, plans and verdicts
tracker director guard --against SNAP    # the accuracy check, by hand
tracker director backfill --write        # cost and time for nights before the record
```

## Watching it

`tracker loop` (on the host) says who is choosing tonight's steps, whether the director
handed them back and why, what the director has spent of its ¥5, the referee's changes,
the latest accuracy check, and a step-by-step table — each step's cost, searches, time
and result beside the prediction, failed steps marked. Its history says who chose each
past night. `tracker --json loop` carries the same, with the steps as records. The
public status page treats a night the director stopped as finished, and its JSON says
who chose each night.

## The accuracy check

`tracker/night/guard.py`, read-only and free. Against the snapshot taken before round 1:

- **the answer key**: verified answers the night turned right, and those it turned from
  right to anything else;
- **flip-backs**: values set back to what a snapshot about a week old held, undoing a
  change some night in between made — one of the two changes was wrong;
- **values with nothing behind them**: set tonight, stated by no citation, obstacle or
  tranche.

Counts can be bought with wrong values: a figure with a real quote from the wrong
campus fills a field as well as the right one. This is what a director's success is
weighed against, not the counts alone.
