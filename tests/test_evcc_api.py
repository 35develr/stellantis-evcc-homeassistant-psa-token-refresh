#!/usr/bin/env python3
"""Self-test for evcc_api.py against a local fake evcc server.

It does NOT test the real evcc, but rather how we build the calls:
URL paths, auth headers, payload shape (flat/nested), vehicle matching,
error handling. Runs without dependencies and without network.

Usage: python3 tests/test_evcc_api.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "psa-token-refresh"))

from evcc_api import EvccApi, EvccApiError  # noqa: E402

API_KEY = "testkey123"
STATE = {"writes": [], "tests": [], "auth_seen": [], "dirty": True}

VEHICLE_LIST = [
    {"name": "db:5", "type": "template", "id": 5,
     "config": {"title": "e5008", "template": "peugeot", "user": "konto@example.org",
                "accessToken": "***", "refreshToken": "***", "vin": "VF3BBBB22222"}},
    {"name": "db:9", "type": "template", "id": 9,
     "config": {"title": "e3008", "template": "peugeot", "user": "konto@example.org",
                "accessToken": "***", "refreshToken": "***", "vin": "VF3AAAA11111"}},
]


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        auth = self.headers.get("Authorization", "")
        STATE["auth_seen"].append(auth)
        return auth == f"Bearer {API_KEY}"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, *args):  # stay quiet
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/api/state":        # allowed without auth (as in real evcc)
            if STATE.get("ready_in", 0) > 0:
                STATE["ready_in"] -= 1
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"version": "0.316.1",
                                 "loadpoints": [{"title": "Wallbox", "charging": False}]})
            return
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path == "/api/config/devices/vehicle":
            self._send(200, VEHICLE_LIST)
        elif self.path == "/api/config/devices/vehicle/9":
            self._send(200, VEHICLE_LIST[1])
        elif self.path == "/api/config/dirty":
            self._send(200, {"dirty": STATE["dirty"]})
        elif self.path == "/api/state":
            # "not yet ready": as long as the port is still answered by Home Assistant,
            # no evcc response arrives here - modelled as 404.
            if STATE.get("ready_in", 0) > 0:
                STATE["ready_in"] -= 1
                self._send(404, {"error": "not found"})
            else:
                self._send(200, {"version": "0.316.1",
                                 "loadpoints": [{"title": "Wallbox", "charging": False}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        body = self._body()
        if self.path.startswith("/api/config/test/vehicle/merge/"):
            STATE["tests"].append(body)
            if body.get("accessToken") == "BAD":
                self._send(400, {"error": "cannot create vehicle type 'peugeot': token expired"})
            else:
                self._send(200, {"vehicleSoc": {"value": 80}, "vehicleRange": {"value": 340}})
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self):  # noqa: N802
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        body = self._body()
        if self.path == "/api/config/devices/vehicle/9":
            STATE["writes"].append(body)
            self._send(200, {"id": 9})
        else:
            self._send(404, {"error": "not found"})


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_wait_until_ready_waits_for_the_port():
    """After start, evcc needs a few seconds; until then Home Assistant answers
    the port with foreign responses (404). A write attempt in this window failed
    seemingly for no reason (run of 01.10.2026 with dry_run=false)."""
    STATE["ready_in"] = 2                      # the first two fetches are still "proxy"
    api = EvccApi(BASE, apikey="K", timeout=5)
    bereit, meldung = api.wait_until_ready(timeout=20, interval=0.2)
    check(bereit is True, f"evcc not recognized as ready: {meldung}")
    check("Version" in meldung or "0.316.1" in meldung, f"version missing: {meldung}")
    check(STATE["ready_in"] == 0, "the wait did not consume the state")
    check(api.version() == "0.316.1", f"version wrong: {api.version()}")

    api2 = EvccApi("http://127.0.0.1:1", timeout=2)     # nothing listens here
    bereit2, meldung2 = api2.wait_until_ready(timeout=1, interval=0.3)
    check(bereit2 is False, "unreachable evcc was reported as ready")
    check("Home Assistant" in meldung2, f"cause missing in the message: {meldung2}")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


SERVER = start_server()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"


def client(**kw):
    return EvccApi(BASE, **kw)


def test_auth_required():
    api = client()
    try:
        api.ensure_auth()
    except EvccApiError as exc:
        check("API key" in str(exc), f"hint about the API key missing: {exc}")
        return
    raise AssertionError("ensure_auth() should have failed")


def test_wrong_apikey_message():
    api = client(apikey="wrong")
    try:
        api.ensure_auth()
    except EvccApiError as exc:
        check("401" in str(exc), f"HTTP status missing in message: {exc}")
        return
    raise AssertionError("wrong API key was accepted")


def test_find_vehicle_by_title():
    api = client(apikey=API_KEY)
    vid, detail, hint = api.find_vehicle("e3008")
    check(vid == 9, f"wrong ID: {vid}")
    check(detail["config"]["vin"] == "VF3AAAA11111", "VIN not in the details")
    check("title" in hint, f"unexpected hint: {hint}")


def test_find_vehicle_unknown():
    api = client(apikey=API_KEY)
    try:
        api.find_vehicle("nonexistent")
    except EvccApiError as exc:
        check("e3008" in str(exc) and "e5008" in str(exc), "existing vehicles missing in the error")
        return
    raise AssertionError("unknown vehicle was found")


def test_payload_is_flat_and_rotates_user():
    api = client(apikey=API_KEY)
    _vid, detail, _h = api.find_vehicle("e3008")
    payload = api.build_payload(detail, "NEW-access", "NEW-refresh", generation=202609291200)
    check("config" not in payload, "payload contains nested 'config'")
    check("id" not in payload, "payload contains 'id'")
    check(payload["accessToken"] == "NEW-access", "accessToken not set")
    check(payload["refreshToken"] == "NEW-refresh", "refreshToken not set")
    check(payload["user"] == "konto@example.org#202609291200", f"user: {payload['user']}")
    check(payload["vin"] == "VF3AAAA11111", "VIN lost")
    check(payload["template"] == "peugeot", "template missing")
    check(payload["title"] == "e3008", "title missing")


def test_payload_nested_variant():
    api = client(apikey=API_KEY)
    _vid, detail, _h = api.find_vehicle("e3008")
    payload = api.build_payload(detail, "a", "r", generation=1, nested=True)
    check("config" in payload and payload["config"]["accessToken"] == "a", "nested wrong")
    check("accessToken" not in payload, "flat tokens in the nested payload")


def test_user_rotation_is_not_doubled():
    api = client(apikey=API_KEY)
    _vid, detail, _h = api.find_vehicle("e3008")
    detail = json.loads(json.dumps(detail))
    detail["config"]["user"] = "konto@example.org#111"
    payload = api.build_payload(detail, "a", "r", generation=222)
    check(payload["user"] == "konto@example.org#222", f"user: {payload['user']}")


def test_test_then_update_roundtrip():
    api = client(apikey=API_KEY)
    vid, detail, _h = api.find_vehicle("e3008")
    payload = api.build_payload(detail, "NEW-access", "NEW-refresh", generation=42)
    ok, info = api.test_vehicle(vid, payload)
    check(ok, f"test failed: {info}")
    check("vehicleSoc" in info, f"test response unexpected: {info}")
    ok, info = api.update_vehicle(vid, payload)
    check(ok, f"PUT failed: {info}")
    written = STATE["writes"][-1]
    check(written["accessToken"] == "NEW-access", "wrong token written")
    check(written["user"] == "konto@example.org#42", "user not rotated")
    check(written["refreshToken"] == "NEW-refresh", "refresh not written")


def test_test_reports_rejection():
    api = client(apikey=API_KEY)
    vid, detail, _h = api.find_vehicle("e3008")
    payload = api.build_payload(detail, "BAD", "r", generation=1)
    ok, info = api.test_vehicle(vid, payload)
    check(not ok, "rejected configuration was reported as ok")
    check("token expired" in info, f"message missing: {info}")


def test_auth_header_used_on_writes():
    STATE["auth_seen"].clear()
    api = client(apikey=API_KEY)
    vid, detail, _h = api.find_vehicle("e3008")
    api.update_vehicle(vid, api.build_payload(detail, "a", "r", generation=1))
    check(all(h.startswith("Bearer ") for h in STATE["auth_seen"]), "Bearer header missing")


def test_dirty_flag_readable():
    api = client(apikey=API_KEY)
    check(api.config_dirty() is True, "dirty flag not read")


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
