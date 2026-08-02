# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

First working version. Nothing has been released yet, so everything below is the
initial build of the v1 PRD.

### Added

- **A survey of government data sources, and the decision not to build one**
  (`docs/government-sources.md`, `scripts/probe_government_sources.py`). Four
  uniform machine-readable routes were tested against the markets holding the
  capacity, and all four failed:

  Municipal permit portals (Socrata) have one API across ~460 jurisdictions and
  none of the ones that matter — Loudoun, Prince William, all of Virginia and
  Arizona return zero datasets; the hits that exist are $500k server-room
  retrofits. FERC and the state PUCs hold the right content behind ASP.NET forms
  with no API. County news feeds produced **0 candidates from 177 entries** scored
  by this project's own discovery filter, with Abilene's city feed silent on
  Stargate Abilene while it is built there. Legistar's agenda API does not have
  the data-center counties as clients, and its "data center" matters are municipal
  IT procurement from 2005.

  Shipping any of them would have added a source class that yields nothing while
  making the source mix look like government coverage exists. The probe script
  exists so the finding can be rechecked rather than believed — portals change.

  What does work is recorded alongside: utilities file large-load and
  interconnection commitments **with the SEC**, which is the already-built
  `ingest edgar --kind utility` path, and a specific `.gov` document can already
  be read with `ingest crawl --url` and is filed as `government_doc`
  automatically. Bulk discovery is the part that does not exist.

- **`tracker logic check` and `tracker logic resolve`**: find values that
  contradict each other, and settle the ones that can be. Every other check
  asks whether a value is supported; this asks whether the supported values agree.
  A row can be perfectly cited and still be impossible.

  Three layers, cost-ordered. **Rules** are free and deterministic — paying a model
  to notice `mw_built > mw_planned` is paying for arithmetic, and a rule states
  its reasoning where anybody can argue with it. On the live database: 21
  impossibilities and 125 warnings across 221 projects.

  **Collisions** report which of two conflicting sources the database keeps, and
  why. The winner is asked for, never re-derived: `upsert.resolve_field` is the
  function the write path uses. That mattered more than expected — the first
  version assumed credibility always decides and reported **73 of 221 rows** as
  drifted from their sources. Each field has a declared policy: built capacity
  takes the largest, `first_announced` the earliest, `phase` the furthest along
  unless something says it stopped, identity fields are never overwritten. Only
  the rest go by credibility then recency. After asking the real resolver, drift is
  **zero**, which is the correct answer for a working write path. `resolve_field`
  is now public for exactly this reason.

  **Judgement** is `--read N`, one call per project, off by default. Findings must
  name two fields and quote their evidence or they are dropped; it may not pick a
  collision winner, because which of two cited numbers is right is a question
  about sources and sources have weights and dates so nobody has to guess. Nothing
  it says is written. Across four rows read during development it returned
  nothing — the guards hold; its usefulness here is unproven and the rules are
  carrying the value.

  Also surfaces a quirk rather than silently working around it:
  `tracks.standing` reads an event's type and never its date, so a milestone dated
  next December counts as reached today. `milestone_in_the_future` reports it,
  because redefining "reached" would move every track strip in the product and
  that is not this command's call to make.

  **`logic resolve` settles what can be settled.** The first cut of this shipped
  as reporting only, which under-read the ask: finding a collision and applying
  its winner are two halves of one job. A row drifts when its stored value is no
  longer what its own citations support — after a hand edit, or a source attached
  by a path that did not re-derive — and `upsert.recompute_from_sources` was
  reachable only through `merge`, so there was no way to repair one. Now there is,
  and it previews unless given `--apply`.

  It settles that and nothing else, which is the honest scope. Measured on the
  live database: **0 of 149 findings** were mechanically resolvable. Whether
  100 MW built against 32 MW planned means a revised plan or two figures about
  different phases of one campus is not in the row, and a tool that picked one
  would be inventing a fact.

  Worth stating plainly because it is the part most likely to surprise: **five
  fields are not decided by credibility.** Built capacity takes the largest cited
  figure, `first_announced` the earliest, `phase` the furthest along unless a
  source says it stopped, and the identity fields are never overwritten once set.
  Only the rest go by source weight and then recency. `logic check` prints which
  rule settled each collision so the reason is always on screen.

- **Utilities and contractors as SEC filers** — 20 companies added to
  `seed/edgar-companies.toml`, taking it from 20 to 40. The power company is the
  counterparty that cannot be bypassed, and its filings say what no operator
  press release does: which large load has actually signed an interconnection
  agreement, and when energisation is expected. `power` is the track this
  database is worst at and the one it refuses to infer from construction, so this
  aims at the gap rather than at more of what already works.

  **Chosen by measured exposure, not by size.** Each utility serves a state
  holding ≥1% of tracked capacity — TX 33.8%, CO 11.7%, NM 8.5%, OH 8.4%,
  GA 7.8%, VA 7.8% — because a utility for a state with no projects is a
  subscription to noise. Constellation and Talen are in for one specific reason:
  a nuclear PPA names its counterparty and its site, which is exactly the
  customer-attribution fact 60% of the database is missing. Contractors are in
  for backlog, which leads energisation by a year or two.

  **Each class is searched with its own phrases** (`[search.by_kind]`). A utility
  does not write "build-to-suit"; it writes "large load" and "interconnection
  agreement". Adding a class of filer without adding its vocabulary is how a new
  source comes back empty and looks like it had nothing to say. Verified against
  the live API: `--kind utility --per-company 1` found 19 filings and prepared 17.

  `--kind` filters a run to one class, which is also the cost dial now that the
  list covers five kinds — reading the utilities is a different question from
  reading the hyperscalers and is worth being able to ask on its own. Utilities
  and contractors are deliberately *not* end users in `capex.attribute`, so
  adding them cannot move anybody's attributed capacity; there is a test.

- **Quarterly buckets on the pipeline.** "Whose capacity lands next quarter" is a
  question a year column cannot answer, and it was the one thing the Capex view
  was asked for and did not have. `tracker capex --by-quarter` and a year/quarter
  toggle on the page.

  Shipped with its own caveat measured rather than assumed: `capex.date_precision`
  counts how many dated projects land on 1 January, which is where a source that
  said only "2027" normalises to. On the live database that is **34%** — so the
  quarters are a shape and the years are the number, and both surfaces say so.

- **`tracker cloudflare`**: publish the console through cloudflared as a
  first-class command rather than a flag on `serve`. Same loopback bind, same
  password requirement, same refusal to publish without one — `serve --tunnel`
  still works and both now run one shared implementation, because the interesting
  part is the refusals and a second copy of those is a second place for one to go
  quietly missing.

  Two things it adds. `--check` verifies the password, that cloudflared is present
  *and executes*, that a named tunnel exists, and that the database and front end
  are there, then exits — worth running once, since a truncated `cloudflared.exe`
  is a valid PE file that dies with WinError 193 and no output. And `--name`
  /`--hostname` run a named tunnel on your own account, so the hostname is yours
  and survives a restart. Creating that tunnel is deliberately not done for you:
  `cloudflared tunnel create` and `route dns` write credentials into your home
  directory and a record into your DNS zone, both of which outlive the process, so
  the command prints the three lines to run and stops.

  `cloudflare` is blocked in the console's own command palette. The stated reason
  is not that it would hold the run slot — it would — but that putting this page
  on the public internet is a decision for somebody at a terminal.

### Fixed

- **The evidence gate was discarding correct values over cosmetic edits to the
  quote.** `enrich` logged `evidence quote for 'mw_planned' is not in the article;
  ignoring` on real, well-sourced figures.

  Measured over 131 evidence quotes from 8 cached articles: 33 failed exact
  containment, and the dominant cause was not fabrication but the model resolving
  references while it quotes — the article says "The campus is a single building
  comprising two data halls that serve as a 16.5 MW data center" and the model
  writes "The *Austin* campus is a single building…". One word substituted, the
  whole citation rejected, the capacity lost.

  `_verbatim_run` now finds the longest stretch of the quote genuinely present in
  the article and stores **the article's own words for that stretch**, never the
  model's edit — then widens it to the enclosing sentence, because one observed run
  stopped at "…the offering was $" where the source wrapped a line inside the
  figure, leaving a real quote that no longer evidenced the value it was cited for.
  Floors of 40 characters and 50% of the quote were tuned against a negative
  control testing every sampled quote against an unrelated article; nothing
  crossed. Acceptance **75% → 95%, zero false positives**.

  The guarantee is unchanged: `_stated_in` runs against the stored text, so a value
  must still be asserted by a sentence somebody published. Risk quotes take the
  same path, with the category check applied to the recovered text.

- **Closing a tab mid-run printed two tracebacks**, and over a Cloudflare tunnel
  that is ordinary rather than rare: the edge drops idle connections and the
  browser's EventSource silently reconnects, so one crawl produces several.

  The cause was platform. Windows raises `ConnectionAbortedError` (WinError 10053)
  where POSIX raises `BrokenPipeError` or `ConnectionResetError`, and the SSE
  handler caught only the latter two — so on the platform this runs on it caught
  nothing. All four are subclasses of `ConnectionError`; that is what is caught
  now, in `_stream`, `do_GET` and `do_POST`.

  The second traceback was the apology failing: the catch-all handler tried to
  send a 500 down the same dead socket, raised again from inside an `except`
  block, and escaped to socketserver. `_error` now tolerates a peer that has
  already gone, and the `ConnectionError` clause sits ahead of the catch-all so it
  never gets that far.

  Verified over a live tunnel — attach to a run's stream, hang up mid-flight: the
  run completes, the console stays healthy, the log stays silent.

- **A Census geocode was rendered as a green, weight-3 official citation.** The
  place-code lookup `ingest geo` uses is served from a `.gov` host, so
  `classify_source_type` files it as `government_doc` — and the drawer then shows
  "a government document supports this project" for a county and a pair of
  coordinates nobody wrote about the campus. 158 of 509 citations are this.

  The scores were never affected, which is worth stating because it was the first
  thing checked: corroboration is counted over `KEY_FIELDS` and a geocode claims
  none of them, so re-scoring every project with the Census sources removed moves
  **0 of 221**. It was only ever a label, so it is fixed as a label — the chip
  reads "reference data — corroborates nothing" for any `derived:` extractor. A
  migration to re-tag rows would have been a lot of moving parts for a word.

- **A wide table in the run log came out shredded.** Rich lays its tables out at
  a fixed `COLUMNS` (160) and draws them with `+-|` characters, so the column
  positions are baked into the text. The log pane was `white-space: pre-wrap`,
  and at a measured 815px — about 113 characters — every 132-character row of
  `tracker list` folded onto a second line and the borders stopped lining up with
  the cells. There is no pane width but 160 characters at which wrapping works,
  so the pane no longer wraps: it scrolls sideways, which is what a terminal
  emulator does, and the scroll is bounded because Rich never emits a line wider
  than `COLUMNS`. Each line is `width: max-content` so a reversed or
  background-coloured run paints its whole width instead of being clipped at the
  pane edge. The page still never scrolls sideways, at 1280px or at 390px.

  A `wrap` toggle sits in the log's corner for the other half of the output —
  `gaps` notes and refusal messages are prose Rich has already wrapped, and
  reading those by scrolling is worse than reading them reflowed. Off by default:
  the tables are the case that breaks rather than merely inconveniences.

- **`tracker cloudflare` failed behind a proxy**, which on the machine it was
  built for meant three times in four. cloudflared builds its own
  `http.Transport` for the one request that asks Cloudflare for a quick tunnel,
  and a zero-value Transport has no proxy function — so that request ignores
  `HTTPS_PROXY` however it is set, while curl, pip and the browser all work.
  Measured against `api.trycloudflare.com` over an hour: 3.8s to 28s direct
  depending on the minute, a steady ~4s through the proxy, against a fixed client
  budget of about ten seconds. The result was `context deadline exceeded` and no
  way to act on it.

  The console now starts a loopback relay, points cloudflared's `--quick-service`
  at it, and forwards that request through the proxy — 4.1s, measured, where the
  direct attempt had just timed out. The proxy is taken from the environment or,
  on Windows, from `Internet Settings`, which is where Windows applications look
  and Go does not. `--proxy` forces one, `--no-proxy` opts out, `--check` reports
  which was found. The relay is not a general proxy: the upstream host is a
  constant, only the path travels, an absolute request URI is refused, and it
  closes with the tunnel.

  Attempts are also retried three times, because the underlying failure is a
  latency race rather than a refusal — the same request measured 3.8s and 28s an
  hour apart. And when it does give up, the message now names the two things that
  help instead of dumping the log: a proxy, or a named tunnel, which never calls
  that endpoint at all.

- **A failed quick tunnel was reported as a working public URL.** cloudflared's
  timeout message contains `Post "https://api.trycloudflare.com/tunnel": context
  deadline exceeded`, the hostname pattern matched the API host inside it, and the
  console printed `public: https://api.trycloudflare.com` — a link to Cloudflare's
  API, handed over as the operator's console. The pattern now excludes `api.`, and
  a "failed to request quick Tunnel" line ends the wait immediately with the real
  reason instead of timing out sixty seconds later with a vague one. Observed
  live; the request itself is transient and succeeded on the next attempt.

### Added

- **A Capex view in the console**, and the duplicate review that belongs beside
  it. `tracker capex` answers the question the site-keyed database cannot — how
  much capacity each end customer has in flight — and the console had no
  equivalent. The view carries the whole thing: the coverage banner saying what
  fraction of projects it can speak for, the buyer table with MW-by-year, at-risk
  and slipped columns, the `*` marker wherever attribution came from ownership
  rather than a cited tenant, the suspect-attribution list, and both honesty
  footers (how much is attributed; every figure is a floor).

  The duplicate review sits on that page rather than under Coverage because
  `capex.py` makes the argument itself: a row stored twice is a nuisance in a site
  listing and a wrong number the moment anything groups by buyer — Abilene was in
  the database four times, so 1.2 GW was counted four times against OpenAI. The
  repair belongs next to the figure it corrupts. Groups appear with their
  candidate rows side by side (capacity, citation count, dates), a radio picks the
  survivor, and the merge runs through the ordinary `/api/run` path rather than a
  bespoke route — one execution path, one lock, one audit log. Deciding which of
  four rows survives by eye is the one thing a browser genuinely does better than
  the CLI.

- **A `DESTRUCTIVE` gate in the console catalog, on its own axis from cost.**
  `tracker merge` permanently deletes project rows and was in neither
  `LLM_COMMANDS` nor `BLOCKED`, so a misplaced click ran it with no confirmation
  at all. It now needs its name typed back, the same ritual `sync` uses for a
  different loss — reporting a merge as "spends LLM tokens" would simply be false,
  which is why `cost` and `destroys` are separate fields rather than one enum. The
  check is on the command *name* and never on its flags: a gate that reads
  arguments is a gate with a bypass in it, and `--dry-run` must not be the thing
  standing between a click and a deletion.

- **`ingest edgar` priced.** It spends one LLM call per filing and shipped outside
  `LLM_COMMANDS`, so it ran from the console without the confirmation
  `ingest crawl` has. Same mechanism, cheaper failure, one line.

- **A distinct label for the second cause of 待确认.** Two very different things
  reach that tier and they need opposite work: nothing quotable backs the value
  (find another source), or the quote is real and the figure belongs to a
  programme rather than this site (correct it — searching would find a citation
  and it would still be wrong). The console reads the disclosure the ingest path
  already wrote into `project.notes`, keyed on `crawl.SCALE_NOTE_MARKER`, and
  shows it as a red "not this site's figure" chip with the recorded sentence. Read
  back rather than recomputed in the browser, which would sometimes accuse a
  figure no gate ever demoted. Six projects in the live database carry one.

- **Variadic positionals in the console catalog.** `merge` takes any number of ids
  as bare arguments, which click models as `nargs=-1` rather than as a `multiple`
  option; reading only `multiple` refused the list, so the Duplicates card could
  never have folded more than one row.

- **A plausibility ceiling on `investment_usd` against `mw_planned`**
  (`crawl._implausible_investment`, $50M/MW). The evidence gate can only confirm
  that a figure was quoted in the article, not that it is *this project's*
  figure — an article about one 1,167 MW campus quoting "OpenAI's $500 billion
  Stargate" verifies fine and would otherwise store the programme total as the
  site's own investment. The threshold was read off the live distribution rather
  than assumed: 41 projects citing both figures cluster under $23.3M/MW with
  nothing until $83.3M+, so it sits in a real gap. A figure over the ceiling is
  demoted to 待确认 rather than dropped, the same treatment migration 0006 gives
  any value the gate can't otherwise verify.

- **`dedup.looks_like_the_same_site`, `company_parts`, `shares_a_party`,
  `distinctive_name_tokens`**: recognise when two rows in one locality are
  probably one campus stored under different company names — a builder, a
  landlord and a tenant each producing their own `dedup_key`. Measured: the
  Abilene Stargate campus existed four times (Crusoe, Oracle, OpenAI,
  "OpenAI/Oracle"), each correctly keyed and each carrying the full 1.2 GW, which
  is exactly what made `tracker capex` overcount before this. Locality alone is
  not the test — Ashburn alone holds fourteen genuinely distinct projects — so a
  match requires either a shared company token or a distinctive shared name
  token once generic industry words and the locality itself are discounted.
  `upsert._find_duplicate_candidate` now also scans same-locality neighbours
  through this check; like every other dedup signal here, a match proposes a
  review candidate and never merges automatically.

- **`tracker ingest edgar`**: SEC filings as a source (`tracker/ingest/edgar.py`).
  Every other source here is somebody's website, and the good ones increasingly
  answer 403 to anything that is not a browser; a filing is a legal obligation to
  publish, served from a government host with a documented rate limit instead of a
  bot filter. Filings are also where the fields this project covers worst actually
  live — investment in the cash-flow statement, in-service dates in MD&A, the end
  customer in the lease footnotes. Precision comes from scoping EDGAR full-text
  search by CIK rather than by phrase (unscoped, "data center campus" returns
  1,066 hits led by shell companies); a 369,000-character 10-Q is reduced by
  scoring paragraphs on evidence density and keeping the best ~6% with their
  neighbours, rather than truncated head-and-tail as a news article would be; and
  an 8-K exhibit that is actually a credit agreement is dropped before it costs a
  call, by legal-vocabulary density (0.3 for a 10-Q, 20.1 for a financing
  exhibit). The module only ever produces article text — the selected section is
  written into the same cache `discover.cache_feed_text` writes to, and `crawl.run`
  reads and extracts it exactly as it would a news article. New
  `seed/edgar-companies.toml` (company name, CIK, `kind`) drives which filings are
  read and doubles as the end-user classification `tracker capex` uses. Needs
  `TRACKER_USER_AGENT` set to a real contact; the command refuses to start on the
  shipped placeholder rather than collect a run's worth of 403s.

- **`tracker capex`**: capacity and spend rolled up by the company actually buying
  it, not the site building it (`tracker/capex.py`). The database is keyed on
  `(operator, locality, state)`; much hyperscaler capacity is built by wholesale
  developers and leased, so the operator on a building is often not the tenant.
  Attribution is a named tenant where a source gives one (folded through
  `dedup.customer_key` so Meta/Facebook are one buyer), otherwise the operator
  itself where the operator is a known end user (from `edgar-companies.toml`'s
  `kind`, plus a short hard-coded list of private companies — OpenAI, xAI,
  Anthropic and others — that file nothing with the SEC and would otherwise be
  invisible), otherwise an explicit unattributed row rather than a silent drop.
  Every figure is a floor: a project whose capacity nobody has cited contributes
  zero. The footer states what fraction of projects the rollup can actually speak
  for, flags projects that name a tracked operator as their own customer
  (usually an extraction error, not a lease — never auto-corrected, only flagged,
  for the same reason `dedup` refuses to auto-merge across granularity), and
  flags likely duplicate rows for one physical site stored under a builder's name
  and a tenant's name both. Never infers a tenant: who signed a lease is a fact
  with a documented answer, and `tracker infer` exists to keep judgement and fact
  apart.

- **`dedup.is_undisclosed`**: recognises when a `customer` value hedges rather
  than names anybody — "a Fortune 100 technology company", "publicly-traded
  global enterprise" — so `tracker capex` doesn't count four hedges as four
  distinct one-project tenants. Matched on live data: 4 of 12 populated `customer`
  values were this shape.

- **Five feeds added to break a single-outlet dependency**
  (`bisnow-datacenter`, `constructiondive`, `semianalysis`, `qts-newsroom`,
  `coreweave-blog`, and others in `seed/feeds.toml`). Measured before adding: 130
  of ~180 editorial citations came from one domain, capping 109 of 124 projects
  below confidence 3 (`confidence.compute` requires two independent registrable
  domains). Each entry's match count is what `discover` actually saw on a real
  poll, not an estimate; `coreweave-blog` and `openai-blog` are deliberately not
  `topic_implied`, since their feeds are mostly product news that would otherwise
  queue GPU benchmarks as data center articles.

- **`discover.cache_feed_text`**: when a feed syndicates the full article body
  (RSS `content:encoded`, Atom `<content>`) rather than a teaser, the body is
  written straight into the article cache and the crawl phase never has to
  request the page. Several outlets — the state nonprofit newsrooms among them —
  serve their feed freely and then answer 403 to any non-browser request for the
  article itself; every one of those articles was previously unreadable. Not a
  bypass: nothing is fetched that the publisher did not hand over, and it's the
  syndication feed used for the purpose a syndication feed exists for. A short-body
  guard (`MIN_USEFUL_CHARS`, the same floor the fetcher itself uses) stops a
  summary-only feed from caching a teaser as though it were the article, and an
  existing cache entry is never overwritten, since a real fetch is more complete
  than a feed excerpt. `tracker discover` and `tracker sync` both report `bodies
  from feed` in their counts now.

- **The console on a phone.** It worked and was miserable: at 490px the sticky
  header took 15% of the screen, the filter card was 558px tall, and the first
  data row sat at y=1137 behind a table showing 321px of a 2112px grid. Below
  720px the table is now a card list — thirteen columns has no good phone form,
  and shrinking the type to force one is worse than changing shape. The header
  drops to a logo and a scrollable view strip, filters collapse behind a count,
  the map takes 60vh and the `compact` mode `dc-map` already had, and the intro
  prose, coverage strip and tier legend step aside (all three are in Help). 940px
  of chrome above the first card became 355px. Coarse pointers get 36px targets
  regardless of width.

- **A Help view.** The three ideas that make the rest legible — evidence tiers
  with the live swatches rather than a picture of them, the five tracks and why
  power is never inferred, confidence 0–3 — plus what each view is for, what
  costs money, and the keyboard. Deliberately does not repeat the README, so the
  two cannot disagree.

- **Colour in the run log.** `runner.py` set `NO_COLOR=1` and `TERM=dumb`, which
  threw away the CLI's own signalling at the one moment someone is reading the
  output. Now forced on and parsed back out of the ANSI stream into styled spans
  (`static/ansi.js`), on a fixed dark terminal surface in both themes — amber on
  cream is a different colour and barely legible, so this is the one surface that
  does not follow the page.

  `FORCE_COLOR=1` alone was not enough, and failed silently: Rich honours it,
  then takes the Windows branch of `_detect_color_system`, finds `legacy_windows`
  and picks `ColorSystem.WINDOWS`, which paints by calling the console API rather
  than writing escapes — down a pipe that API does nothing and the markup is
  simply stripped. `cli._forced_colour` names an ANSI dialect and turns legacy
  mode off, which fixes it for anyone piping this on Windows, not only for the
  console. `NO_COLOR` still wins, per no-color.org.

- **Zoom and pan on the map.** Wheel, drag, and +/−/reset. Geography scales;
  **the marks deliberately do not**. Scaling the bubbles too is the obvious
  implementation and the wrong one — the reason to zoom this map is that a dozen
  Northern Virginia projects sit on top of each other, and bubbles that grow with
  the map stay exactly as overlapped. Button-driven zoom eases in CSS; wheel and
  drag stay instant, because a transition on a continuous gesture reads as lag.

- **`tracker ingest crawl --url URL`**, repeatable, beside the existing
  `--urls FILE`. Reading one link no longer means writing a file first. The
  console's Queue view uses it for a per-article Crawl button, two-step rather
  than typing the command name: that ceremony is proportionate to 25 LLM calls
  and absurd for one. The server-side rule is unchanged — the UI supplies the
  confirmation only after the operator has confirmed. `catalog.build_argv` learned
  repeatable flags, reading click's own `multiple` rather than keeping a list, and
  refuses a list anywhere the CLI takes a single value.

- **`docs/`** — `architecture.md` (how the CLI, database and console fit
  together; no English equivalent existed), `README.md` as an index that says
  which documents you can skip, and the two Chinese documents `guide.zh-CN.md`
  and `architecture.zh-CN.md`. No English `guide.md`: the root README already is
  one, and two documents over the same ground is how one of them becomes wrong.

- **`tracker serve --tunnel`** publishes the console through a Cloudflare quick
  tunnel, and **refuses to start without `TRACKER_CONSOLE_PASSWORD`**. Refusing
  rather than warning is the point: a warning scrolls past, and there is no safe
  reading of "published and open" in front of a process that runs commands.

  The gate (`webui/auth.py`) is not localhost-grade. Everything is behind it —
  before signing in the whole site is one self-contained login page and every API
  route is 401, so not even the frontend is served. Constant-time comparison.
  Session tokens are random, server-side and revocable; the cookie is `HttpOnly`
  and `SameSite=Lax`, which is what actually stops another site POSTing to
  `/api/run`, with an Origin check behind it. Eight failures locks one client for
  15 minutes and **forty across all clients locks the gate** — per-IP limiting
  alone is the wrong shape against a published URL, where an attacker rotates
  addresses. That global limit is what makes a short password safe: 40 attempts
  per 15 minutes is ~3,800 a day against a 36⁷ keyspace, or about 57 million
  years.

  `find_cloudflared` prefers a real executable over npm's `.CMD` shim, which
  swallowed both stdout and the exit status, and checks the binary actually runs
  before starting a tunnel. Found the hard way: npm's postinstall had left a
  7.9 MB `cloudflared.exe` where the real one is 54 MB, and every launch died with
  WinError 193 and no output at all.

- **Honest density in the projects table.** It read as empty — 653 of the cells
  were dashes — because the default columns included fields that are legitimately
  null on most rows. Columns are now **ordered by their measured coverage** from
  `gaps.measure()`, defaulting to those above 50% with the rest one switch away
  and the switch saying how many it is hiding. Self-maintaining: as coverage
  improves, columns promote themselves. Visible cells went from ~46% populated to
  95%, with nothing hidden and no figure massaged.

  Alongside it: a real per-row `9/12`, a coverage strip carrying the three best
  *and* three worst fields with true numerators and denominators, and a distinct
  style for a null that is empty *correctly* (`mw_built` before ground is broken)
  versus one that is simply unknown — `gaps.for_project` already drew that line
  and nothing was reading it. 61 of 653 empty cells turn out to be the former.

- **Motion**, on Meridian's own tokens rather than hand-written curves: staggered
  row entrance capped at twelve, drawer scrim fade, track segments filling left to
  right, coverage bars growing from zero, log lines fading in, and header totals
  counting up on first arrival only — a number that re-animates on every keystroke
  is noise. Everything is a keyframe or a transition so `base.css`'s global
  `prefers-reduced-motion` rule reaches all of it; the count-up is the one timer
  and checks the query itself.

- **`tracker serve`** and `tracker/webui/` — a local console on 127.0.0.1 that
  reads the database live and can run the CLI. Six views (Projects, Map, Queue,
  Coverage, Commands, Runs) built from the Meridian design system, ported from a
  Claude Design mockup. Distinct from `tracker export html`, which stays: the
  export is one emailable file frozen at write time, this is a server.

  The runner is the security boundary and is deliberately narrow. `POST /api/run`
  takes a command name and a flag object, validates both against a catalog
  introspected from Typer, and builds an argv **list** — no shell, so `;` and
  backticks are inert, and an unknown flag is an error rather than something
  passed through. `--db` is injected by the server rather than accepted from the
  request, so a run cannot be pointed at another database and cannot silently
  resolve a default from its working directory. The five LLM-spending commands
  require their own name typed back. One run at a time, because SQLite takes one
  writer and the second would die partway through having already paid for its
  calls. `--no-run` serves the views without the runner.

  No network requests at all: React, htm, d3, topojson, three.js, Lucide, the
  Census boundary TopoJSON and three OFL webfonts are vendored (3 MB, flattened
  because `.gitignore` carries unanchored `dist/` and `build/` rules), and the
  server sends `default-src 'self'`. No build step and no `package.json` — the
  Meridian bundle is already compiled to `React.createElement` and `htm` supplies
  the templates.

  Run history is files, not a table: `data/runs/<id>.jsonl` plus an index. A
  command's stdout is operational exhaust with a different lifetime from the
  tracked data, and the schema is not the place for it. "Projects changed" is
  counted by comparing `updated_at` across the run rather than diffed, because
  field-level history does not exist and a count that is true beats a
  reconstruction.

- **`source.quotes`** (migration `0007_source_quotes.sql`) and
  **`gaps.provenance()`** — the sentence behind each individual value, not one
  excerpt per source. `evidence_gate` always computed the pairing; `_excerpt()`
  then collapsed it and threw the association away, so `tracker show` printed the
  same paragraph under all twelve fields. `provenance()` returns the tier, the
  quote, whether that quote is the field's own or the source excerpt falling back,
  and which citation it came from. It reuses `upsert.claims_by_field()` so the
  quote comes from the source whose value actually won the merge — for a contested
  `mw_planned`, quoting the strongest *tier* would print a sentence stating a
  number the row does not hold. `basis()` is now the tier half of it, so there is
  one definition rather than two that can drift. `tracker export json` gains `prov`
  and per-source `quotes`; schema tag `tracker/4`.

- **A `defaulted` provenance tier.** `phase` is NOT NULL and falls back to
  `announced` when no source states one; reporting that as 待确认 asserted a source
  had claimed it and failed to prove it. 37 values on the live database were
  mislabelled that way. `tracker show` renders it quietly rather than in red.

- `tracker/required.py` — the required-project matching extracted from
  `tracker verify` so the console and the command cannot disagree about what
  "present" means.

- Project scaffold: `pyproject.toml` (`dc-tracker`, console script `tracker`),
  `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`,
  `requirements.lock` (pinned transitive set; there is no uv/poetry here).
- Schema: `project`, `source`, `event`, `ingest_url`. Defined authoritatively in
  `migrations/*.sql`, mirrored by `tracker/models.py`, with
  `tests/test_db.py::test_models_match_migrations` failing the build if the two
  drift apart.
- `tracker/db.py`: engine with `foreign_keys=ON` / WAL / `busy_timeout` pragmas,
  a raw-SQL migration runner with checksum-verified immutability, and
  `open_db(readonly=True)` so read commands cannot write.
- `tracker/normalize.py`: per-field coercion for state, MW, money, dates
  (carrying precision), phase, URLs and excerpts.
- `tracker/confidence.py`: 0–3 scoring by source weight, domain independence,
  agreement and conflict.
- `tracker/dedup.py` + `tracker/upsert.py`: the single write path, with project
  fields recomputed from `source.claims` so ingestion is idempotent and
  order-independent.
- `tracker/ingest/manual.py` and `seed/sample-projects.json`: hand-curated JSON
  ingest, refusing to load a file that still holds `PLACEHOLDER` values.
- `tracker/ingest/iso_maps.py` + `tracker/ingest/pjm.py`: ISO queue ingest for
  PJM/MISO/ERCOT/CAISO, reading CSV, XLSX and JSON. Aborts before row 1 on a
  renamed column, streams in chunks of 1000, and fails loudly when zero rows
  match or the reject rate exceeds 5%.
- `tracker/prompts/` + `tracker/llm.py`: versioned prompt files stamped by content
  hash, and a MiniMax client that enforces the JSON contract in code
  (parse → repair → validate → one corrective retry).
- `tracker/ingest/fetch.py` + `tracker/ingest/crawl.py`: article fetching with
  per-host rate limiting and browser escalation, LLM extraction gated on quoted
  evidence, an on-disk article cache, and per-URL outcomes in `ingest_url`.
- **`tracker/ingest/discover.py`**: polls the RSS/Atom feeds and sitemaps in
  `seed/feeds.toml`, keyword-filters headlines in two tiers (topic + project
  signal), and queues matches for triage. Closes the gap where nothing in the
  system could find an article to read, so the database only ever held what an
  operator typed in by hand. Uses stdlib `xml.etree` and `tomllib` — no new
  dependency.
- **`tracker sync`** — the whole pipeline in one command: discover new candidate
  articles, extract them, refresh existing projects by re-reading their sources,
  then list. Both crawl phases are capped since each article costs an LLM call,
  and `--dry-run` previews a run for free.
- `crawl.stale_sources()` selects the citations of existing projects that have not
  been re-read recently, which is how project data gets *updated* rather than only
  added. Placeholder URLs are excluded, being unfetchable by definition.
- `tracker list --limit N`, which reports the total it capped ("2 of 3").
- **`tracker ingest geo`** and `tracker/ingest/geo.py` — derives `county`, `lat`
  and `lon` from two free US Census files (place-to-county crosswalk and the 2024
  place gazetteer). No API key, no LLM, no per-row cost. Three of the twelve fields
  are a lookup rather than a research problem: articles write "Mount Pleasant,
  Wisconsin" without the county, and never print coordinates at all — which is why
  `lat`/`lon` were 0%, the evidence gate having correctly discarded every
  unquotable value the model produced. On the live database this moved `county`
  from 49% to 80% and coordinates from 0% to 89%.

  It refuses to guess two things. A city spanning several counties (Houston and
  Austin touch four each) leaves `county` NULL and is reported, because choosing
  one would invent a fact — that is what holds county to 80% rather than 100%. And
  every derived coordinate discloses in its own excerpt that it is the centre of
  the place, not the project site.

  Consolidated city-counties are resolved by alias: Census publishes Augusta as
  "Augusta-Richmond County consolidated government (balance)", and without the
  alias Augusta GA and Indianapolis IN were the only project cities that failed to
  match. Exact names always win over aliases.
- `confidence` now excludes any source whose `extractor` begins with `derived:`
  before scoring, and `SourceView` carries `extractor` to make that visible. A
  Census row is a real, checkable citation but it is not testimony about the
  project; counting it would let one press release plus a lookup read as two
  independent domains and reach confidence 3.
- **`tracker export html`** and `tracker/templates/dashboard.html` — the whole
  dataset as one self-contained page: sortable table over the 12 fields, a
  five-segment track strip per project, filters (state, phase, blocked-on,
  confidence, quoted-only), a per-project drawer with citations and milestones,
  capacity-behind-an-obstacle bars, and a coordinate plot.

  No network requests of any kind — no CDN, no webfont, no tile server — so it
  works offline and can be emailed. The dataset is inlined rather than fetched from
  a sibling `.json` because `fetch()` from a `file://` page is blocked as
  cross-origin, and a two-file build would open to an empty table unless the reader
  happened to be running a web server.

  Colour means **trust, never category**: the five tracks are a sequence so they get
  an ordinal neutral ramp, and every hue is reserved for how much to believe a value
  (amber for 待确认, blue for inferred) or for something being wrong (rose for a
  blocked track). The signature element is a provenance ledger that recomputes on
  every filter, so "what do the figures I am currently looking at rest on" is always
  on screen — the question this dataset exists to answer.

  Every measurement is set in a monospace stack with tabular figures and prose in the
  system sans, so number columns align and the family itself distinguishes a
  measurement from a description. Contrast verified in-browser against WCAG AA:
  muted labels 4.83:1 on surface, 待确认 amber 10.04:1, blocked rose 7.17:1.
- **Operator newsrooms as sources.** Seven company press-release archives added as
  `[[sitemap]]` entries. A release opens with the three fields that are hardest to
  find anywhere else — "X announces a $N billion campus, online in YEAR" — and is
  `company_filing`, weight 3. Measured across the whole database beforehand: trade
  press yielded 0.57 of those three fields per citation, and the *two* company
  sources in it yielded 1.50. The vein was untouched, not exhausted.

  A `company` field marks an entry as an operator's own newsroom, which does two
  things. `matches_known_project` stops requiring the company in the slug, because
  the domain already proves it — measured, that took the yield from 15 articles
  over 8 projects to 28 over 13, since a release titled "New Hillsboro campus
  announced" names the city and not the company. And `classify_source_type` learns
  the host is first-party. The locality-or-name-token requirement is unchanged, so
  precision holds: a careers page still matches nothing.

  Not included, with reasons recorded in `seed/feeds.toml`: QTS 404s; Flexential
  and Iron Mountain answer 403/429; STACK, Switch, Vantage, EdgeConneX and
  news.microsoft.com serve child sitemaps only to browser-like clients (curl 200,
  httpx 403 on the same URL — a TLS-fingerprint rule). Their robots.txt explicitly
  permits crawling and advertises the sitemap, so those need `--browser`.
- **A Bocha (博查) backend**, for networks where the other three cannot be signed
  up for — its registration works from mainland China with no Cloudflare
  challenge. It handles the trap that Bocha answers **HTTP 200 with the real
  status in the body's `code` field**, so the status line alone does not mean the
  call succeeded.

  Measured honestly, its index does not fit this tool. A project query returned
  sohu, zhihu, xueqiu and 163; `site:datacenterfrontier.com` returned that site's
  *homepage* rather than any article; and querying the **exact headline** of an
  article already in the database returned no trade-press URL at all. It is an
  index-coverage gap, not a query-syntax one. End to end against the live API:
  10 hits, 10 filtered, 0 survived. Useful for learning that a project exists,
  not for obtaining a citation — so it resolves **last** in `auto` order and can
  never displace a better index.
- **A language gate on search candidates** (`normalize.looks_english`). A host
  blocklist only stops the reposters you already know; one measured Bocha run
  surfaced `dahe.cn`, `topnews.cn` and `uyijian.zhiding.cn`, and the long tail is
  endless. Script is the property that generalises, so a candidate whose title and
  snippet are more than 10% CJK is dropped before it costs a fetch and an LLM call.
- `_SKIP_HOSTS` gained the Chinese portals and UGC platforms that Bocha favours,
  plus document dumps and academic indexes. Every one of sohu, zhihu, toutiao,
  csdn, researchgate and dl.acm.org passed the old filter.
- **Search is pluggable, with Brave and Serper alongside Google.** `PROVIDERS` maps
  a name to a class and `build_provider()` resolves it; `TRACKER_SEARCH_PROVIDER`
  pins one, and `auto` (the default) takes whichever backend holds a key. An
  explicit name without its key fails loudly rather than falling back to a
  different engine, because silently searching somewhere else is worse than
  stopping.

  Brave is the recommended addition: an independent index rather than a Google or
  Bing reseller, so it widens coverage instead of re-asking the same engine — one
  header, no cloud account, 2000 queries/month free. Its free tier answers HTTP 429
  for *pacing* (about one query a second) and not only for an exhausted monthly
  allowance, so a first 429 is retried after a pause instead of ending the run.
  Serper is included too but is Google's index under a simpler API, so it widens
  the quota rather than the coverage.

  **There is no Bing backend: Microsoft retired the standalone Bing Search APIs on
  2025-08-11**, and their documentation page now carries `is_retired: true`, so no
  new subscription key can be created. The successor, Grounding with Bing Search in
  Azure AI Foundry, is licensed for grounding a model's reply rather than for
  building a stored database of facts and citations — precisely what this tool
  does. Asking for it by name prints that explanation, with the Brave drop-in,
  rather than "unknown provider".

  `Settings.has_search_keys()` now means "some backend is configured";
  `has_google_keys()` is the specific check, and `GoogleCSEProvider` uses it so a
  Brave-only setup cannot construct it and fail later at request time.
- **`tracker enrich ID`** and `tracker/ingest/enrich.py` — throws every retrieval
  method at ONE project, in rounds, until a round stops paying. `tracker sync`
  spreads a budget across the database; this inverts it for when you want one row
  complete. Six harvesters run cheapest-first (derive → queue → retry → archive →
  search → refresh) so an expensive one never runs for a field a free one fills,
  and the loop stops on "a round filled nothing new", not at a fixed article count.

  Search queries are templates anchored on the project's quoted company and
  locality rather than LLM-generated: the project is already known, so there is
  nothing to infer, and an unanchored "data center investment billion" returns the
  industry instead of the site.

  `--dry-run` harvests and reports **without fetching or extracting**.
  `crawl.run(dry_run=True)` still fetches every page and still pays for every LLM
  call — it only declines to commit — and on the most expensive command in the tool
  a preview that bills you is a trap.

  Fields a null is correct for are excluded from its score rather than counted as
  failures, so a project with nothing built is not marked down for having no
  `mw_built`. Measured ceiling on a 94-project database with no search key: 17
  projects had unread archive articles and 77 had none, because the configured
  archives never covered them — the honest limit is the corpus, not the budget.
- `tracker/gaps.py` gained `for_project()`, per-field state for a single project
  (`filled` / `missing` / `not_applicable`), and `geo.run()` gained
  `only_project_id` so a single-project command cannot silently rewrite every row.
- **`tracker gaps`** and `tracker/gaps.py` — per-field coverage measured against
  the rows where the field can legitimately be set, not against every project.
  Raw NULL counts pointed effort at work that cannot succeed: 61 announced
  projects were being counted as `mw_built` misses when nothing is built on any of
  them, and `blocker` looked like a 2%-covered backlog when the truth is that
  most projects have no blocker. Fields whose absence carries no information are
  reported as unmeasurable rather than as a low score, and the report closes with
  the measurable fields that have the most rows left to fill.
- **A `risk` table** (`migrations/0004_risk.sql`) — obstacles as typed, dated,
  severity-ranked rows with their own citation, replacing the single free-text
  `project.blocker`. That column could not hold more than one obstacle when the
  PRD's own list names seven; could never be cleared, because `upsert._resolve`
  returns the existing value when a field has no claims, so a resolved obstacle sat
  on the row forever; and could not be counted, which is what the chip/cloud/power
  read-through needs.

  It also needed a carve-out to be evidenced at all. A blocker sentence is a
  paraphrase, so it can never be a verbatim substring of its own quote — both
  blockers in the live database fail `_stated_in` against their own article text —
  and `_SUMMARY_FIELDS` covers that by trusting the model's *label* over a verified
  quote. `risks[].quote` is the stronger form of the same idea: the quote must be
  real **and** must contain wording for the category it is filed under, checked
  against `_RISK_EVIDENCE` exactly as `phase` is checked against `_PHASE_EVIDENCE`.
  So `blocker` left `_SUMMARY_FIELDS`; obstacles became storable by tightening the
  check rather than loosening it.

  `category` is a closed vocabulary mapped to the PRD's obstacle list
  (`grid_capacity`, `transmission`, `permitting`, `environmental`,
  `equipment_supply`, `chip_supply`, `financing`, `offtake`,
  `community_opposition`, `water`, plus `unclassified`). `severity` is
  `watch`/`material`/`blocking`, **ordered** — that order decides which risk
  becomes `project.blocker`. `summary` may be a paraphrase and `quote` holds the
  verified verbatim sentence beside it, the same split `notes` already draws, so
  the gate needed no weakening.

  0004 backfills any existing `blocker` into an `unclassified` risk carrying the
  source that asserted it, so upgrading loses nothing. Verified against a copy of
  the live database: both rows migrated, both cited.
- Schedule slippage is measured. When a recomputed `expected_online` lands later
  than the stored one, `upsert._record_slippage` writes an `event(delayed)` carrying
  both dates and attaches `delay_days` to the project's most severe open risk.
  `expected_online` keeps its `PREFER_WEIGHT` policy: switching to newest-wins to
  make slips visible would have thrown away the source-quality ordering, and
  recording the movement as history keeps both.

  **A number is only attached across a year boundary.** The column stores no
  precision and `norm_date_detail` coarsens hedged dates into it — a bare "2027"
  lands on 2027-01-01 and "late 2027" on 2027-10-01 — so a source restating the same
  year more precisely is indistinguishable from a 273-day delay. Coarsening always
  stays inside the stated year, so a move into a later year cannot be an artefact
  and a move within one might be. The event is written either way, saying which case
  it is; only the unambiguous one gets counted.

  No risk is invented when none is open. A date moving says the timeline changed,
  not why, and manufacturing an obstacle from it would put an uncited guess into the
  field an operator acts on.
- **`tracker exposure`** — planned capacity sitting behind an open obstacle, rolled
  up `--by category | state | company | customer`. Severity is reported as three
  MW columns rather than collapsed into one number: collapsing needs a weighting,
  a weighting is a judgement rather than anything a source said, and this tool does
  not present judgements as facts. `--weighted` adds the single number for whoever
  wants it and prints the weights it used. Projects with an open risk but no cited
  capacity are counted in their own column rather than treated as zero MW at risk.
  Grouping by anything but category counts each project once, in its most severe
  open category, so a project obstructed three ways is not triple-counted.
- **`tracker risks`** — obstacles across the database grouped by kind, each with the
  projects it blocks and the planned MW behind them. This is the query one free-text
  sentence per project could not answer, and it is what carries the read-through:
  MW blocked on transmission is a power and utility signal, MW blocked on `offtake`
  or `chip_supply` is a cloud and semiconductor one. An uncited risk is labelled as
  such rather than presented alongside quoted ones.
- `tracker list --risk <category> --severity <level>`, composing with the existing
  filters. Matching is an EXISTS over *open* risks, so a project obstructed three
  ways is still one row and a resolved obstacle does not match.
- `tracker show` renders each risk with its severity, dates, delay and quote;
  `tracker stats` gains a "by open risk" table with MW at risk; `tracker review`
  calls out an open `blocking` risk that has no citation.
- Exports carry risks. CSV appends a `risks` column of open `category:severity`
  pairs — appended at the end, not slotted in beside `blocker` where it reads
  better, because that tuple is a positional contract. JSON gains a nested `risks`
  array including resolved ones, since it has a `status` field to say so. The JSON
  schema tag moves to `tracker/2`.
- Obstacle extraction: `extract-v1` returns a `risks[]` array instead of a
  `blocker` string, `crawl._risks` gates each entry, and `upsert._upsert_risks`
  writes them the way `_upsert_events` writes milestones — dedup on
  `(category, first_seen)` in Python, so re-ingest stays idempotent even for an
  undated obstacle where the UNIQUE constraint cannot help.

  `project.blocker` is now derived by `upsert._derive_blocker` and is in a new
  `DERIVED_FIELDS` set that the claims-merge loop skips. Sources still record a
  `blocker` claim so `source.fields` says which citation supports it, but the value
  is written and never read. Two consequences worth naming: an obstacle can finally
  be **cleared** (resolve the risk and the column goes NULL), and the citation for
  `blocker` now lives in `risk.source_id` — `test_every_field_is_cited` was extended
  to follow it there rather than dropped.

  An article that stops mentioning an obstacle does not clear it: that is not
  evidence it is gone. Re-reading updates wording and severity in place but never
  revives a risk an operator resolved — `status` belongs to the operator.
- `ingest manual` accepts a `risks` array; a bare `blocker` string still loads and
  becomes one `unclassified` risk cited to the record's first source, so curated
  files written before the table keep working. Two curated risks sharing a category
  and date are refused at validation, where the operator can see which lines
  collide, rather than as an IntegrityError partway through a write.
- **A third discovery tier, `risk_signal`, so obstacle news reaches the queue.**
  Every `signal` term was announcement-shaped — announce, expand, invest, build,
  campus, megawatt — which silently discarded every article about a project going
  *wrong*. Measured against the real filter, all of these were dropped for having
  "no project signal": "Loudoun supervisors reject data center rezoning
  application", "Georgia Power says transmission upgrades delay data center
  energization", "Transformer shortage pushes back hyperscale data center
  timelines", "Moratorium halts new data center development in Fayetteville",
  "Water use concerns stall Tucson data center vote". So the corpus the extractor
  ever saw was announcements only, and no schema change can recover an obstacle
  from an article that was never queued.

  A risk term satisfies the signal tier alone, but `topic` must still match, so a
  transformer shortage at a steel mill is still dropped and the `exclude` tier
  still runs first — commentary, analyst notes and share-price coverage stay out.
  The list is operator-editable in `seed/feeds.toml` and absent from a config
  entirely means the filter behaves exactly as before.

  Measured over one live poll of all eight feeds: 200 entries, 38 kept by the two
  tiers, 44 by the three, of which two were real project obstacles and four topical
  commentary — the same over-collect-and-triage ratio the queue is designed for.
  Short terms are padded (`" sue "`) like `" mw"` in the signal tier: unpadded,
  `sue` matched *issued* and queued "PJM Issued First Backup-Generator Warnings" as
  litigation news.

  This is deliberately **not** a lever for `blocker` coverage; `tracker gaps`
  reports that field as unmeasurable precisely because absence is usually the
  truth. The point is to stop discarding the articles where an obstacle is real.
- Among the queued articles covering a tracked project, the ones reporting an
  obstacle are crawled first (`pending(spec=...)`, `pending_risk_count`). Those are
  the highest-value calls available: a project's own press release never names its
  blocker, so an adversarial second source is the only thing that can record one.
- **The queue is prioritised toward depth.** A queued article covering a project
  already tracked becomes a SECOND source, which fills fields one article cannot
  and lifts confidence from 2 to 3; draining oldest-first instead just grew the
  database sideways into more single-source rows. Measured before the change: 26 of
  29 projects had exactly one source. `--breadth-first` restores the old order.
- **`tracker sync --deep`** walks site archives via their sitemaps, following a
  sitemap index one level and preferring article sitemaps over Company/Event ones.
  A measured run found 799 matching URLs going back to 2015, 477 of them new —
  with no API key, no quota, and no credentials to set up, because sitemaps are
  published expressly for machines to read. This is now the recommended way to
  fill the database; search is optional.
- **`tracker/ingest/search.py`** — search-based discovery. `tracker search` runs
  Google's official Custom Search JSON API, and `--from-llm N` has MiniMax propose
  the queries. Model-proposed project names are search leads only: nothing it says
  is stored, and a project appears only after a real article was fetched and its
  values backed by verbatim quotes. Also `tracker sync --search N`.
- `tracker queue --failed` and `tracker sync --retry-failed`, plus an always-on
  report of unread URLs grouped by host. They were previously invisible: `discover`
  never re-queues a known URL and the pending queue only holds `discovered`, so a
  run could say "queue is empty, 0 failed" while a dozen articles sat unread.
- `datacenterknowledge.com` added as a feed: 50 entries per poll, ~22 matching, and
  unlike datacenterdynamics its articles are fetchable over plain HTTP.
- `tracker/export.py`: deterministic Markdown, CSV and JSON export.
- `tracker/cli.py`: `init`, `ingest {manual,pjm,crawl}`, `discover`, `queue`,
  `list`, `show`, `stats`, `review`, `verify`, `export`, `version`.
- 701 tests, green offline with no API key and no network. 93% coverage on both
  `normalize.py` and `confidence.py`. A `network`-marked test checks the feed URLs
  still resolve.

### Fixed

- **An expired console session was invisible.** Every API call 401'd once the
  session cookie ran out, but the page kept showing whatever it already had and
  each feature reported its own misleading local reason — the 3D map said "3d
  unavailable offline" when the actual cause was an expired login. `api()` now
  redirects to `/` on a 401 rather than letting the caller render a stale page
  as if it were live.

- **A failed module/atlas load in the map and 3D-campus widgets was cached
  forever.** `p = p || import(...)` memoises the *rejected* promise too, so one
  transient failure (a dropped connection, or the 401 above hitting the module
  fetch) left the widget reporting "unavailable offline" for the rest of the
  page's life even after the cause was gone. The promise is now cleared on
  failure and the note becomes a "tap to retry" control
  (`dc-campus.js`, `dc-map.js`, `dc-map3d.js`).

- **A quote popover was unreachable on a touchscreen.** It only ever opened on
  `mouseenter`, so a phone or tablet — no hover — could see the underline
  styling implying a quote existed but never see the quote itself. A click/tap
  now toggles it open and sticky (dismissed by Escape, an outside tap, or
  scrolling) alongside the existing hover behavior; deliberately not given a
  keyboard tab stop, since 124 rows × 12 fields is 1,488 tab stops for a feature
  the drawer already exposes per-field with a real tab order.

- **Two events or risks with the same `(type, date)` in one record crashed the
  whole run.** `_upsert_events`/`_upsert_risks` looked up "already have this
  one?" in a map built once from what was already in the table before the
  record started, so a second same-key row within the same record was never
  seen as a duplicate — both were added, and the flush hit
  `uq_event_project_type_date`, failing the entire upsert including everything
  else the record carried. Seen live on an SEC filing listing two capacity
  expansions dated the same day, a shape a news article rarely produces, which
  is why this survived until filings became a source. Fixed by registering each
  new row into the map immediately after adding it, not just what pre-existed.

- **`html_to_text` mis-read numeric HTML entities**, silently dropping or
  corrupting figures next to them. `&#160;` (a non-breaking space, written
  numerically rather than as `&nbsp;`) appears 17,469 times across 39 SEC
  filings and routinely sits between a number and its unit: `"$ 13.5&#160;billion"`
  parsed as `$13` — a billion-fold error — and `"393&#160;MW"` matched nothing at
  all, silently losing the value. The old table hand-covered nine named entities
  and no numeric ones; replaced with `html.unescape`, which handles both. News
  HTML mostly emits `&nbsp;` (covered already), so this stayed invisible until a
  numeric-entity-heavy source — filings — arrived.

- **The entrance animation smeared the page.** A ghost of the coverage strip kept
  being painted at the bottom of the viewport, below the table, after scrolling.

  `animation-fill-mode: both` retains the final keyframe, and `rise` ends on a
  transform — an identity one, but a transform. That makes the element a
  containing block for every `position: sticky` descendant and keeps it
  composited indefinitely. The table has sticky id and company columns inside
  every row, so 124 rows were each holding a transform, and so was the container
  wrapping the whole table.

  Three changes: `backwards` instead of `both` everywhere, so the transform
  exists only while the animation is actually running; the entrance class is
  applied to the first twelve rows rather than all 124, since animating a hundred
  rows at once is a hundred compositing layers for no visible gain; and anything
  containing a sticky descendant — the table rows and the view container — fades
  without moving. Translation is a nice-to-have, not breaking `position: sticky`
  is not. Verified: zero sticky containers hold a transform, and the sticky
  columns stay pinned while the table scrolls sideways.

- **`--browser` never worked, on any path.** `enrich --browser`, `sync --browser`
  and `ingest crawl --browser` all built a `Crawl4AIFetcher` and passed it down
  without ever entering it, so the first page that needed escalating died with
  `Crawl4AIFetcher must be used as an async context manager`.

  Nobody could have entered it from where it was built: launching Chromium is
  async and every caller is synchronous — `crawl.run` owns the `asyncio.run`. So
  `fetch_all` now starts and stops it, being the only code already inside the
  loop. Lazily, on the first page that actually needs it, because most runs never
  escalate and a browser costs seconds and a process. It is started once per run
  however many pages escalate, and always shut down afterwards: Chromium does not
  exit on its own and a leaked one outlives the command. A browser that fails to
  start now logs once and lets the run finish on plain HTTP, rather than raising
  the same traceback for every URL.

- **`--browser` without the optional extra failed in the wrong place.** The
  `crawl4ai` import lives in `__aenter__`, so constructing the fetcher could not
  raise `MissingDependency` and `enrich`'s `try/except` around it was unreachable
  — the run got as far as the first escalation before noticing. All three
  commands now call `Crawl4AIFetcher.ensure_available()` up front, so the failure
  lands on the flag.

- **Rich ate the install instruction.** `_fail` printed its message as markup, so
  the crawl4ai error told you to run `pip install -e "."` — Rich did not
  recognise `[crawl]` as a style and dropped it. The message explaining how to
  fix the problem was broken by the same mechanism. Error text is data and is now
  escaped.

- **`_print_standing` was registered as a CLI command.** A stray `@app.command()`
  on a private helper put a broken `-print-standing` in `tracker --help`; it took
  a `project` string argument and would have crashed on any invocation.
- **`stats` and `gaps` emitted no JSON at all on an empty database.** Both
  returned early with prose even under `--json`, so a consumer piping stdout into
  a parser got something unparseable instead of "zero projects".
- README's "Known gaps" still said the crawl path had only been run against
  fixtures, never live — stale since `de4821c`'s live-run defect fixes and now
  since `tracker enrich`'s live verification against project #93.
- **A first-party press release scored below the trade-press rewrite of it.**
  `classify_source_type` recognised a company only by subdomain — `news.microsoft
  .com`, `about.fb.com`, `blog.google` — so an operator publishing at
  `www.stackinfra.com/news/…` fell through to `general_media`, weight 1, against
  trade press's 2. The single most authoritative source for capacity, investment
  and timeline was the lowest-weighted one. It now consults the operator hosts
  declared in `seed/feeds.toml`.
- **A foreign-language quote could evidence a project's `phase`.** Every other
  field is protected from a translated repost for free, because "230兆瓦" matches
  no MW pattern, "12亿美元" no currency pattern, and neither matches any English
  phase keyword. But `phase` is a `_SUMMARY_FIELD`, and that carve-out trusts the
  model's *label* over a verified quote — precisely so an honest paraphrase is not
  discarded for sharing no substring with its own evidence. It had no language
  check. Measured against a real Chinese repost of a US announcement,
  `phase=construction` was stored while every quantity in the same sentence was
  correctly dropped. The carve-out now requires its quote to look English too.
- **A company-name suffix is no longer treated as a distinctive project token.**
  `project_identities` excluded name tokens that appear in the *normalized* company
  key, but `company_key` strips corporate suffixes — so "STACK Infrastructure" keys
  to `stack` and left `infrastructure` looking distinctive in "STACK Infrastructure
  Hillsboro Campus", while every STACK slug contains `stack-infrastructure`.
  Company-wide stories ("raises $400 million to fund growth", "expansion into
  Asia-Pacific markets") were harvested as evidence about one Hillsboro project: 8
  false matches for that project alone, and any operator whose name ends in a
  stripped word — Infrastructure, Data Centers, Energy, Systems, Realty — leaked the
  same way. Tokens are now excluded against the raw company name as well as the key,
  which keeps the Facebook → meta aliasing working. Found while testing `tracker
  enrich`, which read 7 such articles and filled nothing.
- Read commands refuse a database that is behind on migrations, naming `tracker
  init` as the fix, instead of failing with a raw SQLAlchemy "no such table"
  traceback. Found on a real v3 database once `risk` landed: `tracker risks`,
  `show`, `stats` and `exposure` all query a table it does not have. A read command
  opens the file `mode=ro` and so cannot migrate on the operator's behalf; saying
  which command will is the only useful thing it can do.

### Changed

- Four additions beyond the PRD's three-table schema, each unblocking a stated
  PRD requirement: `source.claims` (Q2's "keep both conflicting values" and the
  confidence agreement rule), `source.extractor` (prompt-version traceability),
  `project.county` (ISO queues report county, not city), and the `ingest_url`
  table (the PRD's `fetch_error` marker cannot live on `source`, which requires
  a `project_id`).
- Confidence caps at 2 for a single source however authoritative; 3 requires
  independent corroboration or operator verification.
- ISO queue ingest is scoped as candidate generation, not a project feed: the
  public queues are *generator* queues with no data-center column, so matching is
  a keyword heuristic, confidence caps at 1, and queue MW is disclosed in `notes`
  rather than written to `mw_planned`. See README "Why".
- Crawl4AI is an optional extra used for fetching only, not for LLM extraction,
  so that prompt versioning is truthful and the JSON contract is enforced in
  testable code. `httpx` is the default fetcher.
- The database is treated as a build artifact and is not committed; `seed/*.json`
  and `data/raw/*.csv` are, and reproducibility is a documented replay.
- Hedged dates resolve instead of vanishing: `late 2027` → 2027-10-01 at quarter
  precision, `H1 2027` → half precision, `by 2028` → year precision, each with a
  note recording the original phrasing. Only genuinely unanchored phrasing
  (`next spring`) and directional hedges (`before 2028`) stay NULL. Treating every
  hedge as unparseable was discarding most of `expected_online`.

### Fixed

- **The evidence gate no longer discards values over the model's own bookkeeping.**
  It required a quote *tagged* with each field's name, so a correct value whose
  verbatim quote was filed under a different field was thrown away. T5@Augusta
  returned `mw_planned: 200` with the sentence "…a 140-acre, 200 megawatt campus
  in Georgia" attached to `name`, and lost the capacity. Across the first 90
  projects this discarded **89 correctly-evidenced values from 64 projects** — 60
  of them `phase`, which being NOT NULL silently became the `announced` default,
  so the stored phase distribution was an artefact of the gate rather than of the
  projects.

  A value now survives if any *verified* quote actually asserts it, whichever
  field the model filed it under. Quotes are still required to be real substrings
  of the fetched article, and the new check is strictly stronger than the one it
  replaces: numbers and dates are compared after normalization (so "1.2GW"
  evidences `1200.0`), which means a genuine sentence citing a *different* number
  no longer launders an invented value. `phase` is matched on article wording
  ("broke ground" → `construction`) because it is a judgement, never a quotable
  string; `blocker` and `notes` keep the label check, being paraphrases that
  legitimately share no substring with their own evidence.
- **Two overlapping runs no longer collide silently.** SQLite takes one writer, so
  a second `tracker sync` failed partway with a raw forty-line SQLAlchemy
  traceback — after it had already paid for LLM calls. A lock file now refuses the
  second run up front, reclaiming the lock if the holding process has died, and any
  remaining "database is locked" is translated into a message that says what to do.
- `crawl.run` commits after each URL rather than once per run. One transaction
  spanning 150 articles held the write lock for around 25 minutes, and a failure at
  article 149 discarded the other 148.

### Fixed (earlier)

Found by running the crawl path against the live MiniMax API for the first time:

- Rich read `[crawl]` in the "install the extra" hint as a style tag and
  deleted it, so the message told the operator to run `pip install -e "."` —
  omitting the one thing it existed to communicate. Same bug silently emptied
  the `--browser` help text.
- **A placeholder URL is no longer treated as a citation.** It is dropped before
  any weighting, so it can neither supply the "strongest source" nor count
  toward domain independence. Observed live: a real Microsoft project reached
  confidence 3 because a placeholder seed row contributed a weight-3
  `company_filing` for a URL that does not exist. Placeholder-only rows now
  score 0 and are routed to `tracker review`, which is the whole point of the
  seed guard.
- The "dropped unsupported value(s)" note over-reported. Identity fields are
  restored after the evidence gate (a project row cannot exist without them), and
  `notes` is a summary rather than a citable claim — but both were still listed as
  dropped. A false statement in the one place an operator looks to judge data
  quality is worse than no statement.
- Note lines contributed by an ingest record are now scoped to that record, so
  re-extracting an article *replaces* its own disclosures instead of leaving a
  stale variant beside the new one. Observed live: a corrected note appeared next
  to the wrong one it superseded.
- The possible-duplicate warning is derived rather than contributed, so it is
  recomputed from current state every run. It previously vanished when a record
  was re-ingested, even though the ambiguity had not been resolved.
- `--check` used a 16-token budget, but the M2.x/M3 models emit chain-of-thought
  inside `<think>` blocks in the content field, so the entire budget went to
  reasoning and the check reported a truncated thought instead of the answer. It
  now also reports `finish_reason` and whether the model spent thinking tokens.
- The test suite no longer reads the operator's `.env`. Deleting the environment
  variables was not enough; once a real `.env` existed, a test asserting "no key
  configured" passed on a clean machine and failed locally.
- The project's `.env` is read by absolute path. pydantic-settings resolves a
  relative `env_file` against the current directory, so the API key in
  `<project>/.env` was invisible whenever `tracker` ran from anywhere else —
  the normal case now the CLI is on PATH. A `.env` in the current directory is
  still read and still takes precedence.
- The missing-key error named `MINIMAX_API_KEY`; the variable actually read is
  `TRACKER_MINIMAX_API_KEY`.
- `migrations/`, the prompt files and the article cache are located relative to
  the installed package rather than the current directory. `tracker init` run from
  outside the project tree previously failed looking for a `migrations/` folder in
  the operator's home directory.
- Feed filtering normalizes URL slugs, so `data-center` matches the term
  `data cent` and `900MW` matches `mw`. Without it, matching against the URL —
  the only way to filter a sitemap entry or a feed with empty titles — never
  matched anything.
- Feed filtering matches the URL *path*, not the host. `datacenterfrontier.com`
  contains "datacent", so every article on a specialist outlet satisfied the topic
  tier from its domain name alone.
