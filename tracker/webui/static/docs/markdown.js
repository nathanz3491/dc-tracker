/* The documentation's Markdown renderer.
 *
 * Our own, and deliberately small: the docs are written here, by us, so the
 * renderer needs the subset they use and nothing else. A general-purpose library
 * would be a vendored dependency to pin and audit for a feature list we would not
 * touch.
 *
 * WHAT IT READS
 *   # Title                       the page title (the first one only)
 *   ## Heading / ### / ####       sections, each with an id for linking
 *   paragraphs, - and 1. lists (nested by indenting two spaces or more)
 *   ```lang fenced code```        > quotes, and > [!NOTE] / [!TIP] / [!WARNING] callouts
 *   | tables | with | :---: |     --- rules
 *   `code`  **bold**  *italic*  [text](link)
 *   {{v:reported|1,200 MW}}       a value drawn with the console's provenance underline
 *   {{tier:T2}}  {{kbd:/}}         a tier badge, a key
 *
 * SAFETY
 *   Every character of the source is escaped before any markup is added, and no
 *   raw HTML passes through. A link keeps its target only if it is http(s),
 *   mailto, an in-page #anchor or a relative path; anything else (javascript:,
 *   data:, a protocol-relative //host) is rendered as plain text.
 */
(function () {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

  const VALUE_KINDS = new Set(["reported", "derived", "unconfirmed", "inferred", "defaulted", "missing", "na"]);
  const CALLOUTS = {
    NOTE: { en: "Note", zh: "说明" },
    TIP: { en: "Tip", zh: "提示" },
    WARNING: { en: "Watch out", zh: "注意" },
    IMPORTANT: { en: "Important", zh: "重要" },
  };

  /* An id for a heading: letters and digits of any script, hyphens between words. */
  function slugify(text) {
    const s = String(text)
      .toLowerCase()
      .replace(/<[^>]*>/g, "")
      .replace(/[^\p{L}\p{N}\s-]/gu, "")
      .trim()
      .replace(/\s+/g, "-");
    return s || "section";
  }

  function safeHref(raw) {
    const href = String(raw).trim();
    if (/^(https?:|mailto:)/i.test(href)) return { href, external: /^https?:/i.test(href) };
    if (href.startsWith("#")) return { href, external: false };
    // Relative: no scheme, not protocol-relative, not a backslash trick.
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//") || href.includes("\\")) return null;
    // `fields.md#phase` and `fields#phase` both name the page `fields`.
    return { href: href.replace(/\.md(?=$|#)/, ""), external: false };
  }

  /* Inline markup over one line of already-escaped text. */
  function inline(src) {
    const stash = [];
    const keep = (html) => `\u0000${stash.push(html) - 1}\u0000`;
    let s = escape(src);
    // Code spans first, so nothing inside them is read as markup.
    s = s.replace(/`([^`]+)`/g, (_, code) => keep(`<code>${code}</code>`));
    s = s.replace(/\{\{(v|tier|kbd):([^}|]+)(?:\|([^}]*))?\}\}/g, (all, kind, arg, label) => {
      if (kind === "v" && VALUE_KINDS.has(arg)) {
        return keep(`<span class="dv dv--${arg}">${label || "1,200 MW"}</span>`);
      }
      if (kind === "tier" && /^(T[0-3]|—)$/.test(arg)) {
        return keep(`<span class="tier tier--${arg === "—" ? "none" : arg}">${arg}</span>`);
      }
      if (kind === "kbd") return keep(`<kbd>${arg}</kbd>`);
      return all;
    });
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (all, text, target) => {
      const link = safeHref(target.replace(/&amp;/g, "&"));
      if (!link) return text;
      const attrs = link.external ? ' target="_blank" rel="noopener noreferrer"' : "";
      return keep(`<a href="${escape(link.href)}"${attrs}>${text}</a>`);
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^\w*])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^\w])_([^_\s][^_]*?)_(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => stash[Number(i)]);
  }

  const isFence = (line) => /^\s*```/.test(line);
  const isHeading = (line) => /^#{1,4}\s+\S/.test(line);
  const isRule = (line) => /^\s*(---|\*\*\*)\s*$/.test(line);
  const listItem = (line) => /^(\s*)([-*]|\d+[.)])\s+(.*)$/.exec(line);
  const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
  const isTableRule = (line) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line);
  const blank = (line) => !line.trim();

  /* A table row's cells. A `|` inside `{{v:kind|label}}` or escaped as `\|` is
   * part of a cell, not a border. */
  function cells(line) {
    const kept = [];
    const row = line.trim().replace(/\{\{[^}]*\}\}/g, (m) => `\u0001${kept.push(m) - 1}\u0001`);
    return row.replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) =>
      c.trim().replace(/\\\|/g, "|").replace(/\u0001(\d+)\u0001/g, (_, i) => kept[Number(i)]));
  }

  function table(lines) {
    const head = cells(lines[0]);
    const align = cells(lines[1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
    const td = (tag, c, i) => `<${tag}${align[i] ? ` style="text-align:${align[i]}"` : ""}>${inline(c)}</${tag}>`;
    const body = lines.slice(2).map((l) => `<tr>${cells(l).map((c, i) => td("td", c, i)).join("")}</tr>`);
    return `<div class="table-wrap"><table><thead><tr>${head.map((c, i) => td("th", c, i)).join("")}</tr></thead><tbody>${body.join("")}</tbody></table></div>`;
  }

  /* A list starting at `start`, at the indentation of its first item. */
  function list(lines, start) {
    const first = listItem(lines[start]);
    const indent = first[1].length;
    const ordered = /\d/.test(first[2]);
    const items = [];
    let i = start;
    while (i < lines.length) {
      const m = listItem(lines[i]);
      if (m && m[1].length === indent) {
        items.push([m[3]]);
        i += 1;
        continue;
      }
      if (blank(lines[i])) {
        // A blank line ends the list unless the next line carries on inside it.
        const next = lines[i + 1];
        if (next !== undefined && (/^\s+\S/.test(next) && next.search(/\S/) > indent)) {
          items[items.length - 1].push("");
          i += 1;
          continue;
        }
        break;
      }
      if (lines[i].search(/\S/) > indent && items.length) {
        items[items.length - 1].push(lines[i]);
        i += 1;
        continue;
      }
      break;
    }
    const html = items.map((body) => {
      const [text, ...rest] = body;
      const nested = rest.filter((l) => l.trim());
      const inner = nested.length ? blocks(nested.map((l) => l.replace(new RegExp(`^\\s{0,${indent + 4}}`), "")), {}).html : "";
      return `<li>${inline(text)}${inner}</li>`;
    });
    const tag = ordered ? "ol" : "ul";
    return { html: `<${tag}>${html.join("")}</${tag}>`, next: i };
  }

  /* Block structure. `ctx` collects headings and the title for the caller. */
  function blocks(lines, ctx, lang) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (blank(line)) { i += 1; continue; }

      if (isFence(line)) {
        const fence = /^\s*```\s*([\w-]*)/.exec(line);
        const code = [];
        i += 1;
        while (i < lines.length && !isFence(lines[i])) code.push(lines[i++]);
        i += 1;
        const cls = fence[1] ? ` class="lang-${escape(fence[1])}"` : "";
        out.push(`<pre><code${cls}>${escape(code.join("\n"))}</code></pre>`);
        continue;
      }

      if (isHeading(line)) {
        const m = /^(#{1,4})\s+(.*?)\s*(?:\{#([a-z0-9-]+)\})?\s*$/.exec(line);
        const level = m[1].length;
        const text = m[2];
        if (level === 1 && ctx.headings && ctx.title === undefined) {
          ctx.title = text;
          out.push(`<h1>${inline(text)}</h1>`);
        } else if (ctx.headings) {
          let id = m[3] || slugify(text);
          const base = id;
          for (let n = 2; ctx.ids.has(id); n += 1) id = `${base}-${n}`;
          ctx.ids.add(id);
          ctx.headings.push({ level, id, text: text.replace(/[`*_]/g, "") });
          out.push(`<h${level} id="${id}"><a class="anchor" href="#${id}" aria-hidden="true" tabindex="-1">#</a>${inline(text)}</h${level}>`);
        } else {
          out.push(`<h${level}>${inline(text)}</h${level}>`);
        }
        i += 1;
        continue;
      }

      if (isRule(line)) { out.push("<hr>"); i += 1; continue; }

      if (isTableRow(line) && i + 1 < lines.length && isTableRule(lines[i + 1])) {
        const rows = [];
        while (i < lines.length && isTableRow(lines[i])) rows.push(lines[i++]);
        out.push(table(rows));
        continue;
      }

      if (listItem(line)) {
        const got = list(lines, i);
        out.push(got.html);
        i = got.next;
        continue;
      }

      if (/^\s*>/.test(line)) {
        const quoted = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s*>\s?/, ""));
        const call = /^\[!(NOTE|TIP|WARNING|IMPORTANT)\]\s*$/.exec(quoted[0] || "");
        if (call) {
          const label = CALLOUTS[call[1]][lang] || CALLOUTS[call[1]].en;
          const inner = blocks(quoted.slice(1), {}, lang).html;
          out.push(`<aside class="callout callout--${call[1].toLowerCase()}"><p class="callout-title">${label}</p>${inner}</aside>`);
        } else {
          out.push(`<blockquote>${blocks(quoted, {}, lang).html}</blockquote>`);
        }
        continue;
      }

      const para = [];
      while (
        i < lines.length && !blank(lines[i]) && !isFence(lines[i]) && !isHeading(lines[i]) &&
        !isRule(lines[i]) && !listItem(lines[i]) && !/^\s*>/.test(lines[i]) &&
        !(isTableRow(lines[i]) && isTableRule(lines[i + 1] || ""))
      ) para.push(lines[i++].trim());
      out.push(`<p>${inline(para.join(" "))}</p>`);
    }
    return { html: out.join("\n") };
  }

  /* `{ html, title, headings }` for one page of Markdown. */
  function render(source, lang) {
    const ctx = { headings: [], ids: new Set(), title: undefined };
    const lines = String(source).replace(/\r\n?/g, "\n").split("\n");
    const { html } = blocks(lines, ctx, lang || "en");
    return { html, title: ctx.title, headings: ctx.headings };
  }

  /* The searchable text of a page, cut into its sections. */
  function sections(source) {
    const parts = [];
    let current = { heading: "", id: "", lines: [] };
    const ids = new Set();
    let inFence = false;
    for (const raw of String(source).replace(/\r\n?/g, "\n").split("\n")) {
      if (isFence(raw)) inFence = !inFence;
      const m = !inFence && /^(#{2,4})\s+(.*?)\s*(?:\{#([a-z0-9-]+)\})?\s*$/.exec(raw);
      if (m) {
        parts.push(current);
        let id = m[3] || slugify(m[2]);
        const base = id;
        for (let n = 2; ids.has(id); n += 1) id = `${base}-${n}`;
        ids.add(id);
        current = { heading: m[2].replace(/[`*_]/g, ""), id, lines: [] };
      } else if (!/^#\s/.test(raw)) {
        current.lines.push(raw);
      }
    }
    parts.push(current);
    return parts
      .map((p) => ({
        heading: p.heading,
        id: p.id,
        text: p.lines.join(" ")
          .replace(/\{\{(?:v|tier|kbd):([^}|]+)(?:\|([^}]*))?\}\}/g, (_, a, b) => b || a)
          .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
          .replace(/[`*_>|#]|\[!\w+\]/g, " ")
          .replace(/\s+/g, " ")
          .trim(),
      }))
      .filter((p) => p.heading || p.text);
  }

  window.DocsMarkdown = { render, sections, slugify, escape };
})();
