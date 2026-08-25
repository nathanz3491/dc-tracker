# Analysis: buyers, risks and slippage

Who is buying the capacity, what could stop these projects being built, what no article says, and how slippage is measured.

Part of the [dc-tracker documentation](README.md).

---

## Who is actually buying the capacity

```bash
tracker capex
```

The database is keyed on the site — `(operator, locality, state)`. The question an
analyst is paid to answer is on a different axis: how much capacity does each end
customer have in flight, when does it land, and how exposed is it. Those grains do
not coincide, because much hyperscaler capacity is built by wholesale developers
and leased.

Grouping on `project.customer` alone would answer for about a tenth of the
database, so attribution is three rules in order:

1. a **named tenant**, folded so Meta and Facebook are one buyer and a hedge like
   "a Fortune 100 technology company" names nobody;
2. otherwise the **operator, if the operator is an end user** — a Meta campus is
   Meta's, and `customer` being NULL there is correct rather than missing;
3. otherwise **unattributed**, reported as its own row rather than hidden, because
   how much is being built for nobody we can name is itself worth knowing.

Who counts as an end user comes from the `kind` column in
[seed/edgar-companies.toml](../seed/edgar-companies.toml), plus a short list of
private companies that file nothing with the SEC — without which OpenAI and xAI,
two of the largest positions in the table, would have been invisible. Only
`hyperscaler` and `neocloud` count; a utility connects capacity and a contractor
builds it, and neither buys it.

`--by-quarter` buckets the pipeline by calendar quarter instead of by year, which
is the grain the question is usually asked at. Read it as a shape rather than a
schedule: 34% of dated projects land on 1 January, because that is where a source
saying only "sometime in 2027" normalises to. `capex.date_precision` measures it
and both the CLI and the page print the number rather than leaving you to guess.

The footer states what fraction of projects the view can speak for. That is not
decoration: a rollup silently covering a third of the data looks authoritative and
is not.

The table defends itself against the two ways it used to be wrong, and both
defences disclose rather than hide:

- **One row per suspected campus.** A campus stored twice is a nuisance in a site
  listing and a wrong number the moment anything groups by buyer — the Abilene
  campus was in the database four times, so 1.2 GW was counted four times against
  OpenAI. The rollup now counts the one row a merge would most likely keep (a
  named tenant first, then the largest capacity) and sets the others aside;
  the footer says how many megawatts and dollars were skipped. Skipped, not
  merged: `tracker merge` remains the repair, and the rows are still there.
- **Implausible dollars are excluded; merely unquoted ones are counted and
  disclosed.** A figure the `$/MW` plausibility ceiling demoted — the signature of
  a programme-wide total like "OpenAI's $500 billion Stargate" quoted in an
  article about one campus — is kept out of the investment column and shown in the
  footer instead. A figure that simply went unquoted is a different thing: very
  likely correct, and nobody sourced it. Excluding those too understated the one
  number the table exists to state, and one 待确认 bit could not tell them apart —
  which is why the gate now records *why* it refused a value and the rollup reads
  that back rather than re-judging the figure.
- **An obstructed project counts even when its obstacle is 待确认.** The
  obstructed column includes projects whose only open obstacles are unquoted, and
  the footer says how many: understating exposure is the worse direction to be
  wrong in, and an obstacle a source reported is information before it is
  evidenced.

The year columns are a continuous range, so a year nothing is dated for shows as
an empty column between years that have capacity — "nothing is *dated* 2029"
rendered honestly, instead of 2029 silently vanishing between 2028 and 2030.

Two things it deliberately does not do. It never infers a tenant — who signed a
lease is a *fact* with a documented answer, and `tracker infer` exists precisely to
keep judgements and facts apart. And where a project names a tracked operator as
its own customer, it flags the row instead of correcting it, for the same reason
`dedup` refuses to auto-merge across granularity: a landlord genuinely can lease
from another landlord.

The console's **Capex** view is this table made openable, because an aggregate a
reader cannot open is one they have to trust. **Click any figure in a row** and it
breaks into the sites that make it up, with the *column* deciding the view: the
site list, planned capacity with a share bar, what is actually running,
investment per site with the never-confirmed ones marked, what is obstructing
which site, what has slipped, or — from a year column — the sites dated into that
year. The panel never sums, so the rows always add to the cell you clicked, and a
site nobody has sized says so rather than showing a zero. Clicking a site opens
its drawer, so the drill-down ends at citations and not at another total.

**Hover a buyer** and a card shows the instant facts with a model-written reading
of the position streaming under them — the capex twin of the project drawer's AI
overview, same fast model, same rules: cached by content, cut at the sentinel,
never stored, never evidence. That prompt asks for **no figures at all**: measured
over four rounds, the fast model wrote fluent analysis and unreliable arithmetic
(subtracting to invent "3,300 MW due mid-year", summing two sites, "only 30%
online"), so every share it was reaching for is now computed server-side *in
words* and the numbers stay where they are correct — on the card above the prose,
and in the table behind it.

The duplicate review also lives on that page rather than under Coverage: the
repair belongs next to the figure it protects. Reviewing a group by eye is also the one thing a browser does
better than the CLI — the candidate rows sit side by side with their capacity,
citation count and dates, a radio picks the survivor, and the merge runs through
the same `/api/run` path as everything else, behind the typed confirmation.

Which id survives decides more than a row number: quantitative fields are
recomputed from the combined citations, but identity fields — name, company,
locality — keep the survivor's values, so pick the row whose identity should win.

### Saying no: `tracker duplicates park`

```bash
tracker duplicates                       # suspected groups, strongest evidence first
tracker duplicates --no-weak             # only pairs raised by more than a shared word
tracker duplicates park 55 58 --reason "different operators, different buildings"
tracker duplicates parked                # every pair ruled out, and who ruled it out
tracker duplicates resolve               # let a model settle them: parks, and merges with --merge
tracker duplicates unpark 55 58          # put the question back
```

The report proposes and never merges, which left it with exactly one possible
answer. A pair that was simply wrong came back on every run, ahead of the real
ones — and this is not only clutter: `capex` reads the same pairs and holds one row
of every suspected group out of the buyer table, so a false pair takes a real
campus's capacity out of a number somebody quotes. Parking is stored pairwise, so a
third row appearing next week is still asked about.

**Each pair now says what raised it**, because "these look similar" is not
something a reader can check:

| evidence | what it means |
|---|---|
| `same name` | the same company and the same name, once normalized. The strongest claim there is |
| `same tranche` | both rows hold one derived `block_key` that appears in no locality but theirs |
| `shared operator` | one company string names the *other's* operator — how one campus becomes four rows |
| `city vs county` | the two dedup keys describe one place at two granularities |
| `name overlap` | a distinctive word in common, the weakest signal, hidden by `--no-weak` |

**The order is the sort order, and it changed.** `city vs county` used to lead, on
the grounds that a structural key match outranks a textual resemblance. It is a
strong statement about *place* and a weak one about *building* — on the live
database it pairs NTT's Itasca campus with NTT's Chicago one, 31.7 km apart — and
`resolve` refuses to merge on it alone, so the report was opening with 31 of its 49
pairs in the one class nothing automated can settle. The classes that name a
building come first now.

**A pair carries every signal that holds for it**, which sounds obvious and was not
true. The pass that finds cross-granularity pairs recorded the key match and threw
away everything else it had computed, so those 31 pairs were silently sharing 12
distinctive name tokens, 8 real tranche keys and 6 byte-identical names between
them. That is why `city vs county` now appears *alongside* other classes rather than
instead of them, and it is what lets `resolve` tell "granularity and nothing else"
from "granularity, and they are also the same building".

**Two false pairs, and then the opposite problem.** `Aligned Data Centers Phoenix`
matched `NTT Global Data Centers Americas Phoenix` on the token "centers" — the
generic-word list held the singular and not the plural. `Element Critical — Houston
One` matched `Switch — Houston Data Center Campus` because both had a tranche
labelled "existing". Both are fixed. But the rule that fixed the second — *a tranche
key appearing in more than one town is vocabulary* — is exactly backwards for the
case that matters most, because **a campus stored as a city and as a county is in
two towns by construction**. Stargate, stored as Crusoe's Abilene row and Oracle's
Shackelford County row, was invisible to the report while both rows held the tranche
key `county.shackelford`.

Rarity is now asked of the pair rather than of the row: a shared key counts when it
appears in no locality *but these two*. For two rows in one town that is the old
rule unchanged, so the flagship case still survives — Abilene is stored four times
and all four hold `building-1`, in one town. Seven pairs arrive this way that the
report could not previously produce — Cipher's Stingray facility, DataBank's DFW3
stored as Plano and as Dallas, and the IREN/Iris Energy Sweetwater rename among
them. Five go the other way, dropped as vocabulary, and every one of them is a
false pair somebody had already written down.

Widening recall reopens the hole precision closed, so three rules say when a shared
key names nothing:

| shape | example | treated as |
|---|---|---|
| facility number | `iad-3`, `va-2`, `ord-1` | identity inside one market, an airport across two |
| market sequence | `hillsboro-1`, `chicago-2`, `sweetwater-1` | reported, never merged on — see the rails below |
| type words, digits and town names | `capacity-1`, `permanent.plant.power`, `expansion.houston` | nothing at all |

The middle row is the interesting one. `sweetwater-1` is the only thing connecting
IREN's Sweetwater campus to the copy stored under its old name, so discarding it
loses a real duplicate — and `hillsboro-1` is held by Flexential's Hillsboro site
and NTT's, so merging on it destroys a real campus. It is evidence without being
authority, which is a distinction the report can carry and a merge cannot.

None of this touches `blocks.generic` or `TYPE_WORDS`. Those decide whether a
tranche's megawatts are summed; these decide whether two rows are the same building.

### Letting a model answer: `tracker duplicates resolve`

```bash
tracker duplicates resolve --dry-run     # ask about every pair, write nothing — still pays for the calls
tracker duplicates resolve               # park the ones it rules are different sites
tracker duplicates resolve --merge       # ...and fold the ones it rules are one campus
tracker duplicates resolve --ask         # put each pair to you first, model as the fallback
```

One model call per pair, three answers, and they are not equally trusted because
their consequences are not equally reversible.

**`different` parks the pair**, recording `model (0.87)` as the decider —
`not_duplicate.decided_by` was built for exactly that, and `unpark` undoes it. This
is the answer that pays for the run: a false pair holds a real campus out of the
capex roll-up until somebody rules it out.

**`same` merges, and only past every rail.** All of them are refusals stated
elsewhere in the codebase, restated as something a run can print:

| rail | why |
|---|---|
| confidence below 0.9 | unparking undoes a park; nothing undoes a merge |
| name-word evidence only | "a shared name word is a word" |
| cross-granularity evidence only | `dedup` has never merged a county row into a city row unattended |
| market-sequence tranche only | `hillsboro-1` is held by Flexential's Hillsboro site and NTT's |
| names differing only by an ordinal | `Polaris Forge 1` and `Polaris Forge 2` are two campuses that share a tranche key |
| coordinates over 25 km apart | a campus can span a mile, not a county |
| no `--merge` flag | the default run parks and never deletes |

The ordinal rail is there because a measurement found the failure it prevents.
Applied Digital's `Polaris Forge 1` in Ellendale and `Polaris Forge 2` in Harwood
both hold `forge-2.polaris`, because one article listed the pair — every signal
agreeing, one digit apart, and a merge would have destroyed a campus. It also
catches Aligned's `SLC02` against `SLC-04`, and deliberately does not catch
"Sweetwater Data Center" against "IREN Sweetwater 1": one name carrying a number and
the other not is a source being more specific, not a sibling.

What a merge may now rest on that it could not before is `same name` — the same
company and the same name outright — and granularity *plus* a building-level signal.
Neither widens the rails so much as stops them refusing evidence the report was
discarding before they saw it.

**Every rail is a refusal of the model, not of a reader.** `--ask` answers a pair at
the keyboard, and a person's answer skips all of them: somebody with a map outranks
a confidence score. That is still the route for the cross-granularity class, which
is 20 of the 57 pairs on the live database.

**How much of the backlog this settles, without spending anything to find out.**
`--dry-run` is not free — it asks about every pair and rolls back the writes, so it
pays for every call. `scripts/measure_duplicates.py` answers the narrower question
for nothing, by putting a hypothetical confident verdict through the real rails:

```bash
python scripts/measure_duplicates.py           # classes, and what --merge would take
python scripts/measure_duplicates.py --json    # one object per pair, for diffing revisions
```

On the live database it reports 15 of 57 pairs mergeable, 7 of them pairs the report
could not previously see at all. What it cannot tell you is whether those merges are
*right*; only reading the two rows does that.

**`unclear` is a real answer** and the prompt says so twice: a wrong "same"
destroys two rows, a wrong "different" hides a real duplicate, and both are worse
than admitting the two rows do not settle it.

**Which row survives is not the model's choice** — most citations, then most fields
filled, then the lower id. It barely matters either way, because the merge recomputes
every field from the combined claims; what it decides is a row number. The model's
whole output is one word from three, a confidence and a sentence, so it cannot name
a survivor or write a value even if it tried.

**A group larger than two settles in one run.** Four rows for one campus produce six
pairs, and the first merge deletes a row the other five name — which used to end the
run's usefulness: they reported "one of the rows is gone" and you ran the command
again, paying for another set of calls. The run now carries its own merges forward
and asks a later pair about the surviving row. The live database has eight groups of
three and two of four, including the Ashburn group where RagingWire and NTT hold
`va-4`, `va-5` and `va-6` under four names.

A person at the keyboard may merge what the rails refuse a model. The rails guard an
*unattended* decision, and `--ask` is the opposite of unattended.

## What could stop these projects being built

```bash
tracker risks                          # every open obstacle, grouped by kind
tracker risks --severity blocking      # only the ones that have stopped work
tracker risks --uncited                # only the 待确认 ones
tracker risks --summary                # the kinds table alone
tracker risks confirm                  # read the articles behind the uncited ones
tracker list --risk transmission       # projects waiting on grid work
tracker stats                          # includes MW at risk per category
tracker exposure --by company          # capacity behind an obstacle, rolled up
```

```text
obstacles by kind
+----------------------+----------+----------+----------+----------+-------+--------+
| kind                 | projects | capacity | blocking | material | watch | quoted |
+----------------------+----------+----------+----------+----------+-------+--------+
| community_opposition |       21 |   18,836 |        3 |        4 |    14 |   9/21 |
| transmission         |       18 |   23,806 |        — |        2 |    16 |  16/18 |
| grid_capacity        |       15 |   22,176 |        — |        2 |    13 |   4/15 |
+----------------------+----------+----------+----------+----------+-------+--------+

transmission  18 project(s), 23,806 MW  (+4 with no cited capacity)
  material #1 Microsoft — Fairwater (Mount Pleasant, WI · 900 MW)
      Two 345-kilovolt upgrades outstanding before full load.
      "must complete two 345-kilovolt upgrades"
  watch    #29 NTT — Hillsboro (Hillsboro, OR · no capacity)
      Cooling draw questioned by the county board.
      待确认 uncited — the quoted sentence does not state this category
```

**Whether an obstacle is quoted is a column, not a footnote.** The old layout
printed every obstacle at the same weight and then admitted at the bottom that a
third of them rested on nothing quotable, which is the wrong way round: that is the
first thing a reader needs and it was the last thing they were told. The kinds
table carries it per category — `4/15` on `grid_capacity` is a different statement
about that number than `16/18` on `transmission`.

Categories map onto the PRD's obstacle list — `grid_capacity`, `transmission`,
`permitting`, `environmental`, `equipment_supply`, `chip_supply`, `financing`,
`offtake`, `community_opposition`, `water` — which is what makes the read-through
countable: MW blocked on `transmission` is a power and utility signal, MW blocked on
`offtake` or `chip_supply` is a cloud and semiconductor one.

Each obstacle shows the verbatim quote behind it, and one that has none says
`uncited` rather than sitting silently beside the evidenced ones — and says
*which* kind of uncited, because the answers differ. "Quoted nothing for it"
means go and find a source. "The quoted sentence does not state this category"
means the sentence is real and filed under the wrong heading, which is a
correction to a source you already have.

**An obstacle whose quote fails is kept, not deleted.** Until migration 0012 it
was the one thing in the ingest path that still went on the floor, and that fell
hardest on the field this database is worst at: no press release names its own
blocker, so an adversarial second source is the only thing that ever records one.
The failed quote is still never stored beside it. Unconfirmed obstacles count
toward the MW sums, with the count disclosed in the footer — but they cannot
quietly become `project.blocker`, which is one of the twelve tracked fields: a
confirmed obstacle always outranks an unconfirmed one, and if an unconfirmed one
does fill the column it is marked 待确认 there too, so confidence and the 9-of-12
count are never told it was cited.

### Settling them: `tracker risks confirm`

Counting an unevidenced obstacle and marking it 待确认 are both honest, and together
they were unsatisfying: nobody had ever gone back to check which reading was right.
This does, one model call per obstacle, worst first.

**With the whole article, not the excerpt.** The excerpt is the fragment chosen by
the extraction that already failed to find the sentence, so re-reading it would
mostly reproduce the first answer; the article comes from the crawl cache. The
project, the obstacle, and *every other obstacle on the row* go with it — half
these rows are `quote_off_target`, a real sentence filed under the wrong category,
and that is invisible without seeing what the other categories already claim.

**What makes the answer trustworthy is not the model.** A returned quote is
accepted only if it is verbatim in the article, checked with the same matcher the
extraction path's evidence gate uses, and only if the sentence carries wording for
the category it is filed under. A paraphrase is refused. A real sentence about the
wrong thing is refused. The obstacle then stays exactly as it was — so the worst
case of running this is that it cost a call and changed nothing.

Three outcomes: **confirmed** attaches the quote and clears the 待确认 mark;
**refuted** marks the obstacle `superseded`, dropping it out of the open counts
without deleting the record of having believed it; **unclear** writes nothing and
is the honest majority answer. `--dry-run` judges at full cost and writes nothing.

`tracker exposure` is the rollup, and it deliberately **does not produce a single
"MW at risk" number**:

```text
open-risk exposure by company
+-----------+----------+-------------+-------------+----------+----------+-------+
| company   | projects | blocking MW | material MW | watch MW | total MW | no MW |
+-----------+----------+-------------+-------------+----------+----------+-------+
| Microsoft |        1 |           0 |         900 |        0 |      900 |     0 |
| Sabey     |        1 |          70 |           0 |        0 |       70 |     0 |
+-----------+----------+-------------+-------------+----------+----------+-------+
```

Collapsing those three columns into one requires deciding how much a `watch` is
worth against a `blocking`, and that is a judgement rather than anything a source
stated. `--weighted` adds the single number for whoever wants it, and prints the
weights it used on the same screen. Projects with an open risk but no cited capacity
sit in their own `no MW` column rather than being averaged in as zero.

Grouping by anything other than `category` counts each project once, in its most
severe open category. Grouping by `category` deliberately does not — a project
obstructed three ways belongs under all three — so that view says so rather than
inviting you to add the column up.

## What changed since I last looked

```bash
tracker watch                          # the companies and projects the digest is about
tracker watch add "xAI"                # or "xAI | Colossus" for one campus
tracker watch add "Meta" --note "Q3"   # the note is shown on the page
tracker watch rm "xAI"
tracker digest --days 1                # what moved yesterday, signed good or bad
tracker digest --notify --days 1        # only what is worth interrupting you for
tracker digest --notify --markdown      # the nightly note, silent on a quiet night
tracker digest --held                  # including what nobody could quote
```

Every other command here answers "what do we know". This one answers "what is
new", which is a different question and needs a different clock. `event_date` is
when a milestone happened, `risk.first_seen` is the date a source puts on an
obstacle, `source.published_at` is when a publisher published — and none of them
is when the row appeared in our database. A crawl reads one article and imports a
project's whole back-history, so stored milestones span 1997 to 2040 while the rows
arrived last night. Migration 0018 added `created_at` to `event` and `risk` for
exactly this, backfilled from each citation's `fetched_at` and left NULL where
nothing ever recorded it.

So the window filters on when we learned something, and every line prints both
dates. `tracker/feed.py` carries the rest of the reasoning: the sign comes from the
vocabulary rather than from a model, a future-dated milestone is a schedule and not
an achievement, and a signal that reaches the milestone a *blocked* track was
waiting for is ranked above everything else — that is
`tracks.ProjectStanding.watch_for` arriving.

With no watchlist at all, this reads the whole database and says so. Add an entry
and it narrows: a watch covers what that company is building **and** what others
are building for it, because in this dataset the interesting news about a
hyperscaler is routinely filed under a developer's name.

**Showing and notifying are different bars.** The page and a plain `digest` carry
everything; `--notify` prints only what `feed.notable` admits, and prints nothing at
all when nothing does, so a scheduled job sends on the nights that earn it. Three
gates: the signal has to be quote-backed (an unconfirmed one never notifies,
whatever it says), it has to have actually happened (a future-dated milestone is a
schedule), and it has to be material — which means the blocker moving, a decisive
milestone, a dated slip, or an obstacle at `material` severity or worse opening or
clearing. An announcement, a filed permit, earthworks and a new row in the tracker
are all on the page and none of them is worth an interruption.

It exits 1 when it printed nothing, so a shell can tell a quiet night from a
failure:

```bash
tracker digest --notify --markdown --days 1 | mail -s "dc-tracker" you@example.com
```

Nothing records what was already sent, and nothing needs to: the window is on when
we learned a fact, so a row falls inside exactly one `--days 1` window and a nightly
job reports it once.

The same reading is the console's landing page, where the watchlist can also be
edited — see `docs/console-and-export.md`.

## What no article says: `tracker infer`

The PRD asks for two things no document contains — 你还需要分析项目可能遇到的困难,
and 接下来出现什么信号，才可以证明项目正在继续推进. Those are analysis, not
extraction, and this is the command that does them.

```bash
tracker infer 2 3        # one call per project
tracker --json infer 2   # the same, as structure
```

```text
#2 xAI — Colossus  Memphis, TN ─────────────────────────────────────────────

What could obstruct this — 可能遇到的困难
  ████· 0.75  financing material
        Only $6B is announced against a planned 2,000 MW buildout; hyperscale
        capex typically runs $10–15M per MW…

What would show it is still moving — 推进的信号
  ████· 0.75  Announcement of a new xAI debt facility earmarked for Colossus
        Would close the financing gap that gates the full buildout.

Inferred by deepseek-v4-flash from this row's 5 recorded obstacle(s), its milestones and
its gaps. Not stored, not evidence — a judgement drawn from the facts.
```

Two questions, two headings, ranked by the model's own confidence with the figure
beside the bar. The previous layout was a four-column table whose widest column was
free prose, which wrapped to two characters a line on a narrow terminal and made
the two questions indistinguishable.

**It cannot write a fact.** Only obstacles and next-signals are accepted; anything
quantitative the model volunteers is dropped and reported, because 关键数字 must come
from a document. That is enforced in code — `infer.INFERABLE` — not by asking the
model nicely.

In the console, every project's drawer has an **Inferred analysis** panel with a
**Run analysis** button. It is a button and not an automatic panel, which is a
deliberate difference from the AI overview above it: the briefing is cached by
content, so a row is paid for once and reopening is free, while an inference is
never cached or stored and its value is that somebody asked for it against the row
as it stands. One click, one call, one answer.

## Slippage is measured, but only where it is unambiguous

When `expected_online` moves later, a `delayed` event records both dates and
`delay_days` lands on the project's most severe open risk:

```text
risks (2)
  transmission material  2026-02-01  +881d
    Two 345-kilovolt upgrades outstanding before full load.

events (1)
  2031-06-01  delayed  expected_online moved from 2029-01-01 to 2031-06-01 (+881 days)
```

`expected_online` keeps its `PREFER_WEIGHT` merge policy — the strongest source
still wins the value, and the movement is recorded as history beside it. Switching
to newest-wins to make slips visible would have discarded the source-quality
ordering everywhere else.

**The number is only attached across a year boundary.** The column stores no
precision, and hedged dates get coarsened into it: a bare `2027` becomes 2027-01-01
and `late 2027` becomes 2027-10-01, so a source simply restating the same year more
precisely is indistinguishable from a 273-day delay. Every coarsening stays inside
the stated year, so a move into a later year cannot be an artefact while a move
within one might be. The event is written either way and says which case it is; only
the unambiguous one is counted.

And no risk is invented from a date change. A slipping date says the timeline moved,
not why.

Every command takes `--db PATH`. Without it the database is `data/tracker.db`
under the project root, or `$TRACKER_DB` if set.
