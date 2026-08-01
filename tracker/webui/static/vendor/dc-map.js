/* Vendored for `tracker serve`. Identical to the design project's copy
 * except that the CDN URLs below are local paths: the console makes no
 * network requests, so three.js and the Census boundary TopoJSON are
 * served from /static/vendor/ alongside this file.
 */
/* <dc-map> — 2D national map of tracked projects.
 *
 * Real geometry: us-atlas TopoJSON (US Census cartographic boundaries, public
 * domain), projected with d3.geoAlbersUsa. Nothing here is freehand.
 *
 * Honesty rules carried over from tracker/ingest/geo.py and the export dashboard:
 *   - every coordinate is a PLACE or COUNTY centroid, never the project site;
 *   - a project with no cited capacity gets a hollow dashed ring, not a guessed
 *     radius, and never a zero;
 *   - colour means phase or trust, chosen explicitly — never both at once.
 *
 * Attributes: encoding="phase|confidence|company" choropleth="count|mw|none"
 *             clusters selected="<id>" compact
 * Events: dc-pick {detail:{id}}   dc-hover {detail:{id|null}}
 */
(function () {
  const ATLAS = "/static/vendor/us-states-10m.json";
  let atlasPromise = null;
  // A failed load is not cached. `p = p || import(...)` memoises the
  // *rejected* promise, so one transient failure — an expired session
  // 401ing the module fetch, a dropped connection — became permanent for
  // the life of the page, and the element went on reporting "unavailable
  // offline" long after the cause was gone.
  const loadAtlas = () =>
    (atlasPromise =
      atlasPromise ||
      fetch(ATLAS)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
        .catch((e) => {
          atlasPromise = null;
          throw e;
        }));

  const PHASE_TOKEN = {
    announced: "--chart-5", permitting: "--chart-1", construction: "--chart-3",
    operational: "--chart-2", paused: "--warning", cancelled: "--muted-foreground"
  };
  const CONF_TOKEN = { 0: "--muted-foreground", 1: "--warning", 2: "--chart-1", 3: "--success" };
  const CHART = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5"];

  class DCMap extends HTMLElement {
    static get observedAttributes() { return ["encoding", "choropleth", "clusters", "selected", "compact"]; }

    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = "block";
      this.style.position = "relative";
      this.style.width = "100%";
      this.style.height = "100%";
      this.style.minHeight = "260px";

      this._svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      this._svg.setAttribute("width", "100%");
      this._svg.setAttribute("height", "100%");
      this._svg.style.display = "block";
      this._svg.style.overflow = "visible";
      this.appendChild(this._svg);

      this._tip = document.createElement("div");
      this._tip.style.cssText = "position:absolute;pointer-events:none;opacity:0;transition:opacity 120ms cubic-bezier(.4,0,.2,1);z-index:5;background:var(--surface);border:1px solid var(--border);border-radius:999px;box-shadow:var(--shadow-pop);padding:6px 12px;font:500 12px/1.35 var(--font-sans,system-ui);color:var(--foreground);white-space:nowrap";
      this.appendChild(this._tip);

      this._onZoomCmd = (e) => {
        const how = e && e.detail && e.detail.how;
        if (how === "in") this.zoomBy(1.6);
        else if (how === "out") this.zoomBy(1 / 1.6);
        else this.resetZoom();
      };
      this.addEventListener("dc-zoom-cmd", this._onZoomCmd);
      window.addEventListener("dc-reset-view", () => this.resetZoom());

      this._ro = new ResizeObserver(() => this.render());
      this._ro.observe(this);
      this._mo = new MutationObserver(() => this.render());
      this._mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

      const start = () => loadAtlas().then((topo) => {
        this._states = topojson.feature(topo, topo.objects.states);
        this._mesh = topojson.mesh(topo, topo.objects.states, (a, b) => a !== b);
        this.render();
      }).catch(() => {
        this._failed = true;
        this.render();
      });
      if (window.DCTRACKER) start(); else window.addEventListener("dctracker-ready", start, { once: true });
    }

    disconnectedCallback() { this._ro && this._ro.disconnect(); this._mo && this._mo.disconnect(); }
    attributeChangedCallback() { this._built && this.render(); }

    tok(name) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
    }

    render() {
      const data = window.DCTRACKER;
      if (!data || !this._svg) return;
      const w = this.clientWidth || 640;
      const h = this.clientHeight || 360;
      if (w < 20 || h < 20) return;
      const svg = this._svg;
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      svg.setAttribute("viewBox", "0 0 " + w + " " + h);

      if (this._failed || !this._states) {
        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        t.setAttribute("x", w / 2); t.setAttribute("y", h / 2);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("fill", this.tok("--muted-foreground"));
        t.setAttribute("font-family", "var(--font-mono)");
        t.setAttribute("font-size", "12");
        t.textContent = this._failed ? "boundary data unavailable offline" : "loading boundaries…";
        svg.appendChild(t);
        return;
      }

      const compact = this.hasAttribute("compact");
      const pad = compact ? 6 : 18;
      const legendH = compact ? 0 : 34;
      const projection = d3.geoAlbersUsa().fitExtent([[pad, pad], [w - pad, h - pad - legendH]], this._states);
      const path = d3.geoPath(projection);

      const projects = data.projects;
      const encoding = this.getAttribute("encoding") || "phase";
      const chorMode = this.getAttribute("choropleth") || "count";
      const selected = this.getAttribute("selected");

      // --- state aggregates
      const byState = {};
      projects.forEach((p) => {
        const s = (byState[p.state] = byState[p.state] || { n: 0, mw: 0, blocking: 0 });
        s.n += 1;
        s.mw += p.mw_planned || 0;
        if (p.risks.some((r) => r.status === "open" && r.severity === "blocking")) s.blocking += 1;
      });
      const ABBR = { "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", 10: "DE", 11: "DC", 12: "FL", 13: "GA", 15: "HI", 16: "ID", 17: "IL", 18: "IN", 19: "IA", 20: "KS", 21: "KY", 22: "LA", 23: "ME", 24: "MD", 25: "MA", 26: "MI", 27: "MN", 28: "MS", 29: "MO", 30: "MT", 31: "NE", 32: "NV", 33: "NH", 34: "NJ", 35: "NM", 36: "NY", 37: "NC", 38: "ND", 39: "OH", 40: "OK", 41: "OR", 42: "PA", 44: "RI", 45: "SC", 46: "SD", 47: "TN", 48: "TX", 49: "UT", 50: "VT", 51: "VA", 53: "WA", 54: "WV", 55: "WI", 56: "WY" };

      const maxN = Math.max(1, ...Object.values(byState).map((s) => s.n));
      const maxMW = Math.max(1, ...Object.values(byState).map((s) => s.mw));

      const surface = this.tok("--surface");
      const muted = this.tok("--muted");
      const border = this.tok("--border");
      const inputC = this.tok("--input");
      const ink = this.tok("--foreground");
      const dim = this.tok("--muted-foreground");
      const primary = this.tok("--primary");
      const danger = this.tok("--danger");

      // Geography lives in `world` and scales with the zoom. Marks do NOT:
      // see `applyZoom`. The legend stays outside both so it never moves.
      const world = mk("g");
      svg.appendChild(world);
      this._world = world;

      const gStates = mk("g");
      world.appendChild(gStates);

      this._states.features.forEach((f) => {
        const ab = ABBR[f.id];
        const agg = ab && byState[ab];
        let fill = muted;
        if (agg && chorMode !== "none") {
          const t = chorMode === "mw" ? agg.mw / maxMW : agg.n / maxN;
          fill = mix(muted, primary, 0.14 + 0.5 * Math.sqrt(t));
        }
        const el = mk("path");
        el.setAttribute("d", path(f));
        el.setAttribute("fill", fill);
        el.setAttribute("stroke", surface);
        el.setAttribute("stroke-width", "0.7");
        if (agg) {
          el.style.cursor = "pointer";
          el.setAttribute("data-state", ab);
          el.addEventListener("mouseenter", (e) => {
            el.setAttribute("stroke", inputC); el.setAttribute("stroke-width", "1.4");
            this.tip(e, ab + " · " + agg.n + " project" + (agg.n > 1 ? "s" : "") + (agg.mw ? " · " + agg.mw.toLocaleString() + " MW cited" : " · no cited MW"));
          });
          el.addEventListener("mouseleave", () => { el.setAttribute("stroke", surface); el.setAttribute("stroke-width", "0.7"); this.hideTip(); });
          el.addEventListener("click", () => this.dispatchEvent(new CustomEvent("dc-state", { bubbles: true, composed: true, detail: { state: ab } })));
        }
        gStates.appendChild(el);
      });

      const meshEl = mk("path");
      meshEl.setAttribute("d", path(this._mesh));
      meshEl.setAttribute("fill", "none");
      meshEl.setAttribute("stroke", border);
      meshEl.setAttribute("stroke-width", "0.8");
      meshEl.setAttribute("pointer-events", "none");
      world.appendChild(meshEl);

      // --- company clusters
      const pts = projects.map((p) => ({ p: p, xy: p.lat != null ? projection([p.lon, p.lat]) : null })).filter((d) => d.xy);
      if (this.hasAttribute("clusters")) {
        const byCo = {};
        pts.forEach((d) => (byCo[d.p.company] = byCo[d.p.company] || []).push(d));
        let ci = 0;
        Object.keys(byCo).forEach((co) => {
          const group = byCo[co];
          const color = this.tok(CHART[ci++ % CHART.length]);
          if (group.length < 2) return;
          const cx = d3.mean(group, (d) => d.xy[0]);
          const cy = d3.mean(group, (d) => d.xy[1]);
          const r = Math.max(26, d3.max(group, (d) => Math.hypot(d.xy[0] - cx, d.xy[1] - cy)) + 16);
          const ring = mk("circle");
          ring.setAttribute("cx", cx); ring.setAttribute("cy", cy); ring.setAttribute("r", r);
          ring.setAttribute("fill", mix(surface, color, 0.1));
          ring.setAttribute("stroke", color);
          ring.setAttribute("stroke-width", "1");
          ring.setAttribute("stroke-dasharray", "3 4");
          ring.setAttribute("pointer-events", "none");
          world.appendChild(ring);
          const lbl = mk("text");
          lbl.setAttribute("x", cx); lbl.setAttribute("y", cy - r - 5);
          lbl.setAttribute("text-anchor", "middle");
          lbl.setAttribute("fill", color);
          lbl.setAttribute("font-family", "var(--font-mono)");
          lbl.setAttribute("font-size", "12");
          lbl.setAttribute("letter-spacing", ".08em");
          lbl.setAttribute("pointer-events", "none");
          lbl.textContent = co.toUpperCase() + " ×" + group.length;
          world.appendChild(lbl);
        });
      }

      // --- state postal labels for states that carry projects
      if (!compact) {
        this._states.features.forEach((f) => {
          const ab = ABBR[f.id];
          if (!ab || !byState[ab]) return;
          const c = path.centroid(f);
          if (!c || isNaN(c[0])) return;
          const t = mk("text");
          t.setAttribute("x", c[0]); t.setAttribute("y", c[1] + 3);
          t.setAttribute("text-anchor", "middle");
          t.setAttribute("fill", dim);
          t.setAttribute("font-family", "var(--font-mono)");
          t.setAttribute("font-size", "12");
          t.setAttribute("letter-spacing", ".06em");
          t.setAttribute("pointer-events", "none");
          t.textContent = ab;
          world.appendChild(t);
        });
      }

      // --- project marks
      const rScale = d3.scaleSqrt().domain([0, 1200]).range([0, compact ? 15 : 26]);
      const gPts = mk("g");
      svg.appendChild(gPts);
      this._gPts = gPts;
      this._marks = [];
      let ci2 = 0;
      const coColor = {};
      projects.forEach((p) => { if (!(p.company in coColor)) coColor[p.company] = this.tok(CHART[ci2++ % CHART.length]); });

      pts.sort((a, b) => (b.p.mw_planned || 0) - (a.p.mw_planned || 0)).forEach((d) => {
        const p = d.p;
        const x = d.xy[0], y = d.xy[1];
        const hasMW = p.mw_planned != null;
        const r = hasMW ? Math.max(4, rScale(p.mw_planned)) : (compact ? 4 : 5.5);
        const color = encoding === "confidence" ? this.tok(CONF_TOKEN[p.confidence])
          : encoding === "company" ? coColor[p.company]
            : this.tok(PHASE_TOKEN[p.phase] || "--chart-1");
        const blocking = p.risks.some((r2) => r2.status === "open" && r2.severity === "blocking");
        const g = mk("g");
        g.style.cursor = "pointer";
        g.setAttribute("transform", "translate(" + x + "," + y + ")");
        this._marks.push({ g: g, x: x, y: y });

        if (blocking) {
          const halo = mk("circle");
          halo.setAttribute("r", r + 5);
          halo.setAttribute("fill", "none");
          halo.setAttribute("stroke", danger);
          halo.setAttribute("stroke-width", "1.5");
          halo.setAttribute("opacity", ".9");
          const an = document.createElementNS("http://www.w3.org/2000/svg", "animate");
          an.setAttribute("attributeName", "r");
          an.setAttribute("values", r + 3 + ";" + (r + 9) + ";" + (r + 3));
          an.setAttribute("dur", "2.4s");
          an.setAttribute("repeatCount", "indefinite");
          halo.appendChild(an);
          const an2 = document.createElementNS("http://www.w3.org/2000/svg", "animate");
          an2.setAttribute("attributeName", "opacity");
          an2.setAttribute("values", ".85;.25;.85");
          an2.setAttribute("dur", "2.4s");
          an2.setAttribute("repeatCount", "indefinite");
          halo.appendChild(an2);
          g.appendChild(halo);
        }

        const c = mk("circle");
        c.setAttribute("r", r);
        if (hasMW) {
          c.setAttribute("fill", mix(surface, color, 0.62));
          c.setAttribute("stroke", color);
          c.setAttribute("stroke-width", "1.5");
        } else {
          c.setAttribute("fill", "none");
          c.setAttribute("stroke", color);
          c.setAttribute("stroke-width", "1.4");
          c.setAttribute("stroke-dasharray", "2.5 2.5");
        }
        g.appendChild(c);

        if (String(p.id) === selected) {
          const sel = mk("circle");
          sel.setAttribute("r", r + 9);
          sel.setAttribute("fill", "none");
          sel.setAttribute("stroke", ink);
          sel.setAttribute("stroke-width", "1.5");
          g.appendChild(sel);
        }

        const label = p.company + " — " + p.name + "  ·  " + (p.city ? p.city + ", " : p.county ? p.county + " Co., " : "") + p.state
          + "  ·  " + (hasMW ? p.mw_planned.toLocaleString() + " MW planned" : "no cited capacity")
          + (blocking ? "  ·  blocking risk" : "");
        g.addEventListener("mouseenter", (e) => { c.setAttribute("stroke-width", "2.5"); this.tip(e, label); this.dispatchEvent(new CustomEvent("dc-hover", { bubbles: true, composed: true, detail: { id: p.id } })); });
        g.addEventListener("mousemove", (e) => this.tip(e, label));
        g.addEventListener("mouseleave", () => { c.setAttribute("stroke-width", hasMW ? "1.5" : "1.4"); this.hideTip(); this.dispatchEvent(new CustomEvent("dc-hover", { bubbles: true, composed: true, detail: { id: null } })); });
        g.addEventListener("click", () => this.dispatchEvent(new CustomEvent("dc-pick", { bubbles: true, composed: true, detail: { id: p.id } })));
        gPts.appendChild(g);
      });

      // --- legend
      if (!compact) {
        const keys = encoding === "confidence"
          ? [["0 no citation", CONF_TOKEN[0]], ["1 weakly cited", CONF_TOKEN[1]], ["2 one solid source", CONF_TOKEN[2]], ["3 corroborated", CONF_TOKEN[3]]]
          : encoding === "company" ? [] : data.phases.map((ph) => [ph, PHASE_TOKEN[ph]]);
        let lx = pad;
        const ly = h - 14;
        keys.forEach(([label, tokName]) => {
          const dot = mk("circle");
          dot.setAttribute("cx", lx + 4); dot.setAttribute("cy", ly - 4); dot.setAttribute("r", 4);
          dot.setAttribute("fill", mix(surface, this.tok(tokName), 0.62));
          dot.setAttribute("stroke", this.tok(tokName));
          dot.setAttribute("stroke-width", "1.4");
          svg.appendChild(dot);
          const t = mk("text");
          t.setAttribute("x", lx + 13); t.setAttribute("y", ly);
          t.setAttribute("fill", dim);
          t.setAttribute("font-family", "var(--font-mono)");
          t.setAttribute("font-size", "12");
          t.textContent = label;
          svg.appendChild(t);
          lx += 24 + label.length * 6.6;
        });
        const hollow = mk("circle");
        hollow.setAttribute("cx", lx + 4); hollow.setAttribute("cy", ly - 4); hollow.setAttribute("r", 4);
        hollow.setAttribute("fill", "none");
        hollow.setAttribute("stroke", dim);
        hollow.setAttribute("stroke-width", "1.3");
        hollow.setAttribute("stroke-dasharray", "2.5 2.5");
        svg.appendChild(hollow);
        const ht = mk("text");
        ht.setAttribute("x", lx + 13); ht.setAttribute("y", ly);
        ht.setAttribute("fill", dim);
        ht.setAttribute("font-family", "var(--font-mono)");
        ht.setAttribute("font-size", "12");
        ht.textContent = "no cited capacity";
        svg.appendChild(ht);
      }

      this.bindZoom();
      this.applyZoom();
    }

    /* Zoom.
     *
     * Geography scales; marks do not. Scaling the bubbles too would be the
     * obvious implementation and the wrong one — the reason to zoom this map is
     * that a dozen Northern Virginia projects sit on top of each other, and
     * bubbles that grow with the map stay exactly as overlapped as they were.
     * Keeping them a fixed size is what makes zooming useful.
     */
    applyZoom() {
      const t = this._zoomT || { k: 1, x: 0, y: 0 };
      if (this._world) {
        this._world.setAttribute("transform", `translate(${t.x},${t.y}) scale(${t.k})`);
      }
      (this._marks || []).forEach((m) => {
        m.g.setAttribute("transform", `translate(${m.x * t.k + t.x},${m.y * t.k + t.y})`);
      });
      this.dispatchEvent(new CustomEvent("dc-zoom", {
        bubbles: true, composed: true, detail: { k: t.k },
      }));
    }

    bindZoom() {
      if (this._zoomBound || typeof d3.zoom !== "function") return;
      this._zoomBound = true;
      this._zoomT = this._zoomT || { k: 1, x: 0, y: 0 };
      this._zoom = d3.zoom()
        .scaleExtent([1, 12])
        // Panning is bounded to the viewport so the country cannot be dragged
        // off screen and lost, which with no visible edge is easy to do.
        .translateExtent([[0, 0], [this.clientWidth || 640, this.clientHeight || 360]])
        .on("zoom", (event) => {
          this._zoomT = event.transform;
          this.applyZoom();
        });
      const sel = d3.select(this._svg);
      sel.call(this._zoom);
      // A double-click belongs to the project under the cursor, not to the map.
      sel.on("dblclick.zoom", null);
      this._svg.style.cursor = "grab";
    }

    /* Button-driven zoom is applied instantly and animated in CSS.
     *
     * d3's own `.transition().call(zoom.scaleBy, k)` silently did nothing here —
     * the node's __zoom never advanced — while the same call without the
     * transition worked. Rather than ship a smooth animation that depends on
     * behaviour I could not explain, the transform is set directly and the
     * easing is a CSS transition switched on only for this path. Wheel and drag
     * stay instant, which is what they should be anyway. */
    _eased(fn) {
      this.classList.add("dc-map--easing");
      clearTimeout(this._easeT);
      this._easeT = setTimeout(() => this.classList.remove("dc-map--easing"), 280);
      fn();
    }

    zoomBy(factor) {
      if (!this._zoom) return;
      this._eased(() => d3.select(this._svg).call(this._zoom.scaleBy, factor));
    }

    resetZoom() {
      if (!this._zoom) return;
      this._eased(() => d3.select(this._svg).call(this._zoom.transform, d3.zoomIdentity));
    }

    tip(e, text) {
      const r = this.getBoundingClientRect();
      this._tip.textContent = text;
      this._tip.style.opacity = "1";
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      this._tip.style.left = Math.min(Math.max(8, x + 14), Math.max(8, r.width - this._tip.offsetWidth - 8)) + "px";
      this._tip.style.top = Math.max(4, y - 34) + "px";
    }
    hideTip() { this._tip.style.opacity = "0"; }
  }

  function mk(tag) { return document.createElementNS("http://www.w3.org/2000/svg", tag); }
  function mix(a, b, t) { try { return d3.interpolateLab(a, b)(t); } catch (e) { return b; } }

  if (!customElements.get("dc-map")) customElements.define("dc-map", DCMap);
})();
