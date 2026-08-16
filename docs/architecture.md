# Architecture: CLI → web → back to the CLI

How the pieces fit. Logic, not code detail.

---

## In one line

**The CLI is the system. The console is a face on it.**

The console is not a reimplementation and not a second code path. It does two
things: it reads the database and draws it, and when you press a button it
**starts an actual CLI process** and plays its output back to you.

So the whole thing is a loop, and both ends of the loop are the CLI.

---

## Three parts

```
   ┌──────────────┐
   │     CLI      │   the only thing that writes
   │   tracker    │   fetch, extract, merge, score
   └──────┬───────┘
          │ writes
          ▼
   ┌──────────────┐
   │   database   │   one SQLite file
   │  tracker.db  │   projects / sources / events / risks
   └──────┬───────┘
          │ read-only
          ▼
   ┌──────────────┐
   │   console    │   display and triggering
   │tracker serve │   makes no judgements of its own
   └──────────────┘
```

The console has **two faces on one server**, and the split is about who is
looking:

```
   /                                    /dev
   ┌────────────────────────────┐       ┌────────────────────────────┐
   │ Overview · Projects        │       │ Pipeline · Commands · Help │
   │ Sources · Map · Capex      │       │                            │
   │                            │       │ the queue, the runs,       │
   │ reads the dataset.         │       │ the command palette.       │
   │ nothing here changes       │       │ this is where work         │
   │ anything.                  │       │ happens.                   │
   └────────────────────────────┘       └────────────────────────────┘
              │                                      │
              └──────────────┬───────────────────────┘
                             ▼
                   one bundle, one server
                   window.DC_MODE picks the view set
```

**Each view has its own URL** — `/projects`, `/sources`, `/dev/pipeline` — so a
page can be linked to, refreshed and reached with the back button. The server does
not render them differently; it stamps which view was asked for and the front end
opens on it, then `pushState`s as you navigate. The bundle and the dataset are
already in memory, so a real navigation per tab would be slower than the switch it
replaced. An unknown path 404s rather than quietly serving Overview.

**The mode is a display choice, not a permission.** What `/dev` can do is still
governed by `allow_write` on the server — `serve --no-run` serves the page with
every button inert. A page cannot grant itself a capability by asking for a
different URL, which is the same reasoning as "the gate is checked on the command
name, never on its flags".

Why the reading console exists separately at all: it used to carry eight tabs,
three of which were about running the tool rather than about what it found. That
made the machinery compete with the data for the reader's attention, and the
landing page — a filter card and eighteen columns — answered no question at all.

The split is enforced, not merely intended:

- The **CLI** owns everything that changes data — fetching, calling the model,
  checking evidence, merging conflicting claims, scoring confidence.
- The **database** is the single place a fact lives.
- The **console** reads it and shows it. Its only route to changing anything is
  to start the CLI.

---

## The loop

### Step 1 — how the console knows what commands exist

There is no hard-coded list.

On startup the console asks the CLI: what commands do you have, what flags does
each take, what type is each flag, what is its default, what does its help text
say? The CLI answers, and the console draws forms from the answer.

The payoff is direct: **a flag added to the CLI shows up in the browser on the
next start**, with no second place to remember. And the browser can never offer
a button the CLI would not recognise.

Two things the CLI cannot answer, both short hand-written lists rather than
guesses, because guessing wrong is how someone spends money or loses a row by
accident:

* **Which commands cost money** — `sync`, `enrich`, `infer`, `search`,
  `ingest crawl`, `ingest edgar`. Not visible in a parameter definition.
* **Which commands destroy data** — just `merge`. A separate list, not another
  entry in the first: `merge` spends nothing, and telling an operator it spends
  LLM tokens would be false. Both need the command's name typed back, so there is
  one ritual for two different losses.

Being hand-written has a cost, and the third hand-written list pays it: the
*grouping* of commands into palette sections. Four commands arrived and all four
landed in an unnamed "Other" bucket at the bottom until someone listed them. A
test now fails if any command that is not blocked ends up there.

### Step 2 — what pressing Run actually does

Not a simulation of the command. **A real process**, identical to what you would
have typed.

```
you fill in a form and press Run
        │
        ▼
the console checks the request against the list from step 1
   ├─ unknown command → refused
   ├─ unknown flag    → refused
   ├─ wrong type      → refused
   └─ costs money or deletes rows, and not confirmed → refused
      (checked on the command name, never on its flags —
       a gate that reads arguments is a gate with a bypass in it)
        │
        ▼
it assembles an argument list and starts a process
        │
        ▼
each line of output is pushed to the browser as it appears
        │
        ▼
the process ends; the output is kept as a file, visible under "Runs"
```

One detail carries the security of the whole thing: the console assembles an
**argument list**, never a command string. Each element is one separate
argument, so `;`, backticks and `&&` inside a value are ordinary characters with
no special meaning. At no point is anything you type spliced into a command line.

### Step 3 — how the data gets back

The process writes to the database and exits. The page re-reads and redraws.

That last part is automatic, and getting it right took two attempts. The page
polls for the state of the most recent run and refetches when it has finished —
keyed on the run's **id and status**, not on a running→idle transition. A
transition is only observable if some poll caught the run mid-flight, and a merge
takes about a second against a four-second interval. Measured: the merge
completed between two ticks, nothing reloaded, and the log said three rows were
deleted while the table still showed them. An id that has reached a terminal
status is a fact about the past and cannot be missed.

```
the CLI defines what commands exist
        │
        ▼
the console reads that and draws the interface
        │
        ▼
you press a button
        │
        ▼
the console starts the CLI
        │
        ▼
the CLI writes the database
        │
        ▼
the console re-reads it and redraws
        │
        └──────► back to the start
```

---

## The rule: the browser never re-implements a judgement

This is the most important constraint in the design.

Anything that requires a **judgement** — how far a project has got, what a value
rests on, what the coverage figure is, how much capacity sits behind an obstacle
— is computed by the backend and sent ready-made. The browser draws it.

The reason is not tidiness. The moment the browser computes something itself,
there are two implementations of one rule. They will eventually disagree, and
nothing will tell you when: you will simply see the page and the command line
saying different things and have no way to know which is right.

### A real example

The interface was ported from a design mockup. The mockup contained JavaScript
that worked out a project's progress on its own, including this rule:

> if the project's phase is "operational", the power track is complete.

Reasonable-looking. But the backend **explicitly refuses** that inference,
because building ahead of grid connection is the norm in this cycle, and a
finished shell waiting on a substation is the single most valuable signal in the
dataset. The mockup's shortcut would have erased it silently.

So during the port **that JavaScript was deleted outright** and replaced with the
backend's answer.

The console and the CLI now agree line for line about any project — not because
both were written correctly, but because **only one of them is doing the work**.

### What comes from the backend

| On screen | Computed by |
| --- | --- |
| Five tracks, position, next signal to watch | backend |
| Each field's evidence tier and the sentence behind it | backend |
| Confidence 0–3 | backend |
| Field coverage, and which denominator is honest | backend |
| Capacity behind each category of obstacle | backend |
| Which tranches are the utility's plant, not the campus | backend |
| Why *this* obstacle is the project's blocker | backend |
| Sorting, filtering, expand/collapse | browser (pure display, no judgement) |

The last two are recent and both are the same rule applied twice.

**Serving infrastructure.** `blocks.is_generation` has kept a utility's gas and
solar out of every *sum* since it was written. The tranche list did not know that,
so the page showed Entergy's running gas units among Hyperion's data halls and
added them into its "delivering" figure — while the reconciliation three rows
below correctly excluded them. One name, two numbers. The split is made in
`webui/dataset.py` with the same predicate the arithmetic uses; the page draws two
lists and decides nothing.

**The blocker's rationale.** `blocker` is one sentence chosen from twenty-seven
open obstacles, and the page had no way to say the other twenty-six were
considered. `upsert.blocker_rationale` shares `choose_blocker` with the write
path, so the explanation cannot name a different risk than the column holds — and
when several ranked equally and the tie fell to the lowest row id, it says the
choice was arbitrary. A browser-side copy of that rule would drift from the one
that picked the value, which is the whole objection this section opens with.

---

## Why reads and writes are separated

**On the read side the database is opened read-only.**

Not by convention — the handle itself rejects writes. A bug in display code
raises instead of quietly changing a row.

**On the write side only one thing runs at a time.**

SQLite takes one writer. Two overlapping jobs mean the second dies partway
through, after it has already paid for its model calls. So the console checks for
a running job before starting another and refuses with an explanation, rather
than letting you find out eight articles in.

---

## Three doors

The console can run commands, so three things stand in front of it:

**One deliberate hole in the CSP, and only one.** The page declares
`default-src 'self'` so a stray CDN URL fails loudly instead of quietly
reintroducing a network dependency. The sources page needs an exception: it opens a
cited article in a modal, and `frame-src` falls back through `child-src` to
`default-src`, so without `frame-src https:` the browser refuses the frame
outright. That directive is the only addition — scripts, styles, fonts, images and
`connect-src` stay same-origin, so it widens what the page may *display* and
nothing it may load or call. A test pins the rest shut.

It buys less than it appears to, and measurement decided how the modal is built.
Across the fifteen most-cited publishers **ten refuse to be framed**, carrying 388
of their 689 citations — `datacenterdynamics.com`, the most-cited of all, among
them. No header of ours overrides theirs.

So the frame usually holds **our own reader view**, not the publisher's page: the
readability algorithm over their HTML, rendered under our stylesheet, served
same-origin from `/api/article`. Their live page is still one tab away for the
publishers that permit it, which is why the directive lists both. `'self'` is
listed explicitly and has to be — naming `frame-src` at all replaces the fallback
chain to `default-src`, so `frame-src https:` alone forbade our own frame.

**Readability finds the article; it does not tidy it.** It ranks by text density,
which is what makes it work on any publisher, and is indifferent to what shares a
container with the prose. So two passes bracket it: containers named as furniture
are removed before scoring, and the seams are trimmed after — a "Related:" line
inside the prose, a press release's contact block, a legal disclaimer, a stop
heading like "Frequently Asked Questions" that ends the article and everything
after it.

The rule that keeps this honest is a kill criterion fixed before the pass was
written: **it must not cost a single marked quote**. That is what a measurement
can settle, and it caught the failure that eye-checking would not — a WordPress
`<body class="… no-sidebar">` matched the `sidebar` rule, deleted the whole
document, and reported success on three publishers. Nothing structural is name-
matched now, and neither is any container holding most of the page's prose,
because chrome is never most of what a page says.

That inversion is not a retreat. A live page may have been edited since it was
cited, and what the reader came for is not "the page" but *which sentence* carried
a field — so the stored quotes are marked in the text. Locating them is
normalisation and folding, the evidence gate's own judgement, so it happens on the
server; the browser renders and decides nothing, which is the rule this document
opens with.

**Rendering somebody else's markup gets three independent guards.** The HTML is
sanitized to an attribute allowlist, so `on*` handlers and whatever nobody thought
of go together. The frame carries `sandbox` with no `allow-` tokens — it loads
same-origin, so without that the document could script the console; with it the
document has an opaque origin and cannot script at all. And the response carries
its own `default-src 'none'`, images excepted. Any one would do.

**The endpoint reads a page the pipeline chose, and nothing else.** Its allowlist
is the database: a URL that is not a stored `source.url` is refused. Without that
rule a read-only console is a request forwarder aimed at whatever network it runs
on, and "it only reads" says nothing about where it may be pointed.

**The bind address.** Loopback by default. If you can reach localhost you are
already at the machine, and a password there would protect nothing.

**A password.** The moment it is published — a tunnel, a proxy — the first door
stops working, because a tunnel connects from the local machine and so every
request looks local. Publishing therefore *requires* a password; without one the
command refuses to start. Before signing in, the entire site is a login page —
not even the frontend code is served.

**Confirmation.** The five commands that spend money need their name typed
before they run. This door is not for attackers; it is for slipped clicks.

---

## Two web pages, not one

Easy to confuse:

| | `tracker export html` | `tracker serve` |
| --- | --- | --- |
| What it is | a file | a server |
| Data | frozen at export time | re-read every request |
| Can run commands | no | yes |
| Can be emailed | yes, opens by double-click | no |
| Needs anything running | no | yes |

Both are kept because they answer different needs: one is **something to send
someone**, the other is **somewhere to work**.

---

## The whole picture

```
                       ┌───────────────────────────┐
                       │  CLI: tracker             │
                       │                           │
   feeds / news ──────►│  discover  find candidates│
   ISO queue files ───►│  crawl     fetch+extract  │
   Census data ───────►│  geo       derive geo     │
   the model    ◄─────►│  enrich    exhaust one    │
                       │  ...                      │
                       └────────────┬──────────────┘
                                    │ writes (one at a time)
                                    ▼
                       ┌───────────────────────────┐
                       │  tracker.db               │
                       │  projects·sources·events  │
                       └────────────┬──────────────┘
                                    │ read-only
                                    ▼
   ┌────────────────────────────────────────────────────────┐
   │  console: tracker serve                                │
   │                                                        │
   │  ① read the CLI's definitions ──► draw the palette     │
   │  ② read the database          ──► draw the views       │
   │  ③ you press Run              ──► validate, spawn ─────┼──┐
   │  ④ replay its output          ◄── line by line         │  │
   │  ⑤ re-read the database       ──► redraw               │  │
   └────────────────────────────────────────────────────────┘  │
                                    ▲                          │
                                    └──────────────────────────┘
                                         back to the CLI
```

---

## Summary

The browser took nothing away from the CLI. It **reads** the CLI to build an
interface, **starts** it when you ask, and **reads back** what it did.

One place makes the judgements. One path writes.
