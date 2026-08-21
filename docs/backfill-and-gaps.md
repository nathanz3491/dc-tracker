# Backfill and gaps

Finding where the data is thin, and filling it — capacity blocks, county and coordinates, and running the phases separately.

Part of the [dc-tracker documentation](README.md).

---

## Seeing where the data is thin

```bash
tracker gaps
```

Per-field coverage measured against the rows where the field can legitimately be
set — not against every project. That distinction matters more than it sounds:
`mw_built` looked 13% covered while 61 of the 90 projects were merely *announced*,
so nothing was built on any of them and a NULL was the correct answer. Counting
those as misses pointed effort at work that could never succeed. Fields whose
absence carries no information (`blocker`, `customer`) report `n/a` rather than a
low score, and the run ends with the measurable fields that have the most rows left
to fill.

## Filling in capacity blocks on projects ingested before they existed

```bash
tracker backfill blocks --limit 25
```

A campus is rarely one thing. Lake Mariner is 378 MW under construction for
Fluidstack, 60 MW already serving Core42, and 145 MW of legacy capacity energised —
and until blocks existed the row could only say one number and one customer. Every
project ingested before migration `0009` still has no blocks, because turning an
article into blocks needs the article text rather than the schema.

This re-reads the stored articles for that one purpose. It is deliberately **not**
`ingest crawl --force`: a plain re-crawl re-extracts every scalar with a model that
behaves differently today than it did at ingest time, churning 227 rows and every
`updated_at` inside what is supposed to be a backfill. This writes one column,
`source.blocks`, and lets the ordinary rollup do the rest.

Costs one LLM call per article, keyed on URL rather than source row — 373 crawled
source rows are only 229 distinct articles, because 62 feed more than one project.
Most are already cached, so `--refetch` is only needed for the remaining 36. It is
resumable (an article whose blocks are stored is skipped) and idempotent (blocks are
rebuilt wholesale, keyed on `(project_id, block_key)`), so the sensible way to run it
is in tranches: `--limit 25`, look at what came back, then more. Filings sort first,
because per-phase tables are where blocks actually live.

**The part worth understanding before running it.** One article routinely describes
several campuses, and one URL is often already cited by several rows, so deciding
*whose* blocks these are is the whole job — and getting it wrong does not mislabel a
value, it moves megawatts into another campus's total. Two guards, both added after
the unguarded version wrote real wrong numbers into a copy of the database:

- **A row is matched on locality, never on the operator.** An earlier version wrote
  an 80 MW "Portland Expansion" onto eight STACK rows, every one of which matched on
  "STACK Infrastructure". A stated city that *disagrees* is now a veto, not merely a
  low score.
- **A portfolio article is split, but only when it is one.** A Core Scientific filing
  covering five campuses gave all six of its blocks to both the Denton row and the
  Dalton row, recording 588 MW twice. So each block is now routed by its own label —
  but only once some block in the article is found to name one row and not another.
  Demanding it unconditionally was tried and was worse: it emptied Lake Mariner,
  whose blocks are called "Akela" and "La Lupa", because a building is usually named
  after nothing in particular.

Both guards skip rather than guess. A missed block is a gap somebody can see; a
misrouted one is a wrong number nobody can.

**What the blocks then change.** Once a project has them, four `logic` rules stop
calling a partly-live campus contradictory — a campus with one tranche energised and
another going up is a campus, not a defect, and that was all 18
`energized_but_not_operational` findings. `tracker show` grows a block table above
the sources, the console drawer grows a Blocks tab, and Capex splits capacity between
the buyers the tranches name rather than giving a whole campus to one of them. A
project with no blocks behaves exactly as it did before, because no blocks means
nothing has been read rather than that the row agrees with itself.

**A block's capacity counts only if a quote named it.** A block whose `mw` came
through as 待确认 is still recorded and still shown — that is what the tier is for —
but it is left out of the campus total, and `reconcile` says which blocks it left
out. Summing an unconfirmed figure launders it into a total that then reads as
cited, and the first live tranche proved the cost: it raised one campus from 7 MW to
7,500 MW off a single unquoted block. Read the run's notes; `mw_planned` moving by
orders of magnitude is the thing to look for.

## Filling county and coordinates without an LLM

```bash
tracker ingest geo --dry-run
tracker ingest geo
```

Derives `county`, `lat` and `lon` from US Census reference data — no API key, no
LLM, no per-row cost. The two files are gitignored (3.8 MB of national lookup
tables); the command prints their download URLs if they are missing. See
"Deriving county and coordinates" below for why this is a lookup rather than a
search problem, and what it deliberately refuses to guess.

## Or run the phases separately

```bash
tracker discover --since-days 45
tracker queue
tracker ingest crawl --check
tracker ingest crawl --from-queue --limit 10
```

`discover` polls the feeds in [seed/feeds.toml](../seed/feeds.toml), keyword-filters
the headlines and queues what matches. Nothing is fetched or sent to an LLM at
that point — `queue` shows you the headlines so you can drop the noise before
paying for extraction:

```bash
tracker queue                     # newest first, whole URLs, one id per row
tracker queue --feed ntt-newsroom # one source at a time
tracker queue --drop --id 1904    # the id is the handle; the URL is a link
tracker queue --drop --feed ntt-newsroom
tracker queue check               # ask every queued URL whether it is still there
tracker queue prune               # re-apply the filter to what is already queued
```

**The URL column is the whole URL.** It used to be `url[:60]`, which looked tidy
and was the most damaging thing in this output: the string on screen was a *prefix*
of a real link, so opening it gave "404 not found" and pasting it into `--drop
--url` matched nothing. A queue whose links all 404 is a queue nobody trusts. Every
row now carries its id, which is a short handle that cannot be mistaken for a link.
The order is newest-first, because a queue is read by a person deciding what is
worth a crawl while the crawl itself drains oldest-first.

The two maintenance commands exist because a queue is a promise that everything in
it is worth an LLM call, and two things break that promise quietly:

* **`check`** fetches every queued URL and reports which are gone. Conservative
  about what "gone" means: 404 and 410 are dead, **403 and 429 are not** — a
  newsroom answering 403 to a non-browser is what `ingest crawl --browser` is for,
  and on the live queue that was 55 URLs across seven publishers, which is to say
  the best-defended sources. `--drop` removes the dead ones and nothing else.
* **`prune`** re-applies the filter in `seed/feeds.toml` to rows that were queued
  under an earlier version of it. The filter is data and data gets edited; nothing
  ever re-applied it, so the queue accumulated everything that passed any *past*
  filter. On the live database that was 417 of 1,241 queued candidates — NTT
  marketing articles, DataBank compliance blogs, sponsored posts, and Meta's
  announcement of the winners of an AR effects contest. Report-only until `--drop`,
  and a row from a feed no longer in the config is left alone: commenting out a
  feed should not delete a queue.

A representative run over seven feeds saw 150 entries, filtered 132, and queued
18. Of those 18, four were genuine US project announcements. **Precision is
deliberately not the goal** — the queue is a human checkpoint, and it is cheaper
to over-collect and triage than to tune a keyword filter until it silently drops
real projects. Two categories are knowingly left in: industry commentary that
mentions capacity figures, and non-US projects. The latter cost one LLM call and
are then dropped correctly, because `norm_state` will not accept "Islamabad" —
better than a geography keyword rule that also discards real US sites.

You can still supply URLs by hand with `--urls urls.txt` (one per line, `#`
comments allowed), which is the right thing when you already know what to read.

Reviewing and exporting:

```bash
tracker review                    # projects at confidence <= 1, with reasons
tracker review --verify 3         # record that you checked project 3
tracker show 3                    # full detail with every citation
tracker verify                    # progress toward the required project list
tracker export md > tracker.md    # Markdown table, byte-stable across runs
```
