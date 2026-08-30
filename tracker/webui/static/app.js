/* The dc-tracker console.
 *
 * A port of the `DC Tracker Console.dc.html` mockup to real React. The mockup's
 * logic was already a pure data function — every style string, chip and track
 * segment computed in one `renderVals()` — so this is mostly the same code with
 * `{{ }}` become `${}` and `<sc-for>` become `.map`.
 *
 * One thing the port deliberately does NOT carry over. The mockup re-derived
 * track progress in JavaScript with its own shortcuts ("phase is operational, so
 * the power track is complete"). `tracker/tracks.py` already does that properly
 * and refuses exactly that inference: building ahead of power is routine and a
 * finished shell waiting on a substation is the single most valuable signal in
 * the dataset. So `project.standing` comes from the API and nothing here guesses.
 */

import { HelpView } from "/static/views-help.js";

const html = htm.bind(React.createElement);
const { useState, useEffect, useMemo, useRef, useCallback } = React;

/* A fragment, so one loop iteration can emit a group header plus its row without
   wrapping them in a div — the 5-column `dc-blockrow` grid is a direct child of the
   table container, and an extra element between them breaks the alignment. */
const Frag = React.Fragment;
const NS = window.MeridianDesignSystem_6e9015 || {};
const {
  Button, Card, CardHeader, CardTitle, CardDescription, Input, Select, Switch,
  Table, TableHeader, TableBody, TableRow, TableHead, Tabs, TabsList, TabsTrigger,
  StatCard, EmptyState, Skeleton, Glyph, Badge, Alert,
  /* Vendored with the bundle and unused until the sources page needed a large
     modal. It already handles Escape and locks body scroll — the two things a
     hand-rolled dialog gets wrong. */
  Dialog, DialogContent,
} = NS;

/* ---- vocabulary ---------------------------------------------------------- */

const TRACKED = ["name", "company", "customer", "city", "state", "mw_planned",
  "mw_built", "investment_usd", "phase", "first_announced", "expected_online", "blocker"];
/* Shown behind the "all fields" switch, with the tracked twelve. `h200_equivalent`
 * belongs here rather than in TRACKED: those twelve are the PRD's definition of
 * done and "9 of 12" is quoted in the docs, the header and the export, so a
 * thirteenth would silently restate every one of those numbers. */
const AUDIT = ["h200_equivalent", "county", "lat", "lon", "confidence", "last_verified_at"];
const RIGHT = new Set(["mw_planned", "mw_built", "investment_usd", "h200_equivalent",
                       "confidence", "lat", "lon"]);

/* Coverage at or above this shows by default. Chosen so the default table is
 * mostly populated rather than mostly dashes; everything below it is one switch
 * away and the switch says how many. */
const DENSE_THRESHOLD = 50;

/* One definition of "does this project match what I typed", shared by the table
 * filter and every project picker in the command form.
 *
 * They had drifted: the table searched six text fields and not the id, and the
 * pickers did not search at all — they were 224-option dropdowns. Both are the
 * same question, so both ask it here.
 *
 * `#42` means the id and nothing else. A bare `42` stays a substring match,
 * because it is also a capacity, a year and part of a name. */
function matchesProject(p, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if (q.startsWith("#")) return String(p.id) === q.slice(1).trim();
  const hay = [p.id, p.name, p.company, p.customer, p.city, p.county, p.state, p.blocker]
    .filter((v) => v != null && v !== "").join(" ").toLowerCase();
  // Every word has to appear, so "meta ohio" narrows rather than widens.
  return q.split(/\s+/).every((word) => hay.includes(word));
}

/* label, and the sentence shown when a value has no quote of its own. */
const TIER = {
  reported:    ["quoted", "reported"],
  derived:     ["derived", "derived"],
  unconfirmed: ["待确认", "unconfirmed"],
  inferred:    ["inferred", "inferred"],
  defaulted:   ["default", "defaulted"],
  missing:     ["null", "missing"],
  na:          ["n/a", "not applicable"],
};
const TIER_NOTE = {
  missing: "Empty. Nobody has said. That is often the right answer rather than a gap — most projects have no problem to report and no named tenant.",
  defaulted: "Nobody has said, and this field cannot be left blank, so it is showing the fallback. Treated as a guess, not a fact.",
  unconfirmed: "待确认 — a source gave this figure but we could not find a sentence proving it. Kept so it can be checked later, and it counts for nothing until then.",
};
/* Phase hue lives on the map only, where a legend explains it. */
const PHASE_TOKEN = {
  announced: "--chart-5", permitting: "--chart-1", construction: "--chart-3",
  operational: "--chart-2", paused: "--warning", cancelled: "--muted-foreground",
};
const SEV_TOKEN = { watch: "--muted-foreground", material: "--warning", blocking: "--danger" };
const SEV_ORDER = ["watch", "material", "blocking"];

/* ---- formatting ---------------------------------------------------------- */

/* The trillions branch is not hypothetical. Summing investment by buyer put
   OpenAI at $3.2tn — inflated by duplicate rows and programme totals, but real
   output all the same, and it printed as "$3215B", which reads as a typo. */
const fmtUSD = (v) => v == null ? "—"
  : v >= 1e12 ? "$" + (v / 1e12).toFixed(1).replace(/\.0$/, "") + "T"
  : v >= 1e9 ? "$" + (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B"
  : v >= 1e6 ? "$" + Math.round(v / 1e6) + "M"
  : "$" + v.toLocaleString();

function fmt(key, v) {
  if (v == null || v === "") return "—";
  if (key === "mw_planned" || key === "mw_built") return Number(v).toLocaleString();
  if (key === "investment_usd") return fmtUSD(v);
  // Six or seven digits in a table cell are unreadable, and the underlying
  // figure is two significant figures anyway — "690k" says everything "690,000"
  // does without implying the extra precision.
  if (key === "h200_equivalent") {
    const n = Number(v);
    return n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M"
         : n >= 1e3 ? Math.round(n / 1e3) + "k"
         : String(n);
  }
  if (key === "lat" || key === "lon") return Number(v).toFixed(4);
  if (key === "first_announced" || key === "expected_online") return String(v);
  if (key === "last_verified_at" || key === "updated_at") return String(v).slice(0, 10);
  return String(v);
}
/* The same conversion `tracker/compute.py` does, used only to tell a derived
 * count from a cited one so the drawer can label it. Kept in step by the ratio
 * the backend ships in the dataset — never hard-coded here, because two copies
 * of an assumption drift and the label would then lie about provenance. */
function h200FromMw(mw, kwEach) {
  if (mw == null || Number(mw) < 0.1) return null;
  const exact = (Number(mw) * 1000) / (kwEach || H200_KW);
  const magnitude = Math.floor(Math.log10(exact));
  const step = Math.pow(10, Math.max(0, magnitude - 1));
  return Math.round(exact / step) * step;
}
let H200_KW = 1.3;  // replaced from the dataset on load

const place = (p) => (p.city || (p.county ? p.county + " Co." : "")) + (p.state ? ", " + p.state : "");
const chip = (token, outline) => {
  const t = `var(${token})`;
  return {
    display: "inline-flex", alignItems: "center", height: 22, padding: "0 9px",
    borderRadius: 999, fontFamily: "var(--font-mono)", fontSize: 12,
    letterSpacing: ".02em", whiteSpace: "nowrap",
    ...(outline
      ? { background: "transparent", color: "var(--foreground)", border: "1px solid var(--input)" }
      : { background: `color-mix(in oklab, ${t} 14%, transparent)`, color: t,
          border: `1px solid color-mix(in oklab, ${t} 32%, transparent)` }),
  };
};

/* Which tier a field sits at, and the sentence behind it. Both come from the
   API — `gaps.provenance()` — so the page never has to reason about evidence. */
const provOf = (p, key) => (p.prov || {})[key] || null;

/* The claim envelope (migration 0015): what a value is a value *of*, and how
 * exactly the article stated it. Rendered as one glyph on the number rather than
 * as columns of its own — these qualify a figure, they are not figures, and a
 * `bound` column would be empty on most rows and would break the numeric
 * alignment on the rest.
 *
 * Silent unless an axis has something to say. That is the whole discipline: the
 * previous round of added fields were rendered unconditionally, so they were
 * noise on the 190 rows where nothing was in dispute. */
const axesOf = (p, key) => provOf(p, key)?.axes || null;
/* `at_least` is a SUFFIX — "350+" rather than "≥350" — because that is how a
 * reader outside this codebase writes "or more", and the floor is the case that
 * matters most here: Fairwater's 350 MW rests on "Each exceeds 350 MW". The other
 * two stay prefixes, where they read naturally and where no plain-ASCII suffix
 * exists that is not ambiguous with a minus sign. */
const BOUND_PREFIX = { approximate: "~", at_most: "≤" };
const BOUND_SUFFIX = { at_least: "+" };
const withBound = (text, bound) =>
  text == null || text === "" || !bound || bound === "exact"
    ? text
    : `${BOUND_PREFIX[bound] || ""}${text}${BOUND_SUFFIX[bound] || ""}`;
const SCOPE_NOTE = {
  programme: "a programme-wide figure, not this campus",
  region: "economic impact on the region, which is not capex",
  portfolio: "the operator's whole estate, not this site",
  unnamed: "the article did not say what this figure covers",
};
const MODALITY_NOTE = {
  speculated: "reported as a rumour, not stated",
  targeted: "a target, not a commitment",
  contracted: "signed, filed or committed",
  achieved: "已完成 — the article says this has happened",
};
/* A year-precision date rendered as `2024-01-01` asserts a precision nobody
 * published. `normalize.parse_date` has always known the difference. */
const DATE_PRECISION_FMT = { year: 4, month: 7, quarter: 4, half: 4 };
const tierOf = (p, key) => (p[key] == null || p[key] === "") ? "missing" : (provOf(p, key)?.tier || "reported");
/* Why a 待确认 value is 待确认.
 *
 * One tier, two causes, opposite remedies. The ordinary one is that nothing
 * quotable backs the value, and the fix is another source. The other is that the
 * quote is real and the figure is not this site's — a programme total lifted from
 * an article about one campus — and the fix is a correction, because going
 * looking for a citation would find one and it would still be wrong.
 *
 * Recorded by the ingest path, read back here. Never recomputed in the browser:
 * that would sometimes accuse a figure no gate ever demoted. */
const whyUnconfirmed = (p, key) =>
  tierOf(p, key) === "unconfirmed" ? (p.unconfirmed_because || {})[key] || null : null;

function quoteOf(p, key) {
  const tier = tierOf(p, key);
  const pr = provOf(p, key);
  if (tier === "missing") {
    // If the backend knows the field cannot apply here, say which reason it is
    // rather than the generic "no source asserted this".
    const why = (p.nulls || {})[key];
    if (why?.status === "not_applicable" && why.reason) {
      return { text: `Not applicable — ${why.reason}`, exact: false };
    }
    if (why?.reason) return { text: `${TIER_NOTE.missing}\n\n${why.reason}`, exact: false };
    return { text: TIER_NOTE.missing, exact: false };
  }
  if (pr?.quote) return { text: pr.quote, exact: !!pr.quote_is_exact, url: pr.source_url };
  return { text: TIER_NOTE[tier] || "No excerpt recorded for this value.", exact: false };
}

/* ---- api ----------------------------------------------------------------- */

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options?.body ? JSON.stringify(options.body) : undefined,
  });

  // An expired session used to be invisible: the page kept showing the data it
  // had, every request 401'd silently, and each feature reported its own
  // misleading local reason — the 3D map said "unavailable offline" when what
  // had actually happened was that the cookie ran out. Stale numbers that look
  // live are worse than no page, so go back to the gate.
  if (res.status === 401 && !path.endsWith("/login")) {
    window.location.replace("/");
    // Never settles: the reload is already in flight and resolving here would
    // let callers render an error for the frame before it happens.
    return new Promise(() => {});
  }

  const text = await res.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    // NOT `{error: text}`. When the console is behind a tunnel or a proxy and the
    // server is down — a restart, say — what comes back is the *provider's* HTML
    // error page, and taking its markup as an error message dumped a full
    // Cloudflare 502 document into the console's error box under the heading "the
    // console could not read the database". Two wrong statements at once: it is
    // not the database, and the console never answered at all.
    payload = { error: null, html: text };
  }
  // The body travels with the error. A refusal sometimes carries more than a
  // sentence — the confirmation word for a command, say — and the caller cannot
  // get at it if only the message survives.
  if (!res.ok) {
    // `unreachable` means "something in front of the console answered instead of
    // it", and a JSON `error` body is proof that it did not — that shape comes
    // from `Handler._error` and from nowhere else. Without this test the console's
    // own 503s were reported as the console being down, which is the opposite of
    // what happened and sends the reader to check the tunnel.
    const answered = payload?.error != null;
    throw Object.assign(new Error(payload?.error || _gatewayReason(res) || res.statusText),
                        { status: res.status, payload,
                          unreachable: _isGateway(res.status) && !answered });
  }
  return payload;
}

/* 502/503/504 from in front of the console: something between the browser and
 * the server answered instead of it. Worth its own name because the honest
 * message is the opposite of an application error — the console is fine and
 * unreachable, and the reader should wait rather than investigate. */
function _isGateway(status) {
  return status === 502 || status === 503 || status === 504;
}

function _gatewayReason(res) {
  if (!_isGateway(res.status)) return null;
  return `the console did not answer (HTTP ${res.status}). It is probably restarting, or the `
       + `tunnel in front of it has not reconnected yet.`;
}

/* POST, then read server-sent events off the response body.
 *
 * `EventSource` is GET-only, and the two things that stream here both spend
 * money — a GET that costs money is one a back button will happily re-issue. So
 * the request stays a POST and we parse the frames by hand, which is about
 * fifteen lines and buys the right verb. */
async function apiStream(path, body, onEvent, signal) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (res.status === 401) { window.location.replace("/"); return new Promise(() => {}); }
  if (!res.ok) {
    const text = await res.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
    throw Object.assign(new Error(payload?.error || text || res.statusText), { status: res.status });
  }

  const reader = res.body.getReader();
  const decode = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decode.decode(value, { stream: true });
    // Frames are separated by a blank line; a partial one stays in the buffer.
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const payload = frame.split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (!payload) continue;
      try { onEvent(JSON.parse(payload)); } catch { /* a torn frame costs a few words */ }
    }
  }
}

/* The one thing CSS cannot express here: below the breakpoint the table is not
 * restyled, it is replaced by a different component. Everything else that
 * changes on a narrow screen is a media query in app.css. */
const NARROW = "(max-width: 720px)";

function useMedia(query) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/* A number that counts up to its value on first sight.
 *
 * Only on first arrival. Re-animating on every filter keystroke would turn the
 * header into a slot machine, so the target is compared against what was last
 * animated to and a change of *filter* is not a change of *fact*.
 *
 * The one animation in the console driven by a timer rather than by CSS, so it
 * has to check `prefers-reduced-motion` itself — the global rule in base.css
 * cannot reach it. */
function useCountUp(value, { ms = 420 } = {}) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    const target = Number(value) || 0;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(target);
      from.current = target;
      return;
    }
    const start = performance.now();
    const origin = from.current;
    if (origin === target) return;
    let raf = 0;
    const tick = (now) => {
      const t = Math.min(1, (now - start) / ms);
      // Same curve as --ease-spring, so it settles like everything else.
      const eased = 1 - Math.pow(1 - t, 3);
      setShown(Math.round(origin + (target - origin) * eased));
      if (t < 1) raf = requestAnimationFrame(tick);
      else from.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, ms]);
  return shown;
}

const Counted = ({ value }) => html`${useCountUp(value).toLocaleString()}`;

/* ---- small shared pieces ------------------------------------------------- */

/* The identity mark: a citation bracket with a bar that starts at the source and
 * stops where the evidence stops, so the empty half of the bracket is the
 * governing rule drawn literally — an unpublished figure stays null rather than
 * guessed.
 *
 * Inline and in `currentColor` rather than an <img> to a file: it is 168 bytes,
 * so a request would cost more than the drawing, and filling from the cascade is
 * what lets one copy serve both themes — `--primary` is honey #a05e1c on cream
 * and #dca75f on espresso — without a second asset or a media query.
 *
 * `aria-hidden`, because the wordmark it sits beside already says the name; a
 * label here would make a screen reader announce it twice. */
const Mark = ({ size = 20, color = "var(--primary)" }) => html`
  <svg viewBox="0 0 24 24" width=${size} height=${size} fill="currentColor"
       aria-hidden="true" style=${{ display: "block", flex: "none", color }}>
    <path d="M2 2h6v3H5v14h3v3H2zM22 2h-6v3h3v14h-3v3h6z" />
    <path d="M5 10h8v4H5z" />
  </svg>`;

/* One log line, with its ANSI colour intact.
 *
 * Every run is a React child, so the text is escaped on the way in — log lines
 * carry URLs and headlines fetched from the open web, and hand-built markup is
 * how one of those becomes a script tag. */
function Eyebrow({ figure, title, children }) {
  const [open, setOpen] = useState(false);
  return html`
    <div style=${{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                      letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>${figure}</span>
      <h1 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                    letterSpacing: "-0.02em", lineHeight: 1.15 }}>${title}</h1>
      ${children && open && html`
        <p class="dc-intro" style=${{ margin: "2px 0 0", fontSize: 14, lineHeight: "22px",
                                      color: "var(--muted-foreground)", maxWidth: "78ch" }}>${children}</p>`}
      ${children && html`
        <button type="button" class="dc-intro-toggle" aria-expanded=${open}
                onClick=${() => setOpen(!open)}>${open ? "less" : "what is this?"}</button>`}
    </div>`;
}

/* A value, with the sentence behind it one hover — or one tap — away.
 *
 * Hover alone made the product's central interaction unreachable on a phone: no
 * pointer, no quote, and the underline styles sat there implying something you
 * could not do. So a click toggles it too, which costs a mouse user nothing and
 * gives a touch user the feature at all.
 *
 * `stopPropagation`, because in the table a row click opens the drawer and on a
 * card the whole thing is a button — without it, asking for a quote would also
 * navigate.
 *
 * Keyboard deliberately does NOT get a tab stop here. Twelve fields across 124
 * rows is 1,488 of them, which would wreck the one thing keyboard navigation is
 * for. The keyboard path to the same evidence already exists and is better: the
 * row is focusable, Enter opens the drawer, and the drawer lists every field
 * with its tier and its quote inline.
 */
function Value({ project, field, text, extra, onQuote }) {
  const tier = tierOf(project, field);
  // An empty cell that is empty *correctly* reads differently from one that is
  // simply unknown.
  const na = tier === "missing" && (project.nulls || {})[field]?.status === "not_applicable";
  const axes = axesOf(project, field);
  // Two qualifiers that cost no space: the hedge the article used, and the
  // precision it gave a date to. Both make the cell *shorter* — "2024" instead
  // of "2024-01-01" — while claiming less.
  let shown = text ?? fmt(field, project[field]);
  if (axes && text == null) {
    const cut = DATE_PRECISION_FMT[axes.date_precision];
    if (cut && typeof shown === "string") shown = shown.slice(0, cut);
    shown = withBound(shown, axes.bound);
  }
  return html`
    <span class=${`dc-v dc-v--${na ? "na" : tier}`} style=${extra}
          onMouseEnter=${(e) => onQuote(e, project, field)}
          onMouseLeave=${() => onQuote(null, project, field, { hover: true })}
          onClick=${(e) => { e.stopPropagation(); onQuote(e, project, field, { sticky: true }); }}
          >${shown}</span>`;
}

/* Column order and the default column set, both taken from measurement.
 *
 * Hand-picking which columns to show would be a way of choosing how full the
 * table looks. Sorting them by their real coverage is not: the sparse fields are
 * still there, one click away, and the switch says how many it is hiding. As
 * coverage improves, columns promote themselves.
 */
function useColumns(data, showAll) {
  return useMemo(() => {
    const pct = Object.fromEntries(
      (data.gaps?.fields || []).map((g) => [g.field, g.measurable ? g.pct : null]),
    );
    // An unmeasurable field (blocker, customer) sorts last but keeps its real
    // fill count — it has no percentage because absence there carries no
    // information, not because nobody counted.
    const rank = (f) => (pct[f] == null ? -1 : pct[f]);
    const ordered = [...TRACKED].sort((a, b) => rank(b) - rank(a));
    const dense = ordered.filter((f) => rank(f) >= DENSE_THRESHOLD);
    return {
      ordered,
      visible: showAll ? ordered : dense,
      hidden: ordered.length - dense.length,
      pct,
    };
  }, [data, showAll]);
}

/* The five-segment strip. Reached segments are ink; a segment merely *implied*
   by a later milestone on another track is half-strength, because a deduction is
   not a citation; a blocked track's remaining segments turn rose. */
function TrackStrip({ standing, tracks }) {
  return html`<div style=${{ display: "inline-flex", gap: 2 }}>
    ${standing.tracks.map((t) => {
      const total = (tracks.find((x) => x.key === t.track) || {}).milestones?.length || 1;
      const reached = t.reached.length;
      const onlyImplied = reached > 0 && t.reached.every((m) => t.implied.includes(m));
      const cls = reached === 0
        ? (t.blockers.length ? "dc-seg-cell--blocked" : "dc-seg-cell--todo")
        : onlyImplied ? "dc-seg-cell--implied" : "dc-seg-cell--reached";
      return html`<span key=${t.track} class=${`dc-seg-cell ${cls}`}
                        style=${{ width: Math.max(8, 26 / total) }} />`;
    })}
  </div>`;
}

/* ---- quote popover ------------------------------------------------------- */

function QuotePopover({ quote }) {
  if (!quote) return null;
  const tier = TIER[quote.tier] || TIER.reported;
  return html`
    <div class="dc-pop" style=${{
      position: "fixed", zIndex: 70, left: quote.x, top: quote.y, width: "min(400px, 86vw)",
      padding: "13px 15px", background: "var(--surface)", border: "1px solid var(--border)",
      borderRadius: 14, boxShadow: "var(--shadow-pop)", pointerEvents: "none" }}>
      <div style=${{ display: "flex", alignItems: "center", gap: 8, marginBottom: 7, flexWrap: "wrap" }}>
        <span style=${chip(quote.tier === "reported" ? "--foreground"
          : quote.tier === "unconfirmed" ? "--warning"
          : quote.tier === "inferred" ? "--chart-5" : "--muted-foreground")}>${tier[0]}</span>
        ${quote.why && html`<span style=${chip("--danger")}>not this site's figure</span>`}
        ${/* The scope chip. This is the one Hyperion needed: three investment
              figures on one row, none of them in conflict, because $10B was the
              buildout, $27B the campus JV and $50B the regional economic impact.
              Shown only when the scope is something other than this campus —
              a chip on every row saying "this site" would be noise. */
          quote.scope && quote.scope !== "this_site" && html`
          <span style=${chip(quote.scope === "unnamed" ? "--muted-foreground" : "--warning")}
                title=${SCOPE_NOTE[quote.scope] || ""}>
            ${quote.scope.startsWith("block:") ? quote.scope.slice(6) : quote.scope}
          </span>`}
        ${/* Modality, and likewise silent on the default. A target read as an
              achievement is how a 2027 milestone came to count as reached. */
          quote.modality && quote.modality !== "planned" && html`
          <span style=${chip(quote.modality === "achieved" ? "--success" : "--muted-foreground")}
                title=${MODALITY_NOTE[quote.modality] || ""}>${quote.modality}</span>`}
        ${quote.exact === false && quote.hasQuote && html`
          <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
            from the source excerpt, not this field's own sentence
          </span>`}
      </div>
      ${quote.why && html`
        <p style=${{ margin: "0 0 8px", fontSize: 12, lineHeight: "18px", color: "var(--danger)" }}>
          ${quote.why.note}
        </p>`}
      <p style=${{ margin: 0, fontSize: 14, lineHeight: "21px" }}>
        ${quote.exact ? `“${quote.text}”` : quote.text}
      </p>
    </div>`;
}

function useQuote() {
  const [quote, setQuote] = useState(null);
  //: A tapped quote stays until dismissed; a hovered one follows the pointer.
  //: Without the distinction, moving the mouse off a cell would instantly close
  //: a popover the reader had just tapped open.
  const sticky = useRef(false);

  const show = useCallback((event, project, field, opts = {}) => {
    if (event === null) {
      if (opts.hover && sticky.current) return; // mouse left; the tap holds it
      sticky.current = false;
      return setQuote(null);
    }
    const key = `${project.id}:${field}`;
    if (opts.sticky && sticky.current && quote?.key === key) {
      // Tapping the same value again closes it.
      sticky.current = false;
      return setQuote(null);
    }
    if (opts.sticky) sticky.current = true;

    const r = event.currentTarget.getBoundingClientRect();
    const W = 400, H = 150;
    const below = r.bottom + H + 16 < window.innerHeight;
    const q = quoteOf(project, field);
    setQuote({
      key,
      x: Math.max(10, Math.min(r.left, window.innerWidth - W - 12)),
      y: below ? r.bottom + 8 : Math.max(10, r.top - H - 8),
      text: q.text, exact: q.exact, hasQuote: !!provOf(project, field)?.quote,
      tier: tierOf(project, field), sticky: !!opts.sticky,
      why: whyUnconfirmed(project, field),
      scope: axesOf(project, field)?.scope,
      modality: axesOf(project, field)?.modality,
    });
  }, [quote]);

  // A tapped popover needs a way out that is not another tap on the same cell.
  useEffect(() => {
    if (!quote?.sticky) return;
    const dismiss = () => { sticky.current = false; setQuote(null); };
    const onKey = (e) => { if (e.key === "Escape") dismiss(); };
    // Bubble phase, not capture. A value's own handler calls stopPropagation, so
    // a tap on a value never reaches this — which is what lets tapping the same
    // value twice toggle it shut instead of closing and immediately reopening.
    document.addEventListener("click", dismiss);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", dismiss, { passive: true });
    return () => {
      document.removeEventListener("click", dismiss);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", dismiss);
    };
  }, [quote?.sticky, quote?.key]);

  return [quote, show];
}

/* ---- Projects ------------------------------------------------------------ */

/* What the dataset actually rests on, in the operator's line of sight.
 *
 * Three strongest and three weakest measurable fields with their true numerators
 * and denominators. Showing the weak end is the point — the strip is there so the
 * table's completeness is a stated fact rather than an impression, and half a
 * fact would be worse than none. */
function CoverageStrip({ data }) {
  const measurable = (data.gaps?.fields || []).filter((g) => g.measurable && g.applicable > 0);
  if (measurable.length < 4) return null;
  const sorted = [...measurable].sort((a, b) => b.pct - a.pct);
  const best = sorted.slice(0, 3);
  const worst = sorted.slice(-3).reverse();
  const tint = (pct) => (pct >= 80 ? "--success" : pct >= 50 ? "--chart-1" : "--warning");

  const cell = (g) => html`
    <div key=${g.field} style=${{ display: "grid", gap: 4, minWidth: 116 }}>
      <div style=${{ display: "flex", alignItems: "baseline", gap: 6 }}>
        <span class="dc-num" style=${{ fontSize: 15, fontWeight: 600, color: `var(${tint(g.pct)})` }}>
          ${g.pct}%</span>
        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11,
                        color: "var(--muted-foreground)" }}>${g.field}</span>
      </div>
      <div style=${{ height: 4, borderRadius: 999, background: "var(--muted)", overflow: "hidden" }}>
        <div class="dc-bar" style=${{ height: "100%", width: `${g.pct}%`, borderRadius: 999,
                                      background: `var(${tint(g.pct)})` }} />
      </div>
      <span class="dc-num" style=${{ fontSize: 11, color: "var(--muted-foreground)" }}>
        ${g.filled} of ${g.applicable}</span>
    </div>`;

  return html`
    <div class="dc-cover">
    <${Card}>
      <div style=${{ display: "flex", flexWrap: "wrap", gap: "16px 28px", padding: "14px 20px",
                     alignItems: "flex-start" }}>
        <div style=${{ minWidth: 150, maxWidth: 260 }}>
          <div style=${{ fontSize: 12, fontWeight: 500, textTransform: "uppercase",
                         letterSpacing: "0.08em", color: "var(--muted-foreground)" }}>coverage</div>
          <p style=${{ margin: "4px 0 0", fontSize: 12, lineHeight: "17px",
                       color: "var(--muted-foreground)" }}>
            Measured against the rows where each field can legitimately be set, not against
            every project.
          </p>
        </div>
        <div style=${{ display: "flex", flexWrap: "wrap", gap: "14px 20px" }}>${best.map(cell)}</div>
        <div style=${{ display: "flex", flexWrap: "wrap", gap: "14px 20px", opacity: 0.85 }}>
          ${worst.map(cell)}
        </div>
      </div>
    <//>
    </div>`;
}

/* One project as a card. The phone form of a table row: the six facts worth
 * having at a glance, and the same drawer behind a tap. */
function ProjectCard({ p, data, open, onOpen, onQuote }) {
  const blocking = p.risks.some((r) => r.status === "open" && r.severity === "blocking");
  return html`
    <button type="button" class=${`dc-pcard${open ? " dc-pcard--open" : ""}`}
            onClick=${() => onOpen(p.id)}
            aria-label=${`${p.company} ${p.name}, ${place(p)}, confidence ${p.confidence}`}>
      <div class="dc-pcard-top">
        <span class="dc-pcard-name">${p.company} — ${p.name}</span>
        <span style=${chip(p.confidence >= 3 ? "--success" : p.confidence === 2 ? "--chart-1" : "--warning")}>
          ${p.confidence}</span>
      </div>
      <div class="dc-pcard-meta">
        <span>${place(p)}</span><span>·</span><span>${p.phase}</span>
        ${blocking && html`<span style=${{ color: "var(--danger)" }}>· blocking</span>`}
      </div>
      <div class="dc-pcard-facts">
        <${Value} project=${p} field="mw_planned" onQuote=${onQuote}
          text=${p.mw_planned == null ? "no cited MW" : `${p.mw_planned.toLocaleString()} MW`}
          extra=${{ fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", fontSize: 13 }} />
        <${Value} project=${p} field="investment_usd" onQuote=${onQuote}
          extra=${{ fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", fontSize: 13 }} />
        <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
          ${p.filled}/12 fields</span>
      </div>
      <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
        <${TrackStrip} standing=${p.standing} tracks=${data.tracks} />
        <span style=${{ fontSize: 11, color: "var(--muted-foreground)", minWidth: 0,
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          ${p.standing.watch_for ? `watch for ${p.standing.watch_for}` : "nothing outstanding"}</span>
      </div>
    </button>`;
}

const BLANK_FILTERS = { q: "", state: "", phase: "", conf: "", risk: "", severity: "", quoted: false };

function ProjectsView({ data, onOpen, openId }) {
  const [f, setF] = useState(BLANK_FILTERS);
  const [wide, setWide] = useState(false);
  const [sort, setSort] = useState({ key: "confidence", dir: "desc" });
  const [quote, showQuote] = useQuote();
  const set = (k) => (v) => setF((prev) => ({ ...prev, [k]: v }));
  const cols = useColumns(data, wide);
  const narrow = useMedia(NARROW);
  // Six stacked selects is 558px of filter card before any data. Collapsed by
  // default on a phone; search stays out, because it is the one control that
  // gets used constantly.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [showCoverage, setShowCoverage] = useState(false);
  const activeFilters = Object.entries(f).filter(([k, v]) => k !== "q" && v !== "" && v !== false).length;

  const rows = useMemo(() => {
    const q = f.q.trim().toLowerCase();
    const out = data.projects.filter((p) => {
      // The id is searchable, and `#42` finds only #42. Everything else on the
      // page identifies a project by id — the drawer header, the run log, every
      // command that takes one — and the table was the one place you could not
      // type one in. A bare `42` still matches anything containing "42", because
      // that is also a capacity and a year somebody may be looking for.
      if (q && !matchesProject(p, q)) return false;
      if (f.state && p.state !== f.state) return false;
      if (f.phase && p.phase !== f.phase) return false;
      if (f.conf && p.confidence < Number(f.conf)) return false;
      if (f.risk && !p.risks.some((r) => r.status === "open" && r.category === f.risk)) return false;
      if (f.severity && !p.risks.some((r) => r.status === "open" && r.severity === f.severity)) return false;
      // "Quoted only" is about the values, not the score: keep rows where every
      // populated tracked field rests on a verbatim quote or a lookup.
      if (f.quoted && TRACKED.some((k) => ["unconfirmed", "inferred", "defaulted"].includes(tierOf(p, k)))) return false;
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return out.sort((a, b) => {
      const x = a[sort.key], y = b[sort.key];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
    });
  }, [data, f, sort]);

  const columns = ["id", ...cols.visible, ...(wide ? AUDIT : [])];
  const clean = JSON.stringify(f) === JSON.stringify(BLANK_FILTERS);
  const states = useMemo(() => [...new Set(data.projects.map((p) => p.state))].sort(), [data]);

  const field = (id, label, node) => html`
    <div key=${id} style=${{ display: "grid", gap: 5 }}>
      <label for=${id} style=${{ fontSize: 12, fontWeight: 500, textTransform: "uppercase",
                                 letterSpacing: "0.08em", color: "var(--muted-foreground)" }}>${label}</label>
      ${node}
    </div>`;
  const options = (values) => values.map((v) => html`<option key=${v} value=${v}>${v}</option>`);

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 01 — projects" title="Every project, and where each number came from">
        The underline under a value says who vouches for it — a quoted source, a lookup, a model's guess,
        or nobody. Hover it, or tap it on a phone, to read the exact sentence behind it. Click a row for
        everything we hold on that project.
      <//>

      <${Card}>
        ${/* Search stays out of the fold at every width: it is the one control
             that gets used constantly, and six stacked selects is 558px of card
             before any data. The count on the button is what makes a collapsed
             filter honest — a hidden active filter is how a table lies. */ ""}
        <div style=${{ display: "grid", gap: 10, padding: "14px 16px" }}>
          <${Input} size="sm" placeholder="name, city, operator…" value=${f.q}
                    onChange=${(e) => set("q")(e.target.value)} />
          <div style=${{ display: "flex", gap: 10, alignItems: "center" }}>
            <${Button} size="sm" variant="outline" style=${{ flex: narrow ? 1 : "none" }}
              onClick=${() => setFiltersOpen((o) => !o)}>
              ${filtersOpen ? "Hide filters" : `Filters${activeFilters ? ` (${activeFilters})` : ""}`}
            <//>
            ${!clean && html`<${Button} size="sm" variant="ghost"
              onClick=${() => setF(BLANK_FILTERS)}>Clear<//>`}
          </div>
        </div>
        <div style=${{ display: filtersOpen ? "grid" : "none",
                       gridTemplateColumns: "repeat(auto-fit, minmax(148px, 1fr))",
                       gap: "12px 14px", padding: "16px 20px" }}>
          ${/* No search field here — it is above the fold now, and two inputs
               bound to the same state is a bug waiting to be reported. */ ""}
          ${field("f-state", "state", html`<${Select} id="f-state" size="sm" value=${f.state}
             onChange=${(e) => set("state")(e.target.value)}>
               <option value="">any</option>${options(states)}<//>`)}
          ${field("f-phase", "phase", html`<${Select} id="f-phase" size="sm" value=${f.phase}
             onChange=${(e) => set("phase")(e.target.value)}>
               <option value="">any</option>${options(data.phases)}<//>`)}
          ${field("f-conf", "confidence", html`<${Select} id="f-conf" size="sm" value=${f.conf}
             onChange=${(e) => set("conf")(e.target.value)}>
               <option value="">any</option>
               <option value="1">1 or better</option>
               <option value="2">2 or better</option>
               <option value="3">3 only</option><//>`)}
          ${field("f-risk", "obstacle", html`<${Select} id="f-risk" size="sm" value=${f.risk}
             onChange=${(e) => set("risk")(e.target.value)}>
               <option value="">any</option>${options(data.riskCategories)}<//>`)}
          ${field("f-sev", "severity", html`<${Select} id="f-sev" size="sm" value=${f.severity}
             onChange=${(e) => set("severity")(e.target.value)}>
               <option value="">any</option>${options(data.riskSeverities)}<//>`)}
        </div>
        <div style=${{ display: filtersOpen ? "flex" : "none", flexWrap: "wrap",
                       alignItems: "center", gap: 16, padding: "0 20px 18px" }}>
          <${Switch} size="sm" label="Quoted only" checked=${f.quoted} onCheckedChange=${(v) => set("quoted")(!!v)} />
          <${Switch} size="sm"
            label=${wide ? "All fields" : `Show ${cols.hidden} sparser field${cols.hidden === 1 ? "" : "s"}`}
            checked=${wide} onCheckedChange=${(v) => setWide(!!v)} />
          <span style=${{ flex: 1 }} />
          <${Button} size="sm" variant="ghost" disabled=${clean}
                     onClick=${() => setF(BLANK_FILTERS)}>Clear filters<//>
        </div>
      <//>

      ${/* Both the coverage strip and the seven-swatch provenance key now fold.
           They were permanent furniture above the table: six percentages and
           seven labelled swatches to read before the first row, every visit,
           forever. The key is still one click away and the strip is still
           honest about its weak end — see gaps.py — but neither is the answer
           anyone opened this view for. */ ""}
      <div style=${{ display: "flex", flexWrap: "wrap", alignItems: "baseline",
                     justifyContent: "space-between", gap: "10px 18px" }}>
        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
          ${rows.length} of ${data.projects.length} projects · sorted by ${sort.key} ${sort.dir}
        </span>
        <div style=${{ display: "flex", gap: 16 }}>
          <button type="button" class="dc-intro-toggle" aria-expanded=${showKey}
                  onClick=${() => setShowKey(!showKey)}>what do the underlines mean?</button>
          <button type="button" class="dc-intro-toggle" aria-expanded=${showCoverage}
                  onClick=${() => setShowCoverage(!showCoverage)}>coverage</button>
        </div>
      </div>

      ${showKey && html`
        <div class="dc-legend" style=${{ display: "flex", flexWrap: "wrap", gap: "6px 16px",
                                          fontSize: 12, color: "var(--muted-foreground)" }}>
          ${["reported", "derived", "unconfirmed", "inferred", "defaulted", "missing", "na"].map((t) => html`
            <span key=${t} style=${{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              ${/* cursor:default — the swatch is a key, not something to hover for a quote */ ""}
              <span class=${`dc-v dc-v--${t}`} style=${{ fontSize: 12, cursor: "default" }}>
                ${t === "missing" ? "—" : "abc"}</span>
              ${TIER[t][0]}
            </span>`)}
        </div>`}

      ${showCoverage && html`<${CoverageStrip} data=${data} />`}

      ${narrow
        ? html`
          <div style=${{ display: "grid", gap: 9 }}>
            ${rows.map((p, i) => html`
              <div key=${p.id} class=${i < 12 ? "dc-enter" : undefined} style=${i < 12 ? { "--i": i } : undefined}>
                <${ProjectCard} p=${p} data=${data} open=${openId === p.id}
                                onOpen=${onOpen} onQuote=${showQuote} />
              </div>`)}
            ${rows.length === 0 && html`
              <${EmptyState} variant="dashed" title="No project matches these filters"
                description=${`Widen the confidence floor or clear the obstacle filter. ${data.projects.length} projects are loaded.`} />`}
          </div>`
        : html`
      <${Card}>
        ${/* No inner scroll region. The mockup capped the table at 62vh, which
             was invisible against its 13-row sample and actively misleading
             against 124: the page scrolled while the table sat still, so it read
             as though only twenty projects existed. The table is now as long as
             the data and the page scrolls. `overflow: hidden` on this wrapper
             went with it — it makes an ancestor scroll container, which changes
             how everything inside positions. See app.css for why the header row
             cannot also be sticky. */ ""}
        <div style=${{ minWidth: 0 }}>
          <${Table} density="compact">
            <${TableHeader}><${TableRow}>
              ${columns.map((key, i) => html`
                <${TableHead} key=${key}
                  align=${RIGHT.has(key) ? "right" : "left"}
                  sortable=${key !== "id"}
                  sortDirection=${sort.key === key ? sort.dir : null}
                  onSort=${() => setSort((s) => ({ key, dir: s.key === key && s.dir === "desc" ? "asc" : "desc" }))}
                  style=${i < 2 ? { position: "sticky", left: i === 0 ? 0 : 58, zIndex: 4,
                                    background: "var(--surface)" } : undefined}>${key}<//>`)}
              <${TableHead} align="right">filled<//>
              <${TableHead}>tracks<//>
            <//><//>
            <${TableBody}>
              ${rows.map((p, i) => html`
                <tr key=${p.id} class=${`dc-row${i < 12 ? " dc-enter" : ""}${openId === p.id ? " dc-row--open" : ""}`}
                    style=${i < 12 ? { "--i": i } : undefined}
                    role="button" tabindex="0"
                    aria-label=${`${p.company} ${p.name}, ${place(p)}, confidence ${p.confidence}`}
                    onClick=${() => onOpen(p.id)}
                    onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(p.id); } }}>
                  <td class="dc-sticky" style=${{ left: 0, whiteSpace: "nowrap" }}>
                    <span class="dc-v dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}
                          title=${p.dedup_key}>#${p.id}</span>
                  </td>
                  ${columns.slice(1).map((key, i) => html`
                    <td key=${key}
                        class=${`dc-cell${["name", "company", "blocker"].includes(key) ? " dc-cell--wide" : ""}${i === 0 ? " dc-sticky" : ""}`}
                        style=${i === 0 ? { left: 58 } : undefined}>
                      <${Value} project=${p} field=${key} onQuote=${showQuote}
                        extra=${{
                          ...(RIGHT.has(key) ? { fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums" } : {}),
                          ...(key === "name" || key === "company" ? { fontWeight: 600 } : {}),
                        }} />
                    </td>`)}
                  <td class="dc-num" title=${`${p.filled} of the 12 tracked fields are populated`}
                      style=${{ fontSize: 12, whiteSpace: "nowrap",
                                color: p.filled >= 9 ? "var(--success)"
                                     : p.filled >= 6 ? "var(--muted-foreground)" : "var(--warning)" }}>
                    ${p.filled}/12</td>
                  <td><${TrackStrip} standing=${p.standing} tracks=${data.tracks} /></td>
                </tr>`)}
            <//>
          <//>
          ${rows.length === 0 && html`
            <div style=${{ padding: "8px 20px 20px" }}>
              <${EmptyState} variant="dashed" title="No project matches these filters"
                description=${`Widen the confidence floor or clear the obstacle filter. ${data.projects.length} projects are loaded.`} />
            </div>`}
        </div>
      <//>`}
      <${QuotePopover} quote=${quote} />
    </div>`;
}

/* ---- Detail drawer ------------------------------------------------------- */

function Drawer({ data, project, onClose }) {
  const [tab, setTab] = useState("stats");
  /* One project's claim table, fetched when the drawer opens. Keyed by id and
     kept for the session, so re-opening a row costs nothing. */
  const [claims, setClaims] = useState({});
  const [quote, showQuote] = useQuote();
  const closeRef = useRef(null);

  useEffect(() => { setTab("stats"); }, [project?.id]);
  useEffect(() => {
    const id = project?.id;
    if (id == null || claims[id]) return;
    let cancelled = false;
    api(`/api/claims?project=${id}`)
      .then((payload) => {
        if (!cancelled) setClaims((c) => ({ ...c, [id]: payload.claims_by_field || {} }));
      })
      /* A failed fetch leaves the claim tables absent, which is what they looked
         like before this route existed. Every value and its tier is already on
         screen from the list payload. */
      .catch(() => {});
    return () => { cancelled = true; };
  }, [project?.id, claims]);
  useEffect(() => {
    const page = document.getElementById("dc-page");
    if (page) {
      // aria-modal is a claim; inert is what makes it true.
      if (project) { page.setAttribute("inert", ""); page.setAttribute("aria-hidden", "true"); }
      else { page.removeAttribute("inert"); page.removeAttribute("aria-hidden"); }
    }
    if (project) closeRef.current?.focus();
  }, [project]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && project) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [project, onClose]);

  if (!project) return null;
  const p = project;
  const open = p.risks.filter((r) => r.status === "open");
  const populated = TRACKED.filter((k) => p[k] != null).length;
  // Campus tranches only — the utility's plant is counted separately, and the
  // count on the tab is the campus's. A site whose only tranches are its serving
  // power still gets the tab, because that is a fact worth reading.
  const blocks = p.blocks || [];
  const serving = p.serving || [];
  // Only offered when there are blocks. An empty tab on 88% of the database would
  // read as "this campus has one tranche", which is the opposite of what a missing
  // backfill means.
  const tabs = [
    ["stats", "Stats", ""],
    ...(blocks.length || serving.length ? [["blocks", "Blocks", ` ${blocks.length}`]] : []),
    ["risks", "Risks", ` ${open.length}`],
    ["sources", "Sources", ` ${p.sources.length}`],
  ];

  return html`
    <div style=${{ position: "fixed", inset: 0, zIndex: 60, display: "flex" }}>
      <div class="dc-scrim" onClick=${onClose} />
      <aside class="dc-slide dc-drawer" role="dialog" aria-modal="true"
             aria-label=${`${p.company} — ${p.name}`}>
        <div class="dc-drawer-head" style=${{ display: "flex", alignItems: "flex-start", gap: 16, padding: "20px 24px 16px",
                       borderBottom: "1px solid var(--border)", background: "var(--surface)" }}>
          <div style=${{ flex: 1, minWidth: 0, display: "grid", gap: 7 }}>
            <div style=${{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>#${p.id}</span>
              <h2 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                            letterSpacing: "-0.02em", lineHeight: 1.1 }}>${p.company} — ${p.name}</h2>
            </div>
            <div style=${{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style=${chip(p.confidence >= 3 ? "--success" : p.confidence === 2 ? "--chart-1" : "--warning")}>
                confidence ${p.confidence}</span>
              <span style=${chip("--foreground", true)}>${p.phase}</span>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
                ${place(p)}${p.iso ? " · " + p.iso : ""}</span>
            </div>
          </div>
          <button ref=${closeRef} class="dc-xbtn" onClick=${onClose} aria-label="Close">✕</button>
        </div>

        <div class="dc-drawer-tabs" style=${{ padding: "8px 24px 0", background: "var(--surface)", borderBottom: "1px solid var(--border)" }}>
          <${Tabs} variant="underline" value=${tab} onValueChange=${setTab}>
            <${TabsList}>
              ${tabs.map(([key, label, count]) => html`
                <${TabsTrigger} key=${key} value=${key}>${label}${count}<//>`)}
            <//>
          <//>
        </div>

        <div class="dc-drawer-body" style=${{ flex: 1, overflowY: "auto", padding: "20px 24px 56px" }}>
          ${tab === "stats" && html`<${StatsTab} data=${data} p=${p} populated=${populated}
                                                 open=${open} onQuote=${showQuote} onTab=${setTab}
                                                 claims=${claims[p.id]}
                                                 allowAi=${data.allow_ai} />`}
          ${tab === "blocks" && html`<${BlocksTab} p=${p} />`}
          ${tab === "risks" && html`<${RisksTab} data=${data} p=${p} />`}
          ${tab === "sources" && html`<${SourcesTab} data=${data} p=${p} />`}
        </div>
      </aside>
      <${QuotePopover} quote=${quote} />
    </div>`;
}

/* ---- Markdown, rendered to React elements ---------------------------------
 *
 * A deliberately small subset: paragraphs, bullets, bold, italic, inline code.
 * That is what the briefing prompt asks for and nothing else is worth carrying.
 *
 * **Never innerHTML.** This string is written by a model out of articles fetched
 * from the open web, which makes it the least trustworthy text on the page —
 * anything that turns it into markup is an injection path running from someone
 * else's web page, through the extraction pipeline, into a console that runs
 * commands. Emitting elements means a `<script>` in the text is a `<script>` on
 * the screen, as characters.
 *
 * Links are flattened to their text for the same reason. A clickable destination
 * chosen by a model reading an untrusted page is a phishing surface, the prompt
 * does not ask for links, and there is nothing here worth linking to anyway. */
const MD_INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_)/g;

function mdInline(text, key) {
  const parts = [];
  let at = 0;
  let match;
  MD_INLINE.lastIndex = 0;
  while ((match = MD_INLINE.exec(text)) !== null) {
    if (match.index > at) parts.push(text.slice(at, match.index));
    const token = match[0];
    const k = `${key}-${match.index}`;
    if (token.startsWith("**")) parts.push(html`<strong key=${k}>${token.slice(2, -2)}</strong>`);
    else if (token.startsWith("`")) parts.push(html`<code key=${k} class="dc-md-code">${token.slice(1, -1)}</code>`);
    else parts.push(html`<em key=${k}>${token.slice(1, -1)}</em>`);
    at = match.index + token.length;
  }
  if (at < text.length) parts.push(text.slice(at));
  return parts;
}

function renderMarkdown(source) {
  const text = source
    // Links → their text. The target allows one level of nesting so that a URL
    // like `(javascript:alert(1))` is consumed whole rather than leaving its
    // closing bracket behind as debris.
    .replace(/\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)/g, "$1")
    .replace(/^\s*#{1,6}\s*/gm, "")            // headings → ordinary lines
    .replace(/^\s*```.*$/gm, "");              // fences → dropped, never asked for
  const blocks = [];
  let list = null;

  const flush = () => { if (list) { blocks.push(list); list = null; } };

  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) { flush(); continue; }
    const bullet = line.match(/^[-*+]\s+(.*)$/);
    if (bullet) {
      if (!list) list = { type: "ul", items: [] };
      list.items.push(bullet[1]);
      continue;
    }
    flush();
    const last = blocks[blocks.length - 1];
    // Consecutive non-blank lines are one paragraph, the way markdown means it —
    // otherwise a model that hard-wraps produces a line break every nine words.
    if (last && last.type === "p") last.text += " " + line;
    else blocks.push({ type: "p", text: line });
  }
  flush();

  return blocks.map((block, i) =>
    block.type === "ul"
      ? html`<ul key=${i}>${block.items.map((item, j) => html`<li key=${j}>${mdInline(item, `${i}-${j}`)}</li>`)}</ul>`
      : html`<p key=${i}>${mdInline(block.text, String(i))}</p>`);
}

/* Hide a bold marker whose partner has not arrived yet.
 *
 * Without this, streaming shows `**Phoenix` as literal asterisks for a second and
 * then reflows into bold. Dropping the lone marker keeps the words and lets them
 * simply become bold when the closer lands. */
function tidyPartialMarkdown(text) {
  return (text.match(/\*\*/g) || []).length % 2
    ? text.replace(/\*\*(?![\s\S]*\*\*)/, "")
    : text;
}

const REDUCED_MOTION = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Reveal received text at a steady rate rather than in whatever bursts the
 * network delivered.
 *
 * SSE frames arrive clumped — several sentences, then nothing, then several more.
 * Rendering each frame as it lands looks like stuttering, not like writing.
 *
 * Three things make it smooth, and the first version had none of them.
 *
 * **The loop is created once.** Putting `text` in the effect's dependencies tore
 * the animation frame down and rebuilt it on every SSE frame, so the cursor
 * restarted several times a second. That alone was most of the stutter. The text
 * now lives in a ref the loop reads.
 *
 * **The pace is set by elapsed time, not by frames.** A per-frame character step
 * runs at whatever rate the display happens to refresh and stalls whenever the
 * main thread is busy. Characters per *second* looks the same everywhere.
 *
 * **Whole words appear at once.** Revealing mid-word makes every line re-wrap as
 * the word grows, which reads as twitching rather than typing. Rounding the
 * cursor down to the last word boundary also cuts re-renders from ~60 a second to
 * ~12, since the visible string only changes when a word completes. */
const REVEAL_CPS = 62;        // comfortable reading pace when the model is ahead
const REVEAL_CATCHUP_S = 0.7; // never let the backlog sit longer than this

function useTypewriter(text, active) {
  const [shown, setShown] = useState(0);
  const target = useRef(text);
  const cursor = useRef(0);
  target.current = text;

  useEffect(() => {
    if (!active) return;
    if (REDUCED_MOTION()) { setShown(target.current.length); return; }

    let frame = 0;
    let last = performance.now();
    const tick = (now) => {
      const elapsed = Math.min(now - last, 100) / 1000; // a backgrounded tab must not lurch
      last = now;
      const full = target.current;
      const behind = full.length - cursor.current;
      if (behind > 0) {
        // Steady, unless the model has got far enough ahead that a fixed pace
        // would still be typing long after the stream closed.
        const speed = Math.max(REVEAL_CPS, behind / REVEAL_CATCHUP_S);
        cursor.current = Math.min(full.length, cursor.current + speed * elapsed);
        const edge = wordBoundary(full, Math.floor(cursor.current));
        setShown((current) => (edge > current ? edge : current));
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [active]);

  // Snap to the end when the stream closes, so a finished briefing is never a
  // few characters short because the last frame did not run.
  useEffect(() => {
    if (!active) { cursor.current = text.length; setShown(text.length); }
  }, [active, text.length]);

  return active ? Math.min(shown, text.length) : text.length;
}

/* The end of the last complete word at or before `index`.
 *
 * Everything up to the cursor is shown except a partly-revealed final word, which
 * would otherwise grow letter by letter and re-wrap the line under it. Once the
 * whole text has arrived the cursor may sit at the very end, and that is a
 * boundary too. */
function wordBoundary(text, index) {
  if (index >= text.length) return text.length;
  const space = text.lastIndexOf(" ", index);
  const newline = text.lastIndexOf("\n", index);
  return Math.max(space, newline) + 1;
}

/* A reading of the row, from a model — the console's version of an AI overview.
 *
 * It sits at the top of the drawer, generates when the row is opened and streams
 * as it is written. Both were deliberate reversals: it used to be a card at the
 * bottom behind a button, on the reasoning that a drawer which spends money when
 * you click a row is a drawer nobody dares click. In use almost nobody clicked
 * the button, so it cost nothing and did nothing. Cost control moved to where it
 * belongs — the briefing is cached by content, so a row is paid for once and
 * reopening is free.
 *
 * **The honesty signal got quieter, not weaker.** The previous version fronted
 * two lines of disclaimer above three of content, which is the shape of something
 * nobody reads. Now: a labelled heading that says a model wrote it, the tier hue
 * the legend already teaches, and the caveat as a footnote under the text where a
 * reader lands after the claim rather than before it. Still impossible to mistake
 * for a cited value; no longer shouting. */
function InsightPanel({ project, allowAi }) {
  const [state, setState] = useState({ status: "idle", text: "" });
  const [expanded, setExpanded] = useState(false);
  const abort = useRef(null);

  const ask = useCallback(() => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setState({ status: "writing", text: "" });
    setExpanded(false);

    apiStream("/api/overview/stream", { project_id: project.id, confirm: "overview" }, (event) => {
      if (event.type === "text") {
        setState((s) => ({ ...s, status: "writing", text: s.text + event.text }));
      } else if (event.type === "end") {
        setState({ status: "done", text: event.text, model: event.model, cached: event.cached });
      } else if (event.type === "error") {
        setState((s) => ({ ...s, status: "failed", error: event.error }));
      }
    }, controller.signal).catch((e) => {
      if (e.name === "AbortError") return;
      setState({ status: "failed", text: "", error: e.message });
    });
  }, [project.id]);

  // A new project means a new briefing, and the request for the old one is
  // abandoned — without that, opening a second row shows the first row's text
  // arriving under the second row's name.
  useEffect(() => {
    if (!allowAi) { setState({ status: "unavailable", text: "" }); return; }
    ask();
    return () => abort.current?.abort();
  }, [project.id, allowAi, ask]);

  const streaming = state.status === "writing";
  const shown = useTypewriter(state.text, streaming);
  const visible = state.text.slice(0, shown);
  const body = visible.trim()
    ? renderMarkdown(streaming ? tidyPartialMarkdown(visible) : visible)
    : null;

  // Only offer "Show more" when there is more. A short briefing that already
  // fits, under a control implying it is cut off, is worse than no control —
  // and most of them do fit, which is the point of asking for 60 to 110 words.
  const prose = useRef(null);
  const [clipped, setClipped] = useState(false);
  useEffect(() => {
    const el = prose.current;
    setClipped(!!el && el.scrollHeight > el.clientHeight + 2);
  }, [visible, expanded]);

  return html`
    <section class="dc-ai" aria-label="Model-written overview">
      <div class="dc-ai-head">
        <span class="dc-ai-spark" aria-hidden="true">✦</span>
        <span class="dc-ai-label">AI overview</span>
        ${streaming && html`<span class="dc-ai-dots" aria-live="polite" aria-label="writing"><i /><i /><i /></span>`}
        ${state.status === "done" && html`
          <button type="button" class="dc-ai-redo" onClick=${ask}
                  title=${state.model + (state.cached ? " · from cache" : "")}>Rewrite</button>`}
      </div>

      ${state.status === "unavailable" && html`
        <p class="dc-ai-quiet">Unavailable: this console was started with --no-ai.</p>`}

      ${state.status === "failed" && html`
        <p class="dc-ai-quiet">
          ${state.error} <button type="button" class="dc-ai-redo" onClick=${ask}>Try again</button>
        </p>`}

      ${streaming && !body && html`
        <div class="dc-ai-wait" aria-hidden="true">
          ${[96, 88, 62].map((w) => html`<span key=${w} style=${{ width: w + "%" }} />`)}
        </div>`}

      ${body && html`
        <div ref=${prose}
             class=${"dc-ai-body" + (streaming ? " is-writing" : "") + (expanded ? " is-open" : "")}>
          ${body}
        </div>`}

      ${body && !streaming && html`
        <div class="dc-ai-foot">
          ${(clipped || expanded) && html`
            <button type="button" class="dc-ai-more" onClick=${() => setExpanded(!expanded)}>
              ${expanded ? "Show less" : "Show more"}
            </button>`}
          <span>Written by a model from the values below — not stored, not evidence.</span>
        </div>`}
    </section>`;
}

/* `tracker infer`, as a button on the row it is about.
 *
 * The PRD asks two questions no article answers — 可能遇到的困难 and what signal
 * would show the project is still advancing — and the CLI has answered them since
 * the beginning. Reaching them meant leaving the page, finding the id and typing a
 * command, which is the reason almost nobody ran it.
 *
 * **A button, not an automatic panel, and that is a deliberate difference from the
 * overview above it.** The briefing generates on open because it is cached by
 * content: a row is paid for once and reopening is free. An inference is not
 * cached and never stored — its whole value is that somebody asked for it against
 * the row as it stands right now — so running it on open would spend a call every
 * time a drawer was opened. One click, one call, one answer.
 *
 * Everything rendered here is the model's judgement and none of it is evidence.
 * The panel says so once, plainly, at the bottom, and uses the same tint the
 * overview uses so a reader already knows what that colour means. */
function InferPanel({ project, allowAi }) {
  const [state, setState] = useState({ status: "idle" });

  // A new project means the previous project's analysis must go. Without this,
  // opening a second row shows the first row's obstacles under the second name.
  useEffect(() => { setState({ status: "idle" }); }, [project.id]);

  // `api` throws on a refusal rather than returning `{error}` — a panel that
  // checked the return value for an error sat on its loading state forever the
  // first time the console was read-only.
  const run = useCallback(async () => {
    setState({ status: "running" });
    try {
      setState({ status: "done", data: await api("/api/infer", {
        method: "POST",
        body: { project_id: project.id, confirm: "infer" },
      }) });
    } catch (e) {
      setState({ status: "failed", error: e.message });
    }
  }, [project.id]);

  const d = state.data;
  const empty = d && !d.obstacles?.length && !d.signals?.length;

  return html`
    <section class="dc-infer" aria-label="Model-inferred analysis">
      <div class="dc-ai-head">
        <span class="dc-ai-spark" aria-hidden="true">◈</span>
        <span class="dc-ai-label">Inferred analysis</span>
        <span class="dc-infer-sub">what could go wrong, and what would show it is moving</span>
        ${allowAi && state.status !== "running" && html`
          <button type="button" class="dc-ai-redo" onClick=${run}>
            ${state.status === "done" ? "Run again" : "Run analysis"}
          </button>`}
        ${state.status === "running" && html`
          <span class="dc-ai-dots" aria-live="polite" aria-label="thinking"><i /><i /><i /></span>`}
      </div>

      ${!allowAi && html`
        <p class="dc-ai-quiet">Unavailable: this console was started with --no-ai.</p>`}

      ${allowAi && state.status === "idle" && html`
        <p class="dc-ai-quiet">
          Not run yet — it costs one model call, and nothing here is cached or stored.
        </p>`}

      ${state.status === "running" && html`
        <div class="dc-ai-wait" aria-hidden="true">
          ${[92, 74, 84].map((w) => html`<span key=${w} style=${{ width: w + "%" }} />`)}
        </div>`}

      ${state.status === "failed" && html`
        <p class="dc-ai-quiet">
          ${state.error} <button type="button" class="dc-ai-redo" onClick=${run}>Try again</button>
        </p>`}

      ${d?.rejected?.length > 0 && html`
        <p class="dc-infer-refused">
          Refused to accept ${d.rejected.join(", ")} — a model may not assert a fact.
        </p>`}

      ${empty && html`<p class="dc-ai-quiet">No conclusion the recorded facts support.</p>`}

      ${d?.obstacles?.length > 0 && html`
        <div class="dc-infer-group">
          <h4 class="dc-infer-h">What could obstruct this</h4>
          ${d.obstacles.map((o, i) => html`
            <div class="dc-infer-row" key=${"o" + i}>
              <div class="dc-infer-head">
                <${Meter} value=${o.confidence} />
                <span style=${chip("--chart-5")}>${o.category}</span>
                <span style=${chip(SEV_TONE[o.severity] || "--muted-foreground")}>${o.severity}</span>
              </div>
              <p class="dc-infer-why">${o.reasoning}</p>
            </div>`)}
        </div>`}

      ${d?.signals?.length > 0 && html`
        <div class="dc-infer-group">
          <h4 class="dc-infer-h">What would show it is still moving</h4>
          ${d.signals.map((s, i) => html`
            <div class="dc-infer-row" key=${"s" + i}>
              <div class="dc-infer-head">
                <${Meter} value=${s.confidence} />
                <span class="dc-infer-signal">${s.signal}</span>
              </div>
              <p class="dc-infer-why">${s.reasoning}</p>
            </div>`)}
        </div>`}

      ${d && !empty && html`
        <p class="dc-infer-foot">
          Inferred by ${d.model} from this row's recorded obstacles, milestones and gaps.
          Not stored, not evidence — a judgement drawn from the facts, shown beside them.
        </p>`}
    </section>`;
}

/* Confidence as five cells. A bare 0.65 beside three other decimals is something
 * a reader decodes every time; the figure stays, and the shape carries it. */
function Meter({ value }) {
  const filled = Math.max(1, Math.min(5, Math.round((value || 0) * 5)));
  const tone = value >= 0.7 ? "--success" : value >= 0.5 ? "--warning" : "--danger";
  return html`
    <span class="dc-meter" title=${`model confidence ${(value || 0).toFixed(2)}`}>
      ${[0, 1, 2, 3, 4].map((i) => html`
        <i key=${i} style=${{ background: i < filled ? `var(${tone})` : "var(--border)" }} />`)}
      <b>${(value || 0).toFixed(2)}</b>
    </span>`;
}

//: Severity to a colour token, shared by the inferred obstacles and the recorded ones.
const SEV_TONE = { blocking: "--danger", material: "--warning", watch: "--muted-foreground" };

/* What this campus's numbers mean next to something.
 *
 * Replaced a 3D schematic that extruded `mw_planned / 150` into halls, clamped to
 * eight, under a caption asserting "8 halls @ ~150 mw · 0 built" — a wrong number
 * stated twice, from a row whose capacity was 14,462 MW of gas plants and
 * double-counted phases. Its whole input set is rendered better, from real data, by
 * the blocks and tracks panels two cards away.
 *
 * Every figure here is derived in the page from two payload scalars AND LABELLED
 * as derived. That is within the rule `dataset.py` states — the browser must not
 * re-implement a JUDGEMENT — and `h200FromMw` is the existing precedent.
 *
 * Deliberately no "×N households" or "×N times the parish's usage", which is the
 * one LouisianAI idea this codebase should refuse: we hold no consumption
 * baseline, and inventing one puts an uncited number on a page whose whole premise
 * is that every fact traces to a source and a sentence. */
function InContext({ p, data }) {
  const mw = p.mw_planned;
  if (mw == null) return null;

  const totals = data.totals || {};
  const stateTotal = (data.projects || [])
    .filter((x) => x.state === p.state && x.mw_planned)
    .reduce((sum, x) => sum + x.mw_planned, 0);
  const perMw = p.investment_usd && mw ? p.investment_usd / mw : null;
  const acct = p.accounting;

  const rows = [
    stateTotal > 0 && {
      label: `share of tracked ${p.state}`,
      value: `${Math.round((mw / stateTotal) * 100)}%`,
      note: `${fmtMw(mw)} of ${fmtMw(stateTotal)} MW — a floor: only projects citing a figure count`,
    },
    totals.mw_planned > 0 && {
      label: "share of everything tracked",
      value: `${((mw / totals.mw_planned) * 100).toFixed(1)}%`,
      note: `of ${fmtMw(totals.mw_planned)} MW across the database`,
    },
    perMw && {
      label: "capex per MW",
      value: `$${(perMw / 1e6).toFixed(1)}M`,
      note:
        perMw < 3e6
          ? "below the $8–15M/MW a build of this size costs — likely a superseded figure"
          : perMw > 3e7
            ? "above the $8–15M/MW band — likely a programme total, not this site"
            : "inside the $8–15M/MW a campus of this size costs to build",
    },
    p.h200_equivalent && {
      label: "compute",
      value: fmt("h200_equivalent", p.h200_equivalent),
      note: `H200-equivalent at ${data.kwPerH200} kW each — derived from megawatts, not cited`,
    },
    acct?.total_is_floor && {
      label: "itemised",
      value: `${Math.round((acct.counted_mw / (acct.total || 1)) * 100)}%`,
      note: "of the campus total is broken into tranches a quote confirms",
    },
  ].filter(Boolean);

  if (!rows.length) return null;
  return html`
    <${Card}>
      <${CardHeader}>
        <${CardTitle}>In context<//>
        <${CardDescription}>Each figure computed from two stored values, and labelled as
          derived rather than cited.<//>
      <//>
      <div style=${{ display: "grid", gap: 0 }}>
        ${rows.map((r, i) => html`
          <div key=${i} style=${{ display: "grid", gridTemplateColumns: "minmax(0,1fr) auto", gap: 12,
               alignItems: "baseline", padding: "10px 20px", borderTop: "1px solid var(--border)" }}>
            <div style=${{ minWidth: 0 }}>
              <div style=${{ fontSize: 13 }}>${r.label}</div>
              <p style=${{ margin: "2px 0 0", fontSize: 12, lineHeight: "17px",
                           color: "var(--muted-foreground)" }}>${r.note}</p>
            </div>
            <span class="dc-num" style=${{ fontSize: 16, fontWeight: 500 }}>${r.value}</span>
          </div>`)}
      </div>
    <//>`;
}

/* The milestones a project actually reached, one row each.
 *
 * This replaced a flat list of every stored event. Hyperion has 72 of them —
 * eleven `announced`, eight `groundbreaking`, eight `permit_approved` — because
 * each article restates the same moment with its own date, and
 * `uq_event_project_type_date` only dedups exact matches. 72 rows is a log, not a
 * timeline, and a reader cannot see the shape of the project through it.
 *
 * The grouping is done server-side in `export._timeline_json` (the browser never
 * re-implements a judgement): earliest confirmed event per milestone, the rest
 * counted as restatements, and where confirmed dates disagree by more than a year
 * BOTH ends are shown and neither is chosen. Nothing is deleted — every event is
 * still in `events[]` behind the toggle — which is what makes this curation
 * rather than suppression.
 *
 * A CSS grid rather than a chart: dates here range from day precision to
 * year-only, and a horizontal axis would draw false precision on most rows. */
function Timeline({ p }) {
  const [all, setAll] = useState(false);
  const rows = p.standing?.timeline || [];
  const tracks = Object.fromEntries((p.standing?.tracks || []).map((t) => [t.track, t]));

  if (!rows.length && !p.events.length) {
    return html`
      <${Card}>
        <${CardHeader}><${CardTitle}>Milestones<//><//>
        <div style=${{ padding: "4px 20px 20px" }}>
          <${EmptyState} variant="dashed" size="sm" title="No milestones recorded"
            description="Nothing read so far states one. Silence is not evidence — a milestone appears when a source reports it." />
        </div>
      <//>`;
  }

  let lastTrack = null;
  return html`
    <${Card}>
      <${CardHeader}>
        <${CardTitle}>${rows.length} milestone${rows.length === 1 ? "" : "s"} reached<//>
        <${CardDescription}>The earliest cited date for each, grouped by track. A milestone
          restated across several articles is one milestone; ${p.events.length} events were
          recorded in total.<//>
      <//>
      <div style=${{ display: "grid", gap: 0 }}>
        ${rows.map((r, i) => {
          const head = r.track !== lastTrack;
          lastTrack = r.track;
          const t = tracks[r.track];
          return html`
            <${Frag} key=${i}>
              ${head && html`
                <div style=${{ padding: "12px 20px 4px", borderTop: i ? "1px solid var(--border)" : "none",
                               display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
                  <strong style=${{ fontSize: 12, letterSpacing: ".04em", textTransform: "uppercase",
                                    color: "var(--muted-foreground)" }}>${r.track.replace("_", " ")}</strong>
                  ${t?.blocking_risks?.length ? html`
                    <span style=${chip(SEV_TONE[t.blocker_severity] || "--muted-foreground")}
                          title=${t.blocking_risks.map((b) => `#${b.id} ${b.category}: ${b.summary}`).join("\\n")}>
                      blocked — ${t.blocking_risks[0].category}
                      ${t.blocking_risks.length > 1 ? ` +${t.blocking_risks.length - 1}` : ""}
                    </span>` : null}
                </div>`}
              <div style=${{ display: "grid", gridTemplateColumns: "104px minmax(0,1fr)", gap: 14,
                   alignItems: "baseline", padding: "8px 20px 8px 32px" }}>
                <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)",
                      opacity: r.implied ? 0.5 : 1 }}>${r.date || "—"}</span>
                <div style=${{ minWidth: 0 }}>
                  <span style=${{ fontSize: 14, lineHeight: "20px", opacity: r.implied ? 0.6 : 1 }}>
                    ${r.milestone.replace(/_/g, " ")}
                  </span>
                  ${r.implied && html`
                    <span style=${{ ...chip("--muted-foreground"), marginLeft: 8 }}
                          title="deduced from a later milestone, not read anywhere">implied</span>`}
                  ${r.unconfirmed && html`
                    <span style=${{ ...chip("--warning"), marginLeft: 8 }}>not cited</span>`}
                  ${r.restatements > 0 && html`
                    <span style=${{ marginLeft: 8, fontSize: 11, color: "var(--muted-foreground)" }}>
                      +${r.restatements} more mention${r.restatements === 1 ? "" : "s"}</span>`}
                  ${r.conflicting_dates?.length === 2 && html`
                    <p style=${{ margin: "3px 0 0", fontSize: 12, color: "var(--warning)" }}>
                      sources date this between ${r.conflicting_dates[0]} and ${r.conflicting_dates[1]} —
                      shown as the earliest, neither chosen</p>`}
                  ${r.description && html`
                    <p style=${{ margin: "2px 0 0", fontSize: 13, lineHeight: "19px" }}>${r.description}</p>`}
                  ${r.quote && html`
                    <p style=${{ margin: "3px 0 0", fontSize: 12, lineHeight: "17px", maxWidth: "76ch",
                                 color: "var(--muted-foreground)", borderLeft: "2px solid var(--primary)",
                                 paddingLeft: 9 }}>“${r.quote}”</p>`}
                </div>
              </div>
            <//>`;
        })}
      </div>
      <div style=${{ padding: "10px 20px 16px", borderTop: "1px solid var(--border)" }}>
        <button type="button" onClick=${() => setAll(!all)}
          style=${{ background: "none", border: "none", padding: 0, cursor: "pointer", font: "inherit",
                    fontSize: 12, color: "var(--muted-foreground)", textDecoration: "underline",
                    textUnderlineOffset: 3 }}>
          ${all ? "hide" : `show every recorded event (${p.events.length})`}
        </button>
        ${all && html`
          <div style=${{ display: "grid", gap: 0, marginTop: 8 }}>
            ${p.events.map((e, i) => html`
              <div key=${i} style=${{ display: "grid", gridTemplateColumns: "96px 168px minmax(0,1fr)",
                   gap: 14, alignItems: "baseline", padding: "6px 0", borderTop: "1px solid var(--border)" }}>
                <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>${e.event_date}</span>
                <span style=${chip(e.event_type === "delayed" ? "--danger"
                  : e.event_type === "expanded" ? "--chart-5" : "--muted-foreground")}>${e.event_type}</span>
                <span style=${{ fontSize: 13, lineHeight: "19px" }}
                      title=${e.quote ? `“${e.quote}”` : undefined}>
                  ${e.description}
                  ${e.unconfirmed && html`
                    <span style=${{ ...chip("--warning"), marginLeft: 8 }}>not cited</span>`}
                </span>
              </div>`)}
          </div>`}
      </div>
    <//>`;
}

/* Every claim any citation made for one field, beside the value that won.
 *
 * The question a reader actually asks when a figure looks wrong is not "what is
 * the evidence for this" — `prov` answers that above — but "what else did anybody
 * say, and why did this one win?". Hyperion stored $10B for a campus whose current
 * figure is $50B+, and both are in the database; the page simply showed one.
 *
 * Collapsed by default. It is the answer to a question most rows never raise, and
 * a table under all twelve fields would bury the values it is meant to qualify.
 *
 * Everything here is decided server-side in `export._claims_json`: the ORDER is the
 * merge engine's own, the WINNER is resolved through `resolve_field`, and
 * `decided_by` is only set when a real rival exists. The browser sorts nothing and
 * decides nothing — three claims at different scopes are not a disagreement, and
 * saying "credibility won" there would be a plausible sentence and the wrong one. */
function ClaimTable({ claims, field }) {
  const [open, setOpen] = useState(false);
  /* Fetched per drawer rather than shipped with the list — it was 48% of a 19 MB
     payload for a table that renders one project at a time. Absent means still in
     flight, and rendering nothing is right: the row above already carries the
     value and its tier. */
  const env = (claims || {})[field];
  if (!env || env.claims.length < 2) return null;

  const rivals = env.claims.length - 1;
  return html`
    <div style=${{ marginTop: 2 }}>
      <button type="button" onClick=${() => setOpen(!open)}
        style=${{ background: "none", border: "none", padding: 0, cursor: "pointer",
                  font: "inherit", fontSize: 12, color: "var(--muted-foreground)",
                  textDecoration: "underline", textUnderlineOffset: 3 }}>
        ${open ? "hide" : `${rivals} other claim${rivals === 1 ? "" : "s"}`}
      </button>
      ${env.stored_unsupported && html`
        <span style=${{ ...chip("--danger"), marginLeft: 8 }}>no claim supports the stored value</span>`}
      ${open && html`
        <div style=${{ marginTop: 8, display: "grid", gap: 6 }}>
          ${env.why
            ? html`<p style=${{ margin: 0, fontSize: 12, color: "var(--muted-foreground)" }}>
                <strong>why this one:</strong> ${env.why}</p>`
            : html`<p style=${{ margin: 0, fontSize: 12, color: "var(--muted-foreground)" }}>
                ${env.claims.length} claims, none in conflict — different scopes rather than a
                disagreement.</p>`}
          ${env.claims.map((c, i) => html`
            <div key=${i} style=${{ display: "grid", gridTemplateColumns: "minmax(0,1fr) auto",
                 gap: 10, alignItems: "baseline", paddingLeft: 9,
                 borderLeft: `2px solid var(${c.is_winner ? "--primary" : "--border"})` }}>
              <div style=${{ minWidth: 0 }}>
                <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13,
                                fontWeight: c.is_winner ? 600 : 400 }}>${fmt(field, c.value)}</span>
                ${c.is_winner && html`<span style=${{ ...chip("--primary"), marginLeft: 8 }}>kept</span>`}
                ${!c.confirmed && html`
                  <span style=${{ ...chip("--warning"), marginLeft: 8 }}>
                    待确认${c.unconfirmed_reason ? ` · ${c.unconfirmed_reason}` : ""}</span>`}
                ${c.quote && html`
                  <p style=${{ margin: "3px 0 0", fontSize: 12, lineHeight: "17px",
                               color: "var(--muted-foreground)", maxWidth: "72ch" }}>“${c.quote}”</p>`}
              </div>
              <a href=${c.source_url} target="_blank" rel="noopener noreferrer"
                 style=${{ fontSize: 11, color: "var(--muted-foreground)", whiteSpace: "nowrap" }}
                 title=${c.source_url}>
                ${c.source_type} · w${c.weight}
              </a>
            </div>`)}
        </div>`}
    </div>`;
}

/* Why this obstacle and not the other twenty-six.
 *
 * `blocker` is one sentence chosen out of every open obstacle a project carries,
 * and until now the page showed the winner and never said the others had been
 * considered — so a reader could not tell whether it was the worst, the newest, or
 * simply the first row the database returned.
 *
 * The sentence comes from `upsert.blocker_rationale`, which shares `choose_blocker`
 * with the write path. The page does not re-derive it: an explanation free to name
 * a different risk than the column holds is worse than no explanation.
 *
 * When the choice was arbitrary — several obstacles ranking equally, settled on
 * the lowest row id — it says so. That is the case a reader most needs to know
 * about, and it is the one a confident-sounding sentence would hide. */
function BlockerWhy({ why, onTab }) {
  if (!why) return null;
  return html`
    <div style=${{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap",
                   fontSize: 12, lineHeight: "18px",
                   color: why.arbitrary ? "var(--warning)" : "var(--muted-foreground)" }}>
      <span>Chosen because it is ${why.why}.</span>
      ${why.considered > 1 && html`
        <button type="button" class="dc-linkish" onClick=${() => onTab && onTab("risks")}
          >see all ${why.considered}</button>`}
    </div>`;
}

function StatsTab({ data, p, populated, open, onQuote, allowAi, onTab, claims }) {
  const worst = open.slice().sort((a, b) => SEV_ORDER.indexOf(b.severity) - SEV_ORDER.indexOf(a.severity))[0];
  const stats = [
    { label: "IT capacity, planned", value: p.mw_planned == null ? "—" : p.mw_planned.toLocaleString() + " MW",
      hint: p.mw_planned == null ? "no source cited one" : TIER[tierOf(p, "mw_planned")][0] },
    { label: "IT capacity in service", value: p.mw_built == null ? "—" : p.mw_built.toLocaleString() + " MW",
      hint: p.mw_built == null ? "nothing built, or nothing read" : TIER[tierOf(p, "mw_built")][0] },
    { label: "Compute", value: fmt("h200_equivalent", p.h200_equivalent),
      hint: p.h200_equivalent == null ? "no capacity cited, so nothing to convert"
          : p.h200_equivalent === h200FromMw(p.mw_built || p.mw_planned)
          ? "H200-equivalent, derived from MW" : "H200-equivalent, as cited" },
    { label: "Announced investment", value: fmtUSD(p.investment_usd),
      hint: p.investment_usd == null ? "the hardest field to source" : TIER[tierOf(p, "investment_usd")][0] },
    { label: "Open obstacles", value: String(open.length),
      hint: worst ? "worst: " + worst.severity : "none reported" },
  ];
  const label = (key) => (data.tracks.find((t) => t.key === key) || {}).label || key;

  return html`
    <div style=${{ display: "grid", gap: 18 }}>
      <div style=${{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
        ${stats.map((s) => html`<${StatCard} key=${s.label} label=${s.label} value=${s.value} hint=${s.hint} />`)}
      </div>

      ${/* In the flow, under the numbers, scrolling with everything else. It was
            briefly pinned above the tab strip, which made it the one block in the
            drawer you could not scroll past — and put a model's reading in front
            of the figures it is a reading *of*. A card, like the rest. */ ""}
      <${InsightPanel} project=${p} allowAi=${allowAi} />

      ${/* Under the briefing, because it answers the next question: the overview
            says what this row is, and this says what could stop it. Both are a
            model's words and both carry the same tint; only this one costs a call
            per click, so only this one has a button. */ ""}
      <${InferPanel} project=${p} allowAi=${allowAi} />

      <div style=${{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(330px, 1fr))",
                     gap: 18, alignItems: "start" }}>
        <${Card}>
          <${CardHeader}>
            <${CardTitle}>The twelve tracked fields<//>
            <${CardDescription}>${populated} of 12 populated. Megawatts here are the data
              center's own IT load — a utility's generation, transmission or storage figure is a
              different quantity and is never added to them.<//>
          <//>
          <div style=${{ display: "grid", gap: 0 }}>
            ${TRACKED.map((key) => {
              const tier = tierOf(p, key);
              const q = quoteOf(p, key);
              const quoted = tier === "reported" || tier === "derived" || tier === "inferred";
              return html`
                <div key=${key} style=${{ display: "grid", gridTemplateColumns: "132px minmax(0,1fr)",
                     gap: 14, alignItems: "baseline", padding: "11px 20px", borderTop: "1px solid var(--border)" }}>
                  <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>${key}</span>
                  <div style=${{ display: "grid", gap: 5, minWidth: 0 }}>
                    <div style=${{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
                      <span style=${{ fontSize: 16, lineHeight: "24px",
                        ...(tier === "missing" ? { color: "var(--muted-foreground)", opacity: .7 } : { fontWeight: 500 }),
                        ...(RIGHT.has(key) ? { fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums" } : {}) }}>
                        ${fmt(key, p[key])}</span>
                      <span style=${chip(tier === "reported" ? "--foreground" : tier === "unconfirmed" ? "--warning"
                        : tier === "inferred" ? "--chart-5" : "--muted-foreground")}>${TIER[tier][0]}</span>
                      ${whyUnconfirmed(p, key) && html`
                        <span style=${chip("--danger")}>not this site's figure</span>`}
                      ${q.exact === false && !!provOf(p, key)?.quote && html`
                        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
                          excerpt, not this field's sentence</span>`}
                    </div>
                    ${whyUnconfirmed(p, key) && html`
                      <p style=${{ margin: 0, fontSize: 12, lineHeight: "18px", color: "var(--danger)" }}>
                        ${whyUnconfirmed(p, key).note}</p>`}
                    <p style=${{ margin: 0, fontSize: 12, lineHeight: "18px", color: "var(--muted-foreground)",
                      maxWidth: "80ch",
                      ...(q.exact ? { borderLeft: "2px solid var(--primary)", paddingLeft: 9 } : {}),
                      ...(tier === "missing" ? { opacity: .75 } : {}) }}>
                      ${quoted && q.exact ? `“${q.text}”` : q.text}</p>
                    ${key === "blocker" && html`<${BlockerWhy} why=${p.blocker_rationale} onTab=${onTab} />`}
                    <${ClaimTable} claims=${claims} field=${key} />
                  </div>
                </div>`;
            })}
          </div>
        <//>

        <div style=${{ display: "grid", gap: 18 }}>
          <${InContext} p=${p} data=${data} />

          <${Card}>
            <${CardHeader}>
              <${CardTitle}>Five independent tracks<//>
              <${CardDescription}>Five things happen in parallel, not in a line. A campus can own its
                land and still wait four years for power. Look at whichever track is stuck — the next
                milestone on it is the thing to watch for.<//>
            <//>
            <div style=${{ display: "grid", gap: 0 }}>
              ${p.standing.tracks.map((t) => {
                const onlyImplied = t.reached.length > 0 && t.reached.every((m) => t.implied.includes(m));
                /* `complete` means the ladder is exhausted, NOT that the work is
                   finished — so it is said as the milestone reached rather than as
                   a verdict. "construction: complete" beside a campus under
                   construction is the sentence that made this worth changing. */
                const status = t.complete ? `reached ${t.status.replace(/_/g, " ")}`
                  : t.blockers.length ? "blocked" : t.reached.length === 0 ? "not started" : "in progress";
                return html`
                  <div key=${t.track} style=${{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 90px",
                       gap: 12, alignItems: "center", padding: "11px 20px", borderTop: "1px solid var(--border)" }}>
                    <div style=${{ display: "grid", gap: 3, minWidth: 0 }}>
                      <span style=${{ fontSize: 14, fontWeight: 600 }}>${label(t.track)}</span>
                      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12,
                        color: t.blockers.length ? "var(--danger)" : t.complete ? "var(--success)" : "var(--muted-foreground)" }}>
                        ${status}${onlyImplied ? " (implied)" : ""} — ${t.next_milestone ? "watch for " + t.next_milestone
                          : t.blockers.length ? "nothing further on this ladder" : "nothing outstanding"}
                      </span>
                      ${/* WHICH obstacle. `blockers` holds bare category names, so
                            every surface could say a track was obstructed and none
                            could say by what — leaving a reader to guess which of
                            twenty-eight recorded risks was meant. */
                        (t.blocking_risks || []).length > 0 && html`
                        <span style=${{ fontSize: 12, lineHeight: "17px", color: "var(--danger)" }}>
                          blocked by #${t.blocking_risks[0].id} (${t.blocking_risks[0].category}):
                          ${" "}${t.blocking_risks[0].summary}
                          ${t.blocking_risks.length > 1
                            ? html`<span style=${{ color: "var(--muted-foreground)" }}>
                                ${" "}+${t.blocking_risks.length - 1} more</span>`
                            : null}
                        </span>`}
                    </div>
                    <div style=${{ display: "flex", gap: 3, justifyContent: "flex-end" }}>
                      ${((data.tracks.find((x) => x.key === t.track) || {}).milestones || []).map((m, i) => html`
                        <span key=${m} style=${{ "--i": i }} class=${`dc-seg-cell ${
                          t.reached.includes(m) ? (t.implied.includes(m) ? "dc-seg-cell--implied" : "dc-seg-cell--reached")
                          : t.blockers.length ? "dc-seg-cell--blocked" : "dc-seg-cell--todo"}`} />`)}
                    </div>
                  </div>`;
              })}
            </div>
            ${p.standing.watch_for && html`
              <div style=${{ padding: "12px 20px 18px", borderTop: "1px solid var(--border)" }}>
                <span style=${{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.08em",
                                color: "var(--muted-foreground)" }}>watch for</span>
                <p style=${{ margin: "4px 0 0", fontSize: 14, lineHeight: "21px" }}>${p.standing.watch_for}</p>
              </div>`}
          <//>
        </div>
      </div>

      <${Timeline} p=${p} />
    </div>`;
}

//: Block status to a colour token. `serving` and `energized` are the two that mean
//: megawatts are actually delivering, so they share the success colour.
//: One entry per block status: how to colour it, how to say it, and how far along it
//: is. `rank` orders the table so a reader meets a campus the way it was built —
//: running capacity first, plans last — instead of in whatever order the keys sorted.
//: The words are spelled out because `under_construction` is a database value, not
//: something to put in front of an analyst.
const BLOCK_STATE = {
  serving: { tone: "--success", label: "Serving", rank: 7 },
  energized: { tone: "--chart-2", label: "Energised", rank: 6 },
  shell_complete: { tone: "--chart-1", label: "Shell complete", rank: 5 },
  under_construction: { tone: "--warning", label: "Under construction", rank: 4 },
  permitting: { tone: "--chart-5", label: "Permitting", rank: 3 },
  planned: { tone: "--muted-foreground", label: "Planned", rank: 2 },
  paused: { tone: "--danger", label: "Paused", rank: 1 },
  cancelled: { tone: "--danger", label: "Cancelled", rank: 0 },
};

const blockState = (status) =>
  BLOCK_STATE[status] || { tone: "--muted-foreground", label: status, rank: 2 };

//: Megawatts, rounded the way capacity is actually published. A tranche is never
//: quoted to a decimal place, and "36" reads faster than "36.0".
const fmtMw = (v) => (v == null ? "—" : Math.round(v).toLocaleString());

//: A month is as precise as an `expected_online` ever honestly is — most are
//: normalised from "the second half of 2026" — so a day component invites a
//: precision the source never offered.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtMonth(iso) {
  if (!iso) return null;
  const [y, m] = String(iso).split("-");
  return MONTHS[Number(m) - 1] ? `${MONTHS[Number(m) - 1]} ${y}` : iso;
}

//: One row of the reconciliation under the tranches. Kept as table rows rather than
//: a paragraph so the megawatt column is a single column a reader can add up — the
//: whole complaint was that the numbers did not visibly add up.
function ReconRow({ label, mw, note, strong, tone }) {
  return html`
    <div class="dc-blockrow" style=${{ padding: "9px 4px", borderTop: "1px solid var(--border)" }}>
      <span style=${{ fontSize: 13, fontWeight: strong ? 600 : 400,
             color: tone ? `var(${tone})` : "inherit" }}>${label}</span>
      <span class="dc-num" style=${{ fontSize: 13, textAlign: "right", fontWeight: strong ? 600 : 400,
             color: tone ? `var(${tone})` : "inherit" }}>${mw}</span>
      <span style=${{ fontSize: 12, color: "var(--muted-foreground)", gridColumn: "3 / -1" }}>${note || ""}</span>
    </div>`;
}

/* The utility's plant, folded away under the campus it serves.
 *
 * Kept on the page rather than dropped: Entergy building 2,262 MW of gas *for
 * Hyperion* is one of the most important facts about that campus. It is simply
 * not the campus's capacity — a plant's nameplate output and a data center's IT
 * load are different quantities, and generation to serve a site normally runs
 * larger than the load it serves. So it gets its own heading saying whose it is,
 * and it is never added to the numbers above.
 *
 * The split is made in `webui/dataset.py` with the same predicate the sums use.
 * The page does not decide what counts as generation. */
function ServingInfrastructure({ rows, mw }) {
  const [open, setOpen] = useState(false);
  if (!rows.length) return null;
  return html`
    <${Card}>
      <button type="button" class="dc-disclose" aria-expanded=${open}
              onClick=${() => setOpen(!open)}>
        <span style=${{ display: "grid", gap: 3 }}>
          <span style=${{ fontSize: 14, fontWeight: 500 }}>
            Power serving this campus <span class="dc-num">${fmtMw(mw)} MW</span></span>
          <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
            ${rows.length} generation or grid asset${rows.length === 1 ? "" : "s"} — the utility's,
            measured as generating output. Never added to the campus figures above.</span>
        </span>
        <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>${open ? "hide" : "show"}</span>
      </button>
      ${open && html`
        <div style=${{ padding: "0 16px 12px" }}>
          ${rows.map((sec, i) => html`
            <div key=${i} class="dc-blockrow"
                 style=${{ padding: "9px 4px", borderTop: "1px solid var(--border)" }}>
              <span style=${{ fontSize: 13 }}>${sec.parent ? sec.parent + " · " : ""}${sec.label}</span>
              <span class="dc-num" style=${{ fontSize: 13, textAlign: "right" }}>
                ${sec.capacity == null ? "—" : fmtMw(sec.capacity) + " MW"}</span>
              <span style=${{ fontSize: 12, color: "var(--muted-foreground)", gridColumn: "3 / -1" }}>
                ${blockState(sec.status).label}</span>
            </div>`)}
        </div>`}
    <//>`;
}

function BlocksTab({ p }) {
  const blocks = p.blocks || [];
  const serving = p.serving || [];
  const acct = p.accounting;
  // Read off the reconciliation rather than summed here. The backend already puts
  // every megawatt of this campus on exactly one line, and a second sum in the page
  // is how the top of this panel once came to say 132 MW while the bottom said
  // 36,126.
  const servingMw = ((acct && acct.residuals) || [])
    .filter((r) => r.reason === "generation")
    .reduce((sum, r) => sum + r.mw, 0);
  const uncited = blocks.filter((b) => !b.mw_counted && b.mw != null);
  const customers = [...new Set(blocks.map((b) => b.customer).filter(Boolean))];

  // Furthest along first, then largest, so the eye lands on running capacity before
  // plans. Sorted here rather than server-side because it is a reading order, not a
  // fact about the data.
  /* **Rows are sections, ordered by identity — not by state.**
   *
   * A block is a section of a facility, so which section it is comes first and its
   * state is one of its attributes: "Building 3, under construction, delivering 0 of
   * 150 MW". This table used to sort by status and group its arithmetic by evidence
   * tier, which answers a provenance question ("what do we believe") in a panel
   * whose job is a site plan. Applied Digital's Polaris Forge read as seven rows in
   * status order; it is four buildings.
   *
   * The sections come from the backend. Deciding that `Building 2 (ELN-02)` and
   * `Building 2` are one building is a judgement, and the browser draws judgements
   * rather than making them. */
  const rows = p.sections || [];

  const cited = acct && acct.total != null ? acct.total : null;
  const campusTotal =
    cited != null ? cited : blocks.reduce((sum, b) => sum + (b.mw || 0), 0);

  // Which tranches were rejected as implausible is decided by `blocks.account`, not
  // here. Portland 3's "Hillsboro Phase 1" is 36,000 MW against a cited 144 MW
  // campus, and the panel used to announce "36,000 MW delivering of 144 MW". Judging
  // it a second time in the page would be a second definition of the rule, free to
  // disagree with the reconciliation printed a few rows below — which is how the top
  // of this panel came to say 132 MW while the bottom said 36,126.
  const rejectedLabels = new Set(
    (acct && acct.residuals ? acct.residuals : [])
      .filter((r) => r.reason === "out_of_scale")
      .flatMap((r) => r.labels),
  );
  const outOfScale = (b) => rejectedLabels.has(b.label);
  const rejected = blocks.filter(outOfScale);
  const plotted = blocks.filter((b) => b.mw != null && !outOfScale(b));
  const widest = Math.max(...plotted.map((b) => b.mw), 1);

  // Confirmed capacity by state, then everything the campus is made of that we will
  // not commit to. **Only `mw_counted` capacity counts as delivering** — the previous
  // version summed every `serving` tranche regardless, so 36,000 unconfirmed
  // megawatts were reported as running. A figure nobody confirmed cannot be the
  // headline number for a campus.
  const byState = {};
  for (const b of plotted.filter((x) => x.mw_counted)) {
    byState[b.status] = (byState[b.status] || 0) + b.mw;
  }
  const confirmed = Object.entries(byState).sort(
    (a, b) => blockState(b[0]).rank - blockState(a[0]).rank,
  );
  const live = confirmed
    .filter(([s]) => s === "serving" || s === "energized")
    .reduce((sum, [, mw]) => sum + mw, 0);

  /* Why the headline can honestly read 0 while the table below shows tranches
   * running: only cited capacity is summed, and a campus can be genuinely live
   * with every running tranche's MW either absent or 待确认. Fairwater was the
   * live case — energized in April per its own events, "0 MW delivering of
   * 350 MW" on screen. The zero stays (summing an unconfirmed figure to make it
   * go away is the 36,000 MW mistake in the comment above, in reverse), but a
   * bare zero next to running tranches reads as the page disagreeing with
   * itself, so it says why. */
  const running = blocks.filter(
    (b) => (b.status === "serving" || b.status === "energized") && !outOfScale(b),
  );
  const statedLive = running.reduce(
    (sum, b) => sum + (b.mw != null && !b.mw_counted ? b.mw : 0), 0,
  );
  const liveIsUncited = live === 0 && running.length > 0;

  // The bar is the accounting, drawn: confirmed capacity by state, then what is
  // stated but unconfirmed, then the remainder nobody has itemised. It is built to
  // sum to the campus total exactly, so the bar reaching full width *is* the
  // statement that every megawatt is accounted for — and the stated denominator is
  // always the cited total, so the headline can never disagree with the total row.
  // The uncommitted segments come straight from the reconciliation, not from a second
  // pass over the blocks. Recomputing them put "Stated, unconfirmed 54 MW" in the
  // legend against 36 MW three rows below, because the page lumped in capacity the
  // backend had classed as not-tied-to-a-facility. One name, two numbers, again.
  const RESIDUAL_LABEL = {
    unconfirmed: "Stated, unconfirmed",
    unplaceable: "Not tied to a facility",
    unitemised: "Not yet itemised",
  };
  const RESIDUAL_TONE = {
    unconfirmed: "--warning",
    unplaceable: "--chart-5",
    unitemised: "--border",
  };
  const extras = (acct && acct.residuals ? acct.residuals : [])
    .filter((r) => RESIDUAL_LABEL[r.reason] && r.mw > 0.5)
    .map((r) => [`_${r.reason}`, r.mw]);

  const shown = [...confirmed, ...extras].reduce((sum, [, mw]) => sum + mw, 0);
  const denom = Math.max(campusTotal, shown, 1);
  const bars = [...confirmed, ...extras];
  const barTone = (key) =>
    key.startsWith("_") ? RESIDUAL_TONE[key.slice(1)] : blockState(key).tone;
  const barLabel = (key) =>
    key.startsWith("_") ? RESIDUAL_LABEL[key.slice(1)] : blockState(key).label;

  // A date in the past on a tranche that is not running is not a plan, it is news we
  // have missed. Project 39 showed "Under construction" beside "Oct 2021" with no
  // acknowledgement that the date had passed four years ago — a contradiction in one
  // row, which is the whole complaint at the scale of a single line.
  const today = new Date().toISOString().slice(0, 10);
  const isOverdue = (b) =>
    !b.energized_on &&
    b.expected_online &&
    b.expected_online < today &&
    !["serving", "energized", "cancelled", "paused"].includes(b.status);

  return html`
    <div style=${{ display: "grid", gap: 16 }}>
      <!-- The headline: what is running, out of what. Replaces a paragraph of prose
           that made the reader do this arithmetic themselves. -->
      <div>
        <div style=${{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
          <span class="dc-num" style=${{ fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                 letterSpacing: "-0.02em" }}>${fmtMw(live)} MW</span>
          <span style=${{ fontSize: 14, color: "var(--muted-foreground)" }}>
            delivering of ${fmtMw(campusTotal)} MW${acct && acct.total_is_floor ? " (at least)" : ""}
            ${" "}· ${blocks.length} tranche${blocks.length === 1 ? "" : "s"}${customers.length > 1
              ? ` · ${customers.length} customers`
              : customers.length === 1 ? ` · ${customers[0]}` : ""}</span>
          ${liveIsUncited && html`
            <span style=${{ fontSize: 12, color: "var(--warning)" }}>
              ${running.length} tranche${running.length === 1 ? " is" : "s are"} running with no
              cited MW${statedLive > 0 ? ` — ${fmtMw(statedLive)} MW stated, 待确认` : ""}
            </span>`}
        </div>
        <div style=${{ display: "flex", height: 8, marginTop: 10, borderRadius: 4, overflow: "hidden",
             background: "var(--border)" }}>
          ${bars.map(([key, mw], i) => html`
            <div key=${i} title=${`${barLabel(key)}: ${fmtMw(mw)} MW`}
                 style=${{ width: `${(mw / denom) * 100}%`, background: `var(${barTone(key)})` }} />`)}
        </div>
        <div style=${{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 8 }}>
          ${bars.map(([key, mw], i) => html`
            <span key=${i} style=${{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12,
                   color: "var(--muted-foreground)" }}>
              <span style=${{ width: 8, height: 8, borderRadius: 2, background: `var(${barTone(key)})`,
                     border: key === "_gap" ? "1px solid var(--muted-foreground)" : "none" }} />
              ${barLabel(key)} <span class="dc-num">${fmtMw(mw)} MW</span></span>`)}
        </div>
      </div>

      <div>
        <!-- A header row, because the previous table had none: four unlabelled
             columns of dates, names and pills. -->
        <div class="dc-blockrow dc-blockhead" style=${{ padding: "0 4px 7px",
             fontSize: 11, textTransform: "uppercase", letterSpacing: "0.07em",
             color: "var(--muted-foreground)" }}>
          <span>Section</span>
          <span style=${{ textAlign: "right" }}>Delivering / held</span>
          <span>State</span>
          <span>Customer</span>
          <span style=${{ textAlign: "right" }}>Online</span>
        </div>

        ${rows.map((sec, i) => {
          const state = blockState(sec.status);
          const when = sec.energized_on || sec.expected_online;
          const cap = sec.capacity;
          const live = sec.delivering || 0;
          const overdue = !sec.energized_on && sec.expected_online
            && new Date(sec.expected_online) < new Date()
            && !["energized", "serving"].includes(sec.status);
          /* Only the two verdicts that mean something is *wrong* take a hue.
           * "Four sources named this building" is not a defect, so it is said in
           * words. Colour in this product means how much to believe a value, or
           * that a value is broken — never a category. */
          const bad = sec.capacity_conflict.length > 1 ? "var(--danger)"
                    : sec.verdict === "ambiguous" ? "var(--warning)" : null;
          return html`
            <div key=${i} class="dc-blockrow"
                 style=${{ padding: "11px 4px", borderTop: "1px solid var(--border)" }}>
              <div style=${{ minWidth: 0 }}>
                ${sec.parent && html`
                  <div style=${{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em",
                         color: "var(--muted-foreground)" }}>${sec.parent}</div>`}
                <div style=${{ fontSize: 14, fontWeight: 500 }}>${sec.label}</div>
                <!-- The other names sources gave this same section. Shown so the
                     grouping can be checked rather than trusted. -->
                ${sec.aliases.length > 0 && html`
                  <div style=${{ fontSize: 12, color: "var(--muted-foreground)", marginTop: 2 }}>
                    also called ${sec.aliases.join(", ")}</div>`}
                ${sec.verdict === "ambiguous" && html`
                  <div style=${{ fontSize: 12, color: "var(--warning)", marginTop: 2 }}>
                    which section this is could not be settled</div>`}
                ${sec.generic && !sec.parent && sec.aliases.length === 0 && html`
                  <div style=${{ fontSize: 12, color: "var(--muted-foreground)", marginTop: 2 }}>
                    not tied to a named facility</div>`}
              </div>
              <!-- Delivering of held: the question a section answers, and the one a
                   single planned-versus-built pair per campus cannot. NOTE no
                   backticks in here - this sits inside a template literal, and one
                   ends it early and turns the next word into bare code. -->
              <div>
                <div class="dc-num" style=${{ fontSize: 14, textAlign: "right",
                       color: cap == null ? "var(--muted-foreground)" : bad || "inherit" }}>
                  ${cap == null ? "—" : html`
                    <span style=${{ color: live > 0 ? "inherit" : "var(--muted-foreground)" }}
                      >${fmtMw(live)}</span
                    ><span style=${{ color: "var(--muted-foreground)" }}> / ${
                      withBound(fmtMw(cap), sec.capacity_confirmed ? "exact" : null)} MW</span>`}
                </div>
                ${cap != null && html`
                  <div style=${{ height: 3, marginTop: 4, marginLeft: "auto", borderRadius: 2,
                         width: "100%", background: "var(--border)", overflow: "hidden" }}>
                    <div style=${{ height: "100%", width: `${Math.min(100, (live / cap) * 100)}%`,
                           background: `var(${state.tone})`,
                           opacity: sec.capacity_confirmed ? 1 : 0.4 }} /></div>`}
                ${sec.capacity_conflict.length > 1 && html`
                  <div style=${{ fontSize: 10, textAlign: "right", marginTop: 3,
                         color: "var(--danger)" }}>
                    two sources confirm ${sec.capacity_conflict.map((v) => fmtMw(v)).join(" and ")}
                    — two figures, not one</div>`}
                ${cap != null && !sec.capacity_confirmed && html`
                  <div style=${{ fontSize: 10, textAlign: "right", marginTop: 3,
                         color: "var(--warning)" }}>待确认</div>`}
              </div>
              <div class="dc-bmeta" data-label="State">
                <span style=${chip(state.tone)}>${state.label}</span></div>
              <div class="dc-bmeta" data-label="Customer"
                   style=${{ fontSize: 13, minWidth: 0, overflowWrap: "anywhere" }}>
                ${sec.customer || html`<span style=${{ color: "var(--muted-foreground)" }}>—</span>`}</div>
              <div class="dc-bmeta"
                   data-label=${sec.energized_on ? "Live since" : overdue ? "Was due" : "Online"}
                   style=${{ textAlign: "right",
                          color: overdue ? "var(--warning)" : "var(--muted-foreground)" }}>
                ${(sec.energized_on || overdue) && html`
                  <div class="dc-bmeta-label" style=${{ fontSize: 10, textTransform: "uppercase",
                         letterSpacing: "0.05em" }}>${sec.energized_on ? "live since" : "was due"}</div>`}
                <span class="dc-num" style=${{ fontSize: 12 }}>${when ? fmtMonth(when) : "—"}</span>
              </div>
            </div>`;
        })}

        <!-- The arithmetic, closed. Every megawatt of the campus sits on one of these
             lines, which is the point: a reader who cannot make the parts reach the
             total stops trusting the rest of the row too. -->
        ${acct && html`
          <div style=${{ marginTop: 10, paddingTop: 2, borderTop: "2px solid var(--border)" }}>
            <${ReconRow} label="Cited per tranche" mw=${fmtMw(acct.counted_mw)}
              note="capacity a quote confirms for a named tranche" />
            ${acct.residuals.map((r, i) => html`
              <${ReconRow} key=${i} tone="--warning"
                label=${r.reason === "unitemised" ? "Not yet itemised"
                  : r.reason === "unconfirmed" ? "Stated, unconfirmed"
                  : r.reason === "unplaceable" ? "Not tied to a facility"
                  : r.reason === "out_of_scale" ? "Out of scale, excluded"
                  : r.reason === "generation" ? "Power serving the campus"
                  : "Counted twice over"}
                mw=${(r.reason === "overlap" ? "−" : "") + fmtMw(r.mw)} note=${r.note}
                tone=${r.reason === "out_of_scale" ? "--danger"
                  /* Generation is not a defect and takes no warning colour: the
                     line is the arithmetic working, not a fault to chase. */
                  : r.reason === "generation" ? "--muted-foreground" : "--warning"} />`)}
            <${ReconRow} strong label=${acct.total_is_floor ? "Accounted for (at least)" : "Accounted for"}
              mw=${fmtMw(acct.total)}
              note=${acct.total_is_floor ? "no source states a campus figure — this is the sum of the parts" : ""} />
          </div>`}
      </div>

      <${ServingInfrastructure} rows=${serving} mw=${servingMw} />

      ${rejected.length > 0 && html`
        <p style=${{ margin: 0, fontSize: 12, lineHeight: "19px", color: "var(--muted-foreground)" }}>
          <strong style=${{ color: "var(--danger)" }}>Out of scale:</strong>
          ${" "}${rejected.length} tranche${rejected.length === 1 ? "" : "s"}
          ${" "}(${rejected.map((b) => `${b.label} ${fmtMw(b.mw)} MW`).join(", ")})
          ${" "}${rejected.length === 1 ? "is" : "are"} larger than this whole campus, so
          ${" "}${rejected.length === 1 ? "it is" : "they are"} left out of the figures above.
          Almost always a kilowatt figure read as megawatts.
        </p>`}

      ${uncited.length > 0 && html`
        <p style=${{ margin: 0, fontSize: 12, lineHeight: "19px", color: "var(--muted-foreground)" }}>
          <strong style=${{ color: "var(--warning)" }}>待确认</strong> marks a capacity the article
          states without any quote naming that tranche. It is shown and accounted for above, and kept
          out of the cited figure — a number nobody stated should not read as one that was.
        </p>`}
    </div>`;
}

function RisksTab({ data, p }) {
  const label = (key) => (data.tracks.find((t) => t.key === key) || {}).label || key;
  const nextOn = (trackKey) => {
    const t = p.standing.tracks.find((x) => x.track === trackKey);
    return t ? (t.next_milestone || "track complete") : "—";
  };
  return html`
    <div style=${{ display: "grid", gap: 14 }}>
      <p style=${{ margin: 0, fontSize: 14, lineHeight: "22px", color: "var(--muted-foreground)", maxWidth: "88ch" }}>
        Obstacles are rows, not a sentence, so a project can carry several at once and each can be cleared
        on its own. An article that stops mentioning one does not clear it.
      </p>
      ${p.risks.map((r, i) => {
        const track = data.riskTrack[r.category];
        return html`
          <${Card} key=${i} style=${{ padding: "16px 20px",
            borderColor: r.status === "open" && r.severity === "blocking"
              ? "color-mix(in oklab, var(--danger) 34%, var(--border))" : undefined }}>
            <div style=${{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 14, fontWeight: 500 }}>${r.category}</span>
              <span style=${chip(SEV_TOKEN[r.severity])}>${r.severity}</span>
              <span style=${chip(r.status === "open" ? "--danger" : "--success")}>${r.status}</span>
              <span style=${{ flex: 1 }} />
              <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                ${r.first_seen || "—"}${r.resolved_at ? " → " + r.resolved_at : ""}${r.delay_days ? "  +" + r.delay_days + "d" : ""}
              </span>
            </div>
            <p style=${{ margin: "0 0 10px", fontSize: 16, lineHeight: "24px" }}>${r.summary}</p>
            <p style=${{ margin: 0, fontSize: 14, lineHeight: "21px",
              ...(r.quote ? { padding: "10px 13px", background: "var(--muted)",
                              borderLeft: "2px solid var(--primary)", borderRadius: "0 10px 10px 0" }
                          : { color: "var(--warning)", fontFamily: "var(--font-mono)", fontSize: 12 }) }}>
              ${r.quote ? `“${r.quote}”`
                : "uncited — the summary is a paraphrase and no verified sentence carries wording for this category."}
            </p>
            <div style=${{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, flexWrap: "wrap",
                           fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
              <span>track ${track ? label(track) : "unplaced"}</span><span>·</span>
              <span>next signal ${track ? nextOn(track) : "—"}</span>
            </div>
          <//>`;
      })}
      ${p.risks.length === 0 && html`
        <${EmptyState} variant="dashed" title="No open obstacle"
          description="Nothing read about this project reports one. If an obstacle appears later it lands here with the sentence that stated it." />`}
    </div>`;
}

function SourcesTab({ data, p }) {
  return html`
    <div style=${{ display: "grid", gap: 14 }}>
      <p style=${{ margin: 0, fontSize: 14, lineHeight: "22px", color: "var(--muted-foreground)", maxWidth: "88ch" }}>
        Every value above comes from one of these. One source alone caps confidence at 2, however good it
        is — two articles on the same website still count as one voice.
      </p>
      ${p.sources.map((s, i) => {
        const placeholder = s.url.includes("PLACEHOLDER");
        const fields = (s.fields || "").split(",").filter(Boolean);
        const quotes = s.quotes || {};
        /* A Census place-code lookup is stored as `government_doc` because it is
         * served from a .gov host, and it renders as a green weight-3 official
         * citation — which reads as "a government document supports this
         * project". It supports a county and a pair of coordinates, derived
         * deterministically, and nobody wrote it about this campus.
         *
         * The scores are unaffected: corroboration is counted over KEY_FIELDS,
         * which a geocode never claims, so this was only ever a label. Fixed as
         * a label rather than by re-tagging the rows — a migration to correct a
         * chip would be a lot of moving parts for a word. */
        const derived = (s.extractor || "").startsWith("derived:");
        return html`
          <${Card} key=${i}>
            <div style=${{ display: "flex", alignItems: "center", gap: 10, padding: "14px 20px",
                           borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
              <span style=${chip(derived ? "--muted-foreground"
                : ["company_filing", "government_doc"].includes(s.source_type) ? "--success"
                : s.source_type === "iso_queue" ? "--warning" : "--chart-1")}>
                ${derived ? "reference data" : s.source_type}</span>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}
                    title=${derived ? "Derived from a reference table, not reported about this project. It cannot corroborate a capacity, a date or a customer." : undefined}>
                ${derived ? "derived — corroborates nothing"
                          : `weight ${data.sourceWeight[s.source_type] || 1}`}</span>
              <span style=${{ flex: 1 }} />
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
                ${String(s.fetched_at).slice(0, 10)}</span>
            </div>
            <div style=${{ display: "grid", gap: 12, padding: "15px 20px 18px" }}>
              <a href=${s.url} target="_blank" rel="noreferrer"
                 style=${{ fontFamily: "var(--font-mono)", fontSize: 12, lineHeight: "20px", wordBreak: "break-all" }}>
                ${s.url}</a>
              ${placeholder && html`
                <p style=${{ margin: 0, padding: "10px 13px", fontSize: 12, lineHeight: "18px",
                  background: "color-mix(in oklab, var(--danger) 10%, transparent)",
                  border: "1px solid color-mix(in oklab, var(--danger) 28%, transparent)",
                  borderRadius: 12, color: "var(--danger)" }}>
                  This is a PLACEHOLDER URL. A placeholder is not a citation and is dropped before any
                  weighting — without that rule a first-party weight on a URL that does not exist would
                  hand a real project confidence 3 on the strength of nothing.</p>`}
              ${s.excerpt && html`
                <blockquote style=${{ margin: 0, padding: "11px 14px", background: "var(--muted)",
                  borderLeft: "2px solid var(--primary)", borderRadius: "0 10px 10px 0",
                  fontSize: 16, lineHeight: "24px" }}>${s.excerpt}</blockquote>`}
              <div style=${{ display: "grid", gap: 7 }}>
                <span style=${{ fontSize: 12, fontWeight: 500, textTransform: "uppercase",
                                letterSpacing: "0.08em", color: "var(--muted-foreground)" }}>supports</span>
                <div style=${{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                  ${fields.map((name) => html`
                    <span key=${name} style=${chip("--success")} title=${quotes[name] || ""}>
                      ${name}${quotes[name] ? " ❝" : ""}</span>`)}
                  ${fields.length === 0 && html`<span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>nothing</span>`}
                </div>
                ${s.unconfirmed_fields && html`
                  <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--warning)" }}>
                    not quotable, kept as 待确认: ${s.unconfirmed_fields}</span>`}
                ${Object.keys(quotes).length > 0 && html`
                  <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                    ❝ marks a field with its own recorded sentence; hover the chip to read it.</span>`}
              </div>
            </div>
          <//>`;
      })}
    </div>`;
}

/* ---- Map ----------------------------------------------------------------- */

function MapView({ data, onOpen, openId }) {
  const has3d = !!window.customElements?.get("dc-map3d");
  const [mode, setMode] = useState("2d");
  const [enc, setEnc] = useState("phase");
  const [chor, setChor] = useState("count");
  const [clusters, setClusters] = useState(false);
  const [zoom, setZoom] = useState(1);
  const hostRef = useRef(null);
  const mapRef = useRef(null);
  const narrow = useMedia(NARROW);

  // The element owns its own zoom (wheel, drag, pinch); these just drive it, so
  // the buttons and the gesture can never disagree about the current scale.
  const command = (how) => mapRef.current?.dispatchEvent(
    new CustomEvent("dc-zoom-cmd", { detail: { how } }),
  );

  // The custom elements dispatch a bubbling `dc-pick`; React has no synthetic
  // event for it, so listen on the host.
  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    const onPick = (e) => e.detail?.id && onOpen(e.detail.id);
    const onZoom = (e) => setZoom(e.detail?.k || 1);
    el.addEventListener("dc-pick", onPick);
    el.addEventListener("dc-zoom", onZoom);
    return () => { el.removeEventListener("dc-pick", onPick); el.removeEventListener("dc-zoom", onZoom); };
  }, [onOpen]);

  const plotted = data.projects.filter((p) => p.lat != null).length;
  const is3d = mode === "3d";

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 02 — geography" title="Roughly where they are">
        A dot sits on the middle of the town, ${html`<b>not</b>`} on the site — almost nobody publishes an
        address. Bigger dot, more megawatts. A hollow dashed ring means nobody has said how big it is.
        ${" "}${plotted} of ${data.projects.length} projects have coordinates.
      <//>

      <${Card}>
        <div style=${{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "12px 16px", padding: "14px 20px" }}>
          ${has3d && html`
            <div class="dc-seg">
              ${[["2d", "Flat"], ["3d", "Relief"]].map(([m, label]) => html`
                <button key=${m} type="button" class="dc-seg-btn" aria-pressed=${mode === m}
                        onClick=${() => setMode(m)}>${label}</button>`)}
            </div>`}
          <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
            <label for="m-enc" style=${{ fontSize: 12, fontWeight: 500, textTransform: "uppercase",
                                         letterSpacing: "0.08em", color: "var(--muted-foreground)" }}>colour</label>
            <${Select} id="m-enc" size="sm" value=${enc} onChange=${(e) => setEnc(e.target.value)}
                       style=${{ width: 150 }}>
              <option value="phase">phase</option>
              <option value="confidence">confidence</option>
              <option value="company">operator</option>
            <//>
          </div>
          <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
            <label for="m-chor" style=${{ fontSize: 12, fontWeight: 500, textTransform: "uppercase",
                                          letterSpacing: "0.08em", color: "var(--muted-foreground)" }}>state fill</label>
            <${Select} id="m-chor" size="sm" value=${chor} disabled=${is3d}
                       onChange=${(e) => setChor(e.target.value)} style=${{ width: 150 }}>
              <option value="count">project count</option>
              <option value="mw">cited MW</option>
              <option value="none">none</option>
            <//>
          </div>
          <${Switch} size="sm" label="Operator clusters" checked=${clusters} disabled=${is3d}
                     onCheckedChange=${(v) => setClusters(!!v)} />
          <span style=${{ flex: 1 }} />
          ${!is3d && html`
            <div style=${{ display: "flex", alignItems: "center", gap: 6 }}>
              <${Button} size="icon" variant="outline" aria-label="Zoom out"
                         disabled=${zoom <= 1.001} onClick=${() => command("out")}>−<//>
              <span class="dc-num" style=${{ minWidth: 42, textAlign: "center", fontSize: 12,
                                             color: "var(--muted-foreground)" }}>
                ${zoom.toFixed(1)}×</span>
              <${Button} size="icon" variant="outline" aria-label="Zoom in"
                         disabled=${zoom >= 11.9} onClick=${() => command("in")}>+<//>
            </div>`}
          <${Button} size="sm" variant="outline" disabled=${!is3d && zoom <= 1.001}
            onClick=${() => (is3d
              ? window.dispatchEvent(new Event("dc-reset-view"))
              : command("reset"))}>Reset view<//>
        </div>
      <//>

      <div style=${{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "stretch" }} ref=${hostRef}>
        <div style=${{ flex: "1 1 540px", minWidth: 0 }}>
          <${Card}>
            <div style=${{ position: "relative", height: narrow ? "60vh" : 600,
                           borderRadius: 20, overflow: "hidden" }}>
              ${!is3d && html`<div style=${{ position: "absolute", inset: narrow ? 4 : 12 }}>
                ${/* `compact` is dc-map's own narrow mode: no legend, no state
                     labels, tighter padding and smaller marks. At 300px the
                     labels overlap into illegibility. */ ""}
                <dc-map ref=${mapRef} encoding=${enc} choropleth=${chor}
                        clusters=${clusters ? "true" : null} compact=${narrow ? "true" : null}
                        selected=${openId == null ? "" : String(openId)} />
              </div>`}
              ${is3d && html`<div style=${{ position: "absolute", inset: 0 }}>
                <dc-map3d encoding=${enc} selected=${openId == null ? "" : String(openId)} />
              </div>`}
            </div>
          <//>
        </div>

        <div style=${{ flex: "1 1 300px", minWidth: 280, maxWidth: 420, display: "flex",
                       flexDirection: "column", gap: 9, maxHeight: 600, overflowY: "auto", paddingRight: 2 }}>
          ${data.projects.slice().sort((a, b) => (b.mw_planned || 0) - (a.mw_planned || 0)).map((p) => {
            const blocking = p.risks.some((r) => r.status === "open" && r.severity === "blocking");
            const token = enc === "confidence"
              ? ["--muted-foreground", "--warning", "--chart-1", "--success"][p.confidence]
              : PHASE_TOKEN[p.phase] || "--chart-1";
            return html`
              <button key=${p.id} type="button" class=${`dc-tile${openId === p.id ? " dc-tile--on" : ""}`}
                      onClick=${() => onOpen(p.id)}>
                <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style=${{ flex: "none", width: 10, height: 10, borderRadius: 999,
                    ...(p.mw_planned == null ? { border: `1.5px dashed var(${token})` } : { background: `var(${token})` }) }} />
                  <span style=${{ flex: 1, minWidth: 0, fontSize: 14, fontWeight: 600, overflow: "hidden",
                                  textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${p.company} — ${p.name}</span>
                  <span class="dc-num" style=${{ fontSize: 12,
                    color: p.mw_planned == null ? "var(--muted-foreground)" : "var(--foreground)" }}>
                    ${p.mw_planned == null ? "no MW" : p.mw_planned.toLocaleString() + " MW"}</span>
                </div>
                <div style=${{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
                               fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
                  <span>${place(p)}</span><span>·</span><span>${p.phase}</span>
                  ${blocking && html`<span style=${{ color: "var(--danger)" }}>· blocking</span>`}
                </div>
              </button>`;
          })}
        </div>
      </div>
    </div>`;
}

/* ---- Operations views ---------------------------------------------------- */

/* Crawl one queued article.
 *
 * Two-step rather than the typed-command-name confirmation `sync` uses. That
 * ceremony is proportionate to 25 LLM calls and absurd for one; a click, then a
 * confirm, is the same guarantee at the right size — a stray click still cannot
 * spend anything. The server-side rule is unchanged: it still requires the
 * confirmation string, and this supplies it only after the operator has said yes.
 */
function dupeLabel(dupes, ids) {
  const key = ids.join("-");
  const found = (dupes.group_evidence || []).find((g) => g.ids.join("-") === key);
  return found ? found.label : "";
}

/* One suspected duplicate group, as a reading rather than an action.
 *
 * It used to carry the merge: a radio to pick the survivor, the typed-name
 * confirmation, and a POST to /api/run. Folding rows is a CLI job now
 * (`tracker duplicates`, then `tracker merge`), so what is left is the part a
 * reader needs — that these rows may be one campus, how much capacity is claimed
 * twice between them, and *why* the backend raised the pair.
 *
 * Keeping the panel rather than deleting it with the button is deliberate. The
 * capex totals above it are the reason it is on this page at all: a duplicate is
 * how a number gets counted twice, and a reader looking at a total deserves to
 * know one is suspected even though this page cannot fix it. */
function DuplicateGroup({ ids, byId, evidence, label }) {
  const rows = ids.map((id) => byId[id]).filter(Boolean);

  // A row missing from `byId` means the dataset is older than the group.
  if (rows.length < 2) return null;
  const mw = rows.reduce((sum, p) => sum + (p.mw_planned || 0), 0);

  return html`
    <div style=${{ borderTop: "1px solid var(--border)", padding: "14px 20px", display: "grid", gap: 10 }}>
      <div style=${{ display: "flex", flexWrap: "wrap", gap: "4px 12px", alignItems: "baseline" }}>
        <span style=${{ fontSize: 13, fontWeight: 600 }}>
          ${rows[0].city || rows[0].county || "—"}${rows[0].state ? ", " + rows[0].state : ""}
        </span>
        <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
          ${rows.length} rows · ${Math.round(mw).toLocaleString()} MW claimed between them
        </span>
        ${label && html`<${Badge} variant="outline">${label}<//>`}
      </div>

      ${/* Why each pair was raised, in the backend's own words. "These look similar"
            is not something a reader can check; "both hold tranche horizon-1, a key
            that appears in no other town" is. A group of three is three separate
            questions and one of them is often the wrong one. */ ""}
      ${(() => {
        const lines = [];
        for (let i = 0; i < ids.length; i += 1) {
          for (let j = i + 1; j < ids.length; j += 1) {
            const [a, b] = ids[i] < ids[j] ? [ids[i], ids[j]] : [ids[j], ids[i]];
            const found = (evidence || {})[`${a}-${b}`];
            if (found) lines.push(html`<div key=${`${a}-${b}`}>#${a} + #${b}: ${found.why}</div>`);
          }
        }
        return lines.length > 0 && html`
          <div class="dc-num" style=${{ fontSize: 11.5, color: "var(--muted-foreground)",
                                        display: "grid", gap: 2 }}>${lines}</div>`;
      })()}

      <div style=${{ display: "grid", gap: 0 }}>
        ${rows.map((p) => html`
          <div key=${p.id} class="dc-dupe-row">
            <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>#${p.id}</span>
            <span style=${{ minWidth: 0 }}>
              <span style=${{ fontSize: 13, fontWeight: 500 }}>${p.company}</span>
              <span style=${{ fontSize: 13, color: "var(--muted-foreground)" }}> — ${p.name}</span>
            </span>
            <span class="dc-num" style=${{ fontSize: 12, textAlign: "right", whiteSpace: "nowrap" }}>
              ${p.mw_planned == null ? "—" : Math.round(p.mw_planned).toLocaleString() + " MW"}
            </span>
            <span style=${chip(PHASE_TOKEN[p.phase] || "--muted-foreground")}>${p.phase}</span>
            <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)", whiteSpace: "nowrap" }}
                  title=${`${p.sources.length} citation(s), last updated ${String(p.updated_at).slice(0, 10)}`}>
              ${p.sources.length} src · ${String(p.updated_at).slice(0, 10)}
            </span>
          </div>`)}
      </div>

    </div>`;
}

/* The buyer-position twin of InsightPanel: streamed from the capex overview
 * endpoint, cut at the sentinel server-side, cached by content so re-hovering a
 * buyer whose data has not moved is free. Same honesty framing — a model's
 * reading, never stored, never evidence. */
function CapexInsight({ posKey, allowAi }) {
  const [state, setState] = useState({ status: "idle", text: "" });

  useEffect(() => {
    if (!allowAi) { setState({ status: "unavailable", text: "" }); return; }
    const controller = new AbortController();
    setState({ status: "writing", text: "" });
    apiStream("/api/capex/overview/stream", { key: posKey, confirm: "overview" }, (event) => {
      if (event.type === "text") {
        setState((s) => ({ ...s, status: "writing", text: s.text + event.text }));
      } else if (event.type === "end") {
        setState((s) => ({ status: "done", text: event.text || s.text,
                           model: event.model, cached: event.cached }));
      } else if (event.type === "error") {
        setState((s) => ({ ...s, status: "failed", error: event.error }));
      }
    }, controller.signal).catch((e) => {
      if (e.name !== "AbortError") setState({ status: "failed", text: "", error: e.message });
    });
    return () => controller.abort();
  }, [posKey, allowAi]);

  const streaming = state.status === "writing";
  const shown = useTypewriter(state.text, streaming);
  const visible = state.text.slice(0, shown);
  const body = visible.trim()
    ? renderMarkdown(streaming ? tidyPartialMarkdown(visible) : visible)
    : null;

  return html`
    <section class="dc-ai" aria-label="Model-written reading of this buyer" style=${{ marginTop: 10 }}>
      <div class="dc-ai-head">
        <span class="dc-ai-spark" aria-hidden="true">✦</span>
        <span class="dc-ai-label">AI reading</span>
        ${streaming && html`<span class="dc-ai-dots" aria-live="polite" aria-label="writing"><i /><i /><i /></span>`}
      </div>
      ${state.status === "unavailable" && html`
        <p class="dc-ai-quiet">Unavailable: this console was started with --no-ai.</p>`}
      ${state.status === "failed" && html`<p class="dc-ai-quiet">${state.error}</p>`}
      ${streaming && !body && html`
        <div class="dc-ai-wait" aria-hidden="true">
          ${[92, 70].map((w) => html`<span key=${w} style=${{ width: w + "%" }} />`)}
        </div>`}
      ${body && html`
        <div class=${"dc-ai-body is-open" + (streaming ? " is-writing" : "")}>${body}</div>`}
      ${body && !streaming && html`
        <div class="dc-ai-foot">
          <span>Written by a model from the table's own rows — not stored, not evidence.</span>
        </div>`}
    </section>`;
}

/* The hover card: instant deterministic facts on top, the model's reading
 * streaming underneath. Fixed-position like QuotePopover, but interactive —
 * the reader can mouse into it, so it holds itself open. */
function CapexHoverCard({ hover, position, allowAi, onHold, onRelease }) {
  if (!hover || !position) return null;
  const facts = [
    [`${position.projects}`, position.projects === 1 ? "site" : "sites"],
    position.self_built > 0 && [`${position.self_built}`, "attributed from ownership"],
    position.mw_duplicate_skipped > 0 &&
      [`${Math.round(position.mw_duplicate_skipped).toLocaleString()} MW`, "set aside as duplicates"],
    position.investment_excluded_usd > 0 &&
      [fmtUSD(position.investment_excluded_usd), "claimed, never confirmed"],
  ].filter(Boolean);
  return html`
    <div class="dc-pop" onMouseEnter=${onHold} onMouseLeave=${onRelease}
         style=${{ position: "fixed", zIndex: 70, left: hover.x, top: hover.y,
                   width: "min(430px, 88vw)", maxHeight: "62vh", overflowY: "auto",
                   padding: "13px 15px", background: "var(--surface)",
                   border: "1px solid var(--border)", borderRadius: 14,
                   boxShadow: "var(--shadow-pop)" }}>
      <div style=${{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>${position.customer}</div>
      <div style=${{ display: "flex", flexWrap: "wrap", gap: "4px 16px" }}>
        ${facts.map(([big, label]) => html`
          <div key=${label} style=${{ display: "grid" }}>
            <span class="dc-num" style=${{ fontSize: 15, fontWeight: 600 }}>${big}</span>
            <span style=${{ fontSize: 10.5, color: "var(--muted-foreground)" }}>${label}</span>
          </div>`)}
      </div>
      <${CapexInsight} posKey=${position.key} allowAi=${allowAi} />
      <div style=${{ marginTop: 8, fontSize: 10.5, color: "var(--muted-foreground)" }}>
        click any number in the row to see the sites it is made of
      </div>
    </div>`;
}

/* The breakdown under a clicked cell.
 *
 * One component, seven views, because "8 sites", "$185B" and "MW at risk" are
 * three different questions and one generic site list answers none of them well.
 * Every figure here is a stored per-project value — the panel never sums, so a
 * reader adding the rows up must arrive at the cell they clicked. Where they
 * cannot (a site with no cited capacity), the row says so instead of showing a
 * zero that would balance the arithmetic and misstate the world. */
function CapexBreakdown({ position, col, byId, bucket, grain, onOpenProject }) {
  const sites = (position.project_ids || []).map((id) => byId[id]).filter(Boolean);
  const skipped = (position.duplicate_skipped_ids || []).map((id) => byId[id]).filter(Boolean);
  const yearOf = (s) => (s.expected_online ? String(s.expected_online).slice(0, 4) : null);
  const quarterOf = (s) => {
    if (!s.expected_online) return null;
    const m = Number(String(s.expected_online).slice(5, 7));
    return `${String(s.expected_online).slice(0, 4)}Q${Math.floor((m - 1) / 3) + 1}`;
  };

  const label = { fontFamily: "var(--font-mono)", fontSize: 10.5, textTransform: "uppercase",
                  letterSpacing: "0.08em", color: "var(--muted-foreground)", margin: "8px 0 4px" };
  const row = { display: "flex", flexWrap: "wrap", gap: "2px 10px", alignItems: "baseline" };
  const idTag = { color: "var(--muted-foreground)", fontSize: 11 };
  const where = (s) => `${s.city || s.county || "?"}, ${s.state}`;

  // A clickable site line: opens the project's own drawer, so the drill-down
  // bottoms out at the citations rather than at another aggregate.
  const site = (s, extra) => html`
    <div key=${s.id} style=${{ ...row, cursor: "pointer" }}
         onClick=${() => onOpenProject && onOpenProject(s)}
         title="open this project's citations">
      <span class="dc-num" style=${idTag}>#${s.id}</span>
      <span style=${{ fontWeight: 600 }}>${s.company} — ${s.name}</span>
      <span style=${{ color: "var(--muted-foreground)" }}>${where(s)}</span>
      ${extra}
    </div>`;

  const mw = (v) => v ? Math.round(v).toLocaleString() + " MW" : null;
  const missing = (text) => html`<span class="dc-v dc-v--missing">${text}</span>`;

  let heading = "";
  let body = null;

  if (col === "sites") {
    heading = `every site counted for ${position.customer}`;
    body = sites
      .slice()
      .sort((a, b) => (b.mw_planned || 0) - (a.mw_planned || 0))
      .map((s) => site(s, html`
        <span class="dc-num">${mw(s.mw_planned) || missing("unsized")}</span>
        <span style=${{ color: "var(--muted-foreground)" }}>${s.phase}</span>
        ${s.expected_online && html`
          <span style=${{ color: "var(--muted-foreground)" }}>online ${yearOf(s)}</span>`}`));
  } else if (col === "planned" || col === "running") {
    const field = col === "planned" ? "mw_planned" : "mw_built";
    const have = sites.filter((s) => s[field]).sort((a, b) => b[field] - a[field]);
    const without = sites.filter((s) => !s[field]);
    const top = have.length ? have[0][field] : 0;
    heading = col === "planned"
      ? `planned capacity, site by site — adds to ${Math.round(position.mw_planned).toLocaleString()} MW`
      : `capacity a source says is running — adds to ${Math.round(position.mw_built).toLocaleString()} MW`;
    body = html`
      ${have.map((s) => site(s, html`
        <span class="dc-num" style=${{ fontWeight: 600 }}>${mw(s[field])}</span>
        <span aria-hidden="true" style=${{ display: "inline-block", height: 6, borderRadius: 3,
              minWidth: 2, width: Math.max(2, Math.round((s[field] / top) * 90)) + "px",
              background: "var(--chart-2)" }}></span>`))}
      ${without.length > 0 && html`
        <div style=${{ ...label, marginTop: 8 }}>
          ${without.length} site(s) contribute nothing to this figure</div>
        ${without.map((s) => site(s, missing(
          col === "planned" ? "no cited capacity" : "nothing cited as running")))}`}`;
  } else if (col === "money") {
    const have = sites.filter((s) => s.investment_usd)
      .sort((a, b) => b.investment_usd - a.investment_usd);
    const without = sites.filter((s) => !s.investment_usd);
    heading = "investment, site by site — only figures a source confirmed are summed";
    body = html`
      ${have.map((s) => {
        const why = (s.unconfirmed_because || {}).investment_usd;
        return site(s, html`
          <span class="dc-num" style=${{ fontWeight: 600,
                ...(why ? { color: "var(--muted-foreground)" } : {}) }}>
            ${fmtUSD(s.investment_usd)}</span>
          ${why && html`<span style=${chip("--warning")} title=${why}>claimed, not counted</span>`}`);
      })}
      ${without.length > 0 && html`
        <div style=${{ ...label, marginTop: 8 }}>${without.length} site(s) with no cited investment</div>
        ${without.map((s) => site(s, missing("nobody has said")))}`}`;
  } else if (col === "risk") {
    const atRisk = sites.filter((s) => (s.risks || []).some((r) => r.status === "open"));
    heading = "what is obstructing this buyer's sites";
    body = atRisk.length === 0
      ? html`<div style=${{ color: "var(--muted-foreground)" }}>no open obstacle on any counted site</div>`
      : atRisk.map((s) => html`
        <div key=${s.id} style=${{ marginBottom: 6 }}>
          ${site(s, html`<span class="dc-num">${mw(s.mw_planned) || missing("unsized")}</span>`)}
          ${(s.risks || []).filter((r) => r.status === "open").map((r, i) => html`
            <div key=${i} style=${{ paddingLeft: 18, fontSize: 12, display: "flex",
                  flexWrap: "wrap", gap: "2px 8px", alignItems: "baseline" }}>
              <span style=${chip(r.severity === "blocking" ? "--danger" : "--warning")}>
                ${r.category}/${r.severity}</span>
              <span style=${{ color: "var(--muted-foreground)" }}>${r.summary}</span>
            </div>`)}
        </div>`);
  } else if (col === "delays") {
    const slipped = sites.filter((s) => (s.events || []).some((e) => e.event_type === "delayed"));
    heading = "sites whose expected online date has moved later";
    body = slipped.length === 0
      ? html`<div style=${{ color: "var(--muted-foreground)" }}>no recorded slip on any counted site</div>`
      : slipped.map((s) => html`
        <div key=${s.id} style=${{ marginBottom: 6 }}>
          ${site(s, s.expected_online
            ? html`<span class="dc-num">now online ${yearOf(s)}</span>` : null)}
          ${(s.events || []).filter((e) => e.event_type === "delayed").map((e, i) => html`
            <div key=${i} style=${{ paddingLeft: 18, fontSize: 12, color: "var(--muted-foreground)" }}>
              ${e.description}</div>`)}
        </div>`);
  } else if (col === "bucket") {
    const inBucket = sites.filter((s) =>
      (grain === "quarter" ? quarterOf(s) : yearOf(s)) === bucket && s.mw_planned);
    heading = `capacity dated ${bucket} — a site contributes only if it cites both a capacity and a date`;
    body = inBucket.length === 0
      ? html`<div style=${{ color: "var(--muted-foreground)" }}>
          nothing this buyer holds is dated ${bucket}</div>`
      : inBucket.sort((a, b) => b.mw_planned - a.mw_planned).map((s) => site(s, html`
          <span class="dc-num" style=${{ fontWeight: 600 }}>${mw(s.mw_planned)}</span>
          <span style=${{ color: "var(--muted-foreground)" }}>
            online ${String(s.expected_online)}</span>
          <span style=${{ color: "var(--muted-foreground)" }}>${s.phase}</span>`));
  }

  return html`
    <div style=${{ display: "grid", gap: 3, fontSize: 12.5, padding: "2px 0 6px" }}>
      <div style=${label}>${heading}</div>
      ${body}
      ${col === "sites" && skipped.length > 0 && html`
        <div style=${{ color: "var(--muted-foreground)" }}>
          <div style=${label}>set aside as suspected duplicates — not counted anywhere above</div>
          ${skipped.map((s) => html`
            <div key=${s.id} style=${row}>
              <span class="dc-num" style=${{ fontSize: 11 }}>#${s.id}</span>
              <span>${s.company} — ${s.name}</span>
              <span class="dc-num">${mw(s.mw_planned) || "unsized"}</span>
            </div>`)}
        </div>`}
      <div style=${{ fontSize: 10.5, color: "var(--muted-foreground)", marginTop: 4 }}>
        click any site to open its citations
      </div>
    </div>`;
}

function CapexView({ data, allowAi, onOpen }) {
  const capex = data.capex;
  const cover = capex.coverage;
  const dupes = capex.duplicates;
  const byId = useMemo(
    () => Object.fromEntries(data.projects.map((p) => [p.id, p])), [data.projects]);
  const reviewRef = useRef(null);

  // Which periods get a column is a judgement — years as a continuous range so a
  // gap year shows as an empty column, quarters data-only — so the server computes
  // it (capex.year_columns) and this component only renders it.
  const [grain, setGrain] = useState("year");
  const buckets = grain === "quarter"
    ? (capex.quarter_columns || [])
    : (capex.year_columns || []).map(String);
  const bucketOf = (p, b) =>
    grain === "quarter" ? (p.mw_by_quarter || {})[b] : p.mw_by_year[b];
  const attributed = Math.round((cover.attributed_pct / 100) * cover.projects);
  // Sums of server-sent disclosures — arithmetic, not a judgement of our own.
  const skippedMW = capex.positions.reduce((t, p) => t + (p.mw_duplicate_skipped || 0), 0);
  const excludedUSD = capex.positions.reduce((t, p) => t + (p.investment_excluded_usd || 0), 0);

  const num = (v, suffix = "") =>
    v ? Math.round(v).toLocaleString() + suffix : html`<span class="dc-v dc-v--missing">—</span>`;

  // Headline sums of server-sent values — arithmetic only, per the rule that the
  // browser never computes a judgement of its own.
  const totalPlanned = capex.positions.reduce((t, p) => t + (p.mw_planned || 0), 0);
  const totalBuilt = capex.positions.reduce((t, p) => t + (p.mw_built || 0), 0);
  const totalUSD = capex.positions.reduce((t, p) => t + (p.investment_usd || 0), 0);
  const skippedUSD = capex.positions.reduce(
    (t, p) => t + (p.investment_duplicate_skipped_usd || 0), 0);
  const gw = (mw) => mw >= 1000
    ? `${(mw / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} GW`
    : `${Math.round(mw).toLocaleString()} MW`;
  const groupHead = {
    fontSize: 10, fontFamily: "var(--font-mono)", fontWeight: 500,
    textTransform: "uppercase", letterSpacing: "0.08em",
    color: "var(--muted-foreground)", textAlign: "center",
    padding: "8px 8px 0", whiteSpace: "nowrap",
  };
  const head = (label, why, align) => html`
    <${TableHead} align=${align}><span title=${why}>${label}</span><//>`;

  // Every number in this table opens. `open` is {key, col}: which buyer, and
  // which column the reader clicked — because "8 sites" and "$185B" and "MW at
  // risk" want three different breakdowns, and showing one generic list for all
  // of them is how a drill-down becomes decoration. Clicking a second column on
  // the same row switches the view; clicking the same one closes it.
  const [open, setOpen] = useState(null);
  const isOpen = (p, col) => open && open.key === p.key && open.col === col;
  const toggle = (p, col) => setOpen(isOpen(p, col) ? null : { key: p.key, col });
  // A cell that opens something. Deliberately not a <button>: it must not break
  // the numeric alignment the whole table depends on.
  const openable = (p, col, title) => ({
    style: { textAlign: "right", cursor: "pointer",
             ...(isOpen(p, col) ? { background: "var(--accent)" } : {}) },
    title,
    onClick: () => toggle(p, col),
  });
  const [hover, setHover] = useState(null);
  const hoverTimer = useRef(null);
  const leaveTimer = useRef(null);
  useEffect(() => () => { clearTimeout(hoverTimer.current); clearTimeout(leaveTimer.current); }, []);
  const startHover = (event, p) => {
    clearTimeout(hoverTimer.current);
    clearTimeout(leaveTimer.current);
    const r = event.currentTarget.getBoundingClientRect();
    const W = 430, H = 280;
    const below = r.bottom + H + 16 < window.innerHeight;
    const coords = {
      key: p.key,
      x: Math.max(10, Math.min(r.left, window.innerWidth - W - 12)),
      y: below ? r.bottom + 6 : Math.max(10, r.top - H - 6),
    };
    hoverTimer.current = setTimeout(() => setHover(coords), 450);
  };
  const endHover = () => {
    clearTimeout(hoverTimer.current);
    leaveTimer.current = setTimeout(() => setHover(null), 250);
  };
  const holdHover = () => clearTimeout(leaveTimer.current);
  const hovered = hover === null ? null : capex.positions.find((p) => p.key === hover.key);

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 03 — capex" title="Who is actually paying for it">
        Each row is one ${html`<b>buyer</b>`} — the company all that capacity is ultimately for, which is
        often not the company on the building: Meta builds its own campuses, OpenAI mostly rents from
        developers like Crusoe. Read a row left to right: how many sites, how much capacity is planned
        and already running, the confirmed money behind it, and when the megawatts are expected to
        arrive. ${html`<b>*</b>`} means the buyer was worked out from who owns the site, because no
        source named a tenant. ${html`<b>Every number here opens</b>`} — click any figure in a row and
        it breaks down into the sites that make it up, and clicking a site opens its citations. Hover a
        buyer's name for a model's short reading of the position.
      <//>

      ${/* The page in five numbers, before any column has to be decoded. */ ""}
      <${Card}>
        <div class="dc-capex-cover">
          ${[[gw(totalPlanned), "planned or building",
              "capacity in the pipeline across every buyer below; a project nobody has sized counts zero"],
             [gw(totalBuilt), "already running",
              "megawatts a source says are energised and serving"],
             [fmtUSD(totalUSD) || "$0", "confirmed spend",
              "only figures a source confirmed for that specific site"],
             [fmtUSD(excludedUSD + skippedUSD) || "$0", "claimed, not counted",
              "programme-wide totals and duplicate rows — disclosed below, never summed"],
             [`${Math.round(cover.attributed_pct)}%`, "have a named buyer",
              "the rest sit in the bottom row as capacity nobody has claimed"],
            ].map(([big, label, why]) => html`
            <div key=${label} style=${{ display: "grid", gap: 4, minWidth: 0 }}>
              <span class="dc-num" style=${{ fontSize: 22, fontFamily: "var(--font-display)" }}>${big}</span>
              <span style=${{ fontSize: 12, fontWeight: 600 }}>${label}</span>
              <span style=${{ fontSize: 11, lineHeight: "16px", color: "var(--muted-foreground)" }}>${why}</span>
            </div>`)}
        </div>
      <//>

      ${dupes.groups.length > 0 && html`
        <${Alert} variant="warning">
          <div>
            <div class="mrd-alert-title">
              ${dupes.groups.length === 1
                ? "One campus is stored more than once"
                : `${dupes.groups.length} campuses are stored more than once`}
            </div>
            <div class="mrd-alert-desc">
              One campus usually has a builder, a landowner and an occupier, and each name a source
              picks becomes its own row. This table counts one row per suspected campus and sets
              ${" "}${Math.round(skippedMW).toLocaleString()} MW aside — skipped, not merged, so the
              rows are still there.
              ${" "}
              <button type="button" class="dc-link"
                      onClick=${() => reviewRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })}>
                Review them below
              </button> to fold them for real.
            </div>
          </div>
        <//>`}

      <${Card}>
        <div style=${{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px 14px",
                       padding: "14px 20px 0" }}>
          <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
            pipeline by
          </span>
          <div class="dc-seg" style=${{ padding: 3 }}>
            ${[["year", "year"], ["quarter", "quarter"]].map(([key, label]) => html`
              <button key=${key} type="button" class="dc-seg-btn" aria-pressed=${grain === key}
                      onClick=${() => setGrain(key)}>${label}</button>`)}
          </div>
          ${grain === "quarter" && html`
            <span style=${{ fontSize: 12, color: "var(--warning)" }}>
              a shape, not a schedule — ${Math.round(capex.date_precision?.year_only_pct || 0)}% of
              dated projects land on 1 January, which is where "sometime in 2027" normalises to
            </span>`}
        </div>
        <div style=${{ minWidth: 0 }}>
          <${Table} density="compact">
            <${TableHeader}>
              ${/* Group row first: the year columns mean nothing until the reader
                    knows they are megawatts arriving. Raw <th> so colSpan is
                    guaranteed to survive; the real column row stays Meridian. */ ""}
              <tr>
                <th colSpan=${2}></th>
                <th colSpan=${2} style=${groupHead}>capacity, MW</th>
                <th></th>
                <th colSpan=${buckets.length} style=${groupHead}>MW arriving, by ${grain}</th>
                <th colSpan=${3} style=${groupHead}>trouble</th>
              </tr>
              <${TableRow}>
                ${head("buyer", "the company the capacity is ultimately for")}
                ${head("sites", "projects attributed to this buyer", "right")}
                ${head("planned", "capacity a source states is planned or building", "right")}
                ${head("running", "energised and serving today", "right")}
                ${head("confirmed $", "investment some source confirmed for that specific site", "right")}
                ${buckets.map((b) => html`
                  <${TableHead} key=${b} align="right">
                    <span title=${`planned MW expected online in ${b}`}>${b}</span><//>`)}
                ${head("MW at risk", "planned MW on projects with an open obstacle", "right")}
                ${head("delays", "projects whose online date has moved later", "right")}
                ${head("biggest obstacle", "the most severe open obstacle across this buyer's sites")}
              <//>
            <//>
            <${TableBody}>
              ${capex.positions.map((p, i) => {
                const residual = p.key === "";
                if (!p.projects) {
                  // A buyer whose every row was set aside as a suspected duplicate.
                  // A rank of dashes here read as broken data; say what happened.
                  return html`
                    <tr key=${p.key || "__none"} style=${{ color: "var(--muted-foreground)" }}>
                      <td class="dc-cell dc-cell--wide">
                        <span style=${{ fontSize: 13 }}>${p.customer}</span></td>
                      <td colSpan=${7 + buckets.length} style=${{ fontSize: 12, padding: "6px 10px" }}>
                        nothing counted — ${Math.round(p.mw_duplicate_skipped).toLocaleString()} MW${" "}
                        ${p.investment_duplicate_skipped_usd
                          ? `and ${fmtUSD(p.investment_duplicate_skipped_usd)} `
                          : ""}set aside as duplicate rows of a campus counted above
                      </td>
                    </tr>`;
                }
                const anyOpen = open && open.key === p.key;
                return html`
                  <tr key=${p.key || "__none"} class=${i < 12 ? "dc-enter" : undefined}
                      style=${{ ...(i < 12 ? { "--i": i } : {}),
                                ...(residual ? { color: "var(--muted-foreground)" } : {}) }}>
                    <td class="dc-cell dc-cell--wide" style=${{ cursor: "pointer" }}
                        title="click for every site counted for this buyer"
                        onClick=${() => toggle(p, "sites")}
                        onMouseEnter=${(e) => startHover(e, p)}
                        onMouseLeave=${endHover}>
                      <span aria-hidden="true" style=${{ color: "var(--muted-foreground)",
                            fontSize: 10, marginRight: 5 }}>${anyOpen ? "▾" : "▸"}</span>
                      <span style=${{ fontWeight: residual ? 400 : 600, fontSize: 13 }}>${p.customer}</span>
                      ${p.self_built > 0 && html`
                        <span title=${`${p.self_built} of ${p.projects} attributed from ownership rather than a cited tenant`}
                              style=${{ color: "var(--muted-foreground)" }}>
                          ${p.self_built === p.projects ? " *" : ` (${p.self_built}*)`}</span>`}
                    </td>
                    <td class="dc-num" ...${openable(p, "sites", "the sites behind this count")}>
                      ${p.projects}</td>
                    <td class="dc-num" ...${openable(p, "planned", "which sites make up this capacity")}
                        style=${{ textAlign: "right", fontWeight: 600, cursor: "pointer",
                                  ...(isOpen(p, "planned") ? { background: "var(--accent)" } : {}) }}>
                      ${num(p.mw_planned)}</td>
                    <td class="dc-num" ...${openable(p, "running", "which sites are actually running")}>
                      ${num(p.mw_built)}</td>
                    <td class="dc-num" ...${openable(p, "money", "the investment figure site by site")}
                        style=${{ textAlign: "right", whiteSpace: "nowrap", cursor: "pointer",
                                  ...(isOpen(p, "money") ? { background: "var(--accent)" } : {}) }}>
                      ${p.investment_usd ? fmtUSD(p.investment_usd) : html`<span class="dc-v dc-v--missing">—</span>`}
                      ${p.investment_excluded_usd > 0 && html`<span
                            title="claimed by a source but confirmed by none — usually a programme-wide total quoted in an article about one site; disclosed, never summed"
                            style=${{ color: "var(--muted-foreground)", fontWeight: 400, fontSize: 11 }}>
                          ${" "}+${fmtUSD(p.investment_excluded_usd)} claimed</span>`}
                    </td>
                    ${buckets.map((b) => html`
                      <td key=${b} class="dc-num"
                          style=${{ textAlign: "right", cursor: "pointer",
                                    ...(open && open.key === p.key && open.col === "bucket:" + b
                                        ? { background: "var(--accent)" } : {}) }}
                          title=${`which sites land in ${b}`}
                          onClick=${() => toggle(p, "bucket:" + b)}>
                        ${num(bucketOf(p, b))}</td>`)}
                    <td class="dc-num" ...${openable(p, "risk", "what is obstructing these sites")}
                        style=${{ textAlign: "right", cursor: "pointer",
                                  color: p.mw_at_risk ? "var(--warning)" : undefined,
                                  ...(isOpen(p, "risk") ? { background: "var(--accent)" } : {}) }}>
                      ${num(p.mw_at_risk)}</td>
                    <td class="dc-num" ...${openable(p, "delays", "which sites have slipped")}>
                      ${p.slipped || ""}</td>
                    <td style=${{ fontSize: 12, whiteSpace: "nowrap", cursor: "pointer" }}
                        title="what is obstructing these sites"
                        onClick=${() => toggle(p, "risk")}>
                      ${p.worst_open_risk
                        ? html`<span style=${chip(p.worst_open_risk.endsWith("blocking") ? "--danger" : "--warning")}>
                            ${p.worst_open_risk}</span>`
                        : ""}</td>
                  </tr>
                  ${anyOpen && html`
                    <tr key=${(p.key || "__none") + ":open"}>
                      <td colSpan=${8 + buckets.length}
                          style=${{ padding: "2px 16px 14px", borderTop: "none" }}>
                        <${CapexBreakdown} position=${p}
                          col=${open.col.startsWith("bucket:") ? "bucket" : open.col}
                          bucket=${open.col.startsWith("bucket:") ? open.col.slice(7) : null}
                          grain=${grain} byId=${byId}
                          onOpenProject=${(s) => onOpen && onOpen(s.id)} />
                      </td>
                    </tr>`}`;
              })}
            <//>
          <//>
          ${capex.positions.length === 0 && html`
            <div style=${{ padding: "8px 20px 20px" }}>
              <${EmptyState} variant="dashed" title="Nothing to attribute yet"
                description="Load some projects and this becomes the other axis on them." />
            </div>`}
        </div>

        ${/* Two footers, and both are load-bearing. The first says how much of the
              database is in the table at all; the second says that everything in
              it is a lower bound. Either one omitted turns a floor into a total. */ ""}
        <div style=${{ padding: "12px 20px 16px", borderTop: "1px solid var(--border)",
                       display: "grid", gap: 6, fontSize: 12, lineHeight: "18px",
                       color: "var(--muted-foreground)" }}>
          ${/* Every gap between an expression and the next word is an explicit
                ${" "}: htm drops the newline-plus-indent between a `${}` and the
                text after it, which silently produced "attributed —25% because". */ ""}
          <span>
            ${html`<b>Every number here is a minimum.</b>`} If nobody has said how big a project is, it
            counts as zero — so a buyer really has at least this much, usually more.
          </span>
          <span>
            We can name a buyer for ${Math.round(cover.attributed_pct)}% of projects:
            ${" "}${Math.round(cover.named_tenant_pct)}% because a source said so, and
            ${" "}${Math.round(cover.self_built_pct)}% worked out from who owns the site — those are
            marked ${html`<b>*</b>`}. Biggest first, with the "nobody named" row pinned to the bottom;
            it is a leftover, not a buyer.
          </span>
          ${/* The one column where "a floor" is the wrong warning: it can still
                be too high when a confirmed figure covers more than one site, and
                saying only that it is a lower bound would be the opposite of
                honest about it. */ ""}
          <span>
            ${html`<b>Trust the megawatts before the dollars.</b>`} The investment column sums only
            figures some source confirmed for that site.
            ${excludedUSD > 0 && html`${" "}${fmtUSD(excludedUSD)} more was claimed but never
            confirmed — usually a headline number for a whole programme ("OpenAI's $500 billion
            Stargate") attached to one site — and is excluded and disclosed, not summed.`}
            ${" "}Open a project to see which of its numbers are flagged.
          </span>
        </div>
      <//>

      <${CapexHoverCard} hover=${hover} position=${hovered} allowAi=${allowAi}
        onHold=${holdHover} onRelease=${endHover} />

      <${Card}>
        <${CardHeader}>
          <${CardTitle}>How much of the data this page covers<//>
          <${CardDescription}>We can name a buyer for ${attributed} of ${cover.projects} projects.
            The rest sit in the last row. Worth knowing before you quote a number off this table.<//>
        <//>
        <div class="dc-capex-cover">
          ${[["we know the buyer", cover.attributed_pct, "for the rest, nobody has said who it is for"],
             ["a source named them", cover.named_tenant_pct, "someone wrote down who the tenant is"],
             ["worked out from owner", cover.self_built_pct, "the operator builds for itself, so it is the buyer"],
             ["size is known", cover.with_capacity_pct, "the rest count as zero MW here"],
             ["size and date known", cover.in_timeline_pct, "only these can appear in a year column"],
            ].map(([label, pct, why]) => html`
            <div key=${label} style=${{ display: "grid", gap: 4, minWidth: 0 }}>
              <span class="dc-num" style=${{ fontSize: 22, fontFamily: "var(--font-display)" }}>
                ${Math.round(pct)}%</span>
              <span style=${{ fontSize: 12, fontWeight: 600 }}>${label}</span>
              <span style=${{ fontSize: 11, lineHeight: "16px", color: "var(--muted-foreground)" }}>${why}</span>
            </div>`)}
        </div>
      <//>

      ${capex.suspect.length > 0 && html`
        <${Card}>
          <${CardHeader}>
            <${CardTitle}>${capex.suspect.length} project(s) list a builder as the customer<//>
            <${CardDescription}>These name a company we track as an operator as if it were the tenant,
              which is usually a mix-up — a developer builds for other people, it does not rent from
              them. Flagged, not corrected, because occasionally it is genuinely true.<//>
          <//>
          <div style=${{ display: "grid", gap: 0 }}>
            ${capex.suspect.map((s) => html`
              <div key=${s.id} style=${{ display: "flex", flexWrap: "wrap", gap: "2px 10px", alignItems: "baseline",
                   padding: "9px 20px", borderTop: "1px solid var(--border)", fontSize: 13 }}>
                <span class="dc-num" style=${{ color: "var(--muted-foreground)", fontSize: 12 }}>#${s.id}</span>
                <span>${s.operator}</span>
                <span style=${{ color: "var(--muted-foreground)" }}>→ customer</span>
                <span style=${{ fontWeight: 600 }}>${s.customer}</span>
              </div>`)}
          </div>
        <//>`}

      <div ref=${reviewRef}>
        <${Card}>
          <${CardHeader}>
            <${CardTitle}>One campus, several rows<//>
            <${CardDescription}>Rows that look like the same site, with the reason each pair was
              raised. ${html`<b>Read this before trusting a total above</b>`} — a suspected duplicate is
              how capacity gets counted twice. Folding them is a decision with no undo, so it is not a
              button here: ${html`<b class="dc-num">tracker duplicates</b>`} prints the same groups
              and${" "}${html`<b class="dc-num">tracker merge</b>`} acts on them.<//>
          <//>
          ${dupes.groups.map((ids) => html`
            <${DuplicateGroup} key=${ids.join("-")} ids=${ids} byId=${byId}
                               evidence=${dupes.evidence} label=${dupeLabel(dupes, ids)} />`)}
          ${dupes.groups.length === 0 && html`
            <div style=${{ padding: "4px 20px 20px" }}>
              <${EmptyState} variant="dashed" size="sm" title="No suspected duplicates"
                description="No two rows in one locality look like the same site." />
            </div>`}
        <//>
      </div>
    </div>`;
}

/* Type to find a project; click to take it.
 *
 * The pickers began as plain `<select>`s holding every row in the database. With
 * 224 of them that is not a picker, it is a wall — and the report back was that
 * the form had made things *harder*, because a command taking several projects
 * looked like it took one. Same matcher as the table's search box, so `#42`,
 * `meta ohio` and `abilene` behave the same in both places. */
function ProjectSearch({ projects, exclude = [], placeholder, onPick, value }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  const hits = useMemo(() => {
    const skip = new Set(exclude.map(String));
    return projects.filter((p) => !skip.has(String(p.id)) && matchesProject(p, query)).slice(0, 40);
  }, [projects, exclude.join(","), query]);

  // A click anywhere else closes it. Without this the list stays over whatever
  // you were reaching for next.
  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const take = (project) => { onPick(project.id); setQuery(""); setOpen(false); };

  return html`
    <div ref=${box} style=${{ position: "relative" }}>
      <${Input} size="sm" value=${query} placeholder=${placeholder || "search projects…"}
        onFocus=${() => setOpen(true)}
        onChange=${(e) => { setQuery(e.target.value); setOpen(true); }}
        onKeyDown=${(e) => {
          if (e.key === "Enter" && hits.length) { e.preventDefault(); take(hits[0]); }
          if (e.key === "Escape") setOpen(false);
        }} />
      ${open && html`
        <div class="dc-picker">
          ${hits.length === 0 && html`
            <div class="dc-picker-empty">
              nothing matches ${query.trim() ? html`<b>${query.trim()}</b>` : "yet"}
            </div>`}
          ${hits.map((p) => html`
            <button key=${p.id} type="button" class="dc-picker-row" onClick=${() => take(p)}>
              <span class="dc-picker-id">#${p.id}</span>
              <span class="dc-picker-name">${p.company} — ${p.name}</span>
              <span class="dc-picker-where">${place(p)}</span>
            </button>`)}
          ${hits.length === 40 && html`
            <div class="dc-picker-empty">first 40 shown — keep typing to narrow it</div>`}
        </div>`}
      ${value != null && value !== "" && html`
        <div style=${{ marginTop: 6 }}>
          <button type="button" class="dc-chip-x" title="Clear" onClick=${() => onPick("")}>
            ${(() => { const p = projects.find((x) => String(x.id) === String(value));
                       return p ? `#${p.id} ${p.name}` : `#${value}`; })()}
            <span aria-hidden="true">✕</span>
          </button>
        </div>`}
    </div>`;
}

const WINDOWS = [
  [1, "today"],
  [7, "week"],
  [30, "month"],
];

/* Green for good, red for bad, quiet for neither — the semantic tones, not a hue
 * chosen here, so a theme switch cannot leave one of them unreadable. */
const SIGN_TONE = {
  good: { color: "var(--success)", soft: "var(--success-soft)", mark: "+" },
  bad: { color: "var(--danger)", soft: "var(--danger-soft)", mark: "−" },
  neutral: { color: "var(--muted-foreground)", soft: "var(--muted)", mark: "·" },
};

/* A crawl that died looks exactly like a quiet week on a page like this, so the
 * header says when the last citation was fetched and complains past this. Two
 * days, because the ingest runs nightly: one missed night is a hiccup, two is a
 * fault worth a person's attention. */
const STALE_HOURS = 48;

function hoursSince(iso) {
  if (!iso) return null;
  const then = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  return Number.isNaN(then.getTime()) ? null : (Date.now() - then.getTime()) / 3.6e6;
}

function shortDate(iso) {
  return iso ? iso.slice(0, 10) : null;
}

/* What you can watch, offered rather than guessed at.
 *
 * The first version of this was a bare text box, which is a demand that you
 * already know what the database calls things. Everything it needs to answer that
 * is in `/api/dataset`, which the page is holding anyway: 300 projects, their
 * operators, and the tenants they are being built for.
 *
 * **Three kinds of candidate, because a watch has three shapes.** A company covers
 * everything it builds; a tenant covers what others build *for* it (the server
 * matches `project.customer` and per-block customers, so this is not a courtesy —
 * it is the case where the interesting news is filed under a developer's name); a
 * project narrows to one campus. The row says which, because "xAI" and
 * "xAI | Colossus" are different subscriptions and the difference is not visible
 * from the text alone.
 *
 * **Already-watched is computed from the server's own answer, never re-derived
 * here.** Each watchlist entry ships the `project_ids` it resolved to, so a
 * candidate is covered when its projects are already in that set. The alternative
 * was reimplementing `dedup.company_key` — legal-suffix stripping, the alias table
 * — in JavaScript, where it would drift from the Python that actually decides. */
function watchCandidates(projects) {
  const companies = new Map();
  const tenants = new Map();
  for (const p of projects) {
    const company = (p.company || "").trim();
    if (company) {
      const row = companies.get(company.toLowerCase()) || { kind: "company", label: company, entry: company, ids: [], mw: 0 };
      row.ids.push(p.id);
      row.mw += p.mw_planned || 0;
      companies.set(company.toLowerCase(), row);
    }
    /* Tenants only where somebody else is building: a company that is its own
       customer is already offered above, and listing it twice would suggest the
       two entries do different things. `is_undisclosed` cases ("undisclosed
       hyperscaler") name nobody, so they are not a subscription anybody wants. */
    const tenant = (p.customer || "").trim();
    if (tenant && tenant.toLowerCase() !== company.toLowerCase() && !/undisclosed|unnamed|confidential|not disclosed/i.test(tenant)) {
      const row = tenants.get(tenant.toLowerCase()) || { kind: "tenant", label: tenant, entry: tenant, ids: [], mw: 0 };
      row.ids.push(p.id);
      row.mw += p.mw_planned || 0;
      tenants.set(tenant.toLowerCase(), row);
    }
  }
  const byCount = (a, b) => b.ids.length - a.ids.length || b.mw - a.mw || a.label.localeCompare(b.label);
  return [
    ...[...companies.values()].sort(byCount),
    ...[...tenants.values()].sort(byCount),
    ...projects
      .map((p) => ({
        kind: "project",
        label: `${p.company} — ${p.name}`,
        entry: `${p.company} | ${p.name}`,
        where: place(p),
        ids: [p.id],
        mw: p.mw_planned || 0,
        project: p,
      }))
      .sort((a, b) => b.mw - a.mw || a.label.localeCompare(b.label)),
  ];
}

const WATCH_KIND_LABEL = { company: "operator", tenant: "tenant", project: "project" };

/* Rank on where the query hit, not merely whether it did.
 *
 * "meta" has to put the operator Meta above a project whose blocker sentence
 * happens to mention it, and a company above the twelve projects it contains,
 * because the company is the subscription that covers all twelve. Word-wise like
 * `matchesProject`, so "meta ohio" narrows here the same way it narrows the table. */
function rankWatchCandidates(candidates, query, coveredIds) {
  const q = query.trim().toLowerCase();
  const kindRank = { company: 0, tenant: 1, project: 2 };
  const scored = [];
  for (const c of candidates) {
    const covered = c.ids.length > 0 && c.ids.every((id) => coveredIds.has(id));
    if (!q) {
      // The resting state teaches what is in there: the biggest operators, then
      // the biggest tenants. Projects are not offered until asked for — 300 of
      // them is the wall the pickers in Commands were built to avoid.
      if (c.kind === "project") continue;
      scored.push({ ...c, covered, score: kindRank[c.kind] * 1000 - c.ids.length });
      continue;
    }
    const hay = c.kind === "project"
      ? [c.project.name, c.project.company, c.project.customer, c.project.city, c.project.county, c.project.state]
          .filter(Boolean).join(" ").toLowerCase()
      : c.label.toLowerCase();
    const words = q.split(/\s+/);
    if (!words.every((w) => hay.includes(w))) continue;
    const primary = c.label.toLowerCase();
    const at = primary.indexOf(words[0]);
    const exact = primary === q ? -400 : 0;
    const prefix = at === 0 ? -200 : at > 0 ? -100 : 0;
    scored.push({ ...c, covered, score: exact + prefix + kindRank[c.kind] * 10 - Math.min(c.ids.length, 9) });
  }
  scored.sort(
    (a, b) =>
      Number(a.covered) - Number(b.covered) ||
      a.score - b.score ||
      a.label.localeCompare(b.label),
  );
  return scored;
}

const WATCH_LIMIT = 30;

function WatchPicker({ projects, watchlist, disabled, onAdd, error }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const box = useRef(null);
  const list = useRef(null);

  const candidates = useMemo(() => watchCandidates(projects), [projects]);
  const coveredIds = useMemo(
    () => new Set((watchlist || []).flatMap((w) => w.project_ids || [])),
    [watchlist],
  );
  const hits = useMemo(
    () => rankWatchCandidates(candidates, query, coveredIds).slice(0, WATCH_LIMIT),
    [candidates, query, coveredIds],
  );

  const firstOpen = hits.findIndex((h) => !h.covered);
  useEffect(() => { setActive(firstOpen < 0 ? 0 : firstOpen); }, [query, firstOpen]);

  // A click anywhere else closes it, or the list stays over whatever you were
  // reaching for next. Same handler `ProjectSearch` uses.
  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  // Keep the keyboard selection on screen. Without this, holding ↓ walks the
  // highlight off the bottom of a 260px scroller and you are steering blind.
  useEffect(() => {
    const row = list.current?.querySelector('[data-active="true"]');
    if (row) row.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const take = (candidate) => {
    if (!candidate || candidate.covered) return;
    onAdd(candidate.entry);
    setQuery("");
    setOpen(false);
  };

  /* Typed text that matches nothing is still a legitimate watch: setting one
     before the project is tracked is the normal case for a campus somebody read
     about this morning, and the server says so too. So Enter takes it verbatim
     rather than refusing, and the empty state says that is what will happen. */
  const submitTyped = () => {
    const typed = query.trim();
    if (!typed) return;
    onAdd(typed);
    setQuery("");
    setOpen(false);
  };

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      if (!hits.length) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      // Skips rows already watched. They are shown — "you have this one" is worth
      // knowing while you type — but walking onto one gives you a keystroke that
      // does nothing, which reads as the picker being broken.
      setActive((i) => {
        for (let n = 1; n <= hits.length; n += 1) {
          const next = (i + step * n + hits.length * n) % hits.length;
          if (!hits[next].covered) return next;
        }
        return i;
      });
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (open && hits[active] && !hits[active].covered) return take(hits[active]);
      // A covered highlight means the list has nothing left to offer for this
      // text. Taking the text verbatim is still a real request — "Vantage | VA14"
      // when VA13 is watched — so it goes to the server, which decides.
      return submitTyped();
    }
    if (e.key === "Escape") { setOpen(false); e.stopPropagation(); }
  };

  return html`
    <div ref=${box} style=${{ position: "relative", maxWidth: 420 }}>
      <div style=${{ display: "flex", gap: 8, alignItems: "center" }}>
        <${Input} size="sm" value=${query} disabled=${disabled}
                  role="combobox" aria-expanded=${open} aria-autocomplete="list"
                  placeholder="watch a company, a tenant, or one project…"
                  onFocus=${() => setOpen(true)}
                  onChange=${(e) => { setQuery(e.target.value); setOpen(true); }}
                  onKeyDown=${onKeyDown} />
        <${Button} size="sm" variant="outline" disabled=${disabled || !query.trim()}
                   onClick=${() => (hits[active] && !hits[active].covered ? take(hits[active]) : submitTyped())}>
          Watch<//>
      </div>

      ${error && html`
        <div style=${{ fontSize: 12, color: "var(--danger)", paddingTop: 5 }}>${error}</div>`}

      ${open && !disabled && html`
        <div class="dc-picker" ref=${list} role="listbox">
          ${!query.trim() && html`
            <div class="dc-picker-empty">
              the operators and tenants with the most projects — or type a project name
            </div>`}
          ${!hits.length && html`
            <div class="dc-picker-empty">
              nothing here matches <b>${query.trim()}</b> — Enter watches it anyway, and it
              starts reporting as soon as a project appears
            </div>`}
          ${hits.map((c, i) => html`
            <button key=${`${c.kind}:${c.entry}`} type="button" role="option"
                    aria-selected=${i === active} data-active=${i === active}
                    class="dc-picker-row"
                    disabled=${c.covered}
                    style=${{ opacity: c.covered ? 0.5 : 1,
                              background: i === active && !c.covered ? "var(--accent-soft)" : undefined }}
                    onMouseEnter=${() => setActive(i)}
                    onClick=${() => take(c)}>
              <span class="dc-picker-id">${WATCH_KIND_LABEL[c.kind]}</span>
              <span class="dc-picker-name">
                ${c.label}
                ${c.kind === "project" && c.where
                  ? html`<span class="dc-picker-where"> · ${c.where}</span>`
                  : null}
              </span>
              <span class="dc-picker-where">
                ${c.covered
                  ? "watching"
                  : c.kind === "project"
                  ? (c.mw ? `${fmtMw(c.mw)} MW` : "")
                  : `${c.ids.length} project${c.ids.length === 1 ? "" : "s"}`}
              </span>
            </button>`)}
          ${hits.length > 0 && firstOpen < 0 && html`
            <div class="dc-picker-empty">
              you already watch everything matching <b>${query.trim()}</b>
            </div>`}
          ${hits.length === WATCH_LIMIT && html`
            <div class="dc-picker-empty">first ${WATCH_LIMIT} shown — keep typing to narrow it</div>`}
        </div>`}
    </div>`;
}

/* One watched entity as a chip: what it is, how it went, and two things you can
 * do to it.
 *
 * The counts are the point of the strip — "xAI, 3 updates, 1 bad" is the whole
 * page in one line, and it is what makes the list below skippable on a quiet day.
 * The arrows carry a title and an accessible label because a bare ▼ beside a
 * number is a glyph, not a fact.
 *
 * Clicking the body filters the list to that entity, which is the question a chip
 * invites and the first thing tried on it. */
function WatchChip({ entity, digest, onRemove, onFilter, filtered, disabled }) {
  const bad = digest?.bad || 0;
  const good = digest?.good || 0;
  const projects = entity.project_ids.length;
  const parts = [
    `${projects} project${projects === 1 ? "" : "s"}`,
    digest ? `${digest.total} update${digest.total === 1 ? "" : "s"} in this window` : null,
    bad ? `${bad} bad` : null,
    good ? `${good} good` : null,
    entity.note || null,
  ].filter(Boolean);

  return html`
    <span class=${`dc-watch${filtered ? " dc-watch--on" : ""}`}>
      <button type="button" class="dc-watch-body" title=${parts.join(" · ")}
              aria-pressed=${filtered}
              onClick=${() => onFilter(filtered ? null : entity.entry)}>
        <b style=${{ fontWeight: 500 }}>${entity.entry}</b>
        <span class="dc-watch-num" aria-label=${`${projects} projects`}>${projects}p</span>
        ${bad
          ? html`<span class="dc-watch-num" style=${{ color: "var(--danger)" }}
                       aria-label=${`${bad} bad`} title=${`${bad} bad`}>▼${bad}</span>`
          : null}
        ${good
          ? html`<span class="dc-watch-num" style=${{ color: "var(--success)" }}
                       aria-label=${`${good} good`} title=${`${good} good`}>▲${good}</span>`
          : null}
        ${!projects
          ? html`<span class="dc-watch-num" style=${{ color: "var(--warning)" }}
                       title="nothing in the database matches this yet">no match</span>`
          : null}
      </button>
      ${!disabled &&
      html`<button type="button" class="dc-watch-x" aria-label=${`Stop watching ${entity.entry}`}
                   title=${`Stop watching ${entity.entry}`}
                   onClick=${() => onRemove(entity.entry)}>✕</button>`}
    </span>`;
}

/* The watchlist, and the box that edits it.
 *
 * Editable from the page deliberately, and it is the console's *only* write: a
 * `watch` row says whose news to show, nothing derives from it, and the person
 * whose list it is is sitting in front of the published page rather than at a
 * terminal.
 *
 * **It is one account's list, and needs an account to exist.** The server sends
 * `allow_watch: false` to a visitor of a console with no accounts — there is
 * nobody to own a list — so this reads that one field and does not have to know
 * the difference between "--no-watch-edits" and "nobody is signed in". What it
 * does have to get right is that an empty list and an absent list look the same
 * here and mean different things, which is what the two messages below are. */
function Watchlist({ payload, projects, allowWatch, filter, onFilter, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const entities = payload?.watchlist || [];
  const digests = useMemo(
    () => Object.fromEntries((payload?.entities || []).map((e) => [e.entry, e])),
    [payload],
  );
  /* From `/api/dataset`, which the shell already has, rather than from the digest.
     Reading it off the digest meant the input did not exist until that request
     landed, and vanished again on every window change — taking whatever was
     half-typed with it. The server still decides; this only decides whether to
     offer the control. */
  const editable = !!allowWatch;

  const send = async (body) => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await api("/api/watch", { method: "POST", body }));
    } catch (err) {
      // Inline, under the box that caused it. A page-level banner for a rejected
      // entry puts the complaint nowhere near the thing being complained about.
      setError(err.message || "that did not work");
    } finally {
      setBusy(false);
    }
  };

  return html`
    <div style=${{ display: "grid", gap: 10 }}>
      <div style=${{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        ${entities.map((e) => html`
          <${WatchChip} key=${e.entry} entity=${e} digest=${digests[e.entry]}
                        filtered=${filter === e.entry} onFilter=${onFilter}
                        disabled=${!editable || busy}
                        onRemove=${(entry) => send({ action: "remove", entry })} />`)}
        ${!entities.length &&
        html`<span style=${{ fontSize: 13, color: "var(--muted-foreground)" }}>
          ${!editable
            ? html`Everything — ${payload?.projects_watched ?? 0} projects. A watchlist belongs to
                   an account; sign in to keep one.`
            : payload?.watch_all
              ? html`Watching everything — ${payload?.projects_watched ?? 0} projects.
                     Name a company to narrow it.`
              : html`Watching nothing yet. Name a company below, or take
                     all ${projects?.length ?? 0} of them.`}
        </span>`}
        ${editable &&
        html`<button type="button" class="dc-linkish" style=${{ fontSize: 12 }}
                     disabled=${busy}
                     onClick=${() => send({ action: "watch_all", value: !payload?.watch_all })}>
          ${payload?.watch_all ? "watch only my list" : "watch everything"}
        </button>`}
        ${filter &&
        html`<button type="button" class="dc-linkish" style=${{ fontSize: 12 }}
                     onClick=${() => onFilter(null)}>show all watches</button>`}
      </div>

      ${editable &&
      html`<${WatchPicker} projects=${projects} watchlist=${entities} disabled=${busy}
                           error=${error} onAdd=${(entry) => send({ action: "add", entry })} />`}
    </div>`;
}

/* One thing that changed.
 *
 * The order of the lines is the argument: what happened, what it means for the
 * five tracks, the sentence somebody published, then the dates and the publisher.
 * A reader who stops after two lines has still had the finding.
 *
 * **Both dates, always.** "energized (2024-09-01, learned 2026-08-11)" is a
 * milestone from two years ago that reached us last night, and a page that printed
 * only one of those dates would be lying in one direction or the other. */
function SignalCard({ signal, onOpen }) {
  const tone = SIGN_TONE[signal.sign] || SIGN_TONE.neutral;
  const happened = shortDate(signal.happened);
  const learned = shortDate(signal.at);
  return html`
    <article style=${{ display: "grid", gap: 6, padding: "14px 16px",
                       borderLeft: `3px solid ${tone.color}`, background: "var(--card)",
                       borderRadius: "0 var(--radius) var(--radius) 0" }}>
      <div style=${{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
        <span aria-hidden="true" style=${{ color: tone.color, fontWeight: 600 }}>${tone.mark}</span>
        ${/* `headline` rather than `label`: the category alone made a resolved
             obstacle read as a live one, because the sentence under it is the
             obstacle's own summary. Composed server-side so the CLI says the
             same thing. */ ""}
        <b style=${{ fontWeight: 500 }}>${signal.headline}</b>
        <button type="button" class="dc-linkish" onClick=${() => onOpen(signal.project_id)}>
          ${signal.company} — ${signal.project}
        </button>
        ${signal.expected &&
        html`<span style=${{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em",
                             color: "var(--warning)" }}>expected, not reached</span>`}
        ${signal.unblocks &&
        html`<span style=${{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em",
                             color: "var(--success)" }}>the blocker moved</span>`}
        ${signal.notify &&
        html`<span title="this crosses the notification bar: tracker digest --notify"
                   style=${{ fontSize: 11, fontFamily: "var(--font-mono)",
                             color: "var(--muted-foreground)" }}>would notify</span>`}
      </div>

      ${signal.effect &&
      html`<div style=${{ fontSize: 13, color: signal.unblocks ? "var(--foreground)" : "var(--muted-foreground)",
                          fontWeight: signal.unblocks ? 500 : 400 }}>${signal.effect}</div>`}

      <div style=${{ fontSize: 14, lineHeight: "22px" }}>${signal.detail}</div>

      ${signal.quote &&
      html`<blockquote style=${{ margin: 0, paddingLeft: 12, borderLeft: "1px solid var(--border)",
                                 fontSize: 13, color: "var(--muted-foreground)" }}>
        ${signal.quote}
      </blockquote>`}

      <div class="dc-num" style=${{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 12,
                                    color: "var(--muted-foreground)" }}>
        <span>${happened || "undated"}</span>
        ${learned && html`<span>· learned ${learned}</span>`}
        ${signal.publisher && html`<span>· ${signal.publisher}</span>`}
        ${signal.entry && html`<span>· watching ${signal.entry}${
          signal.via && signal.via !== "operator" ? ` (as ${signal.via.replace(/_/g, " ")})` : ""
        }</span>`}
        ${signal.restatements
          ? html`<span title="the same moment, reported again elsewhere">· +${signal.restatements} more report${signal.restatements > 1 ? "s" : ""}</span>`
          : null}
      </div>
    </article>`;
}

function UpdatesView({ data, onOpen }) {
  const [days, setDays] = useState(7);
  const [payload, setPayload] = useState(null);
  const [failed, setFailed] = useState(null);
  const [showHeld, setShowHeld] = useState(false);
  /* Which watch the list is narrowed to, if any. Held here rather than in the
     chip strip because the signal list below is what it filters. */
  const [only, setOnly] = useState(null);
  /* Off by default: the page is the place that shows everything, and the whole
     argument for a notification bar is that it is *higher* than this one. The
     toggle is for the reader who wants to see what a nightly `--notify` would
     have sent. */
  const [onlyAlerts, setOnlyAlerts] = useState(false);

  /* `nonce` is how an edit to the watchlist re-reads the digest. Patching the
     new entry into the payload in place would leave every tally beside it
     describing the previous scope — an entry with no counts reads as a quiet
     week rather than as a number nobody has computed yet. */
  const [nonce, setNonce] = useState(0);

  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(null);
    api(`/api/updates?days=${days}`)
      .then((body) => { if (!cancelled) setPayload(body); })
      .catch((err) => { if (!cancelled) setFailed(err.message || "could not read the updates"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [days, nonce]);

  const stale = hoursSince(payload?.last_crawl);
  const counts = payload?.counts;
  const shown = useMemo(
    () =>
      payload
        ? payload.signals.filter(
            (s) => (!onlyAlerts || s.notify) && (!only || s.entry === only),
          )
        : [],
    [payload, onlyAlerts, only],
  );

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gap: 24, padding: "26px 26px 60px",
                                            maxWidth: 820 }}>
      <${Eyebrow} figure="fig. 00 — updates" title="What changed on what you are watching">
        Good news and bad, most material first, for the companies and projects on your list.
        The window is on when <em>we</em> learned something — a crawl reads one article and
        imports a whole back-history, so a milestone from 2022 can be this morning's news.
        Every line carries both dates. The ones marked <b>would notify</b> are what a nightly
        <code>tracker digest --notify</code> sends: a blocker moving, a decisive milestone, a
        dated slip, or an obstacle of material severity opening or clearing. Everything else
        is here to be read, not to interrupt you.
      <//>

      <div class="dc-band">
        <${Watchlist} payload=${payload} projects=${data.projects}
                      allowWatch=${data.allow_watch}
                      filter=${only} onFilter=${setOnly}
                      onChanged=${(body) => {
                        /* `watch_all` rides along only when the toggle sent it, so
                           an add/remove must not reset it to undefined. */
                        setPayload((p) =>
                          p
                            ? {
                                ...p,
                                watchlist: body.watchlist,
                                watch_all:
                                  body.watch_all === undefined ? p.watch_all : body.watch_all,
                              }
                            : p,
                        );
                        setOnly(null);
                        setNonce((n) => n + 1);
                      }} />
      </div>

      <div class="dc-band" style=${{ display: "flex", gap: 14, alignItems: "center",
                                     flexWrap: "wrap", paddingBottom: 18 }}>
        <div class="dc-seg">
          ${WINDOWS.map(([n, label]) => html`
            <button key=${n} type="button" class="dc-seg-btn" aria-pressed=${days === n}
                    onClick=${() => setDays(n)}>${label}</button>`)}
        </div>
        ${counts && loading &&
        html`<span style=${{ fontSize: 13, color: "var(--muted-foreground)" }}>counting…</span>`}
        ${counts && !loading &&
        html`<span style=${{ fontSize: 13 }}>
          <b>${counts.total}</b> update${counts.total === 1 ? "" : "s"} —
          <span style=${{ color: "var(--success)" }}>${counts.good} good</span>,
          <span style=${{ color: "var(--danger)" }}>${counts.bad} bad</span>
        </span>`}
        ${!!counts?.total && !loading &&
        html`<button type="button" class="dc-linkish" aria-pressed=${onlyAlerts}
                     onClick=${() => setOnlyAlerts((v) => !v)}>
          ${onlyAlerts ? "show all" : `${counts.notify} worth telling you about`}
        </button>`}
        <span style=${{ flex: "1 1 20px" }} />
        ${payload &&
        html`<span style=${{ fontSize: 12, color: stale != null && stale > STALE_HOURS
                                                    ? "var(--warning)" : "var(--muted-foreground)" }}>
          ${payload.last_crawl
            ? `last crawl ${payload.last_crawl.replace("T", " ").slice(0, 16)}${
                stale != null && stale > STALE_HOURS ? ` — ${Math.floor(stale / 24)} days ago` : ""
              }`
            : "nothing has ever been fetched into this database"}
        </span>`}
      </div>

      ${failed && html`
        <${Alert} variant="warning"><div>
          <div class="mrd-alert-title">Updates</div>
          <div class="mrd-alert-desc">${failed}</div>
        </div><//>`}

      ${loading && !payload && !failed &&
      html`<div style=${{ display: "grid", gap: 12 }}>
        ${[0, 1, 2].map((i) => html`<${Skeleton} key=${i} style=${{ height: 96 }} />`)}
      </div>`}

      ${payload && !!payload.signals.length && !shown.length &&
      html`<${EmptyState} variant="dashed"
                          title=${only ? `Nothing for ${only} in this window` : "Nothing crossed the notification bar"}
                          description=${only
                            ? "Other watches did move. Click the chip again to see everything."
                            : "Everything in this window is worth knowing and none of it is worth interrupting you for. Show all to read it."} />`}

      ${payload && !payload.signals.length &&
      html`<${EmptyState} variant="dashed" title="Nothing new in this window"
                          description=${payload.last_crawl
                            ? "The crawl ran and nothing on your list moved. Widen the window, or add a company."
                            : "No citation has ever been fetched, so there is nothing to compare against."} />`}

      ${/* Dimmed rather than replaced while the next window loads: the previous
           answer is still true, and a page that empties itself on every click
           reads as slower than it is. */ ""}
      ${payload &&
      html`<div style=${{ display: "grid", gap: 12, opacity: loading ? 0.55 : 1,
                          transition: "opacity var(--duration-fast, .12s)" }}>
        ${shown.map((s, i) => html`
          <${SignalCard} key=${`${s.project_id}-${s.kind}-${s.label}-${i}`} signal=${s} onOpen=${onOpen} />`)}
      </div>`}

      ${/* Held back rather than hidden. These are signals the evidence gate could
           not confirm — the model asserted them and no quote stood up — and the
           console's standing rule is that a model's answer is not a fact. Counting
           them beside the confirmed ones would quietly abandon that; leaving them
           out entirely would hide work waiting for `tracker risks confirm`. */ ""}
      ${payload && !!payload.held.length &&
      html`<div class="dc-band" style=${{ paddingBottom: 0 }}>
        <button type="button" class="dc-linkish" onClick=${() => setShowHeld((v) => !v)}>
          ${showHeld ? "Hide" : "Show"} ${payload.held.length} unconfirmed —
          nobody could quote ${payload.held.length === 1 ? "it" : "them"}
        </button>
        ${showHeld &&
        html`<div style=${{ display: "grid", gap: 12, opacity: 0.75 }}>
          ${payload.held.map((s, i) => html`
            <${SignalCard} key=${`held-${s.project_id}-${s.label}-${i}`} signal=${s} onOpen=${onOpen} />`)}
        </div>`}
      </div>`}
    </div>`;
}

/* One console now, and it reads.
 *
 * There used to be two faces on one bundle: `/` for reading the dataset and
 * `/dev` for working — the queue, the runs, the command palette. The runner is
 * gone (the database is changed from the CLI, by one person, on the host), so the
 * `/dev` set went with it and `window.DC_MODE` with that.
 *
 * Help stayed and moved here. It explains tiers, tracks and confidence — what a
 * reader needs in order not to misread the data — and was only ever filed under
 * the machinery because that is where the tab happened to sit.
 *
 * Kept in step with `server.READ_VIEWS`, which decides which paths are pages
 * rather than 404s; a test asserts the two agree. */
const VIEWS = [
  ["updates", "Updates"], ["projects", "Projects"], ["sources", "Sources"],
  ["map", "Map"], ["capex", "Capex"], ["help", "Help"],
];

/* One cited article, opened.
 *
 * **The frame will be blank for a lot of publishers, and that is not a bug we can
 * fix.** `frame-src https:` is set on our side, but a site that sends
 * `X-Frame-Options: DENY` or its own `frame-ancestors` refuses, and no header of
 * ours overrides theirs. The browser gives us no reliable event for it either —
 * `onload` fires for a refusal too — so rather than guess, everything we already
 * hold about the URL sits beside the frame permanently. If the frame comes up
 * empty, that panel is the answer; if it loads, the panel is the provenance.
 *
 * `Dialog` comes from the design bundle rather than another hand-rolled overlay:
 * Escape and body-scroll locking are already in it. */
/* One citation, opened.
 *
 * **Reader view first, the live page second, and that order was measured.**
 * Across the fifteen most-cited publishers, ten refuse to be framed —
 * `X-Frame-Options` or `frame-ancestors` — and those ten carry 388 of their 689
 * citations, `datacenterdynamics.com` (the most-cited of all) among them. No
 * header of ours can override a publisher's, so a frame-first modal shows
 * "refused to connect" more often than it shows an article.
 *
 * So the first tab is what every read-later tool does with this problem: the
 * readability algorithm over the publisher's own HTML, rendered under our
 * stylesheet, with the stored quotes marked in it. Structure survives —
 * headings, paragraphs, images, links — and a live page that may have been
 * edited since it was cited is not the thing the reader wanted anyway.
 *
 * **The frame is sandboxed with no `allow-` tokens.** It loads same-origin, so
 * the document could otherwise script *our* page; `sandbox=""` gives it an
 * opaque origin and no script at all. That is the second of three guards, after
 * sanitising the HTML server-side and before the document's own
 * `default-src 'none'`. */
function ArticleModal({ article, onClose }) {
  const [pane, setPane] = useState("read");
  const url = article?.url;
  useEffect(() => { setPane("read"); }, [url]);

  if (!article) return null;
  const fields = (article.fields || "").split(",").filter(Boolean);
  const quotes = article.quotes || {};
  const dark = document.documentElement.classList.contains("dark")
    || document.documentElement.getAttribute("data-theme") === "dark";
  const readerSrc = `/api/article?url=${encodeURIComponent(article.url)}`
    + (dark ? "&theme=dark" : "");
  return html`
    <${Dialog} open=${true} onOpenChange=${(v) => !v && onClose()}>
      <${DialogContent} className="dc-article">
        <div class="dc-article-head">
          <div style=${{ minWidth: 0 }}>
            <div style=${{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style=${chip("--chart-1")}>${article.publisher}</span>
              <span style=${chip(article.source_type === "company_filing"
                || article.source_type === "government_doc" ? "--success" : "--muted-foreground")}
                >${article.source_type}</span>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12,
                              color: "var(--muted-foreground)" }}>
                ${article.published_at
                  ? "published " + String(article.published_at).slice(0, 10)
                  : "crawled " + String(article.fetched_at).slice(0, 10)}</span>
            </div>
            <a href=${article.url} target="_blank" rel="noreferrer" class="dc-article-url"
               >${article.url}</a>
          </div>
          <button type="button" class="dc-xbtn" onClick=${onClose} aria-label="Close">✕</button>
        </div>

        <div class="dc-article-tabs">
          <div class="dc-seg">
            <button type="button" class="dc-seg-btn" aria-pressed=${pane === "read"}
                    onClick=${() => setPane("read")}>Reader</button>
            <button type="button" class="dc-seg-btn" aria-pressed=${pane === "live"}
                    onClick=${() => setPane("live")}>Live page</button>
          </div>
          <span class="dc-article-via">
            ${pane === "read"
              ? "The publisher's own article, extracted and rendered here — most refuse to be framed."
              : "The publisher's live page. Ten of our fifteen most-cited refuse this; a blank panel is the site declining."}
          </span>
          <a href=${article.url} target="_blank" rel="noreferrer" class="dc-article-out"
             >open in a new tab ↗</a>
        </div>

        <div class="dc-article-body">
          ${pane === "live"
            ? html`<iframe class="dc-article-frame" src=${article.url} title=${article.url}
                           referrerpolicy="no-referrer" loading="lazy" />`
            : html`<iframe class="dc-article-frame" src=${readerSrc} sandbox=""
                           title=${"Reader view of " + article.url} />`}
          <aside class="dc-article-side">
            ${fields.length > 0 && html`
              <div>
                <div class="dc-article-h">supports</div>
                ${fields.map((f) => html`
                  <div key=${f} style=${{ marginBottom: 10 }}>
                    <span style=${chip("--success")}>${f}</span>
                    ${quotes[f] && html`
                      <p class="dc-article-quote">“${quotes[f]}”</p>`}
                  </div>`)}
              </div>`}
            ${article.excerpt && html`
              <div>
                <div class="dc-article-h">excerpt</div>
                <p class="dc-article-quote">${article.excerpt}</p>
              </div>`}
            <div>
              <div class="dc-article-h">cited by</div>
              ${article.projects.map((p) => html`
                <div key=${p.id} style=${{ fontSize: 13, marginBottom: 4 }}>
                  <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11,
                                  color: "var(--muted-foreground)" }}>#${p.id}</span>
                  ${" "}${p.name}
                </div>`)}
            </div>
          </aside>
        </div>
      <//>
    <//>`;
}

/* Every publisher, every article, and what each one actually decided.
 *
 * Both halves come from data the payload already carries: the per-publisher record
 * from `/api/publishers`, which the server has computed and shipped since `tracker
 * sources` existed and nothing rendered; and every citation from
 * `projects[].sources[]`.
 *
 * **URLs are deduplicated here, not on the server.** One article routinely cites
 * several projects — 2,758 source rows over 1,928 distinct URLs — and the page
 * wants the article once, carrying the list of projects that rest on it. */
function SourcesView({ data }) {
  const [open, setOpen] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [query, setQuery] = useState("");
  const [trust, setTrust] = useState(null);

  /* The publisher record rides on `/api/publishers` rather than `/api/dataset`,
     because the survey costs ~0.24s and the dataset is refetched after every run.
     The list below paints from data already in hand; this only fills in the
     "decided" column when it arrives. */
  useEffect(() => {
    let cancelled = false;
    api("/api/publishers")
      .then((payload) => { if (!cancelled) setTrust(payload); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const publishers = useMemo(() => {
    const host = (u) => {
      try {
        const h = new URL(u).hostname.replace(/^www\./, "");
        const parts = h.split(".");
        return parts.length > 2 ? parts.slice(-2).join(".") : h;
      } catch { return "?"; }
    };
    const byUrl = new Map();
    for (const p of data.projects || []) {
      for (const s of p.sources || []) {
        const entry = byUrl.get(s.url) || { ...s, publisher: host(s.url), projects: [] };
        entry.projects.push({ id: p.id, name: `${p.company} — ${p.name}` });
        byUrl.set(s.url, entry);
      }
    }
    const groups = new Map();
    for (const a of byUrl.values()) {
      const g = groups.get(a.publisher) || { host: a.publisher, articles: [] };
      g.articles.push(a);
      groups.set(a.publisher, g);
    }
    /* The measured record, keyed onto the same publisher identity the CLI prints.
       Absent for a host with no decisions yet, which is most of them. */
    const stats = new Map((trust?.sources?.top || []).map((h) => [h.host, h]));
    return [...groups.values()]
      .map((g) => ({ ...g, stat: stats.get(g.host) || null }))
      .sort((a, b) => (b.stat?.decisive || 0) - (a.stat?.decisive || 0)
        || b.articles.length - a.articles.length
        || a.host.localeCompare(b.host));
  }, [data.projects, trust]);

  const needle = query.trim().toLowerCase();
  const shown = needle
    ? publishers.filter((p) => p.host.includes(needle)
        || p.articles.some((a) => (a.url + (a.excerpt || "")).toLowerCase().includes(needle)))
    : publishers;
  const articles = publishers.reduce((n, p) => n + p.articles.length, 0);

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)",
                     gap: 16, padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 06 — sources" title="Everything this rests on">
        ${articles} article(s) across ${publishers.length} publisher(s). Ordered by how many
        stored values each publisher's claims actually decided — not by how often it is cited,
        which is a measure of how much we read rather than how much it was worth reading.
        Open one to read it; what it supports is listed beside it either way.
      <//>

      <${Card}>
        <div style=${{ padding: "12px 20px" }}>
          <${Input} placeholder="Filter by publisher or URL…" value=${query}
                    onInput=${(e) => setQuery(e.target.value)} />
        </div>
      <//>

      ${shown.map((p) => html`
        <${Card} key=${p.host}>
          <button type="button" class="dc-disclose"
                  aria-expanded=${expanded === p.host}
                  onClick=${() => setExpanded(expanded === p.host ? null : p.host)}>
            <span style=${{ display: "grid", gap: 3, minWidth: 0 }}>
              <span style=${{ fontSize: 15, fontWeight: 500 }}>${p.host}</span>
              <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                ${p.articles.length} article${p.articles.length === 1 ? "" : "s"}${p.stat
                  ? ` · decided ${p.stat.decisive}, ${p.stat.contested} against a rival`
                  : " · nothing decided yet"}</span>
            </span>
            <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
              ${expanded === p.host ? "hide" : "show"}</span>
          </button>
          ${expanded === p.host && html`
            <div style=${{ padding: "0 16px 12px" }}>
              ${p.articles.map((a) => html`
                <button key=${a.url} type="button" class="dc-srcrow"
                        onClick=${() => setOpen(a)}>
                  <span class="dc-num" style=${{ fontSize: 11, color: "var(--muted-foreground)" }}>
                    ${(a.published_at || a.fetched_at || "").slice(0, 10)}</span>
                  <span style=${{ minWidth: 0 }}>
                    <span class="dc-srcurl">${a.url}</span>
                    ${(a.fields || "") && html`
                      <span style=${{ display: "block", fontSize: 11,
                                      color: "var(--muted-foreground)", marginTop: 2 }}>
                        supports ${a.fields}</span>`}
                  </span>
                  <span style=${{ fontSize: 11, color: "var(--muted-foreground)",
                                  whiteSpace: "nowrap" }}>
                    ${a.projects.length} project${a.projects.length === 1 ? "" : "s"}</span>
                </button>`)}
            </div>`}
        <//>`)}

      ${shown.length === 0 && html`
        <${EmptyState} variant="dashed" title="Nothing matches"
          description="No publisher or URL contains that." />`}

      <${ArticleModal} article=${open} onClose=${() => setOpen(null)} />
    </div>`;
}

function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  /* The URL is the view. `window.DC_VIEW` is stamped into the shell by whichever
     path the server was asked for, so a deep link opens on the right page rather
     than painting the default and swapping — which reads as a flash of the wrong
     screen. */
  const [view, setView] = useState(() => window.DC_VIEW || "updates");
  const [openId, setOpenId] = useState(null);
  const [dark, setDark] = useState(false);

  const load = useCallback(() => api("/api/dataset").then((payload) => {
    if (payload.kwPerH200) H200_KW = payload.kwPerH200;
    setData(payload);
    // The vendored custom elements read `window.DCTRACKER`. They were written
    // against the mockup's shape, which the API deliberately matches, so they
    // need no adaptation — only to be told the data has arrived.
    window.DCTRACKER = payload;
    window.dispatchEvent(new Event("dctracker-ready"));
    setError(null);
    // The whole error object, not just its message: `unreachable` is what lets
    // the page say "nothing answered" instead of blaming the database. A network
    // failure (fetch rejects outright, no response at all) is unreachable too.
  }).catch((e) => setError({
    message: e.message || "the request failed before it reached the server",
    unreachable: e.unreachable === undefined ? e.name === "TypeError" : e.unreachable,
    // `status` as well, and for the same reason: the panel distinguishes a
    // console whose own modules disagree (503) from one that could not read the
    // data, and dropping the code here left it unable to tell.
    status: e.status,
  })), []);
  const goto = useCallback((key, { push = true } = {}) => {
    setView(key);
    /* `pushState`, not a real navigation: the bundle and the dataset are already
       in memory, so re-fetching either to change tab would be slower than the tab
       switch it replaces. The URL is kept honest so refresh, back and a pasted
       link all land where the reader expects. */
    if (push && window.location.pathname !== "/" + key) {
      window.history.pushState({ view: key }, "", "/" + key);
    }
  }, []);

  /* Back and forward. `push: false` so replaying history does not re-push it. */
  useEffect(() => {
    const onPop = () => goto(window.location.pathname.replace(/^\//, "") || "updates",
                             { push: false });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [goto]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { document.documentElement.classList.toggle("dark", dark); }, [dark]);

  // The table's sticky header must sit below the app header, which changes
  // height when it wraps. Measure rather than hard-code.
  useEffect(() => {
    const header = document.querySelector("header");
    if (!header) return;
    const apply = () => document.documentElement.style.setProperty(
      "--dc-header-h", `${Math.round(header.getBoundingClientRect().height)}px`);
    apply();
    const observer = new ResizeObserver(apply);
    observer.observe(header);
    return () => observer.disconnect();
  }, [data]);
  useEffect(() => { if (window.lucide?.createIcons) window.lucide.createIcons(); });

  if (error) {
    // Three different failures, and telling a reader the wrong one costs them a
    // debugging session: nothing answering at all, the server answering that its
    // own code is inconsistent, and the server failing to read the data. Only the
    // last is about the database.
    //
    // The middle case was added after it cost exactly that. A console published
    // the night before answered `/api/dataset` with "internal error"; this page
    // said "could not read the database"; the database was fine and had been read
    // on that very request. The tree had been merged underneath a running process,
    // so a module loaded at startup and one imported after the merge disagreed.
    // The server now names that case — see `Handler._stale_source`.
    const unreachable = !!error.unreachable;
    const stale = error.status === 503 && /source changed on disk/.test(error.message || "");
    return html`<div style=${{ padding: 40, maxWidth: "70ch" }}>
      <${Alert} variant=${unreachable || stale ? "warning" : "danger"}><div>
        <div class="mrd-alert-title">
          ${unreachable
            ? "The console is not answering"
            : stale
            ? "The console is running code that has since changed"
            : "The console could not read the database"}
        </div>
        <div class="mrd-alert-desc" style=${{ whiteSpace: "pre-wrap" }}>${error.message}</div>
        ${unreachable && html`
          <div class="mrd-alert-desc" style=${{ marginTop: 8 }}>
            Nothing is wrong with the data — this page just cannot reach the server. If you
            restarted it, give it a few seconds.
            ${" "}
            <button type="button" class="dc-link" onClick=${() => window.location.reload()}>
              Try again
            </button>.
          </div>`}
      </div><//>
    </div>`;
  }
  if (!data) {
    return html`<div style=${{ padding: 26, display: "grid", gap: 12 }}>
      ${[88, 72, 80, 64].map((w, i) => html`
        <${Skeleton} key=${i} className="mrd-shimmer" height=${18} width=${w + "%"} />`)}
    </div>`;
  }

  const open = openId == null ? null : data.projects.find((p) => p.id === openId);
  const t = data.totals;

  return html`
    <div style=${{ minHeight: "100vh", background: "var(--background)" }}>
      <div id="dc-page">
        <header class="dc-head" style=${{ position: "sticky", top: 0, zIndex: 40, display: "flex", flexWrap: "wrap",
          alignItems: "center", gap: "12px 20px", padding: "14px 26px",
          borderBottom: "1px solid var(--border)",
          background: "color-mix(in oklab, var(--background) 75%, transparent)",
          backdropFilter: "blur(12px)" }}>
          ${/* The lockup sits on one baseline, mark included. A block-level flex item
                has no baseline of its own, so flexbox aligns the mark by its bottom
                edge — which lands it exactly on the wordmark's baseline, and the mark
                is drawn full-height on its 24-unit grid to match a wordmark with no
                descenders. Centring the mark on the line box instead drops it 3.5px,
                which reads as a sag next to a 24px serif. */ ""}
          <div style=${{ display: "flex", alignItems: "baseline", gap: 10, flex: "none" }}>
            <${Mark} />
            <span style=${{ fontFamily: "var(--font-display)", fontSize: 24, fontWeight: 500,
                            letterSpacing: "-0.015em" }}>dc-tracker</span>
            <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12,
                            color: "var(--muted-foreground)" }}>v${data.version}</span>
          </div>

          <div class="dc-seg">
            ${VIEWS.map(([key, label]) => html`
              <button key=${key} type="button" class="dc-seg-btn" aria-pressed=${view === key}
                      onClick=${() => { goto(key); setOpenId(null); }}>${label}</button>`)}
          </div>

          <span style=${{ flex: "1 1 40px" }} />

          <div class="dc-head-actions" style=${{ display: "flex", alignItems: "center", gap: 10, flex: "none" }}>
            <span class="dc-num dc-head-counts" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
              <${Counted} value=${t.projects} /> projects · <${Counted} value=${t.states} /> states · <${Counted} value=${t.citations} /> citations
            </span>
            <${Button} size="icon" variant="outline" aria-label="Toggle theme"
                       onClick=${() => setDark((d) => !d)}>${dark ? "☀" : "☾"}<//>
            ${/* Who is reading, and the way out. `account` is null on a console
                  with no accounts at all, where there is nobody to sign out. */ ""}
            ${data.account && html`
              <span class="dc-head-counts" style=${{ fontSize: 12, color: "var(--muted-foreground)",
                                                     maxWidth: 200, overflow: "hidden",
                                                     textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    title=${data.account.email}>
                ${data.account.name || data.account.email}
              </span>
              <${Button} size="sm" variant="ghost" onClick=${async () => {
                await api("/api/logout", { method: "POST", body: {} });
                window.location.reload();
              }}>Sign out<//>`}
          </div>
        </header>

        ${view === "updates" && html`<${UpdatesView} data=${data} onOpen=${setOpenId} />`}
        ${view === "projects" && html`<${ProjectsView} data=${data} openId=${openId} onOpen=${setOpenId} />`}
        ${view === "sources" && html`<${SourcesView} data=${data} />`}
        ${view === "map" && html`<${MapView} data=${data} openId=${openId} onOpen=${setOpenId} />`}
        ${view === "capex" && html`
          <${CapexView} data=${data} allowAi=${data.allow_ai} onOpen=${setOpenId} />`}
        ${view === "help" && html`<${HelpView} data=${data} />`}
      </div>

      <${Drawer} data=${data} project=${open} onClose=${() => setOpenId(null)} />
    </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
