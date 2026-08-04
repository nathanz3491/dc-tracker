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

import { parseAnsi } from "/static/ansi.js";
import { HelpView } from "/static/views-help.js";

const html = htm.bind(React.createElement);
const { useState, useEffect, useMemo, useRef, useCallback } = React;
const NS = window.MeridianDesignSystem_6e9015 || {};
const {
  Button, Card, CardHeader, CardTitle, CardDescription, Input, Select, Switch,
  Table, TableHeader, TableBody, TableRow, TableHead, Tabs, TabsList, TabsTrigger,
  StatCard, EmptyState, Skeleton, Glyph, Badge, Alert,
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
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { error: text }; }
  // The body travels with the error. A refusal sometimes carries more than a
  // sentence — the confirmation word for a command, say — and the caller cannot
  // get at it if only the message survives.
  if (!res.ok) {
    throw Object.assign(new Error(payload?.error || res.statusText),
                        { status: res.status, payload });
  }
  return payload;
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

/* One log line, with its ANSI colour intact.
 *
 * Every run is a React child, so the text is escaped on the way in — log lines
 * carry URLs and headlines fetched from the open web, and hand-built markup is
 * how one of those becomes a script tag. */
const LogLine = React.memo(({ line }) => html`
  <span class="dc-log-line">
    ${parseAnsi(line).map((run, i) => html`<span key=${i} style=${run.style}>${run.text}</span>`)}
  </span>`);

/* The run log.
 *
 * Does not wrap, and scrolls sideways instead — see `.dc-log` in app.css for
 * why a Rich table cannot survive being reflowed. The toggle exists because the
 * other half of the output is prose: `gaps` notes and refusal messages are
 * sentences Rich has already wrapped at 160 columns, and reading those by
 * scrolling is worse than reading them wrapped. Tables are the default because
 * they are the case that breaks rather than merely inconveniences. */
function LogPane({ lines, innerRef }) {
  const [wrap, setWrap] = useState(false);
  return html`
    <div style=${{ position: "relative" }}>
      ${lines.length > 0 && html`
        <button type="button" class="dc-log-wrap" aria-pressed=${wrap}
                title=${wrap ? "Lines are wrapped; tables will not line up"
                             : "Lines are not wrapped, so tables line up. Scroll sideways to read them."}
                onClick=${() => setWrap((w) => !w)}>${wrap ? "wrap on" : "wrap off"}</button>`}
      <pre class=${`dc-log${lines.length > 0 ? " dc-log--framed" : ""}${wrap ? " dc-log--wrap" : ""}`}
           ref=${innerRef} aria-live="polite" aria-atomic="false">
        ${lines.length === 0
          ? "connecting…"
          : lines.map((line, i) => html`<${LogLine} key=${i} line=${line} />`)}
      </pre>
    </div>`;
}

function Eyebrow({ figure, title, children }) {
  return html`
    <div style=${{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                      letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>${figure}</span>
      <h1 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                    letterSpacing: "-0.02em", lineHeight: 1.15 }}>${title}</h1>
      ${children && html`<p class="dc-intro" style=${{ margin: "2px 0 0", fontSize: 14, lineHeight: "22px",
                                      color: "var(--muted-foreground)", maxWidth: "78ch" }}>${children}</p>`}
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
  return html`
    <span class=${`dc-v dc-v--${na ? "na" : tier}`} style=${extra}
          onMouseEnter=${(e) => onQuote(e, project, field)}
          onMouseLeave=${() => onQuote(null, project, field, { hover: true })}
          onClick=${(e) => { e.stopPropagation(); onQuote(e, project, field, { sticky: true }); }}
          >${text ?? fmt(field, project[field])}</span>`;
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
        ${narrow && html`
          <div style=${{ display: "grid", gap: 10, padding: "14px 16px" }}>
            <${Input} size="sm" placeholder="name, city, operator…" value=${f.q}
                      onChange=${(e) => set("q")(e.target.value)} />
            <div style=${{ display: "flex", gap: 10, alignItems: "center" }}>
              <${Button} size="sm" variant="outline" style=${{ flex: 1 }}
                onClick=${() => setFiltersOpen((o) => !o)}>
                ${filtersOpen ? "Hide filters" : `Filters${activeFilters ? ` (${activeFilters})` : ""}`}
              <//>
              ${!clean && html`<${Button} size="sm" variant="ghost"
                onClick=${() => setF(BLANK_FILTERS)}>Clear<//>`}
            </div>
          </div>`}
        <div style=${{ display: narrow && !filtersOpen ? "none" : "grid",
                       gridTemplateColumns: "repeat(auto-fit, minmax(148px, 1fr))",
                       gap: "12px 14px", padding: "16px 20px" }}>
          ${field("f-q", "search", html`<${Input} id="f-q" size="sm" placeholder="name, city, operator…"
             value=${f.q} onChange=${(e) => set("q")(e.target.value)} />`)}
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
        <div style=${{ display: narrow && !filtersOpen ? "none" : "flex", flexWrap: "wrap",
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

      ${/* Real percentages, straight from gaps.measure(), against the denominators
           it chose. Deliberately shows the weak end as well as the strong one: a
           coverage strip that only reported its best numbers would be the exact
           thing gaps.py exists to avoid. */ ""}
      <${CoverageStrip} data=${data} />

      <div style=${{ display: "flex", flexWrap: "wrap", alignItems: "baseline",
                     justifyContent: "space-between", gap: "10px 18px" }}>
        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
          ${rows.length} of ${data.projects.length} projects · sorted by ${sort.key} ${sort.dir}
        </span>
        <div class="dc-legend" style=${{ display: "flex", flexWrap: "wrap", gap: "6px 16px", fontSize: 12,
                       color: "var(--muted-foreground)" }}>
          ${["reported", "derived", "unconfirmed", "inferred", "defaulted", "missing", "na"].map((t) => html`
            <span key=${t} style=${{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              ${/* cursor:default — the swatch is a key, not something to hover for a quote */ ""}
              <span class=${`dc-v dc-v--${t}`} style=${{ fontSize: 12, cursor: "default" }}>
                ${t === "missing" ? "—" : "abc"}</span>
              ${TIER[t][0]}
            </span>`)}
        </div>
      </div>

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
  const [quote, showQuote] = useQuote();
  const closeRef = useRef(null);

  useEffect(() => { setTab("stats"); }, [project?.id]);
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
  const blocks = p.blocks || [];
  // Only offered when there are blocks. An empty tab on 88% of the database would
  // read as "this campus has one tranche", which is the opposite of what a missing
  // backfill means.
  const tabs = [
    ["stats", "Stats", ""],
    ...(blocks.length ? [["blocks", "Blocks", ` ${blocks.length}`]] : []),
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
                                                 open=${open} onQuote=${showQuote}
                                                 allowWrite=${data.allow_write} />`}
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
function InsightPanel({ project, allowWrite }) {
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
    if (!allowWrite) { setState({ status: "unavailable", text: "" }); return; }
    ask();
    return () => abort.current?.abort();
  }, [project.id, allowWrite, ask]);

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
        <p class="dc-ai-quiet">Unavailable on a read-only console.</p>`}

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

function StatsTab({ data, p, populated, open, onQuote, allowWrite }) {
  const worst = open.slice().sort((a, b) => SEV_ORDER.indexOf(b.severity) - SEV_ORDER.indexOf(a.severity))[0];
  const stats = [
    { label: "Planned capacity", value: p.mw_planned == null ? "—" : p.mw_planned.toLocaleString() + " MW",
      hint: p.mw_planned == null ? "no source cited one" : TIER[tierOf(p, "mw_planned")][0] },
    { label: "Built to date", value: p.mw_built == null ? "—" : p.mw_built.toLocaleString() + " MW",
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
      <${InsightPanel} project=${p} allowWrite=${allowWrite} />

      <div style=${{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(330px, 1fr))",
                     gap: 18, alignItems: "start" }}>
        <${Card}>
          <${CardHeader}>
            <${CardTitle}>The twelve tracked fields<//>
            <${CardDescription}>${populated} of 12 populated<//>
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
                  </div>
                </div>`;
            })}
          </div>
        <//>

        <div style=${{ display: "grid", gap: 18 }}>
          ${window.customElements?.get("dc-campus") && html`
            <${Card}>
              <${CardHeader}>
                <${CardTitle}>Campus schematic<//>
                <${CardDescription}>A diagram built from this row's own numbers, not a rendering of the
                  site. Drag to orbit.<//>
              <//>
              <div style=${{ height: 280, borderTop: "1px solid var(--border)", background: "var(--muted)",
                             borderRadius: "0 0 20px 20px", overflow: "hidden" }}>
                <dc-campus project=${String(p.id)} />
              </div>
            <//>`}

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
                const status = t.complete ? (t.blockers.length ? "complete, still obstructed" : "complete")
                  : t.blockers.length ? "blocked" : t.reached.length === 0 ? "not started" : "in progress";
                return html`
                  <div key=${t.track} style=${{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 90px",
                       gap: 12, alignItems: "center", padding: "11px 20px", borderTop: "1px solid var(--border)" }}>
                    <div style=${{ display: "grid", gap: 3, minWidth: 0 }}>
                      <span style=${{ fontSize: 14, fontWeight: 600 }}>${label(t.track)}</span>
                      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12,
                        color: t.blockers.length ? "var(--danger)" : t.complete ? "var(--success)" : "var(--muted-foreground)" }}>
                        ${status}${onlyImplied ? " (implied)" : ""} — ${t.next_milestone ? "watch for " + t.next_milestone
                          : t.blockers.length ? "an open obstacle still sits here" : "nothing outstanding"}
                      </span>
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

      <${Card}>
        <${CardHeader}>
          <${CardTitle}>${p.events.length} milestone event${p.events.length === 1 ? "" : "s"}<//>
          <${CardDescription}>Milestones as reported, each pointing at the source that stated it.<//>
        <//>
        <div style=${{ display: "grid", gap: 0 }}>
          ${p.events.map((e, i) => html`
            <div key=${i} style=${{ display: "grid", gridTemplateColumns: "96px 168px minmax(0,1fr)", gap: 14,
                 alignItems: "baseline", padding: "10px 20px", borderTop: "1px solid var(--border)" }}>
              <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>${e.event_date}</span>
              <span style=${chip(e.event_type === "delayed" ? "--danger"
                : e.event_type === "expanded" ? "--chart-5" : "--muted-foreground")}>${e.event_type}</span>
              <span style=${{ fontSize: 14, lineHeight: "20px" }}>${e.description}</span>
            </div>`)}
        </div>
        ${p.events.length === 0 && html`
          <div style=${{ padding: "4px 20px 20px" }}>
            <${EmptyState} variant="dashed" size="sm" title="No milestones recorded"
              description="Nothing read so far states one. Silence is not evidence — a milestone appears when a source reports it." />
          </div>`}
      <//>
    </div>`;
}

//: Block status to a colour token. `serving` and `energized` are the two that mean
//: megawatts are actually delivering, so they share the success colour.
const BLOCK_TONE = {
  serving: "--success",
  energized: "--success",
  shell_complete: "--chart-1",
  under_construction: "--warning",
  permitting: "--chart-5",
  planned: "--muted-foreground",
  paused: "--danger",
  cancelled: "--danger",
};

function BlocksTab({ p }) {
  const blocks = p.blocks || [];
  const counted = blocks.filter((b) => b.mw_counted && b.mw != null);
  const uncited = blocks.filter((b) => !b.mw_counted && b.mw != null);
  const live = blocks.filter((b) => b.status === "serving" || b.status === "energized");
  const customers = [...new Set(blocks.map((b) => b.customer).filter(Boolean))];

  return html`
    <div style=${{ display: "grid", gap: 14 }}>
      <p style=${{ margin: 0, fontSize: 14, lineHeight: "22px", color: "var(--muted-foreground)", maxWidth: "88ch" }}>
        A campus is rarely one thing. Each tranche carries its own state, customer and dates, so this
        project can say it is ${live.length
          ? `${live.length} tranche${live.length === 1 ? "" : "s"} already running `
          : "not yet running "}beside capacity still being built — which the single phase and
        capacity above can only summarise.
      </p>

      ${customers.length > 1 && html`
        <${Card} style=${{ padding: "12px 16px" }}>
          <span style=${{ fontSize: 12, textTransform: "uppercase", letterSpacing: "0.08em",
                          color: "var(--muted-foreground)" }}>this campus serves ${customers.length} customers</span>
          <div style=${{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
            ${customers.map((c, i) => html`<span key=${i} style=${chip("--chart-1")}>${c}</span>`)}
          </div>
        <//>`}

      <div style=${{ display: "grid", gap: 0 }}>
        ${blocks.map((b, i) => html`
          <div key=${i} style=${{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 92px 150px 132px",
               gap: 12, alignItems: "baseline", padding: "12px 4px", borderTop: "1px solid var(--border)" }}>
            <div style=${{ minWidth: 0 }}>
              <div style=${{ fontSize: 14, fontWeight: 500 }}>
                ${b.parent ? html`<span style=${{ color: "var(--muted-foreground)" }}>${b.parent} / </span>` : null}${b.label}
              </div>
              ${b.generic && !b.parent && html`
                <div style=${{ fontSize: 12, color: "var(--muted-foreground)", marginTop: 2 }}>
                  names a phase but not of which facility</div>`}
            </div>
            <span class="dc-num" style=${{ fontSize: 13, textAlign: "right",
                  color: b.mw == null ? "var(--muted-foreground)" : b.mw_counted ? "inherit" : "var(--danger)" }}>
              ${b.mw == null ? "—" : `${b.mw} MW`}</span>
            <span style=${chip(BLOCK_TONE[b.status] || "--muted-foreground")}>${b.status}</span>
            <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
              ${b.customer || "—"}${b.energized_on ? ` · live ${b.energized_on}`
                : b.expected_online ? ` · ${b.expected_online}` : ""}</span>
          </div>`)}
      </div>

      ${uncited.length > 0 && html`
        <${Card} style=${{ padding: "12px 16px",
              borderColor: "color-mix(in oklab, var(--danger) 34%, var(--border))" }}>
          <p style=${{ margin: 0, fontSize: 13, lineHeight: "20px" }}>
            <strong>${uncited.length} tranche${uncited.length === 1 ? "" : "s"} carr${uncited.length === 1 ? "ies" : "y"}
            ${" "}a capacity no quote confirms (待确认).</strong>
            ${" "}Shown above, and deliberately left out of the campus total — so
            ${" "}${counted.length ? "the tranche figures will not add up to" : "there is no"} MW planned.
            A figure nobody stated is not a fact, and summing it would make the total read as cited.
          </p>
        <//>`}
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
function CrawlButton({ url, disabled, onStarted }) {
  const [state, setState] = useState("idle"); // idle | confirming | running
  const [error, setError] = useState(null);

  useEffect(() => {
    if (state !== "confirming") return;
    // Arming and then walking away should disarm, not stay hot.
    const timer = setTimeout(() => setState("idle"), 6000);
    return () => clearTimeout(timer);
  }, [state]);

  const run = async () => {
    setState("running");
    setError(null);
    try {
      const res = await api("/api/run", {
        method: "POST",
        body: { cmd: "ingest crawl", flags: { "--url": url }, confirm: "ingest crawl" },
      });
      onStarted(res.run.id);
    } catch (e) {
      setError(e.message);
      setState("idle");
    }
  };

  if (error) {
    return html`<span style=${{ fontSize: 11, color: "var(--danger)", maxWidth: 180 }}>${error}</span>`;
  }
  if (state === "confirming") {
    return html`
      <div style=${{ display: "flex", gap: 6, alignItems: "center" }}>
        <span style=${{ fontSize: 11, color: "var(--warning)", whiteSpace: "nowrap" }}>1 LLM call</span>
        <${Button} size="sm" onClick=${run}>Confirm<//>
      </div>`;
  }
  return html`
    <${Button} size="sm" variant="outline" disabled=${disabled || state === "running"}
               loading=${state === "running"} onClick=${() => setState("confirming")}>
      Crawl
    <//>`;
}

function QueueView({ data, onRan, allowWrite, busy }) {
  const { queue, failed } = data;
  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 04 — queue" title="Articles found, not yet read">
        Nothing here has cost anything yet. ${html`<b>Crawl</b>`} reads one article for one LLM call.
        Start with the ones tagged ${html`<b>deepens</b>`} — they add detail to a project we already
        track. The list over-collects on purpose: a tighter filter starts dropping real projects.
      <//>

      <${Card}>
        <div style=${{ padding: "16px 20px 8px" }}>
          <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
            ${queue.length} queued candidate${queue.length === 1 ? "" : "s"}
          </span>
        </div>
        <div style=${{ display: "grid", gap: 0 }}>
          ${queue.map((c) => html`
            <div key=${c.url} class="dc-queue-row"
                 style=${{ padding: "11px 20px", borderTop: "1px solid var(--border)" }}>
              <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                ${(c.published_at || "—").slice(0, 10)}</span>
              <div style=${{ minWidth: 0, display: "grid", gap: 3 }}>
                <a href=${c.url} target="_blank" rel="noreferrer"
                   style=${{ fontSize: 14, fontWeight: 500 }}>${c.title || c.url}</a>
                <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)",
                                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${c.url}</span>
              </div>
              <div style=${{ display: "flex", gap: 6, justifyContent: "flex-end",
                             alignItems: "center", flexWrap: "wrap" }}>
                ${c.depth && html`<span style=${chip("--success")}>deepens</span>`}
                <span style=${chip("--muted-foreground")}>${c.feed || "manual"}</span>
                ${allowWrite && html`
                  <${CrawlButton} url=${c.url} disabled=${busy} onStarted=${onRan} />`}
              </div>
            </div>`)}
        </div>
        ${queue.length === 0 && html`
          <div style=${{ padding: "4px 20px 20px" }}>
            <${EmptyState} variant="dashed" title="The queue is empty"
              description="Run tracker discover from the Commands view to poll the feeds. Nothing it queues costs anything until you crawl it." />
          </div>`}
      <//>

      <${Card}>
        <${CardHeader}>
          <${CardTitle}>Hosts that would not answer<//>
          <${CardDescription}>Grouped by site, because it is nearly always the site blocking us rather
            than the article. Worth checking: a run can look like it cleared the queue while a dozen
            articles sit here unread.<//>
        <//>
        <div style=${{ display: "grid", gap: 0 }}>
          ${failed.map((h) => html`
            <div key=${h.host} style=${{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 70px 60px",
                 gap: 14, alignItems: "center", padding: "11px 20px", borderTop: "1px solid var(--border)" }}>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13, overflow: "hidden",
                              textOverflow: "ellipsis" }}>${h.host}</span>
              <span style=${chip(h.http_status === 403 ? "--danger" : "--warning")}>${h.http_status || "?"}</span>
              <span class="dc-num" style=${{ fontSize: 12, textAlign: "right",
                                             color: "var(--muted-foreground)" }}>${h.count}</span>
            </div>`)}
        </div>
        ${failed.length === 0 && html`
          <div style=${{ padding: "4px 20px 20px" }}>
            <${EmptyState} variant="dashed" size="sm" title="Nothing failed to fetch" description="Every URL a run has tried was readable." />
          </div>`}
      <//>
    </div>`;
}

function GapsView({ data }) {
  const { fields, worst } = data.gaps;
  const req = data.required;
  const met = req.entries.filter((e) => e.met).length;
  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 05 — coverage" title="What is missing, and what only looks missing">
        Each field is scored against the projects where it could apply, not against all
        ${" "}${data.projects.length}. Built capacity is empty on a project that has not broken ground —
        that is the right answer, not a hole. Fields where an empty value tells you nothing show
        ${" "}${html`<b>n/a</b>`} instead of a bad score.
      <//>

      <${Card}>
        <div style=${{ display: "grid", gap: 0 }}>
          ${fields.map((g) => html`
            <div key=${g.field} class="dc-gap-row" style=${{ display: "grid", gridTemplateColumns: "150px 64px minmax(0,1fr)",
                 gap: 14, alignItems: "center", padding: "10px 20px", borderTop: "1px solid var(--border)" }}>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13,
                fontWeight: worst.includes(g.field) ? 600 : 400 }}>${g.field}</span>
              <span class="dc-num" style=${{ fontSize: 13, textAlign: "right",
                color: !g.measurable ? "var(--muted-foreground)" : g.pct >= 90 ? "var(--success)"
                  : g.pct >= 50 ? "var(--foreground)" : "var(--warning)" }}>
                ${g.measurable ? g.pct + "%" : "n/a"}</span>
              <div style=${{ display: "grid", gap: 4, minWidth: 0 }}>
                ${g.measurable && html`
                  <div style=${{ height: 6, borderRadius: 999, background: "var(--muted)", overflow: "hidden" }}>
                    <div style=${{ height: "100%", width: `${g.pct}%`, borderRadius: 999,
                                   background: worst.includes(g.field) ? "var(--warning)" : "var(--primary)" }} />
                  </div>`}
                <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                  ${g.measurable ? `${g.filled} of ${g.applicable}` : "absence carries no information"}
                  ${g.note ? " — " + g.note : ""}</span>
              </div>
            </div>`)}
        </div>
      <//>

      <${Card}>
        <${CardHeader}>
          <${CardTitle}>Required project list — ${met} of ${req.entries.length} present<//>
          <${CardDescription}>If you have a list of projects you need covered, paste it into
            ${req.path} — one per line — and this becomes a checklist instead of a guess.<//>
        <//>
        <div style=${{ display: "grid", gap: 0 }}>
          ${req.entries.map((e) => html`
            <div key=${e.entry} style=${{ display: "flex", alignItems: "center", gap: 12,
                 padding: "9px 20px", borderTop: "1px solid var(--border)" }}>
              <span style=${chip(e.met ? "--success" : "--danger")}>${e.met ? "#" + e.id : "missing"}</span>
              <span style=${{ fontSize: 14 }}>${e.entry}</span>
            </div>`)}
        </div>
        ${req.entries.length === 0 && html`
          <div style=${{ padding: "4px 20px 20px" }}>
            <${EmptyState} variant="dashed" size="sm" title="No required list yet"
              description=${`Create ${req.path} with one project per line — "Company | Project name", or just a name — to get a present/missing breakdown.`} />
          </div>`}
      <//>

      <${Card}>
        <${CardHeader}>
          <${CardTitle}>Capacity behind an obstacle<//>
          <${CardDescription}>How many megawatts are stuck behind each kind of problem. A project with
            two problems appears twice, so these bars do not add up to a total. Projects with a problem
            but no known size are counted off to the side rather than as zero.<//>
        <//>
        <div style=${{ display: "grid", gap: 0 }}>
          ${data.exposure.map((x) => {
            const max = Math.max(...data.exposure.map((e) => e.mw), 1);
            return html`
              <div key=${x.category} class="dc-exposure-row" style=${{ display: "grid", gridTemplateColumns: "170px minmax(0,1fr) 110px",
                   gap: 14, alignItems: "center", padding: "10px 20px", borderTop: "1px solid var(--border)" }}>
                <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>${x.category}</span>
                <div style=${{ display: "flex", height: 10, borderRadius: 999, overflow: "hidden",
                               background: "var(--muted)", width: `${Math.max(3, (x.mw / max) * 100)}%` }}>
                  ${SEV_ORDER.map((sev) => x.by_severity[sev] > 0 && html`
                    <div key=${sev} style=${{ background: `var(${SEV_TOKEN[sev]})`,
                      width: `${(x.by_severity[sev] / x.mw) * 100}%` }} />`)}
                </div>
                <span class="dc-num" style=${{ fontSize: 12, textAlign: "right", color: "var(--muted-foreground)" }}>
                  ${Math.round(x.mw).toLocaleString()} MW${x.no_mw ? ` +${x.no_mw} uncited` : ""}</span>
              </div>`;
          })}
        </div>
        ${data.exposure.length === 0 && html`
          <div style=${{ padding: "4px 20px 20px" }}>
            <${EmptyState} variant="dashed" size="sm" title="No open obstacle in the database"
              description="Nothing read so far reports one." />
          </div>`}
      <//>
    </div>`;
}

/* ---- Capex --------------------------------------------------------------- */

/* One group of rows that are probably one campus, and the merge that folds them.
 *
 * This is the one screen where a browser genuinely beats the CLI. Deciding which
 * of four rows survives is a judgement made by eye — you want their capacity,
 * their citations and their dates side by side, not four `tracker show` calls.
 *
 * Which id you keep does not decide the values: `merge` moves every citation onto
 * the survivor and then recomputes each field from the combined set, so the only
 * thing the choice picks is a row number. Said on the card, because it is exactly
 * the thing an operator will otherwise agonise over.
 */
function DuplicateGroup({ ids, byId, allowWrite, busy, onRan }) {
  const [keep, setKeep] = useState(ids[0]);
  const [state, setState] = useState("idle"); // idle | confirming | running
  const [typed, setTyped] = useState("");
  const [error, setError] = useState(null);
  const fold = ids.filter((i) => i !== keep);
  const rows = ids.map((id) => byId[id]).filter(Boolean);

  const merge = async () => {
    setState("running");
    setError(null);
    try {
      const res = await api("/api/run", {
        method: "POST",
        body: { cmd: "merge", flags: { "--into": keep, dupe_ids: fold }, confirm: "merge" },
      });
      onRan(res.run.id);
    } catch (e) {
      setError(e.message);
      setState("idle");
    }
  };

  // A row missing from `byId` means the dataset is older than the group — which
  // happens for exactly one render after a merge, before the reload lands.
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
      </div>

      <div style=${{ display: "grid", gap: 0 }}>
        ${rows.map((p) => html`
          <label key=${p.id} class="dc-dupe-row"
                 style=${{ cursor: allowWrite ? "pointer" : "default",
                           background: p.id === keep ? "color-mix(in oklab, var(--success) 8%, transparent)" : "transparent" }}>
            <input type="radio" name=${`keep-${ids.join("-")}`} checked=${p.id === keep}
                   disabled=${!allowWrite || state === "running"}
                   onChange=${() => { setKeep(p.id); setState("idle"); setTyped(""); }}
                   aria-label=${`Keep #${p.id}, ${p.company} ${p.name}`} />
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
          </label>`)}
      </div>

      ${error && html`
        <span style=${{ fontSize: 12, color: "var(--danger)" }}>${error}</span>`}

      ${allowWrite && (state === "confirming"
        ? html`
          <div style=${{ display: "grid", gap: 8, padding: "10px 12px", borderRadius: 10,
                         border: "1px solid color-mix(in oklab, var(--danger) 40%, var(--border))",
                         background: "color-mix(in oklab, var(--danger) 6%, transparent)" }}>
            <span style=${{ fontSize: 13 }}>
              Keeps ${html`<b class="dc-num">#${keep}</b>`} and permanently deletes
              ${" "}${html`<b class="dc-num">${fold.map((i) => "#" + i).join(", ")}</b>`}.
              Their citations, milestones and obstacles move across first, and every field on
              ${" "}#${keep} is then recomputed from the combined set. There is no undo.
            </span>
            <div style=${{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              ${/* Wrapped rather than styled: Input spreads unknown props onto the
                    inner <input>, so a width there would leave the field shell
                    full-bleed around a narrow box. */ ""}
              <div style=${{ width: 140 }}>
                <${Input} size="sm" value=${typed} placeholder="merge"
                          aria-label="Type merge to confirm"
                          onChange=${(e) => setTyped(e.target.value)} />
              </div>
              <${Button} size="sm" variant="danger" disabled=${typed.trim() !== "merge" || busy}
                         onClick=${merge}>Merge<//>
              <${Button} size="sm" variant="ghost"
                         onClick=${() => { setState("idle"); setTyped(""); }}>Cancel<//>
            </div>
          </div>`
        : html`
          <div>
            <${Button} size="sm" variant="outline" disabled=${busy || state === "running"}
                       loading=${state === "running"} onClick=${() => setState("confirming")}>
              Merge ${fold.length} into #${keep}
            <//>
          </div>`)}
    </div>`;
}

function CapexView({ data, allowWrite, busy, onRan }) {
  const capex = data.capex;
  const cover = capex.coverage;
  const dupes = capex.duplicates;
  const byId = useMemo(
    () => Object.fromEntries(data.projects.map((p) => [p.id, p])), [data.projects]);
  const reviewRef = useRef(null);

  // Only the next few periods. An expected-online date already in the past is a
  // data-quality signal rather than a pipeline, and giving it a column would put
  // it in the same row of numbers as capacity that is genuinely coming.
  const [grain, setGrain] = useState("year");
  const buckets = grain === "quarter"
    ? (capex.quarters || []).filter((q) => q >= capex.as_of_quarter).slice(0, 6)
    : capex.years.filter((y) => y >= capex.as_of_year).slice(0, 4).map(String);
  const bucketOf = (p, b) =>
    grain === "quarter" ? (p.mw_by_quarter || {})[b] : p.mw_by_year[b];
  const attributed = Math.round((cover.attributed_pct / 100) * cover.projects);

  const num = (v, suffix = "") =>
    v ? Math.round(v).toLocaleString() + suffix : html`<span class="dc-v dc-v--missing">—</span>`;

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 03 — capex" title="Who is actually paying for it">
        Meta builds its own campuses. OpenAI mostly rents from developers like Crusoe. So the company on
        the building is often not the one paying for the compute — this page groups by whoever pays.
        ${" "}${html`<b>*</b>`} means we worked the buyer out from who owns the site, because no source
        named a tenant.
      <//>

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

      ${dupes.groups.length > 0 && html`
        <${Alert} variant="warning">
          <div>
            <div class="mrd-alert-title">
              ${dupes.groups.length === 1
                ? "One campus is stored more than once"
                : `${dupes.groups.length} campuses are stored more than once`}, holding
              ${" "}${Math.round(dupes.double_counted_mw).toLocaleString()} MW counted twice
            </div>
            <div class="mrd-alert-desc">
              One campus usually has a builder, a landowner and an occupier, and each name a source
              picks becomes its own row. On the Projects page that is just untidy. Here it inflates a
              buyer's total, because the same megawatts get added once per row.
              ${" "}
              <button type="button" class="dc-link"
                      onClick=${() => reviewRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })}>
                Review them below
              </button>.
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
            <${TableHeader}><${TableRow}>
              <${TableHead}>buyer<//>
              <${TableHead} align="right">proj<//>
              <${TableHead} align="right">MW planned<//>
              <${TableHead} align="right">MW built<//>
              <${TableHead} align="right">investment<//>
              ${buckets.map((b) => html`<${TableHead} key=${b} align="right">MW ${b}<//>`)}
              <${TableHead} align="right">at risk<//>
              <${TableHead} align="right">slipped<//>
              <${TableHead}>worst obstacle<//>
            <//><//>
            <${TableBody}>
              ${capex.positions.map((p, i) => {
                const residual = p.key === "";
                return html`
                  <tr key=${p.key || "__none"} class=${i < 12 ? "dc-enter" : undefined}
                      style=${{ ...(i < 12 ? { "--i": i } : {}),
                                ...(residual ? { color: "var(--muted-foreground)" } : {}) }}>
                    <td class="dc-cell dc-cell--wide">
                      <span style=${{ fontWeight: residual ? 400 : 600, fontSize: 13 }}>${p.customer}</span>
                      ${p.self_built > 0 && html`
                        <span title=${`${p.self_built} of ${p.projects} attributed from ownership rather than a cited tenant`}
                              style=${{ color: "var(--muted-foreground)" }}>
                          ${p.self_built === p.projects ? " *" : ` (${p.self_built}*)`}</span>`}
                    </td>
                    <td class="dc-num" style=${{ textAlign: "right" }}>${p.projects}</td>
                    <td class="dc-num" style=${{ textAlign: "right", fontWeight: 600 }}>${num(p.mw_planned)}</td>
                    <td class="dc-num" style=${{ textAlign: "right" }}>${num(p.mw_built)}</td>
                    <td class="dc-num" style=${{ textAlign: "right" }}>
                      ${p.investment_usd ? fmtUSD(p.investment_usd) : html`<span class="dc-v dc-v--missing">—</span>`}</td>
                    ${buckets.map((b) => html`
                      <td key=${b} class="dc-num" style=${{ textAlign: "right" }}>
                        ${num(bucketOf(p, b))}</td>`)}
                    <td class="dc-num" style=${{ textAlign: "right",
                          color: p.mw_at_risk ? "var(--warning)" : undefined }}>${num(p.mw_at_risk)}</td>
                    <td class="dc-num" style=${{ textAlign: "right" }}>${p.slipped || ""}</td>
                    <td style=${{ fontSize: 12, whiteSpace: "nowrap" }}>
                      ${p.worst_open_risk
                        ? html`<span style=${chip(p.worst_open_risk.endsWith("blocking") ? "--danger" : "--warning")}>
                            ${p.worst_open_risk}</span>`
                        : ""}</td>
                  </tr>`;
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
          ${/* The one column where "a floor" is the wrong warning: it can be far
                too high, and saying only that it is a lower bound would be the
                opposite of honest about it. */ ""}
          <span>
            ${html`<b>Trust the megawatts before the dollars.</b>`} The investment column adds up
            everything each project carries, including figures we could not confirm — usually because a
            headline number for a whole programme ("OpenAI's $500 billion Stargate") got attached to one
            site. Duplicate rows then add it again. Open a project to see which of its numbers are
            flagged.
          </span>
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
            <${CardDescription}>Tick the row to keep; the others fold into it and their sources come
              along. ${html`<b>It does not matter which one you pick</b>`} — every value is recalculated
              from the combined sources afterwards, so you are only choosing a row number. Nothing merges
              on its own, because a wrong merge is hard to spot and cannot be undone.<//>
          <//>
          ${!allowWrite && html`
            <div style=${{ padding: "0 20px 14px" }}>
              <${Alert} variant="warning"><div><div class="mrd-alert-desc">
                Started with --no-run, so the merge button is unavailable. The groups below are still
                the ones <b class="dc-num">tracker duplicates</b> would print.
              </div></div><//>
            </div>`}
          ${dupes.groups.map((ids) => html`
            <${DuplicateGroup} key=${ids.join("-")} ids=${ids} byId=${byId}
                               allowWrite=${allowWrite} busy=${busy} onRan=${onRan} />`)}
          ${dupes.groups.length === 0 && html`
            <div style=${{ padding: "4px 20px 20px" }}>
              <${EmptyState} variant="dashed" size="sm" title="No suspected duplicates"
                description="No two rows in one locality look like the same site." />
            </div>`}
        <//>
      </div>
    </div>`;
}

/* ---- Commands and runs --------------------------------------------------- */

/* Flags, rendered for someone who has never seen a command line.
 *
 * The catalog is read from Typer, so the names in it are the CLI's: `--max-articles`,
 * `project_ids`. Those are precise and completely opaque if you have not used the
 * terminal, so each control gets a plain-language label and keeps the real flag
 * underneath it — the argv preview below the form is still the honest record of
 * what will run, and matching the two up is how someone graduates to the CLI. */
const PROJECT_FIELDS = new Set(
  ["project_id", "project_ids", "dupe_ids", "--into", "--verify", "--unverify"]);

const humanize = (name) => {
  const bare = name.replace(/^--/, "").replace(/_/g, "-");
  return bare.charAt(0).toUpperCase() + bare.slice(1).replace(/-/g, " ");
};

/* Presets around the CLI's own default, rather than an empty number box.
 *
 * Every one of these is a budget — articles to read, rows to print, projects to
 * select — and the question is nearly always "the usual, less than usual, or
 * more". A blank box makes that a research task; four options makes it a choice.
 * Custom stays, because the person who knows they want 37 should get 37. */
function NumberChoice({ flag, value, onChange }) {
  const [custom, setCustom] = useState(false);
  const base = typeof flag.d === "number" ? flag.d : null;
  const presets = base == null ? []
    : base === 0 ? [0, 1, 3, 5, 10]
    : [...new Set([Math.max(1, Math.round(base / 2)), base, base * 2, base * 5])].sort((a, b) => a - b);

  if (custom || !presets.length) {
    return html`<${Input} size="sm" type="number" value=${value ?? ""}
                  placeholder=${base == null ? "" : String(base)}
                  onChange=${(e) => onChange(e.target.value)} />`;
  }
  return html`
    <${Select} size="sm" value=${value ?? ""}
      onChange=${(e) => {
        if (e.target.value === "__custom") { setCustom(true); onChange(""); }
        else onChange(e.target.value);
      }}>
      <option value="">${base} (default)</option>
      ${presets.filter((n) => n !== base).map((n) => html`<option key=${n} value=${n}>${n}</option>`)}
      <option value="__custom">Custom…</option>
    <//>`;
}

/* A project picker instead of "type the id you memorised".
 *
 * This is the single biggest barrier in the old form: half the useful commands
 * take a project id, and the only way to learn one was to run `list` in another
 * tab and read it off. The dataset is already loaded on the page.
 *
 * **The multi-value case is add-one-at-a-time, not a multi-select.** It was a
 * native `<select multiple>` holding 224 rows, which works and which nobody can
 * use: `merge` takes several ids, and the report back was "I can only merge one
 * into one". Ctrl-clicking inside a scrolling list of 224 options is not a thing
 * to ask of anyone, and nothing on screen said it was possible. Picking from a
 * dropdown and getting a removable chip says what it does. */
function ProjectChoice({ projects, many, value, onChange }) {
  if (!many) {
    return html`
      <${ProjectSearch} projects=${projects} value=${value}
        placeholder=${value ? "search to change…" : "search projects…"}
        onPick=${(id) => onChange(id === "" ? "" : String(id))} />`;
  }

  const chosen = Array.isArray(value) ? value.map(String) : value ? [String(value)] : [];
  return html`
    <div style=${{ display: "grid", gap: 6 }}>
      ${!!chosen.length && html`
        <div style=${{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          ${chosen.map((id) => {
            const project = projects.find((p) => String(p.id) === id);
            return html`
              <button key=${id} type="button" class="dc-chip-x" title="Remove"
                      onClick=${() => onChange(chosen.filter((x) => x !== id))}>
                #${id}${project ? ` ${project.name}` : ""} <span aria-hidden="true">✕</span>
              </button>`;
          })}
        </div>`}
      <${ProjectSearch} projects=${projects} exclude=${chosen}
        placeholder=${chosen.length ? "add another…" : "search projects to add…"}
        onPick=${(id) => onChange([...chosen, String(id)])} />
      <span style=${{ fontSize: 11, color: "var(--muted-foreground)" }}>
        ${chosen.length
          ? `${chosen.length} chosen — add more, or click one to remove it`
          : "add as many as you need"}
      </span>
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

function FlagField({ flag, projects, value, onChange }) {
  const control = PROJECT_FIELDS.has(flag.f) && projects.length
    ? html`<${ProjectChoice} projects=${projects} many=${flag.many} value=${value}
             onChange=${onChange} />`
    : flag.t === "bool"
    ? html`<${Switch} size="sm" label=${value ? "on" : flag.d ? "on by default" : "off"}
             checked=${!!value} onCheckedChange=${(v) => onChange(!!v)} />`
    : flag.t === "choice"
    ? html`<${Select} size="sm" value=${value ?? ""} onChange=${(e) => onChange(e.target.value)}>
             <option value="">${flag.d ?? "default"}</option>
             ${flag.o.map((o) => html`<option key=${o} value=${o}>${o}</option>`)}<//>`
    : flag.many
    ? html`<textarea class="mrd-input" rows="3" style=${{ height: "auto", resize: "vertical" }}
             placeholder="one per line"
             value=${Array.isArray(value) ? value.join("\n") : (value ?? "")}
             onChange=${(e) => onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))} />`
    : flag.t === "int" || flag.t === "float"
    ? html`<${NumberChoice} flag=${flag} value=${value} onChange=${onChange} />`
    : html`<${Input} size="sm" value=${value ?? ""}
             placeholder=${flag.d == null ? "" : String(flag.d)}
             onChange=${(e) => onChange(e.target.value)} />`;

  return html`
    <div style=${{ display: "grid", gap: 4 }}>
      <label style=${{ display: "flex", alignItems: "baseline", gap: 7, flexWrap: "wrap" }}>
        <span style=${{ fontSize: 13, fontWeight: 500 }}>
          ${humanize(flag.f)}${flag.req ? html`<span style=${{ color: "var(--danger)" }}> *</span>` : ""}
        </span>
        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
          ${flag.f}</span>
      </label>
      ${control}
      ${flag.h && html`<span style=${{ fontSize: 11, lineHeight: "16px", color: "var(--muted-foreground)" }}>
        ${flag.h}</span>`}
    </div>`;
}

/* Named sequences, run as one job.
 *
 * Placed above the command list because for most visits it is the answer: the
 * question "what do I run to catch up" has one right answer and it is three
 * commands in a particular order, not a menu of thirty. */
function WorkflowsPanel({ workflows, allowWrite, busy, onRun }) {
  const [open, setOpen] = useState(null);
  const [armed, setArmed] = useState(null);

  if (!workflows?.length) return null;
  return html`
    <div style=${{ display: "grid", gap: 10 }}>
      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                      letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>Routines</span>
      <div style=${{ display: "grid", gap: 10 }}>
        ${workflows.map((w) => {
          const isOpen = open === w.name;
          const needsConfirm = w.cost === "llm" || !!w.destroys;
          return html`
            <${Card} key=${w.name}>
              <button type="button" onClick=${() => { setOpen(isOpen ? null : w.name); setArmed(null); }}
                style=${{ display: "grid", width: "100%", gap: 6, padding: "14px 20px",
                          background: "transparent", border: 0, textAlign: "left", cursor: "pointer" }}>
                <span style=${{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                  <span style=${{ fontSize: 15, fontWeight: 600 }}>${w.title}</span>
                  <span style=${chip(w.cost === "llm" ? "--warning" : "--muted-foreground")}>${w.cost}</span>
                  <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
                    ${w.steps.map((s) => s.cmd).join(" → ")}</span>
                </span>
                <span style=${{ fontSize: 13, lineHeight: "19px", color: "var(--muted-foreground)" }}>
                  ${w.summary}</span>
              </button>
              ${isOpen && html`
                <div style=${{ borderTop: "1px solid var(--border)", padding: "14px 20px", display: "grid", gap: 12 }}>
                  <ol style=${{ margin: 0, paddingLeft: 20, display: "grid", gap: 8 }}>
                    ${w.steps.map((s, i) => html`
                      <li key=${i} style=${{ fontSize: 13, lineHeight: "19px" }}>
                        <span style=${{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>
                          tracker ${s.cmd}${Object.entries(s.flags || {})
                            .map(([k, v]) => ` ${k} ${v}`).join("")}</span>
                        ${s.tolerates_failure && html`
                          <span style=${{ ...chip("--muted-foreground"), marginLeft: 7 }}>findings ok</span>`}
                        ${s.because && html`<div style=${{ color: "var(--muted-foreground)" }}>${s.because}</div>`}
                      </li>`)}
                  </ol>
                  <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                    Runs as one job. Stops at the first step that genuinely fails.
                  </span>
                  ${armed === w.name
                    ? html`
                      <div style=${{ display: "grid", gap: 8 }}>
                        <span style=${{ fontSize: 13, color: "var(--warning)" }}>
                          ${w.destroys ? `This ${w.destroys}` : "This spends LLM tokens across every step."}
                          ${" "}Run it?
                        </span>
                        <div style=${{ display: "flex", gap: 10 }}>
                          <${Button} size="sm" loading=${busy} variant=${w.destroys ? "danger" : undefined}
                            onClick=${() => onRun(w)}>Yes, run ${w.title.toLowerCase()}<//>
                          <${Button} size="sm" variant="ghost" onClick=${() => setArmed(null)}>Back<//>
                        </div>
                      </div>`
                    : html`
                      <div>
                        <${Button} size="sm" disabled=${!allowWrite || busy}
                          onClick=${() => (needsConfirm ? setArmed(w.name) : onRun(w))}>
                          ${needsConfirm ? "Run…" : "Run"}
                        <//>
                      </div>`}
                </div>`}
            <//>`;
        })}
      </div>
    </div>`;
}

/* A command box, for everything the form cannot say.
 *
 * The form is good at one project and a couple of flags and bad at the rest:
 * `enrich 4 7 9 12 --budget 60`, `ingest crawl --url a --url b`, anything with
 * several positionals. Making the form cover all of that would rebuild a command
 * line out of dropdowns. So: type the command line.
 *
 * **It is not a shell and never becomes one.** The line is parsed on the server
 * by `catalog.parse_command_line` into the same `(cmd, flags)` the form produces,
 * and `build_argv` turns that into the same validated argument list. There is no
 * interpreter: `cd`, `rm`, `;`, `|` and a backtick are words the catalog has
 * never heard of, and it refuses them by name. Anything that reaches a process
 * came out of the catalog.
 *
 * Confirmation is unchanged, and is a second Enter: the server answers the first
 * attempt with the word that confirms it, the box shows what it will cost, and
 * the identical line sent again carries the confirmation. Re-typing the whole
 * command is at least as deliberate as typing its name into a box. */
function CommandLine({ allowWrite, commands, onRan }) {
  const [line, setLine] = useState("");
  const [log, setLog] = useState([]);
  const [pending, setPending] = useState(null); // { line, confirm_with, destroys }
  const [busy, setBusy] = useState(false);
  const [historyAt, setHistoryAt] = useState(-1);
  const input = useRef(null);
  const tail = useRef(null);

  const names = useMemo(
    () => (commands || []).flatMap((g) => g.items).filter((c) => !c.blocked).map((c) => c.cmd),
    [commands]);
  const history = useMemo(() => log.filter((l) => l.kind === "in").map((l) => l.text), [log]);

  useEffect(() => { tail.current?.scrollIntoView({ block: "nearest" }); }, [log]);

  const say = (kind, text) => setLog((l) => [...l.slice(-60), { kind, text }]);

  const submit = async (text) => {
    const typed = text.trim();
    if (!typed || busy) return;
    say("in", typed);
    setLine("");
    setHistoryAt(-1);

    if (typed === "help" || typed === "?") {
      say("out", "Commands: " + names.join(", "));
      say("out", "Type them as you would in a terminal — `merge 4 7 9 --into 2`. Nothing else runs here.");
      return;
    }
    if (typed === "clear") { setLog([]); return; }

    // The identical line, sent again, is the confirmation.
    const confirm = pending && pending.line === typed ? pending.confirm_with : undefined;
    setPending(null);
    setBusy(true);
    try {
      const r = await api("/api/run", { method: "POST", body: { line: typed, confirm } });
      // Deliberately does *not* jump to the Runs view the way the forms do.
      // Switching away unmounts this box and takes its history with it, which is
      // exactly wrong for the one control people use to type several commands in
      // a row. The output is one click away and the click is theirs to make.
      say("run", { text: `started — ${r.run.cmd}`, run: r.run.id });
    } catch (e) {
      if (e.payload?.confirm_with) {
        setPending({ line: typed, ...e.payload });
        say("warn", e.payload.destroys
          ? `This ${e.payload.destroys} Press Enter again to confirm.`
          : "This spends LLM tokens. Press Enter again to confirm.");
      } else {
        say("err", e.message);
      }
    } finally {
      setBusy(false);
      input.current?.focus();
    }
  };

  const onKey = (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(line); return; }
    if (e.key === "Tab") {
      // Complete the longest command name that starts with what is typed.
      e.preventDefault();
      const hits = names.filter((n) => n.startsWith(line.trim()));
      if (hits.length === 1) setLine(hits[0] + " ");
      else if (hits.length > 1) say("out", hits.join("  "));
      return;
    }
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      if (!history.length) return;
      e.preventDefault();
      const next = e.key === "ArrowUp"
        ? Math.min(history.length - 1, historyAt + 1)
        : Math.max(-1, historyAt - 1);
      setHistoryAt(next);
      setLine(next === -1 ? "" : history[history.length - 1 - next]);
    }
  };

  return html`
    <div style=${{ display: "grid", gap: 10 }}>
      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                      letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>Command box</span>
      <${Card}>
        <${CardHeader}>
          <${CardTitle}>Type a command<//>
          <${CardDescription}>
            For everything the forms above cannot say — several projects at once, repeated
            options, a long line you already know. Only ${html`<code class="mrd-code">tracker</code>`}
            commands run here; there is no shell, so ${html`<code class="mrd-code">cd</code>`},
            ${html`<code class="mrd-code">rm</code>`} and pipes are refused by name.
            Tab completes, ↑ recalls, ${html`<code class="mrd-code">help</code>`} lists.
          <//>
        <//>
        <div style=${{ padding: "0 20px 18px", display: "grid", gap: 10 }}>
          ${!!log.length && html`
            <div class="dc-term-log">
              ${log.map((entry, i) => html`
                <div key=${i} class=${"dc-term-line dc-term-" + (entry.kind === "run" ? "ok" : entry.kind)}>
                  ${entry.kind === "in" ? html`<span class="dc-term-prompt">$</span> ` : ""}
                  ${entry.kind === "run" ? entry.text.text : entry.text}
                  ${entry.kind === "run" && html`
                    <button type="button" class="dc-term-link"
                            onClick=${() => onRan(entry.text.run)}>view output</button>`}
                </div>`)}
              <div ref=${tail} />
            </div>`}
          <div class="dc-term-entry">
            <span class="dc-term-prompt" aria-hidden="true">$</span>
            <input ref=${input} class="dc-term-input" spellcheck="false" autocomplete="off"
              placeholder=${allowWrite ? "merge 4 7 9 --into 2" : "read-only console"}
              disabled=${!allowWrite || busy}
              value=${line}
              onInput=${(e) => setLine(e.target.value)}
              onKeyDown=${onKey} />
            <${Button} size="sm" variant="ghost" loading=${busy}
              disabled=${!allowWrite || busy || !line.trim()}
              onClick=${() => submit(line)}>
              ${pending && pending.line === line.trim() ? "Confirm" : "Run"}
            <//>
          </div>
        </div>
      <//>
    </div>`;
}

function CommandsView({ data, onRan }) {
  const [catalogue, setCatalogue] = useState(null);
  const [openCmd, setOpenCmd] = useState(null);
  const [values, setValues] = useState({});
  const [confirm, setConfirm] = useState("");
  const [armed, setArmed] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api("/api/commands").then(setCatalogue).catch((e) => setError(e.message)); }, []);

  const projects = useMemo(
    () => (data.projects || []).slice().sort((a, b) => a.id - b.id), [data.projects]);

  const pick = (cmd) => {
    setOpenCmd(openCmd?.cmd === cmd.cmd ? null : cmd);
    setValues({});
    setConfirm("");
    setArmed(false);
    setShowAll(false);
    setError(null);
  };

  const run = async (cmd, confirmWith) => {
    setBusy(true); setError(null);
    try {
      const r = await api("/api/run", {
        method: "POST",
        body: { cmd: cmd.cmd, flags: values, confirm: confirmWith ?? confirm },
      });
      onRan(r.run.id);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const runWorkflow = async (w) => {
    setBusy(true); setError(null);
    try {
      const r = await api("/api/workflow", { method: "POST", body: { name: w.name, confirm: w.name } });
      onRan(r.run.id);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const preview = (cmd) => "tracker " + cmd.cmd + Object.entries(values)
    .filter(([, v]) => v !== "" && v !== false && v != null && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => {
      const fl = cmd.flags.find((f) => f.f === k);
      const list = Array.isArray(v) ? v : [v];
      if (fl?.t === "bool") return ` ${k}`;
      if (fl?.positional) return list.map((x) => ` ${x}`).join("");
      return list.map((x) => ` ${k} ${x}`).join("");
    }).join("");

  // A required positional with nothing in it is the commonest way a run gets
  // refused, and the server's message arrives after the click. Say it before.
  const unmet = (cmd) => cmd.flags
    .filter((f) => f.req)
    .filter((f) => {
      const v = values[f.f];
      return v == null || v === "" || (Array.isArray(v) && !v.length);
    })
    .map((f) => humanize(f.f));

  if (!catalogue) return html`<div style=${{ padding: 26 }}><${Skeleton} height=${180} width="100%" /></div>`;
  const groups = catalogue.groups;

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 06 — commands" title="Run things">
        Start with a ${html`<b>routine</b>`} — a named sequence that does the steps in the order they
        want doing. The individual commands are below it, read from the CLI itself so they cannot fall
        behind. ${html`<b>llm</b>`} means it spends money; ${html`<b>destructive</b>`} means it deletes
        rows and asks you to type the name. Nothing here is run as a shell command.
      <//>

      ${error && html`<${Alert} variant="danger"><div><div class="mrd-alert-title">Refused</div>
        <div class="mrd-alert-desc">${error}</div></div><//>`}
      ${!data.allow_write && html`<${Alert} variant="warning"><div><div class="mrd-alert-title">Read-only console</div>
        <div class="mrd-alert-desc">Started with --no-run. The argv below is still correct to paste into a terminal.</div></div><//>`}

      <${WorkflowsPanel} workflows=${catalogue.workflows} allowWrite=${data.allow_write}
                         busy=${busy} onRun=${runWorkflow} />

      ${groups.map((g) => html`
        <div key=${g.group} style=${{ display: "grid", gap: 10 }}>
          <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                          letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>${g.group}</span>
          <div style=${{ display: "grid", gap: 10 }}>
            ${g.items.map((cmd) => {
              const isOpen = openCmd?.cmd === cmd.cmd;
              // Two different losses, one ritual. `merge` spends nothing and is
              // the only command here you cannot undo, so it gets the same typed
              // confirmation as `sync` and says a different sentence.
              const needsConfirm = cmd.cost === "llm" || !!cmd.destroys;
              return html`
                <${Card} key=${cmd.cmd}>
                  <button type="button" onClick=${() => pick(cmd)} disabled=${!!cmd.blocked}
                    style=${{ display: "flex", width: "100%", gap: 12, alignItems: "baseline", padding: "14px 20px",
                              background: "transparent", border: 0, textAlign: "left",
                              cursor: cmd.blocked ? "not-allowed" : "pointer", opacity: cmd.blocked ? .55 : 1 }}>
                    <span style=${{ fontFamily: "var(--font-mono)", fontSize: 14, fontWeight: 600 }}>${cmd.cmd}</span>
                    <span style=${chip(cmd.cost === "llm" ? "--warning" : "--muted-foreground")}>${cmd.cost}</span>
                    ${cmd.destroys && html`<span style=${chip("--danger")}>destructive</span>`}
                    <span style=${{ flex: 1, fontSize: 13, color: "var(--muted-foreground)" }}>
                      ${cmd.blocked ? cmd.blocked : cmd.desc}</span>
                  </button>
                  ${isOpen && (() => {
                    // Required first, then a few, then the rest folded away.
                    // `sync` has thirteen flags and all thirteen have defaults
                    // that work; showing them all reads as thirteen decisions.
                    const required = cmd.flags.filter((f) => f.req);
                    const optional = cmd.flags.filter((f) => !f.req);
                    const shown = showAll ? optional : optional.slice(0, 3);
                    const hidden = optional.length - shown.length;
                    const missing = unmet(cmd);
                    const set = (f) => (v) => setValues((s) => ({ ...s, [f]: v }));
                    return html`
                    <div style=${{ borderTop: "1px solid var(--border)", padding: "16px 20px", display: "grid", gap: 14 }}>
                      ${cmd.flags.length > 0 && html`
                        <div style=${{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
                          ${[...required, ...shown].map((fl) => html`
                            <${FlagField} key=${fl.f} flag=${fl} projects=${projects}
                                          value=${values[fl.f]} onChange=${set(fl.f)} />`)}
                        </div>`}
                      ${hidden > 0 && html`
                        <div><${Button} size="sm" variant="ghost" onClick=${() => setShowAll(true)}>
                          ${hidden} more option${hidden === 1 ? "" : "s"}
                        <//></div>`}

                      ${!!missing.length && html`
                        <span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                          Still needed: ${missing.join(", ")}.
                        </span>`}

                      ${/* The argv stays. It is the honest record of what will run,
                            it is what you paste into a terminal on a read-only
                            console, and it is how the labels above teach the
                            flags they stand for. */ ""}
                      <pre class="dc-log" style=${{ maxHeight: 80 }}>${preview(cmd)}</pre>

                      ${/* Two rituals for two different losses. `merge` cannot be
                            undone, so it keeps the typed name — deliberate friction
                            in front of an irreversible act. Spending tokens is
                            recoverable (you are out some money, not some data), so
                            it gets an explicit second click instead of homework. */ ""}
                      ${cmd.destroys && html`
                        <div style=${{ display: "grid", gap: 6 }}>
                          <span style=${{ fontSize: 13, color: "var(--danger)" }}>
                            This ${cmd.destroys} Type
                            ${" "}${html`<b style=${{ fontFamily: "var(--font-mono)" }}>${cmd.cmd}</b>`} to confirm.
                          </span>
                          <${Input} size="sm" value=${confirm} placeholder=${cmd.cmd}
                                    onChange=${(e) => setConfirm(e.target.value)} />
                        </div>`}
                      ${!cmd.destroys && needsConfirm && armed && html`
                        <span style=${{ fontSize: 13, color: "var(--warning)" }}>
                          This spends LLM tokens. Run it?
                        </span>`}

                      <div style=${{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                        ${cmd.destroys
                          ? html`<${Button} size="sm" loading=${busy} variant="danger"
                              disabled=${!data.allow_write || busy || !!missing.length
                                         || confirm.trim() !== cmd.cmd}
                              onClick=${() => run(cmd)}>Run<//>`
                          : needsConfirm && !armed
                          ? html`<${Button} size="sm" disabled=${!data.allow_write || busy || !!missing.length}
                              onClick=${() => setArmed(true)}>Run…<//>`
                          : html`<${Button} size="sm" loading=${busy}
                              disabled=${!data.allow_write || busy || !!missing.length}
                              onClick=${() => run(cmd, needsConfirm ? cmd.cmd : undefined)}>
                              ${needsConfirm ? "Yes, run it" : "Run"}<//>`}
                        <${Button} size="sm" variant="ghost"
                          onClick=${() => (armed ? setArmed(false) : setOpenCmd(null))}>
                          ${armed ? "Back" : "Cancel"}<//>
                      </div>
                    </div>`;
                  })()}
                <//>`;
            })}
          </div>
        </div>`)}

      ${/* Last, because it is the escape hatch rather than the front door: the
            forms cover the ordinary cases, and this covers the ones a form would
            have to become a command line to express. */ ""}
      <${CommandLine} allowWrite=${data.allow_write} commands=${groups} onRan=${onRan} />
    </div>`;
}

function RunsView({ watchId }) {
  const [state, setState] = useState({ runs: [], current: null });
  const [selected, setSelected] = useState(watchId || null);
  const [live, setLive] = useState(null);
  const logRef = useRef(null);

  const refresh = useCallback(() => api("/api/runs").then(setState).catch(() => {}), []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { if (watchId) setSelected(watchId); }, [watchId]);

  // Stream the selected run. A finished run replays from its file and the
  // stream closes immediately, so the same code path serves both.
  useEffect(() => {
    if (!selected) return;
    setLive({ lines: [], done: false });
    const es = new EventSource(`/api/run/${selected}/stream`);
    es.onmessage = (msg) => {
      const event = JSON.parse(msg.data);
      if (event.type === "line") {
        setLive((s) => ({ ...s, lines: [...(s?.lines || []), event.line] }));
      } else if (event.type === "end") {
        setLive((s) => ({ ...s, done: true, run: event.run }));
        es.close();
        refresh();
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [selected, refresh]);

  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [live]);

  const badge = (status) => chip(status === "ok" ? "--success" : status === "running" ? "--chart-1"
    : status === "cancelled" ? "--muted-foreground" : "--danger");

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 07 — runs" title="What has been run, and what it printed">
        The same output the terminal would show, colour and all, kept as one file per run. Wide tables
        scroll sideways so their columns stay lined up — switch ${html`<b>wrap</b>`} on for long messages.
      <//>

      <div style=${{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "flex-start" }}>
        <div style=${{ flex: "1 1 280px", maxWidth: 380, display: "grid", gap: 9 }}>
          ${state.current && state.current.status === "running" && html`
            <button type="button" class="dc-tile dc-tile--on" onClick=${() => setSelected(state.current.id)}>
              <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style=${badge("running")}>running</span>
                <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>${state.current.cmd}</span>
              </div>
            </button>`}
          ${state.runs.map((r) => html`
            <button key=${r.id} type="button" class=${`dc-tile${selected === r.id ? " dc-tile--on" : ""}`}
                    onClick=${() => setSelected(r.id)}>
              <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style=${badge(r.status)}>${r.status}</span>
                <span style=${{ flex: 1, fontFamily: "var(--font-mono)", fontSize: 13, overflow: "hidden",
                                textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${r.cmd}</span>
                <span class="dc-num" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
                  ${r.duration_s == null ? "" : r.duration_s + "s"}</span>
              </div>
              <div style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
                ${r.started_at}${r.projects_touched ? ` · ${r.projects_touched} project(s) changed` : ""}
              </div>
            </button>`)}
          ${state.runs.length === 0 && !state.current && html`
            <${EmptyState} variant="dashed" size="sm" title="Nothing has run yet"
              description="Start something from the Commands view and its output appears here as it happens." />`}
        </div>

        <div style=${{ flex: "1 1 460px", minWidth: 0 }}>
          ${selected
            ? html`<${LogPane} lines=${live?.lines || []} innerRef=${logRef} />`
            : html`<${Card}><div style=${{ padding: 20, color: "var(--muted-foreground)", fontSize: 14 }}>
                Pick a run to read its log.</div><//>`}
        </div>
      </div>
    </div>`;
}

/* ---- Root ---------------------------------------------------------------- */

const VIEWS = [
  ["projects", "Projects"], ["map", "Map"], ["capex", "Capex"], ["queue", "Queue"],
  ["gaps", "Coverage"], ["commands", "Commands"], ["runs", "Runs"], ["help", "Help"],
];

function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState("projects");
  const [openId, setOpenId] = useState(null);
  const [dark, setDark] = useState(false);
  const [watchRun, setWatchRun] = useState(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(() => api("/api/dataset").then((payload) => {
    if (payload.kwPerH200) H200_KW = payload.kwPerH200;
    setData(payload);
    // The vendored custom elements read `window.DCTRACKER`. They were written
    // against the mockup's shape, which the API deliberately matches, so they
    // need no adaptation — only to be told the data has arrived.
    window.DCTRACKER = payload;
    window.dispatchEvent(new Event("dctracker-ready"));
  }).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { document.documentElement.classList.toggle("dark", dark); }, [dark]);

  /* One run at a time is a server rule; knowing about it here is what lets the
   * Queue and the merge buttons disable themselves instead of offering an action
   * that will 409.
   *
   * It also refetches the dataset when a run finishes, which is what makes a
   * command actually change the page. Until this existed, a crawl finished and
   * the article stayed in the queue, a merge finished and the folded rows stayed
   * in the table — the run log said it had worked and every other view
   * disagreed.
   *
   * Keyed on the run's id and status rather than on a running→idle transition.
   * A falling edge is only observable if some poll caught the run *while* it was
   * running, and a merge takes about a second against a four-second interval —
   * measured: the merge completed between two ticks, nothing reloaded, and the
   * folded rows sat there looking merged in the log and present in the table.
   * An id that has reached a terminal status is a fact about the past, so it
   * cannot be missed however briefly the run existed.
   *
   * Polling rather than hooking each button covers every path that can start
   * one, including a run started from another tab. */
  useEffect(() => {
    let cancelled = false;
    let seen = null;
    const poll = () => api("/api/runs")
      .then((r) => {
        if (cancelled) return;
        const current = r.current;
        setRunning(current?.status === "running");
        const stamp = current ? `${current.id}:${current.status}` : null;
        // The first poll only establishes a baseline: reloading here would be a
        // second dataset fetch on every page load, for a run that ended before
        // the tab was even open.
        if (seen !== null && stamp !== seen && current?.status !== "running") load();
        seen = stamp;
      })
      .catch(() => {});
    poll();
    const timer = setInterval(poll, 4000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [load]);

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
    return html`<div style=${{ padding: 40, maxWidth: "70ch" }}>
      <${Alert} variant="danger"><div>
        <div class="mrd-alert-title">The console could not read the database</div>
        <div class="mrd-alert-desc" style=${{ whiteSpace: "pre-wrap" }}>${error}</div>
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
          <div style=${{ display: "flex", alignItems: "baseline", gap: 9, flex: "none" }}>
            <span style=${{ fontFamily: "var(--font-display)", fontSize: 24, fontWeight: 500,
                            letterSpacing: "-0.015em" }}>dc-tracker</span>
            <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12,
                            color: "var(--muted-foreground)" }}>v${data.version}</span>
          </div>

          <div class="dc-seg">
            ${VIEWS.map(([key, label]) => html`
              <button key=${key} type="button" class="dc-seg-btn" aria-pressed=${view === key}
                      onClick=${() => { setView(key); setOpenId(null); }}>${label}</button>`)}
          </div>

          <span style=${{ flex: "1 1 40px" }} />

          <div class="dc-head-actions" style=${{ display: "flex", alignItems: "center", gap: 10, flex: "none" }}>
            <span class="dc-num dc-head-counts" style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>
              <${Counted} value=${t.projects} /> projects · <${Counted} value=${t.states} /> states · <${Counted} value=${t.citations} /> citations
            </span>
            <${Button} size="icon" variant="outline" aria-label="Toggle theme"
                       onClick=${() => setDark((d) => !d)}>${dark ? "☀" : "☾"}<//>
            ${data.password_protected && html`
              <${Button} size="sm" variant="ghost" onClick=${async () => {
                await api("/api/logout", { method: "POST", body: {} });
                window.location.reload();
              }}>Sign out<//>`}
          </div>
        </header>

        ${view === "projects" && html`<${ProjectsView} data=${data} openId=${openId} onOpen=${setOpenId} />`}
        ${view === "map" && html`<${MapView} data=${data} openId=${openId} onOpen=${setOpenId} />`}
        ${view === "capex" && html`
          <${CapexView} data=${data} allowWrite=${data.allow_write} busy=${!!running}
            onRan=${(id) => { setWatchRun(id); setView("runs"); }} />`}
        ${view === "queue" && html`
          <${QueueView} data=${data} allowWrite=${data.allow_write} busy=${!!running}
            onRan=${(id) => { setWatchRun(id); setView("runs"); }} />`}
        ${view === "gaps" && html`<${GapsView} data=${data} />`}
        ${view === "commands" && html`<${CommandsView} data=${data}
          onRan=${(id) => { setWatchRun(id); setView("runs"); }} />`}
        ${view === "runs" && html`<${RunsView} watchId=${watchRun} />`}
        ${view === "help" && html`<${HelpView} data=${data} />`}
      </div>

      <${Drawer} data=${data} project=${open} onClose=${() => setOpenId(null)} />
    </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
