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
  ["reported", "quoted", "A source wrote this down, and we checked the sentence is really in the article. The best we can do."],
  ["derived", "derived", "Looked up, not reported — county and coordinates come from Census data. Checkable, but nobody said it about this project."],
  ["unconfirmed", "待确认", "A model pulled this out and we could not find a sentence proving it. Kept so it can be checked, counts for nothing until then. Two different things land here — see below."],
  ["inferred", "inferred", "A model's opinion, worked out from the facts we hold. Reasoning, not evidence."],
  ["defaulted", "default", "Nobody said anything and the field cannot be blank, so it is showing the fallback."],
  ["missing", "null", "Empty. Usually the right answer, not a hole."],
  ["na", "n/a", "Empty, and it could not be anything else — built capacity on a project that has not broken ground."],
];

const VIEWS = [
  ["Projects", "Every project and every field, with the source behind each value. The best-filled columns show by default; the rest are one switch away. Click a row for the full evidence."],
  ["Map", "Roughly where they are — dots sit on the town, not the site. Bigger dot, more megawatts; a hollow ring means nobody said. Scroll or use +/− to zoom."],
  ["Capex", "The same projects grouped by who is paying rather than by where they are. Also where you merge duplicate rows, which matters most here because duplicates inflate a buyer's total."],
  ["Queue", "Articles we found and have not read. Reading one costs one LLM call. Underneath, the sites that would not let us in."],
  ["Coverage", "What is missing and what only looks missing, plus how many megawatts are stuck behind each kind of problem."],
  ["Commands", "Routines first — a named sequence that does several commands in the order they want doing. Under them, every command individually, read from the CLI itself so it cannot go stale."],
  ["Runs", "What has been run and exactly what it printed. A routine is one entry, not three."],
];

/* The four routines, and why the order in each is the order it is.
 *
 * Listed here as well as on the Commands view because this is where someone
 * comes when the one-line summary was not enough — and "why that order" is
 * exactly the question a sequence raises. */
const ROUTINES = [
  ["Catch up on the news", "sync → ingest geo → logic check. Geography is a free lookup, so deriving it after the read locates the rows that just arrived too. Contradictions come from new values, so the check goes last."],
  ["Deepen what we already have", "enrich → ingest geo → gaps. Grows the rows already tracked instead of finding new ones, then says what is still missing so the next run has a target."],
  ["Tidy the database", "duplicates → logic check → stats. Writes nothing. Finds the same-campus-twice rows that inflate a buyer's total, and the values that disagree with their own sources."],
  ["Prepare a report", "stats → capex → verify. Coverage first, because it sets how much the rest is worth."],
];

const COSTS = [
  ["sync", "The whole loop: find articles, read them, update, list. One call per article, 25 by default."],
  ["enrich", "Throws everything at one project until it stops finding anything."],
  ["ingest crawl", "Reads articles. One call each."],
  ["infer", "Asks a model what is holding a project up."],
  ["search", "Adds web search to the loop. Needs a search key too."],
  ["point", "Goes and gets one named data center. One call to identify it, then whichever branch it takes."],
  ["logic check", "Free on its own. Spends only with --read, which has a model examine the rows it flagged."],
  ["ingest edgar", "Reads SEC filings. One call per filing; --per-company is the dial."],
  ["the briefing panel", "The written summary at the top of a project drawer. One call, then cached — reopening the same row is free until the row changes."],
];

/* Destruction is a different loss from spending, and it gets the same ritual.
 * There is exactly one entry, which is the point: everything else in the console
 * either costs money or costs nothing, and this costs a row. */
const DESTRUCTIVE = [
  ["merge", "Folds duplicate rows into one and deletes the rest. Their sources, milestones and problems move across first, and the surviving row is recalculated from all of them — so nothing anybody wrote is lost. But the rows are gone and there is no undo."],
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
                        letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>fig. 06 — help</span>
        <h1 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                      letterSpacing: "-0.02em", lineHeight: 1.15 }}>Three things worth knowing</h1>
        <p style=${{ margin: "2px 0 0", fontSize: 14, lineHeight: "22px",
                     color: "var(--muted-foreground)", maxWidth: "78ch" }}>
          The whole tool is built on one rule: what a model says is not automatically a fact. That is
          why almost every number on screen tells you where it came from.
        </p>
      </div>

      <${Section} title="1. Where a value came from"
        description="Every filled-in value is underlined, and the underline says who vouches for it. The line style differs too, not just the colour, so it still reads if you cannot tell them apart.">
        ${TIERS.map(([tier, label, text]) => html`
          <${Row} key=${tier} left=${html`
            <div style=${{ display: "flex", alignItems: "center", gap: 8 }}>
              <span class=${`dc-v dc-v--${tier}`} style=${{ cursor: "default", fontSize: 13 }}>abc</span>
              <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12 }}>${label}</span>
            </div>`}>${text}<//>`)}
      <//>

      <${Section} title="2. How far along a project is"
        description="Not one ladder — five, running side by side. A project can be nearly finished on one and not started on another.">
        ${(data.tracks || []).map((t) => html`
          <${Row} key=${t.key} left=${html`<span style=${{ fontSize: 14, fontWeight: 600 }}>${t.label}</span>`}>
            ${t.milestones.join(" → ")}
          <//>`)}
        <div style=${{ padding: "12px 20px 16px", borderTop: "1px solid var(--border)" }}>
          <p style=${{ margin: 0, fontSize: 13, lineHeight: "20px", color: "var(--muted-foreground)",
                       maxWidth: "78ch" }}>
            A campus can own its land outright and still wait four years for a grid connection. We never
            assume ${html`<b>power</b>`} from the others, even when the building is clearly finished —
            builders routinely get ahead of the grid, and a finished shell waiting on a substation is the
            most useful thing in here. ${html`<i>implied</i>`} means we worked a milestone out from a
            later one — you cannot build on land you do not control — and a deduction is not a source,
            so it is labelled.
          </p>
        </div>
      <//>

      <${Section} title="3. How much to believe a row"
        description="A score out of 3, worked out fresh from the sources every time rather than stored.">
        <${Row} left=${html`<span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>0 – 1</span>`}>
          Needs someone to look at it. ${html`<code class="mrd-code">tracker review</code>`} lists these.
        <//>
        <${Row} left=${html`<span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>2</span>`}>
          One good source, or a few weak ones agreeing.
        <//>
        <${Row} left=${html`<span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>3</span>`}>
          Two separate websites say the same thing.
          ${html`<b>One source can never reach 3</b>`}, however good — two articles on the same site
          still count as one voice.
        <//>
      <//>

      <${Section} title="What each view is for">
        ${VIEWS.map(([name, text]) => html`
          <${Row} key=${name} left=${html`<span style=${{ fontSize: 14, fontWeight: 600 }}>${name}</span>`}>
            ${text}
          <//>`)}
      <//>

      <${Section} title="Two different things look 待确认"
        description="Same amber underline, opposite fixes. One needs another source; the other needs correcting.">
        <${Row} left=${html`<span style=${{ fontSize: 14, fontWeight: 600 }}>Nothing backs it</span>`}>
          The usual case. A model gave us the number and we could not find a sentence in the article that
          says it. Kept so it can be checked later. Fix: find another source.
        <//>
        <${Row} left=${html`<span style=${{ fontSize: 14, fontWeight: 600 }}>Wrong project</span>`}>
          Rarer, and more interesting. The number really is in the article — it just belongs to something
          bigger. "OpenAI's $500 billion Stargate" turns up in a piece about one 1,167 MW site. We catch
          it by comparing the money to the megawatts and flag it in red. Fix: correct it. Looking for a
          source will find one, and it will still be wrong.
        <//>
      <//>

      <${Section} title="Routines"
        description="A named sequence, run as one job with one log. Stops at the first step that genuinely fails — though a step like duplicates exits unhappy when it finds something, which is an answer rather than a breakage, and the run carries on.">
        ${ROUTINES.map(([name, text]) => html`
          <${Row} key=${name} left=${html`<span style=${{ fontSize: 13 }}>${name}</span>`}>${text}<//>`)}
      <//>

      <${Section} title="What costs money"
        description="These spend LLM tokens. Everything else is free. None of them can be started by a single click: the button asks again and says what it will cost.">
        ${COSTS.map(([cmd, text]) => html`
          <${Row} key=${cmd} left=${html`
            <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>${cmd}</span>`}>${text}<//>`)}
        <div style=${{ padding: "12px 20px 16px", borderTop: "1px solid var(--border)" }}>
          <p style=${{ margin: 0, fontSize: 13, lineHeight: "20px", color: "var(--muted-foreground)",
                       maxWidth: "78ch" }}>
            Free things to do first: ${html`<code class="mrd-code">discover</code>`} collects articles
            without reading them, the Queue lets you skip the junk,
            ${html`<code class="mrd-code">ingest geo</code>`} fills in counties and coordinates, and
            ${html`<code class="mrd-code">--dry-run</code>`} shows what any command would do for nothing.
          </p>
        </div>
      <//>

      <${Section} title="What cannot be undone"
        description="A different loss, and a heavier ritual: type the command's name out, or it will not start. Spending money can be decided again tomorrow; this cannot.">
        ${DESTRUCTIVE.map(([cmd, text]) => html`
          <${Row} key=${cmd} left=${html`
            <span style=${{ fontFamily: "var(--font-mono)", fontSize: 13 }}>${cmd}</span>`}>${text}<//>`)}
      <//>

      <${Section} title="Keyboard">
        <${Row} left=${kbd("Esc")}>Close the detail drawer.<//>
        <${Row} left=${html`${kbd("Enter")} ${kbd("Space")}`}>Open the focused row.<//>
        <${Row} left=${kbd("Tab")}>Move through rows, filters and controls in reading order.<//>
      <//>

      <p style=${{ margin: "4px 0 0", fontSize: 13, lineHeight: "20px",
                   color: "var(--muted-foreground)", maxWidth: "78ch" }}>
        Installing, API keys and how the data gets in are in the README and
        ${html`<code class="mrd-code">docs/</code>`}. Kept in one place so the two cannot end up saying
        different things.
      </p>
    </div>`;
}
