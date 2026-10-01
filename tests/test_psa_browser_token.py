#!/usr/bin/env python3
"""Self-test for psa-browser-token.py (without browser, without PSA).

The logic is tested: PKCE, authorize URL, code detection, decision-making
(what is to be done on the page?) and the token exchange against a local fake IdP.

Usage: python3 tests/test_psa_browser_token.py
"""

import ast
import base64
import hashlib
import re
import sys
import importlib.util
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "psa-token-refresh", "psa-browser-token.py")

STATE = {"last": None, "status": 200, "body": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        STATE["last"] = {
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "ctype": self.headers.get("Content-Type", ""),
            "form": urllib.parse.parse_qs(self.rfile.read(length).decode()),
        }
        if STATE["status"] != 200:
            body = (STATE["body"] or '{"error":"invalid_grant"}').encode()
            self.send_response(STATE["status"])
        else:
            body = json.dumps(STATE["body"] or {
                "access_token": "A" * 30, "refresh_token": "R" * 30,
                "token_type": "Bearer", "expires_in": 3599,
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
os.environ["PSA_IDP_BASE"] = f"http://127.0.0.1:{SERVER.server_address[1]}"

spec = importlib.util.spec_from_file_location("psa_helper", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_pkce_shape():
    verifier, challenge = helper.generate_pkce()
    check(43 <= len(verifier) <= 128, f"verifier length: {len(verifier)}")
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode()[:-1]
    check(challenge == expected, "challenge does not match SHA256(verifier)")
    check("=" not in challenge, "challenge contains padding")


def test_authorize_url_carries_state():
    _v, challenge = helper.generate_pkce()
    st = helper.new_state()
    url = helper.build_authorize_url("peugeot", "de", challenge, st)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    check(q.get("state", [None])[0] == st, "state missing in the authorize URL")
    check(len(st) >= 12, f"state too short: {st}")
    ohne = helper.build_authorize_url("peugeot", "de", challenge)
    check("state=" not in ohne, "state was appended without being given")


def test_authorize_url_contains_all_params():
    _v, challenge = helper.generate_pkce()
    url = helper.build_authorize_url("peugeot", "de", challenge)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    check(q["client_id"][0] == helper.BRANDS["peugeot"]["clientid"], "client_id wrong")
    check(q["redirect_uri"][0] == "mymap://oauth2redirect/de", f"redirect_uri: {q['redirect_uri']}")
    check(q["code_challenge"][0] == challenge, "code_challenge missing")
    check(q["code_challenge_method"][0] == "S256", "PKCE method missing")
    check(q["response_type"][0] == "code", "response_type missing")
    base = os.environ.pop("PSA_IDP_BASE")          # check the real IdP address
    try:
        opel = helper.build_authorize_url("opel", "de", challenge)
        check("idpcvs.opel.com" in opel, f"brand not considered: {opel[:60]}")
        check("mymopsdk://oauth2redirect/de" in urllib.parse.unquote(opel), "Opel scheme missing")
    finally:
        os.environ["PSA_IDP_BASE"] = base


def test_extract_code_only_accepts_brand_scheme():
    """Regression: intermediate steps (Gigya/OIDC) also carry a code=.

    If one of those is picked up, the token exchange fails - exactly what happened
    in the log of 30.09.2026. Only the redirect to the brand scheme is valid.
    """
    check(helper.extract_code("mymap://oauth2redirect/de?code=ABC") == "ABC",
          "code from brand scheme not recognized")
    check(helper.extract_code("mymopsdk://oauth2redirect/de?code=OPEL1") == "OPEL1",
          "Opel scheme not recognized")
    # intermediate steps must NOT be accepted
    for url in ("https://id-dcr.peugeot.com/gigya/BrandIDproxyPage.php?mode=afterLogin&code=GIGYA",
                "https://id-dcr.peugeot.com/oidc/op/v1.0/KEY/authorize?code=OIDC&state=x",
                "https://idpcvs.peugeot.com/am/oauth2c/OAuthProxy.jsp?code=PROXY"):
        check(helper.extract_code(url) is None, f"intermediate step accepted: {url[:50]}")
    check(helper.extract_code("https://x.example/cb?code=XYZ") is None,
          "foreign https URL was accepted")
    check(helper.extract_code("") is None, "empty URL returned a code")
    check(helper.extract_code("mymap:") is None, "URL without :// returned a code")


def test_extract_code_checks_state():
    url = "mymap://oauth2redirect/de?code=ABC&state=MYSTATE"
    check(helper.extract_code(url, "MYSTATE") == "ABC", "code with matching state discarded")
    check(helper.extract_code(url, "WRONG") is None, "code with wrong state accepted")
    check(helper.extract_code(url) == "ABC", "without a state value the code must pass")


def test_decide_action_state_machine():
    check(helper.decide_action({"url": "mymap://oauth2redirect/de?code=A"}) == "done",
          "target URL not recognized as done")
    check(helper.decide_action({"url": "https://x", "code": True}) == "done",
          "intercepted code not recognized as done")
    check(helper.decide_action({"url": "https://x", "password_inputs": 1}) == "login",
          "login form not recognized")
    check(helper.decide_action({"url": "https://x", "password_inputs": 1,
                                "login_submitted": True}) == "wait",
          "second login attempt after submitting")
    check(helper.decide_action({"url": "https://x", "consent_buttons": 1}) == "consent",
          "consent page not recognized")
    # consent pages are multi-step: after the first click it may be acted again
    # (previously "wait" applied permanently - so the helper ran into the
    # timeout, run of 01.10.2026, 08:28).
    check(helper.decide_action({"url": "https://x", "consent_buttons": 1,
                                "consent_clicked": True, "consent_clicks": 1,
                                "consent_age": 30}) == "consent",
          "second attempt on the consent page was omitted")
    check(helper.decide_action({"url": "https://x", "consent_buttons": 1,
                                "consent_clicks": 1, "consent_age": 1}) == "wait",
          "click interval not respected")
    check(helper.decide_action({"url": "https://x", "consent_buttons": 1,
                                "consent_clicks": helper.MAX_CONSENT_KLICKS,
                                "consent_age": 30}) == "wait",
          "click budget not limited")
    check(helper.decide_action({"url": "https://idpcvs.peugeot.com/am/XUI/#login/"}) == "wait",
          "intermediate page wrongly counted as an action")


def test_consent_page_is_worked():
    """Regression for the run of 30.09.2026, 18:59: the helper stood for 240 s on
    id-dcr.peugeot.com/index/authorize-consentments because the snapshot did not
    collect the consent buttons (consent_buttons was always missing)."""
    consent_url = ("https://id-dcr.peugeot.com/index/authorize-consentments"
                   "?gotoparam=client_id%3D1eebc2d5-5df3-459b-a624-20abfcf82530")
    snap = {"url": consent_url, "consent_page": True, "consent_buttons": 1,
            "consent_labels": ["Zustimmen und fortfahren"]}
    check(helper.decide_action(snap) == "consent", "consent button not operated")
    check("consent" in helper.health_hint(snap), f"interpretation missing: {helper.health_hint(snap)}")
    check("Zustimmen" in helper.health_hint(snap), "button label missing in the interpretation")
    # page without a recognizable button: name the label so the caption is visible
    hint = helper.health_hint({"url": consent_url, "consent_page": True, "consent_buttons": 0,
                              "buttons": ["Ich stimme zu", "Abbrechen"]})
    check("Ich stimme zu" in hint, f"button labels missing: {hint}")
    # No recognizable button, but the page is marked as a consent page: action is
    # taken anyway (attempt 1 = button, from attempt 2 with checkboxes) - this way a
    # surface whose caption I do not know also makes progress.
    check(helper.decide_action({"url": consent_url, "consent_page": True,
                               "consent_buttons": 0, "consent_age": 99}) == "consent",
          "consent page without a button label is not operated")


def test_login_is_allowed_once_per_page():
    """Regression for the run of 01.10.2026, 09:59: the chain ended on the ForgeRock
    form (idpcvs/am/XUI) because after the Gigya login no second login was allowed."""
    gigya = "https://id-dcr.peugeot.com/gigya/OPLoginPage.php?apiKey=x"
    xui = "https://idpcvs.peugeot.com/am/XUI/#login/&realm=%2FclientsB2CPeugeot"
    check(helper.login_erlaubt([], gigya, 99) is True, "first login not allowed")
    check(helper.login_erlaubt([gigya], xui, 99) is True, "second login on a different page refused")
    check(helper.login_erlaubt([gigya], gigya, 99) is False, "login repeated on the same page")
    check(helper.login_erlaubt([gigya, xui], "https://x/login", 99) is False,
          "login budget not limited")
    check(helper.login_erlaubt([gigya], xui, 1) is False, "login interval not respected")
    # callers without URL info keep the old behavior
    check(helper.decide_action({"url": xui, "password_inputs": 1}) == "login",
          "legacy path without login_urls broken")
    check(helper.decide_action({"url": xui, "password_inputs": 1, "login_submitted": True}) == "wait",
          "legacy path ignores login_submitted")
    check(helper.decide_action({"url": xui, "password_inputs": 1, "login_urls": [],
                                "login_age": 99}) == "login", "first login with URL info refused")
    check(helper.decide_action({"url": xui, "password_inputs": 1, "login_urls": [gigya, xui],
                                "login_age": 99}) == "wait", "budget ignored")


def test_consent_page_detection_ignores_query_parameters():
    """The Gigya intermediate step carries a base64 `context` that contains "consent" -
    it must not turn into a consent page from that."""
    snap_js = helper.SNAPSHOT_JS % {"words": json.dumps(list(helper.CONSENT_WORDS))}
    check("location.pathname" in snap_js, "detection does not use the path")
    check("gigya" in snap_js and "OPLoginPage" in snap_js and "XUI" in snap_js,
          "intermediate steps are not excluded")
    check("location.href + ' ' + document.title" not in snap_js,
          "query parameters still count")


def test_consent_escalates_to_checkboxes():
    check(helper.next_consent_action(0) == "button", "the first attempt must be the button")
    check(helper.next_consent_action(1) == "checkbox_then_button",
          "the second attempt must set the checkboxes")
    check(helper.next_consent_action(5) == "checkbox_then_button", "escalation ends too early")


def test_health_hint_marks_the_new_chain():
    check("ForgeRock" in helper.health_hint({"url": "https://idpcvs.peugeot.com/am/XUI/#login/"}),
          "ForgeRock step not interpreted")
    check("BrandID" in helper.health_hint({"url": "https://id-dcr.peugeot.com/gigya/BrandIDproxyPage.php"}),
          "Gigya front page not interpreted")
    check("login form" in helper.health_hint({"url": "https://x/OPLoginPage.php"})
          or "login" in helper.health_hint({"url": "https://x/OPLoginPage.php"}),
          "OPLoginPage not interpreted")
    check(helper.health_hint({}) != "", "empty state without text")
    check("id-dcr" in helper.health_hint({"url": "https://id-dcr.peugeot.com/oidc/op/x"}),
          "OIDC step not interpreted")


def test_exchange_tokens_success():
    STATE["status"] = 200
    STATE["body"] = None
    tokens = helper.exchange_tokens("peugeot", "de", "CODE1", "VERIFIER1")
    check(tokens["access_token"].startswith("A"), "response not passed through")
    sent = STATE["last"]
    check(sent["path"] == "/am/oauth2/access_token", f"path: {sent['path']}")
    check(sent["form"]["grant_type"] == ["authorization_code"], "grant_type wrong")
    check(sent["form"]["code"] == ["CODE1"], "code missing")
    check(sent["form"]["code_verifier"] == ["VERIFIER1"], "code_verifier missing")
    check(sent["form"]["redirect_uri"] == ["mymap://oauth2redirect/de"], "redirect_uri wrong")
    expected = base64.b64encode(
        f"{helper.BRANDS['peugeot']['clientid']}:{helper.BRANDS['peugeot']['clientsecret']}"
        .encode()).decode()
    check(sent["auth"] == f"Basic {expected}", "basic authentication missing")


def test_exchange_tokens_error_is_readable():
    STATE["status"] = 400
    STATE["body"] = '{"error":"invalid_grant","error_description":"code expired"}'
    try:
        helper.exchange_tokens("peugeot", "de", "CODE1", "VERIFIER1")
    except RuntimeError as exc:
        check("invalid_grant" in str(exc) and "HTTP 400" in str(exc),
              f"message unusable: {exc}")
        return
    finally:
        STATE["status"] = 200
        STATE["body"] = None
    raise AssertionError("HTTP error was not reported as RuntimeError")


def test_orchestrator_arguments_are_complete():
    """Regression: the add-on calls the helper with exactly these arguments.

    Previously the script used args.country, but the option was called --countrycode
    -> AttributeError only during the real run. The test additionally checks statically
    that every args.<name> used in the source exists after parsing.
    """
    parser = helper.build_arg_parser()
    argv = ["-b", "peugeot", "-u", "user@example.org", "-p", "secret",
            "-c", "de", "-t", "45", "--deadline", "240"]
    args = parser.parse_args(argv)
    helper.check_args(args)
    check(args.brand == "peugeot", "brand not applied")
    check(args.country == "de", f"country code wrong: {getattr(args, 'country', 'MISSING')}")
    check(args.deadline == 240, "deadline not applied")
    check(args.username == "user@example.org" and args.password == "secret", "credentials missing")
    check(args.probe is False, "probe unexpectedly active")

    # statically via the syntax tree (no comment/docstring false hits)
    tree = ast.parse(open(HELPER, encoding="utf-8").read())
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "args"}
    fehlend = sorted(used - set(vars(args)))
    check(not fehlend, f"arguments used in the source but unknown: {fehlend}")
    check(len(used) >= 6, f"too few occurrences - test does not bite: {used}")


def test_probe_mode_needs_no_credentials():
    parser = helper.build_arg_parser()
    args = parser.parse_args(["-b", "peugeot", "--probe", "--probe-wait", "30"])
    helper.check_args(args)
    check(args.probe is True and args.probe_wait == 30, "probe options wrong")
    check(args.username is None and args.password is None, "probe needs no credentials")


def test_js_templates_render():
    """The JS templates must be fillable with the expected values."""
    fill = helper.FILL_JS % {"user": json.dumps("a@b.de"), "pass": json.dumps("se cret")}
    check("a@b.de" in fill and "se cret" in fill, "credentials do not end up in the script")
    check("%(" not in fill, f"unfilled placeholder in FILL_JS: {fill[:120]}")
    consent = helper.CONSENT_JS % {"words": json.dumps(list(helper.CONSENT_WORDS)),
                                   "klick": helper.KLICK_HELFER_JS}
    check("akzeptieren" in consent, "consent words missing in the script")
    check("querySelectorAll" in helper.SNAPSHOT_JS, "SNAPSHOT_JS does not look like a DOM query")
    # The actual bug of 30.09.2026: the snapshot never reported consent_buttons,
    # so the consent page could never be operated.
    snap = helper.SNAPSHOT_JS % {"words": json.dumps(list(helper.CONSENT_WORDS))}
    check("%(" not in snap, f"unfilled placeholder in SNAPSHOT_JS: {snap[:120]}")
    check("consent_buttons" in snap, "SNAPSHOT_JS does not report consent_buttons")
    check("consent_labels" in snap, "SNAPSHOT_JS does not report the button labels")
    check("consent_page" in snap, "SNAPSHOT_JS does not recognize the consent page")
    check("zustimmen" in snap, "consent words missing in the snapshot")
    check("iframe" in snap, "SNAPSHOT_JS does not report iframes")
    check("function klick" in consent, "click helper missing in CONSENT_JS")
    check("MouseEvent" in consent, "real mouse events missing on click")
    box = helper.CHECKBOX_JS % {"words": json.dumps(list(helper.CONSENT_WORDS)),
                                "klick": helper.KLICK_HELFER_JS}
    terms = helper.TERMS_JS % {"muster": json.dumps("|".join(helper.TERMS_MUSTER))}
    check("new RegExp" in terms and "%(" not in terms, "TERMS_JS incomplete")
    check("type=checkbox" in terms, "TERMS_JS does not look for checkboxes")
    check("datenschutz" in terms and "zustimm" in terms, "consent words missing in TERMS_JS")
    for drin in ("agb", "bedingungen", "datenschutz", "terms", "zustimm", "akzeptier"):
        check(drin in helper.TERMS_MUSTER, f"{drin} missing in the pattern")
    for draussen in ("newsletter", "werbung", "marketing"):
        check(draussen not in helper.TERMS_MUSTER,
              f"{draussen} must not count as consent")
    check("checkbox_details" in snap, "SNAPSHOT_JS does not report checkbox labels")
    check("type=checkbox" in box and "%(" not in box, "CHECKBOX_JS incomplete")


def test_missing_credentials_are_rejected():
    import subprocess as sp
    proc = sp.run([sys.executable, HELPER, "-b", "peugeot"], capture_output=True, text=True)
    check(proc.returncode != 0, "helper started without credentials")
    check("--username" in (proc.stderr + proc.stdout), f"message unclear: {proc.stderr[-200:]}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
