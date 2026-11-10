/* Rules: what applies to a place rather than to one campus.
 *
 * A state's permit pause, a town's ban, a grid operator's rule for large loads, a
 * tariff — read from the news once, quoted, and matched by code to every project in
 * its place and stage (`tracker/conditions.py`). Three surfaces draw them, and all
 * three take their parts from here:
 *   - the Rules view (`RulesView`): every rule held, filterable by state, each with
 *     how many projects it reaches and, opened, which ones and why; then the market —
 *     nationwide and whole-industry rules, which reach no project;
 *   - a project's page, its "Exposed to" section (`ExposureList`);
 *   - Monitor, its "Rules for their places" section (`RuleStatus`, `RuleFit`).
 *
 * What the server decides and this file only draws (`webui/rules.py`,
 * `watchfor.clears`): which projects a rule reaches and how sure that is, the reason
 * in words, and what would clear it. **Kept apart from obstacles everywhere.** A rule
 * reaches a campus because of where it is filed, not because anything was reported
 * about the campus, so no count of obstacles, no blocker and no confidence reads it.
 * **And a rule that eases building is not a threat**: an exemption or by-right zoning
 * is drawn under "Helped by", in good-news colours, with nothing to clear.
 *
 * Kept out of app.js so a reader of one view need not scroll past another. `api` and
 * `onOpen` are passed in, because this file cannot import from app.js (app.js imports
 * it, and so does `views-monitor.js`).
 */

const html = htm.bind(React.createElement);
const { useState, useEffect } = React;
const NS = window.MeridianDesignSystem_6e9015 || {};
const { Alert, EmptyState, Select, Skeleton, Switch } = NS;

/* A rule's status, coloured by how near it is to binding: talked about, promised,
   adopted, in effect. A court's block is a colour of its own, because the rule may
   bind again once the case is done; a restriction lifted or voted down is cleared.
   A rule that eases building gets the good-news colours instead — an exemption in
   force must not be drawn in the colour of a ban in force. */
const RULE_TONE = {
  proposed: "--muted-foreground", pledged: "--chart-1", passed: "--warning",
  in_force: "--danger", blocked: "--chart-5", lifted: "--success", rejected: "--success",
};
const EASING_TONE = {
  proposed: "--muted-foreground", pledged: "--chart-2", passed: "--chart-2",
  in_force: "--success", blocked: "--chart-5", lifted: "--muted-foreground", rejected: "--muted-foreground",
};

export const eases = (rule) => rule.effect === "eases";

const EFFECT_NOTE = {
  restricts: "Restricts: makes building harder here — a ban, a moratorium, a permit pause, a new cost.",
  eases: "Eases: makes building easier here — a tax exemption, building allowed by right, a law letting large loads build their own power.",
};

const STATUS_NOTE = {
  proposed: "Proposed: introduced, petitioned or put to a vote, and not adopted.",
  pledged: "Pledged: promised by an official or a candidate, and not yet acted on.",
  passed: "Passed: adopted, and not yet in effect.",
  in_force: "In force: in effect now.",
  blocked: "Blocked: a court has stopped it for now. It may bind again once the case is done.",
  lifted: "Lifted: repealed, rescinded or expired. It reaches no project.",
  rejected: "Voted down or turned down. It reaches no project.",
};

const FIT_NOTE = {
  exact: "Exact: the campus is filed in the state or county the rule covers, at a stage it covers.",
  likely: "Likely: part of the match is inferred — a town's limits (no campus has a site "
    + "coordinate, only the town it is filed under), a grid region (read from the state), "
    + "or the campus's size (not published). The reason beside it says which.",
};

const chip = (token) => {
  const t = `var(${token})`;
  return {
    display: "inline-flex", alignItems: "center", height: 22, padding: "0 9px",
    borderRadius: 999, fontFamily: "var(--font-mono)", fontSize: 12,
    letterSpacing: ".02em", whiteSpace: "nowrap",
    background: `color-mix(in oklab, ${t} 14%, transparent)`, color: t,
    border: `1px solid color-mix(in oklab, ${t} 32%, transparent)`,
  };
};

/* Exact is a plain outline; likely is dashed, the way the party list marks a role
   that was inferred rather than stated. */
const outline = (dashed) => ({
  display: "inline-flex", alignItems: "center", height: 22, padding: "0 9px",
  borderRadius: 999, fontFamily: "var(--font-mono)", fontSize: 12,
  letterSpacing: ".02em", whiteSpace: "nowrap", background: "transparent",
  color: dashed ? "var(--muted-foreground)" : "var(--foreground)",
  border: `1px ${dashed ? "dashed" : "solid"} var(--input)`,
});

/* After "the": "the article", "the 3 articles". */
const counted = (n, one) => (n === 1 ? one : `${n} ${one}s`);

export function RuleStatus({ rule }) {
  const tones = eases(rule) ? EASING_TONE : RULE_TONE;
  return html`
    <span class="dc-cond-chip" style=${chip(tones[rule.status] || "--muted-foreground")}
          title=${STATUS_NOTE[rule.status] || ""}>${rule.status_label}</span>`;
}

/* Restricts or eases, beside the status: an outline for a restriction, the
   good-news colour for help. */
export function RuleEffect({ rule }) {
  return html`
    <span class="dc-cond-chip" style=${eases(rule) ? chip("--success") : outline(false)}
          title=${EFFECT_NOTE[rule.effect] || ""}>${rule.effect_label || rule.effect}</span>`;
}

export function RuleFit({ fit }) {
  return html`<span class="dc-cond-chip" style=${outline(fit !== "exact")}
                    title=${FIT_NOTE[fit] || ""}>${fit}</span>`;
}

/* The articles behind a rule, newest first, each with the sentences it was kept on.
   One sentence can bear out several things — the rule, where, where it stands — and
   the server lists it once with all of them. */
export function RuleArticles({ sources }) {
  if (!sources || !sources.length) return null;
  return html`
    <ol class="dc-cond-src-list">
      ${sources.map((s, i) => html`
        <li key=${s.url || i} class="dc-cond-src">
          <div class="dc-cond-src-head">
            ${s.url
              ? html`<a href=${s.url} target="_blank" rel="noopener noreferrer"
                        class="dc-cond-src-title">${s.title || s.publisher || "the article"} →</a>`
              : html`<span class="dc-cond-src-title">${s.title || s.publisher || "an article"}</span>`}
            ${s.publisher && html`<span>${s.publisher}</span>`}
            ${(s.published || s.read) && html`
              <span class="dc-num">${s.published || `read ${s.read}`}</span>`}
            <span>says it is ${s.status_label}</span>
          </div>
          ${s.quotes.map((q, j) => html`
            <blockquote key=${j} class="dc-cond-quote">
              “${q.text}”
              <span class="dc-cond-for">${q.for.join(" · ")}</span>
            </blockquote>`)}
        </li>`)}
    </ol>`;
}

function RuleFacts({ rule, why }) {
  const dates = [
    rule.effective_on && `in effect from ${rule.effective_on}`,
    rule.ends_on && `ends ${rule.ends_on}`,
  ].filter(Boolean);
  return html`
    <dl class="dc-cond-facts">
      ${why && html`<div><dt>Why it reaches this campus</dt><dd>${why}</dd></div>`}
      <div><dt>Covers</dt><dd>${rule.covers}</dd></div>
      ${!!dates.length && html`<div><dt>Dates</dt><dd class="dc-num">${dates.join(" · ")}</dd></div>`}
      ${!rule.market && rule.clears && html`<div><dt>Would clear it</dt><dd>${rule.clears}</dd></div>`}
      <div><dt>Reported</dt><dd class="dc-num">
        ${rule.first_reported && rule.first_reported !== rule.last_reported
          ? `${rule.first_reported} → ${rule.last_reported}` : rule.last_reported || "—"}</dd></div>
    </dl>`;
}

/* A project page's "Exposed to": one line per rule, opened for why, what would
   clear it and the articles. Drawn like the obstacle lines above it so the two read
   as lists of the same shape, in a section of its own so they are never one list. */
export function ExposureList({ exposures }) {
  return html`
    <ul class="dc-cond-list">
      ${exposures.map((e) => html`
        <li key=${e.id}>
          <details class="dc-cond">
            <summary>
              <${RuleStatus} rule=${e} />
              <span class="dc-cond-need">${e.need_label}</span>
              <span class="dc-cond-text">${e.title}<span class="dc-cond-where"> · ${e.where}</span></span>
              <${RuleFit} fit=${e.fit} />
              <span class="dc-cond-when dc-num" title="When it was last reported">${e.last_reported || ""}</span>
            </summary>
            <div class="dc-cond-body">
              <p class="dc-cond-summary">${e.summary}</p>
              <${RuleFacts} rule=${e} why=${e.why} />
              <${RuleArticles} sources=${e.sources} />
            </div>
          </details>
        </li>`)}
    </ul>`;
}

/* The projects one rule reaches, asked for when the rule is opened: a statewide rule
   reaches two hundred, and the list of rules should not carry them all. */
const REACH_SHOWN = 30;

function ReachedProjects({ id, api, onOpen }) {
  const [state, setState] = useState({ loading: true, rows: [], error: null });
  const [all, setAll] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api(`/api/rule?id=${id}`)
      .then((body) => { if (!cancelled) setState({ loading: false, rows: body.projects || [], error: null }); })
      .catch((err) => { if (!cancelled) setState({ loading: false, rows: [], error: err.message || "could not read it" }); });
    return () => { cancelled = true; };
  }, [id]);
  if (state.loading) return html`<${Skeleton} style=${{ height: 64 }} />`;
  if (state.error) return html`<div class="dc-wf-meta" style=${{ color: "var(--danger)" }}>${state.error}</div>`;
  if (!state.rows.length) return html`<div class="dc-wf-meta">It reaches no project in the tracker now.</div>`;
  const rows = all ? state.rows : state.rows.slice(0, REACH_SHOWN);
  const hidden = state.rows.length - rows.length;
  return html`
    <div style=${{ display: "grid", gap: 6 }}>
      <ul class="dc-cond-reach">
        ${rows.map((p) => html`
          <li key=${p.project_id}>
            <div style=${{ display: "grid", gap: 1, minWidth: 0 }}>
              <button type="button" class="dc-linkish dc-cond-reach-name" onClick=${() => onOpen(p.project_id)}>
                ${p.project}</button>
              <span class="dc-wf-meta">${[p.company, p.location, p.phase].filter(Boolean).join(" · ")}</span>
            </div>
            <${RuleFit} fit=${p.fit} />
            <span class="dc-cond-reach-why">${p.why}</span>
          </li>`)}
      </ul>
      ${(hidden > 0 || all) && html`
        <button type="button" class="dc-linkish" style=${{ justifySelf: "start", fontSize: 12.5 }}
                onClick=${() => setAll(!all)}>
          ${all ? `show only the first ${REACH_SHOWN}` : `show ${hidden} more (${state.rows.length} in all)`}</button>`}
    </div>`;
}

function RuleCard({ rule, api, onOpen }) {
  const [projectsOpen, setProjectsOpen] = useState(false);
  const [articlesOpen, setArticlesOpen] = useState(false);
  const articles = (rule.sources || []).length;
  return html`
    <article class="dc-wf-card">
      <header style=${{ display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between",
                        flexWrap: "wrap" }}>
        <div style=${{ display: "grid", gap: 2, minWidth: 0 }}>
          <span class="dc-wf-meta" style=${{ fontWeight: 600 }}>
            ${rule.where} · ${rule.kind_label} · ${rule.need_label}</span>
          <h3 class="dc-cond-title">${rule.title}</h3>
        </div>
        <div style=${{ display: "flex", gap: 6, alignItems: "center" }}>
          <${RuleEffect} rule=${rule} />
          <${RuleStatus} rule=${rule} />
        </div>
      </header>
      <p class="dc-cond-summary">${rule.summary}</p>
      <${RuleFacts} rule=${rule} />
      <div style=${{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        ${!rule.market && html`
          <button type="button" class="dc-linkish" aria-expanded=${projectsOpen} disabled=${!rule.projects}
                  onClick=${() => setProjectsOpen(!projectsOpen)}>
            ${rule.projects
              ? `${projectsOpen ? "hide the" : "the"} ${counted(rule.projects, "project")} it reaches`
              : "reaches no project"}</button>`}
        ${articles > 0 && html`
          <button type="button" class="dc-linkish" aria-expanded=${articlesOpen}
                  onClick=${() => setArticlesOpen(!articlesOpen)}>
            ${articlesOpen ? "hide the" : "the"} ${counted(articles, "article")} and ${articles === 1 ? "its" : "their"} sentences</button>`}
      </div>
      ${projectsOpen && html`<${ReachedProjects} id=${rule.id} api=${api} onOpen=${onOpen} />`}
      ${articlesOpen && html`<${RuleArticles} sources=${rule.sources} />`}
    </article>`;
}

function Heading({ figure, title, children }) {
  return html`
    <div style=${{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                      letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>${figure}</span>
      <h1 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                    letterSpacing: "-0.02em", lineHeight: 1.15 }}>${title}</h1>
      ${children && html`<p style=${{ margin: "2px 0 0", fontSize: 14, lineHeight: "22px",
                                      color: "var(--muted-foreground)", maxWidth: "78ch" }}>${children}</p>`}
    </div>`;
}

export function RulesView({ api, onOpen }) {
  const [payload, setPayload] = useState(null);
  const [failed, setFailed] = useState(null);
  const [state, setState] = useState("");
  const [cleared, setCleared] = useState(false);
  const [loading, setLoading] = useState(true);

  /* The filter is the server's (`?state=`), not done here: a state takes in the
     rules of the grid region it is mostly in, which is the match's own inference
     and not something to repeat in the browser. The last answer stays on screen
     while the next is fetched. */
  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams();
    if (state) params.set("state", state);
    if (cleared) params.set("cleared", "1");
    setLoading(true);
    api(`/api/rules${params.toString() ? `?${params}` : ""}`)
      .then((body) => { if (!cancelled) { setPayload(body); setFailed(null); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setFailed(err.message || "could not read the rules"); setLoading(false); } });
    return () => { cancelled = true; };
  }, [state, cleared]);

  const counts = payload?.counts;
  const rules = payload?.rules || [];
  const market = payload?.market || [];
  const tile = (label, value, hint, color) => html`
    <div class="dc-tile">
      <span class="dc-tile-label">${label}</span>
      <span class="dc-tile-value dc-num" style=${{ color: color || "var(--foreground)" }}>${value}</span>
      <span class="dc-tile-hint">${hint}</span>
    </div>`;

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gap: 24, padding: "26px 26px 60px", maxWidth: 920 }}>
      <${Heading} figure="fig. 01 — rules" title="Policies and laws we track">
        Each is a policy or law that affects many projects at once — a state's permit pause, a
        town's ban, a grid operator's rule — with the projects it affects. They are not counted
        as a project's obstacles.
      <//>

      ${failed && html`<${Alert} variant="warning"><div class="mrd-alert-desc">${failed}</div><//>`}
      ${!payload && !failed && html`
        <div style=${{ display: "grid", gap: 12 }}>
          ${[0, 1, 2].map((i) => html`<${Skeleton} key=${i} style=${{ height: 120 }} />`)}
        </div>`}

      ${payload && html`
        <div class="dc-tiles">
          ${tile("Rules", counts.rules,
            `${counts.restricts} restrict · ${counts.eases} ease${cleared
              ? ` · ${counts.standing} still standing` : ""}`)}
          ${tile("Projects restricted", counts.projects,
            `reached by a rule that makes building harder${counts.helped
              ? ` · ${counts.helped} helped by one that eases` : ""}`,
            counts.projects ? "var(--warning)" : undefined)}
          ${tile("Restrictions in force", counts.in_force, "in effect now",
            counts.in_force ? "var(--danger)" : undefined)}
          ${tile("Market", counts.market, "nationwide or whole-industry; flag no project")}
        </div>

        <div class="dc-band" style=${{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap",
                                       paddingBottom: 18, opacity: loading ? 0.6 : 1 }}>
          <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
            <span class="dc-wf-meta">state</span>
            <${Select} size="sm" value=${state} onChange=${(e) => setState(e.target.value)}>
              <option value="">every state</option>
              ${(payload.states || []).map((s) => html`
                <option key=${s.state} value=${s.state}>${s.name} (${s.rules})</option>`)}
            <//>
          </label>
          <${Switch} size="sm" label="Include lifted and voted down" checked=${cleared}
                     onCheckedChange=${(v) => setCleared(!!v)} />
        </div>

        ${!rules.length && html`
          <${EmptyState} variant="dashed" title="No rule for a place here"
                         description=${state
                           ? "No rule held applies to this state or the grid region it is mostly in."
                           : "No rule for a state, county, town or grid region has been read yet."} />`}
        <div style=${{ display: "grid", gap: 14 }}>
          ${rules.map((r) => html`<${RuleCard} key=${r.id} rule=${r} api=${api} onOpen=${onOpen} />`)}
        </div>

        <section class="dc-band" style=${{ display: "grid", gap: 12 }} aria-label="Market">
          <h2 class="dc-wf-h2">Market</h2>
          <p class="dc-wf-meta" style=${{ margin: 0, fontSize: 13, maxWidth: "78ch" }}>
            Rules for the whole country, or for one input every campus needs — a tariff on
            transformers, export rules for chips. They flag no project, because they touch every
            one; a campus hears of them when an article about that campus does.
          </p>
          ${!market.length && html`<div class="dc-wf-meta">None is standing.</div>`}
          <div style=${{ display: "grid", gap: 14 }}>
            ${market.map((r) => html`<${RuleCard} key=${r.id} rule=${r} api=${api} onOpen=${onOpen} />`)}
          </div>
        </section>
      `}
    </div>`;
}
