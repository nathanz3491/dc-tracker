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
const AUDIT = ["county", "lat", "lon", "confidence", "last_verified_at"];
const RIGHT = new Set(["mw_planned", "mw_built", "investment_usd", "confidence", "lat", "lon"]);

/* Coverage at or above this shows by default. Chosen so the default table is
 * mostly populated rather than mostly dashes; everything below it is one switch
 * away and the switch says how many. */
const DENSE_THRESHOLD = 50;

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
  missing: "NULL — no source asserted this value. Absence is not always a gap: for blocker and customer it usually means there is nothing to report.",
  defaulted: "Nobody stated one. The column is NOT NULL, so the schema default is what is sitting there — which is why the row is routed to `tracker review` rather than treated as a fact.",
  unconfirmed: "待确认 — a source asserted this, but no verbatim quote the evidence gate could verify. Kept so the value survives to be confirmed, and excluded from confidence.",
};
/* Phase hue lives on the map only, where a legend explains it. */
const PHASE_TOKEN = {
  announced: "--chart-5", permitting: "--chart-1", construction: "--chart-3",
  operational: "--chart-2", paused: "--warning", cancelled: "--muted-foreground",
};
const SEV_TOKEN = { watch: "--muted-foreground", material: "--warning", blocking: "--danger" };
const SEV_ORDER = ["watch", "material", "blocking"];

/* ---- formatting ---------------------------------------------------------- */

const fmtUSD = (v) => v == null ? "—"
  : v >= 1e9 ? "$" + (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B"
  : v >= 1e6 ? "$" + Math.round(v / 1e6) + "M"
  : "$" + v.toLocaleString();

function fmt(key, v) {
  if (v == null || v === "") return "—";
  if (key === "mw_planned" || key === "mw_built") return Number(v).toLocaleString();
  if (key === "investment_usd") return fmtUSD(v);
  if (key === "lat" || key === "lon") return Number(v).toFixed(4);
  if (key === "last_verified_at" || key === "updated_at") return String(v).slice(0, 10);
  return String(v);
}
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
  if (!res.ok) throw Object.assign(new Error(payload?.error || res.statusText), { status: res.status });
  return payload;
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

function LogPane({ lines, innerRef }) {
  return html`
    <pre class="dc-log" ref=${innerRef} aria-live="polite" aria-atomic="false">
      ${lines.length === 0
        ? "connecting…"
        : lines.map((line, i) => html`<${LogLine} key=${i} line=${line} />`)}
    </pre>`;
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
        ${quote.exact === false && quote.hasQuote && html`
          <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
            from the source excerpt, not this field's own sentence
          </span>`}
      </div>
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
      if (q) {
        const hay = [p.name, p.company, p.customer, p.city, p.county, p.state, p.blocker]
          .filter(Boolean).join(" ").toLowerCase();
        if (!hay.includes(q)) return false;
      }
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
      <${Eyebrow} figure="fig. 01 — projects" title="Every tracked field, and how much of it is quoted">
        Colour here means trust, never category: a value's underline says whether a source quoted it, a
        lookup derived it, a model guessed it, or nobody said anything at all. Hover any value for the
        sentence behind it.
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
  const tabs = [["stats", "Stats", ""], ["risks", "Risks", ` ${open.length}`], ["sources", "Sources", ` ${p.sources.length}`]];

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
                                                 open=${open} onQuote=${showQuote} />`}
          ${tab === "risks" && html`<${RisksTab} data=${data} p=${p} />`}
          ${tab === "sources" && html`<${SourcesTab} data=${data} p=${p} />`}
        </div>
      </aside>
      <${QuotePopover} quote=${quote} />
    </div>`;
}

function StatsTab({ data, p, populated, open, onQuote }) {
  const worst = open.slice().sort((a, b) => SEV_ORDER.indexOf(b.severity) - SEV_ORDER.indexOf(a.severity))[0];
  const stats = [
    { label: "Planned capacity", value: p.mw_planned == null ? "—" : p.mw_planned.toLocaleString() + " MW",
      hint: p.mw_planned == null ? "no source cited one" : TIER[tierOf(p, "mw_planned")][0] },
    { label: "Built to date", value: p.mw_built == null ? "—" : p.mw_built.toLocaleString() + " MW",
      hint: p.mw_built == null ? "nothing built, or nothing read" : TIER[tierOf(p, "mw_built")][0] },
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
                      ${q.exact === false && !!provOf(p, key)?.quote && html`
                        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--muted-foreground)" }}>
                          excerpt, not this field's sentence</span>`}
                    </div>
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
              <${CardDescription}>A campus can own its land outright and still be four years deep in an
                interconnection queue. The signal to watch for is the next unreached milestone on the
                blocked track.<//>
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
        Every non-null value above traces back to one of these. A lone source caps confidence at 2 however
        authoritative — independence is counted by domain, not by row.
      </p>
      ${p.sources.map((s, i) => {
        const placeholder = s.url.includes("PLACEHOLDER");
        const fields = (s.fields || "").split(",").filter(Boolean);
        const quotes = s.quotes || {};
        return html`
          <${Card} key=${i}>
            <div style=${{ display: "flex", alignItems: "center", gap: 10, padding: "14px 20px",
                           borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
              <span style=${chip(["company_filing", "government_doc"].includes(s.source_type) ? "--success"
                : s.source_type === "iso_queue" ? "--warning" : "--chart-1")}>${s.source_type}</span>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--muted-foreground)" }}>
                weight ${data.sourceWeight[s.source_type] || 1}</span>
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
      <${Eyebrow} figure="fig. 02 — geography" title="Where the data centers really are">
        Positions are Census place or county centroids — the centre of the place, ${html`<b>not</b>`} the
        project site. Bubble area is cited planned capacity; a hollow dashed ring means no source cited
        one. ${plotted} of ${data.projects.length} projects have coordinates.
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
      <${Eyebrow} figure="fig. 03 — queue" title="Headlines waiting to be read">
        Discovery filters feeds down to candidates and stops there — nothing is fetched or sent to a model
        until you say so. Precision is deliberately not the goal: it is cheaper to over-collect and triage
        than to tune a keyword filter until it silently drops real projects. A ${html`<b>deepens</b>`} tag
        means the article names a project already tracked, which is where an LLM call pays best — read
        those first. ${html`<b>Crawl</b>`} reads one article now, for one LLM call.
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
          <${CardDescription}>Grouped by host, because the cause almost always is the host. These are
            otherwise invisible: a run can report an empty queue while a dozen articles sit unread.<//>
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
      <${Eyebrow} figure="fig. 04 — coverage" title="Where the data is thin, measured honestly">
        Each field is measured against the rows where it can legitimately be set, not against every
        project. That distinction matters more than it sounds: mw_built looked 13% covered while most
        projects were merely announced, so nothing was built and NULL was the correct answer. Fields whose
        absence carries no information report n/a rather than a low score.
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
          <${CardDescription}>The PRD's definition of done names 30 specific projects but does not list
            them. Paste them into ${req.path} to turn an unmeasurable requirement into a measurable one.<//>
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
          <${CardDescription}>A project appears under every category obstructing it, so these do not add
            up to a fleet total. Projects with an open risk and no cited capacity are counted separately
            rather than as zero.<//>
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

/* ---- Commands and runs --------------------------------------------------- */

function CommandsView({ data, onRan }) {
  const [groups, setGroups] = useState(null);
  const [openCmd, setOpenCmd] = useState(null);
  const [values, setValues] = useState({});
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api("/api/commands").then((r) => setGroups(r.groups)).catch((e) => setError(e.message)); }, []);

  const pick = (cmd) => {
    setOpenCmd(openCmd?.cmd === cmd.cmd ? null : cmd);
    setValues({});
    setConfirm("");
    setError(null);
  };

  const run = async (cmd) => {
    setBusy(true); setError(null);
    try {
      const r = await api("/api/run", { method: "POST", body: { cmd: cmd.cmd, flags: values, confirm } });
      onRan(r.run.id);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const preview = (cmd) => "tracker " + cmd.cmd + Object.entries(values)
    .filter(([, v]) => v !== "" && v !== false && v != null)
    .map(([k, v]) => (cmd.flags.find((f) => f.f === k)?.t === "bool" ? ` ${k}`
      : cmd.flags.find((f) => f.f === k)?.positional ? ` ${v}` : ` ${k} ${v}`)).join("");

  if (!groups) return html`<div style=${{ padding: 26 }}><${Skeleton} height=${180} width="100%" /></div>`;

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 05 — commands" title="The same commands, with their real flags">
        Read straight out of the CLI, so this cannot fall behind it. A command marked
        ${html`<b>llm</b>`} spends real tokens and needs its name typed to confirm — the console builds an
        argument list and never a shell string, so nothing you type here is interpreted as a command.
      <//>

      ${error && html`<${Alert} variant="danger"><div><div class="mrd-alert-title">Refused</div>
        <div class="mrd-alert-desc">${error}</div></div><//>`}
      ${!data.allow_write && html`<${Alert} variant="warning"><div><div class="mrd-alert-title">Read-only console</div>
        <div class="mrd-alert-desc">Started with --no-run. The argv below is still correct to paste into a terminal.</div></div><//>`}

      ${groups.map((g) => html`
        <div key=${g.group} style=${{ display: "grid", gap: 10 }}>
          <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                          letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>${g.group}</span>
          <div style=${{ display: "grid", gap: 10 }}>
            ${g.items.map((cmd) => {
              const isOpen = openCmd?.cmd === cmd.cmd;
              return html`
                <${Card} key=${cmd.cmd}>
                  <button type="button" onClick=${() => pick(cmd)} disabled=${!!cmd.blocked}
                    style=${{ display: "flex", width: "100%", gap: 12, alignItems: "baseline", padding: "14px 20px",
                              background: "transparent", border: 0, textAlign: "left",
                              cursor: cmd.blocked ? "not-allowed" : "pointer", opacity: cmd.blocked ? .55 : 1 }}>
                    <span style=${{ fontFamily: "var(--font-mono)", fontSize: 14, fontWeight: 600 }}>${cmd.cmd}</span>
                    <span style=${chip(cmd.cost === "llm" ? "--warning" : "--muted-foreground")}>${cmd.cost}</span>
                    <span style=${{ flex: 1, fontSize: 13, color: "var(--muted-foreground)" }}>
                      ${cmd.blocked ? cmd.blocked : cmd.desc}</span>
                  </button>
                  ${isOpen && html`
                    <div style=${{ borderTop: "1px solid var(--border)", padding: "16px 20px", display: "grid", gap: 14 }}>
                      ${cmd.flags.length > 0 && html`
                        <div style=${{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
                          ${cmd.flags.map((fl) => html`
                            <div key=${fl.f} style=${{ display: "grid", gap: 4 }}>
                              <label style=${{ fontFamily: "var(--font-mono)", fontSize: 12,
                                color: fl.req ? "var(--foreground)" : "var(--muted-foreground)" }}>
                                ${fl.f}${fl.req ? " *" : ""}</label>
                              ${fl.t === "bool"
                                ? html`<${Switch} size="sm" label=${fl.d ? "on by default" : "off"}
                                        checked=${!!values[fl.f]}
                                        onCheckedChange=${(v) => setValues((s) => ({ ...s, [fl.f]: !!v }))} />`
                                : fl.t === "choice"
                                ? html`<${Select} size="sm" value=${values[fl.f] ?? ""}
                                        onChange=${(e) => setValues((s) => ({ ...s, [fl.f]: e.target.value }))}>
                                        <option value="">${fl.d ?? "default"}</option>
                                        ${fl.o.map((o) => html`<option key=${o} value=${o}>${o}</option>`)}<//>`
                                : html`<${Input} size="sm" value=${values[fl.f] ?? ""}
                                        placeholder=${fl.d == null ? "" : String(fl.d)}
                                        onChange=${(e) => setValues((s) => ({ ...s, [fl.f]: e.target.value }))} />`}
                              <span style=${{ fontSize: 11, lineHeight: "16px", color: "var(--muted-foreground)" }}>${fl.h}</span>
                            </div>`)}
                        </div>`}

                      <pre class="dc-log" style=${{ maxHeight: 80 }}>${preview(cmd)}</pre>

                      ${cmd.cost === "llm" && html`
                        <div style=${{ display: "grid", gap: 6 }}>
                          <span style=${{ fontSize: 13, color: "var(--warning)" }}>
                            This spends LLM tokens. Type ${html`<b style=${{ fontFamily: "var(--font-mono)" }}>${cmd.cmd}</b>`} to confirm.
                          </span>
                          <${Input} size="sm" value=${confirm} placeholder=${cmd.cmd}
                                    onChange=${(e) => setConfirm(e.target.value)} />
                        </div>`}

                      <div style=${{ display: "flex", gap: 10 }}>
                        <${Button} size="sm" loading=${busy}
                          disabled=${!data.allow_write || busy || (cmd.cost === "llm" && confirm.trim() !== cmd.cmd)}
                          onClick=${() => run(cmd)}>Run<//>
                        <${Button} size="sm" variant="ghost" onClick=${() => setOpenCmd(null)}>Cancel<//>
                      </div>
                    </div>`}
                <//>`;
            })}
          </div>
        </div>`)}
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
      <${Eyebrow} figure="fig. 06 — runs" title="What has been run, and what it printed">
        Exactly the output the terminal would show, kept per run beside the database. Runs are recorded as
        files rather than rows: a command's stdout is operational exhaust with a different lifetime from
        the tracked data, and the schema is not the place for it.
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
  ["projects", "Projects"], ["map", "Map"], ["queue", "Queue"],
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

  // One run at a time is a server rule; knowing about it here is what lets the
  // Queue disable its buttons instead of offering an action that will 409.
  useEffect(() => {
    let cancelled = false;
    const poll = () => api("/api/runs")
      .then((r) => { if (!cancelled) setRunning(r.current?.status === "running"); })
      .catch(() => {});
    poll();
    const timer = setInterval(poll, 4000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const load = useCallback(() => api("/api/dataset").then((payload) => {
    setData(payload);
    // The vendored custom elements read `window.DCTRACKER`. They were written
    // against the mockup's shape, which the API deliberately matches, so they
    // need no adaptation — only to be told the data has arrived.
    window.DCTRACKER = payload;
    window.dispatchEvent(new Event("dctracker-ready"));
  }).catch((e) => setError(e.message)), []);
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
