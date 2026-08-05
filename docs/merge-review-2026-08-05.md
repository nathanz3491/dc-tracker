# Merge review — deferred items, 2026-08-05

21 duplicate groups were merged on 2026-08-05 (Stargate Abilene/Milam/Doña Ana/
Lordstown/Shackelford/Michigan, Colossus, Camellia, Stingray, Reveille, the NTT
and Corscale name-variant pairs, Lux, MIT, Steamboat, Hyperion, and the two
AWS-Mississippi pairs from the EDGAR utility ingest). Each was dry-run first and
executed only where the rows plainly described one campus. Merges now persist
across crawls via `project_alias` (migration 0010).

The items below were **deliberately not merged**. Each needs a human call —
mostly because the rows differ in granularity (a building vs its campus), which
`dedup.py` explicitly refuses to auto-merge. Review and either run the command
shown or dismiss with a note.

## 1. Digital Ashburn Campus vs ACC8 (building-in-campus)

- `#14 Digital Realty — Digital Ashburn Campus` (Ashburn, VA)
- `#17 DuPont Fabros Technology — ACC8 Data Center` (Ashburn, VA)

DuPont Fabros was acquired by Digital Realty in 2017, and ACC8 is one building
on that campus. Merging folds a building's figures into a campus row — the MW
may be additive rather than duplicate. If merged: `tracker merge --into 14 17`.
Alternative: leave both and treat ACC8 as a capacity block of #14.

## 2. Iron Mountain VA-2 vs Manassas Campus (building-in-campus)

- `#62 Iron Mountain — VA-2` (Manassas, VA)
- `#82 Iron Mountain Data Centers — Iron Mountain Manassas Campus` (Manassas, VA)

Same shape as item 1: VA-2 is a building on the Manassas campus. Same options.

## 3. Project Jupiter, Doña Ana NM — one site still stored twice

- `#105 Oracle — Project Jupiter (Doña Ana County)` (locality: **Las Cruces**, NM)
- `#183 Oracle — Stargate - Doña Ana County (Project Jupiter)` (locality: **Doña Ana County**, NM)

Six rows for this site arrived under two locality spellings, so `tracker
duplicates` saw two groups; each group was merged internally, leaving these two
survivors. They are almost certainly one campus (Stargate NM / Project Jupiter,
Santa Teresa area), but the rows sit at different locality grains (city vs
county), which is exactly the pattern the dedup docs warn about. If confirmed:
`tracker merge --into 105 183` (keep #105, or pick the locality you prefer —
identity fields keep the survivor's values).

## 4. #231 AWS Hinds County — utility recorded as the operator

- `#231 Entergy Mississippi — AWS Hinds County Data Center` (Hinds County, MS,
  customer = Amazon Web Services)

From the Entergy 10-K ingest. The extraction prompt forbids recording the
utility as `company`, but it happened here; unlike the Madison/Warren twins
there is no AWS-keyed row to merge into. `company` is FILL_ONLY and never
overwritten by re-ingest, so this needs a hand edit (or delete + `tracker point
"AWS Hinds County"` to rebuild it cleanly). Capex attribution is unaffected in
the meantime — the named `customer` wins.

## 5. #197 Stargate Michigan — locality typo

- `#197 Related Digital — Stargate Michigan` (city recorded as **"Salien
  Township"**, should be **Saline Township**, MI)

Identity fields are FILL_ONLY, so the typo will not heal itself. Cosmetic, but
it will block future locality-based duplicate detection for this site.
