#!/usr/bin/env python3
"""Self-test for the supervisor integration (slug lookup, stop/start, messages).

Against a local fake supervisor that mimics exactly the endpoints the
add-on uses: /addons, /addons/<slug>/info|start|stop|logs and
/core/api/services/...

Usage: python3 tests/test_supervisor_api.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "psa-token-refresh"))

SUPERVISOR_TOKEN = "supervisor-token"
STATE = {
    "apps": [],
    "state": {},
    "calls": [],
    "failing": None,      # path prefix that responds with 404/500
    "notify_status": 200,
    "self": {"version": "1.4.3", "hassio_api": True, "hassio_role": "manager",
             "homeassistant_api": True, "host_network": True, "protected": False,
             "options": {"mode": "auto", "account_user": "konto@example.org",
                         "account_password": "SECRET-PSA", "evcc_admin_password": "SECRET-EVCC",
                         "evcc_apikey": "", "trigger_token": "SECRET-TRIGGER",
                         "dry_run": True, "vehicles": [{"name": "e3008"}]}},
}


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        return self.headers.get("Authorization", "") == f"Bearer {SUPERVISOR_TOKEN}"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            self._send(401, {"result": "error", "message": "Unauthorized"})
            return
        STATE["calls"].append(("GET", self.path))
        if STATE["failing"] and self.path.startswith(STATE["failing"]):
            self._send(404, {"result": "error", "message": f"App {self.path} does not exist"})
            return
        if self.path in ("/addons", "/apps"):
            self._send(200, {"data": {self.path[1:]: STATE["apps"]}})
            return
        if self.path.endswith("/self/info"):
            self._send(200, {"data": STATE["self"]})
            return
        if self.path.endswith("/info"):
            slug = self.path.split("/")[2]
            if slug not in STATE["state"]:
                self._send(404, {"result": "error", "message": f"App {slug} does not exist"})
                return
            self._send(200, {"data": {"slug": slug, "state": STATE["state"][slug]}})
            return
        if self.path.startswith("/addons/") and self.path.endswith("/logs"):
            self._send_text(200, "evcc log line\n")
            return
        self._send(404, {"result": "error", "message": "not found"})

    def _send_text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if not self._auth_ok():
            self._send(401, {"result": "error", "message": "Unauthorized"})
            return
        STATE["calls"].append(("POST", self.path))
        if STATE["failing"] and self.path.startswith(STATE["failing"]):
            self._send(404, {"result": "error", "message": f"App {self.path} does not exist"})
            return
        if self.path.startswith("/core/api/"):
            self._send(STATE["notify_status"], {"ok": True})
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[2] in ("start", "stop"):
            slug = parts[1]
            if slug not in STATE["state"]:
                self._send(404, {"result": "error", "message": f"App {slug} does not exist"})
                return
            STATE["state"][slug] = "started" if parts[2] == "start" else "stopped"
            self._send(200, {"result": "ok"})
            return
        self._send(404, {"result": "error", "message": "not found"})


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"

os.environ["SUPERVISOR_TOKEN"] = SUPERVISOR_TOKEN
os.environ["SUPERVISOR_URL"] = BASE
os.environ["DATA_DIR"] = "/tmp/psa-supervisor-test"

from orchestrator import Supervisor  # noqa: E402


def reset(apps, states, failing=None, notify_status=200, self_info=None):
    STATE["apps"] = apps
    STATE["state"] = states
    STATE["calls"].clear()
    STATE["failing"] = failing
    STATE["notify_status"] = notify_status
    STATE["self"] = self_info or {"version": "1.4.3", "hassio_api": True,
                                  "hassio_role": "manager", "homeassistant_api": True,
                                  "host_network": True, "protected": False,
                                  "options": {"mode": "auto", "account_user": "konto@example.org",
                                              "account_password": "SECRET-PSA",
                                              "evcc_admin_password": "SECRET-EVCC",
                                              "evcc_apikey": "", "trigger_token": "SECRET-TRIGGER",
                                              "dry_run": True, "vehicles": [{"name": "e3008"}]}}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def make_sup(**opts):
    base = {"evcc_slug": "", "notify": True}
    base.update(opts)
    return Supervisor(base)


def test_slug_discovery_prefers_exact_name():
    reset(
        apps=[
            {"name": "Mosquitto broker", "slug": "core_mosquitto", "state": "started"},
            {"name": "evcc", "slug": "a1b2c3d4-evcc", "state": "started"},
            {"name": "Stellantis/PSA Token Refresh for evcc", "slug": "local_psa_token_refresh", "state": "started"},
        ],
        states={"a1b2c3d4-evcc": "started", "core_mosquitto": "started",
                "local_psa_token_refresh": "started"},
    )
    sup = make_sup()
    check(sup.resolve_slug() == "a1b2c3d4-evcc", f"wrong slug: {sup._slug}")
    check(sup.state() == "started", "state not read")


def test_configured_slug_is_used_when_installed():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}],
          states={"evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    check(sup.resolve_slug() == "evcc", "configured slug was not used")


def test_wrong_configured_slug_falls_back_to_discovery():
    reset(apps=[{"name": "evcc", "slug": "a1b2c3d4-evcc", "state": "started"}],
          states={"a1b2c3d4-evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    check(sup.resolve_slug() == "a1b2c3d4-evcc", f"no fallback: {sup._slug}")


def test_unknown_slug_raises_with_list():
    reset(apps=[{"name": "Mosquitto broker", "slug": "core_mosquitto", "state": "started"}],
          states={"core_mosquitto": "started"})
    sup = make_sup()
    try:
        sup.resolve_slug()
    except RuntimeError as exc:
        check("core_mosquitto" in str(exc), f"list missing in the message: {exc}")
        return
    raise AssertionError("unknown slug did not lead to an error")


def test_stop_aborts_when_state_unknown():
    """Exactly the failure from practice: a 404 on the info call must not count as 'stopped'."""
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"},
          failing="/addons/evcc/info")
    sup = make_sup(evcc_slug="evcc")
    check(sup.state() is None, "unknown state was not reported as None")
    check(sup.stop_evcc() is False, "stop_evcc() should have aborted")
    check(not any(c[0] == "POST" and c[1].endswith("/stop") for c in STATE["calls"]),
          "a stop call was sent anyway")


def test_stop_and_start_roundtrip():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    check(sup.stop_evcc(wait_seconds=10) is True, "stopping failed")
    check(STATE["state"]["evcc"] == "stopped", "state not stopped in the fake")
    check(sup.start_evcc(wait_seconds=10) is True, "starting failed")
    check(STATE["state"]["evcc"] == "started", "state not started in the fake")


def test_stop_returns_true_when_already_stopped():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "stopped"}], states={"evcc": "stopped"})
    sup = make_sup(evcc_slug="evcc")
    check(sup.stop_evcc(wait_seconds=5) is True, "already stopped was treated as an error")
    check(not any(c[0] == "POST" for c in STATE["calls"]), "unnecessary stop call")


def test_stop_reports_error_status():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"},
          failing="/addons/evcc/stop")
    sup = make_sup(evcc_slug="evcc")
    check(sup.stop_evcc(wait_seconds=5) is False, "HTTP error on stop not recognized")


def test_evcc_log_returns_text():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    check("evcc log line" in sup.evcc_log(), "log not read")


def test_notify_uses_core_api_and_reports_error():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    sup.notify("Title", "Text")
    check(any(c[1].startswith("/core/api/services/persistent_notification/create")
              for c in STATE["calls"]), "notification did not go to /core/api")

    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"},
          notify_status=401)
    sup = make_sup(evcc_slug="evcc")
    try:
        sup.notify("Title", "Text")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"notify() must not raise: {exc}") from exc


def test_apps_list_and_prefix_detection():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    apps = sup.list_apps(reload=True)
    check(len(apps) == 1 and apps[0]["slug"] == "evcc", f"add-on list: {apps}")
    check(sup._prefix == "addons", f"API prefix: {sup._prefix}")


def test_self_diagnostics_reports_version_and_permissions():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"})
    sup = make_sup(evcc_slug="evcc")
    messages = sup.self_diagnostics("1.4.3")
    check(len(messages) == 1, f"unexpected additional messages: {messages}")
    check("1.4.3" in messages[0] and "homeassistant_api=True" in messages[0],
          f"info missing: {messages[0]}")
    check(sup.self_info().get("hassio_role") == "manager", "role not read")


def test_self_info_masks_secrets():
    """The supervisor delivers the options in plain text - /status and /dump must
    therefore not pass them on unfiltered (finding of 01.10.2026: PSA password and
    evcc admin password were in plain text in the status response)."""
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"})
    opts = make_sup(evcc_slug="evcc").self_info().get("options") or {}
    for key in ("account_password", "evcc_admin_password", "trigger_token"):
        check(opts.get(key) == "***", f"{key} is in plain text in the response: {opts.get(key)!r}")
    check(opts.get("account_user") == "konto@example.org", "harmless option was masked too")
    check(opts.get("mode") == "auto", "harmless option was masked too")
    check(opts.get("dry_run") is True, "harmless option was masked too")
    check(opts.get("vehicles") == [{"name": "e3008"}], "nested option damaged")
    # empty value remains empty - one can tell that nothing is set
    check(opts.get("evcc_apikey") == "***", "empty secret value was not masked")


def test_self_diagnostics_warns_on_version_mismatch():
    """Exactly the case from practice: the image is older than the installation."""
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"},
          self_info={"version": "1.4.0", "hassio_api": True, "hassio_role": "manager",
                     "homeassistant_api": True, "host_network": True, "protected": False})
    sup = make_sup(evcc_slug="evcc")
    messages = sup.self_diagnostics("1.4.3")
    check(any("does not match the state of the installation" in m for m in messages),
          f"version warning missing: {messages}")


def test_self_diagnostics_warns_on_missing_permissions():
    reset(apps=[{"name": "evcc", "slug": "evcc", "state": "started"}], states={"evcc": "started"},
          self_info={"version": "1.4.3", "hassio_api": True, "hassio_role": "default",
                     "homeassistant_api": False, "host_network": True, "protected": True})
    sup = make_sup(evcc_slug="evcc")
    messages = " | ".join(sup.self_diagnostics("1.4.3"))
    check("homeassistant_api is NOT granted" in messages, f"hint missing: {messages}")
    check("hassio_role is 'default'" in messages, f"role hint missing: {messages}")


def test_self_diagnostics_without_supervisor_is_graceful():
    reset(apps=[], states={})
    sup = Supervisor({"evcc_slug": "", "notify": False})
    sup.available = False
    messages = sup.self_diagnostics("1.4.3")
    check(len(messages) == 1 and "not available" in messages[0], f"message: {messages}")


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
