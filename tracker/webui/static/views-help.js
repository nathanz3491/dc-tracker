/* The Help view.
 *
 * Authored as components rather than rendered from markdown: no parser to
 * vendor, and the tier swatches can be the real ones from app.css instead of a
 * picture of them — if the palette changes, this page changes with it.
 *
 * Scope is what someone needs to not misread the data. The operational detail
 * (installing, API keys, the ingest paths) belongs in the README, and this says
 * so rather than duplicating it and drifting.
 */

const html = htm.bind(React.createElement);
const NS = window.MeridianDesignSystem_6e9015 || {};
const { Card, CardHeader, CardTitle, CardDescription } = NS;

const TIERS = [
  ["reported", "quoted", "A verbatim sentence in a fetched article supports it, and that sentence was checked against the article text. The strongest thing this tool can say."],
  ["derived", "derived", "Computed from reference data — county and coordinates come from a US Census lookup. Deterministic and checkable, but nobody said it about this project."],
  ["unconfirmed", "待确认", "A model extracted it and no quotable sentence backs it up. Kept rather than deleted, because deleting cost 194 values across 92 projects — but never counted as a fact."],
  ["inferred", "inferred", "A model's judgement over the recorded facts, from `tracker infer`. Reasoning, not evidence."],
  ["defaulted", "default", "Nobody stated anything. The column is NOT NULL, so the schema default is sitting there. Distinct from 待确认, which claims a source tried."],
  ["missing", "null", "No value. Often the correct answer rather than a gap."],
  ["na", "n/a", "No value, and the field cannot apply to this row — built capacity on a project that has not broken ground."],
];

const VIEWS = [
  ["Projects", "Every tracked field, with its provenance. Columns are ordered by measured coverage and the sparse ones are behind a switch. Click a row for the evidence."],
  ["Map", "Where they are. Positions are place or county centroids — the centre of the town, not the site. Bubble area is cited capacity; a dashed hollow ring means nobody cited one. Scroll or use +/− to zoom; bubbles keep their size so overlapping clusters pull apart."],
  ["Queue", "Headlines discovery found and nothing has read yet. Crawl reads one for one LLM call. Below it, the hosts that would not answer."],
  ["Coverage", "Per-field coverage against honest denominators, the required-project list, and how much capacity sits behind each kind of obstacle."],
  ["Commands", "The CLI, read out of the CLI itself. Every flag with its real type and default."],
  ["Runs", "What has been run and exactly what it printed, colour and all."],
];

const COSTS = [
  ["sync", "discover → extract → refresh → list. One LLM call per article, capped by --limit (25 by default)."],
  ["enrich", "Throws every retrieval method at one project until a round stops paying."],
  ["ingest crawl", "Reads articles. One call each."],
  ["infer", "Asks a reasoning model what is obstructing a project."],
  ["search", "Folds web search into the loop. Needs a search key as well."],
];

function Section({ title, description, children }) {
  return html`
    <${Card}>
      <${CardHeader}>
        <${CardTitle}>${title}<//>
        ${description && html`<${CardDescription}>${description}<//>`}
      <//>
      <div style=${{ display: "grid", gap: 0 }}>${children}</div>
    <//>`;
}

function Row({ left, children }) {
  return html`
    <div style=${{ display: "grid", gridTemplateColumns: "150px minmax(0,1fr)", gap: 14,
                   alignItems: "baseline", padding: "11px 20px", borderTop: "1px solid var(--border)" }}
         class="dc-gap-row">
      <div>${left}</div>
      <div style=${{ fontSize: 13, lineHeight: "19px", color: "var(--muted-foreground)" }}>${children}</div>
    </div>`;
}

export function HelpView({ data }) {
  const kbd = (key) => html`<kbd class="mrd-kbd">${key}</kbd>`;
  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)",
                                           gap: 16, padding: "22px 26px 60px" }}>
      <div style=${{ display: "flex", flexDirection: "column", gap: 5 }}>
        <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                        letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>fig. 07 — help</span>
        <h1 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                      letterSpacing: "-0.02em", lineHeight: 1.15 }}>Three ideas, and then the rest is obvious</h1>
        <p style=${{ margin: "2px 0 0", fontSize: 14, lineHeight: "22px",
                     color: "var(--muted-foreground)", maxWidth: "78ch" }}>
          This tool exists because a model's answer is not a fact. Almost everything on screen is shaped
          by that one rule, so it is worth five minutes.
        </p>
      </div>

      <${Section} title="1. Where a value came from"
        description="Every non-null field carries a tier. The underline says which — and the six are told apart by line style as well as colour, because colour alone is never a state signal.">
        ${TIERS.map(([tier, label, text]) => html`
          <${Row} key=${tier} left=${html`
            <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
              <span class=${`dc-v dc-v--${tier}`} style=${{ cursor: "default", fontSize: 13 }}>abc</span>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12 }}>${label}</span>
            </div>`}>${text}<//>`)}
      <//>

      <${Section} title="2. Where a project has got to"
        description="Progress is not one ladder. It is five tracks that advance independently, and a project can be far along one while stuck at the start of another.">
        ${(data.tracks || []).map((t) => html`
          <${Row} key=${t.key} left=${html`<span style=${{ fontSize: 14, fontWeight: 600 }}>${t.label}</span>`}>
            ${t.milestones.join(" → ")}
          <//>`)}
        <div style=${{ padding: "12px 20px 16px", borderTop: "1px solid var(--border)" }}>
          <p style=${{ margin: 0, fontSize: 13, lineHeight: "20px", color: "var(--muted-foreground)",
                       maxWidth: "78ch" }}>
            A campus can own its land outright and still be four years deep in an interconnection queue.
            The ${html`<b>power</b>`} track is never inferred from the others, even when construction is
            clearly finished: building ahead of power is routine, and a completed shell waiting on a
            substation is the single most valuable signal in this dataset. A milestone marked
            ${html`<i>implied</i>`} was deduced from a later one — you cannot pour a building on land you
            do not control — and a deduction is not a citation, which is why it is labelled.
          </p>
        </div>
      <//>

      <${Section} title="3. How much to believe the row"
        description="Confidence 0–3, recomputed from the citations every time rather than stored.">
        <${Row} left=${html`<span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>0 – 1</span>`}>
          Needs a human. These are what ${html`<code class="mrd-code">tracker review</code>`} lists.
        <//>
        <${Row} left=${html`<span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>2</span>`}>
          One solid source, or several weak ones agreeing.
        <//>
        <${Row} left=${html`<span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>3</span>`}>
          Corroborated across independent domains. ${html`<b>One source can never reach 3</b>`}, however
          authoritative — independence is counted by domain, not by row.
        <//>
      <//>

      <${Section} title="What each view is for">
        ${VIEWS.map(([name, text]) => html`
          <${Row} key=${name} left=${html`<span style=${{ fontSize: 14, fontWeight: 600 }}>${name}</span>`}>
            ${text}
          <//>`)}
      <//>

      <${Section} title="What costs money"
        description="Five commands spend LLM tokens. Everything else is free, and the console makes you type the command's name before it will start one of these.">
        ${COSTS.map(([cmd, text]) => html`
          <${Row} key=${cmd} left=${html`
            <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>${cmd}</span>`}>${text}<//>`)}
        <div style=${{ padding: "12px 20px 16px", borderTop: "1px solid var(--border)" }}>
          <p style=${{ margin: 0, fontSize: 13, lineHeight: "20px", color: "var(--muted-foreground)",
                       maxWidth: "78ch" }}>
            Free ways to prepare before spending: ${html`<code class="mrd-code">discover</code>`} finds
            candidates without reading them, the Queue lets you drop the noise first,
            ${html`<code class="mrd-code">ingest geo</code>`} fills county and coordinates from Census
            data, and ${html`<code class="mrd-code">--dry-run</code>`} previews any run for nothing.
          </p>
        </div>
      <//>

      <${Section} title="Keyboard">
        <${Row} left=${kbd("Esc")}>Close the detail drawer.<//>
        <${Row} left=${html`${kbd("Enter")} ${kbd("Space")}`}>Open the focused row.<//>
        <${Row} left=${kbd("Tab")}>Move through rows, filters and controls in reading order.<//>
      <//>

      <p style=${{ margin: "4px 0 0", fontSize: 13, lineHeight: "20px",
                   color: "var(--muted-foreground)", maxWidth: "78ch" }}>
        Installing, API keys, the ingest paths and the reasoning behind the schema are in the
        project README and in ${html`<code class="mrd-code">docs/</code>`}. They are not repeated here so
        that they cannot disagree.
      </p>
    </div>`;
}
