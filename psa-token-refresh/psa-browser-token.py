#!/usr/bin/env python3
"""Fetch PSA/Stellantis tokens via browser - rewrite with polling and diagnosis.

Why new: The previous helper only reacted to the browser's `loadFinished` event
and checked whether 'OPLoginPage.php' was in the URL. Since PSA put a
ForgeRock interface in front of the Gigya login (idpcvs.<brand>.com/am/XUI/#login),
the redirects happen via JavaScript - `loadFinished` does not fire reliably for
that, so the helper runs into its timeout ("timeout", exit 1).

This version:
  * polls the page state every 700 ms (URL, visible fields, buttons),
  * fills the login form generically (first text/email field + password field),
  * confirms consent pages automatically,
  * intercepts the redirect to the brand scheme (mymap:// ...) and exchanges the code
    for tokens,
  * on failure writes a diagnosis dump to /data/psa-login-debug.json
    (URL history, title, fields, buttons) instead of only reporting "timeout",
  * drives the browser through one of two backends: `playwright` (default; Chromium out
    of the Playwright package, works on amd64 and aarch64) or `qt` (PyQt5/QtWebEngine,
    the historical path, only present in the amd64 image). The flow - poll, decide, act -
    is shared, only the engine differs; see the backends section below.

Invocation:
  python3 psa-browser-token.py -b peugeot -u user -p pass [-c de] [-t 30]
  python3 psa-browser-token.py -b peugeot --probe        # without credentials
  python3 psa-browser-token.py -b peugeot --backend qt   # legacy engine

Return codes: 0 = tokens (JSON on stdout), 1 = error, 3 = diagnosis written.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# OAuth client credentials of the official brand apps (myPeugeot, MyCitroen, MyDS, MyOpel).
# These identify the *application*, not a user: they are not linked to any account, vehicle
# or personal data. They are the same credentials the upstream projects publish (evcc and
# hurzhurz/psa-token-helper) and are required to talk to the PSA/Stellantis identity
# servers. Mobile apps cannot keep a secret, so this value is public by design.
#
# If PSA ever rotates them, the project needs an update. If you prefer to use your own
# (extracted from your own app copy), set the environment variables
# PSA_CLIENT_ID_<BRAND> and PSA_CLIENT_SECRET_<BRAND>, e.g.
#   PSA_CLIENT_ID_PEUGEOT=...  PSA_CLIENT_SECRET_PEUGEOT=...
BRANDS = {
    "citroen": {"scheme": "mymacsdk", "realm": "citroen.com",
                "clientid": "5364defc-80e6-447b-bec6-4af8d1542cae",
                "clientsecret": "iE0cD8bB0yJ0dS6rO3nN1hI2wU7uA5xR4gP7lD6vM0oH0nS8dN"},
    "ds": {"scheme": "mymdssdk", "realm": "driveds.com",
           "clientid": "cbf74ee7-a303-4c3d-aba3-29f5994e2dfa",
           "clientsecret": "X6bE6yQ3tH1cG5oA6aW4fS6hK0cR0aK5yN2wE4hP8vL8oW5gU3"},
    "opel": {"scheme": "mymopsdk", "realm": "opel.com",
             "clientid": "07364655-93cb-4194-8158-6b035ac2c24c",
             "clientsecret": "F2kK7lC5kF5qN7tM0wT8kE3cW1dP0wC5pI6vC0sQ5iP5cN8cJ8"},
    "peugeot": {"scheme": "mymap", "realm": "peugeot.com",
                "clientid": "1eebc2d5-5df3-459b-a624-20abfcf82530",
                "clientsecret": "T5tP7iS0cO8sC0lA2iE2aR7gK6uE5rF3lJ8pC3nO1pR7tL8vU1"},
}

def apply_credential_overrides(brands: dict, env=None) -> dict:
    """Allow replacing the built-in app credentials with your own.

    Read from the environment so that a fork can ship without hard-coded credentials:
    PSA_CLIENT_ID_<BRAND> / PSA_CLIENT_SECRET_<BRAND> (brand upper-cased, e.g. PEUGEOT).
    """
    env = os.environ if env is None else env
    out = {name: dict(data) for name, data in brands.items()}
    for name, data in out.items():
        cid = env.get(f"PSA_CLIENT_ID_{name.upper()}")
        secret = env.get(f"PSA_CLIENT_SECRET_{name.upper()}")
        if cid:
            data["clientid"] = cid.strip()
        if secret:
            data["clientsecret"] = secret.strip()
    return out


BRANDS = apply_credential_overrides(BRANDS)

# Labels for which a checkbox counts as consent to terms.
# Deliberately narrow: newsletter/marketing is not included.
TERMS_MUSTER = ("agb", "bedingungen", "nutzungsbedingungen", "datenschutz", "privacy", "terms",
                "zustimm", "akzeptier", "accept", "conditions", "einverstanden")

CONSENT_WORDS = (
    "accept", "allow", "agree", "consent", "continue", "confirm", "ok", "weiter",
    "akzeptieren", "zustimmen", "einverstanden", "annehmen", "bestaetigen", "fortfahren",
)


# ------------------------------------------------------------------ pure logic

def generate_pkce(length: int = 64) -> tuple[str, str]:
    if not 43 <= length <= 128:
        raise ValueError("PKCE length must be between 43 and 128")
    verifier = secrets.token_urlsafe(length)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii")[:-1]
    return verifier, challenge


def idp_base(brand: str) -> str:
    """IdP base URL; overridable via the PSA_IDP_BASE environment variable (tests/diagnosis)."""
    return os.environ.get("PSA_IDP_BASE", "").rstrip("/") or f"https://idpcvs.{BRANDS[brand]['realm']}"


def build_authorize_url(brand: str, country: str, challenge: str,
                        state: str | None = None) -> str:
    data = BRANDS[brand]
    redirect = f"{data['scheme']}://oauth2redirect/{country.lower()}"
    url = (
        f"{idp_base(brand)}/am/oauth2/authorize"
        f"?client_id={data['clientid']}"
        f"&redirect_uri={urllib.parse.quote(redirect, safe='')}"
        f"&response_type=code&scope=openid%20profile"
        f"&code_challenge_method=S256&code_challenge={challenge}"
    )
    if state:
        url += f"&state={state}"
    return url


def new_state() -> str:
    return secrets.token_urlsafe(12)


def zeit() -> str:
    return time.strftime("%H:%M:%S")


def extract_code(url: str, state: str | None = None) -> str | None:
    """Extracts the authorization code - but ONLY from the brand scheme.

    Important: intermediate pages in the flow also carry a `code` (e.g. the
    Gigya/OIDC detour on id-dcr.<brand>.com). If one of those is picked, the token
    exchange fails. Only the redirect to the brand scheme is valid
    (`mymap://oauth2redirect/<country>?code=...`). If `state` is set, it must match -
    so no foreign or stale redirect can be accepted.
    """
    if not url or "://" not in url:
        return None
    scheme, rest = url.split("://", 1)
    if scheme.lower() not in {d["scheme"] for d in BRANDS.values()}:
        return None
    query = rest.split("?", 1)[1] if "?" in rest else ""
    params = urllib.parse.parse_qs(query)
    code = (params.get("code") or [None])[0]
    if not code:
        return None
    if state and (params.get("state") or [""])[0] != state:
        return None
    return code


MAX_CONSENT_KLICKS = 4          # click budget on the consent page
CONSENT_KLICKFRIST = 4.0        # seconds between two clicks
MAX_LOGIN_VERSUCHE = 2          # Gigya and ForgeRock form (same credentials)
LOGIN_FRIST = 5.0               # seconds between two submit attempts


def login_erlaubt(login_urls, url, login_age: float = 99.0) -> bool:
    """May the form be submitted on this page?

    The chain has two login forms with the same credentials: the Gigya page
    (OPLoginPage.php) and the ForgeRock form (idpcvs/am/XUI). A one-time flag
    prevented the second login - the flow then ran into the timeout
    (run of 2026-10-01, 09:59). Now: once per page, at most MAX_LOGIN_VERSUCHE
    times, with LOGIN_FRIST seconds in between.
    """
    if len(login_urls or []) >= MAX_LOGIN_VERSUCHE:
        return False
    if url and url in (login_urls or []):
        return False
    return login_age >= LOGIN_FRIST


def decide_action(snapshot: dict) -> str:
    """What is to be done on the page? 'done' | 'login' | 'consent' | 'wait'.

    Consent pages are often two-step (checkbox + button) or change their content
    after the first click. Therefore action is taken there **repeatedly**: up to
    MAX_CONSENT_KLICKS attempts, with CONSENT_KLICKFRIST seconds of quiet in between.
    Previously 'wait' applied permanently after the first click - if the page stalled,
    the helper ran until the timeout (run of 2026-10-01, 08:28).
    """
    url = (snapshot.get("url") or "").lower()
    if any(url.startswith(d["scheme"].lower() + "://") for d in BRANDS.values()):
        return "done"
    if snapshot.get("code"):
        return "done"
    if snapshot.get("password_inputs", 0) > 0:
        if snapshot.get("login_urls") is None:      # caller without URL info (legacy path)
            return "login" if not snapshot.get("login_submitted") else "wait"
        if login_erlaubt(snapshot.get("login_urls"), snapshot.get("url"),
                         snapshot.get("login_age", 99.0)):
            return "login"
        return "wait"
    if snapshot.get("consent_page") or snapshot.get("consent_buttons", 0) > 0:
        if snapshot.get("consent_clicks", 0) >= MAX_CONSENT_KLICKS:
            return "wait"                      # budget exhausted - do not click endlessly
        if snapshot.get("consent_age", 99.0) < CONSENT_KLICKFRIST:
            return "wait"                      # just clicked: let the page work
        return "consent"
    return "wait"


def next_consent_action(clicks: int) -> str:
    """How the consent page is operated.

    `clicks` = attempts already made. The first attempt is the mere button press;
    from the second on, open checkboxes are additionally checked (many PSA pages
    require both).
    """
    return "checkbox_then_button" if clicks >= 1 else "button"


def health_hint(snapshot: dict) -> str:
    """Short interpretation of the state for the log."""
    url = snapshot.get("url") or ""
    if "am/XUI" in url or "/am/UI/Login" in url:
        return "ForgeRock intermediate page (idpcvs) - redirect to the login still pending"
    if "BrandIDproxyPage" in url:
        return "Gigya pre-page (BrandIDproxyPage) - waiting for redirect to OPLoginPage.php"
    if "OPLoginPage" in url:
        return "Gigya login form loaded"
    if "consent" in url.lower() or snapshot.get("consent_page"):
        labels = ", ".join(snapshot.get("consent_labels") or [])
        return ("consent page from PSA - button is being pressed"
                + (f" [{labels}]" if labels else
                   f" (no button detected; visible: {', '.join(snapshot.get('buttons') or [])[:120]})"))
    if "id-dcr." in url:
        return "OIDC redirect (id-dcr) in progress"
    if snapshot.get("password_inputs"):
        return "login form detected"
    return "no known intermediate stage"


def exchange_tokens(brand: str, country: str, code: str, verifier: str,
                    timeout: int = 30) -> dict:
    data = BRANDS[brand]
    redirect = f"{data['scheme']}://oauth2redirect/{country.lower()}"
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect,
    }).encode()
    auth = base64.b64encode(f"{data['clientid']}:{data['clientsecret']}".encode()).decode()
    req = urllib.request.Request(f"{idp_base(brand)}/am/oauth2/access_token",
                                 data=body, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"token exchange failed (HTTP {exc.code}): "
            f"{exc.read().decode('utf-8', 'replace')[:300]}"
        ) from exc


# ------------------------------------------------------- browser (Qt only)

SNAPSHOT_JS = r"""
(function () {
  var words = %(words)s;
  function visible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  }
  function text(el) { return (el.innerText || el.value || '').trim().slice(0, 60); }
  var pw = 0, txt = 0;
  document.querySelectorAll('input[type=password]').forEach(function (i) { if (visible(i)) pw++; });
  document.querySelectorAll('input[type=text],input[type=email],input:not([type])').forEach(
      function (i) { if (visible(i)) txt++; });
  var sel = 'button,input[type=submit],input[type=button],a[role=button],[role=button],a.button';
  var buttons = [], consent = [];
  document.querySelectorAll(sel).forEach(function (b) {
    if (!visible(b)) return;
    var label = text(b);
    if (!label) return;
    buttons.push(label);
    var low = label.toLowerCase();
    for (var j = 0; j < words.length; j++) {
      if (low.indexOf(words[j]) !== -1) { consent.push(label); break; }
    }
  });
  // Detect the consent page ONLY from path and title. The query parameters
  // must not count: the Gigya intermediate step (BrandIDproxyPage) carries
  // a base64-encoded `context` that may contain "consent" - that had caused
  // the helper to click on the wrong page (run of 2026-10-01, 09:59).
  var pfadTitel = location.pathname + ' ' + document.title;
  var istConsentSeite = /consent|zustimm/i.test(pfadTitel) && !/gigya|OPLoginPage|XUI/i.test(pfadTitel);
  // If the interface is inside an iframe, that is the decisive hint
  var frames = Array.from(document.querySelectorAll('iframe')).map(function (f) {
    return (f.src || f.getAttribute('data-src') || '(no src)').slice(0, 120);
  });
  return JSON.stringify({
    url: location.href,
    title: document.title,
    password_inputs: pw,
    text_inputs: txt,
    consent_page: istConsentSeite,
    consent_buttons: consent.length,
    consent_labels: consent.slice(0, 8),
    checkboxes_unchecked: Array.from(document.querySelectorAll('input[type=checkbox]'))
        .filter(function (c) { return visible(c) && !c.checked; }).length,
    checkbox_details: Array.from(document.querySelectorAll('input[type=checkbox]'))
        .filter(visible).map(function (c) {
          var label = '';
          var um = c.closest('label');
          if (um) { label = (um.innerText || '').trim(); }
          if (!label && c.id) {
            var lab = document.querySelector('label[for="' + c.id + '"]');
            if (lab) { label = (lab.innerText || '').trim(); }
          }
          if (!label) { label = (c.getAttribute('aria-label') || '').trim(); }
          return {id: c.id || '', name: c.name || '', an: c.checked,
                  label: label.slice(0, 90)};
        }).slice(0, 6),
    iframe_count: frames.length,
    iframes: frames.slice(0, 6),
    buttons: buttons.filter(function (b) { return b; }).slice(0, 12)
  });
})()
"""

FILL_JS = r"""
(function () {
  var user = %(user)s, pass = %(pass)s;
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  var pw = Array.from(document.querySelectorAll('input[type=password]')).filter(visible).pop();
  var fields = Array.from(document.querySelectorAll(
      'input[type=text],input[type=email],input[type=tel],input:not([type])')).filter(visible);
  var user_field = fields.filter(function (i) { return i !== pw; })[0];
  if (!pw || !user_field) return 'no login fields';
  function set(el, value) {
    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(el, value);
    ['input', 'change', 'blur'].forEach(function (ev) {
      el.dispatchEvent(new Event(ev, {bubbles: true}));
    });
  }
  set(user_field, user);
  set(pw, pass);
  var form = pw.form || user_field.form;
  var submit = (form && form.querySelector('button[type=submit],input[type=submit]')) ||
               document.querySelector('button[type=submit],input[type=submit]') ||
               document.querySelector('button');
  if (submit) { submit.click(); return 'form submitted'; }
  if (form) { form.submit(); return 'form submitted via submit()'; }
  return 'no submit element found';
})()
"""

TERMS_JS = r"""
(function () {
  // Many login forms require consent to terms. Only things that
  // unambiguously look like it (terms/conditions/privacy/consent) are checked - a
  // newsletter checkbox etc. is left untouched.
  var muster = new RegExp(%(muster)s);
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  }
  var gesetzt = [];
  Array.from(document.querySelectorAll('input[type=checkbox]'))
    .filter(function (c) { return visible(c) && !c.checked && !c.disabled; })
    .forEach(function (c) {
      var text = '';
      var um = c.closest('label');
      if (um) { text = um.innerText || ''; }
      if (!text && c.id) {
        var lab = document.querySelector('label[for="' + c.id + '"]');
        if (lab) { text = lab.innerText || ''; }
      }
      text = (text + ' ' + (c.name || '') + ' ' + (c.id || '') + ' ' +
              (c.getAttribute('aria-label') || '')).toLowerCase();
      if (!muster.test(text)) { return; }
      c.click();
      ['input', 'change'].forEach(function (ev) { c.dispatchEvent(new Event(ev, {bubbles: true})); });
      gesetzt.push((text.replace(/\s+/g, ' ').trim()).slice(0, 60));
    });
  return gesetzt.length ? ('checked: ' + gesetzt.join(' | ')) : 'no consent checkbox open';
})()
"""


KLICK_HELFER_JS = r"""
function klick(el) {
  // Some pages only react to real mouse events or the handler sits on the
  // parent element - so walk up and send the full event chain.
  var ziel = el;
  for (var i = 0; i < 4 && ziel; i++) {
    if (ziel.tagName === 'BUTTON' || ziel.tagName === 'INPUT' || ziel.tagName === 'A' ||
        ziel.getAttribute('role') === 'button' || ziel.onclick) break;
    ziel = ziel.parentElement;
  }
  var r = ziel.getBoundingClientRect();
  var opt = {bubbles: true, cancelable: true, view: window,
             clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
  ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(function (t) {
    ziel.dispatchEvent(new MouseEvent(t, opt));
  });
  if (ziel.tagName === 'BUTTON' && ziel.type === 'submit' && ziel.form) {
    try { ziel.form.submit(); } catch (e) {}
  }
  return ziel;
}
"""

CONSENT_JS = r"""
(function () {
  var words = %(words)s;
  %(klick)s
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  }
  var sel = 'button,input[type=submit],input[type=button],[role=button],a.button,a[role=button]';
  var candidates = Array.from(document.querySelectorAll(sel)).filter(visible);
  var gesehen = [];
  for (var i = 0; i < candidates.length; i++) {
    var label = ((candidates[i].innerText || candidates[i].value ||
                  candidates[i].getAttribute('aria-label') || '') + '').toLowerCase().trim();
    if (label) gesehen.push(label.slice(0, 30));
    for (var j = 0; j < words.length; j++) {
      if (label === words[j] || label.indexOf(words[j]) !== -1) {
        klick(candidates[i]);
        return 'clicked: ' + (label || '(no label)').slice(0, 40);
      }
    }
  }
  return 'no consent button found; visible: ' + gesehen.slice(0, 6).join(' | ');
})()
"""


CHECKBOX_JS = r"""
(function () {
  // Second attempt on the consent page: check open required checkboxes and
  // then press the button again (many PSA pages require both).
  %(klick)s
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  }
  var boxes = Array.from(document.querySelectorAll('input[type=checkbox]'))
      .filter(function (c) { return visible(c) && !c.checked && !c.disabled; });
  boxes.forEach(function (c) {
    c.click();
    ['input', 'change'].forEach(function (ev) { c.dispatchEvent(new Event(ev, {bubbles: true})); });
  });
  var sel = 'button,input[type=submit],input[type=button],[role=button]';
  var words = %(words)s;
  var cands = Array.from(document.querySelectorAll(sel)).filter(visible);
  for (var i = 0; i < cands.length; i++) {
    var label = ((cands[i].innerText || cands[i].value || '') + '').toLowerCase().trim();
    for (var j = 0; j < words.length; j++) {
      if (label.indexOf(words[j]) !== -1) {
        klick(cands[i]);
        return 'checkbox set (' + boxes.length + '), clicked: ' + label.slice(0, 40);
      }
    }
  }
  return 'checkbox set (' + boxes.length + '), no button found';
})()
"""



# ------------------------------------------------------------- browser backends
#
# The flow (poll the page, decide what to do, act) is shared - see flow_cycle() and
# flow_driver() below. Only the browser itself is pluggable, because the two engines are
# not interchangeable:
#
#   playwright  Default. Chromium out of the Playwright package, headless, and available
#               for amd64 AND aarch64 (PyQtWebEngine has no Linux aarch64 wheels, which is
#               why the add-on could only be built for amd64 before).
#               Chromium refuses a navigation to the brand scheme (mymap:// ...): the
#               request ends as net::ERR_ABORTED and the document is never committed
#               (page.url() stays "about:blank", page.goto() raises). The URL of that
#               request is still delivered - as a "request" and as a "requestfailed"
#               event - and that is what carries the authorization code.
#               IMPORTANT: do NOT register a route() handler on this context. With an
#               active route the brand scheme URL is not reported at all (only the last
#               http hop is), and route() never sees the scheme itself
#               (microsoft/playwright#11014, measured 2026-10-15).
#   qt          Legacy PyQt5/QtWebEngine path (only present in the amd64 image).
#               Registers the brand schemes and reads the URL from a request interceptor.
#
# Both backends implement the same methods: start(), evaluate() (blocking page snapshot),
# run_script() (perform an action, wait for it, log the result), code(), requests(),
# skipped_codes(), pause() (let the page work and dispatch pending events) and close().

POLL_SECONDS = 0.7              # polling interval (unchanged)
JS_TIMEOUT = 5.0                # seconds to wait for one JavaScript result
LAUNCH_TIMEOUT_MS = 60000       # navigation/launch timeout

# Chrome refuses to run its sandbox inside an add-on container and /dev/shm is small there
PW_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"]


class BrowserBackend:
    """Interface of the two browser engines (see the module comment above)."""

    name = "abstract"

    def start(self, url: str) -> None:
        raise NotImplementedError

    def evaluate(self, script: str):
        """Run JavaScript in the current page and return its value (None on failure)."""
        raise NotImplementedError

    def run_script(self, script: str, log_line: str, log) -> None:
        """Run JavaScript as an action and write the result to the action log."""
        raise NotImplementedError

    def code(self) -> str | None:
        """The authorization code intercepted at the brand scheme, if any."""
        raise NotImplementedError

    def requests(self) -> list[str]:
        """URLs seen so far (diagnosis)."""
        raise NotImplementedError

    def skipped_codes(self) -> list[str]:
        """Intermediate URLs that carried a `code` but were deliberately not used."""
        raise NotImplementedError

    def pause(self, seconds: float) -> None:
        """Wait, while letting the browser and its pending events run."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # --- shared helper -----------------------------------------------------------
    def _note(self, url: str) -> None:
        """Record one request URL and look for the authorization code in it.

        Only the brand scheme counts (extract_code), and a code is used only when the
        `state` matches: intermediate hops of the chain (Gigya/OIDC, id-dcr ...) carry a
        `code=` of their own, and grabbing that one makes the token exchange fail while
        looking like a successful login.
        """
        if not url:
            return
        seen = self._requests
        if not seen or seen[-1] != url:
            seen.append(url[:300])
        found = extract_code(url, self.oauth_state)
        if found and not self._code:
            self._code = found
            print(f"[psa] authorization code intercepted at the brand scheme "
                  f"({len(found)} characters, state matches)", flush=True)
        elif "code=" in url and not self._code:
            # intermediate step with a foreign code - deliberately NOT to be used
            self._skipped.append(url[:200])


class PlaywrightBackend(BrowserBackend):
    """Chromium via Playwright, headless - the default on amd64 and aarch64."""

    name = "playwright"

    def __init__(self, oauth_state: str):
        self.oauth_state = oauth_state
        self._code = None
        self._requests: list[str] = []
        self._skipped: list[str] = []
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # --- lifecycle --------------------------------------------------------------
    def _user_agent(self) -> str | None:
        """A normal Chrome UA instead of `HeadlessChrome/...`.

        The headless shell announces itself in the user agent, and `navigator.webdriver`
        is true; both are classic bot markers in front of a Gigya/ForgeRock login. The
        real Chrome version of the installed browser is kept, only the "Headless" part is
        dropped. `PSA_BROWSER_UA=keep` disables the override (diagnosis).
        """
        if (os.environ.get("PSA_BROWSER_UA") or "").strip().lower() in ("keep", "none", "0"):
            return None
        version = (getattr(self._browser, "version", "") or "").split(".")[0] or "140"
        machine = platform.machine()
        arch = "aarch64" if machine in ("aarch64", "arm64") else "x86_64"
        return (f"Mozilla/5.0 (X11; Linux {arch}) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version}.0.0.0 Safari/537.36")

    def start(self, url: str) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=list(PW_LAUNCH_ARGS))
        self._context = self._browser.new_context(
            user_agent=self._user_agent(),
            viewport={"width": 1024, "height": 768},
        )
        # navigator.webdriver is set by the automation protocol; hide it like the UA.
        if self._user_agent():
            self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        self._page = self._context.new_page()
        # No route handler here - deliberately, see the module comment.
        self._page.on("request", lambda req: self._note(self._url_of(req)))
        self._page.on("requestfailed", lambda req: self._note(self._url_of(req)))
        print(f"[psa] playwright backend: {self._browser.version} "
              f"({self._browser.browser_type.name}), headless", flush=True)
        self._goto(url)

    @staticmethod
    def _url_of(request) -> str:
        try:
            return request.url
        except Exception:  # noqa: BLE001
            return ""

    def _goto(self, url: str) -> None:
        """Navigate. A failure here is normal: the last hop is the brand scheme.

        Chromium cannot open `mymap://...` and answers with net::ERR_ABORTED while the
        document is never created - the code is already in the request events at that
        point. Retrying or aborting on this error would break working logins.
        """
        try:
            self._page.goto(url, wait_until="commit", timeout=LAUNCH_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            print(f"[psa] navigation ended early ({type(exc).__name__}) - expected when "
                  f"the chain ends at the brand scheme", flush=True)

    # --- interface --------------------------------------------------------------
    def evaluate(self, script: str):
        try:
            return self._page.evaluate(script)
        except Exception:  # noqa: BLE001
            # "Execution context was destroyed" while a navigation is running is normal
            # here; the caller decides on the next poll.
            return None

    def run_script(self, script: str, log_line: str, log) -> None:
        result = self.evaluate(script)
        log(f"{log_line}: {result}")

    def code(self) -> str | None:
        return self._code

    def requests(self) -> list[str]:
        return list(self._requests)

    def skipped_codes(self) -> list[str]:
        return list(self._skipped)

    def pause(self, seconds: float) -> None:
        """Wait via the page: that keeps the event handlers firing.

        A plain time.sleep() would hold back the `request` events (they are dispatched
        while the API is being used), and the authorization code would only arrive after
        the deadline.
        """
        try:
            self._page.wait_for_timeout(int(seconds * 1000))
        except Exception:  # noqa: BLE001
            time.sleep(seconds)

    def close(self) -> None:
        for closer in (getattr(self._context, "close", None),
                       getattr(self._browser, "close", None),
                       getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:  # noqa: BLE001
                pass


class QtBackend(BrowserBackend):
    """Legacy PyQt5/QtWebEngine path (amd64 image only).

    Uses a virtual display (Xvfb, started by run.sh) and a request interceptor. The Qt
    event loop is pumped by hand instead of app.exec_(): the shared driver loop polls the
    page every POLL_SECONDS, and that pumping is what dispatches the JavaScript callbacks.
    """

    name = "qt"

    def __init__(self, oauth_state: str):
        self.oauth_state = oauth_state
        self._code = None
        self._requests: list[str] = []
        self._skipped: list[str] = []

    def start(self, url: str) -> None:
        from PyQt5.QtCore import QByteArray, QEventLoop, QTimer, QUrl
        from PyQt5.QtWebEngineCore import (QWebEngineUrlRequestInterceptor,
                                           QWebEngineUrlScheme, QWebEngineUrlSchemeHandler)
        from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView
        from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

        for brand in BRANDS.values():
            QWebEngineUrlScheme.registerScheme(
                QWebEngineUrlScheme(QByteArray(brand["scheme"].encode())))

        backend = self

        class DummyScheme(QWebEngineUrlSchemeHandler):
            def requestStarted(self, request):  # noqa: N802
                pass

        class Interceptor(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):  # noqa: N802
                backend._note(info.requestUrl().toString())

        class Page(QWebEnginePage):
            def javaScriptConsoleMessage(self, level, msg, line, source):  # noqa: N802, D401
                pass

        class Window(QMainWindow):
            def __init__(self):
                super().__init__()
                self.setWindowTitle("PSA Browser Token")
                self.setGeometry(50, 50, 1024, 768)
                central = QWidget()
                self.setCentralWidget(central)
                layout = QVBoxLayout()
                central.setLayout(layout)
                self.browser = QWebEngineView()
                self.browser.setPage(Page(self.browser))
                layout.addWidget(self.browser)
                self.browser.page().profile().setUrlRequestInterceptor(interceptor)
                for brand in BRANDS.values():
                    self.browser.page().profile().installUrlSchemeHandler(
                        QByteArray(brand["scheme"].encode()), DummyScheme())

        self._QEventLoop = QEventLoop
        self._QTimer = QTimer
        self._QUrl = QUrl
        self._app = QApplication([sys.argv[0]])
        interceptor = Interceptor()
        self._window = Window()
        self._window.show()
        self._page = self._window.browser.page()
        print("[psa] qt backend (PyQt5/QtWebEngine, amd64 only)", flush=True)
        self._page.load(QUrl(url))

    # --- interface --------------------------------------------------------------
    def _pump(self, seconds: float) -> None:
        """Let the Qt event loop run for a while (dispatches pending callbacks)."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._app.processEvents(self._QEventLoop.AllEvents, 50)
            time.sleep(0.02)

    def evaluate(self, script: str):
        """Run JavaScript and wait for its result by pumping the event loop."""
        box: dict = {}

        def callback(value) -> None:
            box["value"] = value

        self._page.runJavaScript(script, callback)
        deadline = time.time() + JS_TIMEOUT
        while "value" not in box and time.time() < deadline:
            self._app.processEvents(self._QEventLoop.AllEvents, 50)
            time.sleep(0.02)
        return box.get("value")

    def run_script(self, script: str, log_line: str, log) -> None:
        log(f"{log_line}: {self.evaluate(script)}")

    def code(self) -> str | None:
        return self._code

    def requests(self) -> list[str]:
        return list(self._requests)

    def skipped_codes(self) -> list[str]:
        return list(self._skipped)

    def pause(self, seconds: float) -> None:
        self._pump(seconds)

    def close(self) -> None:
        try:
            self._window.close()
        except Exception:  # noqa: BLE001
            pass


BACKENDS = {"playwright": PlaywrightBackend, "qt": QtBackend}


def have_playwright() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def have_qt() -> bool:
    try:
        import PyQt5.QtWebEngineWidgets  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def choose_backend(choice: str, playwright_available: bool, qt_available: bool) -> str:
    """Resolve `auto` | `playwright` | `qt` to the backend actually used (pure logic).

    `auto` prefers Playwright: it is the backend that exists on both architectures, and
    the add-on image ships it everywhere. Qt stays as the fallback for an amd64 install
    where the Playwright browser is missing.
    """
    choice = (choice or "auto").strip().lower()
    if choice in ("", "auto"):
        if playwright_available:
            return "playwright"
        if qt_available:
            return "qt"
        raise RuntimeError("no browser backend available: neither playwright nor PyQt5 is "
                           "installed")
    if choice not in BACKENDS:
        raise RuntimeError(f"unknown browser_backend '{choice}' "
                           f"(allowed: auto, {', '.join(sorted(BACKENDS))})")
    if choice == "playwright" and not playwright_available:
        raise RuntimeError("browser_backend 'playwright' is configured but the Playwright "
                           "browser is missing in this image")
    if choice == "qt" and not qt_available:
        raise RuntimeError("browser_backend 'qt' is configured but PyQt5/QtWebEngine is "
                           "missing (the qt backend exists only in the amd64 image)")
    return choice


def make_backend(choice: str, oauth_state: str) -> BrowserBackend:
    resolved = choose_backend(choice, have_playwright(), have_qt())
    print(f"[psa] browser backend: {resolved} "
          f"(configured: {choice or 'auto'})", flush=True)
    return BACKENDS[resolved](oauth_state)


# ------------------------------------------------------------------ the shared flow

def snapshot_script() -> str:
    return SNAPSHOT_JS % {"words": json.dumps(list(CONSENT_WORDS))}


def flow_snapshot(state: dict, raw) -> dict:
    """Take one page snapshot into the state (was: handle_snapshot)."""
    try:
        snap = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        snap = {}
    snap["code"] = bool(state["code"])
    snap["login_submitted"] = state["login_submitted"]
    snap["consent_clicked"] = state["consent_clicked"]
    snap["consent_clicks"] = state["consent_clicks"]
    snap["consent_age"] = (time.time() - state["consent_last_ts"]
                           if state["consent_last_ts"] else 99.0)
    snap["login_urls"] = list(state["login_urls"])
    snap["login_age"] = (time.time() - state["login_last_ts"]
                         if state["login_last_ts"] else 99.0)
    state["snapshot"] = snap
    url = snap.get("url", "")
    if url and (not state["history"] or state["history"][-1] != url):
        state["history"].append(url)
        print(f"[psa] page: {url[:150]} | {health_hint(snap)}", flush=True)
    if "failedlogin" in url.lower() and url not in state["server_fehler"]:
        state["server_fehler"].append(url[:200])
        print("[psa] WARNING: login rejected by the server "
              f"(#failedLogin) at {url[:80]}", flush=True)
    if url:
        seite = state["seiten"].setdefault(url[:160], {"erstmals": zeit()})
        seite["titel"] = snap.get("title")
        seite["knopf_labels"] = snap.get("buttons")
        seite["zustimmung_labels"] = snap.get("consent_labels")
        seite["kaestchen_offen"] = snap.get("checkboxes_unchecked")
        seite["iframes"] = snap.get("iframes")
        seite["letzte_sichtung"] = zeit()
        seite["aktionen"] = state["action_log"][-4:]
        seite["kaestchen"] = snap.get("checkbox_details")
    return snap


def flow_step(backend: BrowserBackend, state: dict, snap: dict, args, log) -> str:
    """Decide what this page needs and do it (was: the body of tick())."""
    action = decide_action(snap)
    url = snap.get("url", "")
    if action == "login":
        state["login_submitted"] = True
        if url and url not in state["login_urls"]:
            state["login_urls"].append(url)
        state["login_last_ts"] = time.time()
        versuch = len(state["login_urls"])
        print(f"[psa] login form: submit attempt {versuch} at {url[:90]}", flush=True)
        # First check consent to terms, then submit: some forms require both, otherwise
        # the login stays incomplete.
        kaestchen = TERMS_JS % {"muster": json.dumps("|".join(TERMS_MUSTER))}
        backend.run_script(kaestchen, f"Terms {versuch} at {url[:60]}", log)
        script2 = FILL_JS % {"user": json.dumps(args.username),
                             "pass": json.dumps(args.password)}
        backend.run_script(script2, f"Login {versuch} at {url[:60]}", log)
    elif action == "consent":
        versuch = state["consent_clicks"] + 1
        wie = next_consent_action(state["consent_clicks"])
        state["consent_clicked"] = True
        state["consent_clicks"] = versuch
        state["consent_last_ts"] = time.time()
        template = CHECKBOX_JS if wie == "checkbox_then_button" else CONSENT_JS
        script = template % {"words": json.dumps(list(CONSENT_WORDS)),
                             "klick": KLICK_HELFER_JS}
        print(f"[psa] consent page: attempt {versuch} ({wie})", flush=True)
        backend.run_script(script, f"Consent {versuch} ({wie}) at {url[:60]}", log)
    return action


def flow_cycle(backend: BrowserBackend, state: dict, args, log):
    """One polling step of the shared driver loop.

    Returns None to keep going, ("finish", 0) when the code is in, or ("dump", reason).
    """
    if backend.code() and not state["code"]:
        state["code"] = backend.code()
    if state["code"]:
        return ("finish", 0)
    elapsed = time.time() - state["started"]
    if elapsed > args.deadline:
        return ("dump", "timeout")
    snap = flow_snapshot(state, backend.evaluate(snapshot_script()))
    flow_step(backend, state, snap, args, log)
    if state["probe"] and elapsed > args.probe_wait and snap.get("url"):
        return ("dump", "probe finished")
    return None


def flow_driver(backend: BrowserBackend, state: dict, args, log) -> int:
    """Poll until the code is there, the deadline hits or the probe is done."""
    while True:
        verdict = flow_cycle(backend, state, args, log)
        if verdict:
            if verdict[0] == "finish":
                return verdict[1]
            state["dump_reason"] = verdict[1]
            dump(state, verdict[1])
            return 3
        backend.pause(POLL_SECONDS)


def run_browser(args) -> int:
    state = {
        "brand": args.brand,
        "code": None,
        "history": [],
        "action_log": [],
        "login_submitted": False,
        "consent_clicked": False,
        "consent_clicks": 0,
        "consent_last_ts": 0.0,
        "login_urls": [],
        "login_last_ts": 0.0,
        "konsent_ergebnis": [],
        "server_fehler": [],
        "seiten": {},
        "started": time.time(),
        "snapshot": {},
        "requests": [],
        "skipped_codes": [],
        "probe": args.probe,
        "backend_configured": getattr(args, "backend", "auto"),
        "backend_used": None,
        "dump_reason": None,
        "debug_path": os.path.join(os.environ.get("DATA_DIR", "/data"), "psa-login-debug.json"),
    }
    verifier, challenge = generate_pkce()
    oauth_state = new_state()
    state["verifier"] = verifier
    state["oauth_state"] = oauth_state
    authorize_url = build_authorize_url(args.brand, args.country, challenge, oauth_state)

    def log(line: str) -> None:
        state["action_log"].append(line)

    try:
        backend = make_backend(getattr(args, "backend", "auto"), oauth_state)
    except RuntimeError as exc:
        print(f"[psa] FEHLER {exc}", file=sys.stderr, flush=True)
        return 1
    state["backend_used"] = backend.name

    print(f"[psa] starting {backend.name} browser for {args.brand} ...", flush=True)
    print(f"[psa] authorize URL: {authorize_url[:120]}...", flush=True)
    try:
        backend.start(authorize_url)
        flow_driver(backend, state, args, log)
    finally:
        # the diagnosis has to be complete even if the driver raised
        state["requests"] = backend.requests()
        state["skipped_codes"] = backend.skipped_codes()
        if backend.code() and not state["code"]:
            state["code"] = backend.code()
        backend.close()

    if state["code"]:
        print("[psa] exchanging code for tokens ...", flush=True)
        try:
            tokens = exchange_tokens(args.brand, args.country, state["code"], state["verifier"])
        except Exception as exc:  # noqa: BLE001
            dump(state, f"token exchange failed: {exc}")
            print(f"[psa] ERROR token exchange: {exc} "
                  f"| code length {len(state['code'])}, intermediate steps with foreign "
                  f"code skipped: {len(state['skipped_codes'])}", file=sys.stderr, flush=True)
            return 1
        print(json.dumps(tokens, indent=4))
        return 0
    if state["probe"]:
        # a probe ends with a dump; only a deadline is an error (exit 3, as before)
        return 3 if state.get("dump_reason") == "timeout" else 0
    return 3


def dump(state: dict, reason: str) -> None:
    payload = {
        "zeitpunkt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "grund": reason,
        "marke": state.get("brand"),
        "backend_configured": state.get("backend_configured"),
        "backend_used": state.get("backend_used"),
        "dauer_s": round(time.time() - state["started"], 1),
        "url_verlauf": state["history"][-12:],
        "request_verlauf": state["requests"][-25:],
        "uebersprungene_codes": state["skipped_codes"][:6],
        "zustimmung": {"klicks": state["consent_clicks"],
                       "labels": state["snapshot"].get("consent_labels"),
                       "kaestchen_offen": state["snapshot"].get("checkboxes_unchecked"),
                       "iframes": state["snapshot"].get("iframes")},
        "seiten": state["seiten"],
        "aktionen": state["action_log"][-12:],
        "login_seiten": state["login_urls"],
        "kaestchen_ergebnis": state["konsent_ergebnis"][-6:],
        "server_fehler": state["server_fehler"][:4],
        "letzter_zustand": state["snapshot"],
        "aktionen": state["action_log"][-12:],
        "deutung": health_hint(state["snapshot"]),
    }
    try:
        os.makedirs(os.path.dirname(state["debug_path"]), exist_ok=True)
        with open(state["debug_path"], "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"[psa] diagnosis: {state['debug_path']}", flush=True)
    except OSError as exc:
        print(f"[psa] diagnosis could not be written: {exc}", file=sys.stderr)
    print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)


REQUIRED_ARG_ATTRS = ("brand", "country", "username", "password", "deadline",
                      "probe", "probe_wait", "backend")


def build_arg_parser() -> "argparse.ArgumentParser":
    parser = argparse.ArgumentParser(description="Fetch PSA tokens via browser")
    parser.add_argument("-b", "--brand", choices=sorted(BRANDS), required=True)
    parser.add_argument("-u", "--username")
    parser.add_argument("-p", "--password")
    parser.add_argument("-c", "--countrycode", dest="country", default="de",
                        help="country code for the redirect URI (default de)")
    parser.add_argument("-t", "--timeout", type=int, default=30,
                        help="time per step (compatibility with the old version only)")
    parser.add_argument("--deadline", type=int, default=240,
                        help="overall deadline in seconds (default 240)")
    parser.add_argument("--probe", action="store_true",
                        help="without credentials, only log the page state")
    parser.add_argument("--probe-wait", type=int, default=45,
                        help="wait time for --probe in seconds")
    parser.add_argument("--backend", choices=("auto", "playwright", "qt"),
                        default=(os.environ.get("PSA_BROWSER") or "auto"),
                        help="browser engine: playwright (default, amd64+aarch64), "
                             "qt (legacy fallback, amd64 only) or auto")
    return parser


def check_args(args) -> None:
    """Ensures that all attributes used in the flow exist.

    This is exactly where a bug lay: the script used args.country, but the option was
    named --countrycode (attribute args.countrycode) -> AttributeError only at runtime.
    """
    missing = [name for name in REQUIRED_ARG_ATTRS if not hasattr(args, name)]
    if missing:
        raise SystemExit(f"internal error: missing arguments: {', '.join(missing)} "
                         f"(present: {', '.join(sorted(vars(args)))} )")


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    check_args(args)

    if args.probe:
        args.username = args.username or ""
        args.password = args.password or ""
    elif not args.username or not args.password:
        parser.error("--username and --password are required (or --probe)")

    return run_browser(args)


if __name__ == "__main__":
    sys.exit(main())
