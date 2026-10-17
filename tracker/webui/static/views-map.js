/* Map: where the campuses are, how big, and how far along.
 *
 * The geometry is the Census state boundaries (us-atlas TopoJSON, public domain),
 * projected with d3.geoAlbersUsa — nothing here is freehand. The honesty rules:
 *   - a dot sits on the centre of the town or county a campus is filed under,
 *     never on the site; almost nobody publishes an address;
 *   - a dot's size is its capacity band, and a campus whose size nobody published
 *     gets a hollow dashed ring — never a guessed radius, never a zero;
 *   - zoom scales the country and leaves the dots their size, because the reason
 *     to zoom is that Northern Virginia's dots sit on top of each other, and dots
 *     that grew with the map would stay exactly as overlapped.
 *
 * What the server decides and this page only draws (`webui/mapsummary.py`, at
 * `/api/map`): the totals, the stage counts and each state's figures, counted the
 * way the Capex page counts — paused and cancelled campuses left out, one row per
 * suspected duplicate — and which duplicate rows not to draw. The dots are the
 * console's own projects, so a dot opens its campus.
 *
 * Kept out of app.js so a reader of one view need not scroll past another. `api`,
 * `onOpen` and `Eyebrow` are passed in, because this file cannot import from app.js
 * (app.js imports it).
 */

const html = htm.bind(React.createElement);
const { useState, useEffect, useMemo, useRef } = React;

const ATLAS = "/static/vendor/us-states-10m.json";
let atlasPromise = null;
// A failed load is not kept: a promise that rejected once would be the answer for
// the life of the page, long after the network came back.
const loadAtlas = () =>
  (atlasPromise = atlasPromise || fetch(ATLAS)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .catch((e) => { atlasPromise = null; throw e; }));

const STAGES = ["announced", "permitting", "construction", "operational", "paused", "cancelled"];
const STAGE_LABEL = {
  announced: "Announced", permitting: "Permitting", construction: "Construction",
  operational: "Operational", paused: "Paused", cancelled: "Cancelled",
};

/* Capacity bands, as (key, lower bound in MW, radius at full width). */
const BANDS = [
  ["under_100", 0, 3.6, "under 100 MW"],
  ["100_299", 100, 5.2, "100–299 MW"],
  ["300_999", 300, 7.2, "300–999 MW"],
  ["1000_2999", 1000, 9.6, "1–3 GW"],
  ["3000_plus", 3000, 12.8, "3 GW or more"],
];
const HOLLOW = 4;

const band = (mw) => {
  if (mw == null || mw <= 0) return null;
  let key = BANDS[0][0];
  BANDS.forEach(([name, floor]) => { if (mw >= floor) key = name; });
  return key;
};
const RADIUS = Object.fromEntries(BANDS.map(([key, , r]) => [key, r]));

/* Dots shrink with the map, down to a little over half, so a phone's map is not all
 * overlap. The legend uses the same factor, so its circles mean the dots. */
const dotScale = (width) => Math.max(0.55, Math.min(1, (width || 900) / 900));

const FIPS = { "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", 10: "DE", 11: "DC", 12: "FL", 13: "GA", 15: "HI", 16: "ID", 17: "IL", 18: "IN", 19: "IA", 20: "KS", 21: "KY", 22: "LA", 23: "ME", 24: "MD", 25: "MA", 26: "MI", 27: "MN", 28: "MS", 29: "MO", 30: "MT", 31: "NE", 32: "NV", 33: "NH", 34: "NJ", 35: "NM", 36: "NY", 37: "NC", 38: "ND", 39: "OH", 40: "OK", 41: "OR", 42: "PA", 44: "RI", 45: "SC", 46: "SD", 47: "TN", 48: "TX", 49: "UT", 50: "VT", 51: "VA", 53: "WA", 54: "WV", 55: "WI", 56: "WY" };
const STATE_NAMES = {
  AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
  CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia",
  HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky",
  LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota",
  MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire",
  NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota",
  OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island",
  SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont",
  VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
};
const stateName = (code) => STATE_NAMES[code] || code;

const fmtNum = (n, digits = 0) => (n == null ? "—"
  : Number(n).toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits }));
const fmtCapacity = (mw) => {
  if (mw == null || mw <= 0) return "not published";
  return mw >= 1000 ? `${fmtNum(mw / 1000, 1)} GW` : `${fmtNum(mw)} MW`;
};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const placeOf = (p) => [p.city || (p.county ? `${p.county}` : ""), p.state].filter(Boolean).join(", ");
const blocking = (p) => (p.risks || []).some((r) => r.status === "open" && r.severity === "blocking");

/* The drawing, imperative: d3 owns the SVG and React owns everything around it.
 * Returns the handles the view needs to filter and zoom without redrawing. */
function drawMap(host, { atlas, states, points, hidden, onPick, tipEl, transform, onZoom }) {
  const d3 = window.d3;
  host.querySelector("svg")?.remove();
  const width = host.clientWidth || 900;
  const height = host.clientHeight || 520;
  const scale = dotScale(width);
  const shapes = window.topojson.feature(atlas, atlas.objects.states);
  const mesh = window.topojson.mesh(atlas, atlas.objects.states, (a, b) => a !== b);
  const pad = width < 560 ? 6 : 18;
  const projection = d3.geoAlbersUsa().fitExtent([[pad, pad], [width - pad, height - pad]], shapes);
  const path = d3.geoPath(projection);

  const land = css("--map-land");
  const ramp = d3.interpolateLab(land, css("--primary"));
  const maxMw = Math.max(1, ...Object.values(states).map((s) => s.planned_mw));

  const tip = (event, markup) => {
    if (markup == null) { tipEl.hidden = true; return; }
    tipEl.innerHTML = markup;
    tipEl.hidden = false;
    const frame = host.getBoundingClientRect();
    const r = tipEl.getBoundingClientRect();
    const x = Math.min(Math.max(8, event.clientX - frame.left + 14), frame.width - r.width - 8);
    const y = event.clientY - frame.top - r.height - 12;
    tipEl.style.left = `${x}px`;
    tipEl.style.top = `${y < 8 ? event.clientY - frame.top + 18 : y}px`;
  };

  const svg = d3.select(host).append("svg")
    .attr("viewBox", `0 0 ${width} ${height}`)
    .attr("role", "img")
    .attr("aria-label", "Map of the United States with a dot for each campus that has a location");
  const world = svg.append("g");
  world.selectAll("path.mapv-state")
    .data(shapes.features)
    .join("path")
    .attr("class", "mapv-state")
    .attr("d", path)
    .attr("fill", (f) => {
      const row = states[FIPS[f.id]];
      return row ? ramp(0.08 + 0.62 * Math.sqrt(row.planned_mw / maxMw)) : land;
    })
    .on("mousemove", (event, f) => {
      const code = FIPS[f.id];
      const row = states[code];
      if (!row) { tip(event, `<b>${esc(stateName(code))}</b><span>no campus counted</span>`); return; }
      tip(event, `<b>${esc(stateName(code))}</b><span>${fmtNum(row.campuses)} campus${row.campuses === 1 ? "" : "es"} · planned: ${esc(fmtCapacity(row.planned_mw))}</span>`);
    })
    .on("mouseleave", (event) => tip(event, null));
  world.append("path")
    .attr("d", path(mesh))
    .attr("fill", "none")
    .attr("stroke", css("--map-land-edge"))
    .attr("stroke-width", 0.8)
    .attr("pointer-events", "none");
  if (width >= 560) {
    world.selectAll("text.mapv-state-label")
      .data(shapes.features.filter((f) => states[FIPS[f.id]]))
      .join("text")
      .attr("class", "mapv-state-label")
      .attr("text-anchor", "middle")
      .attr("transform", (f) => { const c = path.centroid(f); return `translate(${c[0]},${c[1] + 3})`; })
      .text((f) => FIPS[f.id]);
  }

  const radius = (key) => (RADIUS[key] || HOLLOW) * scale;
  const placed = points
    .map((p) => ({ p, band: band(p.mw_planned), xy: projection([p.lon, p.lat]) }))
    .filter((d) => d.xy)
    .sort((a, b) => (RADIUS[b.band] || HOLLOW) - (RADIUS[a.band] || HOLLOW));
  const dots = svg.append("g").selectAll("circle")
    .data(placed)
    .join("circle")
    .attr("class", (d) => `mapv-dot${d.band ? "" : " hollow"}${hidden.has(d.p.phase) ? " off" : ""}`)
    .attr("r", (d) => radius(d.band))
    // Through `style`, not the presentation attributes: an SVG attribute does not
    // read custom properties, and the stage colours change with the theme.
    .style("fill", (d) => `color-mix(in oklab, var(--stage-${d.p.phase}) 62%, var(--surface))`)
    .style("stroke", (d) => `var(--stage-${d.p.phase})`)
    .on("mousemove", (event, d) => {
      const p = d.p;
      const size = p.mw_planned ? `${fmtNum(p.mw_planned)} MW planned` : "size not published";
      tip(event, `<b>${esc(p.name)}</b><span>${esc(p.company)} · ${esc(placeOf(p))}<br>`
        + `${esc(STAGE_LABEL[p.phase] || p.phase)} · ${esc(size)}</span>`
        + (blocking(p) ? `<em>an obstacle is blocking it</em>` : "")
        + `<small>click to open</small>`);
    })
    .on("mouseleave", (event) => tip(event, null))
    .on("click", (_event, d) => onPick(d.p.id));

  const place = (t) => {
    world.attr("transform", t);
    dots.attr("cx", (d) => t.applyX(d.xy[0])).attr("cy", (d) => t.applyY(d.xy[1]));
  };
  const zoom = d3.zoom()
    .scaleExtent([1, 10])
    // Bounded to the frame, so the country cannot be dragged off and lost.
    .translateExtent([[0, 0], [width, height]])
    .on("zoom", (event) => { tip(event, null); place(event.transform); onZoom(event.transform); });
  // A double-click belongs to the dot under the pointer, not to the map.
  svg.call(zoom).on("dblclick.zoom", null);
  const start = transform || d3.zoomIdentity;
  svg.call(zoom.transform, start);
  place(start);
  return {
    filter: (off) => dots.classed("off", (d) => off.has(d.p.phase)),
    // Applied at once rather than through a d3 transition, which leaves the zoom
    // state behind when called this way.
    zoomBy: (k) => svg.call(zoom.scaleBy, k),
    reset: () => svg.call(zoom.transform, d3.zoomIdentity),
    scale,
  };
}

export function MapView({ data, api, onOpen, Eyebrow }) {
  const [summary, setSummary] = useState(null);
  const [atlas, setAtlas] = useState(null);
  const [failed, setFailed] = useState(null);
  const [hidden, setHidden] = useState(() => new Set());
  const [zoomK, setZoomK] = useState(1);
  const [width, setWidth] = useState(0);
  const [theme, setTheme] = useState(0);
  const hostRef = useRef(null);
  const tipRef = useRef(null);
  const handles = useRef(null);
  const transform = useRef(null);

  // The figures are re-read whenever the dataset is, so they never describe
  // a different set of rows from the dots.
  useEffect(() => {
    api("/api/map").then((body) => { setSummary(body); setFailed(null); })
      .catch((e) => setFailed(e.message || "the map's figures could not be read"));
  }, [api, data]);
  useEffect(() => {
    loadAtlas().then(setAtlas).catch(() => setFailed("the state boundaries could not be loaded"));
  }, []);
  // Redrawn when the frame changes size and when the theme flips, because the
  // state shading is computed from the theme's colours.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    const sized = new ResizeObserver(() => setWidth(host.clientWidth));
    sized.observe(host);
    const themed = new MutationObserver(() => setTheme((n) => n + 1));
    themed.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme"] });
    return () => { sized.disconnect(); themed.disconnect(); };
  }, []);

  const skipped = useMemo(() => new Set(summary ? summary.skipped : []), [summary]);
  const projects = data.projects;
  const points = useMemo(
    () => projects.filter((p) => p.lat != null && p.lon != null && !skipped.has(p.id)),
    [projects, skipped],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !atlas || !summary || !window.d3 || !window.topojson) return;
    transform.current = null; // a resize or a new dataset starts from the whole country
    handles.current = drawMap(host, {
      atlas,
      states: summary.states,
      points,
      hidden,
      onPick: onOpen,
      tipEl: tipRef.current,
      transform: transform.current,
      onZoom: (t) => { transform.current = t; setZoomK(t.k); },
    });
    setZoomK(1);
    // `hidden` is applied by the effect below without a redraw.
  }, [atlas, summary, points, width, theme, onOpen]);

  useEffect(() => { handles.current?.filter(hidden); }, [hidden]);

  const toggle = (stage) => setHidden((now) => {
    const next = new Set(now);
    if (next.has(stage)) next.delete(stage); else next.add(stage);
    return next;
  });

  const counts = useMemo(() => {
    const out = {};
    points.forEach((p) => { out[p.phase] = (out[p.phase] || 0) + 1; });
    return out;
  }, [points]);
  const scale = dotScale(width);
  const tot = summary ? summary.totals : null;
  const stateRows = summary
    ? Object.entries(summary.states).sort((a, b) => b[1].planned_mw - a[1].planned_mw || a[0].localeCompare(b[0]))
    : [];
  const half = Math.ceil(stateRows.length / 2);
  const table = (rows, caption) => html`
    <table class="mapv-table">
      <caption aria-hidden=${caption ? null : "true"}>${caption || " "}</caption>
      <thead><tr><th scope="col">State</th><th scope="col">Campuses</th><th scope="col">Planned</th></tr></thead>
      <tbody>${rows.map(([code, r]) => html`
        <tr key=${code}><th scope="row">${stateName(code)}</th><td>${fmtNum(r.campuses)}</td>
          <td>${fmtCapacity(r.planned_mw)}</td></tr>`)}</tbody>
    </table>`;

  return html`
    <div class="dc-view dc-rise mapv" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 18,
                     padding: "22px 26px 60px" }}>
      <${Eyebrow} figure="fig. 02 — geography" title="Who is building, and where">
        A dot sits on the middle of the town, ${html`<b>not</b>`} on the site — almost nobody publishes an
        address. Bigger dot, more capacity planned; a hollow ring means nobody has said how big. The figures
        count what the Capex page counts: paused and cancelled campuses are drawn but left out, and a campus
        suspected of being listed twice is counted and drawn once.
      <//>

      ${failed && html`<p class="mapv-failed" role="alert">${failed}</p>`}

      <dl class="mapv-figures">
        <div><dt>Campuses tracked</dt><dd>${tot ? fmtNum(tot.campuses) : "…"}</dd></div>
        <div><dt>Capacity planned</dt><dd>${tot ? fmtCapacity(tot.planned_mw) : "…"}</dd></div>
        <div><dt>Capacity operating</dt><dd>${tot ? fmtCapacity(tot.operating_mw) : "…"}</dd></div>
        <div><dt>States</dt><dd>${tot ? fmtNum(tot.states) : "…"}</dd></div>
      </dl>

      <div class="mapv-tools">
        <div class="mapv-chips" role="group" aria-label="Show stages">
          ${STAGES.filter((s) => counts[s]).map((s) => html`
            <button key=${s} type="button" class="mapv-chip" aria-pressed=${hidden.has(s) ? "false" : "true"}
                    style=${{ "--c": `var(--stage-${s})` }} onClick=${() => toggle(s)}>
              <i aria-hidden="true"></i>${STAGE_LABEL[s]}<small>${fmtNum(counts[s])}</small>
            </button>`)}
        </div>
        <div class="mapv-zoom" role="group" aria-label="Zoom">
          <button type="button" class="mapv-btn" aria-label="Zoom out" disabled=${zoomK <= 1.001}
                  onClick=${() => handles.current?.zoomBy(1 / 1.6)}>−</button>
          <span class="dc-num mapv-k">${zoomK.toFixed(1)}×</span>
          <button type="button" class="mapv-btn" aria-label="Zoom in" disabled=${zoomK >= 9.9}
                  onClick=${() => handles.current?.zoomBy(1.6)}>+</button>
          <button type="button" class="mapv-btn mapv-btn--text" disabled=${zoomK <= 1.001}
                  onClick=${() => handles.current?.reset()}>Reset</button>
        </div>
      </div>

      <figure class="mapv-frame">
        <div class="mapv-canvas" ref=${hostRef}>
          ${(!atlas || !summary) && !failed && html`<p class="mapv-loading">Loading the map…</p>`}
        </div>
        <div class="mapv-tip" ref=${tipRef} role="status" hidden></div>
        <figcaption class="mapv-legend">
          <span><span class="mapv-ramp" aria-hidden="true"></span> fewer — more GW planned</span>
          ${BANDS.map(([key, , r, label]) => html`
            <span key=${key} class="mapv-scale"><i style=${{ width: `${(r * scale * 2).toFixed(1)}px`,
              height: `${(r * scale * 2).toFixed(1)}px` }}></i>${label}</span>`)}
          <span class="mapv-scale"><i style=${{ width: `${(HOLLOW * scale * 2).toFixed(1)}px`,
            height: `${(HOLLOW * scale * 2).toFixed(1)}px`, borderStyle: "dashed" }}></i>size not published</span>
        </figcaption>
      </figure>
      <p class="mapv-note">
        ${fmtNum(points.length)} of ${fmtNum(projects.length)} campuses have a location and are drawn.
        Hover a dot for the campus, click it to open it; hover a state for its figures.
      </p>

      ${stateRows.length > 0 && html`
        <div class="mapv-states">
          ${table(stateRows.slice(0, half), "By state, planned capacity first")}
          ${stateRows.length > half && table(stateRows.slice(half), null)}
        </div>`}
    </div>`;
}
