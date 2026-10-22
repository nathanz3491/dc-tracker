# A project's page

Every campus has its own page at `/projects/<number>`, opened from any row of [Projects](projects), a card on [Updates](updates) or [Monitor](monitor), a [company page](companies) or the [Map](map). It holds everything the tracker knows about the campus and what each piece rests on.

The page reads in an investor's order: what the campus is and when it delivers first, then how its capacity splits, who is involved and what could make it late, then the evidence behind every value, and last the history, the comparisons and the citations.

**← every project** goes back to the table. A sticky bar of links jumps between the sections: Summary, Capacity by tranche, Who is involved, Delay risk, Fields and evidence, Progress and timeline, In context, Citations. Sections with nothing in them are left out.

**Progress and timeline**, **In context** and **Citations** start closed. Click a section's heading to open or close it; the page remembers that choice in your browser, for every campus. Jumping to a closed section from the bar opens it for that visit only.

## The header

- The campus's number, name and the **☆ Watch** button (see [Monitor](monitor)).
- The operator, linked to its [company page](companies), and **for** the buyer when that is a different company. Each is shown under the company's one name, the same on every page; when the campus's sources spell it differently, their spelling follows in small type — *filed as “Amazon Web Services”* — so the header can be matched against the citations. When the buyer's role has ended — a lease rejected, a site sold — the header says *formerly for* the company and until when instead.
- Where it is, and the regional grid operator area where known.
- Its phase, its **confidence** (0–3; see [Confidence](confidence)) and how many obstacles are open — click that to jump to them.
- **suspected delay · N days past its date**, in amber, when the campus's own online date has passed and nothing reported since says it is running. Hover it for the reason in one sentence; the summary below gives the date, and [Numbers and dates](numbers-and-dates) the full rule.

## Summary

Six figures across the top. Hover over a figure, or tap it, for the sentence it rests on.

- **Planned** and **Running** — the campus's capacity at full build and what a source says is in service, with the value's [tier](tiers) and, when the source said which kind of megawatt it measured, *facility power* or *generator rating* (see [Numbers and dates](numbers-and-dates)). Running also says what share of planned it is.
- **Still to come** — planned less running: arithmetic on the two, not a cited figure. For an operating campus no source gives a running figure for, it is *—*, *running; how much is not cited*: something runs and nobody has said how much, so what is left is unknown rather than all of it. Under it, when the rest is due, read the way a [company page](companies)'s *Supply* reads it: the campus's own date while the period it names is open; *its date has passed*, in amber, when the campus is a suspected delay; otherwise the next tranche still to come.
- **Expected online** — as the source stated it (*Q3 2026*, *2027*). In amber when the campus is a suspected delay, with how long ago the date passed.
- **Delay risk** — *High*, *Elevated* or *Low*, with the first reason; **+N more reasons** jumps to the Delay risk section, which lists them all.
- **Investment** — the announced figure for this site, and the cost per planned megawatt it implies (derived).

Under the figures, one line each:

- **Buyer** — every company a source names as buying the campus's capacity. When there are several, the line says so: [Capex](capex) then credits none of them rather than choosing. The header under the campus's name, the Buyer field and the projects table then name all of them too, so no part of the page names one buyer where the summary names three. When the only buyer named has left, it reads *none now*, with who it was, until when and why.
- **Operator**, linked to its company page.
- **Power** — the utility a source confirms, and the grid operator area.
- **Watch for** — the next milestone that would show the campus moving.
- **Biggest obstacle** — the most serious open obstacle, and how many are open; click the count to jump to them.
- **Evidence** — the row's confidence, how many sources it rests on and the date of the newest, and how many of the twelve fields are filled.

### AI overview

**✦ AI overview**, under the summary, is a short written reading of the campus — where the build stands, what would move it, how far to trust the row — written by a language model from the values on this page. It starts on its own and appears as it is written; **Rewrite** asks again. When the model provider refuses for a moment it is asked again before anything is shown; when it is still not answering, the panel says so — *OpenCode Go, the model provider, is not answering right now* — and **Try again** asks once more.

> [!WARNING]
> The AI overview and the inferred analysis (under Delay risk) are a model's reading, not evidence. Nothing they say is stored, nothing they say changes any value, and they can be wrong. Each one costs money to run, so they are switched on per account. When they are off, the panel says why: switched off on this console, or not switched on for your account — in which case an administrator can turn them on.

## Capacity by tranche

Many campuses are built in pieces: buildings, phases, halls. Each is a **tranche** with its own capacity, state, tenant and date.

- **The headline** says how many megawatts are delivering out of the campus total, and across how many tranches and sections. It is the campus's running figure — the same number as *Running* in the summary — because sources often describe a campus once as a whole and again building by building, and adding up every tranche marked live would count the same megawatts twice. When the tranches add up to more than the campus, the bar draws the campus's own figures — running, then not yet running — and a fold under it gives the tranches as the sources describe them, with why they do not add up, until the nightly tranche review has nested them. A campus with no running figure counts its cited live tranches instead, never past the campus total, so the headline can read 0 even when buildings are running.
- **A bar** shows the tranches by state, with the megawatts of each in the legend under it.
- **One line per section** of the campus: its state, its name (with the facility it belongs to, *Stargate Site 1 › Unit 1*), what it delivers of what it holds, its customer, and its date — *since* the month it went live, or, in amber, *was due* when its whole stated period is over with nothing saying it runs (a paused or cancelled tranche is never marked, and neither is one dated "2026" before 2026 has ended). The first twelve, then **show N more**.
- **Parts sit under what they are part of.** A building inside a phase, or a phase of a campus-wide tranche, is indented under it (└), and the campus counts what its parts add up to — or its own figure where that is larger, the remainder nobody broke down — never both. Click a line to see who placed it: *automatic* (one tranche under several names; a parent its source names; a campus-wide tranche its parts fit inside), *the nightly review* (a model read the quotes), or a person by name.
- **N set aside** lists tranches that are in no figure: another name for a tranche above, an earlier figure the sources have since replaced, or another campus entirely — each with why, and who decided.
- **Admins** can rule on any line from the same place: part of another tranche, the same as one, its own part of the campus, superseded, or another campus. A person's ruling outranks the model's and every automatic one; **Take back** removes it.
- **· N notes** on a line means there is something to check; click the line to read it — the other names sources gave the section, a section that could not be settled, *two sources confirm A and B MW — two figures, not one*, or a capacity marked **待确认** because no quote names that section.
- **How the N MW adds up** opens the arithmetic: every megawatt of the campus on one line — cited per tranche, not yet itemised, stated but unconfirmed, not tied to a named building, counted twice over — down to the total. Capacity that is not counted in the total gets its own line rather than vanishing: out of scale, set aside as another name, superseded or another campus, and the utility's plant, under *Not part of the total*. A tranche larger than the whole campus is named under the list as *out of scale*.
- **Power serving this campus** opens the utility's plant — substations, gas or solar generation — which is never added to the campus's own figures.
- **N relationships not yet closed** lists the [relationships between the figures](invariants) that this campus's sources do not yet settle — tranches that describe it twice, a phase tied to no facility, dates out of order — each in a sentence, with what breaks it here. None of them changes a figure on the page.

## Who is involved

The companies named in connection with the campus, one line per role — operator, developer, owner, customer, contractors and suppliers, utility — with each company as a name. Hover over a name, or tap it, for the sentence that names it and the date a source first named it.

Each company appears once in a role, under the same one name every other page uses. Two spellings that differ only by words such as *Construction*, *Capital*, *Inc.* or *Group* — *DPR* and *DPR Construction* — are one company; when a source wrote the name differently, the popover says how (*written “DPR Construction” by the source*). A source that names several companies as one — *Oracle, OpenAI* — is read as each of them, but only when every name in it is a company the tracker already knows, so a firm such as *Black & Veatch* stays whole. Two different companies with a word in common, such as *Applied Digital* and *Applied Partners*, stay apart.

- A **dashed outline** means no source stated the role outright: it is inferred from the operator or buyer column. An **amber** name was offered with a sentence that would not support the role (**待确认**, with the reason).
- **new** marks a company first named by one of the campus's sources in the last 30 days. They are listed again at the top, under **Newly mentioned**, with the date.
- **Former** lists a company whose role here has ended — a tenant whose lease ended, an owner that sold — with the role, until when and why. Sources go on naming a tenant after it has gone, so the ending is recorded by a person, and it holds through every later reading of those sources. A former company stays on the page as evidence of which campus this is, but it is nobody's buyer now: not in the summary, not on [Capex](capex), not on its company page and not in a watch.

## Delay risk and obstacles

The delay risk first — the level and every reason for it, each something you can check on this page. It is the same reading the company page's *Supply* gives the campus; see [Company pages](companies) for what each level rests on. A campus that is running is read the same way while it still has capacity to deliver; once nothing is left to deliver, or when it is paused or cancelled, the risk is low.

Then the obstacles, one line each: how serious, what kind, what it is, and when it was first seen (and resolved). *uncited* marks a summary nobody could quote. Click a line to open the sentence it rests on, its status, the progress track it holds up and what would show it clearing. Open obstacles come first, the most serious first, twelve at a time; resolved ones are behind **N resolved or set aside**.

**◈ Inferred analysis** asks a model what could obstruct the campus and what news would show it is still moving. It runs only when you press **Run analysis**. Each answer has a five-step meter of the model's confidence.

## Fields and evidence

The twelve tracked fields in two columns, one line each: the field's name in English and Chinese with its unit, the value, and its [tier](tiers) — *quoted*, *derived*, *待确认*, *inferred*, *default*. The [Field dictionary](fields) explains each field.

- Hover over a value, or tap it, for the sentence it rests on.
- **not this site's figure** marks a number found to belong to something larger, such as a company's whole programme.
- **disputed** means the quoted claims say a different value from the one stored, or none supports it.
- **N claims** — more than one source gave a value.

Click a field's name to open its evidence under the grid: the sentence and the site it is from, why a 待确认 value is unconfirmed, the field's internal name (for example `mw_planned`), and every claim the sources made about it — each value, which one was **kept**, why, and a link to each source with its type and weight. For the biggest obstacle it also says why that obstacle was chosen; when two were equally serious it says the choice was arbitrary.

## Progress and timeline

Starts closed.

**Five independent tracks** shows where the campus stands on each progress track — site control, permits, power, construction, customer & finance — what it is waiting for next, and any obstacle holding a track up. See [Phases and progress](progress).

The timeline is a chart of the milestones reached on each track along one time axis, with a dashed line for today and one for the expected online date. A filled dot is cited, a hollow one implied by a later milestone, an amber one reported but not cited. **Every milestone, with what it rests on** lists them by track. When sources give different dates for one milestone, it says so and shows the earliest, choosing neither. **show every recorded event** lists the raw events, including expected ones, marked *expected, not reported done*.

## In context

Starts closed. How this campus compares: its share of what is tracked in its state and overall, investment per megawatt (against a typical band of about $8–15 million per MW), compute — the capacity restated as NVIDIA H200 accelerators — and how much of its capacity is broken down into buildings or phases. Every figure here is worked out from two stored values and labelled as derived.

## Citations

Starts closed. The first five sources, then **View all N citations →**, which opens `/projects/<number>/sources` with every one.

Each citation shows:

- its type and **weight**: company filings and government documents 3, trade press 2, general media 1 (see [Where the data comes from](methodology));
- the date the tracker **fetched** it, which is not necessarily the date it was published;
- the address, which opens the publisher's page in a new tab;
- the excerpt, and which fields it supports — **❝** marks a field with its own sentence, shown on hover;
- *reference data* for a lookup such as Census geography, which corroborates nothing.

To read an article with its quoted sentences highlighted, use [Sources](sources).

## A number that is not there

If a campus's number opens *No project #N*, the row is gone. Most often it was merged into another row as a duplicate; merging removes the row it folds in. See [Duplicates](duplicates).
