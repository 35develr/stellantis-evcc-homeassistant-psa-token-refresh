#!/usr/bin/env python3
"""Regression: in API mode, evcc must not be stopped during the write operations.

The failure from practice (log of 2026-09-29 23:42:46):

    stopping evcc add-on ('<hash>_evcc', state started) ...
    evcc is stopped
    fetching new tokens (peugeot) ...
    tokens received (...)
    ERROR: evcc at http://127.0.0.1:7070/api/config/test/vehicle/merge/9
            not reachable: [Errno 111] Connection refused

The sequence was: stop evcc -> fetch tokens -> write via evcc's HTTP API.
But the config API needs a running evcc. In addition, evcc stayed stopped
after the error (charge control was then out of service).

Why the previous test setup could not see this: tests/test_orchestrator_api.py
never sets SUPERVISOR_TOKEN. Thus sup.available is false, the stop path is never
entered and the suite is green, although the production path is broken.

This test mimics the supervisor and makes the fake evcc really unreachable during
the stop (the listener is closed). It checks the order
stop -> start -> write and that evcc is running at the end.

Usage: python3 tests/test_api_mode_evcc_stop.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "psa-token-refresh")
API_KEY = "testkey123"
SUPERVISOR_TOKEN = "supervisor-token"
SLUG = "<hash>_evcc"

# Event log: record the order of stop, start and write
EVENTS = []

STATE = {
    "writes": [],
    "tests": [],
    "log": "",
    "dirty": False,
    "fail_write": False,
    "fail_test": False,
}

SUP = {"state": "started"}

EVCC = {"server": None, "port": None}

VINS = {"e3008": "VF3AAAA11111", "e5008": "VF3BBBB22222"}
IDS = {"e3008": 9, "e5008": 5}


# ------------------------------------------------------------------ Fake evcc

def detail(vid):
    name = {9: "e3008", 5: "e5008"}[vid]
    return {
        "id": vid,
        "name": f"db:{vid}",
        "config": {
            "title": name,
            "name": name,
            "vin": VINS[name],
            "user": "konto@example.org",
            "accessToken": "***",
            "refreshToken": "***",
        },
    }


class EvccHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b"{}"

    def _send(self, code, payload):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
        else:
            body = str(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path
        if path == "/api/state":          # as in real evcc: no auth, only when running
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
        elif path.startswith("/api/auth/status"):
            self._send(200, "true")
        else:
            self._send(404, {"error": "unknown path", "path": path})

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/config/test/vehicle/merge/"):
            body = json.loads(self._read() or b"{}")
            STATE["tests"].append((self.path, body))
            EVENTS.append("test")
            if STATE["fail_test"]:
                self._send(500, {"error": "PSA rejected"})
            else:
                self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "unknown path", "path": self.path})

    def do_PUT(self):  # noqa: N802
        if self.path.startswith("/api/config/devices/vehicle/"):
            body = json.loads(self._read() or b"{}")
            STATE["writes"].append((self.path, body))
            EVENTS.append("write")
            if STATE["fail_write"]:
                self._send(500, {"error": "save failed"})
            else:
                # like evcc: a changed vehicle configuration requires a restart
                STATE["dirty"] = True
                self._send(200, {"id": int(self.path.rsplit("/", 1)[1])})
        else:
            self._send(404, {"error": "unknown path", "path": self.path})


def evcc_start():
    """Open the listener on the fixed port (idempotent)."""
    if EVCC["server"] is not None:
        return
    server = ThreadingHTTPServer(("127.0.0.1", EVCC["port"]), EvccHandler)
    EVCC["server"] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()


def evcc_stop():
    """Really close the listener - only this produces 'Connection refused'."""
    server = EVCC["server"]
    if server is None:
        return
    EVCC["server"] = None
    server.shutdown()
    server.server_close()


# -------------------------------------------------------------- Fake supervisor

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
                {"name": "evcc", "slug": SLUG, "state": SUP["state"]}]}})
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
            EVENTS.append("stop")
            SUP["state"] = "stopped"
            evcc_stop()
            self._send(200, {"result": "ok"})
        elif self.path.endswith("/start"):
            EVENTS.append("start")
            evcc_start()
            SUP["state"] = "started"
            self._send(200, {"result": "ok"})
        else:
            self._send(404, {"error": "unknown path", "path": self.path})


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
BASE = f"http://127.0.0.1:{EVCC['port']}"


# ------------------------------------------------------------------- Helpers

def reset(extra=None):
    STATE.update({"writes": [], "tests": [], "log": "", "dirty": False,
                  "fail_write": False, "fail_test": False})
    SUP["state"] = "started"
    EVENTS.clear()
    evcc_start()


def write_options(workdir, extra=None):
    opts = {
        "mode": "api",
        "brand": "peugeot",
        "account_user": "konto@example.org",
        "account_password": "secret",
        "vehicles": [{"name": "e3008", "vin": VINS["e3008"]},
                     {"name": "e5008", "vin": VINS["e5008"]}],
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
    return opts


def write_stub(workdir):
    with open(os.path.join(workdir, "stub-helper.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "import json\n"
            "print(json.dumps({'access_token': 'NEW-access-' + 'x'*40,\n"
            "                  'refresh_token': 'NEW-refresh-' + 'y'*40,\n"
            "                  'token_type': 'Bearer', 'expires_in': 3599}))\n"
        )


def run_orchestrator(workdir, extra=None):
    write_options(workdir, extra)
    env = dict(os.environ)
    env["PSA_OPTIONS_FILE"] = os.path.join(workdir, "options.json")
    env["DATA_DIR"] = os.path.join(workdir, "data")
    env["SUPERVISOR_TOKEN"] = SUPERVISOR_TOKEN
    env["SUPERVISOR_URL"] = SUP_URL
    os.makedirs(env["DATA_DIR"], exist_ok=True)
    proc = subprocess.run(
        [sys.executable, os.path.join(ADDON, "orchestrator.py"), "--once"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    proc.out = proc.stdout + proc.stderr
    return proc


def new_workdir(prefix):
    workdir = tempfile.mkdtemp(prefix=prefix)
    write_stub(workdir)
    return workdir


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ----------------------------------------------------------------------- Tests

def test_api_mode_writes_while_evcc_is_running():
    """The core case: stop, fetch tokens, START evcc AGAIN, then write."""
    reset()
    workdir = new_workdir("psa-order-")
    proc = run_orchestrator(workdir)
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-2500:]}")
    check(len(STATE["writes"]) == 2, f"expected 2 PUTs, got {STATE['writes']}")

    paths = sorted(p for p, _ in STATE["writes"])
    check(paths == ["/api/config/devices/vehicle/5", "/api/config/devices/vehicle/9"],
          f"wrong paths: {paths}")
    for _path, body in STATE["writes"]:
        check(body["accessToken"] == "NEW-access-" + "x" * 40,
              "accessToken does not match the helper")
        check(body["refreshToken"] == "NEW-refresh-" + "y" * 40,
              "refreshToken does not match the helper")
        check("#" in body["user"], f"user not rotated: {body['user']}")
    check(len(STATE["tests"]) == 2, f"test calls missing: {STATE['tests']}")

    # the order is the actual regression protection
    check("stop" in EVENTS, f"evcc was not stopped: {EVENTS}")
    check("write" in EVENTS, f"nothing was written: {EVENTS}")
    first_stop = EVENTS.index("stop")
    first_start = EVENTS.index("start") if "start" in EVENTS else None
    first_write = EVENTS.index("write")
    check(first_start is not None, f"evcc was never started after the stop: {EVENTS}")
    check(first_start < first_write,
          f"it wrote BEFORE evcc was running again: {EVENTS}")
    check(first_stop < first_start,
          f"start before stop - implausible order: {EVENTS}")

    check(SUP["state"] == "started", f"evcc is not running at the end: {SUP['state']}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_evcc_runs_again_after_write_error():
    """Safety net: even an error in the write path must not leave evcc dead."""
    reset(extra=None)
    STATE["fail_write"] = True
    workdir = new_workdir("psa-tot-")
    proc = run_orchestrator(workdir)
    check(proc.returncode != 0, f"write error led to exit 0\n{proc.out[-2000:]}")
    check("ERROR" in proc.out, f"no ERROR logged:\n{proc.out[-1500:]}")
    check(SUP["state"] == "started",
          f"evcc stayed stopped (state {SUP['state']}) - charge control dead")
    shutil.rmtree(workdir, ignore_errors=True)


def test_evcc_runs_again_when_token_fetch_fails():
    """If even the token fetch fails, evcc must still run again."""
    reset()
    workdir = new_workdir("psa-helfer-")
    broken = os.path.join(workdir, "stub-helper.py")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("import sys\nsys.exit(3)\n")
    proc = run_orchestrator(workdir)
    check(proc.returncode != 0, f"helper error led to exit 0\n{proc.out[-1500:]}")
    check(SUP["state"] == "started",
          f"evcc stayed stopped after the failed login ({SUP['state']})")
    check(not STATE["writes"], "it wrote despite the missing tokens")
    shutil.rmtree(workdir, ignore_errors=True)


def test_old_log_lines_do_not_break_verification():
    """Old token errors in the log must not turn a successful renewal red."""
    reset()
    STATE["log"] = ("evcc 0.316.1\n[main ] ERROR creating vehicle car2 failed: "
                    "cannot create vehicle type 'template': invalid_grant\n")
    workdir = new_workdir("psa-altlog-")
    proc = run_orchestrator(workdir)
    check(proc.returncode == 0,
          f"old log line tipped the verification\n{proc.out[-2000:]}")
    state = json.load(open(os.path.join(workdir, "data", "state.json"), encoding="utf-8"))
    check(state.get("last_error") is None, f"state.last_error: {state.get('last_error')}")
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
