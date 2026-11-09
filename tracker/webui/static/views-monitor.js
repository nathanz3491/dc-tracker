/* Monitor: what you follow, and what stands in each project's way.
 *
 * Two halves. The top is **the list itself** — add a company or a project, drop
 * one, or follow everything — which used to sit on Updates and moved here so that
 * page could lead with what changed. Pressing ☆ on a Projects row adds to the same
 * list. The rest is **every open obstacle reported in the last two months** on the
 * projects that list reaches, and the milestone that would clear it: the page the
 * morning email's button opens, and the answer to the question a reader is left
 * with on a day when nothing moved.
 *
 * What the server decides and this page only draws (`tracker/watchfor.py`):
 *   - only obstacles reported in the last two months are listed; older open ones
 *     are counted, so the page says they exist without presenting them as current;
 *   - "would clear it" is the next milestone on the blocked track, from
 *     `tracks.NEXT_SIGNAL`, so the page never reasons about tracks itself;
 *   - unconfirmed obstacles are kept apart, labelled, and left out of the email;
 *   - rules for a place — a state's permit pause, a town's ban — that reach the
 *     followed projects are a section of their own, each rule once with the
 *     projects it reaches and what would clear it. A rule makes no project
 *     "blocked" and is counted on no obstacle tile: it reaches a campus because of
 *     where it is, not because anything was reported about the campus.
 *
 * Kept out of app.js so a reader of one view need not scroll past another. `api`, `onOpen` and the
 * list editor are passed in, because this file cannot import from app.js (app.js
 * imports it). The rule chips are `views-rules.js`'s, shared with the Rules view.
 */

import { RuleFit, RuleStatus, eases } from "/static/views-rules.js";

const html = htm.bind(React.createElement);
const { useState, useEffect, useMemo, useCallback } = React;
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

function ProjectCard({ project, level, onOpen, onOpenCompany, onRules }) {
  const [showHeld, setShowHeld] = useState(false);
  const blockers = project.blockers.filter((b) => RANK[b.severity] >= RANK[level === "all" ? "watch" : level]);
  const hidden = project.blockers.length - blockers.length;
  return html`
    <article class="dc-wf-card">
      <header style=${{ display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between",
                        flexWrap: "wrap" }}>
        <div style=${{ display: "grid", gap: 2 }}>
          <span class="dc-wf-meta" style=${{ fontWeight: 600 }}>
            ${project.company_slug && onOpenCompany
              ? html`<button type="button" class="dc-linkish" style=${{ font: "inherit", fontWeight: 600 }}
                             onClick=${() => onOpenCompany(project.company_slug)}>${project.company}</button>`
              : project.company}${project.location ? ` · ${project.location}` : ""}
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

      ${/* A pointer, not a list: each rule is drawn once, under "Rules for their
            places", with every project it reaches. Restricting and easing rules are
            named apart, so help never reads as one more thing in the way. */ ""}
      ${!!(project.rules || []).length && html`
        <div class="dc-wf-meta">
          ${(() => {
            const named = (rows) => rows.map((r) => `${r.title} (${r.status_label})`).join("; ");
            const hard = project.rules.filter((r) => !eases(r));
            const help = project.rules.filter(eases);
            return [
              hard.length && `Also in a place where ${hard.length === 1 ? "a rule restricts" : `${hard.length} rules restrict`} building: ${named(hard)}.`,
              help.length && `Helped by ${help.length === 1 ? "a rule" : `${help.length} rules`} for its place: ${named(help)}.`,
            ].filter(Boolean).join(" ");
          })()}
          ${onRules && html`${" "}<button type="button" class="dc-linkish" style=${{ fontSize: 12 }}
                                          onClick=${onRules}>See the rules ↓</button>`}
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

/* One rule for a place, once, with the followed projects it reaches.
 *
 * What would clear a restriction is the same for every one of them — the rule
 * lifted or voted down, or for a rule on projects with no permit yet, a project's
 * permit approved first — so it is said once, in the signpost box the obstacle
 * cards use for the milestone that would clear a track. A rule that eases building
 * has no such box: there is nothing to clear. */
function RuleCard({ rule, matches, onOpen, onGoto }) {
  const projects = rule.projects.filter(matches);
  return html`
    <article class="dc-wf-card">
      <header style=${{ display: "flex", gap: 12, alignItems: "flex-start", justifyContent: "space-between",
                        flexWrap: "wrap" }}>
        <div style=${{ display: "grid", gap: 2, minWidth: 0 }}>
          <span class="dc-wf-meta" style=${{ fontWeight: 600 }}>
            ${rule.where} · ${rule.need_label} · covers ${rule.covers}</span>
          <span class="dc-cond-title">${rule.title}</span>
        </div>
        <${RuleStatus} rule=${rule} />
      </header>
      <ul class="dc-cond-reach">
        ${projects.map((p) => html`
          <li key=${p.project_id}>
            <div style=${{ display: "grid", gap: 1, minWidth: 0 }}>
              <button type="button" class="dc-linkish dc-cond-reach-name" onClick=${() => onOpen(p.project_id)}>
                ${p.project}</button>
              <span class="dc-wf-meta">${[p.company, p.location].filter(Boolean).join(" · ")}</span>
            </div>
            <${RuleFit} fit=${p.fit} />
            <span class="dc-cond-reach-why">${p.why}</span>
          </li>`)}
      </ul>
      ${!eases(rule) && rule.clears && html`
        <div class="dc-wf-signposts"><div><b>Would clear it:</b> ${rule.clears}.</div></div>`}
      <div class="dc-wf-meta" style=${{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        ${rule.reported && html`<span class="dc-num">last reported ${rule.reported}</span>`}
        ${rule.source_url && html`
          <a href=${rule.source_url} target="_blank" rel="noopener noreferrer"
             style=${{ color: "var(--primary)" }}>${rule.publisher || "source"} →</a>`}
        ${onGoto && html`<button type="button" class="dc-linkish" style=${{ fontSize: 12 }}
                                 onClick=${() => onGoto("rules")}>every rule →</button>`}
      </div>
    </article>`;
}

export function MonitorView({ api, onOpen, onGoto, onOpenCompany, ListEditor, projects: allProjects, onListChanged }) {
  const [payload, setPayload] = useState(null);
  const [failed, setFailed] = useState(null);
  const [level, setLevel] = useState("all");
  const [query, setQuery] = useState("");
  const [showClear, setShowClear] = useState(false);

  /* Re-read after every edit to the list: which projects are followed is the
     server's answer (`watchlist.resolve`), not something to patch in place. */
  const load = useCallback(
    () => api("/api/monitor")
      .then((body) => { setPayload(body); setFailed(null); })
      .catch((err) => setFailed(err.message || "could not read what you monitor")),
    [],
  );
  useEffect(() => { load(); }, [load]);
  const changed = useCallback(async () => {
    await load();
    if (onListChanged) await onListChanged();
  }, [load, onListChanged]);

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
  /* The text filter narrows the rules to the projects it keeps; the severity filter
     does not apply to them, because a rule has no severity of its own. */
  const rules = useMemo(
    () => (payload?.rules || []).filter((r) => r.projects.some(matches)),
    [payload, q],
  );
  const restricting = rules.filter((r) => !eases(r));
  const easing = rules.filter(eases);
  const toRules = () =>
    document.getElementById("monitor-rules")?.scrollIntoView({ behavior: "smooth", block: "start" });
  const tile = (label, value, hint, color) => html`
    <div class="dc-tile">
      <span class="dc-tile-label">${label}</span>
      <span class="dc-tile-value dc-num" style=${{ color: color || "var(--foreground)" }}>${value}</span>
      <span class="dc-tile-hint">${hint}</span>
    </div>`;

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gap: 24, padding: "26px 26px 60px",
                                            maxWidth: 920 }}>
      <${Heading} figure="fig. 01 — monitor" title="What you monitor">
        The companies and projects you follow, and every open obstacle on them that was
        reported in the last two months, with the milestone that would clear it. The morning
        email carries the top of this list; on a day with no news it carries all of it.
      <//>

      <section class="dc-band" style=${{ display: "grid", gap: 12 }} aria-label="Your list">
        <div style=${{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
          <h2 class="dc-wf-h2">Your list</h2>
          ${payload && html`<span class="dc-wf-meta dc-num">
            ${(payload.watchlist || []).length} entr${(payload.watchlist || []).length === 1 ? "y" : "ies"}
            · ${projects.length} project${projects.length === 1 ? "" : "s"}</span>`}
          <button type="button" class="dc-linkish" style=${{ fontSize: 12 }}
                  onClick=${() => onGoto("projects")}>or press ☆ on any project →</button>
        </div>
        ${payload && ListEditor && html`<${ListEditor} payload=${payload} projects=${allProjects}
                                                       onChanged=${changed} onOpen=${onOpen}
                                                       onOpenCompany=${onOpenCompany} />`}
      </section>

      ${failed && html`<${Alert} variant="warning"><div class="mrd-alert-desc">${failed}</div><//>`}
      ${!payload && !failed && html`
        <div style=${{ display: "grid", gap: 12 }}>
          ${[0, 1, 2].map((i) => html`<${Skeleton} key=${i} style=${{ height: 120 }} />`)}
        </div>`}

      ${payload && !projects.length && html`
        <${EmptyState} variant="dashed" title="You are not following any project yet"
                       description="Add a company or a project above, or press ☆ on a row of Projects, and its obstacles appear here." />`}

      ${payload && !!projects.length && html`
        <div class="dc-tiles">
          ${tile("Projects followed", counts.projects, "matched by your watchlist")}
          ${tile("Blocked", counts.blocked, `of ${counts.projects} have an obstacle reported in two months`,
                 counts.blocked ? "var(--warning)" : "var(--success)")}
          ${tile("Blocking or material", (sev.blocking || 0) + (sev.material || 0),
                 `${sev.blocking || 0} blocking · ${sev.material || 0} material · ${sev.watch || 0} to watch`,
                 sev.blocking ? "var(--danger)" : sev.material ? "var(--warning)" : undefined)}
          ${tile("Unconfirmed", counts.unconfirmed, "reported, but nobody could quote them")}
          ${!!(counts.rules || counts.easing) && html`
            <button type="button" class="dc-tile" onClick=${toRules} style=${{ textAlign: "left" }}>
              <span class="dc-tile-label">Rules for their places</span>
              <span class="dc-tile-value dc-num">${counts.rules}</span>
              <span class="dc-tile-hint">${[
                `make building harder for ${counts.exposed} of your ${counts.projects}`,
                counts.easing && `${counts.easing} more ${counts.easing === 1 ? "eases" : "ease"} it for ${counts.helped}`,
                "not obstacles, so not counted as blocked",
              ].filter(Boolean).join(" · ")}</span>
            </button>`}
        </div>

        ${counts.older > 0 && html`
          <div class="dc-wf-meta">
            ${counts.older} older open obstacle${counts.older === 1 ? " is" : "s are"} not listed: nobody has
            reported ${counts.older === 1 ? "it" : "them"} in ${Math.round((counts.window_days || 60) / 30)} months,
            so ${counts.older === 1 ? "it is" : "they are"} history rather than something to watch. The project's
            own page still has ${counts.older === 1 ? "it" : "them"}.
          </div>`}

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
          ${blocked.map((p) => html`<${ProjectCard} key=${p.project_id} project=${p} level=${level} onOpen=${onOpen} onOpenCompany=${onOpenCompany} onRules=${toRules} />`)}
        </div>

        ${!!clear.length && html`
          <div class="dc-band" style=${{ paddingBottom: 0 }}>
            <button type="button" class="dc-linkish" onClick=${() => setShowClear(!showClear)}>
              ${showClear ? "Hide" : "Show"} ${clear.length} project${clear.length === 1 ? "" : "s"} with nothing open${level !== "all" ? " at this level" : ""}
            </button>
            ${showClear && html`
              <div style=${{ display: "grid", gap: 14 }}>
                ${clear.map((p) => html`<${ProjectCard} key=${p.project_id} project=${p} level=${level} onOpen=${onOpen} onOpenCompany=${onOpenCompany} onRules=${toRules} />`)}
              </div>`}
          </div>`}

        ${!!(payload.rules || []).length && html`
          <section id="monitor-rules" class="dc-band" style=${{ display: "grid", gap: 12, scrollMarginTop: 80 }}
                   aria-label="Rules for their places">
            <h2 class="dc-wf-h2">Rules for their places</h2>
            <p class="dc-wf-meta" style=${{ margin: 0, fontSize: 13, maxWidth: "78ch" }}>
              Rules that apply to a place rather than to one campus — a state's permit pause, a
              town's ban, a grid operator's rule — and which of your projects each reaches. None is
              an obstacle reported about these campuses, so none makes a project blocked above.
              <b> exact</b> means the state or county matches; <b>likely</b> means part of the match
              is inferred — the town's limits, the grid region or the size.
            </p>
            ${!rules.length && html`<div class="dc-wf-meta">Nothing matches that filter.</div>`}
            <div style=${{ display: "grid", gap: 14 }}>
              ${restricting.map((r) => html`<${RuleCard} key=${r.condition_id} rule=${r} matches=${matches}
                                                         onOpen=${onOpen} onGoto=${onGoto} />`)}
            </div>
            ${!!easing.length && html`
              <h3 class="dc-cond-subhead">Helped by</h3>
              <p class="dc-wf-meta" style=${{ margin: 0, fontSize: 13, maxWidth: "78ch" }}>
                Rules that make building easier where these projects are — a tax exemption,
                building allowed by right. Nothing to clear.
              </p>
              <div style=${{ display: "grid", gap: 14 }}>
                ${easing.map((r) => html`<${RuleCard} key=${r.condition_id} rule=${r} matches=${matches}
                                                      onOpen=${onOpen} onGoto=${onGoto} />`)}
              </div>`}
          </section>`}
      `}
    </div>`;
}
