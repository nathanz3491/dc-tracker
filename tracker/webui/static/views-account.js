/* The two pages about accounts rather than data: your own, and the admin page.
 *
 * Kept out of app.js for the reason views-help.js is: they share nothing with the
 * dataset views but the design system, and a reader of one should not have to
 * scroll past the other. `api` is passed in rather than re-implemented, so a
 * signed-out session sends these pages to the gate exactly as it does the rest.
 *
 * What the server decides and these pages only reflect:
 *   - changing your own password asks for nothing but the new one, and signs out
 *     every other device (`Handler._own_password`);
 *   - the admin page is drawn for `account.admin` but every route re-checks it, so
 *     hiding the button is courtesy, not the control;
 *   - an admin cannot disable or delete their own account here, and nobody can
 *     grant admin from a browser — `tracker users admin`, on the host.
 */

const html = htm.bind(React.createElement);
const { useState, useEffect, useCallback } = React;
const NS = window.MeridianDesignSystem_6e9015 || {};
const {
  Button, Card, Input, Switch, Alert, EmptyState,
  Table, TableHeader, TableBody, TableRow, TableHead,
} = NS;

function Heading({ figure, title, children }) {
  return html`
    <div style=${{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style=${{ fontFamily: "var(--font-mono)", fontSize: 12, textTransform: "uppercase",
                      letterSpacing: "0.16em", color: "var(--muted-foreground)" }}>${figure}</span>
      <h1 style=${{ margin: 0, fontFamily: "var(--font-display)", fontSize: 30, fontWeight: 500,
                    letterSpacing: "-0.02em", lineHeight: 1.15 }}>${title}</h1>
      ${children && html`<p style=${{ margin: "2px 0 0", fontSize: 14, lineHeight: "22px",
                                      color: "var(--muted-foreground)", maxWidth: "78ch" }}>${children}</p>`}
    </div>`;
}

function Notice({ tone, children }) {
  if (!children) return null;
  return html`<${Alert} variant=${tone}><div class="mrd-alert-desc">${children}</div><//>`;
}

const label = { fontSize: 12, fontWeight: 500, color: "var(--muted-foreground)" };
const pad = { display: "grid", gap: 12, padding: "16px 20px" };

/* Two fields that must match, and the button. Shared by your own password and an
   admin setting somebody else's, so the two cannot drift in what they accept. */
function PasswordForm({ onSubmit, cta, idBase = "pw" }) {
  const [first, setFirst] = useState("");
  const [second, setSecond] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const mismatch = second && first !== second;
  const submit = async (event) => {
    event.preventDefault();
    if (!first || first !== second) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(first);
      setFirst("");
      setSecond("");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return html`
    <form onSubmit=${submit} style=${{ display: "grid", gap: 10, maxWidth: 360 }}>
      <label style=${label} htmlFor=${`${idBase}-1`}>new password</label>
      <${Input} id=${`${idBase}-1`} type="password" autoComplete="new-password" value=${first}
                onChange=${(e) => setFirst(e.target.value)} />
      <label style=${label} htmlFor=${`${idBase}-2`}>again</label>
      <${Input} id=${`${idBase}-2`} type="password" autoComplete="new-password" value=${second}
                onChange=${(e) => setSecond(e.target.value)} />
      ${mismatch && html`<span style=${{ fontSize: 12, color: "var(--danger)" }}>those do not match</span>`}
      <${Notice} tone="danger">${error}<//>
      <div><${Button} type="submit" size="sm" disabled=${busy || !first || first !== second}>
        ${busy ? "Saving…" : cta}<//></div>
    </form>`;
}

export function AccountView({ data, api }) {
  const [done, setDone] = useState(null);
  const account = data.account;
  if (!account) {
    return html`<div class="dc-view" style=${{ padding: "22px 26px 60px" }}>
      <${EmptyState} variant="dashed" title="This console has no accounts"
        description="There is nobody signed in, so there is no password to change." /></div>`;
  }
  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)",
                                            gap: 16, padding: "22px 26px 60px", maxWidth: 760 }}>
      <${Heading} figure="your account" title=${account.name || account.email}>
        Signed in as ${account.email}.
      <//>
      <${Card}>
        <div style=${pad}>
          <div style=${{ fontWeight: 600 }}>Change your password</div>
          <p style=${{ margin: 0, fontSize: 13, color: "var(--muted-foreground)", lineHeight: "20px" }}>
            Being signed in is enough — your current password is not asked for. Every other
            device signed in to this account is signed out; this one stays signed in.
          </p>
          <${Notice} tone="success">${done}<//>
          <${PasswordForm} cta="Change password" idBase="own-pw" onSubmit=${async (password) => {
            setDone(null);
            const reply = await api("/api/account/password", { method: "POST", body: { password } });
            setDone(reply.note || "Password changed.");
          }} />
        </div>
      <//>
    </div>`;
}

function when(value) {
  return value ? value.slice(0, 16).replace("T", " ") : "never";
}

function Row({ a, active, onPick }) {
  return html`
    <tr class="dc-row" role="button" tabindex="0" aria-pressed=${active}
        style=${active ? { background: "var(--muted)" } : undefined}
        onClick=${() => onPick(a.id)}
        onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(a.id); } }}>
      <td class="dc-cell" style=${{ fontWeight: 600 }}>${a.email}</td>
      <td class="dc-cell">${a.name || ""}</td>
      <td class="dc-cell">${a.admin ? "admin" : ""}</td>
      <td class="dc-cell" style=${{ color: a.disabled ? "var(--danger)" : undefined }}>
        ${a.disabled ? "disabled" : "active"}</td>
      <td class="dc-cell">${a.watch_all ? "everything" : `${a.watches} watched`}</td>
      <td class="dc-cell dc-num" style=${{ fontSize: 12 }}>${when(a.last_seen_at)}</td>
    </tr>`;
}

/* One account, editable. The form holds its own draft and is keyed on the id, so
   picking another row starts from that row's values rather than the last one's. */
function Editor({ a, self, act, onDeleted }) {
  const [email, setEmail] = useState(a.email);
  const [name, setName] = useState(a.name || "");
  const [watchAll, setWatchAll] = useState(!!a.watch_all);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async (path, body, ok) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const reply = await act(path, { id: a.id, ...body });
      if (reply.deleted) return onDeleted(reply.deleted);
      const said = reply.changes && reply.changes.length ? reply.changes.join("; ") : ok;
      setMessage(said);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const dirty = email.trim() !== a.email || (name.trim() || null) !== (a.name || null)
    || watchAll !== !!a.watch_all;

  return html`
    <${Card}>
      <div style=${pad}>
        <div style=${{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "baseline" }}>
          <span style=${{ fontFamily: "var(--font-display)", fontSize: 22 }}>${a.email}</span>
          ${a.admin && html`<span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>admin</span>`}
          ${self && html`<span style=${{ fontSize: 12, color: "var(--muted-foreground)" }}>(you)</span>`}
        </div>
        <div style=${{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 14px",
                       fontSize: 13 }}>
          <span style=${label}>status</span>
          <span>${a.disabled ? `disabled since ${when(a.disabled_at)}` : "active"}</span>
          <span style=${label}>joined</span><span>${a.joined}</span>
          <span style=${label}>created</span><span>${when(a.created_at)}</span>
          <span style=${label}>last signed in</span><span>${when(a.last_seen_at)}</span>
          <span style=${label}>last changed</span><span>${when(a.updated_at)}</span>
          <span style=${label}>watchlist</span><span>${a.watches} entr${a.watches === 1 ? "y" : "ies"}</span>
        </div>
        <${Notice} tone="success">${message}<//>
        <${Notice} tone="danger">${error}<//>

        <div style=${{ display: "grid", gap: 10, maxWidth: 420, paddingTop: 6 }}>
          <label style=${label} htmlFor=${`acct-${a.id}-email`}>sign-in email</label>
          <${Input} id=${`acct-${a.id}-email`} value=${email} onChange=${(e) => setEmail(e.target.value)} />
          <label style=${label} htmlFor=${`acct-${a.id}-name`}>display name</label>
          <${Input} id=${`acct-${a.id}-name`} value=${name} placeholder="none"
                    onChange=${(e) => setName(e.target.value)} />
          <${Switch} size="sm" label="Sees the whole database" checked=${watchAll}
                     onCheckedChange=${(v) => setWatchAll(!!v)} />
          <div><${Button} size="sm" disabled=${busy || !dirty}
            onClick=${() => run("update", { email: email.trim(), name, watch_all: watchAll }, "saved")}>
            Save changes<//></div>
        </div>

        <div style=${{ paddingTop: 8, display: "grid", gap: 8 }}>
          <div style=${{ fontWeight: 600, fontSize: 14 }}>Set a new password</div>
          <${PasswordForm} cta="Set password" idBase=${`acct-${a.id}-pw`}
            onSubmit=${(password) => run("password", { password }, "password set")} />
        </div>

        <div style=${{ display: "flex", flexWrap: "wrap", gap: 8, paddingTop: 10 }}>
          <${Button} size="sm" variant="outline" disabled=${busy}
            onClick=${() => run("signout", {}, "signed out everywhere")}>
            ${self ? "Sign out my other devices" : "Sign out everywhere"}<//>
          ${!self && html`
            <${Button} size="sm" variant="outline" disabled=${busy}
              onClick=${() => run(a.disabled ? "enable" : "disable", {}, a.disabled ? "enabled" : "disabled")}>
              ${a.disabled ? "Enable" : "Disable"}<//>
            <${Button} size="sm" variant="ghost" disabled=${busy} style=${{ color: "var(--danger)" }}
              onClick=${() => {
                if (window.confirm(`Delete ${a.email} and their watchlist? This cannot be undone.`)) {
                  run("delete", {}, "deleted");
                }
              }}>Delete<//>`}
        </div>
      </div>
    <//>`;
}

export function AdminView({ data, api }) {
  const [state, setState] = useState({ accounts: null, invites: [], error: null });
  const [picked, setPicked] = useState(null);
  const me = data.account;

  const load = useCallback(() => api("/api/admin/users")
    .then((reply) => setState({ accounts: reply.accounts, invites: reply.invites, error: null }))
    .catch((e) => setState({ accounts: null, invites: [], error: e.message })), [api]);
  useEffect(() => { if (me && me.admin) load(); }, [me, load]);

  if (!me || !me.admin) {
    return html`<div class="dc-view" style=${{ padding: "22px 26px 60px" }}>
      <${EmptyState} variant="dashed" title="The admin page is for admins"
        description="An admin is made at the host's terminal, with tracker users admin." /></div>`;
  }

  /* Every change re-reads the list, so the table never shows a row as it was
     before the edit the panel just reported. */
  const act = async (verb, body) => {
    const reply = await api(`/api/admin/users/${verb}`, { method: "POST", body });
    await load();
    return reply;
  };
  const accounts = state.accounts || [];
  const current = accounts.find((a) => a.id === picked);

  return html`
    <div class="dc-view dc-rise" style=${{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)",
                                            gap: 16, padding: "22px 26px 60px" }}>
      <${Heading} figure="admin" title="Accounts" />
      <${Notice} tone="danger">${state.error}<//>
      <${Card}>
        ${/* Scrolls inside its card on a phone rather than widening the page. */ ""}
        <div style=${{ overflowX: "auto" }}>
        <${Table} density="compact">
          <${TableHeader}><${TableRow}>
            <${TableHead}>email<//><${TableHead}>name<//><${TableHead}>role<//>
            <${TableHead}>status<//><${TableHead}>sees<//><${TableHead}>last signed in<//>
          <//><//>
          <${TableBody}>
            ${accounts.map((a) => html`
              <${Row} key=${a.id} a=${a} active=${a.id === picked} onPick=${setPicked} />`)}
          <//>
        <//>
        </div>
        ${state.invites.length > 0 && html`
          <p style=${{ margin: 0, padding: "10px 16px", fontSize: 12, color: "var(--muted-foreground)" }}>
            ${state.invites.length} unredeemed invite(s):${" "}
            ${state.invites.map((i) => `${i.note || "no note"} (expires ${i.expires_at.slice(0, 10)})`).join(", ")}
          </p>`}
      <//>
      ${current && html`<${Editor} key=${current.id} a=${current} self=${current.id === me.id}
                          act=${act} onDeleted=${() => setPicked(null)} />`}
    </div>`;
}
