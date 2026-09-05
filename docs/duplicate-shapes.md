# What duplicates in this database actually look like

Measured against the 37 merges an operator performed by hand and the 7 pairs a model
ruled out, replayed by `scripts/eval_pairs.py --detection`. The point of writing it
down is that two plausible improvements to duplicate detection are refuted by these
numbers, and both are the kind of thing somebody proposes again every few months.

## The shape

**A duplicate here is one campus filed under different companies, not one place
filed at two precisions.**

| | |
| --- | --- |
| folded identities whose company stem matches their survivor | **3 of 37** |
| folded identities with no key-level signal connecting them to their survivor | **33 of 37** |

The Abilene campus was stored as Crusoe, as OpenAI, as Oracle and as "OpenAI/Oracle";
Richland Parish as Meta and as Entergy Louisiana; Sweetwater as IREN and as Iris
Energy. One party builds the site, one owns the land, one occupies it, and a fourth
sells it power. Each name is correct and each mints its own `dedup_key`.

The company is therefore the axis that disagrees, and it is the axis every key-based
comparison holds fixed. `dedup.is_cross_granularity_match` requires
`(company, state)` to be equal before it will compare localities at all, and
`upsert._find_duplicate_candidate` scopes its first query to
`dedup_key LIKE '<company>|%'`. So no amount of work on the *locality* half of a key
can connect rows whose *company* half already differs.

## Why Census containment does not help

The Census place-to-county table `tracker/ingest/geo.py` already downloads makes a
city row answer "which county am I in", which looks like it should connect a city row
to the county row for the same site — the PRD's flagship case, "Mount Pleasant" versus
"Racine County". Deriving that county at the write path, so an arriving article knows
its county before `_find_duplicate_candidate` runs, was designed and then measured
before it was built:

| | |
| --- | --- |
| city-granular folded keys the Census resolves to a single county | 17 of 24 |
| folds a derived county would have connected | **0 of 37** |
| live rows with a city, a blank county, and a county the Census could fill | **1 of 300** |

The lookup works — 17 of 24 resolve. It changes nothing because of the shape above:
a derived county rewrites the locality half of a key whose company half already
disagrees, and the three folds that *do* share a company are the opposite direction
(the folded key is county-granular and the survivor is the city), which deriving a
county from a city cannot address.

The last row of that table is the other half of the answer. `scripts/overnight.sh`
runs `tracker ingest geo` in the free phase of every round, and `geo.run` fills
`county` on every row the Census can place. By the time anything reads these rows the
work is already done, so there is nothing left for a second derivation to find.

**What would help instead** is the branch that already exists for this shape:
`dedup.looks_like_the_same_site`, reached by `_find_duplicate_candidate`'s
same-locality query, which pairs on a shared distinctive name token or a shared
party. "Stargate Abilene" arriving against a stored "Stargate Abilene" matches on
`stargate` whatever the companies are. That branch is where the cross-company case is
caught, and it is the one to strengthen.

## What the numbers here do not measure

`scripts/eval_pairs.py` scores positives by replaying a folded identity through the
write path, and a folded identity is **all that survives a merge** — the row's name,
citations and tranches are deleted with it. So the script can only exercise the key
path, and it reports "the key path finds the campus: 5 of 37".

That is not the gate's real recall and must not be quoted as one. A record arriving
from a crawl carries a name, `looks_like_the_same_site` uses it, and on the evidence
above that is the branch doing most of the work. Measuring the gate honestly needs a
database snapshot taken *before* a merge, where both rows still exist —
`scripts/eval_pairs.py --before` takes one, and the backups
`scripts/overnight.sh` writes before folding are where it comes from.

## The negatives say where the leverage is

Of the 7 pairs a model ruled `different`, **6 have no rail that refuses a merge**.
Every one of `dupresolve.evidence_blocks_merge`'s refusals passes them, so the only
thing standing between those pairs and a fold is the judge answering `different`.

That is worth being precise about: it is not that six wrong merges are pending — no
merge happens unless a judge says `same` above the floor with a quote. It is that on
this population the *rails contribute nothing* and the judgement is the whole
decision. Work that improves the judge's evidence or its reasoning therefore has
somewhere to land; further work on the rails does not.
