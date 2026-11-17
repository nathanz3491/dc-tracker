/* The spot check: a random sample of each night's changed values, for a person to
 * judge right or wrong (`tracker/spotcheck.py`).
 *
 * Drawn on the Admin page, above the accounts. The answer key only says a night broke
 * nothing already verified; this is the one measure of whether what the loop *writes*
 * is right, and it is only as good as the answers, so it is admins only — the server
 * re-checks on every route.
 *
 * What the server decides and this file only draws: which changes were drawn (at
 * random, once a night), the question each one asks (a value emptied asks whether the
 * old one was wrong), and the score with its interval. `unsure` is an answer, counted
 * apart from right and wrong.
 */

const html = htm.bind(React.createElement);
const { useState, useEffect, useCallback } = React;
const NS = window.MeridianDesignSystem_6e9015 || {};
const { Button, Card, Input, Alert } = NS;

const pad = { display: "grid", gap: 12, padding: "16px 20px" };
const quiet = { fontSize: 12, color: "var(--muted-foreground)" };

const VERDICTS = [["right", "Right"], ["wrong", "Wrong"], ["unsure", "Can't tell"]];

const pct = (x) => `${Math.round(x * 100)}%`;

function tallyLine(t) {
  const judged = t.right + t.wrong;
  const head = judged
    ? `${t.right} of ${judged} right (${pct(t.share)}, 95% interval ${pct(t.low)} to ${pct(t.high)})`
    : "nothing judged yet";
  const extra = [t.unsure && `${t.unsure} can't tell`, t.open && `${t.open} not yet answered`].filter(Boolean);
  return extra.length ? `${head}; ${extra.join(", ")}` : head;
}

function Check({ c, api, onSaved }) {
  const [note, setNote] = useState(c.note || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const answer = async (verdict) => {
    setBusy(true);
    setError(null);
    try {
      await api("/api/admin/checks/mark", { method: "POST", body: { id: c.id, verdict, note } });
      await onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return html`
    <div style=${{ display: "grid", gap: 6, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
      <div style=${{ display: "flex", flexWrap: "wrap", gap: "4px 10px", alignItems: "baseline" }}>
        ${c.project_id != null
          ? html`<a href=${`/projects/${c.project_id}`} target="_blank" rel="noopener"
                    style=${{ fontWeight: 600 }}>${c.label}</a>`
          : html`<span style=${{ fontWeight: 600 }}>${c.label} <span style=${quiet}>(since merged away)</span></span>`}
        <span class="dc-num" style=${quiet}>${c.what}: ${c.before_text} → ${c.after_text}</span>
      </div>
      <div style=${{ fontSize: 14 }}>${c.question}</div>
      ${c.quote && c.via && html`<div style=${quiet}>Reached through ${c.via}.</div>`}
      ${c.quote
        ? html`<blockquote style=${{ margin: 0, paddingLeft: 10, borderLeft: "2px solid var(--border)",
                                     fontSize: 13, color: "var(--muted-foreground)" }}>“${c.quote}”</blockquote>`
        : c.kind !== "emptied" && html`<div style=${quiet}>
            No sentence stands behind it${c.why ? ` — ${c.why}` : ""}${c.via ? ` (${c.via})` : ""}.</div>`}
      ${c.url && html`<a href=${c.url} target="_blank" rel="noopener noreferrer"
                         style=${{ ...quiet, overflowWrap: "anywhere" }}>${c.url}</a>`}
      <div style=${{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        ${VERDICTS.map(([v, label]) => html`
          <${Button} key=${v} size="sm" disabled=${busy}
                     variant=${c.verdict === v ? "secondary" : "outline"}
                     onClick=${() => answer(v)}>${label}<//>`)}
        <div style=${{ flex: "1 1 200px", minWidth: 0 }}>
          <${Input} value=${note} maxLength=${300} placeholder="why, in a line (optional)"
                    aria-label="Note" onChange=${(e) => setNote(e.target.value)} />
        </div>
      </div>
      ${c.verdict && html`<div style=${quiet}>Answered ${c.verdict} by ${c.checked_by}.</div>`}
      ${error && html`<div style=${{ color: "var(--danger)", fontSize: 13 }}>${error}</div>`}
    </div>`;
}

export function ChecksPanel({ api }) {
  const [night, setNight] = useState("");
  const [state, setState] = useState({ page: null, error: null });

  const load = useCallback(() => api(`/api/admin/checks${night ? `?night=${night}` : ""}`)
    .then((page) => setState({ page, error: null }))
    .catch((e) => setState({ page: null, error: e.message })), [api, night]);
  useEffect(() => { load(); }, [load]);

  const page = state.page;
  return html`
    <${Card}>
      <div style=${pad}>
        <div style=${{ display: "flex", flexWrap: "wrap", gap: "6px 14px", alignItems: "baseline" }}>
          <div style=${{ fontWeight: 600, fontSize: 15 }}>Spot check</div>
          ${page && page.nights.length > 0 && html`
            <select value=${page.night || ""} onChange=${(e) => setNight(e.target.value)}
                    aria-label="Which night">
              ${page.nights.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}
            </select>`}
        </div>
        <p style=${{ ...quiet, margin: 0, fontSize: 13, maxWidth: "78ch" }}>
          Each night the loop picks some of the values it changed, at random, for a person to judge.
          The share judged right estimates how much of what the loop writes is right — which the answer
          key cannot: it only says a night broke nothing already verified. Judge the value against the
          article and what you know of the campus, not only the quoted sentence.
        </p>
        ${state.error && html`<${Alert} variant="danger"><div class="mrd-alert-desc">${state.error}</div><//>`}
        ${page && html`
          <div class="dc-num" style=${{ fontSize: 13 }}>
            <div>Last 7 nights: ${tallyLine(page.week)}</div>
            <div style=${quiet}>Since the first sample: ${tallyLine(page.ever)}</div>
          </div>`}
        ${page && page.checks.length === 0 && html`
          <div style=${quiet}>No sample yet: the nightly loop draws one at the end of each night.</div>`}
        ${page && page.checks.map((c) => html`<${Check} key=${c.id} c=${c} api=${api} onSaved=${load} />`)}
      </div>
    <//>`;
}
