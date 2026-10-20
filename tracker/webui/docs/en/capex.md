# Capex

**Capex** adds the tracker up by **buyer**: the company that will actually use the capacity, which is often not the company building it. A developer building for a cloud company, for example, is counted under the cloud company.

Paused and cancelled campuses are left out of every figure on this page.

> [!IMPORTANT]
> **Every number here is a minimum.** Capacity no source has stated counts as zero, so the true totals are higher. Trust the megawatts before the dollars: capacity is reported far more often and more consistently than investment.

## The tiles

| Tile | Meaning |
| --- | --- |
| **planned or building** | Megawatts planned across every campus counted. |
| **already running** | Megawatts switched on today. |
| **confirmed spend** | Investment backed by a quoted sentence. |
| **claimed, not counted** | Investment that was reported but is not added in — for example a figure for a whole programme rather than one site. |
| **have a named buyer** | The share of capacity whose buyer is known. |

## Who counts as the buyer

For each campus, in this order:

1. a company a source names as the **customer**;
2. the **Buyer** field (spellings are folded together, so Meta and Facebook, or AWS and Amazon, count as one);
3. the **operator itself**, when it is a company that uses its own capacity — the large cloud companies, GPU clouds, and a few others such as AI labs;
4. otherwise **(no named customer)**, always shown last whatever its size.

A tenant described only as "a Fortune 100 company" or "a leading hyperscaler" is not a buyer anyone can follow, so its capacity lands in *(no named customer)*.

When a campus is split into buildings or phases with different tenants, each tenant gets the megawatts of its own tranches. Investment is not split this way: dividing it would mean inventing a ratio.

A buyer worked out from who owns the campus, rather than named by a source, is marked with an asterisk (`*`).

Each buyer is shown under one name, the same as on every other page: the name on the tracker's list of operators when it is there, otherwise the spelling most campuses use.

## Duplicates are counted once

The banner **N campuses are stored more than once** appears when the tracker suspects one campus is held in more than one row — usually because different articles named it differently. Each suspected group is counted **once**; the other rows are set aside and their megawatts shown as *skipped*, never added twice.

> [!NOTE]
> Merging duplicate rows into one is done by whoever maintains the tracker, not from the console; nothing on this page changes the data. See [Duplicates](duplicates).

## The table

| Column | Meaning |
| --- | --- |
| **buyer** | Who the capacity is for. |
| **sites** | How many campuses. |
| **planned** | Planned megawatts, with a bar showing the buyer's share. |
| **running** | Megawatts in service. |
| **confirmed $** | Quoted investment, with *+$X claimed* in grey for investment reported but not counted. |
| **MW arriving, by year** | Megawatts due online in each year (or quarter). |
| **MW at risk** | Megawatts on campuses with an open obstacle. |
| **delays** | How many campuses have reported a delay. |
| **biggest obstacle** | The most common serious obstacle, for example *Grid capacity · blocking*. |

**pipeline by year / quarter** switches the arrival columns. Read quarters as a shape, not a schedule: many sources give only a year, and a year-only date cannot be placed in a quarter. The page shows how many dated campuses land on 1 January for that reason.

Small numbers on headings point to numbered notes under the table, which say how each column is counted and what it leaves out.

## Drilling in

Click a buyer's name or any number in its row to open the campuses behind it; click again to close. The breakdown lists each campus with its contribution, and the rows **set aside as suspected duplicates** that are not counted anywhere. Click a campus to open [its page](project-page).

Hovering a buyer's name opens a card with its facts — sites, how many were attributed from ownership, megawatts set aside, money claimed but never confirmed — and a short **AI reading** written by a model from the table's own rows. Like the project page's panels, it is not evidence, is not stored, and costs money to run.

## Below the table

- **How much of the data this page covers** — what share of campuses have each figure.
- **N project(s) list a builder as the customer** — rows where the tenant named is really a construction or development company, which is usually a mistake in the source.
- **One campus, several rows** — the suspected duplicate groups, with the reason each pair was raised.
