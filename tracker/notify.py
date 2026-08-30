"""Email delivery: one message per person, carrying everything they are owed.

**One email, not one per signal.** A channel that sends fourteen messages about
fourteen changes is a channel people filter into a folder, and a filtered channel
protects nobody — the same argument `feed.notable` makes about interrupting too
often, one layer out. So the unit of delivery is a *person and a window*, never a
signal: `send_all` builds one brief per account and sends at most one message to
each, or none.

**Rendering is separate from sending, and pure.** `render` takes a digest and
returns a string; it opens no socket and reads no settings. That is what lets the
whole template be tested offline, and it is why `--preview` can show you exactly
what would arrive without a key configured or a byte leaving the machine.

**The design system had to be inlined, not imported.** Meridian is React 19 plus
Tailwind v4, and an email client runs neither — no build step, no class engine,
no external stylesheet. Gmail strips `<style>` blocks in some contexts and
Outlook renders through Word. So the token *values* are transcribed here as
constants and applied inline, which is the one place in this codebase where
hardcoding a hex is correct rather than forbidden. :data:`TOKENS` names its
source so the two can be diffed when the palette moves.

**Every colour carries its meaning, not its appearance.** `good`, `bad` and
`neutral` come from `feed.EVENT_SIGN`, which is a closed vocabulary rather than a
judgement, so the palette cannot disagree with the digest about which way a
signal cuts.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from tracker.config import Settings, get_settings
from tracker.feed import Digest, Signal

log = logging.getLogger(__name__)

#: Where Resend takes a message. One host, one endpoint.
RESEND_ENDPOINT = "https://api.resend.com/emails"

#: Meridian's light palette, transcribed from `styles/meridian-tokens.css` in the
#: design system. Light only: `prefers-color-scheme` is honoured by a minority of
#: clients and ignored by the ones most people read mail in, so the message is
#: designed to be correct in light and merely *legible* in dark rather than
#: depending on a swap that may never happen.
TOKENS: dict[str, str] = {
    "background": "#faf6ef",
    "surface": "#fffdf8",
    "foreground": "#2e2620",
    "muted": "#f3ecdf",
    "muted_foreground": "#6b5c4f",
    "border": "#eae0d0",
    "primary": "#a05e1c",
    "primary_foreground": "#fffaf2",
    "accent_soft": "#f5e6cc",
    "accent_foreground": "#7e4e14",
    "success": "#3f6033",
    "success_soft": "#e3edd8",
    "warning": "#8a680a",
    "warning_soft": "#f5ebc4",
    "danger": "#b8433a",
    "danger_soft": "#f9deda",
}

#: Meridian maps these to `--font-sans` / `--font-display` / `--font-mono`. Web
#: fonts are not loaded: most clients refuse them, and a fallback that only
#: appears for some readers is worse than one stack everybody gets.
FONT_SANS = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, Helvetica, Arial, sans-serif"
)
FONT_DISPLAY = "'Instrument Serif', Georgia, 'Times New Roman', serif"
FONT_MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"

#: Signal sign -> (dot colour, chip background, chip text). Keyed on
#: `feed.EVENT_SIGN`'s vocabulary so the palette cannot drift from the meaning.
_SIGN_COLOURS: dict[str, tuple[str, str, str]] = {
    "good": (TOKENS["success"], TOKENS["success_soft"], TOKENS["success"]),
    "bad": (TOKENS["danger"], TOKENS["danger_soft"], TOKENS["danger"]),
    "neutral": (TOKENS["muted_foreground"], TOKENS["muted"], TOKENS["muted_foreground"]),
}

#: Email clients are unreliable past roughly this width, and a line of prose is
#: easier to read short anyway.
WIDTH = 600


class EmailError(RuntimeError):
    """The provider refused the message. The text is operator-facing."""


class Transport(Protocol):
    """How a rendered message reaches somebody.

    A protocol rather than a direct call so the tests inject a recorder and never
    open a socket — the same shape as `llm.Extractor`, for the same reason.
    """

    def send(self, *, to: str, subject: str, html_body: str, text_body: str) -> str: ...


@dataclass(frozen=True)
class Outcome:
    """What one run did, per recipient."""

    email: str
    signals: int
    message_id: str | None = None
    skipped: str | None = None

    @property
    def sent(self) -> bool:
        return self.message_id is not None


def esc(text: Any) -> str:
    """Escape for HTML. Everything in a message is data — a project name is
    extracted from an article, and an article can contain anything."""
    return html.escape(str(text or ""), quote=True)


class ResendTransport:
    """Resend's REST API.

    The key is read once at construction so a run cannot start without one, which
    is the same early-fail property `llm.DeepSeekExtractor` has and for the same
    reason: discovering a missing credential after building forty messages wastes
    the work and tells you nothing useful.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        key = self.settings.resend_api_key
        if not (key and key.get_secret_value().strip()):
            raise EmailError(KEY_HELP)
        self._key = key.get_secret_value().strip()
        sender = (self.settings.notify_from or "").strip()
        if not sender:
            raise EmailError(SENDER_HELP)
        self.sender = sender

    def send(self, *, to: str, subject: str, html_body: str, text_body: str) -> str:
        payload = {
            "from": self.sender,
            "to": [to],
            "subject": subject,
            "html": html_body,
            # Always both. A text part is what a screen reader, a plain-text
            # client and every spam filter read, and a message without one scores
            # worse for delivery than the same message with it.
            "text": text_body,
        }
        try:
            response = httpx.post(
                RESEND_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        except httpx.RequestError as exc:
            raise EmailError(f"could not reach Resend: {exc}") from exc

        if response.status_code == 401:
            raise EmailError("Resend rejected the key (HTTP 401).\n\n" + KEY_HELP)
        if response.status_code == 403:
            raise EmailError(
                f"Resend refused to send from {self.sender!r} (HTTP 403). The most "
                "common cause is a domain that has not been verified in the Resend "
                f"dashboard.\n{response.text[:300]}"
            )
        if response.status_code >= 400:
            raise EmailError(f"Resend returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return str(response.json().get("id") or "")
        except ValueError:
            return ""


KEY_HELP = """TRACKER_RESEND_API_KEY is not set.

  Add it to the .env file beside pyproject.toml, which is gitignored:
    TRACKER_RESEND_API_KEY=re_...

  Keys are issued at resend.com/api-keys. The sending address also needs its
  domain verified there, or Resend answers 403.
"""

SENDER_HELP = """TRACKER_NOTIFY_FROM is not set.

  The address messages are sent from, e.g.
    TRACKER_NOTIFY_FROM=system@your-domain.example

  It has no default on purpose: a wrong sender is refused by Resend as an
  unverified domain, which is a confusing way to learn about a typo.
"""


# --- rendering ----------------------------------------------------------------


def subject_for(digest: Digest, signals: tuple[Signal, ...]) -> str:
    """One line that says how much and how bad, because it is read in a list.

    Naming the worst thing rather than counting is deliberate: "3 updates" is a
    number somebody defers, and "grid capacity — obstacle" is a sentence they open.
    """
    count = len(signals)
    bad = sum(1 for s in signals if s.sign == "bad")
    lead = signals[0] if signals else None
    head = f"{count} update{'s' if count != 1 else ''}"
    if lead is not None:
        head += f" — {lead.company}: {lead.headline}"
    if bad and count > 1:
        head += f" (+{bad - 1} more needing attention)" if bad > 1 else ""
    return head[:150]


def _chip(text: str, *, bg: str, fg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f"background:{bg};color:{fg};font-size:12px;font-weight:600;"
        f'font-family:{FONT_SANS};white-space:nowrap;">{esc(text)}</span>'
    )


def _signal_row(signal: Signal) -> str:
    """One signal as a card.

    Both dates ride on every row, which is the same rule the page follows: "new"
    means new to us, and a milestone from 2022 that we read yesterday has to read
    as what it is. The recency gate means a notification rarely carries an old one
    now, but the label is what makes that visible rather than assumed.
    """
    dot, chip_bg, chip_fg = _SIGN_COLOURS.get(signal.sign, _SIGN_COLOURS["neutral"])
    when = signal.happened.isoformat() if signal.happened else "undated"
    learned = f" · learned {signal.at.date().isoformat()}" if signal.at else ""
    source = ""
    if signal.source_url:
        label = esc(signal.publisher or "source")
        source = (
            f'<a href="{esc(signal.source_url)}" '
            f'style="color:{TOKENS["primary"]};text-decoration:none;font-weight:600;white-space:nowrap;">'
            f"{label} →</a>"
        )
    unblocks = (
        _chip("was blocked", bg=TOKENS["accent_soft"], fg=TOKENS["accent_foreground"])
        if signal.unblocks
        else ""
    )
    quote = ""
    if signal.quote:
        quote = (
            f'<tr><td style="padding:8px 0 0 0;">'
            f'<div style="border-left:3px solid {TOKENS["border"]};padding:2px 0 2px 12px;'
            f"color:{TOKENS['muted_foreground']};font-size:13px;line-height:1.5;"
            f'font-family:{FONT_SANS};font-style:italic;">“{esc(signal.quote[:240])}”</div>'
            f"</td></tr>"
        )

    return f"""
    <tr><td style="padding:0 0 12px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="background:{TOKENS["surface"]};border:1px solid {TOKENS["border"]};
                    border-radius:14px;border-left:4px solid {dot};">
        <tr><td style="padding:16px 18px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="font-family:{FONT_SANS};font-size:13px;font-weight:600;
                         color:{TOKENS["muted_foreground"]};padding-bottom:4px;">
                {esc(signal.company)}
              </td>
              <td align="right" style="padding-bottom:4px;">
                {_chip(signal.headline, bg=chip_bg, fg=chip_fg)} {unblocks}
              </td>
            </tr>
            <tr><td colspan="2" style="font-family:{FONT_DISPLAY};font-size:19px;
                       line-height:1.3;color:{TOKENS["foreground"]};padding-bottom:6px;">
              {esc(signal.project)}
            </td></tr>
            <tr><td colspan="2" style="font-family:{FONT_SANS};font-size:14px;
                       line-height:1.55;color:{TOKENS["foreground"]};">
              {esc(signal.detail)}
            </td></tr>
            {quote}
            <tr><td colspan="2" style="padding-top:10px;font-family:{FONT_MONO};
                       font-size:11px;color:{TOKENS["muted_foreground"]};">
              {esc(when)}{esc(learned)} &nbsp; {source}
            </td></tr>
          </table>
        </td></tr>
      </table>
    </td></tr>"""


def render(
    digest: Digest,
    signals: tuple[Signal, ...],
    *,
    name: str | None = None,
    console_url: str | None = None,
) -> str:
    """The whole message, as one HTML string.

    Tables and inline styles throughout, because that is what survives Outlook's
    Word renderer and Gmail's stylesheet stripping. No external font, no image, no
    script — the message has to be readable with everything blocked, which is how
    most clients open it the first time.
    """
    greeting = f"Morning, {esc(name)}." if name else "Here is what moved."
    window = digest.since.date().isoformat()
    rows = "".join(_signal_row(s) for s in signals)

    button = ""
    if console_url:
        button = f"""
    <tr><td align="center" style="padding:24px 0 8px 0;">
      <a href="{esc(console_url)}"
         style="display:inline-block;background:{TOKENS["primary"]};
                color:{TOKENS["primary_foreground"]};font-family:{FONT_SANS};
                font-size:14px;font-weight:600;text-decoration:none;
                padding:11px 22px;border-radius:10px;">Open the console</a>
    </td></tr>"""

    # A preheader is the grey line a client shows beside the subject. Left empty
    # it shows whatever the first markup happens to be, which is usually "Morning".
    preheader = esc(f"{len(signals)} update{'s' if len(signals) != 1 else ''} since {window}")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>dc-tracker</title>
</head>
<body style="margin:0;padding:0;background:{TOKENS["background"]};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{TOKENS["background"]};padding:28px 12px;">
  <tr><td align="center">
    <table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" border="0"
           style="width:100%;max-width:{WIDTH}px;">

      <tr><td style="padding:0 0 20px 0;">
        <div style="font-family:{FONT_DISPLAY};font-size:26px;color:{TOKENS["foreground"]};">
          dc-tracker
        </div>
        <div style="font-family:{FONT_SANS};font-size:14px;color:{TOKENS["muted_foreground"]};
                    padding-top:4px;">
          {greeting} {len(signals)} update{"s" if len(signals) != 1 else ""} since {esc(window)}.
        </div>
      </td></tr>

      {rows}
      {button}

      <tr><td style="padding:26px 0 0 0;border-top:1px solid {TOKENS["border"]};
                 font-family:{FONT_SANS};font-size:12px;line-height:1.6;
                 color:{TOKENS["muted_foreground"]};">
        You are receiving this because these companies are on your watchlist.
        Every figure above is traceable to the article that stated it — the
        dates are shown as <em>when it happened</em> and <em>when we learned
        it</em>, which are rarely the same.
      </td></tr>

    </table>
  </td></tr>
</table>
</body></html>"""


def render_text(digest: Digest, signals: tuple[Signal, ...]) -> str:
    """The plain-text part. Not a courtesy — a message without one is filtered
    more often, and it is what a screen reader actually reads."""
    lines = [f"{len(signals)} update(s) since {digest.since.date().isoformat()}", ""]
    for signal in signals:
        when = signal.happened.isoformat() if signal.happened else "undated"
        learned = f", learned {signal.at.date().isoformat()}" if signal.at else ""
        lines.append(f"* {signal.company} — {signal.project}: {signal.headline}")
        lines.append(f"  {signal.detail}")
        lines.append(
            f"  ({when}{learned})" + (f" {signal.source_url}" if signal.source_url else "")
        )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "FONT_DISPLAY",
    "FONT_MONO",
    "FONT_SANS",
    "RESEND_ENDPOINT",
    "TOKENS",
    "WIDTH",
    "EmailError",
    "Outcome",
    "ResendTransport",
    "Transport",
    "esc",
    "render",
    "render_text",
    "subject_for",
]


# --- one message per person ----------------------------------------------------


def send_all(
    session,
    *,
    transport: Transport,
    days: int = 1,
    console_url: str | None = None,
    max_items: int | None = None,
    only_email: str | None = None,
) -> list[Outcome]:
    """Build one brief per account and send each person at most one message.

    **The loop is over people, not signals.** That is the whole point: fourteen
    changes on somebody's watchlist is one email with fourteen cards, never
    fourteen emails. Anyone whose window is quiet gets nothing at all, which is
    what keeps the channel worth reading — the same argument `feed.notable` makes
    about the bar for interrupting somebody.

    **An account with no watchlist is skipped, not mailed everything.** `digest`
    falls back to the whole database when nobody has said what they care about,
    which is right for a page and wrong for mail. Same rule as
    `digest --notify`, enforced here rather than trusted to the caller.

    **Every update is listed. The message is never truncated.** `digest --notify`
    caps its *terminal* output at `feed.NOTIFY_MAX_ITEMS` and counts the rest,
    which is right for a stream somebody is watching scroll past. It is wrong
    here: a reader works the message, and one that ends "…and 3 more, not listed"
    sends them somewhere else to find the rest, which is the workflow this exists
    to save. So the email carries the lot, however long that makes it.

    The one boundary worth knowing: **Gmail clips a message past roughly 102 KB**
    behind a "View entire message" link. Measured on this template, a card is
    2.2 KB and twenty-five of them render to 54.8 KB, so the clip arrives at about
    **46 updates in one window**. A nightly run averages 4.3 and is nowhere near
    it; a `--days 30` catch-up after an outage would cross it. Nothing here can
    prevent that — it is stated so the failure is recognisable rather than
    mysterious, and it is an argument for running this nightly rather than
    occasionally.
    """
    from tracker import accounts
    from tracker.feed import digest

    out: list[Outcome] = []

    for account in accounts.listing(session):
        if only_email and account.email_key != accounts.normalize_email(only_email):
            continue

        brief = digest(session, days=days, account_id=account.id)
        if brief.watching_everything:
            out.append(Outcome(account.email, 0, skipped="no watchlist"))
            continue

        sending = brief.notifying
        if not sending:
            out.append(Outcome(account.email, 0, skipped="nothing worth sending"))
            continue

        if max_items is not None:
            # Only a caller that explicitly asks gets a shorter message. Nothing in
            # this codebase does; it exists so a future one-off cannot be tempted to
            # slice the tuple at the call site and lose the disclosure.
            sending = sending[:max_items]

        message_id = transport.send(
            to=account.email,
            subject=subject_for(brief, sending),
            html_body=render(brief, sending, name=account.name, console_url=console_url),
            text_body=render_text(brief, sending),
        )
        out.append(Outcome(account.email, len(sending), message_id=message_id or ""))

    return out


__all__ += ["send_all"]
