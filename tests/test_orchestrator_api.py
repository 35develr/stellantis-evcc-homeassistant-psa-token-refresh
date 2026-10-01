#!/usr/bin/env python3
"""Integration test for orchestrator.py against a fake evcc.

The real code is tested (subprocess, as in the add-on), but against a
local fake server and a stub token helper - so without PSA, without real evcc.

Covered:
  * mode detection, dry run, writing both vehicles, error cases
  * automatic generation of the evcc API key from the admin password incl. storage
  * reuse of a stored key, regeneration when it becomes invalid
  * clear messages on a wrong password or a missing admin password

Usage: python3 tests/test_orchestrator_api.py
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "psa-token-refresh")
API_KEY = "testkey123"

STATE = {
    "writes": [],
    "tests": [],
    "log": "",
    "valid_keys": set(),
    "admin_password": "",          # password the fake evcc accepts
    "auth_state": "configured",    # configured | missing | disabled
    "apikey_posts": 0,
    "password_puts": [],
    "logins": 0,
    "session_cookie": "",
    "auth_calls": [],
}

VEHICLES = [
    {"name": "db:5", "type": "template", "id": 5,
     "config": {"title": "e5008", "template": "peugeot", "user": "konto@example.org",
                "accessToken": "***", "refreshToken": "***", "vin": "VF3BBBB22222",
                "capacity": 73}},
    {"name": "db:9", "type": "template", "id": 9,
     "config": {"title": "e3008", "template": "peugeot", "user": "konto@example.org",
                "accessToken": "***", "refreshToken": "***", "vin": "VF3AAAA11111",
                "capacity": 70}},
]


def reset_fake(**kw):
    STATE["writes"].clear()
    STATE["tests"].clear()
    STATE["apikey_posts"] = 0
    STATE["password_puts"].clear()
    STATE["logins"] = 0
    STATE["session_cookie"] = ""
    STATE["auth_calls"].clear()
    STATE["log"] = ""
    STATE["valid_keys"] = set(kw.pop("valid_keys", {API_KEY}))
    STATE["admin_password"] = kw.pop("admin_password", "")
    STATE["auth_state"] = kw.pop("auth_state", "configured")
    for k, v in kw.items():
        STATE[k] = v


class Handler(BaseHTTPRequestHandler):
    def _key_ok(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in STATE["valid_keys"]

    def _session_ok(self):
        """As in real evcc: EnsureAuthHandler accepts key OR session cookie."""
        return bool(STATE["session_cookie"]) and STATE["session_cookie"] in self.headers.get("Cookie", "")

    def _authed(self):
        return self._key_ok() or self._session_ok()

    def _send(self, code, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text, cookie=None):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------- GET

    def do_GET(self):  # noqa: N802
        if self.path == "/api/auth/status":
            if STATE["auth_state"] == "disabled":
                self._text(200, "true")
            elif STATE["auth_state"] == "missing":
                self._text(501, "Not implemented")
            else:
                self._text(200, "true" if self._key_ok() else "false")
            return

        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return

        if self.path == "/api/config/devices/vehicle":
            self._send(200, VEHICLES)
        elif self.path.startswith("/api/config/devices/vehicle/"):
            vid = int(self.path.rsplit("/", 1)[1])
            self._send(200, next(v for v in VEHICLES if v["id"] == vid))
        elif self.path == "/api/config/dirty":
            self._send(200, {"dirty": True})
        elif self.path == "/api/state":
            self._send(200, {"loadpoints": [{"title": "Wallbox", "charging": False,
                                             "chargePower": 0}]})
        elif self.path == "/api/system/log":
            self._text(200, STATE["log"] or "evcc started\n")
        else:
            self._send(404, {"error": "not found"})

    # ---------------------------------------------------------- POST

    def do_POST(self):  # noqa: N802
        if self.path == "/api/auth/apikey":
            STATE["apikey_posts"] += 1
            STATE["auth_calls"].append("apikey")
            body = self._body()
            # evcc: EnsureAuthHandler before the password check
            if not self._authed():
                self._send(401, {"error": "Unauthorized (no session)"})
                return
            if STATE["auth_state"] == "missing":
                self._send(501, {"error": "no admin password configured"})
                return
            if not STATE["admin_password"] or body.get("password") != STATE["admin_password"]:
                self._send(401, {"error": "Unauthorized"})
                return
            key = "gen-key-%d" % STATE["apikey_posts"]
            STATE["valid_keys"].add(key)
            self._send(200, {"key": key})
            return

        if self.path == "/api/auth/login":
            STATE["logins"] += 1
            STATE["auth_calls"].append("login")
            body = self._body()
            if body.get("password") and body["password"] == STATE["admin_password"]:
                STATE["session_cookie"] = "session-%d" % STATE["logins"]
                self._send(200, {"ok": True},
                           cookie=f'auth={STATE["session_cookie"]}; Path=/; HttpOnly')
            else:
                self._send(401, {"error": "Invalid password"})
            return

        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        body = self._body()
        if self.path.startswith("/api/config/test/vehicle/merge/"):
            STATE["tests"].append(body)
            self._send(200, {"vehicleSoc": {"value": 80}})
        else:
            self._send(404, {"error": "not found"})

    # ---------------------------------------------------------- PUT

    def do_PUT(self):  # noqa: N802
        if self.path == "/api/auth/password":
            body = self._body()
            if STATE["admin_password"] and body.get("current") != STATE["admin_password"]:
                self._send(400, {"error": "Invalid password"})
                return
            created = not STATE["admin_password"]
            STATE["admin_password"] = body.get("new", "")
            STATE["auth_state"] = "configured"
            STATE["password_puts"].append(body)
            self._send(201 if created else 202, {"ok": True})
            return

        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        body = self._body()
        STATE["writes"].append((self.path, body))
        self._send(200, {"id": int(self.path.rsplit("/", 1)[1])})


def start_fake():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


SERVER = start_fake()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"


def write_options(workdir, extra=None):
    opts = {
        "mode": "api",
        "brand": "peugeot",
        "account_user": "konto@example.org",
        "account_password": "secret",
        "vehicles": [{"name": "e3008", "vin": "VF3AAAA11111"},
                     {"name": "e5008", "vin": "VF3BBBB22222"}],
        "evcc_url": BASE,
        "evcc_apikey": API_KEY,
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
    path = os.path.join(workdir, "options.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(opts, fh)
    return opts


def run_orchestrator(workdir, extra=None, args=("--once",)):
    """Starts the orchestrator as a subprocess; logs go to stderr."""
    write_options(workdir, extra)
    env = dict(os.environ)
    env["PSA_OPTIONS_FILE"] = os.path.join(workdir, "options.json")
    env["DATA_DIR"] = os.path.join(workdir, "data")
    os.makedirs(env["DATA_DIR"], exist_ok=True)
    proc = subprocess.run(
        [sys.executable, os.path.join(ADDON, "orchestrator.py"), *args],
        capture_output=True, text=True, env=env, timeout=180,
    )
    proc.out = proc.stdout + proc.stderr
    return proc


def write_stub(workdir):
    with open(os.path.join(workdir, "stub-helper.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "import json, sys\n"
            "print(json.dumps({'access_token': 'NEW-access-' + 'x'*40,\n"
            "                  'refresh_token': 'NEW-refresh-' + 'y'*40,\n"
            "                  'token_type': 'Bearer', 'expires_in': 3599}))\n"
        )


def new_workdir(prefix):
    workdir = tempfile.mkdtemp(prefix=prefix)
    write_stub(workdir)
    return workdir


def key_file(workdir):
    return os.path.join(workdir, "data", "evcc_apikey")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------- Tests

def test_dry_run_writes_payloads_only():
    reset_fake()
    workdir = new_workdir("psa-dry-")
    proc = run_orchestrator(workdir, {"dry_run": True})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    check(not STATE["writes"], "dry run wrote anyway!")
    check(not STATE["tests"], "dry run made the test call")
    files = [f for f in os.listdir(os.path.join(workdir, "data")) if f.startswith("dryrun-")]
    check(len(files) == 2, f"expected 2 dry-run files, got {files}")
    payload = json.load(open(os.path.join(workdir, "data", files[0]), encoding="utf-8"))
    check(payload["accessToken"].startswith("<"), "token not masked in the dry-run file")
    check("config" not in payload, f"payload nested: {list(payload)[:8]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_dry_run_success_clears_previous_failure():
    """Lesson from practice (01.10.2026): after a successful dry run, the state
    still held the old error (last_error, fails, wait) - that looked like a stuck
    run and misled the success check."""
    reset_fake()
    workdir = new_workdir("psa-dry-clean-")
    daten = os.path.join(workdir, "data")
    os.makedirs(daten, exist_ok=True)
    with open(os.path.join(daten, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({"last_error": "old error", "fails": 3, "next_attempt_ts": 9e9,
                   "last_version": "1.0.0"}, fh)
    proc = run_orchestrator(workdir, {"dry_run": True})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    state = json.load(open(os.path.join(daten, "state.json"), encoding="utf-8"))
    check(state.get("last_dry_run_ts"), f"success not recorded: {state}")
    check(not state.get("last_error"), f"old error still in the state: {state}")
    check(state.get("fails") == 0, f"fails not reset: {state}")
    check(float(state.get("next_attempt_ts") or 0) == 0,
          f"wait not cleared: {state}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_api_writes_both_vehicles():
    reset_fake()
    workdir = new_workdir("psa-api-")
    proc = run_orchestrator(workdir, {"dry_run": False})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-2000:]}")
    check(len(STATE["writes"]) == 2, f"expected 2 PUTs: {STATE['writes']}")
    paths = sorted(p for p, _ in STATE["writes"])
    check(paths == ["/api/config/devices/vehicle/5", "/api/config/devices/vehicle/9"],
          f"wrong paths: {paths}")
    for _path, body in STATE["writes"]:
        check(body["accessToken"] == "NEW-access-" + "x" * 40, "accessToken wrong")
        check("#" in body["user"], f"user not rotated: {body['user']}")
    users = {body["user"] for _p, body in STATE["writes"]}
    check(len(users) == 1, f"both vehicles must have the same user key: {users}")
    check(len(STATE["tests"]) == 2, f"test calls missing: {STATE['tests']}")
    state = json.load(open(os.path.join(workdir, "data", "state.json"), encoding="utf-8"))
    check(state.get("last_mode") == "api", f"state: {state}")
    check(state.get("fails") == 0, f"fails != 0: {state}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_api_key_generated_saved_and_reused():
    reset_fake(admin_password="correct", valid_keys=set())
    workdir = new_workdir("psa-keygen-")

    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "correct"})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    check(STATE["apikey_posts"] == 1, f"expected 1 key generation, had {STATE['apikey_posts']}")
    check("newly generated" in proc.out, f"key origin not logged:\n{proc.out[-800:]}")

    path = key_file(workdir)
    check(os.path.exists(path), f"key was not stored ({path})")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    check(mode == 0o600, f"wrong permissions on the key file: {oct(mode)}")
    stored = open(path, encoding="utf-8").read().strip()
    check(stored in STATE["valid_keys"], "stored key is unknown to the fake evcc")

    # second run: key from the file, no new generation
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "correct"})
    check(proc.returncode == 0, f"Exit 2nd run {proc.returncode}\n{proc.out[-1200:]}")
    check(STATE["apikey_posts"] == 1, "key was unnecessarily newly generated")
    check("stored key" in proc.out, f"source not logged:\n{proc.out[-600:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_stored_key_rejected_is_regenerated():
    reset_fake(admin_password="correct", valid_keys=set())
    workdir = new_workdir("psa-keyrotate-")
    run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                               "evcc_admin_password": "correct"})
    first = open(key_file(workdir), encoding="utf-8").read().strip()

    # evcc "forgets" the key (e.g. after restoring a backup)
    STATE["valid_keys"] = set()
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "correct"})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    check("rejected" in proc.out, "no hint about the rejected key")
    second = open(key_file(workdir), encoding="utf-8").read().strip()
    check(second != first, "no new key was stored")
    check(second in STATE["valid_keys"], "new key not valid with the fake evcc")
    shutil.rmtree(workdir, ignore_errors=True)


def test_wrong_admin_password_gives_clear_error():
    reset_fake(admin_password="correct", valid_keys=set())
    workdir = new_workdir("psa-keywrong-")
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "wrong"})
    check(proc.returncode != 0, "wrong password led to exit 0")
    check("login to evcc failed" in proc.out.lower() and "evcc_admin_password" in proc.out,
          f"message does not name the cause:\n{proc.out[-1200:]}")
    check(not os.path.exists(key_file(workdir)), "a key was stored despite the error")
    shutil.rmtree(workdir, ignore_errors=True)


def test_missing_admin_password_hint_and_creation():
    # a) evcc without admin password, option not enabled -> clear instructions
    reset_fake(auth_state="missing", admin_password="", valid_keys=set())
    workdir = new_workdir("psa-nopw-")
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "new12345",
                                      "evcc_set_admin_password": False})
    check(proc.returncode != 0, "missing admin password led to exit 0")
    check("admin password" in proc.out and "evcc_set_admin_password" in proc.out,
          f"hint missing:\n{proc.out[-1200:]}")

    # b) option enabled -> create password and generate key
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "new12345",
                                      "evcc_set_admin_password": True})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    check(len(STATE["password_puts"]) == 1, f"password was not set: {STATE['password_puts']}")
    check(STATE["password_puts"][0]["new"] == "new12345", "wrong password set")
    check(STATE["apikey_posts"] == 1 and os.path.exists(key_file(workdir)),
          "key was not generated/stored after creating the password")
    shutil.rmtree(workdir, ignore_errors=True)


def test_apikey_requires_session_like_evcc():
    """Regression: evcc puts POST /api/auth/apikey behind EnsureAuthHandler.

    Without a prior login, evcc replies with 401 - regardless of the password.
    So the add-on must log in first and then generate the key.
    """
    reset_fake(admin_password="correct", valid_keys=set())
    workdir = new_workdir("psa-session-")
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "correct"})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    check(STATE["auth_calls"][:2] == ["login", "apikey"],
          f"order wrong: {STATE['auth_calls']}")
    check(os.path.exists(key_file(workdir)), "no key stored")
    check("logged in" in proc.out, f"login not logged:\n{proc.out[-800:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_session_fallback_keeps_api_mode_working():
    """Regression: if key generation fails, the run must continue via session.

    Previously the client got no credentials without a key -> all further calls
    ran into 401 ("API mode not available").
    """
    reset_fake(admin_password="correct", valid_keys=set(), failing="/api/auth/apikey")
    workdir = new_workdir("psa-sessiononly-")
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "correct"})
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-2000:]}")
    check("config mode: api" in proc.out, "API mode was not detected")
    check(not STATE["password_puts"], "password unexpectedly set")
    files = [f for f in os.listdir(os.path.join(workdir, "data")) if f.startswith("dryrun-")]
    check(len(files) == 2, f"dry run did not complete: {files}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_vin_mismatch_is_warned_not_fatal():
    reset_fake()
    workdir = new_workdir("psa-vin-")
    proc = run_orchestrator(workdir, {"dry_run": False,
                                      "vehicles": [{"name": "e3008", "vin": "VF3FALSCH"}]})
    check("VF3FALSCH" in proc.out, f"VIN warning missing:\n{proc.out[-1500:]}")
    check(proc.returncode == 0, f"Exit {proc.returncode}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_unknown_vehicle_fails_cleanly():
    reset_fake()
    workdir = new_workdir("psa-unknown-")
    proc = run_orchestrator(workdir, {"dry_run": False, "vehicles": [{"name": "nonexistent"}]})
    check(proc.returncode != 0, "unknown vehicle led to exit 0")
    check("nonexistent" in proc.out, f"error message missing:\n{proc.out[-1500:]}")
    check(not STATE["writes"], "it wrote despite the error")
    check("e3008" in proc.out, "hint about existing vehicles missing")
    shutil.rmtree(workdir, ignore_errors=True)


def test_wrong_apikey_fails_cleanly():
    reset_fake()
    workdir = new_workdir("psa-401-")
    proc = run_orchestrator(workdir, {"dry_run": False, "evcc_apikey": "wrong",
                                      "evcc_apikey_auto": False})
    check(proc.returncode != 0, "wrong API key led to exit 0")
    check("API key" in proc.out, f"message: {proc.out[-1500:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_missing_psa_account_waits_instead_of_exit():
    reset_fake()
    workdir = new_workdir("psa-noaccount-")
    write_options(workdir, {"account_user": None, "account_password": None})
    env = dict(os.environ)
    env["PSA_OPTIONS_FILE"] = os.path.join(workdir, "options.json")
    env["DATA_DIR"] = os.path.join(workdir, "data")
    os.makedirs(env["DATA_DIR"], exist_ok=True)
    # without a PSA account the process keeps running -> terminate it itself after 8 s
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ADDON, "orchestrator.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        proc.wait(timeout=8)
        raise AssertionError(f"add-on exited (exit {proc.returncode})")
    except subprocess.TimeoutExpired:
        proc.terminate()          # terminate first, otherwise reading the pipe blocks
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        check("WAITING FOR CONFIGURATION" in (out or ""), f"hint missing:\n{(out or '')[-800:]}")
    shutil.rmtree(workdir, ignore_errors=True)


def test_diagnose_mode_reports_key_source():
    reset_fake(admin_password="correct", valid_keys=set())
    workdir = new_workdir("psa-diag-")
    proc = run_orchestrator(workdir, {"dry_run": True, "evcc_apikey": None,
                                      "evcc_admin_password": "correct"}, args=("--dump",))
    check(proc.returncode == 0, f"Exit {proc.returncode}\n{proc.out[-1500:]}")
    data = json.loads(proc.stdout)
    check(data["mode_resolved"] == "api", f"mode_resolved: {data.get('mode_resolved')}")
    check("api_key_source" in data, "api_key_source missing in the diagnostics")
    check(data["psa_account_configured"] is True, "PSA account not recognized as configured")
    check(len(data["evcc_db_vehicles"]) == 2, "vehicle list missing")
    planned = data["vehicle_details"]["e3008"]["put_body_planned"]
    check(planned["accessToken"] == "<new-access>", "planned payload wrong")
    shutil.rmtree(workdir, ignore_errors=True)


def test_detect_reason_uses_evcc_log():
    """detect_reason() must find the token error in the evcc log (without action)."""
    reset_fake()
    workdir = new_workdir("psa-detect-")
    opts = write_options(workdir, {"dry_run": True})
    STATE["log"] = ("evcc 0.316.1\n[main ] ERROR creating vehicle car2 failed: "
                    "cannot create vehicle type 'template': cannot create vehicle type "
                    "'peugeot': token expired\n")
    code = (
        "import os, sys;"
        f"sys.path.insert(0,{ADDON!r});"
        "import orchestrator as o;"
        "opts=o.load_options();"
        "sup=o.Supervisor(opts);"
        "r=o.Refresher(opts,sup);"
        "print('REASON=' + str(r.detect_reason()))"
    )
    env = dict(os.environ)
    env["PSA_OPTIONS_FILE"] = os.path.join(workdir, "options.json")
    env["DATA_DIR"] = os.path.join(workdir, "data")
    os.makedirs(env["DATA_DIR"], exist_ok=True)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=60)
    out = proc.stdout + proc.stderr
    check("REASON=token error detected in the evcc log" in out, f"output: {out[-800:]}")
    check(opts["evcc_url"] == BASE, "test config does not point at the fake evcc")
    STATE["log"] = ""
    shutil.rmtree(workdir, ignore_errors=True)


def test_unknown_flag_is_rejected_without_starting_daemon():
    reset_fake()
    workdir = new_workdir("psa-flag-")
    proc = run_orchestrator(workdir, {"dry_run": True}, args=("--detect",))
    check(proc.returncode == 2, f"exit {proc.returncode} instead of 2\n{proc.out[-800:]}")
    check("unknown option" in proc.out, f"message missing: {proc.out[-500:]}")
    shutil.rmtree(workdir, ignore_errors=True)


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
