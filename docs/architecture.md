# Architecture: one writer, three faces

How the pieces fit. Logic, not code detail.

---

## In one line

**The CLI is the system. The console and the TUI are faces on it.**

Only the CLI writes. The console reads the database and draws it; the TUI reads
the same database, draws it in a terminal, and can also *start* CLI commands
because it runs on the machine that owns the data.

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
   │  tracker.db  │   projects / sources / events / risks / accounts
   └──────┬───────┘
          │ read-only
          ▼
   ┌──────────────┐
   │   console    │   display, and one preference
   │tracker serve │   makes no judgements of its own
   └──────────────┘
```

**The console reads.** Six views — Updates, Projects, Sources, Map, Capex, Help —
and it cannot change a project, a citation or a figure. There is exactly one
exception, `POST /api/watch`, and the section below is about why it is allowed.

**Each view has its own URL** — `/projects`, `/sources`, `/help` — so a page can
be linked to, refreshed and reached with the back button. The server does not
render them differently; it stamps which view was asked for and the front end
opens on it, then `pushState`s as you navigate. The bundle and the dataset are
already in memory, so a real navigation per tab would be slower than the switch it
replaced. An unknown path 404s rather than quietly serving Updates.

### It used to run commands, and no longer does

There were two faces on one server: `/` for reading and `/dev` for working — a
command palette built by introspecting the CLI, a run streamer, a real subprocess
per button. That is gone.

The reasoning is not that it was badly built. It worked, and three doors stood in
front of it: a typed-name confirmation for anything that spends money or deletes
rows, a single-writer check because SQLite takes one writer, and the rule that the
console assembles an **argument list** and never a command string. All three were
correct.

The reasoning is that **nobody used it**. The database is changed from the CLI, by
one person, on the host — that is what `CLAUDE.md` has said all along. So the
runner was three security properties that had to stay correct forever, behind a
public URL, in exchange for a feature with no users. Deleting it removes the whole
class of question rather than answering it again each time the page changes.

`tracker tui` is where the buttons live now, and it is the better home for them:
it runs in a terminal on the machine that owns the database, so "who may start
this?" is answered by ssh rather than by a cookie. It still shares
`webui/catalog.py` and `webui/runner.py` with what used to be here — the
introspection and the process handling were never the problem — which is why
those modules survive a change that deleted their only HTTP caller.

---

## Accounts, and the one write

### Why there are accounts at all

The console was gated by one shared password. That made every reader the same
principal, and it had a consequence beyond authentication: **the landing page
could only ever draw one watchlist.** With no way to tell two people apart, "the
things I am watching" was not a sentence the data could express, so `watch` was a
property of the database rather than of the reader.

`tracker users add` creates an account — an email, a password hashed with
`scrypt`, and nothing else. Every account can do exactly what the shared password
allowed, which after the change above is: read the dataset, and keep a watchlist.
There are no roles, because there is nothing left to have a role *about*.

**Zero accounts is a legitimate state and means an open console.** That is what a
fresh install is in, and it is right for loopback: reaching 127.0.0.1 already
means having the machine. What refuses is *publishing* — `serve --tunnel` will not
put a page with no way to gate it on the open internet, exactly as it refused
without a password before.

Creating the first account changes what a running console does, within seconds and
without a restart. The server counts rows rather than reading a flag it was given
at startup, because `tracker users add` runs in a **different process** and a flag
read once would leave a published console open until somebody noticed.

### Getting an account without a terminal

Two routes, and neither is open registration. Behind a tunnel the login page is a
public URL, and while an account can no longer run a command it can still read the
whole dataset.

- `tracker users add` — at a terminal, prompting for the password.
- `tracker users invite` — mints a single-use code, printed once. The holder
  redeems it on the console's own sign-in page and chooses their own email and
  password. That is the one worth using when you are not the person who will be
  typing the password: a password you picked and sent them is a password in a chat
  log.

The code is stored as a sha256 and never in the clear, because this database
travels between machines through `scripts/sync_db.py` and sits in `backups/` —
a plaintext code in it would be a live credential in every copy.

### The one write, and why it does not break the split

`POST /api/watch` adds or drops a row of `watch` — a statement about whose news the
Updates page should show, for the account making the request. Nothing derives from
it, no ingest consults it, and losing the table would lose a preference rather than
a fact.

It has its own flag, `--no-watch-edits`, for the same reason `--ai` is separate:
these are different risks, and collapsing them cost the console the one thing it
could safely offer. What the flag does *not* do is loosen anything else — a
watchlist edit cannot touch a project, a citation or a figure, and it cannot spend
a token.

It needs an **account**, not merely a session. An anonymous visitor to an open
console has no list to edit, because a watchlist without an owner is the shared
list that accounts exist to replace. They get the whole-database digest instead,
which is the same "no watchlist, so read everything" path `tracker digest` has
always had for an empty list.

### The terminal reads across everybody

`tracker watch` and `tracker digest` default to **every** account's entries,
because a terminal on the host is looking at the database rather than at one
person's slice of it; the listing carries an owner column so the rows stay
distinguishable. `--user alice@example.com` narrows either to one person, and
`digest --user` is what reproduces exactly the page that person sees — the form to
schedule if the nightly note is going to *her*.

Writing is the other way round: `watch add` and `watch rm` **require** `--user`.
Reading everybody's list is useful; writing without naming an owner would put an
entry on somebody's page that they did not ask for, and there is no shared list to
fall back on.

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
| Whether this reader may edit a watchlist | backend |
| Sorting, filtering, expand/collapse | browser (pure display, no judgement) |

Three of those are worth naming, because each is the same rule applied twice.

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
choice was arbitrary.

**`allow_watch`.** It answers "may *this reader* edit a watchlist", not "is the
feature enabled" — so it folds in the `--no-watch-edits` flag *and* whether anybody
is signed in. Sent the same way by `/api/dataset` and `/api/updates`, because
whichever one the page happened to believe would otherwise decide, and two answers
to one question is the failure this whole section is about.

---

## Why reads and writes are separated

**On the read side the database is opened read-only.**

Not by convention — the handle itself rejects writes. A bug in display code
raises instead of quietly changing a row. Every route except `POST /api/watch`,
`POST /api/login` and `POST /api/register` opens it `mode=ro`.

The three that do not are as narrow as their jobs: one row of `watch`, one
`last_seen_at` stamp on a successful sign-in, and one `account` row created by
spending an invite.

**On the write side only one thing runs at a time.**

SQLite takes one writer, and a `tracker` command holds a lock file for the hours a
crawl runs. The watchlist write deliberately does **not** take that lock: it is a
rule about derived data, which a `watch` row is not, and blocking somebody from
changing which companies they are told about because tonight's crawl is still
running would be a worse answer than letting the two interleave. SQLite's
`busy_timeout` covers the contention.

---

## Three doors

The console is a reader, so the doors are about who may read and how fast anyone
may knock.

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
already at the machine, and a password there would protect nothing. `--allow-remote`
is required for anything else, because what is behind the port is the whole dataset
and — with `--ai` — a model panel that spends real tokens per click.

**Accounts, and a rate limit.** The moment the console is published — a tunnel, a
proxy — loopback stops meaning anything, because a tunnel connects from the local
machine and so every request looks local. Publishing therefore *requires* an
account; without one the command refuses to start. Before signing in, the entire
site is a login page — not even the frontend code is served.

What makes a short password safe is not its length, it is the rate: eight failures
lock one client out for fifteen minutes, and forty across *all* clients close the
gate for fifteen minutes. The second counter is the one that matters behind a
tunnel, where an attacker with a thousand addresses would otherwise get a thousand
budgets. Nothing is counted per email — that would let anyone who knows an address
lock its owner out.

Sessions are random server-side tokens in an `HttpOnly; SameSite=Lax` cookie,
holding no claim the server has to trust. They live in memory, so a restart signs
everybody out; the deployer restarts this process on every commit, so that happens
often and is not worth engineering around.

---

## Two web pages, not one

Easy to confuse:

| | `tracker export html` | `tracker serve` |
| --- | --- | --- |
| What it is | a file | a server |
| Data | frozen at export time | re-read every request |
| Per-reader watchlist | no | yes |
| Can be emailed | yes, opens by double-click | no |
| Needs anything running | no | yes |

Both are kept because they answer different needs: one is **something to send
someone**, the other is **somewhere to read**.

And if what you want is the commands, that is a third thing: `tracker tui`.

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
                       │  users     who may read   │
                       └────────────┬──────────────┘
                                    │ writes (one at a time)
                                    ▼
                       ┌───────────────────────────┐
                       │  tracker.db               │
                       │  projects·sources·events  │
                       │  account·watch            │
                       └──────┬─────────────┬──────┘
                     read-only│             │read-write
                              ▼             ▼
        ┌──────────────────────────┐   ┌──────────────────────┐
        │ console: tracker serve   │   │ tui: tracker tui     │
        │                          │   │                      │
        │ sign in, read the data,  │   │ the same data, plus  │
        │ keep your own watchlist  │   │ the commands, on the │
        │                          │   │ machine that owns it │
        └──────────────────────────┘   └──────────────────────┘
```

---

## Summary

One place makes the judgements. One path writes.

The console is a reader with accounts: it draws what the backend decided, and the
only thing it can change is which companies *you* are told about. The commands
moved to the terminal, where the person who runs them already is.
