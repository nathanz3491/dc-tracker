"""The email that tells somebody how their console account is now set up.

Sent by `tracker users notify --to <address> --about <account>`, and **only**
that way: the operator types the address. After changing somebody's sign-in email,
the person to tell is usually at the *old* address, which the account no longer
knows — so nothing here is automatic, and a notice goes wherever it is pointed.

**It describes the account as it is now.** Every settings row is the row's current
value, read at send time, so a notice cannot describe an edit that was later
undone. What the operator adds on top is their own: a subject, a message, the old
address the change moved away from, and — only when they pass one — a new password.

**A password is included only when the operator asks for it**, and the command that
does so also sets it on the account, so the message and the account cannot
disagree. The message then says to change it after signing in. Without one, the
footer says the message carries no password, which is the default and the safer
habit: an emailed password sits in every copy of that mailbox.

Rendering is pure and separate from sending, like `notify.render`: `render`
returns strings and opens no socket, which is what lets `--preview` show exactly
what would arrive without a key configured. The styling is the digest's —
`notify.TOKENS`, inline, tables — for the reasons that module gives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracker.notify import FONT_DISPLAY, FONT_MONO, FONT_SANS, TOKENS, WIDTH, esc

SUBJECT = "Your dc-tracker console account has been updated"


@dataclass(frozen=True)
class Notice:
    """One rendered message: what `Transport.send` takes."""

    subject: str
    html_body: str
    text_body: str


def settings_rows(account: dict[str, Any]) -> list[tuple[str, str]]:
    """The account as a reader should see it, from `accounts.detail`. Labels, not fields."""
    return [
        ("Sign-in email", str(account["email"])),
        ("Name", str(account.get("name") or "—")),
        ("Status", "Disabled — it cannot sign in" if account.get("disabled") else "Active"),
        (
            "Sees",
            "The whole database" if account.get("watch_all") else "Only its watchlist",
        ),
        ("Role", "Administrator" if account.get("admin") else "Reader"),
    ]


def _lead(account: dict[str, Any], old_email: str | None) -> str:
    """The first sentence: what happened, in the reader's terms."""
    if old_email and old_email.strip().lower() != str(account["email"]).lower():
        return (
            f"The sign-in email for your dc-tracker console account has changed from "
            f"{old_email.strip()} to {account['email']}. Sign in with the new address from now on."
        )
    return "The administrator of the dc-tracker console has updated the account below."


def render(
    account: dict[str, Any],
    *,
    note: str | None = None,
    console_url: str | None = None,
    subject: str | None = None,
    old_email: str | None = None,
    new_password: str | None = None,
) -> Notice:
    """The whole message for one account's current settings. Pure; sends nothing.

    `note` is the operator's own paragraph, `old_email` the address the account
    moved away from, and `new_password` a password to include — each optional, and
    each shown only when given.
    """
    rows = settings_rows(account)
    greeting = f"Hello {esc(account['name'])}," if account.get("name") else "Hello,"
    lead = _lead(account, old_email)
    table = "".join(
        f"""
          <tr>
            <td style="padding:8px 12px 8px 0;font-family:{FONT_SANS};font-size:13px;
                       color:{TOKENS["muted_foreground"]};white-space:nowrap;
                       vertical-align:top;">{esc(label)}</td>
            <td style="padding:8px 0;font-family:{FONT_SANS};font-size:14px;
                       color:{TOKENS["foreground"]};">{esc(value)}</td>
          </tr>"""
        for label, value in rows
    )
    note_block = ""
    if note and note.strip():
        note_block = f"""
      <tr><td style="padding:0 0 18px 0;">
        <div style="border-left:3px solid {TOKENS["primary"]};padding:4px 0 4px 14px;
                    font-family:{FONT_SANS};font-size:14px;line-height:1.55;
                    color:{TOKENS["foreground"]};white-space:pre-wrap;">{esc(note.strip())}</div>
      </td></tr>"""
    password_block = ""
    if new_password:
        password_block = f"""
      <tr><td style="padding:0 0 18px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:{TOKENS["accent_soft"]};border-radius:12px;">
          <tr><td style="padding:14px 18px;font-family:{FONT_SANS};font-size:14px;
                     line-height:1.55;color:{TOKENS["accent_foreground"]};">
            Your new password:
            <span style="font-family:{FONT_MONO};font-size:15px;font-weight:600;
                         color:{TOKENS["foreground"]};">{esc(new_password)}</span><br>
            Please change it after you sign in: click your name at the top of the
            console and choose a password of your own.
          </td></tr>
        </table>
      </td></tr>"""
    button = ""
    if console_url:
        button = f"""
      <tr><td align="center" style="padding:22px 0 6px 0;">
        <a href="{esc(console_url)}"
           style="display:inline-block;background:{TOKENS["primary"]};
                  color:{TOKENS["primary_foreground"]};font-family:{FONT_SANS};
                  font-size:14px;font-weight:600;text-decoration:none;
                  padding:11px 22px;border-radius:10px;">Open the console</a>
      </td></tr>"""
    footer = (
        "This message contains a password. Change it after you sign in, and delete "
        "this email once you have."
        if new_password
        else "This message never contains a password. If you need one, or did not "
        "expect this change, contact whoever runs the console."
    )
    title = (subject or "").strip() or SUBJECT

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
      <tr><td style="padding:0 0 14px 0;font-family:{FONT_SANS};font-size:15px;
                 line-height:1.55;color:{TOKENS["foreground"]};">
        {greeting}<br><br>
        {esc(lead)} This is how it is set up now:
      </td></tr>
      {note_block}
      {password_block}
      <tr><td style="padding:0 0 6px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:{TOKENS["surface"]};border:1px solid {TOKENS["border"]};
                      border-radius:14px;">
          <tr><td style="padding:10px 18px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0">{table}
            </table>
          </td></tr>
        </table>
      </td></tr>
      {button}
      <tr><td style="padding:24px 0 0 0;border-top:1px solid {TOKENS["border"]};
                 font-family:{FONT_SANS};font-size:12px;line-height:1.6;
                 color:{TOKENS["muted_foreground"]};">
        {esc(footer)}
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

    lines = [
        "Hello" + (f" {account['name']}," if account.get("name") else ","),
        "",
        f"{lead} This is how it is set up now:",
        "",
    ]
    if note and note.strip():
        lines += [note.strip(), ""]
    if new_password:
        lines += [
            f"Your new password: {new_password}",
            "Please change it after you sign in: click your name at the top of the",
            "console and choose a password of your own.",
            "",
        ]
    lines += [f"  {label}: {value}" for label, value in rows]
    if console_url:
        lines += ["", f"Open the console: {console_url}"]
    lines += ["", footer]
    return Notice(title, html_body, "\n".join(lines))


__all__ = ["SUBJECT", "Notice", "render", "settings_rows"]
