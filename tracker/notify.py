"""Email delivery: one message per person per day, and nothing sent to anybody twice.

**What goes in.** Every morning each person gets one email. An update is in it if
they have never been sent it, it is worth interrupting them for (`feed.notable`),
and either

* we **recorded it since their last email** and it happened within the last
  :data:`MAX_AGE_DAYS` (45) days, or
* it **happened within the last** :data:`CATCH_UP_DAYS` (14) days — the catch-up
  for a company they only just started watching, or an email that failed.

An undated update is judged by the day we recorded it. Nothing older than 45 days
is ever sent. With no earlier email to count from, "since your last email" means
the last day.

**A day with no news still gets an email** — the list of what to watch for on
each followed project (`watchfor`), so a quiet day reads as a quiet day and not as
a broken service. A day with news carries a short version of the same list. Both
link to the console's *Watch for* page and to the week on the *Updates* page, for
anybody who skipped a day.

**The mailer has a memory, and that is what makes it consistent.** It used to
choose by the clock — "whatever we learned in the last N days" — and every way
that could go wrong did: a run that failed or never happened lost its updates for
good, a reboot moved the window, the window's midnight was read in the wrong time
zone, a re-run sent everything twice, and a fact written weeks after its article
was fetched landed behind a window that had already run and was never sent at
all. Now every update a person is sent is recorded against them
(`models.NotifySent`), every message attempted is a row (`NotifyDelivery`), and
every run is a row (`NotifyRun`). A missed day is caught up the next morning
without anybody doing anything.

**One person's failure is theirs alone.** Each message is posted on its own, with
retries for the failures that clear by themselves (rate limits, the provider's own
errors, a dropped connection) and an idempotency key so a retry can never deliver
twice. A person whose message still fails is recorded as failed and is simply owed
those updates tomorrow; everybody after them is still sent. When anything fails —
or the crawler has gone quiet, which makes every email a quiet one for a reason
nobody would guess — the administrators are emailed about it.

**Rendering is separate from sending, and pure.** The `render*` functions take
data and return strings; they open no socket and read no settings. That is what
lets the whole template be tested offline, and it is why `notify preview` shows
exactly what would arrive without a key configured or a byte leaving the machine.

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

import datetime as dt
import hashlib
import html
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.feed import NOTIFY_MAX_AGE_DAYS, Digest, Signal, occurred
from tracker.models import Account, NotifyDelivery, NotifyRun, NotifySent, Source, utcnow
from tracker.watchfor import (
    EMAIL_BLOCKERS_PER_PROJECT,
    EMAIL_PROJECTS,
    ProjectWatch,
    WatchReport,
    track_label,
)

log = logging.getLogger(__name__)

#: Where Resend takes a message. One host, one endpoint.
RESEND_ENDPOINT = "https://api.resend.com/emails"

#: With no earlier email to count from, "new since your last email" means this.
FIRST_WINDOW: Final = dt.timedelta(days=1)

#: An unsent update that happened this recently goes in whenever it was recorded.
CATCH_UP_DAYS: Final[int] = 14

#: Nothing that happened longer ago than this is ever mailed. The same number
#: `feed.notable` gates on, named here because it is this module's rule.
MAX_AGE_DAYS: Final[int] = NOTIFY_MAX_AGE_DAYS

#: Tries per message, and the waits between them. A rate limit or a provider
#: hiccup clears in seconds; one still failing after ~40 s is owed tomorrow.
SEND_ATTEMPTS: Final[int] = 4
BACKOFF_SECONDS: Final[tuple[float, ...]] = (2.0, 8.0, 30.0)

#: The longest `Retry-After` honoured. Past it, the message is owed tomorrow.
MAX_RETRY_AFTER: Final[float] = 60.0

#: Pause between two people's messages. Resend allows 10 requests a second per
#: team, across every key; this stays well under it with room for retries.
SEND_INTERVAL: Final[float] = 0.25

#: A crawler that has fetched nothing for this long makes every email a quiet one.
STALE_CRAWL_HOURS: Final[int] = 48

#: How many blockers the quiet-day email names per project before it points at
#: the page for the rest.
QUIET_BLOCKERS_PER_PROJECT: Final[int] = 5

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

#: Obstacle severity -> (chip background, chip text).
_SEVERITY_COLOURS: dict[str, tuple[str, str]] = {
    "blocking": (TOKENS["danger_soft"], TOKENS["danger"]),
    "material": (TOKENS["warning_soft"], TOKENS["warning"]),
    "watch": (TOKENS["muted"], TOKENS["muted_foreground"]),
}

#: Email clients are unreliable past roughly this width, and a line of prose is
#: easier to read short anyway.
WIDTH = 600


class EmailError(RuntimeError):
    """The provider refused the message. The text is operator-facing."""

    #: How many posts were made before giving up. Set by `deliver`.
    attempts: int = 1


class TransientEmailError(EmailError):
    """A refusal that clears by itself: a rate limit, the provider's own fault, the
    network. Worth retrying; `retry_after` is the provider's own wait, if it gave
    one."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Transport(Protocol):
    """How a rendered message reaches somebody.

    A protocol rather than a direct call so the tests inject a recorder and never
    open a socket — the same shape as `llm.Extractor`, for the same reason.

    `idempotency_key` is passed by the daily run and never by the account notice.
    A transport that honours it must deliver a message at most once per key.
    """

    def send(
        self,
        *,
        to: str,
        subject: str,
        html_body: str,
        text_body: str,
        idempotency_key: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class Outcome:
    """What one run did, per recipient."""

    email: str
    signals: int
    message_id: str | None = None
    skipped: str | None = None
    #: `updates` or `quiet` when a message was built.
    kind: str | None = None
    blockers: int = 0
    attempts: int = 0
    error: str | None = None

    @property
    def sent(self) -> bool:
        return self.message_id is not None and self.error is None

    @property
    def failed(self) -> bool:
        return self.error is not None


def esc(text: Any) -> str:
    """Escape for HTML. Everything in a message is data — a project name is
    extracted from an article, and an article can contain anything."""
    return html.escape(str(text or ""), quote=True)


#: Statuses that clear by themselves. 409 is Resend's "the same idempotency key is
#: in flight", which is exactly the case a retry resolves.
_TRANSIENT_STATUS: Final = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after") or response.headers.get("ratelimit-reset")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class ResendTransport:
    """Resend's REST API.

    The key is read once at construction so a run cannot start without one, which
    is the same early-fail property `llm.DeepSeekExtractor` has and for the same
    reason: discovering a missing credential after building forty messages wastes
    the work and tells you nothing useful.

    It makes one attempt per call and says whether the failure is worth another;
    the retrying is `send_all`'s, so it can be tested without a network.
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

    def send(
        self,
        *,
        to: str,
        subject: str,
        html_body: str,
        text_body: str,
        idempotency_key: str | None = None,
    ) -> str:
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
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        if idempotency_key:
            # Resend delivers a key at most once for 24 hours, which is what makes
            # retrying a timed-out post safe: the first one may have gone through.
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = httpx.post(
                RESEND_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        except httpx.RequestError as exc:
            raise TransientEmailError(f"could not reach Resend: {exc}") from exc

        if response.status_code == 401:
            raise EmailError("Resend rejected the key (HTTP 401).\n\n" + KEY_HELP)
        if response.status_code == 403:
            raise EmailError(
                f"Resend refused to send from {self.sender!r} (HTTP 403). The most "
                "common cause is a domain that has not been verified in the Resend "
                f"dashboard.\n{response.text[:300]}"
            )
        if response.status_code in _TRANSIENT_STATUS:
            raise TransientEmailError(
                f"Resend returned HTTP {response.status_code}: {response.text[:300]}",
                retry_after=_retry_after(response),
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


# --- links ----------------------------------------------------------------------


def page_url(console_url: str | None, view: str) -> str | None:
    """A console page's address, or None when no console URL is configured."""
    if not console_url:
        return None
    return f"{console_url.rstrip('/')}/{view}"


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


def quiet_subject(watch: WatchReport) -> str:
    """The subject on a day with no news. Says so, and says what is still open."""
    blocked = len(watch.blocked)
    if not blocked:
        return "No new updates today — nothing open on your projects"
    total = len(watch.projects)
    verb = "has" if blocked == 1 else "have"
    return (
        f"No new updates today — {blocked} of your {total} project{'s' if total != 1 else ''} "
        f"still {verb} open blockers"
    )[:150]


def _chip(text: str, *, bg: str, fg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f"background:{bg};color:{fg};font-size:12px;font-weight:600;"
        f'font-family:{FONT_SANS};white-space:nowrap;">{esc(text)}</span>'
    )


def _button(label: str, href: str, *, primary: bool = True) -> str:
    bg = TOKENS["primary"] if primary else TOKENS["surface"]
    fg = TOKENS["primary_foreground"] if primary else TOKENS["primary"]
    border = TOKENS["primary"]
    return (
        f'<a href="{esc(href)}" style="display:inline-block;background:{bg};color:{fg};'
        f"border:1px solid {border};font-family:{FONT_SANS};font-size:14px;"
        f"font-weight:600;text-decoration:none;padding:10px 20px;border-radius:10px;"
        f'margin:4px;">{esc(label)}</a>'
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


def _heading(text: str) -> str:
    return (
        f'<tr><td style="padding:22px 0 10px 0;font-family:{FONT_MONO};font-size:11px;'
        f"letter-spacing:0.12em;text-transform:uppercase;"
        f'color:{TOKENS["muted_foreground"]};">{esc(text)}</td></tr>'
    )


def _open_for(days: int | None) -> str:
    if days is None:
        return ""
    if days < 1:
        return "open since today"
    if days < 60:
        return f"open {days} day{'s' if days != 1 else ''}"
    return f"open {days // 30} months"


def _project_block(project: ProjectWatch, *, blockers: int) -> str:
    """One followed project in the watch-for section: its blockers, then its signposts."""
    shown = project.blockers[:blockers]
    more = len(project.blockers) - len(shown)
    lines = []
    for b in shown:
        bg, fg = _SEVERITY_COLOURS.get(b.severity, _SEVERITY_COLOURS["watch"])
        meta = " · ".join(part for part in (track_label(b.track), _open_for(b.days_open)) if part)
        lines.append(
            f'<tr><td style="padding:6px 0 0 0;font-family:{FONT_SANS};font-size:14px;'
            f'line-height:1.5;color:{TOKENS["foreground"]};">'
            f"{_chip(f'{b.severity} · {b.label}', bg=bg, fg=fg)} {esc(b.summary)}"
            f'<div style="font-family:{FONT_MONO};font-size:11px;'
            f'color:{TOKENS["muted_foreground"]};padding-top:2px;">{esc(meta)}</div>'
            f"</td></tr>"
        )
    if more:
        lines.append(
            f'<tr><td style="padding:6px 0 0 0;font-family:{FONT_SANS};font-size:13px;'
            f'color:{TOKENS["muted_foreground"]};">+{more} more open on this project</td></tr>'
        )
    for sign in project.signposts[:2]:
        prefix = "Would clear it" if sign.blocked else "Next step"
        lines.append(
            f'<tr><td style="padding:8px 0 0 0;font-family:{FONT_SANS};font-size:13px;'
            f'line-height:1.5;color:{TOKENS["accent_foreground"]};">'
            f"<strong>{esc(prefix)}:</strong> {esc(track_label(sign.track))} — "
            f"{esc(sign.milestone.replace('_', ' '))}. Look for {esc(sign.looks_like)}."
            f"</td></tr>"
        )
    where = f" · {esc(project.location)}" if project.location else ""
    return f"""
    <tr><td style="padding:0 0 12px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="background:{TOKENS["surface"]};border:1px solid {TOKENS["border"]};
                    border-radius:14px;">
        <tr><td style="padding:14px 18px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="font-family:{FONT_SANS};font-size:13px;font-weight:600;
                       color:{TOKENS["muted_foreground"]};">{esc(project.company)}{where}</td></tr>
            <tr><td style="font-family:{FONT_DISPLAY};font-size:18px;line-height:1.3;
                       color:{TOKENS["foreground"]};">{esc(project.project)}</td></tr>
            {"".join(lines)}
          </table>
        </td></tr>
      </table>
    </td></tr>"""


def _watch_section(
    watch: WatchReport | None, *, full: bool, console_url: str | None
) -> tuple[str, list[str]]:
    """The "what to watch for" part, as HTML rows and as text lines.

    `full` is the quiet day's version: every blocked project, up to
    :data:`QUIET_BLOCKERS_PER_PROJECT` blockers each. Otherwise the short version
    that rides under the news: the :data:`watchfor.EMAIL_PROJECTS` most obstructed
    projects, two blockers each. Both end with a link to the page that has it all.
    """
    if watch is None or not watch.projects:
        return "", []
    blocked = watch.blocked
    limit = len(blocked) if full else EMAIL_PROJECTS
    per = QUIET_BLOCKERS_PER_PROJECT if full else EMAIL_BLOCKERS_PER_PROJECT
    shown = blocked[:limit]
    rows = [_heading("What to watch for on your projects")]
    text = ["", "WHAT TO WATCH FOR ON YOUR PROJECTS", ""]
    if not blocked:
        rows.append(
            f'<tr><td style="padding:0 0 10px 0;font-family:{FONT_SANS};font-size:14px;'
            f'color:{TOKENS["foreground"]};">None of your {len(watch.projects)} projects has '
            f"an open blocker right now.</td></tr>"
        )
        text.append(f"None of your {len(watch.projects)} projects has an open blocker right now.")
    for project in shown:
        rows.append(_project_block(project, blockers=per))
        where = f" ({project.location})" if project.location else ""
        text.append(f"* {project.company} — {project.project}{where}")
        for b in project.blockers[:per]:
            meta = ", ".join(p for p in (track_label(b.track), _open_for(b.days_open)) if p)
            text.append(f"  - [{b.severity}] {b.label}: {b.summary} ({meta})")
        if len(project.blockers) > per:
            text.append(f"  - +{len(project.blockers) - per} more open on this project")
        for sign in project.signposts[:2]:
            prefix = "Would clear it" if sign.blocked else "Next step"
            text.append(
                f"  {prefix}: {track_label(sign.track)} — "
                f"{sign.milestone.replace('_', ' ')}. Look for {sign.looks_like}."
            )
        text.append("")
    hidden = len(blocked) - len(shown)
    clear = [p for p in watch.projects if not p.blocked]
    notes = []
    if hidden:
        notes.append(f"{hidden} more project{'s' if hidden != 1 else ''} with open blockers.")
    if clear:
        names = ", ".join(f"{p.company} — {p.project}" for p in clear[:6])
        extra = f" and {len(clear) - 6} more" if len(clear) > 6 else ""
        notes.append(f"No open blockers: {names}{extra}.")
    for note in notes:
        rows.append(
            f'<tr><td style="padding:0 0 8px 0;font-family:{FONT_SANS};font-size:13px;'
            f'color:{TOKENS["muted_foreground"]};">{esc(note)}</td></tr>'
        )
        text.append(note)
    link = page_url(console_url, "watch-for")
    if link:
        rows.append(
            f'<tr><td align="left" style="padding:6px 0 0 0;">'
            f"{_button('See the full list of what to watch for', link, primary=False)}</td></tr>"
        )
        text.append(f"The full list: {link}")
    return "".join(rows), text


def _missed_note(console_url: str | None) -> tuple[str, list[str]]:
    """The line for anybody who skipped a day: the whole week is on the page."""
    link = page_url(console_url, "updates")
    words = "Missed an email? Every update from the past 7 days is on your Updates page"
    if not link:
        return "", []
    return (
        f'<tr><td style="padding:4px 0 0 0;font-family:{FONT_SANS};font-size:13px;'
        f'color:{TOKENS["muted_foreground"]};">{esc(words)} — '
        f'<a href="{esc(link)}" style="color:{TOKENS["primary"]};font-weight:600;'
        f'text-decoration:none;">open it →</a></td></tr>'
    ), ["", f"{words}: {link}"]


def _since_words(last_email: dt.datetime | None) -> str:
    if last_email is None:
        return "in the last day"
    return f"since your last email on {local_time(last_email).strftime('%b %d')}"


def _page(*, preheader: str, intro: str, body: str, footer: str) -> str:
    """The frame every message shares: masthead, intro line, body rows, footer.

    Tables and inline styles throughout, because that is what survives Outlook's
    Word renderer and Gmail's stylesheet stripping. No external font, no image, no
    script — the message has to be readable with everything blocked, which is how
    most clients open it the first time.
    """
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>dc-tracker</title>
</head>
<body style="margin:0;padding:0;background:{TOKENS["background"]};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(preheader)}</div>
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
          {intro}
        </div>
      </td></tr>

      {body}

      <tr><td style="padding:26px 0 0 0;border-top:1px solid {TOKENS["border"]};
                 font-family:{FONT_SANS};font-size:12px;line-height:1.6;
                 color:{TOKENS["muted_foreground"]};">
        {footer}
      </td></tr>

    </table>
  </td></tr>
</table>
</body></html>"""


_FOOTER = (
    "You are receiving this because these companies are on your watchlist. Every "
    "figure above is traceable to the article that stated it — the dates are shown "
    "as <em>when it happened</em> and <em>when we learned it</em>, which are rarely "
    "the same."
)


def render(
    digest: Digest,
    signals: tuple[Signal, ...],
    *,
    name: str | None = None,
    console_url: str | None = None,
    watch: WatchReport | None = None,
    last_email: dt.datetime | None = None,
) -> str:
    """The day's message when there is news, as one HTML string."""
    greeting = f"Morning, {esc(name)}." if name else "Here is what moved."
    count = f"{len(signals)} new update{'s' if len(signals) != 1 else ''}"
    rows = [_heading(f"{count} {_since_words(last_email)}")]
    rows += [_signal_row(s) for s in signals]
    missed, _ = _missed_note(console_url)
    rows.append(missed)
    watch_rows, _ = _watch_section(watch, full=False, console_url=console_url)
    rows.append(watch_rows)
    link = page_url(console_url, "updates")
    if link:
        rows.append(
            f'<tr><td align="center" style="padding:24px 0 8px 0;">'
            f"{_button('Open this week in the console', link)}</td></tr>"
        )
    return _page(
        preheader=f"{count} {_since_words(last_email)}",
        intro=f"{greeting} {esc(count)} {esc(_since_words(last_email))}.",
        body="".join(rows),
        footer=_FOOTER,
    )


def render_text(
    digest: Digest,
    signals: tuple[Signal, ...],
    *,
    console_url: str | None = None,
    watch: WatchReport | None = None,
    last_email: dt.datetime | None = None,
) -> str:
    """The plain-text part. Not a courtesy — a message without one is filtered
    more often, and it is what a screen reader actually reads."""
    lines = [f"{len(signals)} new update(s) {_since_words(last_email)}", ""]
    for signal in signals:
        when = signal.happened.isoformat() if signal.happened else "undated"
        learned = f", learned {signal.at.date().isoformat()}" if signal.at else ""
        lines.append(f"* {signal.company} — {signal.project}: {signal.headline}")
        lines.append(f"  {signal.detail}")
        lines.append(
            f"  ({when}{learned})" + (f" {signal.source_url}" if signal.source_url else "")
        )
        lines.append("")
    lines += _missed_note(console_url)[1]
    lines += _watch_section(watch, full=False, console_url=console_url)[1]
    return "\n".join(lines)


def render_quiet(
    watch: WatchReport,
    *,
    name: str | None = None,
    console_url: str | None = None,
    last_email: dt.datetime | None = None,
) -> str:
    """The day's message when nothing new crossed the bar: what to watch for."""
    greeting = f"Morning, {esc(name)}." if name else "Good morning."
    intro = (
        f"{greeting} Nothing new on your watchlist {esc(_since_words(last_email))}. "
        "Here is what is still open, and what would move it."
    )
    watch_rows, _ = _watch_section(watch, full=True, console_url=console_url)
    missed, _ = _missed_note(console_url)
    return _page(
        preheader="Nothing new today — what to watch for on your projects",
        intro=intro,
        body=watch_rows + missed,
        footer=(
            "You are receiving this because these companies are on your watchlist. "
            "An email arrives every morning; on a day with news it leads with the news."
        ),
    )


def render_quiet_text(
    watch: WatchReport, *, console_url: str | None = None, last_email: dt.datetime | None = None
) -> str:
    lines = [f"Nothing new on your watchlist {_since_words(last_email)}."]
    lines += _watch_section(watch, full=True, console_url=console_url)[1]
    lines += _missed_note(console_url)[1]
    return "\n".join(lines)


# --- the ledger -----------------------------------------------------------------


def local_time(ts: dt.datetime) -> dt.datetime:
    """A stored naive-UTC time in this machine's zone, which is the schedule's."""
    return ts.replace(tzinfo=dt.UTC).astimezone()


def last_email(session: Session, account_id: int) -> NotifyDelivery | None:
    """The last daily message this person was actually sent."""
    return session.scalars(
        select(NotifyDelivery)
        .where(
            NotifyDelivery.account_id == account_id,
            NotifyDelivery.status == "sent",
            NotifyDelivery.kind.in_(("updates", "quiet")),
        )
        .order_by(NotifyDelivery.prepared_at.desc(), NotifyDelivery.id.desc())
        .limit(1)
    ).first()


def sent_keys(session: Session, account_id: int) -> set[str]:
    """Every update this person has been sent, by `feed.Signal.key`."""
    return set(
        session.scalars(
            select(NotifySent.signal_key).where(NotifySent.account_id == account_id)
        ).all()
    )


def is_sent(signal: Signal, sent: set[str]) -> bool:
    """A folded card counts as sent if any fact behind it was."""
    return any(key in sent for key in signal.all_keys)


def choose(
    signals: Iterable[Signal],
    *,
    since: dt.datetime,
    sent: set[str],
    today: dt.date | None = None,
) -> tuple[Signal, ...]:
    """The updates owed to one person today, in the order given. See the module rules.

    The catch-up rule admits a milestone only if we recorded it on or after the day
    it is dated. One recorded *before* its date was a schedule when we read it; the
    date passing does not make it news, and without this every "expected online
    September 20" read in June would arrive on September 21 as an energisation.
    """
    today = today or dt.date.today()
    out = []
    for signal in signals:
        if not signal.notify or is_sent(signal, sent):
            continue
        when = occurred(signal)
        if when is None or (today - when).days > MAX_AGE_DAYS:
            continue
        new = signal.at is not None and signal.at >= since
        reported_after = signal.at is None or signal.at.date() >= when
        recent = (today - when).days <= CATCH_UP_DAYS and reported_after
        if new or recent:
            out.append(signal)
    return tuple(out)


@dataclass(frozen=True)
class Message:
    """One person's email for today, built and not yet sent."""

    account_id: int
    email: str
    kind: str
    subject: str
    html_body: str
    text_body: str
    prepared_at: dt.datetime
    signals: tuple[Signal, ...] = ()
    blockers: int = 0
    #: Sent with `--force`, on purpose, to somebody already emailed today.
    forced: bool = False

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(k for s in self.signals for k in s.all_keys))

    def idempotency_key(self) -> str:
        """The same person, day and contents give the same key, so a retry — or a
        re-run after a crash that sent but did not record — cannot deliver it twice.

        A forced resend carries its own clock in the key. Otherwise the provider
        would recognise an identical quiet-day message as the morning's and
        silently not deliver the resend somebody asked for.
        """
        day = local_time(self.prepared_at).date().isoformat()
        digest = hashlib.sha256(f"{self.subject}\n{self.text_body}".encode()).hexdigest()
        forced = f"-f{self.prepared_at:%H%M%S}" if self.forced else ""
        return f"dct-{self.account_id}-{day}-{digest[:40]}{forced}"


def compose(
    session: Session,
    account: Account,
    *,
    now: dt.datetime | None = None,
    console_url: str | None = None,
    force: bool = False,
) -> Message | str:
    """Today's message for one person, or the reason there is none. Writes nothing."""
    from tracker import watchfor, watchlist
    from tracker.feed import digest

    now = now or utcnow()
    if account.disabled_at is not None:
        return "account disabled"
    if account.watch_all:
        # Wanting the whole database on a page is reasonable; having all of it
        # mailed every morning is a firehose, and mail arrives uninvited.
        return "watches the whole database — not mailed"
    entities = watchlist.watched(session, account_id=account.id)
    if not entities:
        return "no watchlist"
    if not any(e.matches for e in entities):
        return "watchlist matches no project"

    previous = last_email(session, account.id)
    if (
        previous is not None
        and not force
        and local_time(previous.prepared_at).date() == local_time(now).date()
    ):
        return "already emailed today"
    last = previous.prepared_at if previous is not None else None
    since = last or (now - FIRST_WINDOW)
    # Wide enough for both rules: the catch-up reaches back CATCH_UP_DAYS.
    earliest = min(since, now - dt.timedelta(days=CATCH_UP_DAYS + 1))
    brief = digest(session, since=earliest, account_id=account.id, entities=entities)
    owed = choose(brief.signals, since=since, sent=sent_keys(session, account.id))
    watch = watchfor.report(session, account_id=account.id, entities=entities, everything=False)

    if owed:
        return Message(
            account_id=account.id,
            email=account.email,
            kind="updates",
            subject=subject_for(brief, owed),
            html_body=render(
                brief,
                owed,
                name=account.name,
                console_url=console_url,
                watch=watch,
                last_email=last,
            ),
            text_body=render_text(
                brief, owed, console_url=console_url, watch=watch, last_email=last
            ),
            prepared_at=now,
            signals=owed,
            blockers=watch.blockers,
            forced=force,
        )
    return Message(
        account_id=account.id,
        email=account.email,
        kind="quiet",
        subject=quiet_subject(watch),
        html_body=render_quiet(watch, name=account.name, console_url=console_url, last_email=last),
        text_body=render_quiet_text(watch, console_url=console_url, last_email=last),
        prepared_at=now,
        blockers=watch.blockers,
        forced=force,
    )


def deliver(
    transport: Transport,
    message: Message,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int]:
    """Post one message, retrying what clears by itself. Returns (id, attempts).

    Raises the last `EmailError` when the message cannot be delivered.
    """
    key = message.idempotency_key()
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            message_id = transport.send(
                to=message.email,
                subject=message.subject,
                html_body=message.html_body,
                text_body=message.text_body,
                idempotency_key=key,
            )
            return message_id or "", attempt
        except TransientEmailError as exc:
            if attempt == SEND_ATTEMPTS:
                exc.attempts = attempt
                raise
            wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            if exc.retry_after is not None:
                wait = max(wait, min(exc.retry_after, MAX_RETRY_AFTER))
            log.warning("send to %s failed (%s); retrying in %.0fs", message.email, exc, wait)
            sleep(wait)
        except EmailError as exc:
            exc.attempts = attempt
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def _commit(session: Session, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Commit, waiting out a busy writer rather than losing the record.

    The overnight loop can hold the database for long stretches, and losing the
    record of a message that was already delivered means sending it again
    tomorrow. SQLite's own busy timeout covers five seconds; this covers minutes.
    """
    for attempt in range(12):
        try:
            session.commit()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 11:
                raise
            sleep(10.0)


def _record(
    session: Session,
    *,
    run: NotifyRun | None,
    message: Message,
    status: str,
    message_id: str | None = None,
    attempts: int = 0,
    error: str | None = None,
) -> None:
    delivery = NotifyDelivery(
        run_id=run.id if run is not None else None,
        account_id=message.account_id,
        email=message.email,
        kind=message.kind,
        status=status,
        prepared_at=message.prepared_at,
        sent_at=utcnow() if status == "sent" else None,
        updates=len(message.signals),
        blockers=message.blockers,
        subject=message.subject,
        message_id=message_id,
        attempts=attempts,
        error=(error or "")[:2000] or None,
    )
    session.add(delivery)
    session.flush()
    if status == "sent":
        for key in message.keys:
            session.add(
                NotifySent(account_id=message.account_id, signal_key=key, delivery_id=delivery.id)
            )


# --- one message per person ----------------------------------------------------


def send_all(
    session: Session,
    *,
    transport: Transport,
    console_url: str | None = None,
    only_email: str | None = None,
    now: dt.datetime | None = None,
    record: bool = True,
    force: bool = False,
    alert: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """Send each person today's one message, and remember what was sent.

    **The loop is over people, not signals.** Fourteen changes on somebody's
    watchlist is one email with fourteen cards, never fourteen emails, and a
    day with none is one email saying what to watch for.

    **Each person is committed on their own**, straight after their message goes,
    so a crash halfway down the list keeps the record of everybody already sent,
    and a failure for one person is recorded and passed over rather than stopping
    the rest. `record=False` sends without remembering, which only a dry run
    (whose transport sends nothing) should want.

    **Every update is listed. The message is never truncated.** A reader works
    the message, and one that ends "…and 3 more, not listed" sends them somewhere
    else to find the rest. The one boundary worth knowing: **Gmail clips a message
    past roughly 102 KB** behind a "View entire message" link — at about 46
    updates on this template. A daily run is nowhere near it; a catch-up after a
    long outage could be.
    """
    from tracker import accounts

    now = now or utcnow()
    run = None
    if record:
        run = NotifyRun(started_at=now)
        session.add(run)
        _commit(session, sleep=sleep)

    out: list[Outcome] = []
    posted = False
    for account in accounts.listing(session):
        if only_email and account.email_key != accounts.normalize_email(only_email):
            continue
        plan = compose(session, account, now=now, console_url=console_url, force=force)
        if isinstance(plan, str):
            out.append(Outcome(account.email, 0, skipped=plan))
            continue
        if posted:
            sleep(SEND_INTERVAL)
        posted = True
        try:
            message_id, attempts = deliver(transport, plan, sleep=sleep)
        except EmailError as exc:
            attempts = exc.attempts
            log.error("could not send to %s: %s", plan.email, exc)
            if record:
                _record(
                    session,
                    run=run,
                    message=plan,
                    status="failed",
                    attempts=attempts,
                    error=str(exc),
                )
                _commit(session, sleep=sleep)
            out.append(
                Outcome(
                    plan.email,
                    len(plan.signals),
                    kind=plan.kind,
                    blockers=plan.blockers,
                    attempts=attempts,
                    error=str(exc),
                )
            )
            continue
        if record:
            _record(
                session,
                run=run,
                message=plan,
                status="sent",
                message_id=message_id,
                attempts=attempts,
            )
            _commit(session, sleep=sleep)
        out.append(
            Outcome(
                plan.email,
                len(plan.signals),
                message_id=message_id,
                kind=plan.kind,
                blockers=plan.blockers,
                attempts=attempts,
            )
        )

    if run is not None:
        run.finished_at = utcnow()
        run.sent = sum(1 for o in out if o.sent)
        run.failed = sum(1 for o in out if o.failed)
        run.skipped = sum(1 for o in out if o.skipped)
        _commit(session, sleep=sleep)
        if alert:
            alert_admins(session, transport, out, run=run, sleep=sleep)
    return out


# --- telling somebody when it goes wrong ---------------------------------------


def problems(session: Session, outcomes: list[Outcome]) -> list[str]:
    """What an administrator needs to hear about this run, in plain words."""
    lines = [
        f"Could not deliver to {o.email} after {o.attempts} attempt(s): {o.error}"
        for o in outcomes
        if o.failed
    ]
    last = session.scalar(select(func.max(Source.fetched_at)))
    if last is not None:
        age = utcnow() - last
        if age > dt.timedelta(hours=STALE_CRAWL_HOURS):
            lines.append(
                f"Nothing has been fetched for {age.days} day(s) {age.seconds // 3600} "
                "hour(s). Every email is a quiet one until the crawler runs again."
            )
    return lines


def alert_admins(
    session: Session,
    transport: Transport,
    outcomes: list[Outcome],
    *,
    run: NotifyRun | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """Email every enabled administrator when this run has something to report."""
    from tracker import accounts

    found = problems(session, outcomes)
    if not found:
        return []
    now = utcnow()
    sent = sum(1 for o in outcomes if o.sent)
    subject = f"dc-tracker: {len(found)} problem(s) with this morning's emails"
    text = "\n".join(
        [
            f"This morning's run sent {sent} email(s) and found:",
            "",
            *[f"* {f}" for f in found],
            "",
            "Anyone not delivered to is owed those updates and gets them on the next run.",
        ]
    )
    body = "".join(
        f'<tr><td style="padding:0 0 10px 0;font-family:{FONT_SANS};font-size:14px;'
        f'line-height:1.5;color:{TOKENS["foreground"]};">{esc(f)}</td></tr>'
        for f in found
    )
    html_body = _page(
        preheader=subject,
        intro=f"This morning's run sent {sent} email(s) and found {len(found)} problem(s).",
        body=body,
        footer="Anyone not delivered to is owed those updates and gets them on the next run.",
    )
    out = []
    for admin in accounts.listing(session):
        if not admin.is_admin or admin.disabled_at is not None:
            continue
        message = Message(
            account_id=admin.id,
            email=admin.email,
            kind="alert",
            subject=subject,
            html_body=html_body,
            text_body=text,
            prepared_at=now,
        )
        try:
            message_id, attempts = deliver(transport, message, sleep=sleep)
        except EmailError as exc:
            log.error("could not alert %s: %s", admin.email, exc)
            _record(
                session,
                run=run,
                message=message,
                status="failed",
                attempts=exc.attempts,
                error=str(exc),
            )
            out.append(Outcome(admin.email, 0, kind="alert", error=str(exc)))
        else:
            _record(
                session,
                run=run,
                message=message,
                status="sent",
                message_id=message_id,
                attempts=attempts,
            )
            out.append(Outcome(admin.email, 0, message_id=message_id, kind="alert"))
        _commit(session, sleep=sleep)
    return out


__all__ = [
    "BACKOFF_SECONDS",
    "CATCH_UP_DAYS",
    "FIRST_WINDOW",
    "FONT_DISPLAY",
    "FONT_MONO",
    "FONT_SANS",
    "MAX_AGE_DAYS",
    "RESEND_ENDPOINT",
    "SEND_ATTEMPTS",
    "TOKENS",
    "WIDTH",
    "EmailError",
    "Message",
    "Outcome",
    "ResendTransport",
    "TransientEmailError",
    "Transport",
    "alert_admins",
    "choose",
    "compose",
    "deliver",
    "esc",
    "is_sent",
    "last_email",
    "local_time",
    "page_url",
    "problems",
    "quiet_subject",
    "render",
    "render_quiet",
    "render_quiet_text",
    "render_text",
    "send_all",
    "sent_keys",
    "subject_for",
]
