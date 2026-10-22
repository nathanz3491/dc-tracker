# Field dictionary

Every field a campus has, what it means, and how the tracker chooses its value when sources disagree. Column names are as they appear in [Projects](projects); the name in `code` is the field's internal name, used on a [project's page](project-page).

Before a field's own rule applies, every field follows the same order: a ruled-out claim no longer counts, a quoted claim beats a 待确认 one, a whole-campus figure beats one building's, and a first-hand source beats a directory. See [Where a value came from](provenance).

## The twelve tracked fields

| Column | Field | Meaning | When sources disagree |
| --- | --- | --- | --- |
| **Project** | `name` | What the campus is called. | The first name seen is kept; later names do not replace it. |
| **Operator** | `company` | The company building and running it. | The first seen is kept. Other companies involved are listed under *Who is involved*. |
| **Buyer** | `customer` | The tenant that will occupy it and use the compute. | The strongest, latest source. If none names one, it may come from a company named as customer or from the tenant of its largest building, labelled *derived*. |
| **City** | `city` | The town it is filed under. | First seen. |
| **State** | `state` | Two-letter US state. | First seen. |
| **Phase** | `phase` | How far along it is. See [Phases and progress](progress). | A stop (paused, cancelled) from a credible source wins; otherwise the furthest-along phase a quoted source supports — and never below a milestone the campus has reached: an approved permit means at least permitting, a groundbreaking at least construction. |
| **Planned (MW)** | `mw_planned` | Planned capacity, meant as computing (IT) load, in megawatts. | The strongest, latest source. A building's figure only when nothing covers the whole campus. |
| **Running (MW)** | `mw_built` | Capacity switched on today. | The **largest** figure any source gives. *n/a* until something is running. |
| **Investment (USD)** | `investment_usd` | Announced investment in the campus, in US dollars. | The strongest, latest source. Programme, regional and portfolio totals are kept out. |
| **Announced** | `first_announced` | When the campus was first made public. | The **earliest** date any source gives. |
| **Online** | `expected_online` | When it is expected to come online. | The strongest, latest source. A later date records a *delayed* milestone. |
| **Biggest obstacle** | `blocker` | The most serious open obstacle, in one sentence. | Worked out from the obstacles: quoted before unquoted, then most severe. Empty when nothing is open. |

## Other fields

| Column | Field | Meaning |
| --- | --- | --- |
| **County** | `county` | The county. Filled from Census data where a town lies in only one county. |
| **Lat**, **Lon** | `lat`, `lon` | The **centre of the town or county**, from Census data — not the site. |
| **Compute (H200-eq)** | `h200_equivalent` | Capacity restated as NVIDIA H200 accelerators, about 770 per MW. A stated chip count wins. *Derived.* |
| **Confidence (0-3)** | `confidence` | How well backed the row is. See [Confidence](confidence). |
| **Verified** | `last_verified_at` | When a person last checked and signed the row off. |
| — | `country` | Always US. |
| — | `notes` | Decisions and disclosures: figures ruled out, merges, fields searched for and not found. |

## About each figure

Each claim behind a value can carry qualifiers, shown in the value's popover when they are not the ordinary case:

| Qualifier | Values | Meaning |
| --- | --- | --- |
| **bound** | exact, approximate (~), at least (+), at most (≤) | How precisely the source stated the number. |
| **scope** | this site, programme, region, portfolio, one building, a named phase | What the figure covers. |
| **modality** | speculated, targeted, planned, contracted, achieved | How firmly the source stated it. |
| **as of** | a date | When the figure was true, if not the article's date. |
| **basis** | IT load, facility (◒), nameplate (⚡), not stated | For megawatts only: which quantity. See [Numbers and dates](numbers-and-dates). |
| **precision** | day, month, quarter, half, year | For dates only: how precisely the source gave it. |

## Who is involved

Companies other than the operator are recorded as **parties**, each with a role: developer, owner, operator, customer, utility or contractor, and the sentence that names them.

## Tranches

A tranche is one building, phase or hall of a campus, with its own name, capacity, state, tenant, dates and investment. Its state is one of: planned, permitting, under construction, shell complete (built, no power), energized (power on), serving (a customer is running on it), paused or cancelled. See [A project's page](project-page).

## Milestones and obstacles

Milestones are dated events on the five progress tracks: announced, land acquired, permit filed, permit approved, interconnection agreement, site work, groundbreaking, equipment installed, energized, first customer — plus *delayed* and *expanded*. Obstacles have a kind, a severity and a status. See [Phases and progress](progress).
