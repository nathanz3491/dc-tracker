# Revision plan: source breadth, and the customer axis

Written 2026-07-31, revised 2026-08-01 after a research sweep.

Responds to two pieces of review feedback:

1. **Data sources are too narrow** — cloud-operator newsrooms plus a few trade
   sites, with no government (permits, zoning), utility, or construction coverage.
2. **Weak linkage to the actual question** — the point of tracking construction is
   to read the pace of AI capex, which is a question about *end customers*
   (Meta, Google, Microsoft, OpenAI, Oracle), not about whoever pours the concrete.

Both are correct.

> **Provenance warning.** Five research subagents wrote reports into the session
> scratchpad (`research-*.md`). **Do not build from those files.** Roughly a third
> of what they reported as "verified" did not survive re-testing, including
> fabricated HTTP 200s and wrong CIKs. Everything in *this* document was
> re-verified by hand. Corrections are listed in Part 4.

---

## Part 0: where we actually stand

Measured against `data/tracker.db`, 124 projects / 264 sources.

| Metric | Value | Consequence |
|---|---|---|
| Editorial citations from `datacenterfrontier.com` | 130 of ~180 (72%) | One outlet is effectively the dataset |
| `government_doc` sources | 84 — **all `www2.census.gov`** | Zero real government documents; those 84 are the geo derivation |
| Projects resting on a single domain | 109 of 124 | `confidence.compute()` needs 2 independent domains for a 3, so only 12 reach it |
| `customer` populated | 12 of 124 | 4 are anonymised non-names, 1 (`Aligned Data Centers`) is an operator misfiled as tenant |
| `expected_online` populated | 29 of 124, 17 already past | "What lands next quarter" is unanswerable today |
| Open `transmission` risks | 2, across 124 projects | Obviously wrong for this industry — a direct symptom of no power-side sources |

---

## Part 1: verified source inventory

Everything below was tested with real HTTP requests. Where a source was tested
with the project's own `_RawFetcher` + `parse_feed` + live `[filter]`, the match
count is what `tracker discover` would really see.

### 1a. Best single find — Prince William County, VA

Free, unauthenticated, queryable ArcGIS JSON:

```
https://gisweb.pwcva.gov/arcgis/rest/services/CountyMapper/LandDevelopment/MapServer/{layer}/query
  ?where=1%3D1&outFields=*&returnGeometry=false&f=json
```

**Layer 10 — Data Center Buildings, 211 records.** County-assigned status:
88 Planned, 57 Completed, 36 Pending, 28 Under Construction, 2 Under Review.
Fields: `BuildingName`, `Address`, `BuildingStatus`, `YearBuilt`, `GFA`,
`ApprovedGFA`, `PermittedGFA`, `OCCDate`, `PermitCase`, `PermitStatus`,
`PlanningCaseNumber`, `EnergovID`. Totals: 59.7M sq ft planned, 26.3M approved,
3.7M permitted.

**Layer 11 — Data Center Campuses, 70 records.** `CampusName` **names the
tenant**:

```
Microsoft Azure Gainesville Tech Park   PlannedGFA 1,245,499  Pending
Microsoft Azure Balls Ford Road         PlannedGFA 1,145,989  Planned
CorScale Gainesville Crossing           PlannedGFA 1,981,643  Planned
Amazon Bethlehem Technology Park        PlannedGFA   740,000  Completed
Yondr Bristow                           PlannedGFA 2,427,137  Planned
```

Plus `RemainingGFA` (entitled but unbuilt — a pipeline metric nothing else
gives us), `ProjectStatus`, `CaseNumber` (joins to the rezoning case),
`ZoningDistrict`, `GISAcreage`, `DCOOD`. Layer 2 "Pending Planning Cases" adds
`StaffReportLink`.

**Honesty constraint: GFA is square feet, not MW.** Converting is an estimate,
and this project does not store estimates as facts. It must be its own field,
never `mw_planned`.

### 1b. Other county/state GIS — much thinner than it first appeared

| Source | Records | Verdict |
|---|---|---|
| VA DEQ `Data_Center_Project` (`services2.arcgis.com/SbxFMy5nFd6wmwCw`) | 26 | **Genuine.** Air-permit registrations with `Site_Name` carrying operator facility codes (`Amazon Data Services (IAD-130, IAD-131…)`), `Registration_No`, lat/lon. But the layer is titled *"Prince William Co. Data Centers with Permits"* — one county, all 26 already Operating. Value: proves the air-permit angle and gives an exact join key |
| Fairfax County `data_center_points_aggregated_9_8_23` | 16 | Marginal. Has `Prop_name`, `Owner`, `Total_GFA`, but tiny and a stale 2023 snapshot |
| Loudoun `Data_Center_Building_Outlines` | 271 | **Near-useless.** Names are redacted — `"Data Center - XXXX BP (1.79 Acres)"` — and fields are map graphics (`SymbolID`, `Extruded`, `Shape_Area`). No operator, status, or date |
| Bay Area MTC "Permitted and Proposed Data Centers" | 126 | Marginal. Raw geocoder output (`Loc_name: World`, `Score`, `Match_addr`). Addresses only |

**Conclusion:** Prince William is exceptional because that county chose to
publish a data center inventory. It is *not* a pattern you get for free
elsewhere. The generalisable asset is the **ArcGIS Online search API**
(`https://www.arcgis.com/sharing/rest/search?f=json&q=…`, free, unauthenticated)
as a discovery tool — but field quality varies from excellent to worthless, so
each jurisdiction needs individual assessment.

### 1c. SEC EDGAR — the highest-leverage build

Free, no API key, `User-Agent` header required. **The precision problem is
solved:** `ciks` scoping works when the CIK is 10-digit zero-padded.

```
ciks=1326801    (unpadded)   →     0 hits    ← the trap
ciks=0001326801 (padded)     →   105 hits, all Meta
ciks=0001297996 (DLR)        →   687 hits, all Digital Realty
ciks=0001297996,0001101239   → 1,218 hits  (687 + 531, sums exactly)
ciks=0001297996&forms=10-K   →    36 hits
```

Comma-separates fine; space-separated returns 0. `entityName` works as an
alternative. **`sics` is silently ignored** — 6798, 6770 and no filter all
return an identical 4,868 — but that no longer matters.

Querying per-CIK gives 100% precision by construction. The micro-cap and shell
noise (`CalEthos`, `Jet.AI`, `Nixxy`, `Blue Acquisition Corp`) that dominates
bare phrase search is simply never in the result set.

**Document retrieval**, verified — both parts come from the hit's `_id`
(`0001326801-23-000067:meta-20230331.htm`):

```
https://www.sec.gov/Archives/edgar/data/{cik-unpadded}/{adsh-without-dashes}/{filename}
```

**The real engineering problem is size.** A Meta 10-Q is 2,119,975 bytes of
HTML → **368,972 characters** of plain text, against our 24,000-character LLM
input cap. That is 15× over, for the form type an agent report called "8–15 KB,
fits". Section extraction is mandatory for every form, not just 10-Ks.

The content justifies the work. First `data center` hit in that filing:

> "…a pivot towards a next generation data center design, including cancellation
> of multiple data center projects…"

That is a `cancelled` phase transition with a citation — a fact type nothing
else in the pipeline currently supplies.

**XBRL capex bridge:** `data.sec.gov/api/xbrl/companyconcept/CIK{padded}/us-gaap/
PaymentsToAcquirePropertyPlantAndEquipment.json`. Verified for Meta. **10-Q
values are year-to-date cumulative, not quarterly** (2025: Q1 $12.9B → Q2 $29.5B
→ Q3 $48.3B) and must be differenced, resetting at the fiscal-year boundary.

### 1d. Feeds and sitemaps verified working

**Operator / neocloud newsrooms** — `company_filing` weight 3, the highest-value
source type. Only 7 are configured today. Verified, with live match counts:

| Company | Endpoint | urls | match |
|---|---|---|---|
| OpenAI | `openai.com/blog/rss.xml` | 1,060 | 95 |
| Galaxy Digital | `galaxy.com/sitemap.xml` | 1,194 | 71 |
| Applied Digital | `applieddigital.com/sitemap.xml` | 94 | 45 |
| Anthropic | `anthropic.com/sitemap.xml` | 508 | 39 |
| Compass Datacenters | `compassdatacenters.com/news-sitemap.xml` | 105 | 36 |
| Fermi America | `fermiamerica.com/post-sitemap.xml` | 61 | 33 |
| H5 Data Centers | `h5datacenters.com/sitemap.xml` | 182 | 27 |
| Quantum Loophole | `quantumloophole.com/sitemap.xml` | 114 | 20 |
| Lambda | `lambda.ai/sitemap.xml` | 327 | 19 |
| TeraWulf | `terawulf.com/sitemap.xml` | 217 | 18 |
| Novva | `novva.com/post-sitemap.xml` | 48 | 14 |
| CoreWeave | `coreweave.com/blog/rss.xml` | 100 | 13 |
| QTS | `qtsdatacenters.com/news/feed/` | 9 | 5 |
| Yondr | `yondrgroup.com/post-sitemap.xml` | 97 | 5 |

Blocked (403): Equinix, Stream, Flexential, IREN, xAI, Oracle, Apple. Iron
Mountain 429. Not finished testing: Vantage, Aligned, STACK, EdgeConneX,
Switch, CloudHQ, PowerHouse, Tract, Corscale, T5 — the sweep timed out.

**Trade press and EPC:**

| Source | urls | match |
|---|---|---|
| `bisnow.com/rss/data-center` | 30 | 11 |
| `turnerconstruction.com/sitemap.xml` | 2,183 | 12 |
| `bisnow.com/rss/national` | 30 | 8 |
| `constructiondive.com/feeds/news/` | 10 | 1 |
| `semianalysis.com/feed/` | 10 | 1 |
| `jobsohio.com/sitemap.xml` | 337 | 1 |
| `news.duke-energy.com/sitemap.xml` | 2,959 | 1 |

**State nonprofit newsrooms.** All 21 fetch and parse (100 entries each for the
States Newsroom network). Only 3 match on a single poll — but the matches are
precisely the obstacle coverage we lack:

```
virginiamercury.com/feed/      100 entries, 6 match
  "Brown water, dust and loud noises: Louisa homeowner sues Amazon over data center"
  "Loudoun supervisors mull data center moratorium"
nevadacurrent.com/feed/        100 entries, 3 match
  "Henderson City Council rejects data center moratorium"
texastribune.org/feeds/main/    20 entries, 1 match
  "Religion motivates data center opponents in Texas"
```

The other 18 (Ohio Capital Journal, Georgia Recorder, Iowa Capital Dispatch,
Nebraska Examiner, Wisconsin Examiner, Louisiana Illuminator, Oregon Capital
Chronicle, Utah News Dispatch, Indiana Capital Chronicle, Michigan Advance,
Missouri Independent, NC Newsline, Arizona Mirror, Mississippi Today, Maryland
Matters — **`marylandmatters.org`**, not the wrong domain an agent reported —
Cardinal News, MLK50, Rough Draft Atlanta) return 0 today. Add them anyway:
each poll is one HTTP request with no LLM cost until something matches, and
they are all WordPress, so `/sitemap.xml` gives a deep archive for backfill.

### 1e. Verified dead ends

Recording these so nobody spends a day rediscovering them.

- **No utility or RTO publishes a machine-readable large-load queue.** ERCOT's
  ~438 GW queue exists but per-project Load Information Forms are confidential
  submissions; only aggregates are public. Same for PJM.
- **Government and utility *websites* carry almost no per-project records.**
  Raw substring counts over whole sitemaps: `loudoun.gov` 2,636 URLs → 5 hits
  (all static policy pages); `maricopa.gov` 3,127 → 0; `gov.georgia.gov` 1,723
  → 0; `southerncompany.com` 625 → 0; `governor.iowa.gov` 1,590 → 0.
  `georgiapower.com`'s 15 hits are transmission project pages, not news.
- **Every state PUC docket system is an HTML form.** None exposes an API. Agent
  reports claiming these are "queryable" verified only that the *page* loads,
  not that a parameterised query returns data. Treat as unproven.
- **Bot-blocked (403/202/429):** `ferc.gov`, `epa.gov`, `insidelines.pjm.com`,
  `news.dominionenergy.com`, `entergy.com`, `henrico.gov`, `pwcva.gov` (the www
  site; the GIS host works), `wedc.org`, `vedp.org`, `georgia.org`,
  `rtoinsider.com`, MISO, Missouri PSC. Lee Enterprises papers (Loudoun Times,
  Prince William Times, InsideNoVa, Journal Times, Omaha.com) return 429;
  Gannett and the Business Journals 403/404.
- **Commercial datasets** (Baxtel, DataCenterMap, Cloudscene, DC Byte, Synergy)
  are all paid or authenticated. OpenStreetMap is free (ODbL) but carries no
  MW / tenant / status fields.
- **No public Tyler EnerGov API** — see Part 4.
- **EPA ECHO endpoints returned 404/403** in testing. FERC eLibrary, FAA OE/AAA
  form-only; USACE 403.

### 1f. The filter blocker

Even once a government page is fetchable, the two-tier keyword filter drops it.
A governor's release is slugged
`gov-kemp-announces-2-billion-investment-social-circle` — it names the company
and county and never says "data center", so the `topic` tier fails. Adding
government feeds without fixing this yields nothing.

---

## Part 2: plan for problem 1 (sources)

### Phase A — config only, ~half a day

Add the verified 1d sources to `seed/feeds.toml`: 14 operator newsrooms, Bisnow,
Turner, Construction Dive, SemiAnalysis, JobsOhio, Duke, and all 21 state
newsrooms.

Success measure: `datacenterfrontier`'s share of editorial citations drops below
50%, and projects with ≥2 independent domains rises from 15.

### Phase B — `tracker ingest edgar`, the highest-leverage build

New module + subcommand, two modes: discovery (CIK-scoped phrase queries across
~17 whitelisted companies) and enrichment (per tracked project, called as a
seventh harvester in `enrich.py`). No schema change, no key; `sec.gov` already
classifies as `company_filing` weight 3.

**Prototype the section extractor first** — locating the right ~20k window
inside a 370k-character filing is the only unsolved piece. Everything else about
this path is now settled.

### Phase C — one county adapter, Prince William

Build for the county that actually publishes good data, and treat it as the
proof of concept rather than assuming it generalises. GFA stays its own field.
Use the ArcGIS Online search API to shortlist other jurisdictions, and assess
each individually — 1b shows the quality range is enormous.

### Phase D — give discovery the escalation the article path already has

`fetch.should_escalate` and `Crawl4AIFetcher` exist and are used by `ingest
crawl`; `discover._RawFetcher` has no equivalent, so every 403 in 1e is a hard
failure at discovery time. Keep the existing policy: escalate only where
`robots.txt` permits, leave deliberate blocks discovery-only as
DataCenterDynamics already is.

**Open question for the operator:** this makes the `[crawl]` extra (Chromium +
~33 transitive deps) a routine dependency for discovery, which it deliberately
is not today. Needs a decision.

### Phase E — the `government = true` filter mode

For a feed so flagged, satisfy the `topic` tier via a known project's company or
locality instead of the phrase "data center", reusing
`discover.matches_known_project` and `project_identities`. Without this, Phase D
unlocks fetching and still yields nothing.

---

## Part 3: plan for problem 2 (the customer axis)

The database is keyed on the *site* — `(operator, locality, state)`. The question
being asked is on the **end-customer × time** axis. Much hyperscaler capacity is
built by colo developers (STACK, Vantage, QTS, Aligned, Crusoe) and leased, so
the two grains do not coincide.

### Step 1 — customer identity (small, do first)

- `customer_key()` in `dedup.py` mirroring `company_key()`, with a
  `CUSTOMER_ALIASES` table. Fixes Meta/Facebook as two operators; folds AWS.
- Anonymised tenants are not identities. `"Fortune 100 technology company"` and
  `"Publicly-traded global enterprise (technology company based in the San
  Francisco Bay Area)"` should normalise to an explicit `undisclosed` marker.
  That is 4 of the 12 populated values.
- Flag for review any customer whose `customer_key` matches a known operator's
  `company_key` — catches the `Aligned Data Centers` misfiling.

### Step 2 — attribution as a table (migration 0008)

One `customer` string cannot express a multi-tenant campus, and partial leasing
is the norm. New table `(project_id, customer, mw, share, basis, source_id)`
with `basis ∈ {stated, lease_filing, inferred}`.

Keep `project.customer` as a **derived** column — largest-share stated
attribution. This is exactly the pattern migration 0004 established for
`blocker`/`risk`, so the 12 PRD fields, the confidence metric and the export
shape stay unchanged, on a precedent that is already tested.

### Step 3 — inferred tenant, using machinery that exists

Tenant attribution is often circumstantial. `infer.py` is already the enforced
home for judgements: an `inferred:` source that `confidence` ignores and
`gaps.basis()` reports distinctly. Add `customer` to `INFERABLE` — the one
non-quantitative field that belongs there.

**Do not add `mw_planned`, `investment_usd` or `expected_online`.** The boundary
that module enforces in code is why any of this is trustworthy, and those are
关键数字 that must come from a document.

### Step 4 — `tracker capex`

Rows = customer. Columns = MW planned, MW built, MW by expected-online quarter,
disclosed $, open-risk exposure, count of slipped projects.

Call what exists rather than reimplementing: `tracks.standing()` for real
position, the `delayed` events `upsert._record_slippage` already writes for slip,
`exposure` for risk rollup, `gaps.basis()` to mark each cell reported / 待确认 /
inferred. Show the EDGAR XBRL capex series beside it as a clearly labelled
reference row — the reconciliation gap is the interesting output.

### Step 5 — the time axis gates everything above

`expected_online` at 29/124 with 17 already past means the quarterly view is
mostly empty whatever we build. Phase B is the best new supply: filings state
in-service dates that trade press drops. Prince William's `OCCDate` is a second.

---

## Part 4: corrections to the subagent reports

The `research-*.md` files in the session scratchpad contain these errors. Listed
so they are not propagated.

| Claim | Reality |
|---|---|
| Tyler EnerGov is a unified permit API; `energov.com/api/v1`, `pwc.energov.com`, `loudoun.energov.com` all "HTTP 200" | All three fail with `ConnectError` — the hosts do not resolve. No public EnerGov API was established. `EnergovID` is an internal key |
| EDGAR `ciks` filtering "completely broken, all scopes return 0 hits" | Works correctly with 10-digit zero-padding. The agent used unpadded CIKs |
| Therefore use phrase search + post-hoc whitelist, "70–90% precision" | Unnecessary. CIK-scoped queries are 100% precise by construction |
| 10-Q documents are "8–15 KB, fits the 24KB limit" | A Meta 10-Q is 2.1 MB HTML → 368,972 chars of text, 15× the cap |
| Document URL `sec.gov/Archives/{cik}/{adsh}/{file}` | Wrong. Needs `/Archives/edgar/data/{cik-unpadded}/{adsh-no-dashes}/{file}` |
| Equinix CIK `0000109357` | Wrong; contributes 0 hits. Correct is `0001101239` |
| Digital Realty CIK `0001393110` | Wrong. Correct is `0001297996` |
| EIA `electricity/rto/region-data` returns "facility name, capacity_mw, fuel_type, year_installed" | That endpoint is regional hourly demand. The example also contains a literal `api_key=YOUR_KEY`, so nothing was run |
| Census CBP endpoint `api.census.gov/data/timeseries/econ/cbo/county` | Also carries `key=YOUR_KEY`; unverified, and `cbo` is not the CBP dataset |
| Loudoun `Data_Center_Building_Outlines` ranked the top ArcGIS source | Names are redacted, fields are map graphics. Near-useless |
| Maryland Matters feed at `mattersmedia.org/feed/` | Wrong domain. Real feed is `marylandmatters.org/feed/` (100 entries) |
| State PUC dockets "machine-readable, queryable" | Only the pages were fetched. No parameterised query was shown returning data |

---

## Proposed sequence

1. **Phase A** — config, half a day. Immediate breadth, measurable.
2. **Step 1 + Step 5 measurement** — small; tells us how far off the capex view is.
3. **Phase B (EDGAR)** — the big one. Serves both problems. Prototype the section
   extractor first.
4. **Phase C** — the Prince William adapter.
5. **Phases D/E** — unlock blocked sources; needs the `[crawl]` dependency decision.
6. **Steps 2–4** — the customer axis proper, once EDGAR supplies tenant and timing facts.

## Risks and things deliberately not proposed

- **Do not loosen the evidence gate** to raise coverage. The numbers are
  trustworthy because it is strict; thin coverage is fixed with more sources.
- **Do not let `infer` touch quantitative fields**, however tempting for the
  capex view.
- **Do not defeat access controls.** The 403 list stays subject to existing policy.
- **GFA is not MW.** Square footage is a size proxy; converting it is an estimate
  and must never land in `mw_planned`.
- **Prince William is not representative.** Budget per-jurisdiction assessment,
  not a generic county adapter.
