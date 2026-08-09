# Documentation

Seven documents, and honestly you need at most two of them. Start with
`what-we-built.zh-CN.md` if you are new.

| | What it is | Read it when |
| --- | --- | --- |
| [`what-we-built.zh-CN.md`](what-we-built.zh-CN.md) | 最短的一份。要的是什么、有了什么、怎么跑、报数前要知道哪些数字。~100 行。 | 你刚接手，或要把这个交给别人。 |
| [`../README.md`](../README.md) | The exhaustive one. Install, API keys, every command, every design decision and why. ~1100 lines. | You are setting this up, or you want to know why something works the way it does. |
| [`architecture.md`](architecture.md) | How the CLI, the database and the web console fit together. Logic, not code. | You are changing the console, or wondering why the browser does not compute anything itself. |
| [`guide.zh-CN.md`](guide.zh-CN.md) | 中文使用指南。按「你想做什么」组织。 | 你想用它，而不是改它。 |
| [`architecture.zh-CN.md`](architecture.zh-CN.md) | 架构说明，中文。 | 同上。 |
| [`government-sources.md`](government-sources.md) | Four routes to bulk government data, all measured, all rejected — and what to do instead. | You are about to go looking for permit or docket data. Read this first; it is a day you do not have to spend. |
| [`plan-scale-with-sources.md`](plan-scale-with-sources.md) | What breaks now that the best rows hold 26 citations instead of 2: `logic check`'s findings growing quadratically, and one building acquiring sixteen names. Measured; nothing built yet. | You are about to work on contradictions or capacity blocks, or you are wondering whether to point an LLM at either. |

There is no English counterpart to `guide.zh-CN.md`, and that is deliberate:
the root `README.md` already is one, and two documents covering the same ground
is how one of them becomes wrong.

## The three ideas

If you read nothing else, read these — everything on screen is shaped by them,
and they are also in the console's own Help tab.

**A model's answer is not a fact.** Every stored value carries a tier saying what
it rests on: `reported` (a verbatim sentence in a fetched article, checked
against that article), `derived` (a Census lookup — deterministic, but nobody
said it), `unconfirmed` (待确认 — extracted and unquotable, kept but never
counted), `inferred` (a model's judgement), `defaulted` (nobody said anything and
the column is NOT NULL). Absence is a fifth answer, and it is often the correct
one.

**Progress is five tracks, not one ladder.** Site control, permits, power,
construction, commercial. A campus can own its land outright and be four years
into an interconnection queue. Power is never inferred from the others, because
building ahead of grid connection is normal and a finished shell waiting on a
substation is the most valuable signal here.

**Confidence is recomputed, never stored,** and one source can never reach 3
however authoritative — independence is counted by domain.

## Not documented here

The migration files carry their own reasoning in comments; `0007_source_quotes.sql`
is the one worth reading if you care about how per-field evidence works.
`tracker/tracks.py` and `tracker/gaps.py` both open with an explanation of the
judgement they encode, and those are the two modules where the reasoning matters
more than the code.
