# A project's page

Every campus has its own page at `/projects/<number>`, opened from any row of [Projects](projects), a card on [Updates](updates) or [Monitor](monitor), a [company page](companies) or the [Map](map). It holds everything the tracker knows about the campus and what each piece rests on.

**← every project** goes back to the table. A sticky bar of links jumps between the sections: Figures, Who is involved, The twelve fields, In context, Tranches, Obstacles, Citations, Timeline. Sections with nothing in them are left out.

## The header

- The campus's number, name and the **☆ Watch** button (see [Monitor](monitor)).
- The operator, linked to its [company page](companies), and **for** the buyer when that is a different company.
- Where it is, and the regional grid operator area where known.
- Its phase, its **confidence** (0–3; see [Confidence](confidence)) and how many obstacles are open — click that to jump to them.

## The figures

Five cards: planned capacity, capacity in service, compute, announced investment and open obstacles.

The capacity cards name the **kind** of megawatt the source gave: *IT capacity* (the computing load), *Facility power* (the whole site's draw) or *Generator rating*; plain *Capacity* when the source did not say. See [Numbers and dates](numbers-and-dates). **Compute** is the capacity restated as NVIDIA H200 accelerators; it is worked out from the megawatts unless a source states a chip count.

### AI overview

**✦ AI overview** is a short written reading of the campus — where the build stands, what would move it, how far to trust the row — written by a language model from the values on this page. It starts on its own and appears as it is written; **Rewrite** asks again.

### Inferred analysis

**◈ Inferred analysis** asks a model what could obstruct the campus and what news would show it is still moving. It runs only when you press **Run analysis**. Each answer has a five-step meter of the model's confidence.

> [!WARNING]
> Both panels are a model's reading, not evidence. Nothing they say is stored, nothing they say changes any value, and they can be wrong. Each one costs money to run, so they are switched on per account. When they are off, the panel says why: switched off on this console, or not switched on for your account — in which case an administrator can turn them on.

## Who is involved

The companies named in connection with the campus, each with its role — developer, owner, operator, customer, utility or contractor — and the sentence that names it. **inferred** means no source stated the role outright; **待确认** means the sentence could not be checked, with the reason.

## The twelve fields

All twelve tracked fields, each with its value, its label, and the sentence it rests on. The fields are listed by their internal names (for example `mw_planned`); the [Field dictionary](fields) gives each one in plain words.

- **not this site's figure** marks a number that was found to belong to something larger, such as a company's whole programme.
- **excerpt, not this field's sentence** means the source's excerpt is shown because no sentence was recorded for this field specifically.
- The biggest-obstacle row says why that obstacle was chosen; when two were equally serious it says the choice was arbitrary.

**N other claims** opens what the other sources said about the field: each value, which one was **kept**, why, and a link to each source with its type and weight. When sources disagree, this is where you see it.

## In context

How this campus compares: its share of what is tracked in its state and overall, investment per megawatt (against a typical band of about $8–15 million per MW), compute, and how much of its capacity is broken down into buildings or phases.

**Five independent tracks** shows where the campus stands on each progress track — site control, permits, power, construction, customer & finance — what it is waiting for next, and any obstacle holding a track up. See [Phases and progress](progress).

## Tranches

Many campuses are built in pieces: buildings, phases, halls. Each is a **tranche** with its own capacity, state, tenant and date.

- The headline says how many megawatts are delivering out of the campus total, across how many tranches. Only cited capacity counts as delivering, so the headline can read 0 even when buildings are running.
- A bar shows the tranches by state.
- The table lists each tranche: its name, megawatts, state, customer and online date, with notes such as *also called…*, *was due* (amber when overdue) or *two sources confirm A and B — two figures, not one*.
- The reconciliation rows account for every megawatt of the campus: cited per tranche, not yet itemised, stated but unconfirmed, not tied to a named building, out of scale, counted twice over. Capacity that is not counted in the total gets its own line rather than vanishing.
- **Power serving this campus** lists the utility's plant — substations, gas or solar generation — which is never added to the campus's own figures.

## Obstacles

Open obstacles first, up to twelve at a time (**show N more** for the rest). Each shows its kind, severity and status, when it was first seen and resolved, a summary, the sentence it rests on — or *uncited* when the summary is a paraphrase nobody could quote — and which track it holds up and what would show it clearing.

## Citations

The first five sources, then **View all N citations →**, which opens `/projects/<number>/sources` with every one.

Each citation shows:

- its type and **weight**: company filings and government documents 3, trade press 2, general media 1 (see [Where the data comes from](methodology));
- the date the tracker **fetched** it, which is not necessarily the date it was published;
- the address, which opens the publisher's page in a new tab;
- the excerpt, and which fields it supports — **❝** marks a field with its own sentence, shown on hover;
- *reference data* for a lookup such as Census geography, which corroborates nothing.

To read an article with its quoted sentences highlighted, use [Sources](sources).

## Timeline

A chart of the milestones reached on each of the five tracks along one time axis, with a dashed line for today and one for the expected online date. A filled dot is cited, a hollow one implied by a later milestone, an amber one reported but not cited.

**Every milestone, with what it rests on** lists them by track. When sources give different dates for one milestone, it says so and shows the earliest, choosing neither. **show every recorded event** lists the raw events, including expected ones, marked *expected, not reported done*.

## A number that is not there

If a campus's number opens *No project #N*, the row is gone. Most often it was merged into another row as a duplicate; merging removes the row it folds in. See [Duplicates](duplicates).
