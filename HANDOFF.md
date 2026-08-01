# Handoff

## Yesterday (state at start of 2026-08-02)

2026-08-01 (`836945b`) added console auth/tunnel, mobile layout, honest
coverage, a Help view and `docs/`: `tracker serve --tunnel` publishing through
a Cloudflare quick tunnel with a password gate that refuses to start without
`TRACKER_CONSOLE_PASSWORD`; the projects table reordered by measured coverage
(visible cells ~46% -> 95%); a sub-720px mobile card layout; a Help view plus
`docs/architecture.md` and Chinese translations; colour restored in the run
log; map zoom/pan; and `tracker ingest crawl --url URL` for a single link. 615
tests, green offline.

Carried into today, per that HANDOFF's own "Tomorrow": the 30-required-projects
gap, ERCOT/CAISO column names in `iso_maps.py` still unverified against a real
export, the two free Google CSE keys not yet configured, and `tracker serve
--tunnel` untested against a real Cloudflare tunnel.

## Today (2026-08-02)

Working tree held substantial uncommitted work — a new ingest source, a new
report, and several correctness fixes underneath both. 759 tests (up from
615), verified green offline before committing. Largest first:

- **`tracker ingest edgar`** (`tracker/ingest/edgar.py`, new): SEC filings as a
  source. Publication there is a legal obligation rather than a website's
  goodwill, so it is the one source that can't 403 us, and it's where the
  fields this project covers worst actually live — investment, in-service
  dates, named tenants in lease footnotes. Precision comes from scoping
  EDGAR's full-text search by CIK (unscoped, "data center campus" returns
  1,066 hits led by shell companies); a 369,000-character 10-Q is reduced by
  scoring paragraphs for evidence density rather than truncated head-and-tail;
  and a credit agreement mis-filed as an 8-K exhibit is dropped by
  legal-vocabulary density before it costs a model call. New
  `seed/edgar-companies.toml` names which companies and CIKs to read, and its
  `kind` column (hyperscaler/neocloud/landlord) is reused by `capex` below.
  Needs `TRACKER_USER_AGENT` set to a real contact — SEC blocks the shipped
  placeholder outright.

- **`tracker capex`** (`tracker/capex.py`, new): capacity and spend rolled up
  by the company actually *buying* it, not the site building it. The database
  is keyed on `(operator, locality, state)`; a lot of hyperscaler capacity is
  built by wholesale developers and leased, so that key often isn't the
  tenant. Attribution is a named tenant, else the operator when the operator
  is itself a known end user (from `edgar-companies.toml` plus a short
  hard-coded list of SEC-silent private buyers — OpenAI, xAI, Anthropic,
  others — without which two of the largest positions in the table would be
  invisible), else an explicit unattributed row. Every number is a floor, and
  the footer says what fraction of projects the rollup can actually speak
  for. Flags rather than auto-corrects two smells: a project naming a tracked
  operator as its own customer, and rows that look like the same physical
  site stored under a builder's name and a tenant's name both.

- **Same-site dedup detection** (`dedup.looks_like_the_same_site` and
  supporting helpers): recognizes when a builder/landlord/tenant split put one
  campus in the database three or four times under different company
  spellings — found live on the Abilene Stargate campus, stored as Crusoe,
  Oracle, OpenAI and "OpenAI/Oracle", each correctly keyed and each carrying
  the full 1.2 GW. This is exactly what made the first pass at `capex`
  overcount. Locality alone isn't the signal (Ashburn alone holds fourteen
  genuinely distinct projects); a match needs a shared company token or a
  distinctive shared name token. Proposes a review candidate, never merges.

- **An investment/MW plausibility ceiling** (`crawl._implausible_investment`,
  $50M/MW, read off the live distribution rather than assumed): catches an
  article about one campus that quotes a programme total ("OpenAI's $500
  billion Stargate" in a piece about one 1,167 MW site) — the evidence gate
  correctly confirms the number is in the text, but the number isn't this
  site's. Demoted to 待确认 rather than dropped.

- **`discover.cache_feed_text`**: when a feed syndicates the full article body
  (`content:encoded`/Atom `content`) rather than a teaser, it's written
  straight into the article cache so crawl never re-requests the page — fixing
  every article from outlets (state nonprofit newsrooms among them) that serve
  a free feed and then 403 any non-browser fetch of the article itself. Two
  guards: short bodies are skipped (a summary read as if it were the article
  would starve the evidence gate), and an existing cache entry is never
  overwritten (a real fetch is more complete than a feed excerpt).

- **Five new feeds** (`seed/feeds.toml`: bisnow-datacenter, constructiondive,
  semianalysis, qts-newsroom, coreweave-blog, others) to break a
  single-outlet dependency — 130 of ~180 editorial citations had come from one
  domain, capping 109 of 124 projects below confidence 3.

- **Six bug fixes underneath the above**, all now in CHANGELOG's Fixed
  section: `html_to_text` mis-reading numeric HTML entities (`&#160;` appears
  17,469 times across 39 filings and silently corrupted or dropped adjacent
  figures — replaced the nine-entity hand table with `html.unescape`); two
  events or risks sharing a `(type, date)` within one record crashing the
  whole upsert (a filing can report two same-day expansions, which a news
  article rarely does); `--browser` never actually working on any of
  `enrich`/`sync`/`ingest crawl` (the fetcher was built but never entered —
  fixed by having `fetch_all` start/stop it lazily, since it's the only code
  already inside the event loop); an expired console session being invisible
  (each widget blamed its own local cause — the 3D map said "unavailable
  offline" — instead of the session having expired; `api()` now redirects to
  `/` on 401); a failed map/3D-atlas module load being cached forever
  (`p = p || import(...)` memoises rejected promises too — now cleared on
  failure with a "tap to retry" control); and a quote popover in the console
  table being unreachable on a touchscreen (hover-only; now also opens on tap).

- **Housekeeping**: CHANGELOG's Added section was missing entries for edgar,
  capex, the dedup/plausibility work and the new feeds — all had landed in
  code and in README but not in CHANGELOG. Added entries for all of the above
  (seven Added, five Fixed) this session to close that gap. README already
  covered edgar and capex with worked examples; no changes needed there. No
  AGENTS.md exists or was added — still a single-CLI project with no
  multi-agent architecture to document.

## Tomorrow

- The 30-required-projects gap is still open.
- ERCOT/CAISO column-name assumptions in `iso_maps.py` remain unverified
  against a real export.
- The two free Google CSE keys are still not configured.
- `tracker serve --tunnel` and its auth gate remain unverified against a real
  Cloudflare tunnel.
- `tracker ingest edgar` has not yet been run against the live SEC endpoint in
  this session — worth a real run (with `TRACKER_USER_AGENT` set) to confirm
  the full-text search, section extraction and rate limiting behave as
  measured against a fresh set of filings rather than the 39 already sampled.
- `tracker capex`'s duplicate-site detection is heuristic (shared company
  token or shared distinctive name token) and has only been checked against
  the one known case (Abilene); worth watching for false positives/negatives
  as more filings widen the company list.
