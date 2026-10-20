# Company pages

Each company has a page at `/companies/<name>`, with every campus it builds or uses. You reach it from a company name on a [project's page](project-page), on a [Monitor](monitor) card, or from a watchlist entry.

## The header

- The company's logo (its own website icon) or its initials, its name, and its website.
- A summary such as *15 campuses · builds 12 · tenant on 3 · in 7 states · 4 open obstacles*.
- **☆ Watch company** adds the company to your watchlist, which covers every campus where it is operator, developer or tenant. **★ Watching** removes it again.

## The figures

The four cards are all in megawatts, because a company's size is its capacity, not its number of campuses (the count is in the summary above).

| Card | Meaning |
| --- | --- |
| **planned** | Planned capacity, counted the way the [Capex](capex) page counts it. |
| **running** | Capacity in service, counted the same way. |
| **under construction** | Capacity not yet running on campuses being built. |
| **due in the next three years** | Capacity not yet running whose campus is due this year or in the next two — see *Supply* below. When some of it is at high delay risk, the card says how much. |

The line under the cards says how the capacity figures are counted. They follow the [Capex](capex) page's rules:

- a campus suspected of being stored twice counts once (see [Duplicates](duplicates));
- paused and cancelled campuses count nothing;
- a tenant of some of a campus's buildings is credited with those buildings, not the whole campus.

So **planned** is the company's own row on the Capex page, plus the campuses it builds that name no buyer — Capex lists those under *(no named customer)*. The line gives both parts. A campus it builds for a named tenant counts for that tenant, as on Capex. Every megawatt is therefore on exactly one company's page, and the company pages add up to the Capex total. That total can be slightly higher than the [Map](map)'s, for the reason given there.

## Campuses

A table of every campus, with the company's role on each — **builds**, **tenant**, or **tenant (tranche)** when it uses only some buildings — and who builds it when that is someone else. Then the phase, planned capacity with a bar, capacity running, and open obstacles. Click a row or press Enter to open the campus.

When a campus does not count in full towards the figures above, a short note beside the role says why:

| Note | Meaning |
| --- | --- |
| *not counted: cancelled* (or *paused*) | Left out, as on Capex. |
| *not counted: #12 is the same campus* | Suspected to be a second row for campus #12, which is counted instead. |
| *counted for …* | Capex credits this campus to another buyer, usually the tenant the company builds it for. |
| *not counted: one of several buyers named …* | The campus's sources name several buyers and say nothing about how it divides between them, so none is credited with it. See [Capex](capex). |
| *200 MW counted here, the rest for …* | The company is credited with part of the campus — its own buildings, or what is left after other tenants' buildings. |

Beside the table, **Where** shows the states the campuses are in, and **Stage, in megawatts** splits the planned figure by how far along it is: running; under construction; planned or permitting; and still to come at campuses already operating — an expansion, or capacity nothing yet dates. The four parts add up to **planned**.

## Supply, the next three years

Capacity the company is credited with that is not yet running, by the year each campus is due — this year and the next two — as a bar per year, then a table of the campuses with their share of it and their **delay risk**.

- A campus whose stated online period has passed with nothing running is **overdue**: still expected, and nobody has said when. Its capacity is shown under this year, in amber. Overdue means exactly a suspected delay (below).
- Capacity due later, and capacity with no date, are given in one line under the bars and are not in the figures.
- **Delay risk** is *high*, *elevated* or *low*, with the evidence it rests on, each something you can check on the campus's page:
  - *high* — a suspected delay; the online date has slipped twice or more; or an open obstacle a source says has stopped work;
  - *elevated* — one slip; an open obstacle a source says moved the schedule or scope; or due within a year with no construction reported.
  Only obstacles with a quoted sentence count, and a date restated more precisely inside the same year is not a slip.
- When one campus is **a fifth or more** of the three years' supply and is at elevated or high delay risk, a line above the bars names it: *Conesville is 38% of this supply (600 of 1,596 MW), and is at high delay risk: …*

A tenant's share uses the campus's own date; a tranche's separate date is not yet read here.

## Suspected delays

When any of the company's campuses is a suspected delay — its own online date has passed and nothing reported since says it is running — a **Suspected delays** section lists them, most overdue first: the campus, its expected online date as the source stated it, how many days late it is, and its phase. The heading counts them and says how much planned capacity they hold between them. Hover a row for the reason; click it to open the campus. The section is left out when there are none. [Numbers and dates](numbers-and-dates) gives the rule.

## Recent news

**Reported in the last two months** lists the updates about these campuses, the same cards as on [Updates](updates).

## How names are matched

The page gathers campuses by the company's name across all the roles a source can give it. Spelling variants are folded together (Meta and Facebook, AWS and Amazon, RagingWire and NTT), and two genuinely different companies with similar names stay apart. A campus the Capex page credits to the company because a source names it the customer, even though the campus is filed under another company's name, is listed too.

The company is shown under **one name**, here and on every other page of the console: the name on the tracker's list of operators when it is on it, otherwise the spelling most campuses use. A campus's own page also shows the spelling its sources use when that differs.

If a campus you expect is missing, its source may have named a subsidiary or a joint venture instead.
