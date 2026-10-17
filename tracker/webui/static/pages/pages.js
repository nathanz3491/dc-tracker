/* dc-tracker public pages: what the status page is built on.
 *
 *   theme      system, light or dark, remembered, cycled by the sun button
 *   language   English or 中文, remembered, else the browser's; every element with
 *              `data-i18n="key"` is given that key's text
 *   links      the other sites' addresses, from the `dc-*` metas the server fills
 *   numbers    dates, times and figures in the reader's language and time zone
 *
 * Remembered means `localStorage`, wrapped so a browser that refuses it (a private
 * window, blocked site data) still gets a working page that simply forgets.
 *
 * A page script calls `DCPages.start(strings, render)`: `strings` is
 * `{en: {...}, zh: {...}}`, merged over the shared ones below, and `render(lang)`
 * runs once now and again whenever the language or the theme changes.
 */
(function () {
  "use strict";

  const store = {
    get(key) { try { return window.localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (_) { /* not kept */ } },
  };
  const meta = (name) => {
    const el = document.querySelector(`meta[name="${name}"]`);
    return el ? el.getAttribute("content") || "" : "";
  };
  const $ = (id) => document.getElementById(id);

  const SHARED = {
    en: {
      htmlLang: "en", other: "中文", skip: "Skip to content",
      theme: { system: "Theme: follows your system", light: "Theme: light", dark: "Theme: dark" },
      console: "Console", docs: "Documentation", api: "API", status: "Status",
      updated: "Updated", never: "never", justNow: "just now",
      ago: (n, unit) => `${n} ${unit}${n === 1 ? "" : "s"} ago`,
      units: { minute: "minute", hour: "hour", day: "day" },
    },
    zh: {
      htmlLang: "zh-CN", other: "English", skip: "跳到正文",
      theme: { system: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色" },
      console: "控制台", docs: "文档", api: "API", status: "运行状态",
      updated: "更新于", never: "从未", justNow: "刚刚",
      ago: (n, unit) => `${n} ${unit}前`,
      units: { minute: "分钟", hour: "小时", day: "天" },
    },
  };
  const THEMES = ["system", "light", "dark"];

  let strings = SHARED;
  let lang = "en";
  let theme = THEMES.includes(store.get("dc-pages-theme")) ? store.get("dc-pages-theme") : "system";
  let render = () => {};

  function applyTheme() {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }
  applyTheme();

  function preferredLang() {
    const kept = store.get("dc-pages-lang");
    if (kept === "en" || kept === "zh") return kept;
    return (navigator.languages || [navigator.language || "en"]).some((l) => /^zh\b/i.test(l)) ? "zh" : "en";
  }

  const t = (key) => {
    const value = strings[lang][key];
    return value === undefined ? strings.en[key] : value;
  };

  function locale() { return lang === "zh" ? "zh-CN" : "en-US"; }

  const fmt = {
    num(n, digits = 0) {
      return n == null ? "—" : Number(n).toLocaleString(locale(), { maximumFractionDigits: digits, minimumFractionDigits: digits });
    },
    pct(x) {
      if (x == null) return "—";
      const p = x * 100;
      const digits = p >= 99.995 ? 0 : p >= 99 ? 2 : 1;
      return `${p.toFixed(digits)}%`;
    },
    capacity(mw) {
      if (mw == null) return "—";
      return mw >= 1000 ? `${fmt.num(mw / 1000, 1)} GW` : `${fmt.num(mw)} MW`;
    },
    date(iso, opts = { month: "short", day: "numeric" }) {
      return iso ? new Date(iso).toLocaleDateString(locale(), opts) : "—";
    },
    day(isoDate) {
      // A calendar date from the server, which is not a moment: read it as local noon.
      return new Date(`${isoDate}T12:00:00`).toLocaleDateString(locale(), { month: "short", day: "numeric", weekday: "short" });
    },
    time(iso) {
      return iso ? new Date(iso).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
    },
    dateTime(iso) {
      if (!iso) return "—";
      return new Date(iso).toLocaleString(locale(), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
    },
    ago(iso) {
      if (!iso) return t("never");
      const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
      const units = t("units");
      if (minutes < 1) return t("justNow");
      if (minutes < 60) return t("ago")(minutes, units.minute);
      const hours = Math.round(minutes / 60);
      if (hours < 48) return t("ago")(hours, units.hour);
      return t("ago")(Math.round(hours / 24), units.day);
    },
    span(startIso, endIso) {
      const minutes = Math.max(0, Math.round((new Date(endIso) - new Date(startIso)) / 60000));
      if (minutes < 60) return lang === "zh" ? `${minutes} 分钟` : `${minutes} min`;
      const h = Math.floor(minutes / 60);
      const m = minutes % 60;
      return lang === "zh" ? `${h} 小时 ${m} 分` : `${h} h ${m} min`;
    },
  };

  function paint() {
    const s = strings[lang];
    document.documentElement.lang = s.htmlLang;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const value = t(el.dataset.i18n);
      if (typeof value === "string") el.textContent = value;
    });
    const langBtn = $("lang-btn");
    if (langBtn) {
      langBtn.textContent = s.other;
      langBtn.lang = lang === "en" ? "zh" : "en";
    }
    const themeBtn = $("theme-btn");
    if (themeBtn) {
      themeBtn.setAttribute("aria-label", t("theme")[theme]);
      themeBtn.title = t("theme")[theme];
    }
    render(lang);
  }

  function links() {
    const set = (id, href) => { const el = $(id); if (el && href) el.href = href; };
    set("brand", meta("dc-base") || "/");
    set("console-link", meta("dc-console"));
    set("foot-console", meta("dc-console"));
    set("foot-docs", meta("dc-docs"));
    set("foot-api", meta("dc-api"));
    set("foot-status", meta("dc-status"));
  }

  function start(pageStrings, onRender) {
    strings = {
      en: { ...SHARED.en, ...(pageStrings.en || {}) },
      zh: { ...SHARED.zh, ...(pageStrings.zh || {}) },
    };
    render = onRender || render;
    lang = preferredLang();
    links();
    $("lang-btn")?.addEventListener("click", () => {
      lang = lang === "en" ? "zh" : "en";
      store.set("dc-pages-lang", lang);
      paint();
    });
    $("theme-btn")?.addEventListener("click", () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      store.set("dc-pages-theme", theme);
      applyTheme();
      paint();
    });
    window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
      if (theme === "system") paint();
    });
    paint();
  }

  /* A small floating note for anything hovered or focused, positioned at the
   * pointer and kept inside the window. One per page. */
  let tipEl = null;
  function tip(event, html) {
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "tip";
      tipEl.setAttribute("role", "tooltip");
      document.body.appendChild(tipEl);
    }
    if (html == null) { tipEl.hidden = true; return; }
    tipEl.innerHTML = html;
    tipEl.hidden = false;
    const r = tipEl.getBoundingClientRect();
    let x = event.clientX + 14;
    let y = event.clientY - r.height - 12;
    if (event.clientX === undefined && event.target) {
      const box = event.target.getBoundingClientRect();
      x = box.left; y = box.top - r.height - 8;
    }
    x = Math.min(Math.max(8, x), window.innerWidth - r.width - 8);
    y = y < 8 ? (event.clientY || 0) + 18 : y;
    tipEl.style.left = `${x}px`;
    tipEl.style.top = `${y}px`;
  }

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  window.DCPages = {
    start, meta, store, fmt, tip, escape, css,
    get lang() { return lang; },
    t,
  };
})();
