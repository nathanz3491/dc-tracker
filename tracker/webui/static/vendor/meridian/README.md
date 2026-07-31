# Meridian, vendored

Copied from the Claude Design project *DC Tracker Console*
(`a71fc737-550b-4a20-9078-67b8cc3397a5`), which is itself a plain-CSS + React
port of the Meridian design system. Nothing here is edited except as noted.

## What is here

| Path | Notes |
| --- | --- |
| `styles.css` | The entry point. Link this one file. |
| `tokens/*.css` | Verbatim, except `fonts.css` — see below. |
| `css/*.css` | **Subsets.** See below. |
| `_ds_bundle.js` | Verbatim. 81 components on `window.MeridianDesignSystem_6e9015`, already compiled to `React.createElement`; needs global `React` and `window.lucide`. |

## The bundle's export block was rebuilt

`_ds_bundle.js` arrived truncated: the read API caps a file at 256 KiB and the
bundle is 262 KB, so it was cut mid-statement inside the trailing
`__ds_ns.X = __ds_scope.X;` block and the IIFE never closed. Nothing registered
on `window`, and the page rendered blank with no error.

All 181 component *definitions* survived — the cut lands after the last one — so
the missing 127 export lines were regenerated from the `@ds-bundle` manifest in
the file's own header comment, and `})();` appended. Verified in the browser:
182 keys on the namespace and an empty `__errors` array.

If you re-pull this file, **check that it ends with `})();`** before trusting it.

## Two deliberate departures

**Fonts are local.** Upstream `tokens/fonts.css` `@import`s the Google Fonts CSS
API. This console makes no network requests at all — the rule
`tracker export html` already follows — so the `@font-face` rules are inlined and
the woff2 files live in `../fonts/`. Latin and latin-ext subsets only. Inter,
Instrument Serif and JetBrains Mono are all SIL OFL 1.1.

**The component CSS is subset.** Each `css/components-*.css` carries only the
families the console mounts, and each says at the top what was left out.
`components-media.css` is absent entirely. The bundle still defines every
component, so **mounting one whose CSS was not copied renders it unstyled** — if
you reach for `Menu`, `Calendar`, `Toast` or `Timeline`, copy its block across
from the design project first.

That trade is worth naming: vendoring the whole layer would be simpler to
maintain, and the reason not to was size rather than principle.

## Refreshing

The files come from
`_ds/meridian-design-system-6e90155f-cc0d-4fc5-a0d3-450d0b283894/` in that
project. Paths here are flattened relative to it, and every vendored file is
excluded from git's text normalization by `.gitattributes` so the bytes match
what was published.
