#!/usr/bin/env python3
"""Self-test for the evcc control option (`evcc_control`, issue #2).

evcc no longer has to be an add-on. Covered here:

  1. supervisor - the previous path via the Supervisor API (regression: must keep working,
     including the order stop -> tokens -> start -> write)
  2. api        - POST /api/system/shutdown; the process manager or the add-on watchdog
                  brings evcc back (simulated here with a timer)
  3. api without the watchdog/restart policy: the run aborts with a clear message
  4. auto       - no evcc add-on found: the config is written, but evcc is NOT stopped
  5. webhook    - the restart is triggered via POST to evcc_restart_webhook
  6. none       - evcc is never touched

The fake evcc really closes its listener on shutdown (that is the only way to produce
"Connection refused"), and the fake watchdog opens it again shortly afterwards - exactly
what the Home Assistant add-on watchdog does.

Usage: python3 tests/test_evcc_control.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "psa-token-refresh")
API_KEY = "testkey123"
SUPERVISOR_TOKEN = "supervisor-token"
SLUG = "49686a9f_evcc"

EVENTS = []
STATE = {"writes": [], "tests": [], "log": "", "dirty": False}
SUP = {"apps": [{"name": "evcc", "slug": SLUG, "state": "started"}],
       "state": "started"}
EVCC = {"server": None, "port": None, "watchdog": True, "watchdog_delay": 2.0,
        "timer": None, "lock": threading.Lock()}
WEBHOOK = {"calls": []}

VINS = {"e3008": "VF3AAAA11111", "e5008": "VF3BBBB22222"}
IDS = {"e3008": 9, "e5008": 5}


# ------------------------------------------------------------------ fake evcc

def detail(vid):
    name = {9: "e3008", 5: "e5008"}[vid]
    return {
        "id": vid,
        "name": f"db:{vid}",
        "config": {"title": name, "name": name, "vin": VINS[name],
                   "user": "konto@example.org", "accessToken": "***", "refreshToken": "***"},
    }


class EvccHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b"{}"

    def _send(self, code, payload):
        body = (json.dumps(payload) if isinstance(payload, (dict, list))
                else str(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path
        if path == "/api/state":
            self._send(200, {"version": "0.316.1",
                             "loadpoints": [{"title": "Wallbox", "charging": False}]})
        elif path.startswith("/api/config/dirty"):
            self._send(200, {"dirty": STATE["dirty"]})
        elif path.rstrip("/") == "/api/config/devices/vehicle":
            self._send(200, [detail(9), detail(5)])
        elif path.startswith("/api/config/devices/vehicle/"):
            self._send(200, detail(int(path.rsplit("/", 1)[1])))
        elif path.startswith("/api/system/log"):
            self._send(200, STATE["log"])
        else:
            self._send(404, {"error": "unknown path", "path": path})

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/system/shutdown"):
            # evcc shuts down; the process manager/watchdog brings it back
            EVENTS.append("shutdown")
            self._send(204, "")
            threading.Thread(target=evcc_stop, daemon=True).start()
            if EVCC["watchdog"]:
                _watchdog(EVCC["watchdog_delay"])
        elif self.path.startswith("/api/config/test/vehicle/merge/"):
            STATE["tests"].append((self.path, json.loads(self._read() or b"{}")))
            EVENTS.append("test")
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "unknown path", "path": self.path})

    def do_PUT(self):  # noqa: N802
        if self.path.startswith("/api/config/devices/vehicle/"):
            STATE["writes"].append((self.path, json.loads(self._read() or b"{}")))
            EVENTS.append("write")
            STATE["dirty"] = True
            self._send(200, {"id": int(self.path.rsplit("/", 1)[1])})
        else:
            self._send(404, {"error": "unknown path", "path": self.path})


def _watchdog(delay):
    """The add-on watchdog: brings evcc back after a short while (a timer)."""
    if EVCC["timer"] is not None:
        EVCC["timer"].cancel()
    EVCC["timer"] = threading.Timer(delay, evcc_start)
    EVCC["timer"].daemon = True
    EVCC["timer"].start()


def evcc_start():
    with EVCC["lock"]:
        if EVCC["server"] is not None:
            return
        server = None
        for _ in range(8):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", EVCC["port"]), EvccHandler)
                break
            except OSError:
                time.sleep(0.4)          # the old listener is still cleaning up
        if server is None:
            return                        # apparently already running
        EVCC["server"] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if EVENTS and EVENTS[-1] == "shutdown":
        EVENTS.append("watchdog-start")


def evcc_stop():
    with EVCC["lock"]:
        server = EVCC["server"]
        EVCC["server"] = None
    if server is None:
        return
    server.shutdown()
    server.server_close()


# -------------------------------------------------------------- fake Supervisor

class SupHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = (json.dumps(payload) if isinstance(payload, (dict, list))
                else str(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("Authorization", "") == f"Bearer {SUPERVISOR_TOKEN}"

    def do_GET(self):  # noqa: N802
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path.rstrip("/") in ("/addons", "/apps"):
            self._send(200, {"data": {"addons": [
                dict(a, state=SUP["state"]) for a in SUP["apps"]]}})
        elif self.path.endswith("/info"):
            self._send(200, {"data": {"state": SUP["state"]}})
        elif self.path.endswith("/logs"):
            self._send(200, STATE["log"])
        else:
            self._send(404, {"error": "unknown path", "path": self.path})

    def do_POST(self):  # noqa: N802
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/stop"):
            EVENTS.append("sup-stop")
            SUP["state"] = "stopped"
            evcc_stop()
            self._send(200, {"result": "ok"})
        elif self.path.endswith("/start"):
            EVENTS.append("sup-start")
            evcc_start()
            SUP["state"] = "started"
            self._send(200, {"result": "ok"})
        else:
            self._send(404, {"error": "unknown path", "path": self.path})


# ------------------------------------------------------------------ fake webhook

class HookHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        WEBHOOK["calls"].append(self.path)
        EVENTS.append("webhook")
        _watchdog(1.0)                             # the webhook restarts evcc
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


EVCC["port"] = free_port()
evcc_start()
SUP_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), SupHandler)
threading.Thread(target=SUP_SERVER.serve_forever, daemon=True).start()
SUP_URL = f"http://127.0.0.1:{SUP_SERVER.server_address[1]}"
HOOK_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), HookHandler)
threading.Thread(target=HOOK_SERVER.serve_forever, daemon=True).start()
HOOK_URL = f"http://127.0.0.1:{HOOK_SERVER.server_address[1]}/restart"
BASE = f"http://127.0.0.1:{EVCC['port']}"


# ------------------------------------------------------------------- utilities

def reset(extra=None, sup_apps=None, watchdog=True):
    STATE.update({"writes": [], "tests": [], "log": "", "dirty": False})
    EVENTS.clear()
    WEBHOOK["calls"].clear()
    if EVCC["timer"] is not None:
        EVCC["timer"].cancel()
        EVCC["timer"] = None
    SUP["apps"] = (sup_apps if sup_apps is not None
                   else [{"name": "evcc", "slug": SLUG, "state": "started"}])
    EVCC["watchdog"] = watchdog
    EVCC["watchdog_delay"] = 2.0
    SUP["state"] = "started"
    evcc_stop()
    evcc_start()
    time.sleep(0.2)


def write_stub(workdir):
    with open(os.path.join(workdir, "stub-helper.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "import json\n"
            "print(json.dumps({'access_token': 'NEW-access-' + 'x'*40,\n"
            "                  'refresh_token': 'NEW-refresh-' + 'y'*40,\n"
            "                  'token_type': 'Bearer', 'expires_in': 3599}))\n"
        )


def run_orchestrator(extra=None, sup_apps=None, watchdog=True, supervisor=True):
    reset(extra=extra, sup_apps=sup_apps, watchdog=watchdog)
    workdir = tempfile.mkdtemp(prefix="psa-ctl-")
    write_stub(workdir)
    opts = {
        "mode": "api",
        "brand": "peugeot",
        "account_user": "konto@example.org",
        "account_password": "secret",
        "vehicles": [{"name": "e3008", "vin": VINS["e3008"]}],
        "evcc_url": BASE,
        "evcc_apikey": API_KEY,
        "evcc_slug": SLUG,
        "helper_cmd": f"python3 {os.path.join(workdir, 'stub-helper.py')}",
        "notify": False,
        "verify_delay_seconds": 10,
        "api_verify_with_test": True,
    }
    for key, value in (extra or {}).items():
        if value is None:
            opts.pop(key, None)
        else:
            opts[key] = value
    with open(os.path.join(workdir, "options.json"), "w", encoding="utf-8") as fh:
        json.dump(opts, fh)
    env = dict(os.environ)
    env["PSA_OPTIONS_FILE"] = os.path.join(workdir, "options.json")
    env["DATA_DIR"] = os.path.join(workdir, "data")
    if supervisor:
        env["SUPERVISOR_TOKEN"] = SUPERVISOR_TOKEN
        env["SUPERVISOR_URL"] = SUP_URL
    else:
        env.pop("SUPERVISOR_TOKEN", None)
        env.pop("SUPERVISOR_URL", None)
    os.makedirs(env["DATA_DIR"], exist_ok=True)
    proc = subprocess.run(
        [sys.executable, os.path.join(ADDON, "orchestrator.py"), "--once"],
        capture_output=True, text=True, env=env, timeout=300,
    )
    proc.out = proc.stdout + proc.stderr
    return proc, workdir


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ----------------------------------------------------------------------- tests

def test_supervisor_mode_still_works():
    """Regression: evcc_control=supervisor keeps using the Supervisor API."""
    proc, workdir = run_orchestrator({"evcc_control": "supervisor"})
    check(proc.returncode == 0, f"exit {proc.returncode}\n{proc.out[-2500:]}")
    check("sup-stop" in EVENTS, f"Supervisor stop missing: {EVENTS}")
    check("sup-start" in EVENTS, f"Supervisor start missing: {EVENTS}")
    check("shutdown" not in EVENTS, f"used the evcc API shutdown instead: {EVENTS}")
    check(EVENTS.index("sup-stop") < EVENTS.index("sup-start") < EVENTS.index("write"),
          f"wrong order: {EVENTS}")
    check(len(STATE["writes"]) == 1, f"expected 1 PUT, got {STATE['writes']}")
    check(EVCC["server"] is not None, "evcc is not running at the end")
    shutil.rmtree(workdir, ignore_errors=True)


def test_auto_uses_supervisor_when_evcc_addon_exists():
    """auto (default) must use the Supervisor as long as an evcc add-on exists."""
    proc, workdir = run_orchestrator({})     # evcc_control not set = auto
    check(proc.returncode == 0, f"exit {proc.returncode}\n{proc.out[-2500:]}")
    check("sup-stop" in EVENTS and "sup-start" in EVENTS,
          f"auto did not use the Supervisor: {EVENTS}")
    check("shutdown" not in EVENTS, f"auto shut evcc down via the API: {EVENTS}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_api_mode_shutdown_and_watchdog_restart():
    """evcc_control=api: shutdown via API, watchdog restarts evcc, then write."""
    proc, workdir = run_orchestrator({"evcc_control": "api"})
    check(proc.returncode == 0, f"exit {proc.returncode}\n{proc.out[-2500:]}")
    check("shutdown" in EVENTS, f"no POST /api/system/shutdown: {EVENTS}")
    check("sup-stop" not in EVENTS, f"the Supervisor was used anyway: {EVENTS}")
    check("write" in EVENTS, f"nothing was written: {EVENTS}")
    check(EVENTS.index("shutdown") < EVENTS.index("write"),
          f"wrong order: {EVENTS}")
    check("watchdog-start" in EVENTS, f"the watchdog restart was not awaited: {EVENTS}")
    check(EVCC["server"] is not None, "evcc is not running at the end")
    check("evcc bereit" in proc.out or "evcc ready" in proc.out,
          f"no readiness message:\n{proc.out[-1500:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_api_mode_without_restart_policy_fails_clearly():
    """Without watchdog/restart policy nothing may be written blindly."""
    proc, workdir = run_orchestrator({"evcc_control": "api", "evcc_ready_timeout": 10},
                                     watchdog=False)
    check(proc.returncode != 0, f"a missing restart produced exit 0\n{proc.out[-2000:]}")
    check("restart policy" in proc.out or "watchdog" in proc.out,
          f"hint about the restart policy missing:\n{proc.out[-2000:]}")
    check(not STATE["writes"], f"wrote without readiness: {STATE['writes']}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_auto_without_evcc_addon_writes_without_stopping():
    """Without an evcc add-on, auto does not stop anything - it only writes."""
    # drop evcc_slug as well: otherwise the add-on assumes the configured slug is installed
    proc, workdir = run_orchestrator({"evcc_slug": None}, sup_apps=[
        {"name": "Mosquitto broker", "slug": "core_mosquitto", "state": "started"}])
    check(proc.returncode == 0, f"exit {proc.returncode}\n{proc.out[-2500:]}")
    check("shutdown" not in EVENTS, f"evcc was shut down anyway: {EVENTS}")
    check("sup-stop" not in EVENTS, f"the Supervisor was used: {EVENTS}")
    check(len(STATE["writes"]) == 1, f"expected 1 PUT, got {STATE['writes']}")
    check("no evcc add-on" in proc.out.lower() or "evcc_control: auto" in proc.out,
          f"no hint about skipping the stop:\n{proc.out[-1500:]}")
    check(EVCC["server"] is not None, "evcc is no longer running")
    shutil.rmtree(workdir, ignore_errors=True)


def test_webhook_mode_triggers_restart():
    """evcc_control=webhook: the restart comes from POSTing to the configured URL."""
    proc, workdir = run_orchestrator({"evcc_control": "webhook",
                                      "evcc_restart_webhook": HOOK_URL},
                                     watchdog=False)
    check(proc.returncode == 0, f"exit {proc.returncode}\n{proc.out[-2500:]}")
    check(WEBHOOK["calls"], "the restart webhook was never called")
    check("shutdown" in EVENTS, f"no shutdown before the restart: {EVENTS}")
    check(EVENTS.index("shutdown") < EVENTS.index("webhook"),
          f"the webhook came before the shutdown: {EVENTS}")
    check(EVCC["server"] is not None, "evcc is not running at the end")
    shutil.rmtree(workdir, ignore_errors=True)


def test_webhook_mode_without_url_fails():
    """Without a URL, webhook mode must not pretend it restarted anything."""
    proc, workdir = run_orchestrator({"evcc_control": "webhook", "evcc_restart_webhook": "",
                                      "evcc_ready_timeout": 10}, watchdog=False)
    check(proc.returncode != 0, f"an empty webhook URL produced exit 0\n{proc.out[-2000:]}")
    check("evcc_restart_webhook is empty" in proc.out,
          f"clear message missing:\n{proc.out[-2000:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_none_mode_never_touches_evcc():
    """none: the config is written, but evcc is not stopped or restarted."""
    proc, workdir = run_orchestrator({"evcc_control": "none"})
    check(proc.returncode == 0, f"exit {proc.returncode}\n{proc.out[-2500:]}")
    check("shutdown" not in EVENTS, f"evcc was shut down: {EVENTS}")
    check("sup-stop" not in EVENTS, f"the Supervisor was used: {EVENTS}")
    check(len(STATE["writes"]) == 1, f"expected 1 PUT, got {STATE['writes']}")
    check("evcc is not stopped" in proc.out.lower(),
          f"hint about skipping the stop missing:\n{proc.out[-1500:]}")
    check(EVCC["server"] is not None, "evcc is no longer running")
    shutil.rmtree(workdir, ignore_errors=True)


def test_yaml_mode_requires_supervisor():
    """YAML mode with an external evcc: a clear abort instead of a useless patch."""
    proc, workdir = run_orchestrator({"evcc_control": "api", "mode": "yaml"})
    check(proc.returncode != 0, f"YAML without a Supervisor produced exit 0\n{proc.out[-2000:]}")
    check("YAML mode requires a Supervisor" in proc.out,
          f"clear message missing:\n{proc.out[-2000:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
