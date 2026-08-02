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
| Sorting, filtering, expand/collapse | browser (pure display, no judgement) |

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
