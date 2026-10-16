# Accounts and signing in

Everything in the console is behind a sign-in. These docs are not: anybody can read them.

## Getting an account

There are two ways in.

**With an invite code.** Somebody who runs the console can give you a code. Enter it on the **Create an account** page, in the *Invite code* field, and you are signed in straight away. A code works for seven days from when it was made; an expired or used one is refused with "that invite code is not usable. Ask for a fresh one."

**Without a code.** Fill in **Create an account** with your email, a password of at least six characters and, if you like, your name (it is shown to whoever approves you). Then:

1. **Confirm your email.** We send a message titled *Confirm your email for dc-tracker*. Open its link and press **Confirm my email**. The link works for 24 hours, and nothing happens until you press the button, so a mail scanner cannot confirm it for you.
2. **Wait for approval.** An administrator reviews the request. When they approve it you receive *Your dc-tracker account is ready*, with a **Sign in** button.

> [!NOTE]
> The page says the same thing whether or not the address already has an account, so it cannot be used to find out who has one. If you already had an account, the email you receive says so and offers a password reset instead.

An unconfirmed request is removed after a week. If the console cannot send email at all, it says so and asks you to get an invite code instead.

## Signing in

Use **Sign in** with your email and password. **Show** reveals the password as you type it.

| Message | What it means |
| --- | --- |
| Wrong email or password. | One of the two is wrong. |
| Too many attempts. Locked for about N more minutes. | Eight wrong attempts lock sign-in from your connection for 15 minutes. |
| Confirm your email first | Open the link in the confirmation email. If you lost it, create the account again with the same address and a fresh link is sent. |
| Your account is waiting for an administrator to approve it. | Nothing to do but wait; you will be emailed. |
| This account is disabled. | Ask whoever runs the console. |

If you followed a link to a console page while signed out, you are taken back to that page after signing in.

A session lasts **12 hours**. When it runs out, the next thing you do takes you to the sign-in page, and back to where you were afterwards.

## Forgotten password

On the sign-in page choose **Forgot password?**, enter your address and press **Email me a link**. If an account uses that address, it receives *Reset your dc-tracker password*. The link works for one hour and only once. Setting a new password signs you out on every other device.

## Changing your password

Signed in, open your name in the top-right corner to reach [Your account](account), where you can set a new password. Doing so signs out every *other* device you were signed in on.

## Signing out

**Sign out** in the top-right corner ends the session and shows "You're signed out."

## What an account lets you do

Every account reads the same data. What is yours is:

- your **watchlist**, which decides what the [Updates](updates) page and the morning email show you (see [Monitor](monitor));
- your **password**;
- whether the **AI panels** on a project's page are switched on for you. They cost money each time they run, so new accounts that signed up without a code start with them off; an administrator can switch them on;
- whether you may use the [API](api). The same rule: accounts that signed up without a code start with it off.

Nothing you do in the console changes the tracker's data. The only things you can change are your own watchlist and password.
