# Conditions: rules that apply to a place

Since 2026-11-10 the tracker keeps a second kind of fact beside the campus: a
**condition**, a rule that applies to a place or an input rather than to one campus —
a state's permit pause, a town's ban, a grid operator's large-load rule, a tariff. The
code is `tracker/conditions.py`; the commands are `tracker conditions`.

## Why

Every fact in the database belongs to a campus an article named, and the reader drops
an article that names none. A rule that applies to a whole state or town names none.
In the week after the 2026-11-03 elections 72 articles about the vote were filed "no
project", and the Texas governor-elect's permit moratorium reached 21 of 205 Texas
projects — only the ones a stock story happened to name. A rule that applies to a place
should reach every project in that place and stage, the day it is reported.

## How a rule gets in

1. **Pages.** Every page the news step reads is saved to the article cache, including
   the ones it found no campus in. `conditions read` walks the pages read since a date
   (two days by default) and keeps those a free word test passes: three or more words a
   rule is stated in (moratorium, ban, pause, ordinance, ballot, governor…), a word for
   a jurisdiction, and data centers. About 150 of ~700 pages a day pass.
2. **One model call per page per prompt** (`STAMP`, `condition_read`). The model is
   shown the rules already held for the states the article names, and offers rules
   with one sentence for each thing it says: the rule, where, its status, its size
   threshold.
3. **The gate.** A rule is kept only when each sentence is the article's own (the
   extraction path's matcher — the stored text is the article's, never the model's),
   the place it is filed under is named in those sentences or the headline, its
   status is worded in them ("voters approved", "she vowed", "a judge blocked"), and
   any threshold's number is in them. A rule that fails is counted and dropped, never
   stored half-true.
4. **One rule, however many articles.** The model's `same_as` (checked: it must be the
   same place) or, failing that, the same place, need and kind and a title about the
   same thing (Aurora, Colorado had two zoning rules in one week). Status, summary and
   dates come from the newest article, so a pledge that becomes an order, and an order
   a court blocks, read as one history (`condition_source` holds each article's
   statement and sentences).

**A rule can help as well as hinder** (`effect`, migration 0052): `restricts` for a
ban, moratorium, pause, new cost or requirement; `eases` for a tax exemption, by-right
zoning, a fast track, a law letting a large load build its own power. The first dry
run kept Utah's self-generation law and an airport district's by-right zoning beside
the moratoriums; shown as "exposed to", help reads as a threat. A rule the model does
not class is taken to restrict.

The article reader's own prompt is not touched. It is versioned and measured against
the answer key; a second job inside it would move both.

## Which projects a rule reaches

Code, never the model (`conditions.reach`), rebuilt wholesale by `conditions match`:

| scope | matched on | fit |
| --- | --- | --- |
| state | the project's state | exact |
| county | state and county | exact |
| place (town) | state and the town the project is filed under | likely |
| grid region | the project's state, where the state is mostly one operator | likely |
| national, input | nothing | market context only |

Then the stage (`new`: announced or permitting with no approved permit on record;
`unbuilt`: not yet running; `all`: not cancelled) and the size threshold (a project of
unknown size is `likely`; a smaller one is out). Every exposure stores why, e.g.
"TX · announced · no permit on record". A rule lifted or voted down reaches nothing.

**Exposures are kept apart from obstacles.** They are not counted in the blocker,
confidence, completeness or the answer key, so "every value traces to an article about
this campus" still holds.

### Two limits of the data

- **No project has a site coordinate.** All 1,150 stored coordinates are town centres
  derived from the city name (`tracker/ingest/geo.py`), so whether a campus is inside a
  town's limits cannot be checked. A town's rule matches a project filed under that
  town as `likely`, and the reason says the limits were not checked. A campus filed
  under a mailing city but sitting in the township next door will be matched wrongly
  until a real site location is known.
- **Grid regions are inferred from the state** (`STATE_REGIONS`). Only states wholly or
  mostly in one operator's region are listed; a split state (Illinois, Indiana,
  Michigan, Kentucky, Missouri, Mississippi, the Dakotas, North Carolina) matches no
  grid-region rule. Known exceptions inside listed states: Texas outside ERCOT (El Paso,
  the Panhandle, the north-east corner) and California outside CAISO (LADWP, SMUD, IID).

- **No project records its utility.** A single utility's tariff or rate class covers
  that utility's customers, not the state: AEP Ohio's tariff for 25 MW+ customers, read
  as statewide on 2026-11-10, flagged 80 Ohio projects. Such a rule is now filed as
  market context, or under its city when the utility is a city's own (Seattle City
  Light).

### Why whole-industry rules flag nothing

A tariff on transformers, chip export rules, or a war's effect on supply touches every
project. Flagging every row says nothing a reader can act on, so these are listed as
market context, and a campus hears of them when an article about that campus does
("transformer delivery slipped to 2028").

## Who hears of it

- **The console**: a project's page lists the rules reaching it under **Exposed to**
  (restricting) and **Helped by** (easing), apart from its obstacles; Monitor says what
  would clear each; the **Rules** view lists them all, with a market section for the
  nationwide and whole-industry ones.
- **The morning email**: one card per rule per status for a reader whose watchlist it
  reaches ("Texas: pause on new data center permits — pledged — 31 of your projects"),
  a "cleared" card once when a rule they were told of is lifted or voted down, and
  easing rules under their own heading (`tracker/notify.py`).
- **The public API**: `/v1/conditions` and `/v1/conditions/{id}`, with each article's
  own sentences and every project a rule reaches, its fit and why.
- **Follow-up searches**: a rule not yet settled is searched for by the news watch
  every three days, at most 20 a night (`tracker news --rules`), so a pledge that became
  an order is read without waiting for a campus story to mention it.

## Commands

```bash
tracker conditions                       # the rules held, and how many projects each reaches
tracker conditions show 12               # one rule: articles, sentences, projects
tracker conditions read --since 2026-11-03 --dry-run   # read at full cost, write nothing
tracker conditions read                  # the last two days' saved pages
tracker conditions match                 # rebuild which projects each rule reaches; free
tracker conditions drop 12               # a rule read wrongly
```

`read`, `match` and `drop` write, so they run on the host, outside the nightly loop's
hours. The nightly loop runs `read` in round 1 right after the news step
(`--conditions`, 150 pages by default, `0` to skip), and `match` in every round's free
phase and once more after the last round, so a project merged or moved to another stage
during the night is under the right rules by morning.
