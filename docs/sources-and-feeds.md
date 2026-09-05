# Sources and feeds

Which publishers are worth crawling, what discovery costs per feed, and the command that acts on the measurement.

Part of the [dc-tracker documentation](README.md).

---

## Which publishers are actually worth crawling

```bash
tracker sources                      # by how much we use it
tracker sources --by contested       # by wins against a disagreeing rival
tracker sources --by yield           # decided per citation
```

`SOURCE_WEIGHTS` gives six *source types* a weight from 1 to 3, by hand, and
`docs/plan-claim-envelope.md` names it as the cautionary example of a field
nothing can verify. The obvious next step — hand-rank every publisher 1 to 10 —
is the same mistake at higher resolution: ten asserted numbers per host instead of
three per category, still with nothing to check them against.

So this ranks nothing. It counts what each publisher's claims actually **did**,
which re-runs to the same answer unless the database moved:

```
publisher                 cited  decided  contested  inert  per cite  weight
datacenterfrontier.com      267      240        116      5      0.90       2
sec.gov                     133      179         87      1      1.35       3
datacenterdynamics.com      170      163        105      0      0.96       2
yahoo.com                    49       20         11      0      0.41       1
```

`decided` is a value this host's claim won. **`contested` is the subset where a
rival asserted something different, and it is the only column that is evidence of
anything** — an unopposed win just means nobody else spoke, which a single-source
project gives away for free. The first version of this report ranked by
decided-per-citation and put eight `.gov` pages cited once apiece above every
trade outlet in the database, purely because each had no rival. Per-citation
orderings now need five citations before they rank a host.

Three details carry it:

* **Identity fields are excluded.** `name`, `company`, `city` and `state` are
  `FILL_ONLY` — the first source to arrive sets them and nothing can displace one —
  so a "win" there records crawl order. Counting them also made every citation
  look like a participant: `inert` came out as 0 across all 2,758 rows.
* **The winner is whoever the *policy* picked, not the head of the sorted list.**
  `mw_built` takes the MAX, `first_announced` the MIN, `phase` the furthest rung,
  so on four of the twelve fields the strongest source routinely loses to a weaker
  one. It asks `upsert.resolve_field` — the same function the write path asks —
  rather than re-deriving the order, which is what `logic check` learned the hard
  way when a hand-rolled copy reported 73 rows as changed and nothing had changed.
* **Reference data is not a publisher.** Census geography is cited like a source
  but publishes nothing, and ranking it produced `www2.census.gov: 184 cited, 184
  inert, 0 decided` — the worst outlet in the database, for a lookup table.

`cited == decided_sources + contributing + inert` holds by construction and is
asserted in the tests, so a row that does not add up is a bug here rather than a
judgement call.

The finding it exists to surface is already visible above: **Data Center Frontier
and DataCenterDynamics are `trade_press`, weight 2, and out-decide almost every
weight-3 host in the database.** Nothing here changes a weight —
`SOURCE_WEIGHTS` is still edited by hand in `tracker/confidence.py`. This is the
evidence for doing so, and `--json` is the machine-readable form.

## Acting on it: `tracker sources policy`

```bash
tracker sources policy               # propose; writes nothing
tracker sources policy --apply       # write <home>/seed/sources.toml
```

The ranking above could only ever be *printed*. This turns it into a file that
`sync`, `enrich` and `ingest crawl` read: `priority` domains are offered first when
a run is working to a budget, `ignore` domains are not queued or fetched again.

**It changes what gets read. It never changes what a stored citation is worth.**
Weight stays per `source_type`; applying the policy across 300 projects left every
field byte-identical, and a test snapshots whole rows to keep it so.

On the live database it proposes **16 priority and 1 ignore** out of 654
publishers. Both thresholds came from measuring, and the first two attempts were
wrong:

| rule tried | result |
|---|---|
| ignore anything deciding nothing | 9 publishers at a sensible floor, 1 at a strict one |
| priority above `LOW_YIELD` (0.15) | **75 of the 94** judgeable — not an ordering |
| **priority above the fleet's own mean** (0.58 today) | **16**, and they are the ones carrying the dataset |

So `priority` is measured against the corpus rather than a constant, and adjusts as
it grows. `ignore` fires on **zero, never on thin** — `LOW_YIELD` is documented in
`funnel.py` as reported and never proposed, and that discipline carries down: thin
is a prompt to look, zero is a proposal.

**The refusals carry more than the proposals**, so they are printed:

```
cannot read       digitalrealty.com (28 unread against 11 cited)
thin              cnbc.com (0.12 per citation), datacenterscatalogs.com (0.05) and 4 more
too few to judge  560 publishers below five citations
```

`cannot read` is checked before anything can propose ignoring a host: a publisher we
mostly cannot *fetch* looks identical to a worthless one from the citation count
alone. That is the mistake `tracker feeds` documents, and here it would be worse,
because this writes a file that silences the host. Also refused — a domain still
configured as a feed (retire it in `feeds.toml`, or discovery keeps polling and
discarding it), and an operator's own newsroom.

**It proposes; you decide.** A domain already in the file keeps its rank *and* its
sentence on a re-run, and nothing is ever deleted — an entry the evidence no longer
supports is reported and left alone. The argument for that is in the data: `cnbc.com`
and `entergy.com` both sit in the thin band, and Entergy is the utility building
Hyperion's power.

The file is keyed on `confidence.registrable_domain`, the same identity the ranking
prints, so a row is directly pasteable. That matters more than it sounds: there are
five different URL→host normalisations in this codebase, and a policy keyed on the
wrong one would silently never fire while the run *looked* like it obeyed. Matching
is on label boundaries — `x.com` covers `mobile.x.com` and must never touch
`equinix.com`, which a substring test once did.

## What discovery costs, per feed

```bash
tracker queue stats
```

The number it exists for: **2,381 of 4,854 URLs that reached an LLM call produced
no project at all — 49%.** The filter is two tiers of keywords over a headline and
a URL path, so it cannot tell an article *about* a project from one that mentions
the industry, and half the extraction budget goes on the difference.

```
feed                       queued  read  none  waste  cited  dated
applied-digital-newsroom       44    17    17   100%      0      0
cologix-newsroom               66    54    45    83%     12     66
datacenterknowledge            34    23    16    70%     11     34
```

Per feed it becomes actionable. `applied-digital-newsroom` is 17 calls and 17
misses — the site-wide banner card that `MIN_PROSE_CHARS` was measured against.
A `topic_implied` newsroom that covers a wider beat than the flag assumes shows up
as a column of `none`.

It also prints why fetches failed, because a timeout, a 403 and a 404 have three
different remedies and one `fetch_error` count cannot tell them apart. That audit
has already earned its place by coming back **negative**: of 1,081 failures, 625
are HTTP 403 and 198 are 429 — deliberate blocks, which is what the `curl_cffi`
rung exists for — not the swallowed timeouts that were suspected.

Everything is derived from `ingest_url` on each run, so there is no counter to
drift. `/api/discover` serves the same payload to the console.

## Feeds worth adding, and feeds worth retiring

```bash
tracker feeds                        # both halves
tracker feeds --no-probe             # retire only, free, no network
tracker feeds datacenterfrontier.com # or probe one host by name
```

**Retiring is where the naive metric goes wrong**, and it is worth stating why.
"Found the most, used the least" sounds like the right rule and is not — three
different situations produce an identical zero:

```
feed                       queued  read  none failed cited
applied-digital-newsroom       44    17    17      0     0   read it, said nothing
datacenterdynamics             39     1     0     12     0   couldn't read it
utilitydive-archive            73     0     0      0     0   never read it
```

Only the first is a candidate. The second is behind Cloudflare and is kept on
purpose — `tracker/seed/feeds.toml` carries ten lines saying so, because the headlines
still tell you which projects exist. The third has never been read, so retiring
it would be deciding on a sample of nothing. A queued-versus-cited ratio ranks
all three the same and puts the deliberately-kept one at the top of the kill
list, so the split is on **what happened after the fetch**, not on volume.

A feed is proposed only when it has been read at least ten times and has never
once backed a stored value. A feed that cites anything is reported as low-yield
and never proposed — a citation is a contribution, however thin. The proposal
quotes the *queued* count rather than the calls already made, because calls
already made are sunk and what retiring buys is not making the next ones.

It prints the `tracker queue --drop --feed X` line and stops. **It does not edit
`tracker/seed/feeds.toml`** — that file is mostly hand-written justification, including
the comment that stops someone deleting DataCenterDynamics, and a command that
rewrote it would strip the reasoning that prevents the mistake.

One number is printed with every run, because it bounds the whole exercise:
**2,148 of 2,381 wasted calls came from URLs no feed found** — search and archive
sweeps. Retiring feeds can address the other 10%. If you want the 49% down, the
filter and the search templates are where the volume is.

`tracker/seed/feeds.toml` is hand-maintained, which is the wrong way round: the database
already knows which publishers decide stored values. Candidates are the hosts
`tracker sources` ranks highest that the config does not list. No LLM — the answer
is in the data, and `docs/plan-scale-with-sources.md` already measured what asking
a model to do a deterministic job costs.

Three rungs per host: `robots.txt` `Sitemap:` lines, then `/feed`, `/rss.xml` and
friends, then the homepage's `<link rel="alternate">`. A `<sitemapindex>` is
followed one level, and that detail is what makes it work — `/sitemap.xml` on Data
Center Frontier is an index, parses to zero entries and reads as "not a feed";
following it lands on `sitemap/Article.xml`, which is exactly the entry
`feeds.toml` already carries.

**Every hit is parsed and run through the real filter**, so the report says how
many entries would have been *queued* rather than that a URL answered. That is
what stops a bad add: `sec.gov` decides 179 values and its sitemap queues 0%,
because EDGAR is reached by `ingest edgar` and never by polling a feed. It prints
TOML to paste and writes nothing.
