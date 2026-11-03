"""The terminal palette: Meridian's design tokens, for `tracker loop` and `tracker tui`.

Meridian is the design system the project's web work draws from; its tokens live in
`meridian-tokens.css` (copied by `meridian add <component>`), light under `:root` and
dark under `.dark`. A terminal cannot read CSS variables, so the values are copied
here, once, and everything in the terminal refers to them by token name rather than
by colour. The **dark** set: a terminal's background is the reader's, and both
machines' terminals are dark — the light set's espresso text would be unreadable on
them. Taken from Meridian v0.1.0; to refresh, fetch any component and copy the
`.dark` block's values over the ones below, then re-pick the terminal roles further
down from them.
"""

from __future__ import annotations

from typing import Final

#: Meridian's `.dark` tokens, by their CSS names without the leading `--`.
TOKENS: Final[dict[str, str]] = {
    "background": "#1b1410",
    "surface": "#251b14",
    "foreground": "#f3eadb",
    "muted": "#2d231b",
    "muted-foreground": "#b0a089",
    "border": "#372a1f",
    "input": "#423325",
    "primary": "#dca75f",
    "primary-foreground": "#2a1d0e",
    "accent-foreground": "#eac08a",
    "secondary": "#2d231b",
    "success": "#a3c48c",
    "warning": "#dfb75c",
    "danger": "#e0806f",
    "danger-foreground": "#2a1410",
    "chart-1": "#e0ac62",
    "chart-2": "#a3c48c",
    "chart-3": "#e08b66",
    "chart-4": "#d4888f",
    "chart-5": "#b9a6cf",
}

# --- what the terminal draws -----------------------------------------------------------
#
# The tokens above are not drawn as they are. A terminal reached over ssh announces
# 256 colours (the host's sessions carry no COLORTERM), so Rich and Textual squeeze
# every colour to the nearest of those — and Meridian's warm neutrals land badly:
# its cream text becomes xterm colour 224, a pink; its warm greys and the bar track
# become olive (144, 58); its amber and its gold become the same colour (179). Seen
# on 2026-11-03: the whole TUI read pink.
#
# So each role below is an exact xterm-256 colour, which no terminal changes: the
# accents are the nearest that keep Meridian's hue (amber, sage, terracotta,
# lavender), the neutrals are true greys, and the rose series is a steel blue, since
# every rose in the 256 lands on pink.

FOREGROUND: Final = "#e4e4e4"  # 254
MUTED: Final = "#a8a8a8"  # 248
#: The unused part of a bar, and rules.
TRACK: Final = "#444444"  # 238
PRIMARY: Final = "#d7af5f"  # 179, Meridian's primary amber
ACCENT: Final = "#ffd787"  # 222, its accent sand
SUCCESS: Final = "#87af87"  # 108, its success sage
WARNING: Final = "#ffaf5f"  # 215, apart from the amber, which its own gold is not
DANGER: Final = "#d75f5f"  # 167, its danger, made a red rather than a coral
#: The chart series, in Meridian's order, rose replaced: amber, sage, terracotta,
#: steel, lavender — distinct stretches of one stacked bar.
CHART: Final = ("#d7af5f", "#87af87", "#d7875f", "#87afd7", "#afafd7")
#: The TUI's grounds: Meridian's espresso steps, as greys.
BACKGROUND: Final = "#1c1c1c"  # 234
SURFACE: Final = "#262626"  # 235
PANEL: Final = "#303030"  # 236
BOOST: Final = "#3a3a3a"  # 237
INK: Final = BACKGROUND

#: Rich styles built from them.
DIM: Final = MUTED
BOLD_PRIMARY: Final = f"bold {PRIMARY}"


def badge(role: str) -> str:
    """A filled label (RUNNING, FINISHED) in a role's colour, dark text on it."""
    return f"bold {INK} on {role}"


def textual_theme():
    """The same roles as a Textual theme, so `$primary`, `$panel`, `$error` and the
    widgets' own colours in `tracker tui` match `tracker loop`."""
    from textual.theme import Theme

    return Theme(
        name="meridian",
        primary=PRIMARY,
        secondary=CHART[4],
        accent=ACCENT,
        foreground=FOREGROUND,
        background=BACKGROUND,
        surface=SURFACE,
        panel=PANEL,
        boost=BOOST,
        success=SUCCESS,
        warning=WARNING,
        error=DANGER,
        dark=True,
        # Full strength: Textual otherwise blends the text into the background, and
        # a blend is a colour the 256 do not have.
        text_alpha=1.0,
        # The highlighted row: amber with dark text, rather than Textual's tint of
        # the primary, which a 256-colour terminal draws olive.
        variables={
            "block-cursor-background": PRIMARY,
            "block-cursor-foreground": INK,
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": BOOST,
            "block-cursor-blurred-foreground": FOREGROUND,
        },
    )
