# The dc-tracker crawler

This page is for publishers and site operators who found `dc-tracker` in their
logs. It says what the crawler is, what it requests, how often, what it keeps,
and how to ask for a change. The crawler's User-Agent points here.

## What it is

dc-tracker is a small research tool that tracks US data-center construction:
where campuses are planned, how many megawatts they claim, and what permits,
power deals and obstacles move them. It reads public reporting and company
filings, extracts those facts, and keeps each one with a short quote and a link
to the article it came from. A handful of analysts use it, through a private,
account-gated console. It is not a search engine, does not republish articles,
and does not train AI models.

## How to recognise it

Every request the project makes sends the User-Agent

```
dc-tracker/0.1 (+https://github.com/nathanz3491/dc-tracker/blob/main/docs/crawler.md; <contact address>)
```

That covers feeds, sitemaps, articles, the date check, a company's site icon,
and the page a reader opens from inside the console. The string does not
change when the crawler falls back to a different HTTP client (below).

## What it requests, and how often

- **Feeds and sitemaps** that are listed by hand in
  [`tracker/seed/feeds.toml`](../tracker/seed/feeds.toml). Feeds are polled once a
  night. Sitemaps are walked less often, at most four child sitemaps and 5,000
  URLs a walk.
- **Article pages** whose headline or URL mentions a data-center project, drawn
  from those feeds and sitemaps or from a web search.
- **A publication-date check**: one request each for pages that recorded no
  date, at most 150 a night. A page that still gives no date is not asked again
  for 90 days.
- **A re-read of an article** it already cites, every few weeks, to see whether
  the story changed. Each failed re-read doubles the wait.

Across every publisher together, that comes to a few hundred page requests a
night, mostly between 18:00 and 08:00 China Standard Time (UTC+8).

## How it behaves

- **It reads `robots.txt` before it requests anything from a site**, and follows
  it. That covers every request: articles, feeds, sitemaps, the date check, the
  reader view and a company's site icon. A `User-agent: dc-tracker` group applies
  to it, and so does `User-agent: *`. The file is read the way RFC 9309 says. A
  4xx means there is no file, so nothing is disallowed. A 5xx or no answer means
  the site cannot say, so nothing is fetched for half an hour, then it asks again.
  A file that was read is kept for a day.
- **`Crawl-delay` is honoured**, up to 60 seconds between two requests to the same
  site. A longer delay is treated as 60, and on the few pages a night it reads from
  any one site that is the same thing.
- **One request at a time per site**, at least one second apart (or the site's
  `Crawl-delay`), and no more than four in flight across all sites. Requests time
  out after 30 seconds.
- **Retries** happen only on transient errors (408, 425, 429, 5xx), at most three
  attempts with exponential backoff. It never retries 401, 402, 404, 410 or 451.
- **On a 403, 429 or 503**, it may try the same page once more with a client
  that presents a browser's TLS handshake. It still sends the User-Agent above.
  If a headless browser is enabled, that is tried last. Neither is tried for a
  page `robots.txt` disallows.
- **A site that challenges every request is left alone.** If a site puts every
  page, its feed and its `robots.txt` behind a challenge, it is marked closed in
  `feeds.toml`. The crawler does not try to get past a challenge. A closed site
  gets one request a week to see whether it has reopened. Its pages go to the
  back of every crawl queue and are requested only when nothing else is left.
  The console's reader view does not request them at all.
- **Pages are cached.** An article that was read once is served from a local
  copy after that. The nightly jobs re-read that copy rather than the site.

## What it keeps

- **Facts with their source**: a project's capacity, location, dates and
  milestones. Each fact carries the sentence it came from and the article URL.
  The excerpt stored per source is capped at 500 characters by the database
  schema.
- **A local copy of the article text**, on disk rather than in the database, so
  a fact can be checked again without a new request.
- **Inside the private console**, a signed-in reader can open a cited article in
  a reader view to check a quote. The view always links to the original page.

Article text is sent to a commercial language-model API to extract the facts.
The project does not train models on it.

## Asking for a change

To slow it down, cap it, exclude a section, or stop it entirely, open an issue on
this repository or write to the address in the User-Agent string. Exclusion
takes effect from the next night. If you'd rather the crawler identify itself
with a token you issue, or come from a fixed address, say so in the issue.
