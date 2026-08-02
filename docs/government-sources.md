# Government sources: what was tested, and why none of it shipped

The review asked for government data — permits, approvals — and asked for the
research first: 可以试着先研究还有哪些潜在数据源. This is that research.

**Result: four routes tested, four failures.** Nothing was built, because every
uniform machine-readable route either does not cover the jurisdictions where the
capacity is, or returns the wrong kind of document. Building one would have added
a source class that yields nothing while making the source-mix look like
government coverage exists.

Reproduce any of this with:

```bash
python scripts/probe_government_sources.py
```

Measured 2 August 2026, aimed at the markets holding the tracked capacity:
TX 34%, CO 12%, NM 9%, OH 8%, GA 8%, VA 8%.

---

## 1. Municipal open-data portals (Socrata)

The most promising on paper. ~460 portals publish building permits through one
identical API, JSON, no key needed. Full-text search works.

**Why it fails: the jurisdictions that matter are not on it.**

| search | datasets found |
| --- | --- |
| Loudoun permits | **0** |
| Prince William permits | **0** |
| Virginia building permits | **0** |
| Arizona building permits | **0** |
| Texas building permits | 16 (Austin, state portal) |
| Georgia building permits | 1 (Fulton County — Atlanta, not the data-center counties) |

Loudoun County is the largest data-center market on earth and publishes nothing
here. Ohio returns Cincinnati, not Columbus or New Albany.

**And the hits that do exist are the wrong size.** Searching Dallas for
"data center" returns:

```
$   631,668  ACCESS CONTROL FOR DATA CENTERS
$ 2,250,000  DATA CENTER ELECTRICAL INFRASTRUCTURE UPGRADE
$   495,000  DATA CENTER - INSTALL 2 PUMPS & HEAT EXCHANGER
```

Maintenance work on existing server rooms. A 1 GW campus does not appear as a
$631k permit in a city portal.

## 2. FERC eLibrary and state utility commissions

Where interconnection agreements and large-load filings actually live. This is the
right *content*.

**Why it fails: no machine-readable interface.**

| | |
| --- | --- |
| FERC eLibrary API | 404 on every documented path tried |
| Ohio PUCO DIS | `text/html` |
| Virginia SCC docket search | `text/html` |
| Texas PUC Interchange | `text/html` |

All three commissions are ASP.NET WebForms — session state, viewstate, POST-only
search. That is four bespoke scrapers against four sites that change, which is the
same reason `ingest pjm` takes a downloaded file rather than fetching one.

## 3. County and agency news feeds

Six government RSS feeds respond, including the two places that should matter most.
Scored with **this project's own discovery filter**, not a guess:

| feed | kept | of |
| --- | ---: | ---: |
| loudoun-county-va | 0 | 58 |
| abilene-tx | 0 | 50 |
| new-albany-oh | 0 | 10 |
| maricopa-county-az | 0 | 25 |
| energy.gov | 0 | 10 |
| eia.gov Today in Energy | 0 | 24 |
| **total** | **0** | **177** |

Abilene's own city news feed carries nothing about Stargate Abilene while it is
being built there. County press offices write about road closures and library
hours; data-center approvals go through planning commissions, and those publish
agenda PDFs, not press releases.

## 4. Legistar (planning-commission agendas)

The right *venue* — rezonings and special-exception applications are exactly what
a planning commission votes on — and a uniform JSON API across many jurisdictions.

**Why it fails: same coverage gap, plus the wrong matters.**

Loudoun, Prince William and Abilene all return HTTP 500 (not clients). Phoenix,
Columbus and San Antonio respond, and their "data center" matters are municipal IT
procurement from 2005–2020: a cooling-maintenance contract, a city lease. Not
hyperscaler campus approvals.

---

## What to do instead

**The government layer that pays is already built and has not been run.** Utilities
file their large-load commitments and interconnection agreements with the SEC —
a legally mandated disclosure served from a government host with a documented rate
limit rather than a bot filter. Fourteen utilities are configured, searched with
their own vocabulary ("large load", "interconnection agreement"):

```bash
tracker ingest edgar --kind utility --per-company 1
```

Verified against the live API: 19 filings found, 17 prepared. Roughly 17 LLM calls.

**One-off government documents already work.** The crawl path reads any URL, and
`classify_source_type` maps `.gov` and `.mil` to `government_doc` automatically:

```bash
tracker ingest crawl --url https://www.loudoun.gov/.../rezoning-application
```

So reading a specific permit, staff report or docket filing is supported today.
What does not exist is **bulk discovery** of them, and that is the thing four
routes failed to provide.

**If someone wants to push further**, the tractable order is:

1. **Virginia SCC and Georgia PSC dockets**, scraped as a deliberate exception —
   two portals, not forty, in the two states holding 16% of tracked capacity.
   Worth it only if utility SEC filings turn out to be too coarse.
2. **Loudoun and Prince William planning-commission agendas**, which are PDFs on a
   predictable URL pattern. The crawl path can already read a PDF's text; what is
   missing is enumerating the agendas.
3. Not county building permits. Route 1 shows what those contain.
