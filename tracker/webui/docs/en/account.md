# Your account

Your name in the top-right corner of the console opens **your account**.

## Changing your password

Enter the new password twice and press **Change password**. You are not asked for the old one, because you are already signed in. Changing it **signs out every other device** you were signed in on; the one you are using stays signed in.

If you have forgotten your password, sign out and use **Forgot password?** on the sign-in page instead. See [Accounts and signing in](signing-in).

## API keys

**API keys** lets a program read the tracker through the API with your access. Give a key a name, press **Create key**, and copy it at once: it starts with `dct_` and is shown only that one time. The list shows each key's name, first characters, when it was made and last used, and a **Revoke** button; revoking stops a key at once. An account can hold five active keys. See [The API](api).

## What is kept about you

- your email address and, if you gave one, your name;
- your password, stored only in a form that cannot be turned back into the password;
- your [watchlist](monitor), and which updates you have already been emailed;
- when you last signed in;
- whether the AI panels are switched on for you;
- your API keys' names, first characters and dates — never the keys themselves.

## For administrators

Accounts with administrator rights see an **Admin** button next to their name. The Admin page has two parts.

### Waiting for approval

Requests that have confirmed their email, with the address, name and when they signed up and confirmed.

- **Approve** lets the person in and emails them that their account is ready.
- **Approve with AI panels** does the same and switches the AI panels on for them.
- **Turn down** deletes the request, after asking you to confirm.

### Accounts

Every account, with its role, status (*active*, *disabled*, *waiting for approval*, *email not confirmed*), what it sees (*everything*, or how many entries it watches) and when it last signed in. Click a row to edit it:

- **sign-in email** and **display name**;
- **Sees the whole database** — this is the person's *watch everything* setting. Every account can already read all the data; this switch only widens their Updates and Monitor pages to everything, and stops their morning email;
- **May use the AI panels (they spend tokens)**;
- **Set a new password** — sets it directly; no email is sent to the person;
- **Sign out everywhere** — ends all their sessions;
- **Disable / Enable**, and **Delete**, which removes the account and its watchlist for good.

Disabling an account also stops its API keys working; deleting it deletes them. You cannot disable or delete your own account. Making someone an administrator, and creating invite codes, are done by whoever maintains the tracker, not on this page; outstanding invite codes are listed at the bottom with their expiry dates.
