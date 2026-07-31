/* ANSI SGR → React elements.
 *
 * The console runs the CLI with colour forced on, so what arrives over SSE is
 * exactly the byte stream a terminal would get. This turns it back into styled
 * spans so the browser shows what the terminal shows: red rejections, amber
 * 待确认, green confirmations, dim hints.
 *
 * It emits React elements, never HTML strings. That is a security property, not
 * a style preference — log lines contain URLs and headlines fetched from the open
 * web, and building markup out of them by hand is how a headline becomes a script
 * tag. React escapes every text child on the way in.
 *
 * Scope is deliberately small: the CLI's whole palette is eight named colours and
 * two attributes (grep says red, bright_red, green, yellow, cyan, magenta, white,
 * bold, dim). 256-colour and truecolor are handled because `COLORTERM=truecolor`
 * makes Rich emit them, and everything else — cursor moves, erase codes, OSC
 * sequences — is dropped rather than rendered.
 */

/* The 16 ANSI colours, as CSS variables resolved against the terminal surface in
 * app.css. Not the page palette: these sit on a dark background in both themes,
 * because that is what the colours were chosen for. Amber on cream is a different
 * colour to amber on charcoal, and the point is fidelity. */
const BASE = [
  "--t-black", "--t-red", "--t-green", "--t-yellow",
  "--t-blue", "--t-magenta", "--t-cyan", "--t-white",
];
const BRIGHT = [
  "--t-bright-black", "--t-bright-red", "--t-bright-green", "--t-bright-yellow",
  "--t-bright-blue", "--t-bright-magenta", "--t-bright-cyan", "--t-bright-white",
];

/* xterm's 256-colour cube, for the 8-bit codes Rich emits on some platforms. */
function xterm256(n) {
  if (n < 8) return `var(${BASE[n]})`;
  if (n < 16) return `var(${BRIGHT[n - 8]})`;
  if (n < 232) {
    const i = n - 16;
    const step = (v) => [0, 95, 135, 175, 215, 255][v];
    return `rgb(${step(Math.floor(i / 36))} ${step(Math.floor(i / 6) % 6)} ${step(i % 6)})`;
  }
  const grey = 8 + (n - 232) * 10;
  return `rgb(${grey} ${grey} ${grey})`;
}

const EMPTY = { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, reverse: false };

/* One SGR sequence against the running state. Unknown codes are ignored rather
 * than guessed at. */
function apply(state, codes) {
  let next = { ...state };
  for (let i = 0; i < codes.length; i++) {
    const code = codes[i];
    if (code === 0) next = { ...EMPTY };
    else if (code === 1) next.bold = true;
    else if (code === 2) next.dim = true;
    else if (code === 3) next.italic = true;
    else if (code === 4) next.underline = true;
    else if (code === 7) next.reverse = true;
    else if (code === 22) { next.bold = false; next.dim = false; }
    else if (code === 23) next.italic = false;
    else if (code === 24) next.underline = false;
    else if (code === 27) next.reverse = false;
    else if (code >= 30 && code <= 37) next.fg = `var(${BASE[code - 30]})`;
    else if (code === 39) next.fg = null;
    else if (code >= 40 && code <= 47) next.bg = `var(${BASE[code - 40]})`;
    else if (code === 49) next.bg = null;
    else if (code >= 90 && code <= 97) next.fg = `var(${BRIGHT[code - 90]})`;
    else if (code >= 100 && code <= 107) next.bg = `var(${BRIGHT[code - 100]})`;
    else if (code === 38 || code === 48) {
      // 38;5;N (256-colour) or 38;2;R;G;B (truecolor). Consume the arguments so
      // they are never mistaken for further codes — an unconsumed `2` would
      // otherwise read as "dim" and quietly wash out the rest of the line.
      const key = code === 38 ? "fg" : "bg";
      if (codes[i + 1] === 5) { next[key] = xterm256(codes[i + 2] || 0); i += 2; }
      else if (codes[i + 1] === 2) {
        next[key] = `rgb(${codes[i + 2] || 0} ${codes[i + 3] || 0} ${codes[i + 4] || 0})`;
        i += 4;
      }
    }
  }
  return next;
}

function styleOf(state) {
  const style = {};
  const fg = state.reverse ? state.bg : state.fg;
  const bg = state.reverse ? state.fg : state.bg;
  if (fg) style.color = fg;
  if (bg) style.background = bg;
  if (state.reverse && !state.bg) style.background = "var(--t-fg)";
  if (state.reverse && !state.fg) style.color = "var(--t-bg)";
  if (state.bold) style.fontWeight = 600;
  // Rich leans on `dim` heavily — 53 uses — for hints and secondary detail.
  // Opacity rather than a dim colour, so it composes with whatever fg is set.
  if (state.dim) style.opacity = 0.62;
  if (state.italic) style.fontStyle = "italic";
  if (state.underline) style.textDecoration = "underline";
  return style;
}

/* CSI sequences. The `m` ones are colour; the rest (cursor moves, erases) are
 * matched only so they can be removed instead of printed as mojibake. */
const CSI = /\x1b\[([0-9;:?]*)([A-Za-z])/g;
/* OSC — window titles and hyperlinks — terminated by BEL or ST. */
const OSC = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;

/**
 * Parse one line into an array of {text, style} runs.
 *
 * Returned as plain data rather than elements so the caller decides how to key
 * them, and so this is testable without a renderer.
 */
export function parseAnsi(line) {
  const clean = String(line == null ? "" : line).replace(OSC, "");
  const runs = [];
  let state = { ...EMPTY };
  let cursor = 0;
  let match;
  CSI.lastIndex = 0;
  while ((match = CSI.exec(clean)) !== null) {
    if (match.index > cursor) {
      runs.push({ text: clean.slice(cursor, match.index), style: styleOf(state) });
    }
    if (match[2] === "m") {
      const codes = match[1].split(";").map((part) => parseInt(part, 10) || 0);
      state = apply(state, match[1] === "" ? [0] : codes);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < clean.length) {
    runs.push({ text: clean.slice(cursor), style: styleOf(state) });
  }
  return runs.filter((run) => run.text.length > 0);
}

/** Everything an ANSI string would print, with the escapes removed. */
export function stripAnsi(line) {
  return parseAnsi(line).map((run) => run.text).join("");
}
