/* dc-tracker docs: the viewer.
 *
 * The server hands over one page shell for every docs address and the Markdown
 * files under `content/`; everything a reader sees is drawn here. Addresses are
 * `<base><lang>/<slug>`, where the base is `/` on the docs hostname and `/docs/`
 * anywhere else (`webui/docsite.py`), so every URL below is built from `BASE`
 * rather than written out.
 *
 * WHAT IT DOES
 *   route        the address → a page of `content/manifest.json`, or a 404
 *   page         the Markdown rendered (`markdown.js`), links rewritten to real
 *                addresses, the contents, "on this page" and previous/next
 *   search       every page in both languages, fetched once on first use and
 *                searched by substring, so Chinese works without a word splitter
 *   language     a switch to the same page in the other language, remembered
 *   theme        system, light or dark, remembered
 *   changelog    on the release notes, the project changelog by month and week,
 *                each change's text fetched only when opened — never the whole file
 *
 * Remembered means `localStorage`, wrapped so a browser that refuses it (a
 * private window, blocked site data) still gets a working page.
 */
(function () {
  "use strict";

  const MD = window.DocsMarkdown;
  // Not a `<base>` element: the public pages' CSP says `base-uri 'none'`, so a
  // browser ignores one. Every address below is built from this instead.
  const BASE = (document.querySelector('meta[name="dc-base"]') || {}).content || "/";
  const CONSOLE = (document.querySelector('meta[name="dc-console"]') || {}).content || "/";
  // The API's real address. Pages write `{{api}}` instead of a hostname: the repo
  // never names the host (CLAUDE.md §6), and the server knows it from `.env`.
  const API = (document.querySelector('meta[name="dc-api"]') || {}).content || "/api/v1";
  // The status page, written `{{status}}` the same way.
  const STATUS = (document.querySelector('meta[name="dc-status"]') || {}).content || "/status/";
  const withAddresses = (text) => text.split("{{api}}").join(API).split("{{status}}").join(STATUS);
  const LANGS = ["en", "zh"];
  const $ = (id) => document.getElementById(id);

  const UI = {
    en: {
      name: "English", htmlLang: "en", search: "Search the docs", onPage: "On this page",
      prev: "Previous", next: "Next", console: "Open the console", skip: "Skip to content",
      menuOpen: "Open the contents", menuClose: "Close the contents", loading: "Loading…",
      failed: "This page could not be loaded. Check your connection and try again.",
      noResults: "Nothing matches", notFound: "Page not found",
      notFoundBody: "There is no page at this address. It may have moved or been renamed.",
      home: "Go to the docs home", docs: "docs",
      theme: { system: "Theme: follows your system", light: "Theme: light", dark: "Theme: dark" },
      changelog: {
        failed: "The changelog could not be loaded. Reload the page to try again.",
        none: "This installation has no changelog.",
        partFailed: "This change could not be loaded. Close it and open it again to retry.",
        summary: (n, since) => `${n.toLocaleString()} changes${since ? ` since ${since}` : ""}, newest first. Open a change to read it; its text loads then.`,
        week: (date) => `Week of ${date}`,
        undated: "Changes, by heading",
        count: (n) => `${n} ${n === 1 ? "change" : "changes"}`,
        openAll: "Open all", closeAll: "Close all",
        groups: {},
      },
    },
    zh: {
      name: "中文", htmlLang: "zh-CN", search: "搜索文档", onPage: "本页内容",
      prev: "上一页", next: "下一页", console: "打开控制台", skip: "跳到正文",
      menuOpen: "打开目录", menuClose: "关闭目录", loading: "加载中…",
      failed: "页面加载失败。请检查网络后重试。",
      noResults: "没有匹配的结果", notFound: "找不到页面",
      notFoundBody: "这个地址没有页面，它可能已被移动或改名。",
      home: "回到文档首页", docs: "文档",
      theme: { system: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色" },
      changelog: {
        failed: "更新日志加载失败。请刷新页面重试。",
        none: "此安装没有更新日志。",
        partFailed: "这一项加载失败。收起后再展开即可重试。",
        summary: (n, since) => `${since ? `自${since}以来` : ""}共 ${n.toLocaleString()} 项改动，最新的在前。展开某一项即可阅读，其内容在展开时才加载。`,
        week: (date) => `${date}这一周`,
        undated: "按标题列出的改动",
        count: (n) => `${n} 项改动`,
        openAll: "全部展开", closeAll: "全部收起",
        groups: { Added: "新增", Changed: "变更", Fixed: "修复", Removed: "移除", Deprecated: "弃用", Security: "安全" },
      },
    },
  };

  const store = {
    get(key) { try { return window.localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (_) { /* not kept */ } },
  };

  /* ---- theme ------------------------------------------------------------- */

  const THEMES = ["system", "light", "dark"];
  function applyTheme(mode) {
    const root = document.documentElement;
    if (mode === "light" || mode === "dark") root.dataset.theme = mode;
    else delete root.dataset.theme;
  }
  let theme = THEMES.includes(store.get("dc-docs-theme")) ? store.get("dc-docs-theme") : "system";
  applyTheme(theme);

  /* ---- the manifest ------------------------------------------------------ */

  let pages = [];        // [{ slug, section, title: {en, zh}, sectionTitle: {en, zh} }], in reading order
  let sections = [];     // the manifest's sections, as given
  const bySlug = new Map();
  const texts = new Map(); // "lang/slug" -> Markdown

  async function loadManifest() {
    const response = await fetch(`${BASE}content/manifest.json`, { cache: "no-cache" });
    if (!response.ok) throw new Error(`manifest: HTTP ${response.status}`);
    const data = await response.json();
    sections = data.sections;
    pages = sections.flatMap((s) => s.pages.map((p) => ({ ...p, section: s.id, sectionTitle: s.title })));
    pages.forEach((p, i) => bySlug.set(p.slug, { ...p, index: i }));
  }

  async function text(lang, slug) {
    const key = `${lang}/${slug}`;
    if (!texts.has(key)) {
      const response = await fetch(`${BASE}content/${key}.md`, { cache: "no-cache" });
      if (!response.ok) throw new Error(`${key}: HTTP ${response.status}`);
      texts.set(key, withAddresses(await response.text()));
    }
    return texts.get(key);
  }

  /* ---- addresses --------------------------------------------------------- */

  const pageHref = (lang, slug, hash = "") => `${BASE}${lang}/${slug}${hash}`;

  function preferredLang() {
    const kept = store.get("dc-docs-lang");
    if (LANGS.includes(kept)) return kept;
    const asked = navigator.languages || [navigator.language || ""];
    return asked.some((l) => /^zh\b/i.test(l)) ? "zh" : "en";
  }

  /* The address's parts after the base. */
  function parts() {
    const path = decodeURIComponent(location.pathname);
    return path.startsWith(BASE) ? path.slice(BASE.length).split("/").filter(Boolean) : [];
  }

  /* ---- chrome ------------------------------------------------------------ */

  let lang = preferredLang();
  let slug = null;

  function chrome() {
    const t = UI[lang];
    document.documentElement.lang = t.htmlLang;
    const other = lang === "en" ? "zh" : "en";
    const langBtn = $("lang-btn");
    langBtn.textContent = UI[other].name;
    langBtn.lang = UI[other].htmlLang;
    langBtn.setAttribute("aria-label", other === "zh" ? "阅读中文版" : "Read in English");
    $("search").placeholder = t.search;
    $("search").setAttribute("aria-label", t.search);
    $("brand").href = `${BASE}${lang}/${pages[0].slug}`;
    $("brand").querySelector(".brand-docs").textContent = t.docs;
    document.querySelector(".skip").textContent = t.skip;
    const consoleLink = $("console-link");
    consoleLink.textContent = t.console;
    consoleLink.href = CONSOLE;
    // On the docs hostname with no console hostname configured, "/" is the docs
    // themselves: a link labelled "console" would go nowhere new.
    consoleLink.hidden = CONSOLE === "/" && BASE === "/";
    const themeBtn = $("theme-btn");
    themeBtn.setAttribute("aria-label", t.theme[theme]);
    themeBtn.title = t.theme[theme];
    const sidebarOpen = $("sidebar").classList.contains("open");
    $("menu-btn").setAttribute("aria-label", sidebarOpen ? t.menuClose : t.menuOpen);
  }

  function sidebar() {
    const html = sections.map((s) => {
      const items = s.pages.map((p) => {
        const current = p.slug === slug ? ' aria-current="page"' : "";
        return `<li><a href="${pageHref(lang, p.slug)}"${current}>${MD.escape(p.title[lang])}</a></li>`;
      }).join("");
      return `<section class="nav-section"><h2>${MD.escape(s.title[lang])}</h2><ul>${items}</ul></section>`;
    }).join("");
    $("sidebar").innerHTML = html;
  }

  function openDrawer(open) {
    $("sidebar").classList.toggle("open", open);
    $("scrim").hidden = !open;
    $("menu-btn").setAttribute("aria-expanded", String(open));
    $("menu-btn").setAttribute("aria-label", open ? UI[lang].menuClose : UI[lang].menuOpen);
  }

  /* ---- one page ---------------------------------------------------------- */

  /* Links in the Markdown are written relative to the page's language folder
   * (`tiers`, `fields#phase`, `../zh/tiers`) and as bare `#anchors`, so each is
   * turned into its real address here: the same page works under either base. */
  function fixLinks(root) {
    const folder = `${location.origin}${BASE}${lang}/`;
    root.querySelectorAll("a[href]").forEach((a) => {
      const raw = a.getAttribute("href");
      if (/^(https?:|mailto:)/i.test(raw)) return;
      if (raw.startsWith("#")) { a.setAttribute("href", pageHref(lang, slug, raw)); return; }
      const url = new URL(raw, folder);
      a.setAttribute("href", url.pathname + url.hash);
    });
  }

  let spy = null;
  function toc(headings) {
    const listed = headings.filter((h) => h.level === 2 || h.level === 3);
    if (spy) { spy.disconnect(); spy = null; }
    if (listed.length < 2) { $("toc").innerHTML = ""; return; }
    const items = listed.map((h) =>
      `<li><a class="depth-${h.level}" href="${pageHref(lang, slug, `#${h.id}`)}" data-id="${h.id}">${MD.escape(h.text)}</a></li>`).join("");
    $("toc").innerHTML = `<h2>${UI[lang].onPage}</h2><ul>${items}</ul>`;

    // The section being read is the last heading above a line just under the top
    // bar, or the last one once the page cannot scroll further.
    const links = new Map([...$("toc").querySelectorAll("a")].map((a) => [a.dataset.id, a]));
    const marks = listed.map((h) => document.getElementById(h.id)).filter(Boolean);
    let queued = false;
    const update = () => {
      queued = false;
      const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      let current = marks[0];
      for (const el of marks) if (el.getBoundingClientRect().top < 110) current = el;
      if (atEnd) current = marks[marks.length - 1];
      links.forEach((a, id) => a.classList.toggle("active", current && id === current.id));
    };
    const onScroll = () => { if (!queued) { queued = true; requestAnimationFrame(update); } };
    window.addEventListener("scroll", onScroll, { passive: true });
    spy = { disconnect: () => window.removeEventListener("scroll", onScroll) };
    update();
  }

  function pager() {
    const here = bySlug.get(slug);
    const prev = pages[here.index - 1];
    const next = pages[here.index + 1];
    const t = UI[lang];
    const link = (p, cls, label) => p
      ? `<a class="${cls}" href="${pageHref(lang, p.slug)}"><small>${label}</small><span>${MD.escape(p.title[lang])}</span></a>`
      : "";
    $("pager").innerHTML = link(prev, "prev", `← ${t.prev}`) + link(next, "next", `${t.next} →`);
  }

  function notFound() {
    slug = null;
    sidebar();
    const t = UI[lang];
    $("article").innerHTML = `<div class="notfound"><h1>${t.notFound}</h1><p>${t.notFoundBody}</p>
      <p><a href="${pageHref(lang, pages[0].slug)}">${t.home}</a></p></div>`;
    $("toc").innerHTML = "";
    $("pager").innerHTML = "";
    document.title = `${t.notFound} — dc-tracker ${t.docs}`;
  }

  async function show(wantLang, wantSlug, { focus }) {
    lang = wantLang;
    slug = wantSlug;
    store.set("dc-docs-lang", lang);
    chrome();
    sidebar();
    openDrawer(false);
    const page = bySlug.get(slug);
    const t = UI[lang];
    if (!texts.has(`${lang}/${slug}`)) $("article").innerHTML = `<p class="loading">${t.loading}</p>`;
    let source;
    try {
      source = await text(lang, slug);
    } catch (_) {
      $("article").innerHTML = `<p class="loading">${t.failed}</p>`;
      return;
    }
    if (lang !== wantLang || slug !== wantSlug) return; // the reader moved on while it loaded
    const out = MD.render(source, lang);
    const crumb = `<p class="crumbs">${MD.escape(page.sectionTitle[lang])}</p>`;
    $("article").innerHTML = crumb + out.html;
    fixLinks($("article"));
    toc(out.headings);
    pager();
    // The changelog's months join "On this page" once its index has arrived.
    mountChangelog($("article")).then((months) => {
      if (months.length && lang === wantLang && slug === wantSlug) toc([...out.headings, ...months]);
    });
    document.title = `${out.title || page.title[lang]} — dc-tracker ${t.docs}`;

    const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target) target.scrollIntoView();
    else window.scrollTo(0, 0);
    if (focus) $("doc").focus({ preventScroll: true });
  }

  async function route({ focus = false } = {}) {
    const [first, second, ...rest] = parts();
    if (!first) {
      history.replaceState(null, "", pageHref(preferredLang(), pages[0].slug));
      return route({ focus });
    }
    if (!LANGS.includes(first)) { lang = preferredLang(); chrome(); return notFound(); }
    if (!second) {
      history.replaceState(null, "", pageHref(first, pages[0].slug));
      return route({ focus });
    }
    if (rest.length || !bySlug.has(second)) { lang = first; chrome(); return notFound(); }
    return show(first, second, { focus });
  }

  function go(href) {
    const url = new URL(href, location.href);
    const samePage = url.pathname === location.pathname;
    history.pushState(null, "", url.pathname + url.hash);
    if (samePage && url.hash) {
      const el = document.getElementById(decodeURIComponent(url.hash.slice(1)));
      if (el) el.scrollIntoView();
      return;
    }
    route({ focus: true });
  }

  /* ---- the changelog --------------------------------------------------------
   *
   * The release notes end with the project's CHANGELOG.md, which is over half a
   * megabyte. The page never loads it whole. It loads an index first — every
   * change's title, heading group and the day it was written, about a twentieth of
   * the file (`webui/docsite.py`) — and draws the history from that: a header per
   * month (also listed under "On this page"), a week at a time, Added / Changed /
   * Fixed within each week, each change a row. A change's text is fetched when its
   * row is opened, in a part shared with its neighbours, so a reader pays only for
   * what they open. The newest week starts open.
   *
   * The changelog is written in English, so its titles and text are marked `lang="en"`
   * on both language pages, and search does not read it. */

  const GROUP_ORDER = ["Added", "Changed", "Fixed", "Removed", "Deprecated", "Security"];
  // "Fixed (earlier)" is still a Fixed heading.
  const groupOf = (name) => GROUP_ORDER.find((g) => name.startsWith(g)) || name;
  const partCache = new Map();
  const DATE_LOCALE = { en: "en-GB", zh: "zh-CN" };

  function dateText(iso, options) {
    return new Intl.DateTimeFormat(DATE_LOCALE[lang], { timeZone: "UTC", ...options })
      .format(new Date(`${iso}T00:00:00Z`));
  }

  /* The Monday a day's week starts on. */
  function weekOf(iso) {
    const d = new Date(`${iso}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
    return d.toISOString().slice(0, 10);
  }

  /* An index's changes as months, weeks and groups, newest first. Undated, they are
   * one block, grouped by heading. */
  function shape(index) {
    const items = index.items.map((it, n) => ({ ...it, n, group: groupOf(it.group) }));
    const weeks = new Map();
    for (const it of items) {
      const key = index.dated && it.date ? weekOf(it.date) : "";
      if (!weeks.has(key)) weeks.set(key, []);
      weeks.get(key).push(it);
    }
    const months = [];
    for (const key of [...weeks.keys()].sort().reverse()) {
      const month = key ? key.slice(0, 7) : "";
      if (!months.length || months[months.length - 1].key !== month) months.push({ key: month, weeks: [] });
      const rank = (g) => (GROUP_ORDER.includes(g) ? GROUP_ORDER.indexOf(g) : GROUP_ORDER.length);
      const groups = new Map();
      for (const it of weeks.get(key)) {
        if (!groups.has(it.group)) groups.set(it.group, []);
        groups.get(it.group).push(it);
      }
      const ordered = [...groups.entries()]
        .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
        .map(([name, list]) => ({
          name,
          items: list.sort((a, b) => (b.date || "").localeCompare(a.date || "") || a.n - b.n),
        }));
      months[months.length - 1].weeks.push({ key, groups: ordered, count: weeks.get(key).length });
    }
    return months;
  }

  function renderWeek(week, open) {
    const t = UI[lang].changelog;
    const title = week.key ? t.week(dateText(week.key, { day: "numeric", month: "long" })) : t.undated;
    const tally = week.groups.map((g) => `${t.groups[g.name] || g.name} ${g.items.length}`).join(" · ");
    const groups = week.groups.map((g) => {
      const rows = g.items.map((it) => {
        const when = it.date
          ? `<time datetime="${it.date}">${dateText(it.date, { day: "numeric", month: "short" })}</time>`
          : "";
        return `<li><details class="cl-entry" data-part="${it.part}" data-at="${it.at}">
          <summary><span class="cl-entry-title" lang="en">${MD.escape(it.title)}</span>${when}</summary>
          <div class="cl-entry-body" lang="en"></div></details></li>`;
      }).join("");
      return `<h4 class="cl-group">${MD.escape(t.groups[g.name] || g.name)}<span class="cl-count">${g.items.length}</span></h4>
        <ul class="cl-list">${rows}</ul>`;
    }).join("");
    return `<details class="cl-week"${open ? " open" : ""}>
      <summary><span class="cl-week-title">${MD.escape(title)}</span>
        <span class="cl-week-tally">${t.count(week.count)} · ${MD.escape(tally)}</span></summary>
      <div class="cl-week-body"><p class="cl-tools"><button type="button" class="cl-all">${t.openAll}</button></p>${groups}</div>
    </details>`;
  }

  /* Draws the changelog into the page; returns its month headings for "On this page". */
  async function mountChangelog(root) {
    const host = root.querySelector("[data-changelog]");
    if (!host) return [];
    const t = UI[lang].changelog;
    host.innerHTML = `<p class="loading">${UI[lang].loading}</p>`;
    let index;
    try {
      const response = await fetch(`${BASE}content/changelog.json`, { cache: "no-cache" });
      if (response.status === 404) { host.innerHTML = `<p class="loading">${t.none}</p>`; return []; }
      if (!response.ok) throw new Error(`changelog: HTTP ${response.status}`);
      index = await response.json();
    } catch (_) {
      host.innerHTML = `<p class="loading">${t.failed}</p>`;
      return [];
    }
    if (!host.isConnected) return []; // the reader moved on while it loaded

    const months = shape(index);
    const dates = index.items.map((it) => it.date).filter(Boolean).sort();
    const since = dates.length ? dateText(dates[0], { day: "numeric", month: "long", year: "numeric" }) : "";
    const headings = [];
    let first = true;
    const html = months.map((m) => {
      const label = m.key ? dateText(`${m.key}-01`, { month: "long", year: "numeric" }) : t.undated;
      const id = `changelog-${m.key || "undated"}`;
      headings.push({ level: 3, id, text: label });
      const weeks = m.weeks.map((w) => { const out = renderWeek(w, first); first = false; return out; }).join("");
      return `<section class="cl-month"><h3 id="${id}"><a class="anchor" href="#${id}" aria-hidden="true" tabindex="-1">#</a>${MD.escape(label)}</h3>${weeks}</section>`;
    }).join("");
    host.innerHTML =
      (index.intro ? `<p class="cl-intro" lang="en">${MD.escape(index.intro)}</p>` : "") +
      `<p class="cl-meta">${t.summary(index.entries, since)}</p>` + html;
    fixLinks(host);

    // `toggle` does not bubble; a capturing listener on the host still hears it.
    host.addEventListener("toggle", (event) => {
      const entry = event.target;
      if (entry.matches && entry.matches("details.cl-entry") && entry.open) loadEntry(entry);
    }, true);
    host.addEventListener("click", (event) => {
      const button = event.target.closest("button.cl-all");
      if (!button) return;
      const entries = [...button.closest(".cl-week").querySelectorAll("details.cl-entry")];
      const open = !entries.every((e) => e.open);
      entries.forEach((e) => { e.open = open; });
      button.textContent = open ? t.closeAll : t.openAll;
    });
    return headings;
  }

  function part(id) {
    if (!partCache.has(id)) {
      partCache.set(id, fetch(`${BASE}content/changelog/${id}.json`, { cache: "no-cache" })
        .then((response) => {
          if (!response.ok) throw new Error(`changelog part: HTTP ${response.status}`);
          return response.json();
        })
        .catch((error) => { partCache.delete(id); throw error; }));
    }
    return partCache.get(id);
  }

  /* An entry as the changelog writes it — "- **Title** (files). Text…", the rest
   * indented two spaces — as its text alone: the row above it shows the title. */
  function entryBody(md) {
    const [head, ...rest] = md.split("\n");
    const text = [head.replace(/^[-*]\s+/, ""), ...rest.map((l) => l.replace(/^ {2}/, ""))].join("\n");
    return text.replace(/^\*\*[\s\S]+?\*\*\s*/, "");
  }

  async function loadEntry(entry) {
    if (entry.dataset.state) return; // loading, or done
    entry.dataset.state = "loading";
    const body = entry.querySelector(".cl-entry-body");
    body.innerHTML = `<p class="loading">${UI[lang].loading}</p>`;
    try {
      const data = await part(entry.dataset.part);
      body.innerHTML = MD.render(entryBody(data.entries[Number(entry.dataset.at)] || ""), "en").html;
      fixLinks(body);
      entry.dataset.state = "done";
    } catch (_) {
      entry.dataset.state = "";
      body.innerHTML = `<p class="loading">${UI[lang].changelog.partFailed}</p>`;
    }
  }

  /* ---- search ------------------------------------------------------------ */

  let index = null;
  let indexing = null;
  let results = [];
  let selected = -1;

  function buildIndex() {
    if (indexing) return indexing;
    indexing = Promise.all(LANGS.flatMap((l) => pages.map(async (p) => {
      let source = "";
      try { source = await text(l, p.slug); } catch (_) { return []; }
      return MD.sections(source).map((s) => ({
        lang: l, slug: p.slug, id: s.id,
        page: p.title[l], section: p.sectionTitle[l],
        heading: s.heading, text: s.text,
        hay: `${p.title[l]} ${s.heading} ${s.text}`.toLowerCase(),
      }));
    }))).then((all) => { index = all.flat(); return index; });
    return indexing;
  }

  function terms(query) {
    const q = query.toLowerCase().trim();
    if (!q) return [];
    return q.split(/\s+/).filter(Boolean);
  }

  function search(query) {
    const words = terms(query);
    if (!words.length || !index) return [];
    const scored = [];
    for (const entry of index) {
      if (!words.every((w) => entry.hay.includes(w))) continue;
      let score = entry.lang === lang ? 100 : 0;
      for (const w of words) {
        if (entry.page.toLowerCase().includes(w)) score += 8;
        if (entry.heading.toLowerCase().includes(w)) score += 5;
        score += Math.min(5, entry.text.toLowerCase().split(w).length - 1);
      }
      scored.push({ entry, score });
    }
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 12).map((s) => s.entry);
  }

  function snippet(entry, words) {
    const body = entry.text;
    const low = body.toLowerCase();
    let at = -1;
    for (const w of words) { at = low.indexOf(w); if (at >= 0) break; }
    const start = Math.max(0, at - 50);
    let cut = body.slice(start, start + 170);
    if (start > 0) cut = `…${cut}`;
    if (start + 170 < body.length) cut = `${cut}…`;
    return mark(cut, words);
  }

  function mark(raw, words) {
    let html = MD.escape(raw);
    for (const w of words) {
      const safe = MD.escape(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      html = html.replace(new RegExp(safe, "gi"), (m) => `<mark>${m}</mark>`);
    }
    return html;
  }

  function drawResults(query) {
    const box = $("search-results");
    const input = $("search");
    const words = terms(query);
    if (!words.length) { box.hidden = true; input.setAttribute("aria-expanded", "false"); return; }
    results = search(query);
    selected = results.length ? 0 : -1;
    if (!results.length) {
      box.innerHTML = `<div class="no-results">${UI[lang].noResults}</div>`;
    } else {
      box.innerHTML = results.map((r, i) => {
        const tag = r.lang === lang ? "" : `<span class="lang-tag">${UI[r.lang].name}</span>`;
        const where = `${MD.escape(r.section)} › ${MD.escape(r.page)}${tag}`;
        const title = mark(r.heading || r.page, words);
        const hash = r.id ? `#${r.id}` : "";
        return `<a class="result" id="result-${i}" role="option" aria-selected="${i === selected}" href="${pageHref(r.lang, r.slug, hash)}">
          <div class="result-where">${where}</div><div class="result-title">${title}</div>
          <div class="result-snippet">${snippet(r, words)}</div></a>`;
      }).join("");
    }
    box.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-activedescendant", selected >= 0 ? `result-${selected}` : "");
  }

  function select(delta) {
    if (!results.length) return;
    selected = (selected + delta + results.length) % results.length;
    $("search-results").querySelectorAll(".result").forEach((el, i) => el.setAttribute("aria-selected", String(i === selected)));
    const el = $(`result-${selected}`);
    if (el) el.scrollIntoView({ block: "nearest" });
    $("search").setAttribute("aria-activedescendant", `result-${selected}`);
  }

  function closeSearch() {
    $("search-results").hidden = true;
    $("search").setAttribute("aria-expanded", "false");
  }

  /* ---- wiring ------------------------------------------------------------ */

  function wire() {
    document.addEventListener("click", (event) => {
      const a = event.target.closest("a[href]");
      if (!a || event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || a.target) return;
      const url = new URL(a.href, location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith(BASE)) return;
      const inside = url.pathname.slice(BASE.length).split("/")[0];
      if (!LANGS.includes(inside)) return;
      event.preventDefault();
      closeSearch();
      $("search").blur();
      go(url.pathname + url.hash);
    });
    window.addEventListener("popstate", () => route());

    $("lang-btn").addEventListener("click", () => {
      const other = lang === "en" ? "zh" : "en";
      store.set("dc-docs-lang", other);
      go(slug ? pageHref(other, slug) : pageHref(other, pages[0].slug));
    });

    $("theme-btn").addEventListener("click", () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      store.set("dc-docs-theme", theme);
      applyTheme(theme);
      chrome();
    });

    $("menu-btn").addEventListener("click", () => openDrawer(!$("sidebar").classList.contains("open")));
    $("scrim").addEventListener("click", () => openDrawer(false));

    const input = $("search");
    let timer = 0;
    input.addEventListener("focus", () => { buildIndex().then(() => input.value && drawResults(input.value)); });
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => buildIndex().then(() => drawResults(input.value)), 80);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); select(1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); select(-1); }
      else if (event.key === "Enter" && selected >= 0) {
        event.preventDefault();
        const href = $(`result-${selected}`).getAttribute("href");
        closeSearch(); input.blur(); go(href);
      } else if (event.key === "Escape") { closeSearch(); input.blur(); }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".search")) closeSearch();
    });
    document.addEventListener("keydown", (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
      if (!typing && (event.key === "/" || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k"))) {
        event.preventDefault();
        input.focus();
        input.select();
      } else if (event.key === "Escape" && $("sidebar").classList.contains("open")) {
        openDrawer(false);
      }
    });
  }

  async function start() {
    // The address's language wins over the remembered one, even for an error.
    if (LANGS.includes(parts()[0])) lang = parts()[0];
    try {
      await loadManifest();
    } catch (_) {
      $("article").innerHTML = `<p class="loading">${UI[lang].failed}</p>`;
      return;
    }
    wire();
    route();
  }

  start();
})();
