# Handoff

## Yesterday (state at start of 2026-08-01)

2026-07-31 was `tracker enrich ID` (`9226d1c`) plus the whole web console build,
all in one day (`e65ac8f` through `694b204`, 15 commits): a pluggable search
backend (Brave, Serper, Bocha), operator newsrooms as first-party sources,
`tracker enrich --all` under a shared budget, the five-track stage model with
its derived signal, the 待确认 tier for unconfirmed values, tier 3 reasoned
inference, `--json` output carrying tiers and tracks, `tracker export html` (a
self-contained snapshot page), and `tracker serve` — a local console on
127.0.0.1 with six views (Projects, Map, Queue, Coverage, Commands, Runs) built
from the Meridian design system. 579+ tests, green offline.

Open items carried into today, per HANDOFF's own "Tomorrow": the 30-required-
projects gap, ERCOT/CAISO column names in `iso_maps.py` unverified against a
real export, and the two free Google CSE keys not yet configured to lift the
archive-reading ceiling.

## Today (2026-08-01)

Working tree had substantial uncommitted work extending the web console;
verified all 615 tests green offline before committing. Changes, largest first:

- **`tracker serve --tunnel`** publishes the console through a Cloudflare quick
  tunnel and **refuses to start without `TRACKER_CONSOLE_PASSWORD`** — refusing
  rather than warning, since there's no safe reading of "published and open" in
  front of a process that runs commands. New `webui/auth.py`: constant-time
  password check, server-side revocable session tokens, `HttpOnly`/`SameSite=Lax`
  cookie plus an Origin check, and a global 40-attempts/15-min lockout (not just
  per-IP, which doesn't defend a published URL against a rotating attacker). New
  `webui/tunnel.py` and `static/login.html`. `find_cloudflared` prefers the real
  executable over npm's `.CMD` shim after it silently produced a 7.9 MB fake
  binary in place of the real 54 MB one.
- **Honest density in the projects table**: columns now ordered by measured
  coverage from `gaps.measure()`, defaulting to >50%-populated with the rest one
  switch away. Visible cells went from ~46% to 95% populated. New per-row `9/12`,
  a coverage strip, and a distinct style for legitimately-null vs. unknown
  (`gaps.for_project`'s existing distinction, now actually surfaced).
- **Mobile layout** (below 720px, `app.css`): table becomes a card list, header
  collapses to logo + view strip, filters hide behind a count, map takes 60vh.
  940px of chrome above the first card dropped to 355px.
- **A Help view** and **`docs/`** (`architecture.md`, `guide.zh-CN.md`,
  `architecture.zh-CN.md`, plus a `docs/README.md` index) — the Help tab covers
  the console's own concepts (tiers, tracks, confidence) without duplicating the
  README; `architecture.md` is the first place the CLI/database/console
  relationship is written down.
- **Colour in the run log**: `runner.py` had been forcing `NO_COLOR=1`/
  `TERM=dumb`, discarding the CLI's own signalling exactly when someone reads the
  output. Now parsed back out of the ANSI stream (`static/ansi.js`) onto a fixed
  dark terminal surface. Root cause of the original Windows color failure:
  `FORCE_COLOR=1` alone still hit Rich's `legacy_windows` branch, which paints via
  the console API rather than escapes, and the markup was silently stripped.
- **Map zoom/pan** (wheel, drag, +/−/reset) — geography scales, marks
  deliberately don't, since the reason to zoom is overlapping bubbles.
- **`tracker ingest crawl --url URL`**, repeatable, alongside the existing
  `--urls FILE`; the Queue view's per-article Crawl button uses it.
- **Motion** on Meridian's tokens (row entrance, drawer fade, track fill,
  coverage bars, count-up header totals), all reachable by `prefers-reduced-motion`
  except the count-up timer, which checks the query itself.
- README gained a Documentation table indexing the new `docs/` files and the
  Help tab; `.env.example` documents `TRACKER_CONSOLE_PASSWORD` and why it's an
  env var rather than a `--password` flag (shell history, `ps` output).
- CHANGELOG.md and README.md were already current with the above — no
  housekeeping corrections needed. No AGENTS.md exists or was added: this is a
  single-CLI project with no multi-agent architecture to document.

## Tomorrow

- The 30-required-projects gap is still open.
- The two free Google CSE keys are still not configured; `tracker enrich`'s own
  measurement (17/94 projects with unread archive articles, from 07-31) is the
  reason to prioritize this.
- ERCOT/CAISO column-name assumptions in `iso_maps.py` remain unverified against
  a real export.
- `tracker serve --tunnel` and the auth gate are new and unverified against a
  real Cloudflare tunnel in this session — worth a live smoke test (start tunnel,
  hit the public URL, confirm the login page gates every route) before relying
  on it.
- No other in-progress or blocked work was left open at end of day.
