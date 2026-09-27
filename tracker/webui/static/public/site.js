/*
 * dc-tracker's public pages: the front page, the five account pages and the 404 page.
 *
 * Loaded in <head> without defer, on purpose. The first statement marks the document
 * as scripted before its first paint, which is the only way to do that under a policy
 * that allows no inline script. The front page's drawing hides its strokes only under
 * that mark, so a script that fails to load leaves the drawing complete, not blank.
 *
 * Each page is its own document and names itself in <body data-page>. Nothing here
 * adds or removes a field: a form stays as it loaded until its result replaces it.
 */
document.documentElement.classList.add("js");

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const NO_ANSWER = "The console didn't answer. Check your connection, then try again.";
  const UNREADABLE = "That didn't work. Try again in a moment.";

  /** POST a JSON body. An unreadable reply becomes {}. */
  const post = async (route, body) => {
    const res = await fetch(route, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let payload = {};
    try {
      const parsed = await res.json();
      if (parsed && typeof parsed === "object") payload = parsed;
    } catch {
      payload = {};
    }
    return { res, payload };
  };

  /**
   * Leave for `next`. The server has already checked it against the console's own
   * pages; this is a second guard, so nothing but a same-site path is ever followed.
   * Replace rather than assign, so the form is not behind the console in the history.
   */
  const go = (next) => {
    window.location.replace(typeof next === "string" && /^\/(?![/\\])/.test(next) ? next : "/");
    return true;
  };

  /** Show an element and replay its fade, so a second answer is seen as new. */
  const reveal = (el) => {
    el.hidden = false;
    el.classList.remove("is-new");
    void el.offsetWidth;
    el.classList.add("is-new");
  };

  /** A message under the button. Server refusals are written for a terminal too, so
   *  they may start lowercase. */
  const show = (text, tone) => {
    const msg = $("msg");
    msg.textContent = text.charAt(0).toUpperCase() + text.slice(1);
    msg.className = "msg msg--" + tone;
    reveal(msg);
  };

  const hideMessage = () => {
    const msg = $("msg");
    msg.hidden = true;
    msg.textContent = "";
  };

  /** The server's refusal, verbatim. A lockout, an account that is not ready yet, and
   *  a console that cannot take this form are warnings; anything else is an error. */
  const refuse = (res, payload) => {
    const warn = res.status === 429 || res.status === 403 || Boolean(payload.reason);
    const text = typeof payload.error === "string" && payload.error ? payload.error : UNREADABLE;
    show(text, warn ? "warn" : "err");
  };

  /** Rewrite the card's one-line foot: an optional sentence, then one link. */
  const setFoot = (sentence, text, href) => {
    const link = document.createElement("a");
    link.href = href;
    link.textContent = text;
    $("foot").replaceChildren(...(sentence ? [sentence, link] : [link]));
  };

  /** Replace a finished form with its result. The head keeps its size, so the card
   *  only ever gets shorter. */
  const finish = ({ eyebrow, title, result, action = false, foot }) => {
    if (eyebrow) $("eyebrow").textContent = eyebrow;
    $("title").textContent = title;
    $("lede").textContent = "";
    $("form").hidden = true;
    hideMessage();
    const out = $("result");
    out.textContent = typeof result === "string" ? result : "";
    reveal(out);
    $("action").hidden = !action;
    setFoot(...foot);
    $("title").focus({ preventScroll: true });
  };

  /**
   * One submit: the button is disabled and says what is happening while the request
   * is out. `answer(res, payload)` returns true when it is leaving the page, so the
   * button stays busy until the next page arrives instead of flicking back.
   */
  const submit = async (event, busyText, request, answer) => {
    event.preventDefault();
    const form = $("form");
    const button = $("go");
    const label = $("go-label");
    if (button.disabled) return;
    const idle = label.textContent;
    button.disabled = true;
    label.textContent = busyText;
    form.setAttribute("aria-busy", "true");
    hideMessage();
    let leaving = false;
    try {
      const { res, payload } = await request();
      leaving = answer(res, payload) === true;
    } catch {
      // Nothing was judged, so nothing typed is cleared.
      show(NO_ANSWER, "err");
    }
    if (!leaving) {
      button.disabled = false;
      label.textContent = idle;
      form.removeAttribute("aria-busy");
    }
  };

  /** After a refusal, clear the password and put the cursor back in it. */
  const retype = (input) => {
    input.value = "";
    input.focus();
  };

  /** A mailed link's token: read once, kept in memory, and taken out of the address
   *  so it sits in neither the history nor the screen. */
  const takeToken = () => {
    const token = new URLSearchParams(window.location.search).get("t") || "";
    window.history.replaceState(null, "", window.location.pathname);
    return token;
  };

  /** Show/Hide inside a password field. The name and the text change together, so the
   *  button never says one thing and does another. */
  const wireReveal = () => {
    document.querySelectorAll(".reveal").forEach((button) => {
      const input = $(button.getAttribute("aria-controls"));
      if (!input) return;
      button.addEventListener("click", () => {
        const showing = input.type === "password";
        input.type = showing ? "text" : "password";
        button.textContent = showing ? "Hide" : "Show";
        button.setAttribute("aria-label", showing ? "Hide password" : "Show password");
      });
    });
  };

  // ---- the pages ---------------------------------------------------------------

  /** The front page: Fig. 1 draws itself once, the first time it is mostly in view. */
  const home = () => {
    const plate = document.querySelector(".plate");
    if (!plate) return;
    const draw = () => plate.classList.add("is-drawn");
    if (!("IntersectionObserver" in window)) {
      draw();
      return;
    }
    const seen = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting && entry.intersectionRatio >= 0.34)) {
          draw();
          seen.disconnect();
        }
      },
      { threshold: 0.35 },
    );
    seen.observe(plate);
  };

  const signin = () => {
    const params = new URLSearchParams(window.location.search);
    const next = params.get("next");
    if (next) $("alt").setAttribute("href", "/register?next=" + encodeURIComponent(next));
    if (params.get("out") === "1") {
      show("You're signed out.", "ok");
      window.history.replaceState(null, "", "/signin");
    }
    const email = $("email");
    const password = $("password");
    $("form").addEventListener("submit", (event) =>
      submit(
        event,
        "Signing in…",
        () => post("/api/login", { email: email.value, password: password.value, next: next || undefined }),
        (res, payload) => {
          if (res.ok) return go(payload.next);
          refuse(res, payload);
          retype(password);
          return false;
        },
      ),
    );
  };

  const register = () => {
    const next = new URLSearchParams(window.location.search).get("next");
    if (next) $("alt").setAttribute("href", "/signin?next=" + encodeURIComponent(next));
    const email = $("email");
    const password = $("password");
    const name = $("name");
    const code = $("code");
    $("form").addEventListener("submit", (event) =>
      submit(
        event,
        "Sending…",
        () =>
          post("/api/signup", {
            email: email.value,
            password: password.value,
            name: name.value,
            code: code.value.trim(),
            next: next || undefined,
          }),
        (res, payload) => {
          // With an invite code the answer signs you in; without one it says what
          // happens next, the same words whether or not the address was new.
          if (res.ok && !payload.message) return go(payload.next);
          if (res.ok) {
            finish({
              eyebrow: "Step 2 of 3 · Your email",
              title: "Request sent",
              result: payload.message,
              foot: ["", "Back to the front page", "/"],
            });
            return false;
          }
          refuse(res, payload);
          // Sign-ups are closed here (no mail, or nobody to approve). An invite code
          // still works, so keep the password and go to the one way in that is left.
          if (payload.reason) {
            code.focus();
            return false;
          }
          retype(password);
          return false;
        },
      ),
    );
  };

  const forgot = () => {
    const email = $("email");
    $("form").addEventListener("submit", (event) =>
      submit(
        event,
        "Sending…",
        () => post("/api/forgot", { email: email.value }),
        (res, payload) => {
          if (res.ok) {
            finish({
              title: "Check your inbox",
              result: payload.message,
              foot: ["", "Back to sign in", "/signin"],
            });
            return false;
          }
          refuse(res, payload);
          email.focus();
          return false;
        },
      ),
    );
  };

  const reset = () => {
    const token = takeToken();
    const password = $("password");
    $("form").addEventListener("submit", (event) =>
      submit(
        event,
        "Saving…",
        () => post("/api/reset", { token, password: password.value }),
        (res, payload) => {
          if (res.ok && payload.signed_in === true) return go("/");
          if (res.ok) {
            finish({
              // Not signed in, so the account is still waiting for approval: a
              // link back to signing in would offer what the sentence just refused.
              title: "Password set",
              result: payload.message,
              foot: ["", "Back to the front page", "/"],
            });
            return false;
          }
          refuse(res, payload);
          retype(password);
          return false;
        },
      ),
    );
  };

  /**
   * The page never confirms on load: only a press of its button does. A mail filter
   * that opens links and runs their scripts therefore cannot confirm an address for
   * somebody who never asked.
   */
  const confirmEmail = () => {
    const token = takeToken();
    $("form").addEventListener("submit", (event) =>
      submit(
        event,
        "Confirming…",
        () => post("/api/confirm", { token }),
        (res, payload) => {
          if (res.ok) {
            const active = payload.status === "active";
            finish({
              eyebrow: active ? "All set" : "Step 3 of 3 · Approval",
              title: "Email confirmed",
              result: payload.message,
              action: active,
              foot: ["", "Back to the front page", "/"],
            });
            return false;
          }
          if (res.status === 400) {
            // The link is spent or wrong; pressing again cannot help.
            $("title").textContent = "That link didn't work";
            $("lede").textContent = "";
            $("form").hidden = true;
            refuse(res, payload);
            setFoot("Need a fresh link? ", "Create an account", "/register");
            $("title").focus({ preventScroll: true });
            return false;
          }
          // A lockout or a busy database: the button stays, and pressing it later works.
          refuse(res, payload);
          return false;
        },
      ),
    );
  };

  const PAGES = { home, signin, register, forgot, reset, confirm: confirmEmail };

  document.addEventListener("DOMContentLoaded", () => {
    wireReveal();
    const page = PAGES[document.body.dataset.page];
    if (page) page();
  });
})();
