# What duplicates in this database actually look like

Measured against the 90 merges an operator performed by hand and the 38 pairs a model
ruled out, replayed by `scripts/eval_pairs.py --detection` on the production database.

> **Measure on production, not on a pulled copy.** This page first carried numbers
> taken from a development copy holding 300 projects and 37 aliases, against
> production's 437 and 90 — and on that smaller corpus the headline conclusion came
> out backwards. "A derived county would connect 0 of 37 folds" became **11 of 90**
> the moment it was asked of the real database. A copy pulled weeks earlier is a
> different corpus, not a smaller view of the same one, and a conclusion drawn from
> one is worth what its sample is worth. `scripts/eval_pairs.py` runs read-only, so
> it can be run on the host: `ssh $PROD 'cd ~/dev/tracker/repo && .venv/bin/python
> scripts/eval_pairs.py --detection'`.

## The shape

Duplicates here come in two shapes, and neither dominates.

| | |
| --- | --- |
| folded identities whose company stem matches their survivor | **43 of 90** |
| folded identities with no key-level signal connecting them to their survivor | 48 of 90 |
| of the rest: shared key 26, shared party 5, cross-granularity 5, both 4 | |

**One campus under several companies** is the shape the tooling was built around, and
it is about half the corpus. The Abilene campus was stored as Crusoe, as OpenAI, as
Oracle and as "OpenAI/Oracle"; Richland Parish as Meta and as Entergy Louisiana. One
party builds the site, one owns the land, one occupies it, and a fourth sells it
power. Each name is correct and each mints its own `dedup_key`. Nothing that compares
keys can connect these, because every key comparison holds the company fixed:
`dedup.is_cross_granularity_match` requires `(company, state)` to match before it will
look at localities at all.

**One place at two precisions** is the other half, and it is the half a key *can*
reach — 43 of 90 folds share a company stem, so the locality is the only axis that
disagrees. That is what makes the Census containment table worth using.

## Census containment: worth it at the gate, not in the report

`tracker/ingest/geo.py` already downloads the Census place-to-county table, and
`overnight.sh` runs `tracker ingest geo` every round, which fills `county` on stored
rows. The question is whether deriving that county at the *write path* — so an
arriving article knows its county before `upsert._find_duplicate_candidate` runs —
would catch duplicates the gate currently misses.

| | |
| --- | --- |
| city-granular folded keys the Census resolves to a single county | 39 of 50 |
| **folds a derived county would have connected** | **11 of 90** |
| live rows with a city, a blank county, and a county the Census could fill | **0 of 437** |

The two ends of that table are the whole answer, and they point opposite ways.

**In the report, nothing.** `ingest geo` has already filled every county it can, so
there is no stored row left for a second derivation to improve. Deriving counties in
`capex.suspected_duplicates` would raise no pair that is not raised today.

**At the gate, eleven real folds.** An *arriving* article has a city and no county —
extraction reads one page and knows nothing about counties — so the incoming key set
never contains the county-granular key that would meet a stored county row. Those
eleven are ordinary and repeatable: `compass datacenters|city:ashburn|VA` and
`compass datacenters|city:leesburg|VA` both belong to a Compass row filed under
Loudoun County; `vantage|city:sterling|VA` and `digital realty|city:sterling|VA` the
same; `nscale|city:point pleasant|WV` against Mason County, which is the case
`upsert.py`'s own docstring names as unsolved.

Each is a duplicate that was created, sat in the report, and was eventually folded by
hand. Preventing one costs a lookup in a table already on disk.

## What these numbers do not measure

`scripts/eval_pairs.py` scores positives by replaying a folded identity through the
write path, and a folded identity is **all that survives a merge** — the row's name,
citations and tranches are deleted with it. So it exercises the key path only, and
reports "the key path finds the campus: 38 of 90".

That is not the gate's real recall and must not be quoted as one. A record arriving
from a crawl carries a name, `dedup.looks_like_the_same_site` uses it, and on the
evidence above that branch is what catches the cross-company half. Measuring the gate
honestly needs a snapshot taken *before* a merge, where both rows still exist —
`--before` takes one, and the backups `scripts/overnight.sh` writes before folding are
where it comes from.

## The negatives say where the judgement matters

Of the 38 pairs a model ruled `different`, **24 have no rail that refuses a merge**.
Every refusal in `dupresolve.evidence_blocks_merge` passes them, so the only thing
between those pairs and a fold is the judge answering `different`.

That is not 24 wrong merges pending — no merge happens unless a judge says `same`
above the floor with a quote. It is that on nearly two thirds of this population the
*rails contribute nothing* and the judgement is the whole decision. Work that improves
the judge's evidence or its reasoning has somewhere to land; further work on the rails
has less.
