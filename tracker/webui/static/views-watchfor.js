/* What to watch for: every open obstacle on the projects you follow, and what
 * would clear it.
 *
 * The page the morning email's "see the full list" button opens, and the answer to
 * the question a reader is left with on a day when nothing moved: what is standing
 * between each of my projects and done, and what would the next good news look
 * like?
 *
 * What the server decides and this page only draws (`tracker/watchfor.py`):
 *   - every open obstacle is listed, however old — a four-month permit fight that
 *     is still unresolved is the thing most worth watching;
 *   - "would clear it" is the next milestone on the blocked track, from
 *     `tracks.NEXT_SIGNAL`, so the page never reasons about tracks itself;
 *   - unconfirmed obstacles are kept apart, labelled, and left out of the email.
 *
 * Kept out of app.js for the reason views-help.js is. `api` and `onOpen` are
 * passed in, because this file cannot import from app.js (app.js imports it).
 */

const html = htm.bind(React.createElement);
const { useState, useEffect, useMemo } = React;
const NS = window.MeridianDesignSystem_6e9015 || {};
const { Alert, EmptyState, Input, Skeleton } = NS;

const SEVERITY_TONE = { blocking: "--danger", material: "--warning", watch: "--muted-foreground" };
const RANK = { blocking: 2, material: 1, watch: 0 };

const LEVELS = [
  ["all", "every blocker"],
  ["material", "material and worse"],
  ["blocking", "blocking only"],
];

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

function openFor(days) {
  if (days == null) return "";
  if (days < 1) return "open since today";
  if (days < 60) return `open ${days} day${days === 1 ? "" : "s"}`;
  if (days < 730) return `open ${Math.floor(days / 30)} months`;
  return `open ${Math.floor(days / 365)} years`;
}

/* The five tracks as labelled cells: done, blocked, or still to come. */
function Tracks({ tracks }) {
  return html`
    <div class="dc-wf-tracks">
      ${tracks.map((t) => {
        const state = t.blocked ? "blocked" : t.complete ? "done" : t.status === "unknown" ? "todo" : "moving";
        const words = t.blocked
          ? "blocked"
          : t.complete
            ? "done"
            : t.status === "unknown"
              ? "nothing yet"
              : t.status.replace(/_/g, " ");
        return html`
          <span key=${t.track} class=${`dc-wf-track dc-wf-track--${state}`}
                title=${`${t.label}: ${words}`}>
            <b>${t.label}</b><span>${words}</span>
          </span>`;
      })}
    </div>`;
}

function Blocker({ blocker, muted }) {
  const [open, setOpen] = useState(false);
  return html`
    <li class="dc-wf-blocker" style=${{ opacity: muted ? 0.75 : 1 }}>
      <div style=${{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span style=${chip(SEVERITY_TONE[blocker.severity] || "--muted-foreground")}>${blocker.severity}</span>
        <b style=${{ fontWeight: 500 }}>${blocker.label}</b>
        ${blocker.track_label && html`<span class="dc-wf-meta">${blocker.track_label}</span>`}
        <span class="dc-wf-meta dc-num">${openFor(blocker.days_open)}</span>
        ${muted && html`<span style=${chip("--warning")}>unconfirmed</span>`}
      </div>
      <div style=${{ fontSize: 14, lineHeight: "22px" }}>${blocker.summary}</div>
      <div class="dc-wf-meta" style=${{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        ${blocker.since && html`<span class="dc-num">since ${blocker.since}</span>`}
        ${blocker.source_url && html`
          <a href=${blocker.source_url} target="_blank" rel="noopener noreferrer"
             style=${{ color: "var(--primary)" }}>${blocker.publisher || "source"} →</a>`}
        ${blocker.quote && html`
          <button type="button" class="dc-linkish" onClick=${() => setOpen(!open)}>
            ${open ? "hide the quote" : "the quote"}</button>`}
      </div>
      ${open && blocker.quote && html`
        <blockquote style=${{ margin: 0, paddingLeft: 12, borderLeft: "1px solid var(--border)",
                               fontSize: 13, color: "var(--muted-foreground)" }}>${blocker.quote}</blockquote>`}
    </li>`;
}

function ProjectCard({ project, level, onOpen }) {
  const [showHeld, setShowHeld] = useState(false);
  const blockers = project.blockers.filter((b) => RANK[b.severity] >= RANK[level === "all" ? "watch" : level]);
  const hidden = project.blockers.length - blockers.length;
  return html`
    <article class="dc-wf-card">
      <header style=${{ display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between",
                        flexWrap: "wrap" }}>
        <div style=${{ display: "grid", gap: 2 }}>
          <span class="dc-wf-meta" style=${{ fontWeight: 600 }}>
            ${project.company}${project.location ? ` · ${project.location}` : ""}
          </span>
          <button type="button" class="dc-linkish dc-wf-name" onClick=${() => onOpen(project.project_id)}>
            ${project.project}
          </button>
        </div>
        <div style=${{ display: "flex", gap: 6, alignItems: "center" }}>
          ${project.blockers.length
            ? html`<span style=${chip(SEVERITY_TONE[project.worst] || "--muted-foreground")}>
                ${project.blockers.length} open</span>`
            : html`<span style=${chip("--success")}>nothing open</span>`}
        </div>
      </header>

      <${Tracks} tracks=${project.tracks} />

      ${!!blockers.length && html`
        <ul class="dc-wf-list">
          ${blockers.map((b) => html`<${Blocker} key=${b.risk_id} blocker=${b} />`)}
        </ul>`}
      ${hidden > 0 && html`
        <div class="dc-wf-meta">${hidden} lower-severity blocker${hidden === 1 ? "" : "s"} hidden by the filter</div>`}

      ${!!project.signposts.length && html`
        <div class="dc-wf-signposts">
          ${project.signposts.map((s) => html`
            <div key=${s.track}>
              <b>${s.blocked ? "Would clear it" : "Next step"}:</b> ${s.track_label} — ${s.milestone_label}.
              <span style=${{ color: "var(--muted-foreground)" }}> Look for ${s.looks_like}.</span>
            </div>`)}
        </div>`}

      ${!!project.unconfirmed.length && html`
        <div>
          <button type="button" class="dc-linkish" style=${{ fontSize: 13 }}
                  onClick=${() => setShowHeld(!showHeld)}>
            ${showHeld ? "Hide" : "Show"} ${project.unconfirmed.length} unconfirmed —
            nobody could quote ${project.unconfirmed.length === 1 ? "it" : "them"}
          </button>
          ${showHeld && html`
            <ul class="dc-wf-list" style=${{ marginTop: 10 }}>
              ${project.unconfirmed.map((b) => html`<${Blocker} key=${b.risk_id} blocker=${b} muted=${true} />`)}
            </ul>`}
        </div>`}
    </article>`;
}

export function WatchForView({ api, onOpen, onGoto }) {
  const [payload, setPayload] = useState(null);
  const [failed, setFailed] = useState(null);
  const [level, setLevel] = useState("all");
  const [query, setQuery] = useState("");
  const [showClear, setShowClear] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api("/api/watch-for")
      .then((body) => { if (!cancelled) setPayload(body); })
      .catch((err) => { if (!cancelled) setFailed(err.message || "could not read what to watch for"); });
    return () => { cancelled = true; };
  }, []);

  const q = query.trim().toLowerCase();
  const projects = payload?.projects || [];
  const matches = (p) =>
    !q || [p.company, p.project, p.location, p.entry].filter(Boolean).join(" ").toLowerCase().includes(q);
  const blocked = useMemo(
    () => projects.filter((p) => p.blockers.length && matches(p) &&
      (level === "all" || p.blockers.some((b) => RANK[b.severity] >= RANK[level]))),
    [projects, q, level],
  );
  const clear = useMemo(
    () => projects.filter((p) => matches(p) && !blocked.includes(p)),
    [projects, blocked, q],
  );
  const counts = payload?.counts;
  const sev = counts?.severity || {};
  const tile = (label, value, hint, color) => html`
    <div class="dc-tile">
      <span class="dc-tile-label">${label}</span>
      <span class="dc-tile-value dc-num" style=${{ color: color || "var(--foreground)" }}>${value}</span>
      <span class="dc-tile-hint">${hint}</span>
    </div>`;

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gap: 24, padding: "26px 26px 60px",
                                            maxWidth: 920 }}>
      <${Heading} figure="fig. 01 — watch for" title="What to watch for on your projects">
        Every open obstacle on the projects you follow, however long it has been open, and the
        milestone that would clear it. The morning email carries the top of this list; on a day
        with no news it carries all of it.
      <//>

      ${failed && html`<${Alert} variant="warning"><div class="mrd-alert-desc">${failed}</div><//>`}
      ${!payload && !failed && html`
        <div style=${{ display: "grid", gap: 12 }}>
          ${[0, 1, 2].map((i) => html`<${Skeleton} key=${i} style=${{ height: 120 }} />`)}
        </div>`}

      ${payload && !projects.length && html`
        <${EmptyState} variant="dashed" title="You are not following any project yet"
                       description="Add a company or a project on the Updates page, and its blockers appear here." />
        <div><button type="button" class="dc-linkish" onClick=${() => onGoto("updates")}>Go to Updates →</button></div>`}

      ${payload && !!projects.length && html`
        <div class="dc-tiles">
          ${tile("Projects followed", counts.projects, "matched by your watchlist")}
          ${tile("Blocked", counts.blocked, `of ${counts.projects} have an open obstacle`,
                 counts.blocked ? "var(--warning)" : "var(--success)")}
          ${tile("Blocking or material", (sev.blocking || 0) + (sev.material || 0),
                 `${sev.blocking || 0} blocking · ${sev.material || 0} material · ${sev.watch || 0} to watch`,
                 sev.blocking ? "var(--danger)" : sev.material ? "var(--warning)" : undefined)}
          ${tile("Unconfirmed", counts.unconfirmed, "reported, but nobody could quote them")}
        </div>

        <div class="dc-band" style=${{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap",
                                       paddingBottom: 18 }}>
          <div class="dc-seg" aria-label="severity">
            ${LEVELS.map(([key, label]) => html`
              <button key=${key} type="button" class="dc-seg-btn" aria-pressed=${level === key}
                      onClick=${() => setLevel(key)}>${label}</button>`)}
          </div>
          <div style=${{ minWidth: 220, flex: "0 1 300px" }}>
            <${Input} size="sm" value=${query} placeholder="filter by company or project…"
                      onChange=${(e) => setQuery(e.target.value)} />
          </div>
        </div>

        ${!blocked.length && html`
          <${EmptyState} variant="dashed" title="Nothing open at this level"
                         description=${q ? "Nothing matches that filter." : "None of your projects has an obstacle this severe."} />`}

        <div style=${{ display: "grid", gap: 14 }}>
          ${blocked.map((p) => html`<${ProjectCard} key=${p.project_id} project=${p} level=${level} onOpen=${onOpen} />`)}
        </div>

        ${!!clear.length && html`
          <div class="dc-band" style=${{ paddingBottom: 0 }}>
            <button type="button" class="dc-linkish" onClick=${() => setShowClear(!showClear)}>
              ${showClear ? "Hide" : "Show"} ${clear.length} project${clear.length === 1 ? "" : "s"}
              with nothing open${level !== "all" ? " at this level" : ""}
            </button>
            ${showClear && html`
              <div style=${{ display: "grid", gap: 14 }}>
                ${clear.map((p) => html`<${ProjectCard} key=${p.project_id} project=${p} level=${level} onOpen=${onOpen} />`)}
              </div>`}
          </div>`}
      `}
    </div>`;
}
