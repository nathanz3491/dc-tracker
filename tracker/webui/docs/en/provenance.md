# Where a value came from

Every value in the console carries a label saying who vouches for it. The label is drawn into the value itself — its colour and underline — so you can tell at a glance, and hovering the value shows the sentence or source behind it.

## The labels

| Looks like | Label | What it means |
| --- | --- | --- |
| {{v:reported|1,200 MW}} | **quoted** | A source stated this, and the tracker checked that the sentence is really in the article and really says this value. The strongest a value gets. |
| {{v:derived|Loudoun County}} | **derived** | Looked up or worked out, not reported: a county or coordinates from US Census data, compute from megawatts, a campus's tenant taken from its buildings. Checkable, but nobody said it about this campus. |
| {{v:unconfirmed|1,200 MW}} | **待确认** | "To be confirmed." Read out of an article, but no sentence could be found that backs it. Kept so it can be checked; counts for nothing in [confidence](confidence) until then. |
| {{v:inferred|grid capacity}} | **inferred** | A model's judgement from the facts stored. Only ever used for likely obstacles and what to watch for — never for a number, a date or a company. |
| {{v:defaulted|announced}} | **default** | Nobody said anything, and the field cannot be blank, so it shows the fallback. In practice: a phase of *announced* that no source stated. |
| {{v:missing|—}} | **null** | Empty. Often the right answer — see [Why a field is empty](empty-fields). |
| {{v:na|—}} | **n/a** | Empty, and it could not be anything else: running capacity on a campus where nothing is built yet. |

The styles differ in line as well as colour — none, dotted, dashed — so they can be told apart without colour.

## The quote check

Values are read out of articles by a language model. A model can misread, misattribute or invent, so the tracker does not take its word:

1. Every value must come with the **sentence** that states it.
2. That sentence must **really be in the article** as published. If the model reworded it, the tracker finds the article's own sentence or rejects it; it stores the article's words, never the model's.
3. The sentence must **really state the value**. Numbers are compared after converting units, so "1.2GW" backs 1,200 MW, but a real sentence that says something else does not count.

A value that passes all three is **quoted**. One that fails is kept as **待确认**.

## Two kinds of 待确认

A 待确认 value is unconfirmed for one of two very different reasons, and they need opposite responses:

- **Nothing backs it.** The model offered no sentence, or the sentence was not in the article, or did not state the value. The figure may be right; the tracker could not show it. Look for another source.
- **It belongs to something else.** The sentence is real, but the figure is too large to be this campus's — usually a company's whole programme quoted in an article about one site, such as "a $500 billion buildout". These are marked in red **not this site's figure**. The figure is real; it is the wrong thing.
- **It is not money spent on the site.** The sentence is real, but the investment it quotes is somebody's extrapolation or projection ("an extrapolated total capital expenditure … potentially ranging from $8.3 billion"), or what a customer contract is worth ("a $9.7 billion AI cloud agreement"). Kept and shown with that reason, and left out of [Capex](capex). A budget reported as "could reach $900 million" is an ordinary plan and is not marked.

## What the popover shows

Hovering a value opens a small card with:

- the label;
- **not this site's figure**, in red, where it applies;
- what the figure covers, when it is not this campus as a whole: a *programme*, a *region*, a whole *portfolio*, one *building*, or a named phase;
- whether the source spoke of it as *speculated*, *targeted*, *contracted* or *achieved*, when it was not simply planned;
- the sentence, in quotation marks — or the source's excerpt, marked *from the source excerpt, not this field's own sentence*, when no sentence was recorded for the field.

## When sources disagree

Each campus is rebuilt from what all its sources say, never edited by hand a little at a time. When sources disagree on a field, the tracker chooses in this order:

1. A claim that has been ruled out — found to be about something else, or replaced by a later statement — no longer counts.
2. A quoted claim beats a 待确认 one.
3. A figure for the whole campus beats a figure for one building.
4. A first-hand source beats a directory or wiki, which only fills gaps.
5. A statement published more than a year before another of the same standing no longer wins for coming from a weightier source: a builder's 2016 press release does not outrank this year's report. The date is the one the sentence gives for the figure when it gives one, else the article's.
6. Then the field's own rule: the latest and strongest source for most fields, the largest for running capacity (it only grows), the earliest for the announcement date.

The project page's **N other claims** shows every competing value and which was kept. [Field dictionary](fields) gives each field's rule.

## Every statement, in one table

Behind the page, every statement each source makes — a campus figure, a tranche's capacity or date, a company's role, a milestone, an obstacle — is kept as one row with the same columns, whatever it is about: what it is about, the value, how far it can be trusted, the sentence, the citation, and whether it is the value the campus shows. Since 22 October 2026 this table is rebuilt from the sources every time a campus is written, so it always matches the page.

How far a statement can be trusted, most first:

| Tier | Meaning |
| --- | --- |
| **confirmed** | Quoted, and from a first-hand source. |
| **directory** | Quoted, but from a directory or digest that gathers other sources. |
| **derived** | Worked out from a public dataset, such as the Census place a town sits in — not stated. |
| **inferred** | A role nobody stated, read off the operator or buyer column. |
| **unconfirmed** | 待确认: stated, but no sentence backs it. |
| **ruled out** | Decided against: about something else, or replaced by a later statement. |

It is the master data the figures are chosen from, including every value that lost, and it can be downloaded: per campus from the API (`GET /v1/projects/{id}/facts`, see [API reference](api-reference)), or whole with `tracker export facts`.
