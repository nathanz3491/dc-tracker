# Company pages

Each company has a page at `/companies/<name>`, with every campus it builds or uses. You reach it from a company name on a [project's page](project-page), on a [Monitor](monitor) card, or from a watchlist entry.

## The header

- The company's logo (its own website icon) or its initials, its name, and its website.
- A summary such as *builds 12 · tenant on 3 · in 7 states*.
- **☆ Watch company** adds the company to your watchlist, which covers every campus where it is operator, developer or tenant. **★ Watching** removes it again.

## The figures

| Card | Meaning |
| --- | --- |
| **campuses** | How many, split into built by the company and used by it as a tenant. Every campus in the table below counts here, whether or not it counts in the capacity figures. |
| **planned** | Planned capacity, counted the way the [Capex](capex) page counts it. |
| **running** | Capacity in service, counted the same way. |
| **open obstacles** | Open obstacles on these campuses, however long ago they were reported. |

The line under the cards says how the two capacity figures are counted. They follow the [Capex](capex) page's rules:

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
| *200 MW counted here, the rest for …* | The company is credited with part of the campus — its own buildings, or what is left after other tenants' buildings. |

Beside the table, **Where** shows the states the campuses are in and **Stage** shows how many are at each phase.

## Recent news

**Reported in the last two months** lists the updates about these campuses, the same cards as on [Updates](updates).

## How names are matched

The page gathers campuses by the company's name across all the roles a source can give it. Spelling variants are folded together (Meta and Facebook, AWS and Amazon, RagingWire and NTT), and two genuinely different companies with similar names stay apart. A campus the Capex page credits to the company because a source names it the customer, even though the campus is filed under another company's name, is listed too.

The company is shown under **one name**, here and on every other page of the console: the name on the tracker's list of operators when it is on it, otherwise the spelling most campuses use. A campus's own page also shows the spelling its sources use when that differs.

If a campus you expect is missing, its source may have named a subsidiary or a joint venture instead.
