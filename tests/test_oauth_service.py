#!/usr/bin/env python3
"""Self-test for the local login service (`POST /oauth-code`).

The Home Assistant integration "Stellantis Vehicles"
(andreadegiovine/homeassistant-stellantis-vehicles) asks a login service for an
OAuth code: POST {url, email, password} -> {"code": ...}. By default that is a
shared Render.com worker; this add-on now offers the same contract locally.

Covered here:
  1. success - the code from the helper is returned as {"code": ...}
  2. the helper is called with --authorize-url (the caller's URL, not our own)
     and --code-only (no token exchange)
  3. missing fields -> 400 with a message (the integration shows it)
  4. helper failure -> 400, not a silent empty code
  5. oauth_service: false -> 404
  6. the LAN restriction (is_private_address) rejects public sources
  7. the server also starts when no trigger_token is set (login service only)

Usage: python3 tests/test_oauth_service.py
"""

import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "psa-token-refresh")
sys.path.insert(0, ADDON)

TMP = tempfile.mkdtemp(prefix="oauth-service-")
os.environ["DATA_DIR"] = TMP

import orchestrator  # noqa: E402

FAILS = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


STUB = os.path.join(TMP, "stub-helper.py")
with open(STUB, "w", encoding="utf-8") as fh:
    fh.write(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['STUB_ARGV'], 'w') as fh:\n"
        "    json.dump(sys.argv[1:], fh)\n"
        "if os.environ.get('STUB_FAIL'):\n"
        "    print('boom', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('THE-CODE-123')\n"
    )
os.chmod(STUB, os.stat(STUB).st_mode | stat.S_IEXEC)

ARGV_FILE = os.path.join(TMP, "argv.json")
os.environ["STUB_ARGV"] = ARGV_FILE


def make_opts(**kw):
    opts = {
        "brand": "peugeot",
        "helper_cmd": f"{sys.executable} {STUB}",
        "helper_deadline": 30,
        "browser_backend": "playwright",
        "trigger_port": 0,
        "trigger_token": "",
        "oauth_service": True,
    }
    opts.update(kw)
    return opts


def start(opts):
    refresher = orchestrator.Refresher(opts, orchestrator.Supervisor(opts))
    server = orchestrator.start_trigger_server(refresher, opts)
    if server is None:
        return None, None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(base, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


AUTHORIZE = ("https://idpcvs.peugeot.com/am/oauth2/authorize?client_id=abc"
             "&response_type=code&redirect_uri=mymap%3A%2F%2Foauth2redirect%2Fde")

print("== 1/2 success and helper arguments")
server, base = start(make_opts())
check("server started without trigger_token", server is not None)
status, body = post(base, "/oauth-code",
                    {"url": AUTHORIZE, "email": "konto@example.org", "password": "geheim"})
check("HTTP 200", status == 200, f"got {status} {body}")
check("code returned", body.get("code") == "THE-CODE-123", str(body))
argv = json.load(open(ARGV_FILE, encoding="utf-8"))
check("--authorize-url passed", "--authorize-url" in argv and AUTHORIZE in argv, str(argv))
check("--code-only passed", "--code-only" in argv, str(argv))
check("credentials passed", "konto@example.org" in argv and "geheim" in argv, str(argv))
server.shutdown()

print("== 3 missing fields")
server, base = start(make_opts())
status, body = post(base, "/oauth-code", {"url": AUTHORIZE, "email": "konto@example.org"})
check("HTTP 400", status == 400, f"got {status}")
check("message names the missing field", "password" in str(body.get("message", "")), str(body))
status, body = post(base, "/oauth-code", {"url": "http://evil.example/x",
                                          "email": "a@b.c", "password": "x"})
check("non-https URL rejected", status == 400, f"got {status} {body}")
server.shutdown()

print("== 4 helper failure")
os.environ["STUB_FAIL"] = "1"
server, base = start(make_opts())
status, body = post(base, "/oauth-code",
                    {"url": AUTHORIZE, "email": "konto@example.org", "password": "geheim"})
check("HTTP 400 on helper failure", status == 400, f"got {status}")
check("message mentions the failure", "login failed" in str(body.get("message", "")), str(body))
check("no code in the error body", "code" not in body or body.get("code") == 400, str(body))
server.shutdown()
os.environ.pop("STUB_FAIL", None)

print("== 5 oauth_service off")
server, base = start(make_opts(oauth_service=False))
check("no server at all when trigger_token is empty too", server is None)
server, base = start(make_opts(oauth_service=False, trigger_token="tok"))
status, body = post(base, "/oauth-code",
                    {"url": AUTHORIZE, "email": "konto@example.org", "password": "geheim"})
check("HTTP 404 when disabled", status == 404, f"got {status} {body}")
server.shutdown()

print("== 6 LAN restriction")
check("127.0.0.1 is private", orchestrator.is_private_address("127.0.0.1"))
check("192.168.10.10 is private", orchestrator.is_private_address("192.168.10.10"))
check("172.30.32.2 is private", orchestrator.is_private_address("172.30.32.2"))
check("8.8.8.8 is not private", not orchestrator.is_private_address("8.8.8.8"))
check("garbage is not private", not orchestrator.is_private_address("nope"))

print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
    sys.exit(1)
print("All checks passed.")
