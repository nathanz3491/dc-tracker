/* The public map: draws `data.json`, built by `webui/publicmap.py`.
 *
 * The same geometry as the console's map — the Census state boundaries, projected
 * with d3.geoAlbersUsa — and the same honesty rules: a dot sits on a town's centre,
 * never on a site; a campus whose size nobody published gets a hollow ring, never a
 * guessed radius. What a dot may say is decided by the server, at one of three
 * levels; this file only draws what it was sent, and draws nothing it was not.
 *
 * Zoom scales the geography and leaves the dots their size, so zooming in on
 * Northern Virginia separates the dots instead of growing them.
 */
(function () {
  "use strict";
  const P = window.DCPages;
  const $ = (id) => document.getElementById(id);
  const esc = P.escape;
  // The state boundaries, at the versioned address the page preloads them from.
  const ATLAS = (document.querySelector('link[rel="preload"][as="fetch"]') || {}).href
    || "/static/vendor/us-states-10m.json";

  const STATE_NAMES = {
    AL: ["Alabama", "阿拉巴马州"], AK: ["Alaska", "阿拉斯加州"], AZ: ["Arizona", "亚利桑那州"], AR: ["Arkansas", "阿肯色州"],
    CA: ["California", "加利福尼亚州"], CO: ["Colorado", "科罗拉多州"], CT: ["Connecticut", "康涅狄格州"], DE: ["Delaware", "特拉华州"],
    DC: ["District of Columbia", "哥伦比亚特区"], FL: ["Florida", "佛罗里达州"], GA: ["Georgia", "佐治亚州"], HI: ["Hawaii", "夏威夷州"],
    ID: ["Idaho", "爱达荷州"], IL: ["Illinois", "伊利诺伊州"], IN: ["Indiana", "印第安纳州"], IA: ["Iowa", "艾奥瓦州"],
    KS: ["Kansas", "堪萨斯州"], KY: ["Kentucky", "肯塔基州"], LA: ["Louisiana", "路易斯安那州"], ME: ["Maine", "缅因州"],
    MD: ["Maryland", "马里兰州"], MA: ["Massachusetts", "马萨诸塞州"], MI: ["Michigan", "密歇根州"], MN: ["Minnesota", "明尼苏达州"],
    MS: ["Mississippi", "密西西比州"], MO: ["Missouri", "密苏里州"], MT: ["Montana", "蒙大拿州"], NE: ["Nebraska", "内布拉斯加州"],
    NV: ["Nevada", "内华达州"], NH: ["New Hampshire", "新罕布什尔州"], NJ: ["New Jersey", "新泽西州"], NM: ["New Mexico", "新墨西哥州"],
    NY: ["New York", "纽约州"], NC: ["North Carolina", "北卡罗来纳州"], ND: ["North Dakota", "北达科他州"], OH: ["Ohio", "俄亥俄州"],
    OK: ["Oklahoma", "俄克拉何马州"], OR: ["Oregon", "俄勒冈州"], PA: ["Pennsylvania", "宾夕法尼亚州"], RI: ["Rhode Island", "罗得岛州"],
    SC: ["South Carolina", "南卡罗来纳州"], SD: ["South Dakota", "南达科他州"], TN: ["Tennessee", "田纳西州"], TX: ["Texas", "得克萨斯州"],
    UT: ["Utah", "犹他州"], VT: ["Vermont", "佛蒙特州"], VA: ["Virginia", "弗吉尼亚州"], WA: ["Washington", "华盛顿州"],
    WV: ["West Virginia", "西弗吉尼亚州"], WI: ["Wisconsin", "威斯康星州"], WY: ["Wyoming", "怀俄明州"],
  };
  const FIPS = { "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", 10: "DE", 11: "DC", 12: "FL", 13: "GA", 15: "HI", 16: "ID", 17: "IL", 18: "IN", 19: "IA", 20: "KS", 21: "KY", 22: "LA", 23: "ME", 24: "MD", 25: "MA", 26: "MI", 27: "MN", 28: "MS", 29: "MO", 30: "MT", 31: "NE", 32: "NV", 33: "NH", 34: "NJ", 35: "NM", 36: "NY", 37: "NC", 38: "ND", 39: "OH", 40: "OK", 41: "OR", 42: "PA", 44: "RI", 45: "SC", 46: "SD", 47: "TN", 48: "TX", 49: "UT", 50: "VT", 51: "VA", 53: "WA", 54: "WV", 55: "WI", 56: "WY" };

  /* Dot radius per capacity band. Sized by band, never by the figure, because the
   * `dots` level is never sent the figure. */
  const RADIUS = { under_100: 3.6, "100_299": 5.2, "300_999": 7.2, "1000_2999": 9.6, "3000_plus": 12.8 };
  const HOLLOW = 4;

  /* Dots shrink with the map, down to a little over half, so a phone's map is not
   * all overlap. The legend uses the same factor, so its circles mean the dots. */
  const dotScale = () => Math.max(0.55, Math.min(1, ($("map").clientWidth || 900) / 900));
  const radius = (band) => (RADIUS[band] || HOLLOW) * dotScale();

  const STRINGS = {
    en: {
      tag: "map", signIn: "Sign in for the sources", signInShort: "Sign in",
      heroEyebrow: "US data-center campuses", heroTitle: "Who is building, and where.",
      heroLede: "Every campus the tracker follows, from first announcement to running halls. Each figure behind this map comes from a published source, and the console shows which.",
      mapTitle: "Map", reset: "Reset",
      mapNote: "A dot sits on the middle of the town, not on the site — almost nobody publishes an address. Bigger dot, more capacity planned; a hollow ring means no source has said how big.",
      mapNoteStates: "Each state is shaded by the capacity its campuses plan. Hover or tap a state for its figures.",
      aboutTitle: "How these numbers are counted",
      about1: "Every figure is a floor: a campus whose size nobody has published counts as zero. Paused and cancelled campuses are drawn but left out of the totals, and a campus suspected of being listed twice is counted once — the same campuses the console's Capex page counts, each at its own planned capacity.",
      about2: "The console has what this page leaves out: the sentence behind every figure, the money, the tenants, the timelines and what stands in the way.",
      methodology: "How the tracker reads and checks its sources",
      figures: { campuses: "Campuses tracked", planned: "Capacity planned", operating: "Capacity operating", states: "States" },
      stages: { announced: "Announced", permitting: "Permitting", construction: "Construction", operational: "Operational", paused: "Paused", cancelled: "Cancelled" },
      bands: { under_100: "under 100 MW", "100_299": "100–299 MW", "300_999": "300–999 MW", "1000_2999": "1–3 GW", "3000_plus": "3 GW or more" },
      noSize: "size not published", notPublished: "not published",
      stateTip: (name, n, cap) => `<b>${name}</b><span>${n} campus${n === 1 ? "" : "es"} · planned: ${cap}</span>`,
      tableCaption: "By state, planned capacity first", colState: "State", colCampuses: "Campuses", colPlanned: "Planned",
      legendShade: "fewer — more GW planned",
      updated: "Data as of", loading: "Loading the map…", failed: "The map could not be loaded.",
      zoomIn: "Zoom in", zoomOut: "Zoom out",
    },
    zh: {
      tag: "地图", signIn: "登录查看来源", signInShort: "登录",
      heroEyebrow: "美国数据中心园区", heroTitle: "谁在建设，建在哪里。",
      heroLede: "追踪系统关注的每一个园区，从首次宣布到投入运行。这张地图背后的每个数字都来自公开来源，控制台会注明出处。",
      mapTitle: "地图", reset: "复位",
      mapNote: "圆点位于城镇中心，而不是具体场址——几乎没有人公布地址。圆点越大，规划容量越大；空心圆圈表示没有来源说明规模。",
      mapNoteStates: "各州按其园区的规划容量着色。将鼠标悬停或点按某个州即可查看数字。",
      aboutTitle: "这些数字如何统计",
      about1: "每个数字都是下限：没有公布规模的园区按零计算。暂停和取消的园区会显示在地图上，但不计入总数；疑似重复登记的园区只计一次——与控制台资本支出页面统计的园区相同，每个园区按其自身的规划容量计算。",
      about2: "控制台提供本页省略的内容：每个数字背后的原句、投资金额、租户、时间线以及面临的障碍。",
      methodology: "追踪系统如何读取和核查来源",
      figures: { campuses: "追踪的园区", planned: "规划容量", operating: "运行容量", states: "州" },
      stages: { announced: "已宣布", permitting: "审批中", construction: "建设中", operational: "已运行", paused: "已暂停", cancelled: "已取消" },
      bands: { under_100: "100 MW 以下", "100_299": "100–299 MW", "300_999": "300–999 MW", "1000_2999": "1–3 GW", "3000_plus": "3 GW 及以上" },
      noSize: "未公布规模", notPublished: "未公布",
      stateTip: (name, n, cap) => `<b>${name}</b><span>${n} 个园区 · 规划 ${cap}</span>`,
      tableCaption: "按州统计，规划容量从高到低", colState: "州", colCampuses: "园区", colPlanned: "规划",
      legendShade: "规划 GW 少 — 多",
      updated: "数据截至", loading: "正在加载地图…", failed: "地图无法加载。",
      zoomIn: "放大", zoomOut: "缩小",
    },
  };

  const base = P.meta("dc-base") || "/";
  const state = { data: null, atlas: null, failed: false, hidden: new Set(), zoom: null, transform: null };

  const stateName = (code) => (STATE_NAMES[code] || [code, code])[P.lang === "zh" ? 1 : 0];

  function renderSummary(t) {
    const box = $("summary");
    if (!state.data) return;
    const tot = state.data.totals;
    const f = t("figures");
    box.innerHTML = `<dl class="figures">
      <div><dt>${esc(f.campuses)}</dt><dd>${esc(P.fmt.num(tot.campuses))}</dd></div>
      <div><dt>${esc(f.planned)}</dt><dd>${esc(P.fmt.capacity(tot.planned_mw))}</dd></div>
      <div><dt>${esc(f.operating)}</dt><dd>${esc(P.fmt.capacity(tot.operating_mw))}</dd></div>
      <div><dt>${esc(f.states)}</dt><dd>${esc(P.fmt.num(tot.states))}</dd></div>
    </dl>`;
  }

  function renderTable(t) {
    let section = $("states-section");
    if (!section) {
      section = document.createElement("section");
      section.id = "states-section";
      section.className = "state-grid";
      document.querySelector(".map-block").after(section);
    }
    if (!state.data) { section.innerHTML = ""; return; }
    const rows = Object.entries(state.data.states)
      .sort((a, b) => b[1].planned_mw - a[1].planned_mw || a[0].localeCompare(b[0]));
    const half = Math.ceil(rows.length / 2);
    const table = (part, caption) => `<table class="state-table">
      ${caption ? `<caption>${esc(t("tableCaption"))}</caption>` : `<caption aria-hidden="true">&nbsp;</caption>`}
      <thead><tr><th scope="col">${esc(t("colState"))}</th><th scope="col">${esc(t("colCampuses"))}</th><th scope="col">${esc(t("colPlanned"))}</th></tr></thead>
      <tbody>${part.map(([code, r]) => `<tr><th scope="row">${esc(stateName(code))}</th><td>${esc(P.fmt.num(r.campuses))}</td><td>${esc(r.planned_mw > 0 ? P.fmt.capacity(r.planned_mw) : t("notPublished"))}</td></tr>`).join("")}</tbody>
    </table>`;
    section.innerHTML = table(rows.slice(0, half), true) + (rows.length > half ? table(rows.slice(half), false) : "");
  }

  function renderChips(t) {
    const box = $("stages");
    const pts = state.data ? state.data.points : [];
    if (!state.data || !pts.length) { box.innerHTML = ""; box.hidden = true; return; }
    box.hidden = false;
    const counts = {};
    pts.forEach((p) => { counts[p.stage] = (counts[p.stage] || 0) + 1; });
    const stages = Object.keys(t("stages")).filter((s) => counts[s]);
    box.innerHTML = stages.map((s) => `<button type="button" class="chip" data-stage="${esc(s)}"
        aria-pressed="${state.hidden.has(s) ? "false" : "true"}" style="--c: var(--stage-${esc(s)})">
        <i aria-hidden="true"></i>${esc(t("stages")[s])}<small>${esc(P.fmt.num(counts[s]))}</small></button>`).join("");
  }

  function renderLegend(t) {
    const box = $("legend");
    const parts = [];
    const land = P.css("--land");
    const amber = P.css("--amber");
    parts.push(`<span><span class="legend-ramp" style="background:linear-gradient(90deg, ${esc(land)}, ${esc(amber)})"></span> ${esc(t("legendShade"))}</span>`);
    if (state.data && state.data.points.length) {
      const bands = t("bands");
      parts.push(...Object.keys(RADIUS).map((key) => {
        const d = (radius(key) * 2).toFixed(1);
        return `<span class="legend-scale"><i style="width:${d}px;height:${d}px"></i>${esc(bands[key])}</span>`;
      }));
      const h = (radius(null) * 2).toFixed(1);
      parts.push(`<span class="legend-scale"><i style="width:${h}px;height:${h}px;border-style:dashed"></i>${esc(t("noSize"))}</span>`);
    }
    box.innerHTML = parts.join("");
    $("map-note").textContent = state.data && !state.data.points.length ? t("mapNoteStates") : t("mapNote");
  }

  function tipFor(p, t) {
    const stage = t("stages")[p.stage] || p.stage;
    const size = p.mw != null ? P.fmt.capacity(p.mw) : p.band ? t("bands")[p.band] : t("noSize");
    if (p.name) {
      const where = [p.place, p.state].filter(Boolean).join(", ");
      return `<b>${esc(p.name)}</b><span>${esc(p.operator)} · ${esc(where)}<br>${esc(stage)} · ${esc(size)}</span>`;
    }
    return `<b>${esc(p.operator)}</b><span>${esc(stateName(p.state))} · ${esc(stage)} · ${esc(size)}</span>`;
  }

  function showTip(event, html) {
    const box = $("tip");
    const frame = $("map").getBoundingClientRect();
    if (html == null) { box.hidden = true; return; }
    box.innerHTML = html;
    box.hidden = false;
    const r = box.getBoundingClientRect();
    const x = Math.min(Math.max(8, event.clientX - frame.left + 14), frame.width - r.width - 8);
    const y = event.clientY - frame.top - r.height - 12;
    box.style.left = `${x}px`;
    box.style.top = `${y < 8 ? event.clientY - frame.top + 18 : y}px`;
  }

  function drawMap(t) {
    const host = $("map");
    host.querySelector("svg")?.remove();
    host.querySelector(".empty")?.remove();
    if (state.failed) {
      host.insertAdjacentHTML("beforeend", `<p class="empty" style="padding:24px">${esc(t("failed"))}</p>`);
      return;
    }
    if (!state.data || !state.atlas || !window.d3 || !window.topojson) {
      host.insertAdjacentHTML("beforeend", `<p class="empty" style="padding:24px">${esc(t("loading"))}</p>`);
      return;
    }
    const d3 = window.d3;
    const width = host.clientWidth || 900;
    const height = host.clientHeight || 520;
    const states = window.topojson.feature(state.atlas, state.atlas.objects.states);
    const mesh = window.topojson.mesh(state.atlas, state.atlas.objects.states, (a, b) => a !== b);
    const pad = width < 560 ? 6 : 18;
    const projection = d3.geoAlbersUsa().fitExtent([[pad, pad], [width - pad, height - pad]], states);
    const path = d3.geoPath(projection);

    const land = P.css("--land");
    const amber = P.css("--amber");
    const ramp = d3.interpolateLab(land, amber);
    const byState = state.data.states;
    const maxMw = Math.max(1, ...Object.values(byState).map((s) => s.planned_mw));

    const svg = d3.select(host).append("svg")
      .attr("viewBox", `0 0 ${width} ${height}`)
      .attr("aria-hidden", "true");
    const world = svg.append("g");
    world.selectAll("path.state-shape")
      .data(states.features)
      .join("path")
      .attr("class", "state-shape")
      .attr("d", path)
      .attr("fill", (f) => {
        const row = byState[FIPS[f.id]];
        return row ? ramp(0.08 + 0.62 * Math.sqrt(row.planned_mw / maxMw)) : land;
      })
      .on("mousemove", (event, f) => {
        const code = FIPS[f.id];
        const row = byState[code];
        if (!row) { showTip(event, `<b>${esc(stateName(code))}</b>`); return; }
        const cap = row.planned_mw > 0 ? P.fmt.capacity(row.planned_mw) : t("notPublished");
        showTip(event, t("stateTip")(esc(stateName(code)), row.campuses, esc(cap)));
      })
      .on("mouseleave", (event) => showTip(event, null));
    world.append("path")
      .attr("d", path(mesh))
      .attr("fill", "none")
      .attr("stroke", P.css("--land-edge"))
      .attr("stroke-width", 0.8)
      .attr("pointer-events", "none");
    if (width >= 560) {
      world.selectAll("text.state-label")
        .data(states.features.filter((f) => byState[FIPS[f.id]]))
        .join("text")
        .attr("class", "state-label")
        .attr("text-anchor", "middle")
        .attr("transform", (f) => { const c = path.centroid(f); return `translate(${c[0]},${c[1] + 3})`; })
        .text((f) => FIPS[f.id]);
    }

    const placed = state.data.points
      .map((p) => ({ p, xy: projection([p.lon, p.lat]) }))
      .filter((d) => d.xy)
      .sort((a, b) => (RADIUS[b.p.band] || HOLLOW) - (RADIUS[a.p.band] || HOLLOW));
    const dots = svg.append("g").selectAll("circle")
      .data(placed)
      .join("circle")
      .attr("class", (d) => `dot${d.p.band ? "" : " hollow"}${state.hidden.has(d.p.stage) ? " off" : ""}`)
      .attr("r", (d) => radius(d.p.band))
      // Through `style`, not the presentation attributes: an SVG attribute does not
      // read custom properties, and the stage colours change with the theme.
      .style("fill", (d) => `color-mix(in oklab, var(--stage-${d.p.stage}) 62%, var(--surface))`)
      .style("stroke", (d) => `var(--stage-${d.p.stage})`)
      .on("mousemove", (event, d) => showTip(event, tipFor(d.p, t)))
      .on("mouseleave", (event) => showTip(event, null))
      .on("click", (event, d) => showTip(event, tipFor(d.p, t)));

    const place = (transform) => {
      world.attr("transform", transform);
      dots.attr("cx", (d) => transform.applyX(d.xy[0])).attr("cy", (d) => transform.applyY(d.xy[1]));
      $("zoom-out").disabled = transform.k <= 1.001;
      $("zoom-reset").disabled = transform.k <= 1.001;
      $("zoom-in").disabled = transform.k >= 9.9;
    };
    state.zoom = d3.zoom()
      .scaleExtent([1, 10])
      .translateExtent([[0, 0], [width, height]])
      .on("zoom", (event) => { state.transform = event.transform; showTip(event, null); place(event.transform); });
    svg.call(state.zoom).on("dblclick.zoom", null);
    const keep = state.transform || d3.zoomIdentity;
    svg.call(state.zoom.transform, keep);
    place(keep);
    state.svg = svg;
  }

  function render(lang) {
    const t = P.t;
    renderSummary(t);
    renderChips(t);
    renderLegend(t);
    renderTable(t);
    drawMap(t);
    $("zoom-in").setAttribute("aria-label", t("zoomIn"));
    $("zoom-out").setAttribute("aria-label", t("zoomOut"));
    $("docs-link").href = `${P.meta("dc-docs")}${lang}/methodology`;
    $("updated").textContent = state.data && state.data.updated_at
      ? `${t("updated")} ${P.fmt.date(`${state.data.updated_at}T12:00:00`, { year: "numeric", month: "long", day: "numeric" })}`
      : "";
  }

  function bind() {
    $("stages").addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (!chip) return;
      const stage = chip.dataset.stage;
      if (state.hidden.has(stage)) state.hidden.delete(stage); else state.hidden.add(stage);
      chip.setAttribute("aria-pressed", state.hidden.has(stage) ? "false" : "true");
      if (state.svg) state.svg.selectAll("circle.dot").classed("off", (d) => state.hidden.has(d.p.stage));
    });
    // Applied at once rather than through a d3 transition, which the console's map
    // found to leave the zoom state behind (`vendor/dc-map.js`, `_eased`).
    const zoomBy = (k) => state.svg && state.svg.call(state.zoom.scaleBy, k);
    $("zoom-in").addEventListener("click", () => zoomBy(1.6));
    $("zoom-out").addEventListener("click", () => zoomBy(1 / 1.6));
    $("zoom-reset").addEventListener("click", () => state.svg && state.svg.call(state.zoom.transform, window.d3.zoomIdentity));
    let pending = 0;
    new ResizeObserver(() => {
      cancelAnimationFrame(pending);
      pending = requestAnimationFrame(() => { state.transform = null; renderLegend(P.t); drawMap(P.t); });
    }).observe($("map"));
  }

  async function load() {
    try {
      const [data, atlas] = await Promise.all([
        fetch(`${base}data.json`, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
        fetch(ATLAS).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
      ]);
      state.data = data;
      state.atlas = atlas;
    } catch (_) {
      state.failed = true;
    }
    render(P.lang);
  }

  P.start(STRINGS, render);
  bind();
  load();
})();
