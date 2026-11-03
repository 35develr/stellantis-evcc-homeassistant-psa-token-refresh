#!/usr/bin/env python3
"""Self-test for the Supervisor discovery announcement.

Home Assistant turns `POST http://supervisor/discovery` with
{"service": "stellantis_vehicles", "config": {"host": ..., "port": ...}} into a
config flow with source "hassio". Once the integration implements
`async_step_hassio`, host and port pre-fill its "Login service URL" - the user no
longer types the address.

Covered here:
  1. hostname from /addons/self/info + trigger port are announced
  2. the payload carries service, host and port - and nothing else
  3. no hostname from the Supervisor -> no announcement, a warning instead
  4. the Supervisor rejects the announcement (403/500) -> no crash
  5. outside a Supervisor (no SUPERVISOR_TOKEN) -> no request at all

Usage: python3 tests/test_discovery_announce.py
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "psa-token-refresh")
sys.path.insert(0, ADDON)

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="discovery-")

import orchestrator  # noqa: E402

FAILS = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}" + (f" -> {detail}" if detail else ""))


class FakeHTTP:
    """Replacement for orchestrator.http_request that records every call."""

    def __init__(self, info_status=200, info_body=None, discovery_status=200):
        self.calls = []
        self.info_status = info_status
        self.info_body = info_body if info_body is not None else {
            "result": "ok", "data": {"hostname": "0e0578fd-psa-token-refresh",
                                     "slug": "psa_token_refresh", "version": "1.8.2"}}
        self.discovery_status = discovery_status

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30):
        self.calls.append({"url": url, "method": method, "body": body,
                           "headers": headers, "timeout": timeout})
        if url.endswith("/addons/self/info"):
            return self.info_status, json.dumps(self.info_body)
        if url.endswith("/discovery"):
            return self.discovery_status, '{"result": "ok"}'
        return 404, "not found"


def make_supervisor(fake, token="test-token"):
    if token:
        os.environ["SUPERVISOR_TOKEN"] = token
    else:
        os.environ.pop("SUPERVISOR_TOKEN", None)
    orchestrator.http_request = fake
    return orchestrator.Supervisor({"trigger_port": 8909})


print("discovery: anhaengen an den Supervisor")
fake = FakeHTTP()
sup = make_supervisor(fake)
sup.announce_discovery(8909)

check("zwei Aufrufe (self/info, discovery)", len(fake.calls) == 2, str(fake.calls))
check("erster Aufruf ist /addons/self/info",
      fake.calls[0]["url"].endswith("/addons/self/info"), fake.calls[0]["url"])
check("Authorization-Header gesetzt",
      fake.calls[0]["headers"]["Authorization"] == "Bearer test-token")

post = fake.calls[1]
check("zweiter Aufruf ist POST /discovery",
      post["method"] == "POST" and post["url"].endswith("/discovery"),
      f"{post['method']} {post['url']}")
check("payload nennt den Dienst",
      post["body"]["service"] == "stellantis_vehicles", str(post["body"]))
check("payload nennt Hostname und Port",
      post["body"]["config"] == {"host": "0e0578fd-psa-token-refresh", "port": 8909},
      str(post["body"]))
check("payload enthaelt nichts weiteres",
      set(post["body"]) == {"service", "config"}, str(post["body"]))

print("discovery: Supervisor liefert keinen Hostnamen")
fake = FakeHTTP(info_body={"result": "ok", "data": {"slug": ""}})
sup = make_supervisor(fake)
sup.announce_discovery(8909)
check("kein POST ohne Hostnamen",
      [c["method"] for c in fake.calls] == ["GET"], str([c["method"] for c in fake.calls]))

print("discovery: Supervisor lehnt die Anmeldung ab")
fake = FakeHTTP(discovery_status=403)
sup = make_supervisor(fake)
try:
    sup.announce_discovery(8909)
    crashed = False
except Exception as exc:  # noqa: BLE001
    crashed = exc
check("403 stuerzt nicht ab", crashed is False, str(crashed))
check("POST wurde trotzdem versucht", fake.calls[-1]["method"] == "POST")

print("discovery: kein Supervisor (Betrieb ohne Add-on-Kontext)")
fake = FakeHTTP()
sup = make_supervisor(fake, token="")
sup.announce_discovery(8909)
check("kein einziger Aufruf ohne SUPERVISOR_TOKEN", fake.calls == [], str(fake.calls))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("all checks passed")
