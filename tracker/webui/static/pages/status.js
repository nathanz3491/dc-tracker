/* The status page: draws `status.json`, and asks for it again every minute.
 *
 * Everything shown comes from that one object, built by `webui/statussite.py`.
 * Text from it is escaped before it is written; the only free text in it is an
 * operator's note.
 */
(function () {
  "use strict";
  const P = window.DCPages;
  const $ = (id) => document.getElementById(id);
  const esc = P.escape;

  const STRINGS = {
    en: {
      tag: "status", openConsole: "Open the console", loading: "Checking…",
      overall: {
        operational: "Everything is answering",
        degraded: "Some answers are slow or failing",
        partial_outage: "Part of the tracker is down",
        outage: "The tracker is down",
        unknown: "Not checked yet",
        stale: "The checks have stopped reporting",
      },
      checkedEvery: (min, ago) => `Checked every ${min} minutes · last check ${ago}`,
      noChecks: "No check has run yet. They start once the status page is live, every few minutes after that.",
      servicesEyebrow: "Public addresses", servicesTitle: "Is it answering?",
      servicesNote: (days) => `Each bar is a day, the last ${days}. Green answered all day; amber missed some of it; red missed much of it; grey was not checked.`,
      names: { console: "Console", api: "API", docs: "Documentation", map: "Map" },
      states: { operational: "Operational", degraded: "Degraded", outage: "Down", unknown: "Unknown" },
      uptime: (pct) => `${pct} uptime`, latency: (ms) => `${ms} ms`,
      sinceMonitoring: (when) => `since ${when}`,
      noData: "Not checked", dayUp: (pct) => `${pct} answered`,
      today: "Today", daysAgo: (n) => `${n} days ago`,
      nightlyEyebrow: "The nightly update", nightlyTitle: "Is the data current?",
      nightlyNote: "Every evening the tracker reads new articles, settles what it can and recounts. When it ran, and how it ended:",
      lastUpdate: "Last update", lastRead: "Last article read", lastChange: "Last value changed", campuses: "Campuses tracked",
      outcome: {
        settled: "Finished", spend_limit: "Stopped at its spending limit", time_limit: "Stopped at its time limit",
        complete: "Finished", interrupted: "Cut off", running: "Running now",
      },
      noNights: "No nightly update is on record.",
      incidentsEyebrow: "The last 90 days", incidentsTitle: "What went wrong",
      noIncidents: "Nothing in the last 90 days.",
      kinds: {
        offline: "Every address was unreachable",
        restart: "The server restarted",
        silent: "The checks have stopped reporting",
        night_interrupted: "The nightly update was cut off",
      },
      bodies: {
        offline: "No check ran in this stretch: the server was not running.",
        restart: (boot) => `The machine booted again at ${boot}. Nothing answered in between.`,
        silent: "The page is being served, but no check has reported since. What it shows may be out of date.",
        night_interrupted: "It stopped before finishing. Whatever it had settled is kept; the next night runs in full.",
        outage: "Failed every check in this stretch.",
      },
      down: (names) => `${names} down`,
      ongoing: "ongoing", and: " and ",
      howTitle: "How this page knows",
      how1: "Every few minutes the server asks each public address for a page, over the internet, the way a reader would. A page that answers is up; one that fails twice running is down.",
      how2: "The checks run on the same server as everything they check, so when that server is off nothing is checked at all. A stretch with no checks is counted as downtime for every address, and marked as a restart when the machine booted during it — never left out, which would make the worst days look the best.",
      how3: "The nightly update is read from its own log. Times are shown in your time zone.",
      failed: "Could not load the status. Retrying…",
    },
    zh: {
      tag: "运行状态", openConsole: "打开控制台", loading: "检查中…",
      overall: {
        operational: "一切正常",
        degraded: "部分响应缓慢或失败",
        partial_outage: "部分服务无法访问",
        outage: "服务无法访问",
        unknown: "尚未检查",
        stale: "检查已停止报告",
      },
      checkedEvery: (min, ago) => `每 ${min} 分钟检查一次 · 最近一次在${ago}`,
      noChecks: "还没有进行过检查。状态页上线后开始，此后每隔几分钟检查一次。",
      servicesEyebrow: "公开地址", servicesTitle: "能否访问？",
      servicesNote: (days) => `每一格代表一天，共最近 ${days} 天。绿色表示全天可访问；琥珀色表示部分时间无法访问；红色表示大部分时间无法访问；灰色表示未检查。`,
      names: { console: "控制台", api: "API", docs: "文档", map: "地图" },
      states: { operational: "正常", degraded: "降级", outage: "中断", unknown: "未知" },
      uptime: (pct) => `可用率 ${pct}`, latency: (ms) => `${ms} 毫秒`,
      sinceMonitoring: (when) => `自 ${when} 起`,
      noData: "未检查", dayUp: (pct) => `可访问 ${pct}`,
      today: "今天", daysAgo: (n) => `${n} 天前`,
      nightlyEyebrow: "每晚更新", nightlyTitle: "数据是否最新？",
      nightlyNote: "追踪系统每天傍晚读取新文章、处理能处理的问题并重新统计。运行时间和结束方式如下：",
      lastUpdate: "最近一次更新", lastRead: "最近读取文章", lastChange: "最近数值变动", campuses: "追踪的园区",
      outcome: {
        settled: "已完成", spend_limit: "达到花费上限后停止", time_limit: "达到时间上限后停止",
        complete: "已完成", interrupted: "中途中断", running: "正在运行",
      },
      noNights: "没有每晚更新的记录。",
      incidentsEyebrow: "最近 90 天", incidentsTitle: "出过的问题",
      noIncidents: "最近 90 天没有问题。",
      kinds: {
        offline: "所有地址都无法访问",
        restart: "服务器重启",
        silent: "检查已停止报告",
        night_interrupted: "每晚更新中途中断",
      },
      bodies: {
        offline: "这段时间没有进行任何检查：服务器没有运行。",
        restart: (boot) => `机器于 ${boot} 重新启动，期间没有任何服务响应。`,
        silent: "页面仍在提供，但此后没有检查结果，显示的内容可能已过时。",
        night_interrupted: "在完成前停止。已处理的部分会保留，下一晚会完整运行。",
        outage: "这段时间每次检查都失败。",
      },
      down: (names) => `${names}中断`,
      ongoing: "仍在持续", and: "和",
      howTitle: "这个页面如何得知",
      how1: "服务器每隔几分钟就像读者一样，通过互联网访问每个公开地址。能打开就是正常；连续两次失败就算中断。",
      how2: "检查程序与被检查的服务运行在同一台服务器上，所以服务器关闭时不会进行任何检查。没有检查的时段会计为所有地址的停机时间；如果期间机器重新启动，则标记为重启。这些时段从不被略去，否则最糟糕的日子反而会显得最好。",
      how3: "每晚更新的情况读取自它自己的日志。时间以你所在的时区显示。",
      failed: "无法加载状态，正在重试…",
    },
  };

  let data = null;
  let failed = false;
  const base = P.meta("dc-base") || "/";

  function quality(uptime) {
    if (uptime == null) return "none";
    if (uptime >= 0.995) return "ok";
    if (uptime >= 0.95) return "minor";
    return "major";
  }

  function renderOverall(t) {
    const box = $("overall");
    if (!data) {
      box.dataset.state = "unknown";
      $("overall-title").textContent = failed ? t("failed") : t("loading");
      $("overall-sub").textContent = "";
      return;
    }
    box.dataset.state = data.overall;
    // "unknown" is either nothing checked yet or checks that have gone quiet; the
    // second has a history, and is the one worth saying plainly.
    const stale = data.overall === "unknown" && data.monitoring_since;
    $("overall-title").textContent = t("overall")[stale ? "stale" : data.overall];
    const last = data.services.map((s) => s.checked_at).filter(Boolean).sort().pop();
    $("overall-sub").textContent = last
      ? t("checkedEvery")(Math.round(data.interval_s / 60), P.fmt.ago(last))
      : t("noChecks");
  }

  function renderServices(t) {
    $("services-note").textContent = t("servicesNote")(data ? data.history_days : 90);
    const list = $("services");
    if (!data || !data.services.length) {
      list.innerHTML = `<li class="empty">${esc(t("noChecks"))}</li>`;
      return;
    }
    const days = data.services[0].days.length;
    list.innerHTML = data.services.map((s) => {
      const name = t("names")[s.id] || s.id;
      const meta = [
        s.uptime != null ? t("uptime")(P.fmt.pct(s.uptime)) : null,
        s.latency_ms != null ? t("latency")(P.fmt.num(s.latency_ms)) : null,
      ].filter(Boolean).join(" · ");
      const bars = s.days.map((d, i) => {
        const label = `${P.fmt.day(d.date)} — ${d.uptime == null ? t("noData") : t("dayUp")(P.fmt.pct(d.uptime))}`;
        return `<span class="day" data-q="${quality(d.uptime)}" data-i="${i}" tabindex="${i === days - 1 ? 0 : -1}" aria-label="${esc(label)}"></span>`;
      }).join("");
      const nameHtml = s.url
        ? `<a class="service-name" href="${esc(s.url)}" rel="noopener">${esc(name)}</a>`
        : `<span class="service-name">${esc(name)}</span>`;
      return `<li class="service" data-id="${esc(s.id)}">
        <div class="service-head">${nameHtml}<span class="service-meta">${esc(meta)}</span>
          <span class="service-state state-${esc(s.state)}">${esc(t("states")[s.state] || s.state)}</span></div>
        <div class="days" role="group" aria-label="${esc(name)}">${bars}</div>
        <div class="days-axis"><span class="axis-long">${esc(t("daysAgo")(days - 1))}</span><span class="axis-short">${esc(t("daysAgo")(Math.min(days, 30) - 1))}</span><b>${esc(t("today"))}</b></div>
      </li>`;
    }).join("");
  }

  function renderNightly(t) {
    const fresh = $("fresh");
    const nights = $("nights");
    if (!data) { fresh.innerHTML = ""; nights.innerHTML = ""; return; }
    const latest = data.nightly.latest;
    const outcomeOf = (n) => (n.outcome === "complete" ? (n.stop || "complete") : n.outcome);
    const facts = [];
    if (latest) {
      const when = latest.ended_at || latest.last_seen_at || latest.started_at;
      facts.push([t("lastUpdate"), P.fmt.ago(when), `${P.fmt.dateTime(latest.started_at)} · ${t("outcome")[outcomeOf(latest)]}`]);
    }
    facts.push([t("lastRead"), P.fmt.ago(data.data.last_read_at), P.fmt.dateTime(data.data.last_read_at)]);
    facts.push([t("lastChange"), P.fmt.ago(data.data.last_change_at), P.fmt.dateTime(data.data.last_change_at)]);
    if (data.data.campuses != null) facts.push([t("campuses"), P.fmt.num(data.data.campuses), ""]);
    fresh.innerHTML = `<dl style="display:contents">${facts.map(([k, v, small]) =>
      `<div class="fact"><dt>${esc(k)}</dt><dd>${esc(v)}</dd>${small ? `<small>${esc(small)}</small>` : ""}</div>`).join("")}</dl>`;

    if (!data.nightly.nights.length) {
      nights.innerHTML = `<li class="empty">${esc(t("noNights"))}</li>`;
      return;
    }
    const longest = Math.max(60, ...data.nightly.nights.map((n) => n.minutes || 0));
    nights.innerHTML = data.nightly.nights.map((n) => {
      const outcome = outcomeOf(n);
      const tone = n.outcome === "interrupted" ? "bad" : n.outcome === "running" ? "busy"
        : (n.stop === "spend_limit" || n.stop === "time_limit") ? "warn" : "ok";
      const end = n.ended_at || n.last_seen_at;
      const span = end ? `${P.fmt.time(n.started_at)} → ${P.fmt.time(end)} · ${P.fmt.span(n.started_at, end)}` : P.fmt.time(n.started_at);
      const width = Math.max(4, Math.round(((n.minutes || 0) / longest) * 100));
      return `<li class="night" data-outcome="${esc(n.outcome)}">
        <span><span class="night-date">${esc(P.fmt.date(n.started_at, { weekday: "short", month: "short", day: "numeric" }))}</span><br>
          <span class="night-span">${esc(span)}</span></span>
        <span class="night-bar" aria-hidden="true"><i style="width:${width}%"></i></span>
        <span class="badge" data-tone="${tone}">${esc(t("outcome")[outcome] || outcome)}</span>
      </li>`;
    }).join("");
  }

  function renderIncidents(t) {
    const list = $("incidents");
    if (!data) { list.innerHTML = ""; return; }
    if (!data.incidents.length) {
      list.innerHTML = `<li class="empty">${esc(t("noIncidents"))}</li>`;
      return;
    }
    const names = (ids) => ids.map((id) => t("names")[id] || id).join(t("and"));
    list.innerHTML = data.incidents.map((i) => {
      let title;
      let body;
      if (i.kind === "note") {
        title = (P.lang === "zh" && i.title_zh) || i.title;
        body = "";
      } else if (i.kind === "outage") {
        title = t("down")(names(i.services));
        body = t("bodies").outage;
      } else {
        title = t("kinds")[i.kind] || i.kind;
        const b = t("bodies")[i.kind];
        body = typeof b === "function" ? b(P.fmt.dateTime(i.booted_at)) : b || "";
      }
      const when = i.end
        ? `${P.fmt.dateTime(i.start)} – ${P.fmt.time(i.end)} · ${P.fmt.span(i.start, i.end)}`
        : `${P.fmt.dateTime(i.start)} – ${t("ongoing")}`;
      return `<li class="incident" data-kind="${esc(i.kind)}">
        <span class="incident-mark" aria-hidden="true"></span>
        <span class="incident-title">${esc(title)}</span>
        <span class="incident-when">${esc(when)}</span>
        ${body ? `<p class="incident-body">${esc(body)}</p>` : ""}
      </li>`;
    }).join("");
  }

  function render() {
    const t = P.t;
    renderOverall(t);
    renderServices(t);
    renderNightly(t);
    renderIncidents(t);
    $("updated").textContent = data ? `${t("updated")} ${P.fmt.time(data.generated_at)}` : "";
  }

  /* One tooltip for every day bar, by delegation: 360 listeners would be the same
   * thing, slower. Keyboard: arrow keys walk the focused service's days. */
  function bindDays() {
    const list = $("services");
    const show = (event) => {
      const day = event.target.closest(".day");
      if (!day) { P.tip(event, null); return; }
      const [when, what] = day.getAttribute("aria-label").split(" — ");
      P.tip(event, `<b>${esc(when)}</b><br><span>${esc(what)}</span>`);
    };
    list.addEventListener("mouseover", show);
    list.addEventListener("mousemove", show);
    list.addEventListener("mouseleave", (e) => P.tip(e, null));
    list.addEventListener("focusin", show);
    list.addEventListener("focusout", (e) => P.tip(e, null));
    list.addEventListener("keydown", (event) => {
      const day = event.target.closest(".day");
      if (!day || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const days = [...day.parentElement.children].filter((d) => d.offsetParent !== null);
      let at = days.indexOf(day);
      if (event.key === "ArrowLeft") at = Math.max(0, at - 1);
      if (event.key === "ArrowRight") at = Math.min(days.length - 1, at + 1);
      if (event.key === "Home") at = 0;
      if (event.key === "End") at = days.length - 1;
      event.preventDefault();
      day.tabIndex = -1;
      days[at].tabIndex = 0;
      days[at].focus();
    });
  }

  async function load() {
    try {
      const response = await fetch(`${base}status.json`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      failed = false;
    } catch (_) {
      failed = true;
    }
    render();
  }

  P.start(STRINGS, render);
  bindDays();
  load();
  setInterval(load, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
})();
