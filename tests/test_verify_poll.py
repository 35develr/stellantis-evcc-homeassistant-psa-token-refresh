#!/usr/bin/env python3
"""Verification of a renewal: wait for the first PSA poll, then judge the evcc log.

Background (report in #3, log of 2026-10-19): the add-on wrote the tokens, slept a fixed
45 s, searched three patterns in the evcc log and reported "successfully verified" - while
evcc had not necessarily polled PSA at all, and while `invalid_token`,
`authentication failed` and `401 Unauthorized` were not looked at.

This test drives `_verify_api` against a fake evcc:

  * a loadpoint that reports vehicle data -> the poll is the proof, no fixed sleep
  * no loadpoint with the vehicle      -> "nothing to observe", log check instead
  * a new PSA token error in the log    -> the renewal is red
  * a foreign 401 (tariff/MQTT)         -> still a success

Usage: python3 tests/test_verify_poll.py
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "psa-token-refresh")

TMP = tempfile.mkdtemp(prefix="verify-poll-")
os.environ["DATA_DIR"] = TMP
sys.path.insert(0, ADDON)

import orchestrator as o  # noqa: E402
from evcc_api import EvccApi  # noqa: E402

FAILS = []
STATE = {"loadpoints": [], "log": ""}


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


class Handler(BaseHTTPRequestHandler):
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

    def do_GET(self):  # noqa: N802
        if self.path == "/api/state":
            self._send(200, {"version": "0.317.2", "loadpoints": STATE["loadpoints"]})
        elif self.path.startswith("/api/system/log"):
            self._send(200, STATE["log"])
        else:
            self._send(404, {"error": "unknown path", "path": self.path})


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"


class Sup:
    """Supervisor substitute - only the notification matters here."""

    def __init__(self):
        self.notes = []

    def notify(self, title, message):
        self.notes.append((title, message))


def refresher(**opts):
    r = o.Refresher.__new__(o.Refresher)      # no __init__: no supervisor/network
    r.opts = {"verify_poll_timeout_seconds": 0, "verify_delay_seconds": 10}
    r.opts.update(opts)
    r.sup = Sup()
    return r


def api():
    return EvccApi(BASE, apikey="testkey")


def resolved(names=("e3008",)):
    return [({"name": name, "vin": ""}, 9, {"config": {"title": name}}, "by name")
            for name in names]


def verify(r, original_log="evcc started\n", restart_done=False):
    return r._verify_api(api(), {}, "proactive renewal", resolved(), restart_done, original_log)


# --------------------------------------------------------------------------- tests

def test_poll_observed_is_the_proof():
    STATE["loadpoints"] = [{"title": "Wallbox", "vehicleTitle": "e3008",
                            "vehicleName": "db:9", "vehicleSoc": 71,
                            "vehicleRange": 310, "vehicleOdometer": 41230}]
    STATE["log"] = "evcc started\n"
    r = refresher(verify_poll_timeout_seconds=30)
    ok = verify(r, restart_done=True)          # restart_done: no grace must be slept
    check(ok is True, "a poll with vehicle data must count as verified")
    notes = " ".join(m for _t, m in r.sup.notes)
    check("PSA poll observed" in notes, f"notification does not mention the poll: {notes}")
    saved = json.load(open(os.path.join(TMP, "state.json"), encoding="utf-8"))
    check(saved.get("last_verify", {}).get("psa_poll_observed") is True,
          f"state.last_verify: {saved.get('last_verify')}")


def test_no_vehicle_on_a_loadpoint_falls_back_to_the_log():
    STATE["loadpoints"] = [{"title": "Wallbox", "charging": False}]
    STATE["log"] = "evcc started\n"
    r = refresher()
    check(verify(r) is True, "without an observable poll the log check decides")
    notes = " ".join(m for _t, m in r.sup.notes)
    check("verified via evcc log" in notes,
          f"notification must say it could not observe a poll: {notes}")


def test_new_token_error_for_the_vehicle_is_red():
    STATE["loadpoints"] = [{"title": "Wallbox", "vehicleTitle": "e3008",
                            "vehicleSoc": 71}]
    STATE["log"] = ("evcc started\n"
                    "[psa ] ERROR e3008: request failed: 401 Unauthorized\n")
    r = refresher()
    try:
        verify(r)
    except RuntimeError as exc:
        check("401 Unauthorized" in str(exc), f"the pattern is not named: {exc}")
    else:
        check(False, "a new PSA 401 was accepted as success")


def test_invalid_token_is_detected_at_all():
    STATE["loadpoints"] = []
    STATE["log"] = ("evcc started\n"
                    "[db:9] ERROR token refresh failed: invalid_token\n")
    try:
        verify(refresher())
    except RuntimeError as exc:
        check("invalid_token" in str(exc), f"invalid_token not named: {exc}")
    else:
        check(False, "invalid_token was not detected (the old tuple knew only three patterns)")


def test_foreign_401_is_not_a_token_error():
    STATE["loadpoints"] = []
    STATE["log"] = ("evcc started\n"
                    "[tariff] ERROR grid price: 401 Unauthorized (api.octopus)\n")
    check(verify(refresher()) is True,
          "a 401 from a tariff must not turn the renewal red")


def test_old_errors_do_not_count_again():
    STATE["loadpoints"] = []
    baseline = ("evcc started\n"
                "[main ] ERROR creating vehicle failed: invalid_grant\n")
    STATE["log"] = baseline
    check(verify(refresher(), original_log=baseline) is True,
          "an error that was already in the log before the write must be ignored")


def test_pattern_helper_is_line_scoped():
    check(o.new_token_errors("", "[psa] ERROR authentication failed\n") ==
          ["authentication failed"], "PSA context must be recognised")
    check(o.new_token_errors("", "[mqtt] ERROR authentication failed\n") == [],
          "a foreign 'authentication failed' must be ignored")
    check(o.new_token_errors("", "cannot create vehicle type 'template'") ==
          ["cannot create vehicle type 'template'"],
          "the unambiguous patterns must count without context")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
