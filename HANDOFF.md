# Handoff

## Yesterday (state at start of 2026-08-03)

2026-08-02 (`1ef2209`) added SEC filings ingest and the capacity-by-customer
rollup: `tracker ingest edgar` (scoped EDGAR full-text search by CIK, paragraph
scoring instead of head-and-tail truncation, legal-vocabulary filtering to skip
credit-agreement exhibits) and `tracker capex` (rolled up by the company
actually buying capacity, not the site building it, with a same-site dedup
detector and a $50M/MW plausibility ceiling for investment figures). 759 tests,
green offline.

Carried into today, per that HANDOFF's own "Tomorrow": the 30-required-projects
gap, ERCOT/CAISO column names in `iso_maps.py` unverified against a real
export, the two free Google CSE keys not configured, `tracker ingest edgar` not
yet run against the live SEC endpoint, and `tracker capex`'s duplicate-site
heuristic checked against only one known case (Abilene).

## Today (2026-08-03)

Working tree held a large batch of uncommitted work — a survey that killed a
planned feature, a new contradiction-checking/merge/point-lookup subsystem,
and a cluster of correctness and Windows-compatibility fixes underneath the
console. Test suite grew from 759 to 1309 tests, verified green offline
(`.venv/Scripts/python -m pytest`, exit 0) before writing this file. Largest
first:

- **A government-sources survey, and the decision not to build one**
  (`docs/government-sources.md`, `scripts/probe_government_sources.py`, new):
  four candidate bulk-discovery routes — Socrata municipal permits, FERC/state
  PUC dockets, county news feeds, Legistar agendas — were probed live against
  the jurisdictions that actually hold data center capacity, and all four came
  back empty or irrelevant (0 candidates from 177 county-feed entries scored
  by the project's own discovery filter; Socrata has no datasets for Loudoun,
  Prince William, Virginia or Arizona). Conclusion recorded rather than
  assumed: don't build a government ingest path — utilities already file
  large-load commitments with the SEC (`ingest edgar --kind utility`), and a
  one-off `.gov` document is already handled by `ingest crawl --url`. The probe
  script exists so the finding can be rechecked as portals change.

- **`tracker logic check` / `tracker logic resolve`** (`tracker/logic.py`,
  new): a three-layer, cost-ordered contradiction checker — free deterministic
  rules (built MW exceeding planned, energized-but-not-operational, milestones
  dated in the future), free source-collision reporting that asks the real
  `upsert.resolve_field` (now public) which of two conflicting claims wins and
  why, and an optional paid LLM pass (`--read N`) that can only name
  contradictions, never pick a winner. Measured on the live DB: 21
  impossibilities, 125 warnings across 221 projects, and 0/149 findings were
  mechanically resolvable — everything else needs a human, via an
  interactive/`--llm` triage flow that writes decisions into `project.notes`
  tagged `operator resolved` vs `model resolved`. An earlier draft that
  re-derived the winner itself instead of asking the real resolver had falsely
  reported 73/221 rows as "drifted" — worth remembering as the reason this
  goes through `upsert.resolve_field` rather than reimplementing the policy.

- **`tracker merge --into ID ids...`** (`tracker/merge.py`, new): folds
  duplicate rows — the same campus stored under a builder's name, a
  landlord's, and a tenant's — into one survivor. Sources, milestones and
  risks move via the ORM relationship rather than raw FK edits (to respect
  cascade-delete), and every field on the survivor is recomputed from sources
  afterward so the choice of which row survives never determines the values.
  Gated behind a new `DESTRUCTIVE` command class (separate from `LLM_COMMANDS`)
  in the console, requiring a typed "merge" confirmation — it's the one
  command in this project that deletes anything. Per the new Chinese status
  doc, merging fixes a measured 24,125 MW double-count where OpenAI's
  attributed capacity was inflated 53% and Oracle's 100%.

- **`tracker point "<name>"`** (`tracker/point.py`, new): the on-demand
  counterpart to the batch commands — given a data center name, it builds a
  token-overlap shortlist, asks a model to match it to an existing row or
  "none" (confidence floor 0.7, deliberately asymmetric: a false "no match"
  just creates a duplicate that `tracker duplicates` can catch, a false "yes"
  silently corrupts another project's history). Matched runs `enrich`;
  unmatched runs targeted searches plus `crawl` to build a fresh profile.

- **`tracker/overview.py`** (new, no CLI command — served via `POST
  /api/overview`): a cached, fingerprinted LLM narrative briefing for one
  project's drawer in the console, invalidated whenever its sources, fields or
  milestones change, and explicitly never stored as fact.

- **Utilities and contractors as SEC filers**: 20 new companies in
  `seed/edgar-companies.toml` (20 → 40), chosen by measured state-capacity
  exposure (Texas 33.8%, Colorado 11.7%, etc.), with per-kind search phrases
  since a utility writes "large load" where a builder writes "build-to-suit."
  Wired up but not yet run against the live DB — today's source mix is
  unchanged until that ingest actually executes.

- **`tracker capex --by-quarter`**: buckets the capacity rollup by calendar
  quarter instead of by year, with a measured caveat surfaced in both the CLI
  and the console — 34% of dated projects normalize to 1 January, so the
  quarterly view is "a shape, not a schedule."

- **`tracker cloudflare`** (promoted from `serve --tunnel`'s flag to its own
  command): `--check` preflight, named-tunnel support (runs a tunnel someone
  already created; deliberately does not create one itself, to avoid
  credential/DNS side effects), and a `--name`/`--hostname` path for a stable
  public URL instead of a random `trycloudflare.com` one. Two tunnel bugs
  fixed underneath: cloudflared's own quick-tunnel API call ignores
  `HTTPS_PROXY`, so a loopback relay (`_QuickRelay`) now forwards it through
  whatever proxy is detected (env var or the Windows registry); and a failed
  tunnel was being reported as a working public URL because the failure
  message's own hostname (`api.trycloudflare.com`) matched the tunnel-URL
  regex.

- **Evidence-gate quote recovery** (`crawl._verbatim_run`): measured 131
  quotes across 8 articles, 33 of which failed exact-match — mostly the model
  resolving a pronoun while quoting ("The campus" → "The Austin campus").
  Acceptance went from 75% to 95% with zero false positives on a
  negative-control test, by recovering the longest verbatim run around a
  near-miss quote rather than rejecting it outright.

- **Windows socket-error handling**: `ConnectionAbortedError` (WinError
  10053), raised on tab-close on Windows, was never caught by the old
  `except (BrokenPipeError, ConnectionResetError)`, producing double
  tracebacks in the console's SSE stream. Widened to `ConnectionError` in
  `_stream`, `do_GET` and `do_POST`.

- **Console UI**: a Capex view with duplicate-review groups and merge-with-
  confirmation UI; an `InsightPanel` for the new overview briefing
  (deliberately placed below cited evidence, not above it); the run-log pane
  switched from wrapping to sideways-scrolling (`.dc-log` `pre-wrap` → `pre`,
  with a wrap toggle) so wide Rich tables stop shredding; a `$3.2T`-style USD
  formatting fix; and the dataset now auto-reloads on run completion instead
  of only polling run status.

- **Housekeeping**: `CHANGELOG.md` gained matching Added/Fixed entries for all
  of the above. `README.md` had zero mentions of the new `tracker point`
  command and still opened with "There is no web UI — this is a backend plus
  a CLI," directly contradicted by its own console section further down (added
  `694b204`/`836945b`, never corrected here) — both fixed. The stated test
  count ("579 tests") was stale by two sessions' worth of growth (`836945b`'s
  615, `1ef2209`'s 759); updated to the current 1309. No AGENTS.md added —
  the new logic/merge/overview/point modules are functions inside one CLI,
  not autonomous agents, so there is still no multi-agent architecture to
  document.

## Tomorrow

- The 30-required-projects gap is still open.
- ERCOT/CAISO column-name assumptions in `iso_maps.py` remain unverified
  against a real export.
- The two free Google CSE keys are still not configured.
- The 20 new utility/contractor EDGAR companies in `seed/edgar-companies.toml`
  are wired up but have not been run — worth an `ingest edgar --kind utility`
  pass to see whether utility filings actually move the needle on
  investment/in-service-date coverage as hoped.
- `tracker logic check`'s optional paid layer-3 judgement is explicitly called
  out in code as unproven — across 4 rows read during development it returned
  nothing beyond what the free rules already found. Worth watching whether it
  ever earns its cost, or should be cut.
- `tracker point`'s 0.7 confidence floor and `tracker merge`'s duplicate
  detection are both heuristics validated against a small number of known
  cases (mainly Abilene); worth revisiting as more real usage accumulates.
- `tracker cloudflare --name` still needs a real run against a named tunnel
  with DNS already pointed at it — the loopback-relay proxy fix is untested
  against an actual corporate proxy, only against the failure mode it targets.
