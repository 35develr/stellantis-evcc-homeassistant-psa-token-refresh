#!/usr/bin/env python3
"""Self-test for the Playwright backend against a fake OAuth chain.

It reproduces the part of the PSA flow that is easy to get wrong: the last redirect goes
to the brand scheme (`mymap://oauth2redirect/de?code=...`). Chromium refuses that
navigation (net::ERR_ABORTED, no document is ever committed), and Playwright's route()
API does NOT see it - microsoft/playwright#11014. The authorization code still arrives,
but only as a `request` / `requestfailed` event, and ONLY as long as no route handler is
registered on the context.

That is what this test pins down:
  1. without a route handler the code is intercepted (the normal path),
  2. with a route handler it is not - so nobody "improves" the backend by adding
     route() and breaks the login silently,
  3. the headless user agent does not announce itself as HeadlessChrome.

Needs a browser (`playwright install --only-shell chromium`). Without Playwright or
without a browser binary the test skips instead of failing: the add-on image has both.

Usage: python3 tests/test_playwright_redirect.py
"""

import http.server
import importlib.util
import json
import os
import socketserver
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.normpath(os.path.join(HERE, "..", "psa-token-refresh", "psa-browser-token.py"))

spec = importlib.util.spec_from_file_location("psa_helper", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

CODE = "TESTCODE123456"
STATE = "state-abc"
BRAND = "mymap"
REDIRECT = f"{BRAND}://oauth2redirect/de?code={CODE}&state={STATE}"
# an intermediate hop carries a foreign code - it must never be used
FOREIGN_QUERY = "?code=WRONGCODE&state=other"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the test output clean
        pass

    def do_GET(self):
        if self.path.startswith("/start"):
            self._redirect("/hop")
        elif self.path.startswith("/hop"):
            self._redirect("/foreign" + FOREIGN_QUERY)
        elif self.path.startswith("/foreign"):
            self._redirect(REDIRECT)
        elif self.path.startswith("/plain"):
            body = b"<html><body><h1>plain</h1><input type=password></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def serve():
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_code_is_intercepted_without_a_route_handler():
    _, base = serve()
    backend = helper.PlaywrightBackend(STATE)
    try:
        backend.start(f"{base}/start")      # must not raise despite net::ERR_ABORTED
        backend.pause(3.0)
        check(backend.code() == CODE,
              f"the authorization code was not intercepted (got {backend.code()!r}); "
              f"requests seen: {backend.requests()[-4:]}")
        check(any(u.startswith(BRAND + "://") for u in backend.requests()),
              "the brand scheme URL is missing from the request history")
        # the foreign code of the intermediate hop is recorded for the diagnosis but
        # must not be used - the brand scheme wins
        check(any("WRONGCODE" in u for u in backend.skipped_codes()),
              f"the foreign code was not recorded for diagnosis: {backend.skipped_codes()}")
        check(backend.code() == CODE,
              f"a foreign code was used instead of the brand scheme: {backend.code()!r}")
    finally:
        backend.close()


def test_route_handler_hides_the_brand_scheme():
    """Documents the trap: an active route() suppresses the brand scheme URL.

    The route has to be registered before the first navigation, so the backend's own
    _goto() is wrapped for this case only.
    """
    _, base = serve()
    real_goto = helper.PlaywrightBackend._goto

    def goto_with_route(self, url):
        self._context.route("**/*", lambda route: route.continue_())
        return real_goto(self, url)

    helper.PlaywrightBackend._goto = goto_with_route
    backend = helper.PlaywrightBackend(STATE)
    try:
        backend.start(f"{base}/start")
        backend.pause(3.0)
        check(backend.code() is None,
              "with a route handler the code was intercepted - Playwright's behaviour "
              "changed; the comment in psa-browser-token.py needs an update")
    finally:
        helper.PlaywrightBackend._goto = real_goto
        backend.close()


def test_headless_user_agent_is_not_advertised():
    _, base = serve()
    backend = helper.PlaywrightBackend(STATE)
    try:
        backend.start(f"{base}/plain")      # chain that ends normally
        ua = backend.evaluate("navigator.userAgent") or ""
        driver = backend.evaluate("String(navigator.webdriver)")
        check("HeadlessChrome" not in ua, f"the user agent still says HeadlessChrome: {ua}")
        check("Chrome/" in ua, f"the user agent does not look like Chrome: {ua}")
        check(driver in ("undefined", "false"),
              f"navigator.webdriver is still visible: {driver}")
    finally:
        backend.close()


def main():
    if not helper.have_playwright():
        print("SKIP  playwright is not installed here (the add-on image has it)")
        return 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed, skipped = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "Executable doesn't exist" in text or "playwright install" in text:
                skipped += 1
                print(f"SKIP  {t.__name__}: no browser binary ({text.splitlines()[0][:80]})")
                continue
            failed += 1
            print(f"FAIL  {t.__name__}: {text}")
    print(f"\n{len(tests) - failed - skipped} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
