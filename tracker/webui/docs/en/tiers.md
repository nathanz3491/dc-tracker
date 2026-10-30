# Quality tiers

Alongside [confidence](confidence), the tracker grades each campus on a ladder of **quality tiers**. Confidence asks *how well backed* a row is; tiers ask *how finished* it is. The tracker uses them to decide what to work on each night and to report progress — "rows at T2 or above" is how its own update reports measure it — so you may meet them in release notes and summaries. The console does not print a tier on each row.

The tiers stack: a campus is at a tier only if it passes every check of that tier and of every tier below it.

| Tier | Name | The campus… |
| --- | --- | --- |
| {{tier:—}} | not sourced | fails one of the T0 checks. |
| {{tier:T0}} | **Sourced** | has at least one real source, and nothing on it contradicts itself. |
| {{tier:T1}} | **Sound** | has no figure that cannot be true. Nothing it adds to a total is a lie. |
| {{tier:T2}} | **Complete** | has the fields a reader acts on, each backed — or searched for and found to be unpublished. |
| {{tier:T3}} | **Settled** | has every open question answered. |

## T0 — Sourced

- At least one source that is not just reference data such as a Census lookup.
- No value presented as fact without a sentence recorded for it.
- No impossible combination left open — for example more capacity running than planned.

## T1 — Sound

T1 is the tracker's minimum bar, because it publishes sums. A campus is sound when:

- **no figure fails a plausibility check**: the same figure stored in two units (a thousand times apart), more than 8,000 MW on one site, a building larger than its campus, a gigawatt claim with no quote, or an investment outside $0.3 million–$60 million per megawatt;
- **no value was chosen by the order articles were read** where publication order would choose differently;
- **any suspected duplicate has been answered** — merged, or ruled a different site. See [Duplicates](duplicates).

## T2 — Complete

- The measurable fields are filled, or do not apply (running capacity before anything is built counts as filled). Tenant and biggest obstacle are not required: empty is often right for both.
- A field that has been **searched for twice with nothing found** counts as answered, and is listed as *published nowhere* rather than missing. It reopens automatically when the campus gains a new source.
- No value is presented as confirmed without a quote. 待确认 values are allowed: the row is honest about them.

So T2 means "complete as far as anything has been published", with the unknowns still listed.

## T3 — Settled

- No open warnings, such as an expected online date long past on a campus still under construction.
- No tranches that may be one building counted twice.
- Every open obstacle backed by a sentence.
- Every source read by the current version of the tracker's reading instructions, so nothing rests on an older, looser reading.
- No **candidate twin** left unjudged: another campus in the same county that states the same figure or shares a distinctive name word. Until the nightly duplicate check rules on the pair, both campuses still count in every total — it is a question, not an error.

## Why a campus stays low

Most campuses below T2 are missing a basic defining fact, or have a field nobody has published yet, or rest on a source read before the checks were tightened. The nightly update works through them — see [How the data is kept current](how-it-is-kept-current).
