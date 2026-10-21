# Numbers and dates

The same-looking number can mean different things. This page explains what the tracker's megawatts, dollars and dates are, and the marks that say when one is not what it seems.

## Three kinds of megawatt

A data center's "megawatts" can be any of three quantities, which routinely differ by 30–40% and occasionally by ten times:

| Kind | What it measures | Mark |
| --- | --- | --- |
| **IT load** | The power the computing equipment itself uses. This is what *planned capacity* is meant to be. | none |
| **Facility power** | The whole site's draw: IT load plus cooling and everything else. Usually 10–30% above IT load. | ◒ |
| **Nameplate** | A power plant's rated output. Not a data-center quantity at all. | ⚡ |

Most articles write "a 200 MW campus" without saying which they mean, so an unmarked figure is either IT load or not stated — and "not stated" is the usual case. A figure marked ◒ or ⚡ is **not comparable** with an unmarked one. On a [project's page](project-page) the summary names the kind under the planned and running figures when the source gave one: *IT capacity*, *Facility power* or *Generator rating*.

## One building or the whole campus

Articles often give one building's capacity or cost: "the 36 MW first phase". The tracker detects this and keeps a building's figure out of the campus total whenever a figure for the whole campus exists. With none, the building's figure fills the gap and is marked as covering *one building*.

## Planned and running capacity

- **Planned** is the capacity the campus is meant to have when finished, from the best available source.
- **Running** is the capacity switched on today. It takes the **largest** figure any source gives, because running capacity only grows. It is *n/a* until something is running.
- **Running is never more than planned.** When the two disagree, one of them is about something else, and the better-supported figure — quoted, from a source that reports rather than a directory — decides which. If the running figure is at least as well supported, the plan described one phase: the smallest stated figure that covers what runs is used instead, or the running figure itself when no source states one. If the plan is better supported, the running figure describes a bigger site and gives way, to a smaller running figure that fits or to *n/a*. A plan stated as a minimum ("more than 20 MW") is never contradicted by more running.
- Units are converted: kilowatts and gigawatts become megawatts. A range such as "500–700 MW" is stored as its lower end, with a note.
- **Kilowatts read as megawatts are set aside.** A figure of a gigawatt or more, about a thousand times a figure a quoted source gives for the same campus, and stated by nothing quoted itself, is almost always a kilowatt figure misread. It stops counting and the row's notes say which source it came from. A campus whose own quote says 1,200 MW is never affected by a small pilot figure beside it.

These rules run every time a row is worked out from its sources, so the list, the project page, the company page and the totals all show the same two numbers.

## Investment

Investment is stored in US dollars, a range as its lower end. Three kinds of figure are kept out of a campus's investment, because they measure something else:

- a **programme total** quoted in an article about one site ("part of a $500 billion buildout"). Anything above $50 million per megawatt is treated as this and marked **not this site's figure**;
- a **regional** economic-impact figure;
- an operator's **whole portfolio**.

One campus can carry several real figures for different things — an initial phase, a revised plan, a regional total — so check the sentence before comparing.

## Compute

**Compute (H200-equivalent)** restates capacity as a number of NVIDIA H200 accelerators, at about 770 per megawatt (1.3 kW of facility power each). It uses running capacity if any, otherwise planned. It is a unit conversion, labelled *derived*, and only as good as the megawatts behind it. A chip count stated outright by a source is used instead.

## Bounds

| Mark | Meaning |
| --- | --- |
| **~5,000** | approximate: "about", "roughly", "an estimated" |
| **≤5,000** | at most |
| **5,000+** | at least: "more than", "over", "exceeds" |

## Dates and their precision

A date is kept at the precision its source gave:

| The source said | Kept as | Shown in tables as |
| --- | --- | --- |
| "on March 14, 2027" | 14 March 2027, to the day | 2027-03-14 |
| "in March 2027" | March 2027, to the month | 2027-03 |
| "Q3 2027", "late 2027" | the quarter starting July or October 2027 | 2027 |
| "the second half of 2026" | the half-year starting July 2026 | 2026 |
| "in 2028", "by 2028" | the year 2028 | 2028 |
| "next spring", "soon", "before 2028" | nothing: no usable date | — |

> [!TIP]
> A date shown as just a year means the source gave only a year. It does not mean 1 January. In tables, quarter and half-year dates are shortened to the year as well; hover for the full statement.

A promise of a period is not overdue until the period is over: a campus "expected online in 2026" is not late until 2027.

Two dates have their own rules:

- **Announced** takes the **earliest** date any source gives.
- **Online** takes the best source's date. When it moves later, a *delayed* milestone is recorded; the number of days is attached only when the date crosses into a later year, so a vague date being made precise is not mistaken for a slip.

[Capex](capex) groups arrivals by year or quarter. Because so many sources give only a year, read its quarter view as a shape, not a schedule.

## Suspected delays

A campus is flagged **Suspected delay** when all four of these hold:

- it has an expected online date, and the whole period that date names is over — "Q3 2025" became late on 1 October 2025, "2025" on 1 January 2026. A date stored on the first of a month with no precision recorded is read as that month, or as the whole year when it is 1 January, so a vague date is never called late early;
- its phase is not operational, and it has not been paused or cancelled — a stopped campus has a status of its own, which says more than "late";
- nothing says it has gone live: no capacity is recorded as running, no building or phase is running, and no energisation was reported on or after the expected date (one that was only *expected* does not count);
- no building or phase is still due later. When the campus date was the first phase's and the next phase has its own later date, the campus is not late.

It is a suspicion, not a finding. The usual causes are a slip nobody reported and a campus that opened without news we read, and either is worth a search. The flag gives the date as the source stated it and how many days ago its period ended, and it is worked out again every day. One rule is behind the chip on a [project's page](project-page), the **Late** column and the **Suspected delays only** filter in [Projects](projects), the list on a [company page](companies), the [API](api-reference) and the contradiction check the maintainers run, so they always agree.

A building or phase is marked **was due** on the same terms: its date's period is over, it is not running, no energisation date is recorded for it, and it is not paused or cancelled. A building or phase records no precision of its own, so its date is read the way an unrecorded one is above.
