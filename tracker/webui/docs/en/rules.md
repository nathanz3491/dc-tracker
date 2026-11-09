# Rules

Almost everything the tracker knows belongs to one campus: an article named it, and every value on its page traces to a sentence about it. Some news is about a place instead — a state pausing new data-center permits, a town banning large sites, a grid operator's rule for big new loads, a tax break for the whole state. **Rules** is where those are kept, and where you see which projects each one reaches.

## What a rule is

A rule is something that applies to a place, or to every campus at once, rather than to one campus. It is read from a news article and kept only when the article's own sentences say what it is, where it applies and where it stands; a number such as a size threshold must be in them too. A rule the article does not bear out is dropped, never stored half-true. Several articles about the same rule are one rule, and the newest one says where it stands now — so a campaign pledge that becomes an order, and an order a court then blocks, read as one history.

Each rule has:

| | What it says |
| --- | --- |
| **Status** | *proposed* (introduced, petitioned or put to a vote), *pledged* (promised by an official or a candidate), *passed* (adopted, not yet in effect), *in force*, *blocked* (stopped by a court for now — it may bind again), *lifted* (repealed or expired) or *voted down*. |
| **Effect** | *restricts* — makes building harder: a ban, a moratorium, a permit pause, a new cost. *eases* — makes it easier: a tax exemption, building allowed by right, a law letting large loads build their own power. |
| **Need** | Which need of a campus it touches, in the same words as obstacles: permitting, grid capacity, water, equipment supply… |
| **Covers** | Which projects: those with no permit yet, those not yet running, or every project that is not cancelled — and only those above a size, when the rule names one. |

## Which projects a rule reaches

Matching is done by fixed rules in the code, never by a model, and every match says why in a line such as *TX · announced · no permit on record*.

| A rule for | Matches a project on | Fit |
| --- | --- | --- |
| a state | the project's state | exact |
| a county | its state and county | exact |
| a town | its state and the town it is filed under | likely |
| a grid operator's region | its state, where that state is mostly in the region | likely |
| the whole country, or one input | nothing | — |

Then the stage: a rule for projects with no permit yet reaches only those announced or in permitting with no approved permit on record; a rule for projects not yet running leaves out the ones running. A project smaller than the rule's size threshold is left out; one whose size nobody has published is kept, as *likely*. A rule lifted or voted down reaches nothing.

### Exact and likely

**Exact** means the campus is filed in the state or county the rule names, at a stage it covers. **Likely** means part of the match is inferred, and the reason says which part:

- **A town's limits are not checked.** No project in the tracker has a site coordinate: the point on the map is the centre of the town it is filed under. Whether the campus is inside that town's limits cannot be checked, so a town's rule matches the projects filed under that town as *likely*. A campus filed under a mailing city but sitting in the township next door will be matched wrongly until its real site is known.
- **A grid region is read from the state.** Only states wholly or mostly in one grid operator's region are matched — Texas to ERCOT, Virginia and Ohio to PJM, and so on. A state split between operators, such as Illinois, Indiana, Michigan, Kentucky or Missouri, matches no grid operator's rule. Known exceptions inside matched states: parts of Texas outside ERCOT (El Paso, the Panhandle, the north-east corner) and parts of California outside CAISO.
- **The size is unknown.** The rule has a threshold and nobody has published the campus's size.

## A rule is not an obstacle

A rule reaching a campus is kept apart from the campus's own obstacles. It is not counted among them, not chosen as the biggest obstacle, does not change the campus's confidence or completeness, and never makes a project *blocked* on [Monitor](monitor). Everything else on a project's page traces to an article about that campus; a rule reaches it because of where it is, and keeping the two apart is what keeps that true.

## Rules for the whole industry flag nothing

A tariff on transformers, export rules for chips, or a war's effect on supply touches every campus at once. Marking all of them would tell you nothing you could act on, so these are listed under **Market** at the foot of the page and reach no project. A campus hears of them when an article about that campus does — "transformer delivery slipped to 2028" is an obstacle of that campus, with its own sentence.

## The page

- **Tiles** — how many rules are shown and how many restrict or ease, how many projects a restricting rule reaches, how many restrictions are in force, and how many market rules there are.
- **state** — show one state's rules. A state also takes in the rules of the grid operator whose region it is mostly in, so Texas shows the ERCOT rules.
- **Include lifted and voted down** — adds rules that no longer stand. They reach nothing; they are history.
- **A card per rule** — where, what kind and which need; its status and its effect; a one-sentence summary; what it covers, its dates, when it was first and last reported, and, for a rule that restricts, what would clear it.
- **the N projects it reaches** opens the list: each project (a link to [its page](project-page)), its company, place and stage, the fit, and why it matched. Exact matches come first.
- **the N articles** opens the articles behind the rule, newest first: the headline (opens the article), the publisher, the date, what that article said the status was, and each sentence the rule was kept on, labelled with what it bears out — *the rule*, *where*, *where it stands*, *the size threshold*. A sentence that bears out several is shown once.

## Where else rules appear

- On a [project's page](project-page), **Exposed to** lists the rules that restrict the campus's place, and **Helped by** the ones that ease it.
- On [Monitor](monitor), **Rules for their places** lists each rule reaching your projects once, with the projects it reaches and, for a restriction, what would clear it.
