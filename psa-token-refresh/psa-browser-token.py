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
    (URL history, title, fields, buttons) instead of only reporting "timeout".

Invocation:
  python3 psa-browser-token.py -b peugeot -u user -p pass [-c de] [-t 30]
  python3 psa-browser-token.py -b peugeot --probe        # without credentials

Return codes: 0 = tokens (JSON on stdout), 1 = error, 3 = diagnosis written.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
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


def run_browser(args) -> int:
    from PyQt5.QtCore import QByteArray, QTimer, QUrl
    from PyQt5.QtWebEngineCore import (QWebEngineUrlRequestInterceptor, QWebEngineUrlScheme,
                                       QWebEngineUrlSchemeHandler)
    from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineView
    from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

    for brand in BRANDS.values():
        QWebEngineUrlScheme.registerScheme(QWebEngineUrlScheme(QByteArray(brand["scheme"].encode())))

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
        "debug_path": os.path.join(os.environ.get("DATA_DIR", "/data"), "psa-login-debug.json"),
    }
    verifier, challenge = generate_pkce()
    oauth_state = new_state()
    state["verifier"] = verifier
    state["oauth_state"] = oauth_state
    authorize_url = build_authorize_url(args.brand, args.country, challenge, oauth_state)

    def finish(code: int, message: str = "") -> None:
        if message:
            print(message, file=sys.stderr, flush=True)
        app.exit(code)

    class DummyScheme(QWebEngineUrlSchemeHandler):
        def requestStarted(self, request):  # noqa: N802
            pass

    class Interceptor(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):  # noqa: N802
            url = info.requestUrl().toString()
            if url and (not state["requests"] or state["requests"][-1] != url):
                state["requests"].append(url[:300])
            found = extract_code(url, oauth_state)
            if found and not state["code"]:
                state["code"] = found
                state["history"].append(url)
                print(f"[psa] authorization code intercepted at the brand scheme "
                      f"({len(found)} characters, state matches)", flush=True)
                app.quit()
            elif "code=" in url and not state["code"]:
                # intermediate step with a foreign code - deliberately NOT to be used
                state["skipped_codes"].append(url[:200])

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

    app = QApplication([sys.argv[0]])
    interceptor = Interceptor()
    window = Window()
    window.show()

    def tick() -> None:
        if state["code"]:
            return
        elapsed = time.time() - state["started"]
        if elapsed > args.deadline:
            dump(state, "timeout")
            finish(3, f"[psa] abort after {int(elapsed)} s - diagnosis in {state['debug_path']}")
            return

        def handle_snapshot(raw: str) -> None:
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

            action = decide_action(snap)
            if action == "login":
                state["login_submitted"] = True
                if url and url not in state["login_urls"]:
                    state["login_urls"].append(url)
                state["login_last_ts"] = time.time()
                versuch = len(state["login_urls"])
                print(f"[psa] login form: submit attempt {versuch} at {url[:90]}", flush=True)

                def nach_kaestchen(ergebnis, v=versuch):
                    # First check consent to terms, then submit: some forms
                    # require both, otherwise the login stays incomplete.
                    state["konsent_ergebnis"].append(str(ergebnis))
                    if "checked" in str(ergebnis):
                        print(f"[psa] checkbox: {ergebnis}", flush=True)
                    script2 = FILL_JS % {"user": json.dumps(args.username),
                                         "pass": json.dumps(args.password)}
                    window.browser.page().runJavaScript(script2, lambda r2: state[
                        "action_log"].append(f"Login {v} at {url[:60]}: {r2}"))

                kaestchen = TERMS_JS % {"muster": json.dumps("|".join(TERMS_MUSTER))}
                window.browser.page().runJavaScript(kaestchen, nach_kaestchen)
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
                window.browser.page().runJavaScript(script, lambda r, v=versuch: state[
                    "action_log"].append(f"Consent {v} ({wie}) at {url[:60]}: {r}"))
            if state["probe"] and elapsed > args.probe_wait and snap.get("url"):
                dump(state, "probe finished")
                print("[psa] probe dump written", flush=True)
                finish(0)

        window.browser.page().runJavaScript(SNAPSHOT_JS % {"words": json.dumps(list(CONSENT_WORDS))},
                                            handle_snapshot)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(700)

    print(f"[psa] starting browser for {args.brand} ...", flush=True)
    print(f"[psa] authorize URL: {authorize_url[:120]}...", flush=True)
    window.browser.load(QUrl(authorize_url))
    app.exec_()

    if state["code"]:
        print("[psa] exchanging code for tokens ...", flush=True)
        try:
            tokens = exchange_tokens(args.brand, args.country, state["code"], state["verifier"])
        except Exception as exc:  # noqa: BLE001
            dump(state, f"token exchange failed: {exc}")
            finish(1, f"[psa] ERROR token exchange: {exc} "
                      f"| code length {len(state['code'])}, intermediate steps with foreign "
                      f"code skipped: {len(state['skipped_codes'])}")
            return 1
        print(json.dumps(tokens, indent=4))
        return 0
    if state["probe"]:
        return 0
    return 3


def dump(state: dict, reason: str) -> None:
    payload = {
        "zeitpunkt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "grund": reason,
        "marke": state.get("brand"),
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
                      "probe", "probe_wait")


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
