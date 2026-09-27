"""The emails the account pages cause: confirm an address, reset a password, and
tell an administrator — then the person — about a sign-up.

Each links the page it is about — `/confirm?t=`, `/reset?t=`, `/admin`, and
`/signin` with a `/forgot` footer for an approval — never the console's root, which
is the public front page for anybody not yet signed in. `/admin` needs a session,
so an administrator who is signed out goes by `/signin?next=/admin` and comes back.

Rendering is pure, like `account_notice` and `notify.render`: each function returns
a `Notice` and opens no socket. `send` is the one place that mails one, through the
same Resend transport the morning email uses.

**Every link is built from `TRACKER_NOTIFY_CONSOLE_URL`, never from the request.**
The Host header is whatever the client sent, and a reset link built from it can be
pointed at somebody else's server by anyone who asks for a reset of your address.
A console without that setting has no sign-up and no reset by email.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from tracker.account_notice import Notice
from tracker.notify import FONT_DISPLAY, FONT_SANS, TOKENS, WIDTH, esc

log = logging.getLogger(__name__)


def link(console_url: str, path: str, token: str | None = None) -> str:
    base = console_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}" + (f"?t={quote(token)}" if token else "")


def _frame(
    title: str, paragraphs: list[str], *, button: tuple[str, str] | None, footer: str
) -> Notice:
    """One message in the digest's styling: a heading, some sentences, one button."""
    body = "".join(
        f"""
      <tr><td style="padding:0 0 14px 0;font-family:{FONT_SANS};font-size:15px;
                 line-height:1.55;color:{TOKENS["foreground"]};">{esc(p)}</td></tr>"""
        for p in paragraphs
    )
    cta = ""
    if button:
        label, href = button
        cta = f"""
      <tr><td align="center" style="padding:10px 0 6px 0;">
        <a href="{esc(href)}"
           style="display:inline-block;background:{TOKENS["primary"]};
                  color:{TOKENS["primary_foreground"]};font-family:{FONT_SANS};
                  font-size:14px;font-weight:600;text-decoration:none;
                  padding:11px 22px;border-radius:10px;">{esc(label)}</a>
      </td></tr>
      <tr><td style="padding:10px 0 0 0;font-family:{FONT_SANS};font-size:12px;
                 line-height:1.6;color:{TOKENS["muted_foreground"]};word-break:break-all;">
        Or open this address: {esc(href)}
      </td></tr>"""
    html_body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>dc-tracker</title>
</head>
<body style="margin:0;padding:0;background:{TOKENS["background"]};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(title)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{TOKENS["background"]};padding:28px 12px;">
  <tr><td align="center">
    <table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" border="0"
           style="width:100%;max-width:{WIDTH}px;">
      <tr><td style="padding:0 0 18px 0;">
        <div style="font-family:{FONT_DISPLAY};font-size:26px;color:{TOKENS["foreground"]};">
          dc-tracker
        </div>
      </td></tr>
      {body}
      {cta}
      <tr><td style="padding:24px 0 0 0;border-top:1px solid {TOKENS["border"]};
                 font-family:{FONT_SANS};font-size:12px;line-height:1.6;
                 color:{TOKENS["muted_foreground"]};">
        {esc(footer)}
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""
    lines = [*paragraphs, ""]
    if button:
        lines += [f"{button[0]}: {button[1]}", ""]
    lines += ["--", footer]
    return Notice(subject=title, html_body=html_body, text_body="\n".join(lines) + "\n")


def confirm(console_url: str, token: str) -> Notice:
    return _frame(
        "Confirm your email for dc-tracker",
        [
            "Somebody — hopefully you — asked for a dc-tracker console account with this "
            "address. Confirm it's yours with the button below; the link works for 24 hours.",
            "After that, an administrator reviews the request. You'll get another email "
            "when your account is ready to use.",
        ],
        button=("Confirm my email", link(console_url, "confirm", token)),
        footer="If this wasn't you, ignore this email: nobody can sign in with this "
        "address until it is confirmed, and an unconfirmed request is removed after a week.",
    )


def already_registered(console_url: str, token: str, *, waiting: bool = False) -> Notice:
    """`waiting`: the account exists but an administrator has not approved it yet, so
    "just sign in" would be refused — say where it stands instead."""
    after = (
        "Your account is still waiting for an administrator's approval; we'll email "
        "you when it's ready."
        if waiting
        else "Otherwise, just sign in as usual."
    )
    return _frame(
        "You already have a dc-tracker account",
        [
            "Somebody — hopefully you — tried to sign up for the dc-tracker console with "
            "this address, but it already has an account.",
            "If you've forgotten the password, set a new one with the button below. The "
            "link works for one hour. " + after,
        ],
        button=("Set a new password", link(console_url, "reset", token)),
        footer="If this wasn't you, ignore this email. Your password has not changed.",
    )


def reset(console_url: str, token: str) -> Notice:
    return _frame(
        "Reset your dc-tracker password",
        [
            "Somebody — hopefully you — asked to reset the password for your dc-tracker "
            "console account. Choose a new one with the button below; the link works for "
            "one hour and only once.",
            "Setting a new password signs you out on every other device.",
        ],
        button=("Choose a new password", link(console_url, "reset", token)),
        footer="If this wasn't you, ignore this email. Your password stays as it is.",
    )


def pending_for_admin(console_url: str | None, email: str, name: str | None) -> Notice:
    who = f"{name} ({email})" if name else email
    return _frame(
        f"New sign-up waiting for approval: {email}",
        [
            f"{who} signed up for the dc-tracker console and confirmed the address.",
            "They can't sign in until you approve them. New accounts start without the "
            "AI panels; you can switch those on when you approve.",
        ],
        button=("Review on the Admin page", link(console_url, "admin")) if console_url else None,
        footer="Or at the host: tracker users approve " + email,
    )


def approved(console_url: str | None, name: str | None) -> Notice:
    greeting = f"Hello {name}," if name else "Hello,"
    return _frame(
        "Your dc-tracker account is ready",
        [
            greeting,
            "An administrator has approved your dc-tracker console account. Sign in with "
            "the email and password you signed up with.",
        ],
        button=("Sign in", link(console_url, "signin")) if console_url else None,
        footer=(
            "Forgotten the password? Choose a new one at " + link(console_url, "forgot")
            if console_url
            else 'Forgotten the password? Use "Forgot password?" on the sign-in page.'
        ),
    )


def send(to: str, notice: Notice, *, key: str | None = None) -> bool:
    """Mail one notice. Returns whether it went; a failure is logged, never raised.

    Never raised, because every caller has already answered the person at the form,
    and what that answer says cannot depend on whether a mail server was up.
    """
    from tracker.notify import EmailError, ResendTransport

    try:
        ResendTransport().send(
            to=to,
            subject=notice.subject,
            html_body=notice.html_body,
            text_body=notice.text_body,
            idempotency_key=key,
        )
    except EmailError as exc:
        log.warning("account mail to %s failed: %s", to, exc)
        return False
    return True


__all__ = [
    "already_registered",
    "approved",
    "confirm",
    "link",
    "pending_for_admin",
    "reset",
    "send",
]
