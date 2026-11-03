"""The terminal palette: Meridian's design tokens, for `tracker loop` and `tracker tui`.

Meridian is the design system the project's web work draws from; its tokens live in
`meridian-tokens.css` (copied by `meridian add <component>`), light under `:root` and
dark under `.dark`. A terminal cannot read CSS variables, so the values are copied
here, once, and everything in the terminal refers to them by token name rather than
by colour. The **dark** set: a terminal's background is the reader's, and both
machines' terminals are dark — the light set's espresso text would be unreadable on
them. Taken from Meridian v0.1.0; to refresh, fetch any component and copy the
`.dark` block's values over the ones below.
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

# The names the drawing code uses: a role, never a colour.
FOREGROUND: Final = TOKENS["foreground"]
MUTED: Final = TOKENS["muted-foreground"]
#: The unused part of a bar, and rules: Meridian's border, lifted a step so a thin
#: line of it still shows on the terminal's own background.
TRACK: Final = TOKENS["input"]
PRIMARY: Final = TOKENS["primary"]
ACCENT: Final = TOKENS["accent-foreground"]
SUCCESS: Final = TOKENS["success"]
WARNING: Final = TOKENS["warning"]
DANGER: Final = TOKENS["danger"]
#: The chart series, in Meridian's order: distinct stretches of one stacked bar.
CHART: Final = tuple(TOKENS[f"chart-{n}"] for n in range(1, 6))

#: Rich styles built from them.
DIM: Final = MUTED
BOLD_PRIMARY: Final = f"bold {PRIMARY}"


def badge(role: str) -> str:
    """A filled label (RUNNING, FINISHED) in a role's colour, dark text on it."""
    ink = TOKENS["danger-foreground"] if role == DANGER else TOKENS["primary-foreground"]
    return f"bold {ink} on {role}"


def textual_theme():
    """The same tokens as a Textual theme, so `$primary`, `$panel`, `$error` and the
    widgets' own colours in `tracker tui` are Meridian's too."""
    from textual.theme import Theme

    return Theme(
        name="meridian",
        primary=TOKENS["primary"],
        secondary=TOKENS["chart-5"],
        accent=TOKENS["accent-foreground"],
        foreground=TOKENS["foreground"],
        background=TOKENS["background"],
        surface=TOKENS["surface"],
        panel=TOKENS["muted"],
        boost=TOKENS["border"],
        success=TOKENS["success"],
        warning=TOKENS["warning"],
        error=TOKENS["danger"],
        dark=True,
    )
