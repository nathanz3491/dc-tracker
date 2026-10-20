# Projects

**Projects** is every campus the tracker knows, one row each, with every value showing where it came from.

## Searching

Type into **name, city, operator…**. Every word must appear somewhere in the row: its number, name, company, tenant, city, county, state or biggest obstacle. A company is found both by the name the table shows and by the spelling its source used, so `amazon` finds a campus filed as "AWS". To go straight to one campus, type its number with a hash, for example `#42`.

## Filters

**Filters** opens the filter panel; the button shows how many are set, and **Clear** removes them all.

| Filter | What it keeps |
| --- | --- |
| **state** | Campuses in one state. |
| **phase** | One phase: announced, permitting, construction, operational, paused or cancelled. See [Phases and progress](progress). |
| **confidence** | *1 or better*, *2 or better* or *3 only*. See [Confidence](confidence). |
| **obstacle** | Campuses with an open obstacle of one kind, such as `grid_capacity` or `community_opposition`. |
| **severity** | Campuses with an open obstacle of at least this severity: watch, material, blocking. |
| **Quoted only** | Rows where every filled field is quoted or looked up — none is 待确认, inferred or a default. |
| **Suspected delays only** | Campuses whose own online date has passed with nothing saying they run. The switch shows how many there are. See [Numbers and dates](numbers-and-dates). |

The obstacle and severity filters are checked separately: a row passes if it has *an* open obstacle of that kind and *an* open obstacle of that severity, which need not be the same one.

## Columns

The table always starts with **#** — the campus's number, with its ☆ watch button — followed by the tracked fields. Which fields show by default depends on how well filled they are: a field filled for at least half of the campuses is shown, the rest are behind **Show N sparser fields**. So the default columns can change as the data grows.

| Column | Meaning |
| --- | --- |
| **Project** | The campus's name. |
| **Operator** | The company building and running it. |
| **Buyer** | The tenant that will use it, if a source names one. |
| **City**, **State** | Where it is. |
| **Phase** | How far along it is. |
| **Late** | For a suspected delay, how many days ago its online date's period ended, in amber; hover for the reason. Empty for every other campus. Always shown, beside Phase. |
| **Planned (MW)** | Planned capacity, in megawatts. |
| **Running (MW)** | Capacity switched on today. |
| **Investment (USD)** | Announced investment. |
| **Announced** | When it was first announced. |
| **Online** | When it is expected to come online. |
| **Biggest obstacle** | The most serious open obstacle, in one sentence. |

**Operator** and **Buyer** show each company under one name, however a campus's source spelled it: AWS, Amazon Web Services and Amazon Data Services all read *Amazon*, and RagingWire reads *NTT*. The name is the one on the tracker's list of operators, otherwise the spelling most campuses use, and every page of the console uses the same one. A campus's own page shows its source's spelling beside the name when the two differ. A tenant described without a name, such as "a Fortune 100 company", is shown as written.

With **All fields** on you also get **Compute (H200-eq)**, **County**, **Lat**, **Lon**, **Confidence (0-3)** and **Verified**. Every field is defined in the [Field dictionary](fields).

Two columns at the end are summaries and cannot be sorted:

- **filled** — how many of the twelve tracked fields are filled: green at 9 or more, amber below 6.
- **tracks** — five small cells, one per progress track. Dark: reached. Faint: only implied by a later milestone. Red: blocked. Grey: not yet. See [Phases and progress](progress).

## Reading a value

Each value is drawn in a style that says where it came from, and hovering it (tapping, on a phone) shows the sentence behind it. **what do the underlines mean?** above the table opens the key. In short:

| Looks like | Means |
| --- | --- |
| {{v:reported|1,200 MW}} | Quoted: a source says so, and we checked the sentence. |
| {{v:derived|Loudoun County}} | Derived: looked up or worked out, not reported. |
| {{v:unconfirmed|1,200 MW}} | 待确认: extracted, but no sentence backs it yet. |
| {{v:inferred|grid capacity}} | Inferred: a model's judgement. |
| {{v:defaulted|announced}} | Default: nobody said; this is the fallback. |
| {{v:missing|—}} | Empty. |

[Where a value came from](provenance) explains each one. Small marks on numbers matter too:

- **~** approximate, **≤** at most, **+** after a number means at least;
- **◒** the figure is the whole site's power draw, not its computing load; **⚡** it is a generator's rating. Neither is comparable with an unmarked figure. See [Numbers and dates](numbers-and-dates);
- a date shows only as precisely as the source gave it: "2027" means the source said 2027, not 1 January.

**coverage** above the table shows the three best- and worst-filled fields.

## Sorting and loading

Click a column header to sort by it; the first click sorts high to low, the next reverses. Empty values always go last. The status line above the table says how many rows match and how they are sorted. Sorting by **Late** puts the most overdue campus first, and with **Suspected delays only** on it lists every suspected delay, worst first.

The default order is by confidence, highest first — even though the Confidence column only appears with **All fields** on.

Rows load thirty at a time as you scroll; **Load N more** does the same from the keyboard.

## Opening a campus

Click a row, or tab to it and press Enter, to open [its page](project-page). Hovering the **#** number shows the campus's internal identity key, which is what makes two rows the same campus or not.

On a narrow screen the table becomes cards: name, confidence, place and phase (with *late N days* for a suspected delay), planned MW and investment, how many fields are filled, and the progress tracks.

## When nothing matches

*No project matches these filters* — widen the confidence floor, clear the obstacle filter or clear the search.
