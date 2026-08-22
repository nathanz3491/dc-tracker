# Documentation

The root [`README.md`](../README.md) is the tour — what this is, how to install it,
the core loop, and the three ideas everything else is shaped by. These files are the
detail behind it.

## Using it

| | What it covers |
| --- | --- |
| [Ingesting](ingesting.md) | The API key, the one-command loop, the operators we have no rows for, depth versus breadth, reaching back for older projects, operator press releases, optional search providers, and SEC filings |
| [Sources and feeds](sources-and-feeds.md) | Which publishers actually decide stored values, what discovery costs per feed, feeds worth adding and retiring, and `tracker sources policy` |
| [Backfill and gaps](backfill-and-gaps.md) | Seeing where the data is thin, filling capacity blocks on older rows, deriving county and coordinates without an LLM |
| [The terminal interface](tui.md) | `tracker tui` — six panes over the same data, the run pane that reads the CLI rather than listing it, and how it is verified over ssh |
| [The console, and exporting](console-and-export.md) | The whole dataset as one page, the live console and its two modes, driving it without a browser, and publishing it |
| [Analysis](analysis.md) | Who is buying the capacity, what could stop these projects being built, `tracker infer`, and how slippage is measured |

## Understanding it

| | What it covers |
| --- | --- |
| [Architecture](architecture.md) | How the CLI, the database and the console fit together — and why the browser never computes a judgement of its own |
| [Data quality](data-quality.md) | Numbers that cannot be true, one tranche wearing several names, values that contradict each other, what each stored value rests on, and why crawl order is not publication order |
| [Design decisions](design-decisions.md) | The reasoning that is not obvious from the code, and where this diverges from the PRD — the evidence gate, provenance per field, confidence, the schema additions, the seed file |
| [Government sources](government-sources.md) | Four routes to bulk permit and docket data, all measured, all rejected, and what to do instead. Read it before going looking; it is a day you do not have to spend |

## Not documented here

The migration files carry their own reasoning in comments; `0007_source_quotes.sql`
is the one worth reading if you care about how per-field evidence works.
`tracker/tracks.py` and `tracker/gaps.py` both open with an explanation of the
judgement they encode, and those are the two modules where the reasoning matters
more than the code.

`CLAUDE.md` at the repo root is the operating rules — which machine may write data,
how code reaches production, and what must never be committed.
