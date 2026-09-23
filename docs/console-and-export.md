# The console, and exporting

One page for the whole dataset, the live console and its two modes, driving it without a browser, and publishing it.

Part of the [dc-tracker documentation](README.md).

---

## The whole dataset as one page

```bash
tracker export html --out data/exports/tracker.html
```

One self-contained file: open it by double-click. Sortable table over the 12
fields, a five-segment track strip per project so stage is scannable down the
column, filters for state / phase / blocked-on / confidence / quoted-only, a panel
per project with its citations and milestones, capacity-behind-an-obstacle bars,
and a coordinate plot. It is one file with everything inlined, so it does its own
filtering — the paging the live console gained has no meaning for a document that
is already wholly in memory.

**No network requests at all** — no CDN, no webfont, no map tiles — so it works
offline and survives being emailed. The dataset is inlined rather than fetched from
a sibling `.json` on purpose: `fetch()` from a `file://` page is blocked as
cross-origin, so a two-file build would open to an empty table unless the reader
happened to be running a web server.

Two deliberate choices worth knowing. **Colour means trust, never category** — the
five tracks get a neutral ordinal ramp, and every hue is reserved for how much to
believe a value (amber = 待确认, blue = inferred) or for something being wrong
(rose = a blocked track). And the header strip is a **provenance ledger** that
recomputes on every filter, so the trust composition of whatever you are looking at
is always visible rather than something you have to go and check.

The coordinate panel is a plot, not a map: there is no coastline because there is no
boundary data here, and drawing one would be illustration rather than reporting.
Positions are city centres, not sites.

## The console: the same dataset, live, one reader at a time

```bash
tracker serve
```

## One console, and it reads

Six views — **Updates**, Projects, Sources, Map, Capex, Help — each on its own
URL, so a page can be linked to, refreshed and reached with the back button.
Sources opens any cited article in a reader view. Nothing here changes a project,
a citation or a figure.

**And a page per project, at `/projects/<id>`.** That used to be a drawer, and it
was the wrong shape for two reasons. It could not hold the data: 1040px with four
tabs, so most of what the database knows about a campus sat behind a click and
the visible part was squeezed into 330px columns. And it read as temporary,
because it had no URL — the one thing in the console you could not link to was
the thing the console is about. The rule the six views are held to is stated in
their own test: *"a page you cannot link to, refresh or reach with the back
button is a tab."*

The page reads one project fresh from `GET /api/project?id=<id>` rather than out
of the list payload, which buys it the per-field claim tables — those are 48% of
the list payload and are deliberately left out of it. Tabs became sections on one
page, so a reader can see a campus's capacity and the obstacle blocking it at the
same time, with a sticky jump nav to skip rather than to hide.

### The server answers the questions; the browser does not download the database

Everything the console shows used to arrive in one response, and the browser then
searched, filtered, sorted and paged it itself. That works until it doesn't:
measured on a 437-project fleet the response was **4.8 MB**, all of which had to
land and parse before the first row could be drawn, and it grows with the
database.

Now the table asks. `GET /api/projects?q=…&sort=…&offset=…` returns thirty rows
and a count for the whole filter; scrolling asks for the next thirty. The shell
payload keeps a **light index** of every project — identity, the headline
figures, open obstacles — which is what the two map components read and what the
watchlist picker searches. Capex and the citations list each fetch their own
data when their view opens: the rollup alone was 304 ms of the old payload's
406 ms, paid by five views that never draw it.

| | before | after |
|---|---|---|
| first screen, 437 projects | 4,825 KB raw / 114 KB gz | **390 KB raw / 17 KB gz** |
| server time for it | 551 ms | **73 ms** |

**Two rules the front end is held to while it waits.** A pending request must
never look like an answer: an empty table means "nothing matches", dimmed rows
mean "this is last second's answer", and skeleton rows mean "we have not been
told yet". And the indicator starts at the keystroke rather than at the request,
so the quarter-second search debounce is visibly deliberate rather than a dead
control.

Sorting refetches from the first page rather than reordering what is loaded —
sorting thirty of four hundred rows and presenting it as the ranking is a lie
with nothing on screen to catch it. And "quoted only" is the one filter the
server cannot express in SQL, because a value's tier is derived from its sources
rather than stored; it is computed once and memoised against a fingerprint of the
data. See `tracker/webui/query.py`.

Its timeline is five lanes on one time axis rather than a list, because the five
tracks run in parallel and **the gap between two dates is the signal** — a list
draws every gap the same height. Colour there carries state (reached, implied,
blocked) and never track identity, which the lane labels already carry.

Long lists are capped at a dozen rows with an exact count and an in-place "show N
more". Measured on a project with 70 citations: that section alone was 14,660px —
fifteen screens, two thirds of the page — and a reader scrolling past it had no
way to know how much was left.

There used to be a second face at `/dev` carrying Pipeline, Commands and Runs: a
palette built by introspecting the CLI, and a real subprocess per button. **It is
gone**, and not because it was broken. The database is changed from the CLI, by
one person, on the host, so the runner was three security properties that had to
stay correct forever — a typed-name confirmation, a single-writer check, and
argv-never-a-string — behind a public URL, for a feature nobody used.

`tracker tui` is where the commands live now, and it is the better home: it runs
in a terminal on the machine that owns the database, so "who may start this?" is
answered by ssh rather than by a cookie. See [the terminal interface](tui.md).

## Accounts

```bash
tracker users add you@example.com     # prompts for a password
tracker users                         # who exists, and how much each watches
tracker users invite --note carol     # a single-use code, printed once
tracker users passwd you@example.com
tracker users rm you@example.com      # takes their watchlist with it
```

The console used to have one shared password. That made every reader the same
principal, and the cost was not only authentication: **the landing page could only
ever draw one watchlist**, because with no way to tell two people apart "the
things I am watching" was not a sentence the data could express.

**Zero accounts means an open console**, which is what a fresh install is in and
is right on loopback — reaching 127.0.0.1 already means having the machine. What
refuses is publishing: `serve --tunnel` will not put a page with no way to gate it
on the open internet. Creating the first account closes the gate on a *running*
console within a few seconds, without a restart.

**A console that is already published never opens.** Behind a tunnel every
request arrives from 127.0.0.1, so the loopback argument is simply false there,
and the console requires a sign-in for as long as it runs whatever the account
count. Deleting the last account therefore refuses everyone, and the sign-in form
says `tracker users add` is the fix. It used to do the opposite: the check that
refused to publish ran only at startup, so `tracker users rm` of the last account
put the whole dataset on the public URL within five seconds, and the CLI reported
that the console was "open again".

There is no open registration. Behind a tunnel the login page is a public URL, and
while an account cannot run a command it can still read the whole dataset. So an
account is made either at a terminal or by redeeming a code that was minted at
one — `tracker users invite` prints it once, stores only its sha256, and the
holder chooses their own email and password on the sign-in page. Use the invite
when you are not the person who will be typing the password: a password you picked
and sent them is a password in a chat log.

Nothing here is a role. Every account can do exactly what the shared password
allowed, which is now: read the dataset, and keep a watchlist.

**A session lasts only as long as its account does.** Sessions live in the
console's memory and `tracker users` runs in another process, so each request
re-checks its session against the account row, at most once every five seconds
per session. `tracker users rm` and `tracker users passwd` therefore end that
account's open sessions within seconds, on every route, with no restart — and so
does SQLite handing a deleted account's id to the next account created, which it
does. Before this only the landing page's data route looked the row up, and a
deleted account went on reading every other route for the rest of its twelve-hour
session.

**The landing page answers one question: what changed on what I am
watching.** Two pages have held this slot. The first opened on the projects
table — filter card, coverage strip, eighteen columns, all at equal weight before
you had read a number. The second asked "can these numbers be quoted?" and
answered it well, but it described the *dataset*, and a reader arriving in the
morning is not asking about the dataset. Projects already carries the inventory
and carries it better.

**Updates** is a list of what moved, signed good or bad, most material first, for
the companies and projects on a watchlist. The vocabulary does the signing: an
`energized` event is good news and a `delayed` one is bad, an obstacle opening is
bad and the same obstacle clearing is good — all four are closed enums, so the
sign is a lookup rather than an opinion. `tracker/feed.py` has the reasoning, and
three parts of it are worth knowing here:

* **The window is on when *we* learned a fact.** A crawl reads one article and
  imports a project's whole back-history, so stored milestones run from 1997 to
  2040 while the rows themselves arrived last night. Filtering on the milestone's
  own date would report 2022 every morning. Every line therefore carries both
  dates — "energized (2024-09-01, learned 2026-08-11)" — because either one alone
  is a lie in one direction.
* **A future-dated milestone is a schedule, not an achievement.** "Full Phase 1
  *expected* online 2028" is marked as expected, scores lowest, and never counts
  as the blocker moving. This is the same trap `tracks.standing` filters with
  `as_of`, and it bit here too before the real database was pointed at the page.
* **A signal that reaches the milestone a *blocked* track was waiting for leads
  the page.** Power blocked, then an interconnection agreement signed: that is
  `ProjectStanding.watch_for` arriving, and it is the single most informative
  thing this dataset can say.

Signals whose evidence the gate could not confirm are held in their own tray,
counted separately, never mixed in — the console's standing rule is that a model's
answer is not a fact, and a briefing is the last place to abandon it.

**Showing and notifying are different bars.** Lines marked *would notify* are what
a nightly `tracker digest --notify` sends: the blocker moving, a decisive milestone,
a dated slip, or an obstacle of material severity opening or clearing. Everything
else is there to be read. The count beside the window control toggles the page down
to just those, which is how you check what the schedule would have sent without
waiting for it.

**The watchlist is editable here, and it is the only thing on the console that
writes.** A `watch` row says whose news to show: nothing derives from it, no
ingest reads it, and losing the table would lose a preference rather than a fact.
It has its own flag — `serve --no-watch-edits` — because these are different risks
from spending a token and deserve different switches.

**It is your list, not the database's.** Each account keeps its own, so two people
reading the same console get two different landing pages, and neither can drop the
other's entry. A visitor to a console with *no* accounts has no list to keep —
there is nobody to own one — and gets the whole-database digest instead, which is
the same fallback an empty watchlist has always had.

The same rows are `tracker watch` on the machine that holds the database, where
the view is deliberately the opposite: bare `tracker watch` reads **every**
account's entries with an owner column, because a terminal on the host is looking
at the database rather than at one person's slice of it. `--user alice@example.com`
narrows it, and `tracker digest --user alice@example.com` reproduces exactly the
page alice sees — the form to schedule if the nightly note is going to her.
Writing requires `--user`: reading across everybody is useful, but writing without
naming an owner would put an entry on somebody's page that they did not ask for.

The box that edits it is a picker rather than a text field, over the three shapes a
watch has: an **operator** (everything it builds), a **tenant** (what others build
for it) and a **project** (one campus). Every candidate comes from the dataset the
page already holds, so an empty box offers the largest operators instead of
demanding you know what the database calls things; an entry already covered is
shown as *watching*, sorted last, and skipped by the arrow keys. Text that matches
nothing is still accepted — a watch set before the project is tracked starts
reporting when the project appears. Clicking a chip narrows the list to that watch.

The header carries the last citation fetch, and complains after two days. A crawler
that died on Tuesday and a genuinely quiet week look identical on a page like this,
and only one of them is good news.

The evidence census and tier sweep the previous landing page led with have not gone
anywhere: they are `tracker stats` and `tracker clean`, which is where they were
computed from all along.

**Everything explanatory folds.** Each view opened with a paragraph that is useful
once and furniture thereafter — the caption and heading stay, the prose sits behind
"what is this?". On Projects the coverage strip and the seven-swatch provenance key
fold too; both were permanent furniture above the table. The filter card collapses
at every width, with a count on the button so a hidden active filter cannot
mislead you.

## Driving the console from outside a browser

```bash
curl -s localhost:8765/api | python -m json.tool
```

`GET /api` is a hand-written index of every route: what it answers, what it
reads, whether it writes, and what it costs. It exists because this console is
driven from a terminal about as often as from a browser, and "what can I ask this
server?" previously had no answer short of reading `_route_get`.

Hand-written for the same reason `catalog.GROUPS` is — a derived list describes
the code, and a caller needs to know what a route is *for*. A test compares it
against the routes the handler actually dispatches and fails if one is
undocumented, which is what stops it rotting; it caught six on the first run.

The three worth knowing:

| route | answers | cost |
|---|---|---|
| `GET /api/dataset` | a light index of every project, plus gaps, queue, exposure, totals | ~275 KB at 437 projects, refetched after each run |
| `GET /api/projects` | one page of the table, filtered and sorted by the server | 30 rows; `total` counts the whole filter |
| `GET /api/capex` | capacity by the company buying it | ~0.4 s on a copy of production (it was ~1.0 s and 2,031 statements); its own route for that reason |
| `GET /api/articles` | publishers, and one publisher's citations when asked | counts at rest; `?host=` for the list |
| `GET /api/updates` | what changed on the watchlist, signed and ranked | one pass over projects, events and risks |
| `POST /api/watch` | adds or drops a watchlist entry | **the only write there is** |
| `POST /api/login` | exchanges an email and password for a session cookie | — |
| `POST /api/register` | spends an invite code and creates the account | — |

`POST /api/run` is gone, along with `/api/runs`, `/api/commands` and
`/api/discover`. They 404 rather than 403: there is no runner to refuse.

`POST /api/watch` acts on the signed-in account's list, so it needs an account and
not merely a session — an anonymous visitor to an open console gets a 403 saying
so rather than a list with no owner.

Opens `http://127.0.0.1:8765/`. Six views — **Updates**, Projects, Sources, Map,
Capex, Help — reading the database on every request, so it reflects what the last
run did without re-exporting anything. Reload to pick up a run that finished while
you were reading; nothing on the page can start one.

**Different from `tracker export html`, and both are worth having.** The export is
one self-contained file you can email; it is frozen at the moment it was written.
The console is a server: live, reading the database every time it is asked.

Hovering any value shows the sentence behind it. That works because the evidence
gate's per-field quotes are now stored (`source.quotes`, migration 0007) rather
than collapsed into one excerpt — see "Provenance is per field, not per source"
below. Citations recorded before that migration fall back to the source excerpt
and the page says so rather than passing a paragraph off as the sentence behind
one number.

**It runs no commands.** The Commands and Runs views went with the runner (see
above), and later so did the six routines — named sequences such as `sync` →
`ingest geo` → `logic check`, run as one job — whose module had outlived its only
caller and was reachable from nothing but its own tests. The sequences that matter
are scripts now, each stating why its order is what it is:
[`scripts/settle.sh`](../scripts/settle.sh), [`resolve.sh`](../scripts/resolve.sh)
and [`overnight.sh`](../scripts/overnight.sh). Single commands run from
`tracker tui`.

**The AI overview** on each project page is the one thing in the console that
is a *reading* of the values rather than one of them. It is a card in the
figures section's ordinary flow, under the figures it is a reading of — it was
briefly pinned
above the tab strip, which made it the one block you could not scroll past. It generates
when you open the row and streams as it is written, and it is cached by content —
so a row is paid for once, and reopening it is free until something about the row
actually changes. It is never stored, never becomes a source, and cannot move
confidence. See `overview.py`.

It is written by `fast_extractor` — `TRACKER_DEEPSEEK_FAST_MODEL` with **reasoning
disabled**, and it is now the *only* tier that does not reason. That is
deliberate, and it is the one place in this tool where speed beats depth: the
briefing is a reading of values already on the page, it is labelled as a model's
opinion, it is never stored, it never becomes a source, and it cannot move
confidence. Nothing it writes reaches the database. Extraction and `infer`, which
do write, both reason.

**The latency here is a thinking question, not a model question**, and that took a
provider migration to be able to act on. Measured across MiniMax's whole roster on
this prompt, time to the first *visible* word ranged from 12.4s to 46.6s — and
`MiniMax-M3` was worse than slow, spending the entire completion budget thinking
and returning an empty briefing. Tokens spent inside `<think>` are invisible, so a
model that streams instantly and then deliberates is not fast; only removing the
reasoning moved the number. On MiniMax the only way to remove it was to pick the
one model that could not think (`M2-her`, 2.7s) — `thinking`, `reasoning_effort`
and `enable_thinking` were all accepted by that API and all ignored, and an
assistant prefill of `</think>` did not suppress it either.

That workaround had a measured cost. `M2-her` is built for dialogue and sometimes
read the data wrong: on Fairwater — construction track `nothing reached`, every
other track passed — it wrote *"All tracks complete; construction the last to
finish"*, inverting the most informative field in the row. It also named a utility
and a permit process that appear nowhere in the data.

DeepSeek honours `thinking: {"type": "disabled"}`, so the fast path is now the
same model as everything else with reasoning switched off at request
time, and **that accuracy trade is gone** — the role is unchanged, but it is no
longer paid for with a worse model. Any reasoning that does arrive is still
stripped as it streams, so it never reaches the page.

One guard survives the move and earns its place:

* the prompt asks for an `[[END]]` sentinel, and `overview.RUNAWAY` cuts the
  **stream** there, or at the point the model starts a second answer. Cutting the
  stream rather than the finished text is what saves the time: abandoning the
  generator closes the connection, so tokens after the answer are never waited
  for. Left alone, `M2-her` wrote 756–982 words against a 110-word instruction,
  repeated itself under headings like "Final answer (last round)", and narrated
  its own word count. "The model stops when asked" is not a property worth
  assuming of a provider on the strength of not yet having seen it fail.

`MODEL_TOKEN_CAP` also survives, empty. It clamped the budget to the 2048 `M2-her`
accepted — without it every request was an HTTP 400 and there was no briefing at
all. The v4 models take 384K, far above anything asked for here, so today it is a
no-op guarding a hazard that has happened once.

The reply is markdown — one sentence, then two or three bullets — rendered to
React elements by a small parser in `app.js`. Deliberately **not** `innerHTML`:
this text is written by a model out of articles fetched from the open web, which
makes it the least trustworthy string in the product, and turning it into markup
would run a path from someone else's page into a signed-in session on the console.
Links are flattened to their text for the same reason. Verified by feeding the
panel a briefing containing `<script>` and an `onerror` attribute: both render as
characters, nothing executes.

What bounds the console is short now. **The bind address**: loopback only, and
`--host` anything else is refused without `--allow-remote`, because anyone who
can reach the port could read the whole dataset and — with `--ai` — spend LLM
tokens a panel at a time. It cannot start a command at all. The runner is gone
from here, and with it the checks that stood in front of it — a typed
confirmation before anything that spends or deletes, and flags validated into an
argument list against the CLI's own catalog. Both still guard `tracker tui`,
which runs commands through the same executor; see
[the terminal interface](tui.md).

`--ai/--no-ai` governs the panels that call a model — the project briefing,
`infer`, the capex overview. They *read* a row and spend tokens, and `tracker
infer` has never written its answer anywhere, which is why spending was always its
own switch rather than a consequence of some other one. `serve --no-ai` is for a
console you would rather nobody could run up a bill on.

**No network requests at all** — React, d3, three.js, Lucide, the Census boundary
file and all three webfonts are vendored under `tracker/webui/static/vendor/`
(3 MB), and the server sends a `default-src 'self'` CSP so a CDN URL creeping back
in fails loudly instead of quietly reintroducing the dependency. The front end has
no build step and the repo has no `package.json`: React is a UMD global, the
Meridian component bundle is already compiled, and `htm` supplies JSX-like
templates from tagged template literals.

**The mark**, beside the wordmark in the header, on the sign-in card and in the
tab, is a citation bracket whose bar stops where the evidence stops — the empty
half of the bracket is this project's one rule drawn literally: a figure nobody
published stays null rather than guessed. It is 168 bytes of inline SVG filled
from `currentColor`, which is what lets a single copy serve both themes (`--primary`
is honey `#a05e1c` on cream and `#dca75f` on espresso) at no request. The favicon
is the same two paths with **no background plate** — a honey tile would sit lit in
dark browser chrome, whereas the bare mark takes whatever the tab strip is. It is
drawn full-height on its 24-unit grid and aligned by baseline rather than centred,
because `dc-tracker` has no descenders and centring the mark on the line box drops
it 3.5px below the wordmark's optical middle.

## Putting the console on the internet

```bash
tracker cloudflare --check     # is everything in place?
tracker cloudflare             # publish it
```

The console still binds loopback. `cloudflared` makes an **outbound** connection
to Cloudflare and relays traffic back down it, so nothing is opened on your
network and no router or firewall changes are involved. `tracker serve --tunnel`
is the same thing in one flag; the command exists because publishing deserves a
readiness check and a second shape.

**Two shapes.** A *named tunnel* is one you created once on your own account: the
hostname is yours and survives a restart, which is what you want if the link is
going to anyone else — re-sending a fresh URL every session is how one ends up
written down somewhere it should not be. A *quick tunnel* is anonymous: a random
`https://<four-words>.trycloudflare.com`, no Cloudflare account, and a different
URL every session. **A quick-tunnel URL cannot be preserved across a restart**;
that is Cloudflare's design, not a missing flag.

Creating the named tunnel is deliberately left to you. It writes credentials into
your home directory and a DNS record into your zone, both of which outlive the
process:

```bash
cloudflared tunnel login
cloudflared tunnel create dc-console
cloudflared tunnel route dns dc-console console.example.com
```

Then record it once, in `.env`, and publishing takes no arguments:

```
TRACKER_TUNNEL_NAME=dc-console
TRACKER_TUNNEL_HOSTNAME=console.example.com
```

```bash
tracker cloudflare          # the configured tunnel; same URL every time
tracker cloudflare --quick  # a throwaway URL instead
```

These are settings rather than flags retyped every session because they describe
the machine, not the run. `--name` and `--hostname` override them **together**:
an explicit `--name` will not inherit the configured hostname, because printing a
real hostname beside a different tunnel produces a URL that looks right and points
at the wrong thing. `serve --tunnel` uses the same pair, so the two ways of
publishing cannot land on different URLs.

Restarting to pick up new code is then just Ctrl-C and the same command. The
console re-reads its static files from disk on every request and stamps every
asset URL with that file's version, so a restart genuinely replaces the front end
— a browser or a CDN edge cannot keep serving the previous one, because a changed
file is a different URL. Only the Python process needs the restart.

**At least one account is required for either shape** and the command refuses to
start without one — `tracker users add you@example.com` — and a published console
keeps requiring one: delete the last account while it runs and it refuses every
sign-in rather than opening. A quick-tunnel hostname
is random but **not secret** — it goes over the wire and Cloudflare knows it — so
it is obscurity, not access control. The sign-in, the per-client and global
lockouts, and the fact that nothing here can start a command are the access
control. The console never sees the tunnel: cloudflared connects to it over
loopback, which means the "refuse a non-loopback bind" check never fires and the
sign-in is what replaces it.

What makes a short password safe is not its length, it is the rate: eight failures
lock one client out for fifteen minutes, and forty across *all* clients within any
fifteen minutes close the gate for fifteen more. The second counter is the one that
matters here, where every request arrives from 127.0.0.1 and an attacker with a
thousand addresses would otherwise get a thousand budgets. Signing in successfully
forgets only your own failures, never the shared count — otherwise anybody with an
account could buy unlimited guesses by signing in as themselves between them.

**`--check` before you need it.** It verifies that an account exists, that
`cloudflared` is
present *and actually executes*, that a `--name` tunnel exists on the account, and
that the database and front-end files are there — then exits. Worth running once,
because two of those fail in ways that are otherwise discovered at the worst
moment: a truncated `cloudflared.exe` is a valid PE file that dies with WinError
193 and no output, and npm's `.CMD` shim on Windows swallows both.

**Behind a proxy, the quick tunnel is relayed.** cloudflared builds its own
`http.Transport` for the one request that asks Cloudflare for a quick tunnel, and
a zero-value Transport has no proxy function — so that request ignores
`HTTPS_PROXY` however it is set, while every other tool on the machine honours it.
On a filtered link that is the difference between working and not: measured
against `api.trycloudflare.com` over an hour, direct swung between 3.8s and 28s
while the proxy stayed near 4s, against a fixed client budget of about ten
seconds. `tracker cloudflare` failed with `context deadline exceeded` roughly
three times in four.

So the console starts a loopback relay and points cloudflared's `--quick-service`
at it. The relay forwards that one request through the proxy and hands the JSON
back; the outbound leg is still TLS to Cloudflare and the only plaintext hop is
inside loopback, where cloudflared and the console already talk. It is not a
general proxy — the upstream host is a constant, only the path travels, and an
absolute request URI is refused. The proxy is found in the environment or, on
Windows, in `Internet Settings`, which is where Windows apps look and Go does not.

`--proxy http://host:port` forces one, `--no-proxy` skips the relay entirely, and
`--check` prints which was found. Attempts are retried three times regardless,
because the underlying failure is a latency race rather than a refusal.

Before this, a failed request did something worse than fail: the error text
contains the API's own URL, the hostname pattern matched it, and the console
printed `public: https://api.trycloudflare.com` — a link to Cloudflare's API
presented as your console.

If it still times out, use a named tunnel. It never calls that endpoint.
