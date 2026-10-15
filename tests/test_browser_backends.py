#!/usr/bin/env python3
"""Self-test for the pluggable browser backends and the shared flow.

The flow (poll, decide, act) used to live inside run_browser() and was reachable only
with a running PyQtWebEngine - i.e. it had no test coverage at all. It is now shared
between both engines, so it can be driven by a fake backend: no browser, no network, no
Qt, no Playwright. That is what this file does.

The real engine handshake (Playwright/Chromium and the redirect to the brand scheme) is
covered separately in tests/test_playwright_redirect.py, which needs a browser.

Usage: python3 tests/test_browser_backends.py
"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.normpath(os.path.join(HERE, "..", "psa-token-refresh", "psa-browser-token.py"))

spec = importlib.util.spec_from_file_location("psa_helper", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --- snapshots the fake backend hands out ----------------------------------------------
LOGIN_PAGE = {
    "url": "https://idpcvs.peugeot.com/am/XUI/#login",
    "title": "Login",
    "password_inputs": 1,
    "text_inputs": 1,
    "consent_page": False,
    "consent_buttons": 0,
    "checkboxes_unchecked": 1,
    "buttons": ["Sign in"],
}
CONSENT_PAGE = {
    "url": "https://idpcvs.peugeot.com/consent",
    "title": "Consent",
    "password_inputs": 0,
    "text_inputs": 0,
    "consent_page": True,
    "consent_buttons": 1,
    "checkboxes_unchecked": 1,
    "buttons": ["Accept"],
}
WAITING_PAGE = {
    "url": "https://idpcvs.peugeot.com/am/XUI/#wait",
    "title": "please wait",
    "password_inputs": 0,
    "text_inputs": 0,
    "consent_page": False,
    "consent_buttons": 0,
    "checkboxes_unchecked": 0,
    "buttons": [],
}

CODE = "AUTHORIZATIONCODE"
BRAND_URL = "mymap://oauth2redirect/de?code=" + CODE


class FakeBackend(helper.BrowserBackend):
    """Scripted backend: hands out snapshots and can 'intercept' the code."""

    name = "fake"

    def __init__(self, oauth_state, snaps, code_at_call=None, code=CODE):
        self.oauth_state = oauth_state
        self._code = None
        self._requests = []
        self._skipped = []
        self._snaps = list(snaps)
        self._code_at = code_at_call
        self._code_value = code
        self.calls = []
        self.pauses = 0
        self.started_url = None
        self.closed = False

    def _evaluations(self):
        return [c for c in self.calls if c[0] == "evaluate"]

    def start(self, url):
        self.started_url = url
        self.calls.append(("start", url))

    def evaluate(self, script):
        self.calls.append(("evaluate",))
        n = len(self._evaluations())
        if self._code_at and n >= self._code_at:
            # what the engines do: the brand scheme URL shows up in the request events
            self._requests.append(BRAND_URL)
            self._code = self._code_value
        idx = min(n - 1, len(self._snaps) - 1) if self._snaps else 0
        return json.dumps(self._snaps[idx]) if self._snaps else None

    def run_script(self, script, log_line, log):
        self.calls.append(("run_script", log_line))
        log(f"{log_line}: faked")

    def code(self):
        return self._code

    def requests(self):
        return list(self._requests)

    def skipped_codes(self):
        return list(self._skipped)

    def pause(self, seconds):
        self.pauses += 1

    def close(self):
        self.closed = True


def run_helper(argv, backend, tokens=None, data_dir=None):
    """Run run_browser() with the fake backend and a faked token endpoint."""
    parser = helper.build_arg_parser()
    args = parser.parse_args(argv)
    helper.check_args(args)

    seen = {}
    real_make, real_exchange = helper.make_backend, helper.exchange_tokens

    def fake_make(choice, oauth_state):
        seen["choice"] = choice
        return backend

    def fake_exchange(brand, country, code, verifier, timeout=30):
        seen["exchange"] = {"brand": brand, "country": country, "code": code,
                            "verifier": verifier}
        if tokens is None:
            raise RuntimeError("token endpoint said no")
        return tokens

    helper.make_backend, helper.exchange_tokens = fake_make, fake_exchange
    old_data = os.environ.get("DATA_DIR")
    if data_dir:
        os.environ["DATA_DIR"] = data_dir
    old_poll, old_deadline = helper.POLL_SECONDS, None
    try:
        code = helper.run_browser(args)
    finally:
        helper.make_backend, helper.exchange_tokens = real_make, real_exchange
        if old_data is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = old_data
    return code, seen


def dump_of(data_dir):
    path = os.path.join(data_dir, "psa-login-debug.json")
    check(os.path.exists(path), f"no diagnosis written to {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_choose_backend_matrix():
    check(helper.choose_backend("auto", True, True) == "playwright",
          "auto must prefer playwright, not qt")
    check(helper.choose_backend("auto", True, False) == "playwright", "auto/playwright")
    check(helper.choose_backend("auto", False, True) == "qt", "auto must fall back to qt")
    check(helper.choose_backend("", False, True) == "qt", "empty means auto")
    check(helper.choose_backend("qt", True, True) == "qt", "explicit qt stays qt")
    check(helper.choose_backend("playwright", True, False) == "playwright", "explicit pw")
    for choice, pw, qt in (("auto", False, False), ("playwright", False, True),
                           ("qt", True, False)):
        try:
            helper.choose_backend(choice, pw, qt)
            raise AssertionError(f"{choice} (playwright={pw}, qt={qt}) should have failed")
        except RuntimeError as exc:
            check(choice in str(exc) or "backend" in str(exc),
                  f"unhelpful error message: {exc}")
    try:
        helper.choose_backend("chrome", True, True)
        raise AssertionError("an unknown backend must be rejected")
    except RuntimeError as exc:
        check("chrome" in str(exc), f"the error must name the value: {exc}")


def test_flow_logs_in_once_and_exchanges_tokens():
    snaps = [LOGIN_PAGE, LOGIN_PAGE, WAITING_PAGE]
    backend = FakeBackend("st", snaps, code_at_call=2)
    tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3599}
    code, seen = run_helper(["-b", "peugeot", "-u", "u@example.org", "-p", "geheim",
                             "--deadline", "30"], backend, tokens=tokens)
    check(code == 0, f"expected exit 0 with tokens, got {code}")
    check(seen["exchange"]["code"] == CODE, "the intercepted code was not handed over")
    logins = [c[1] for c in backend.calls if c[0] == "run_script" and c[1].startswith("Login")]
    terms = [c[1] for c in backend.calls if c[0] == "run_script" and c[1].startswith("Terms")]
    check(len(logins) == 1, f"the login must be submitted exactly once, was {len(logins)}")
    check(len(terms) == 1, "the terms checkbox must be handled before submitting")
    order = [c[1].split()[0] for c in backend.calls if c[0] == "run_script"]
    check(order[:2] == ["Terms", "Login"], f"terms must come before the login: {order}")
    check(backend.closed, "the backend has to be closed (no orphaned browser)")


def test_consent_page_escalates_to_checkboxes():
    helper.CONSENT_KLICKFRIST = 0.0          # no waiting inside the test
    snaps = [CONSENT_PAGE, CONSENT_PAGE, WAITING_PAGE]
    backend = FakeBackend("st", snaps, code_at_call=3)
    code, _ = run_helper(["-b", "peugeot", "-u", "u", "-p", "p", "--deadline", "30"],
                         backend, tokens={"access_token": "a"})
    check(code == 0, f"expected exit 0, got {code}")
    clicks = [c[1] for c in backend.calls if c[0] == "run_script" and c[1].startswith("Consent")]
    check(len(clicks) == 2, f"the consent page should be worked twice, was {len(clicks)}")
    check(clicks[0].startswith("Consent 1 (button)"), clicks[0])
    check(clicks[1].startswith("Consent 2 (checkbox_then_button)"), clicks[1])


def test_deadline_writes_diagnosis():
    with tempfile.TemporaryDirectory() as tmp:
        backend = FakeBackend("st", [WAITING_PAGE])
        code, _ = run_helper(["-b", "peugeot", "-u", "u", "-p", "p", "--deadline", "1"],
                             backend, data_dir=tmp)
        check(code == 3, f"a timeout must exit with 3, got {code}")
        dump = dump_of(tmp)
        check(dump["grund"] == "timeout", f"diagnosis reason: {dump['grund']}")
        check(dump["backend_used"] == "fake", "the diagnosis must name the backend used")
        check(dump["backend_configured"] is not None, "backend_configured is missing")
        check(backend.closed, "the backend has to be closed after a timeout")


def test_probe_without_credentials_ends_with_a_dump():
    with tempfile.TemporaryDirectory() as tmp:
        # no code: a probe never gets that far, it just documents the chain
        backend = FakeBackend("st", [WAITING_PAGE])
        code, _ = run_helper(["-b", "peugeot", "--probe", "--probe-wait", "0",
                              "--deadline", "30"], backend, data_dir=tmp)
        check(code == 0, f"a probe ends with exit 0, got {code}")
        dump = dump_of(tmp)
        check(dump["backend_used"] == "fake", "probe dump without the backend")
        check(dump["grund"] == "probe finished", f"probe reason: {dump['grund']}")
        check(dump["letzter_zustand"].get("url"), "the probe dump has no page state")


def test_unavailable_backend_is_reported():
    """No engine installed at all (e.g. a broken image) -> exit 1, not a traceback."""
    parser = helper.build_arg_parser()
    args = parser.parse_args(["-b", "peugeot", "-u", "u", "-p", "p"])
    real_pw, real_qt = helper.have_playwright, helper.have_qt
    helper.have_playwright, helper.have_qt = (lambda: False), (lambda: False)
    try:
        code = helper.run_browser(args)
    finally:
        helper.have_playwright, helper.have_qt = real_pw, real_qt
    check(code == 1, f"an unusable backend must exit with 1, got {code}")

    # an explicit choice that this image cannot deliver is named, not silently ignored
    real_pw, real_qt = helper.have_playwright, helper.have_qt
    helper.have_playwright, helper.have_qt = (lambda: True), (lambda: False)
    try:
        args = parser.parse_args(["-b", "peugeot", "-u", "u", "-p", "p", "--backend", "qt"])
        code = helper.run_browser(args)
    finally:
        helper.have_playwright, helper.have_qt = real_pw, real_qt
    check(code == 1, f"configured qt without PyQt5 must exit with 1, got {code}")


def test_backend_is_forwarded_and_visible_in_diagnostics():
    parser = helper.build_arg_parser()
    args = parser.parse_args(["-b", "peugeot", "-u", "u", "-p", "p", "--backend", "qt"])
    check(getattr(args, "backend", None) == "qt", "--backend was not parsed")
    default = parser.parse_args(["-b", "peugeot", "-u", "u", "-p", "p"])
    check(getattr(default, "backend", None) == "auto",
          "without --backend the helper must fall back to auto")
    with tempfile.TemporaryDirectory() as tmp:
        backend = FakeBackend("st", [WAITING_PAGE])
        _, seen = run_helper(["-b", "peugeot", "-u", "u", "-p", "p", "--deadline", "1",
                              "--backend", "qt"], backend, data_dir=tmp)
        check(seen["choice"] == "qt", f"the orchestrator value did not arrive: {seen}")
        check(dump_of(tmp)["backend_configured"] == "qt", "diagnosis misses the setting")


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
    sys.exit(main())
