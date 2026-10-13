#!/usr/bin/env python3
"""psa-token-refresh - orchestrator for evcc as a Home Assistant add-on.

What the add-on does (short version):

  1. detects that evcc's PSA tokens are invalid (evcc log) - or is triggered via
     HA automation/HTTP, or proactively renewed after X days
  2. fetches fresh access/refresh tokens via a headless browser login (PSA token helper)
  3. writes them back to evcc - depending on the vehicle configuration:
       Mode "api"   : via the evcc configuration API (vehicles from the evcc UI/DB)
       Mode "yaml"  : directly in evcc.yaml (vehicles from the file)
       Mode "auto"  : detects this itself
  4. restarts evcc when evcc requires a restart, and afterwards checks
     the evcc log for token errors

Important about the order: PSA refresh tokens are single-use, and evcc persists
the rotated token in evcc.db (table settings, key "psa.<brand>.<user>").
Therefore evcc is stopped before fetching tokens and restarted IMMEDIATELY afterwards:
the stop only protects the login window. Writing then happens via the
evcc configuration API, and that needs a RUNNING evcc - the old flow
still wrote during the stop and failed with "Connection refused". The
user field is set to a new generation (suffix "#YYYYMMDDHHMM") so that a
dead persisted token is no longer used at all.

Standard library only (urllib) - the token helper needs PyQt5/QtWebEngine (Dockerfile).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evcc_api import EvccApi, EvccApiError, EvccCredentials  # noqa: E402
from patch_evcc_yaml import PatchError, patch_evcc_yaml  # noqa: E402

LOG_PREFIX = "psa-token-refresh"
VERSION = "1.5.0"
ERROR_PATTERNS = re.compile(
    r"token expired|invalid_grant|invalid_token|cannot create vehicle type 'template'"
    r"|authentication failed|401 Unauthorized"
)


# ---------------------------------------------------------------- basic helpers

def log(msg: str) -> None:
    """Log lines to stderr so the diagnosis JSON on stdout stays clean."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{LOG_PREFIX}] {msg}",
          file=sys.stderr, flush=True)


def data_dir() -> str:
    return os.environ.get("DATA_DIR", "/data")


def load_options() -> dict:
    path = os.environ.get("PSA_OPTIONS_FILE", os.path.join(data_dir(), "options.json"))
    opts: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            opts = json.load(fh)
    except FileNotFoundError:
        log(f"WARNING: {path} not found - using defaults")
    except json.JSONDecodeError as exc:
        log(f"ERROR: {path} is not valid JSON: {exc}")
    defaults = {
        "mode": "auto",
        "brand": "peugeot",
        "account_user": "",
        "account_password": "",
        "country": "de",
        "vehicles": [],
        "evcc_url": "http://127.0.0.1:7070",
        "evcc_apikey": "",
        "evcc_admin_password": "",
        "evcc_user": "",
        "evcc_apikey_auto": True,
        "evcc_set_admin_password": False,
        "evcc_slug": "evcc",
        "evcc_config_path": "auto",
        "evcc_control": "auto",
        "evcc_restart_webhook": "",
        "api_verify_with_test": True,
        "api_payload_nested": False,
        "check_interval_minutes": 60,
        "proactive_refresh_days": 30,
        "retry_backoff_minutes": 30,
        "verify_delay_seconds": 45,
        "evcc_ready_timeout": 150,
        "rotate_user_key": True,
        "skip_while_charging": True,
        "helper_timeout": 45,
        "helper_deadline": 240,
        # own helper (polling, works with the new ForgeRock/Gigya chain);
        "helper_cmd": "python3 /app/psa-browser-token.py",
        "trigger_port": 8909,
        "trigger_token": "",
        "notify": True,
        "dry_run": False,
    }
    defaults.update({k: v for k, v in opts.items() if v is not None})
    return defaults


SENSIBLE_TEILE = ("password", "passwort", "apikey", "api_key", "token", "secret", "pwd")


def maskiere_optionen(options):
    """Replaces values of sensitive options with '***'.

    The Supervisor returns the options in plain text under /addons/self/info - including
    `account_password`, `evcc_admin_password`, `evcc_apikey` and `trigger_token`. Everything
    this endpoint (or /dump) returns goes through this function; key names
    stay visible so you can tell which option is set.
    """
    if not isinstance(options, dict):
        return options
    out = {}
    for key, value in options.items():
        if isinstance(value, (str, int, float, bool)) and \
                any(teil in str(key).lower() for teil in SENSIBLE_TEILE):
            out[key] = "***"
        else:
            out[key] = value
    return out


def state_path() -> str:
    return os.path.join(data_dir(), "state.json")


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    try:
        os.makedirs(data_dir(), exist_ok=True)
        tmp = state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, state_path())
    except Exception as exc:  # noqa: BLE001
        log(f"could not save state: {exc}")


def http_request(url: str, method: str = "GET", body: dict | None = None,
                 headers: dict | None = None, timeout: int = 20,
                 read_limit: int | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(read_limit) if read_limit else resp.read()
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:2000]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


# ------------------------------------------------------------- Supervisor / HA

class Supervisor:
    """Stop/start of the evcc add-on, add-on log and notifications.

    The slug (internal identifier) of the evcc add-on is looked up on first access via
    GET /addons itself - depending on the installation source it is called "evcc",
    "a1b2c3d4-evcc" or similar. An explicitly set `evcc_slug` takes precedence.
    """

    def __init__(self, opts: dict):
        self.opts = opts
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = os.environ.get("SUPERVISOR_URL", "http://supervisor").rstrip("/")
        self.available = bool(self.token)
        self._slug: str | None = None
        self._prefix = "addons"
        self.apps: list[dict] = []

    # -------------------------------------------------------------- API

    def _call(self, path: str, method: str = "GET", body: dict | None = None,
              timeout: int = 30) -> tuple[int, str]:
        if not self.available:
            return 0, "SUPERVISOR_TOKEN not set (not running as an add-on?)"
        return http_request(
            f"{self.base}{path}", method=method, body=body,
            headers={"Authorization": f"Bearer {self.token}"}, timeout=timeout,
        )

    def list_apps(self, reload: bool = False) -> list[dict]:
        """Installed add-ons per Supervisor (for slug lookup and diagnosis)."""
        if self.apps and not reload:
            return self.apps
        if not self.available:
            return []
        for prefix in ("addons", "apps"):
            status, text = self._call(f"/{prefix}")
            if status != 200:
                continue
            try:
                data = json.loads(text)["data"]
            except Exception:  # noqa: BLE001
                continue
            apps = data.get(prefix) or data.get("addons") or data.get("apps") or []
            apps = [a for a in apps if isinstance(a, dict)]
            if apps:
                self._prefix = prefix
                self.apps = apps
                return apps
        return []

    def resolve_slug(self, reload: bool = False) -> str:
        """Determines the slug of the evcc add-on."""
        configured = (self.opts.get("evcc_slug") or "").strip()
        apps = self.list_apps(reload=reload)
        if configured and configured != "auto":
            if not apps or any(str(a.get("slug")) == configured for a in apps):
                self._slug = configured
                return configured
            log(f"WARNING: evcc_slug '{configured}' is not installed per Supervisor - "
                "searching for the evcc add-on automatically")
        if apps:
            names = ", ".join(
                f"{a.get('name', '?')} ({a.get('slug', '?')}, {a.get('state', '?')})" for a in apps
            )
            log(f"Supervisor knows {len(apps)} add-on(s): {names}")

        def score(app: dict) -> int:
            slug = str(app.get("slug", "")).lower()
            name = str(app.get("name", "")).lower()
            if slug == "evcc":
                return 100
            if slug.endswith("evcc") or slug.endswith("_evcc") or slug.endswith("-evcc"):
                return 80
            if name == "evcc":
                return 70
            if "evcc" in slug or "evcc" in name:
                return 50
            return 0

        candidates = sorted(apps, key=score, reverse=True)
        if candidates and score(candidates[0]) > 0:
            slug = candidates[0]["slug"]
            if configured and configured not in ("", "auto") and configured != slug:
                log(f"WARNING: evcc_slug '{configured}' is not installed - "
                    f"using '{slug}'")
            self._slug = slug
            return slug

        # A configured slug the Supervisor does not know (the list above is complete):
        # do not guess. Otherwise 'auto' sees an evcc add-on that does not exist and an
        # evcc running outside Home Assistant is never detected.

        hint = ", ".join(f"{a.get('name', '?')}={a.get('slug', '?')}" for a in apps) or "none"
        raise RuntimeError(
            "evcc add-on not found (or the configured 'evcc_slug' is not installed). "
            "Installed add-ons per Supervisor: "
            f"{hint}. Please set the correct slug as 'evcc_slug' in the options."
        )

    def self_info(self) -> dict:
        """Own add-on info per Supervisor (version, permissions, protection mode).

        The options are masked - the Supervisor returns them in plain text.
        """
        if not self.available:
            return {}
        for prefix in (self._prefix, "addons", "apps"):
            status, text = self._call(f"/{prefix}/self/info", timeout=20)
            if status != 200:
                continue
            try:
                data = json.loads(text).get("data") or {}
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data:
                self._prefix = prefix
                if isinstance(data.get("options"), dict):
                    data["options"] = maskiere_optionen(data["options"])
                return data
        return {}

    def self_diagnostics(self, file_version: str) -> list[str]:
        """Messages about the running image (version, permissions) for the log."""
        info = self.self_info()
        if not info:
            return ["add-on info not available (missing Supervisor access?) - "
                    "check the version in the log above"]
        messages = [
            "add-on (per Supervisor): version %s | hassio_api=%s hassio_role=%s "
            "homeassistant_api=%s host_network=%s protected=%s"
            % (info.get("version"), info.get("hassio_api"), info.get("hassio_role"),
               info.get("homeassistant_api"), info.get("host_network"), info.get("protected"))
        ]
        supervisor_version = info.get("version")
        if supervisor_version and str(supervisor_version) != str(file_version):
            messages.append(
                f"WARNING: image reports version {file_version}, the Supervisor knows "
                f"{supervisor_version} - the running image does not match the state of the "
                "installation (incomplete update or files replaced by hand)."
            )
        if info.get("homeassistant_api") is False:
            messages.append("WARNING: homeassistant_api is NOT granted - "
                            "notifications in Home Assistant fail with 401.")
        if info.get("hassio_api") is False:
            messages.append("WARNING: hassio_api is NOT granted - evcc cannot be "
                            "stopped/started; 'stop_while_changing' has no effect.")
        elif str(info.get("hassio_role") or "").lower() not in ("manager", "admin"):
            messages.append(f"WARNING: hassio_role is '{info.get('hassio_role')}' - "
                            "'manager' is required to stop/start third-party add-ons.")
        return messages

    # -------------------------------------------------------------- state

    def state(self) -> str | None:
        """'started'/'stopped'/... - None if the state cannot be determined."""
        slug = self.resolve_slug()
        status, text = self._call(f"/{self._prefix}/{slug}/info")
        if status != 200:
            log(f"WARNING: state of '{slug}' cannot be determined (HTTP {status}: {text[:160]})")
            return None
        try:
            return json.loads(text)["data"]["state"]
        except Exception:  # noqa: BLE001
            log(f"WARNING: state response for '{slug}' not readable")
            return None

    def _wait_for_state(self, wanted, wait_seconds: int, max_unknown: int = 5) -> bool:
        """Waits until `wanted(state)` is satisfied. None states are tolerated."""
        deadline = time.time() + wait_seconds
        unknown = 0
        while time.time() < deadline:
            state = self.state()
            if state is None:
                unknown += 1
                if unknown > max_unknown:
                    return False
            elif wanted(state):
                return True
            else:
                unknown = 0
            time.sleep(3)
        return False

    def stop_evcc(self, wait_seconds: int = 90) -> bool:
        slug = self.resolve_slug()
        state = self.state()
        if state is None:
            log("ERROR: state of the evcc add-on cannot be determined - aborting, "
                "evcc stays untouched (check slug/permissions)")
            return False
        if state != "started":
            log(f"evcc is not running (state: {state}) - no stop needed")
            return True

        log(f"stopping evcc add-on ('{slug}', state {state}) ...")
        status, text = self._call(f"/{self._prefix}/{slug}/stop", method="POST")
        if status not in (200, 201, 202, 204):
            log(f"ERROR: stop call returned HTTP {status}: {text[:200]}")
            return False
        if self._wait_for_state(lambda s: s != "started", wait_seconds):
            log("evcc is stopped")
            return True
        log(f"ERROR: evcc is still running after {wait_seconds}s - aborting")
        return False

    def start_evcc(self, wait_seconds: int = 180) -> bool:
        slug = self.resolve_slug()
        log(f"starting evcc add-on ('{slug}') ...")
        status, text = self._call(f"/{self._prefix}/{slug}/start", method="POST")
        if status not in (200, 201, 202, 204):
            log(f"WARNING: start call returned HTTP {status}: {text[:200]}")
        if self._wait_for_state(lambda s: s == "started", wait_seconds):
            log("evcc is running again")
            return True
        log(f"WARNING: evcc does not report 'started' after {wait_seconds}s")
        return False

    def restart_evcc(self, stop_wait: int = 90, start_wait: int = 180) -> bool:
        """Stop and start in one go.

        Needed because a 'start' on an already running add-on is a no-op:
        a configuration marked as 'dirty' by the evcc API only becomes
        effective after a real restart.
        """
        if not self.stop_evcc(stop_wait):
            return False
        return self.start_evcc(start_wait)

    def evcc_log(self, limit: int = 60000) -> str:
        try:
            slug = self.resolve_slug()
        except RuntimeError:
            return ""
        status, text = self._call(f"/{self._prefix}/{slug}/logs", timeout=30)
        return text[-limit:] if status == 200 else ""

    # -------------------------------------------------------------- messages

    def notify(self, title: str, message: str, notification_id: str = "psa_token_refresh") -> None:
        if not self.opts.get("notify", True):
            log(f"NOTIFY suppressed: {title}: {message}")
            return
        status, text = self._call(
            "/core/api/services/persistent_notification/create",
            method="POST",
            body={"title": title, "message": message, "notification_id": notification_id},
        )
        if status not in (200, 201):
            hint = ""
            if status in (401, 403):
                hint = (" - for this the add-on needs 'homeassistant_api: true' in its "
                        "config.yaml; alternatively set 'notify: false'")
            log(f"WARNING: notification failed ({status}: {text[:160]}){hint}")


class EvccControl:
    """Stops and starts evcc - depending on the operating mode (`evcc_control`).

    supervisor: via the Supervisor API; evcc runs as an add-on (previous behaviour).
    api:        POST /api/system/shutdown; the restart is done by a process manager
                (docker 'restart: always', systemd 'Restart=always', NSSM) or by the
                Home Assistant add-on watchdog. Afterwards the add-on waits for
                GET /api/state to answer again.
    webhook:    like api, plus POST to `evcc_restart_webhook` to trigger the restart
                (Portainer webhook, HA rest_command, ...). Deliberately a URL and not a
                shell command - the add-on never executes arbitrary commands.
    none:       evcc is never touched; the add-on only writes.

    `auto` (default) picks supervisor when the Supervisor knows an evcc add-on - otherwise
    none. A foreign evcc is never shut down on its own.
    """

    MODES = ("auto", "supervisor", "api", "webhook", "none")

    def __init__(self, opts: dict, supervisor: Supervisor):
        self.opts = opts
        self.sup = supervisor
        self._mode: str | None = None

    # --- resolution

    def configured(self) -> str:
        mode = str(self.opts.get("evcc_control") or "auto").strip().lower()
        if mode not in self.MODES:
            log(f"WARNING: evcc_control '{mode}' is unknown - using 'auto'")
            return "auto"
        return mode

    def webhook(self) -> str:
        return str(self.opts.get("evcc_restart_webhook") or "").strip()

    def forget(self) -> None:
        """Drop the resolved mode (after the options changed)."""
        self._mode = None

    def resolve(self) -> str:
        """Operating mode for this run (evaluates 'auto', remembers the result)."""
        if self._mode:
            return self._mode
        mode = self.configured()
        if mode == "auto":
            mode = "none"
            if self.sup.available:
                try:
                    self.sup.resolve_slug()
                    mode = "supervisor"
                except Exception as exc:  # noqa: BLE001
                    log(f"no evcc add-on found ({exc})")
                    log("evcc is NOT stopped (evcc_control: auto). For an evcc that runs "
                        "outside Home Assistant set 'evcc_control: api' (process manager/"
                        "watchdog) or 'webhook'.")
            else:
                log("no Supervisor access - evcc is not stopped (evcc_control: auto)")
        self._mode = mode
        return mode

    def available(self) -> bool:
        return self.resolve() != "none"

    # --- actions

    def stop(self, api: "EvccApi | None" = None) -> bool:
        mode = self.resolve()
        if mode == "supervisor":
            return self.sup.stop_evcc()
        if mode in ("api", "webhook"):
            if api is None:
                log("ERROR: no evcc API client - evcc cannot be stopped")
                return False
            ok, info = api.shutdown()
            if ok:
                log(f"evcc shut down ({info}) - the restart is done by "
                    + ("the webhook" if mode == "webhook" else "the process manager/watchdog"))
            else:
                log(f"ERROR: evcc could not be shut down: {info}")
            return ok
        log("evcc is NOT stopped (evcc_control: none) - the refresh token is used by two "
            "consumers during the login window")
        return False

    def start(self, api: "EvccApi | None" = None) -> bool:
        """Starts evcc again, or waits until it answers again."""
        mode = self.resolve()
        if mode == "supervisor":
            return self.sup.start_evcc()
        if mode == "none":
            log("evcc is not restarted (evcc_control: none) - please do it manually, "
                "otherwise the changed configuration stays inactive")
            return False
        if api is None:
            log("ERROR: no evcc API client - evcc cannot be started")
            return False
        if mode == "webhook":
            url = self.webhook()
            if not url:
                log("ERROR: evcc_control is 'webhook', but evcc_restart_webhook is empty")
                return False
            status, text = http_request(url, method="POST", timeout=30)
            if status not in (200, 201, 202, 204):
                log(f"ERROR: restart webhook {url} -> HTTP {status}: {text[:160]}")
                return False
            log(f"restart triggered via webhook ({url} -> HTTP {status})")
        bereit, meldung = api.wait_until_ready(
            timeout=int(self.opts.get("evcc_ready_timeout", 150)))
        log(meldung)
        if not bereit:
            log("ERROR: evcc did not come back up - is a process manager with a restart "
                "policy running, or is the add-on watchdog enabled?")
        return bereit

    def restart(self, api: "EvccApi | None" = None) -> bool:
        if not self.stop(api):
            return False
        return self.start(api)

    def describe(self) -> str:
        mode = self.resolve()
        if mode == "supervisor":
            return ("supervisor (evcc add-on '"
                    f"{self.sup._slug or self.opts.get('evcc_slug') or '?'}')")
        if mode == "webhook":
            return f"webhook ({self.webhook() or 'URL missing'})"
        if mode == "api":
            return "api (shutdown + process manager/watchdog)"
        return "none (evcc is not touched)"


# ------------------------------------------------------------------ environment

def make_api(opts: dict) -> EvccApi:
    """Client without credentials - for open endpoints like /api/state."""
    return EvccApi(opts["evcc_url"])


def api_key_store() -> str:
    return os.path.join(data_dir(), "evcc_apikey")


def build_api(opts: dict, log_fn=None) -> tuple[EvccApi, str]:
    """Returns (client with a valid API key, source of the key).

    The key comes from the options, from an earlier generation, or is
    newly generated and stored with the evcc admin password.
    """
    creds = EvccCredentials(
        opts["evcc_url"], api_key_store(),
        apikey=opts.get("evcc_apikey", ""),
        admin_password=opts.get("evcc_admin_password", ""),
        account_user=opts.get("evcc_user", ""),
        auto=bool(opts.get("evcc_apikey_auto", True)),
        create_admin_password=bool(opts.get("evcc_set_admin_password", False)),
        log=log_fn or log,
    )
    key, source = creds.resolve()
    # Pass the password along: if no key could be generated, the client
    # logs in itself via a session (otherwise all further calls would be 401).
    return EvccApi(opts["evcc_url"], apikey=key,
                   password=opts.get("evcc_admin_password", ""), timeout=30), source


def is_charging(opts: dict) -> tuple[bool, str]:
    try:
        state = make_api(opts).state()
    except EvccApiError as exc:
        return False, f"evcc API not reachable ({exc})"
    for lp in state.get("loadpoints", []) or []:
        power = lp.get("chargePower") or 0
        if lp.get("charging") is True or (isinstance(power, (int, float)) and power > 100):
            return True, f"charging active at '{lp.get('title') or lp.get('name')}' ({power} W)"
    return False, "no charging active"


def find_evcc_config(opts: dict) -> str:
    configured = (opts.get("evcc_config_path") or "auto").strip()
    if configured and configured != "auto":
        if not os.path.exists(configured):
            raise RuntimeError(f"evcc_config_path '{configured}' does not exist")
        return configured

    candidates: list[str] = []
    for root in ("/addon_configs", "/config", "/homeassistant"):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) > root.count(os.sep) + 2:
                dirnames[:] = []
                continue
            for name in filenames:
                if name in ("evcc.yaml", "evcc.yml"):
                    candidates.append(os.path.join(dirpath, name))
    if not candidates:
        raise RuntimeError(
            "evcc.yaml not found - please set 'evcc_config_path' explicitly or "
            "run the add-on with 'all_addon_configs:rw' (map)"
        )
    candidates.sort(key=lambda p: (p.count(os.sep), len(p)))
    return candidates[0]


def fetch_tokens(opts: dict) -> dict:
    cmd = shlex.split(opts["helper_cmd"])
    cmd += [
        "-b", opts["brand"],
        "-u", opts["account_user"],
        "-p", opts["account_password"],
        "-c", str(opts.get("country") or "de"),
        "-t", str(opts.get("helper_timeout", 45)),
        # ignored by the old helper (parse_known_args), the new one uses it
        "--deadline", str(opts.get("helper_deadline", 240)),
    ]
    log(f"fetching new tokens ({opts['brand']}) - this takes 30-90 s ...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("token helper hit a hard timeout (900 s)") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        diagnosis = helper_diagnosis()
        detail = ""
        if diagnosis:
            grund = diagnosis.get("grund") or ""
            if grund:
                detail += f" cause from diagnosis: {grund}"
            if diagnosis.get("deutung"):
                detail += f" Last step: {diagnosis['deutung']}."
            if diagnosis.get("uebersprungene_codes"):
                detail += (f" ({len(diagnosis['uebersprungene_codes'])} intermediate redirects with a "
                           "foreign code were deliberately ignored)")
        raise RuntimeError(
            f"token helper failed (exit {proc.returncode}). Output: "
            f"{(stdout or stderr)[-300:]!r}.{detail} Typical causes: wrong password, "
            "2FA enabled, changed PSA login page. Full diagnosis: "
            f"{os.path.join(data_dir(), 'psa-login-debug.json')}"
        )
    match = re.search(r"\{.*\}", stdout, re.S)
    if not match:
        raise RuntimeError(f"no JSON response from the helper: {stdout[-400:]!r}")
    try:
        tokens = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"helper JSON not readable: {exc}") from exc
    for key in ("access_token", "refresh_token"):
        if not tokens.get(key):
            raise RuntimeError(f"helper response does not contain '{key}'")
    log(f"tokens received (access {len(tokens['access_token'])} characters, "
        f"refresh {len(tokens['refresh_token'])} characters, expires_in {tokens.get('expires_in')})")
    return tokens


def helper_diagnosis() -> dict | None:
    """Reads the browser's diagnosis dump (if present)."""
    path = os.path.join(data_dir(), "psa-login-debug.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def backup_file(path: str) -> str | None:
    try:
        target_dir = os.path.join(data_dir(), "backups")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(
            target_dir, f"{os.path.basename(path)}.{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(path, target)
        return target
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: backup failed: {exc}")
        return None


def write_atomic(path: str, content: str) -> None:
    tmp = f"{path}.tmp-psa-refresh"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    if os.path.exists(path):
        shutil.copystat(path, tmp)
    os.replace(tmp, path)


def configured_vehicles(opts: dict) -> list[dict]:
    vehicles = [v for v in (opts.get("vehicles") or []) if v.get("name")]
    if not vehicles:
        raise RuntimeError("no vehicles configured ('vehicles' is empty)")
    return vehicles


def redact_payload(payload: dict) -> dict:
    out = json.loads(json.dumps(payload))

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and re.search(
                    r"token|password|secret", key, re.I
                ) and value and value != "***":
                    node[key] = f"<{len(value)} characters>"
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out


# --------------------------------------------------------------------- main run

class Refresher:
    def __init__(self, opts: dict, supervisor: Supervisor):
        self.opts = opts
        self.sup = supervisor
        self.control = EvccControl(opts, supervisor)
        self.lock = threading.Lock()
        self.last_mode: str | None = None
        self._api: EvccApi | None = None
        self.api_source = ""
        # Flag: did this run stop evcc and not restart it yet?
        # The safety net in run() then restarts evcc in any case.
        self._evcc_stopped = False

    # --- options/client

    def reload(self) -> dict:
        """Re-read options - changes take effect without an add-on restart."""
        self.opts = load_options()
        self.sup.opts = self.opts
        self.control.opts = self.opts
        self.control.forget()
        self._api = None
        return self.opts

    def api_client(self) -> tuple[EvccApi, str]:
        """Client with a valid API key (obtained once per run)."""
        if self._api is None:
            self._api, self.api_source = build_api(self.opts)
        return self._api, self.api_source

    # --- mode detection

    def _resolve_mode(self) -> str:
        mode = (self.opts.get("mode") or "auto").lower()
        if mode in ("api", "yaml"):
            return mode
        # auto: API first (vehicles in the DB/UI), otherwise YAML
        try:
            api, source = self.api_client()
            api.ensure_auth()
            self.last_mode_source = source
            entries = api.list_vehicles()
            if entries:
                return "api"
            log("evcc knows no vehicles configured via UI/DB - checking YAML mode")
        except EvccApiError as exc:
            log(f"API mode not available ({exc}) - checking YAML mode")
        return "yaml"

    # --- individual steps

    def _should_skip_charging(self) -> str | None:
        if not self.opts.get("skip_while_charging", True):
            return None
        charging, why = is_charging(self.opts)
        return why if charging else None

    def run(self, reason: str, force_stop: bool = True) -> bool:
        if not self.lock.acquire(blocking=False):
            log("a refresh is already running - request ignored")
            return False
        started = time.time()
        state = load_state()
        self._api = None          # re-resolve credentials per run
        try:
            log(f"starting refresh - reason: {reason}")
            skip = self._should_skip_charging()
            if skip:
                log(f"skipped: {skip}")
                return False
            mode = self._resolve_mode()
            self.last_mode = mode
            log(f"config mode: {mode}")
            if mode == "api":
                return self._run_api(state, reason, force_stop)
            return self._run_yaml(state, reason, force_stop)
        except Exception as exc:  # noqa: BLE001
            state["last_error"] = str(exc)
            state["last_version"] = VERSION
            state["fails"] = int(state.get("fails", 0)) + 1
            state["next_attempt_ts"] = time.time() + self.opts["retry_backoff_minutes"] * 60
            save_state(state)
            log(f"ERROR: {exc}")
            self.sup.notify(
                "PSA token refresh failed",
                f"{exc}\n\nNext attempt in {self.opts['retry_backoff_minutes']} minutes.",
            )
            return False
        finally:
            # Safety net: no matter how the run ends - evcc must not be left
            # stopped. Previously the add-on stayed dead after an error in the
            # write path, and charge control was then out of action.
            if self._evcc_stopped and self.control.available():
                log("safety net: restarting evcc")
                if not self.control.start(self._api):
                    self.sup.notify(
                        "PSA token refresh: evcc is down",
                        "The run aborted and evcc could not be restarted. "
                        "Please start the evcc add-on manually in Home Assistant.",
                    )
                self._evcc_stopped = False
            log(f"refresh finished after {int(time.time() - started)} s")
            self.lock.release()

    # --- API mode (vehicles configured via evcc UI/DB)

    def _run_api(self, state: dict, reason: str, force_stop: bool) -> bool:
        opts = self.opts
        api, key_source = self.api_client()
        auth = api.ensure_auth()
        log(f"evcc API access: {auth} | API key: {key_source}")

        vehicles = configured_vehicles(opts)
        resolved = []
        for veh in vehicles:
            vid, detail, hint = api.find_vehicle(veh["name"], veh.get("id"))
            resolved.append((veh, vid, detail, hint))
            log(f"  - {veh['name']}: found as ID {vid} ({hint})")

        dry_run = bool(opts.get("dry_run"))
        if dry_run:
            log("dry run: evcc is not stopped (nothing happens to the running system)")
        elif not force_stop:
            log("force_stop=False - evcc is not stopped/started (test run)")
        elif not self.control.available():
            log(f"WARNING: evcc is not stopped (evcc_control: {self.control.resolve()}) - "
                "the refresh token is used by two consumers during the login window. For an "
                "external evcc set 'evcc_control: api' (process manager/watchdog) or "
                "'webhook'")
        else:
            log(f"evcc control: {self.control.describe()}")
            if not self.control.stop(api):
                raise RuntimeError("evcc could not be stopped - aborting, nothing changed")
            self._evcc_stopped = True

        tokens = fetch_tokens(opts)

        # The stop only protects the login window (single-use refresh token).
        # After that everything goes through the evcc configuration API, and that needs
        # a RUNNING evcc. Previously the write happened right after login,
        # while evcc was still stopped -> every call failed with
        # "Connection refused" and evcc stayed dead.
        if self._evcc_stopped:
            if not self.control.start(api):
                raise RuntimeError(
                    "evcc does not come back up after fetching the tokens - the new "
                    "tokens could not be written"
                )
            self._evcc_stopped = False
            # Without this wait, the test/PUT hits a port that Home
            # Assistant is still answering (response e.g. 404 "404 page not found").
            bereit, meldung = api.wait_until_ready(
                timeout=int(opts.get("evcc_ready_timeout", 150)))
            log(meldung)
            if not bereit:
                raise RuntimeError(
                    "the new tokens could not be written: " + meldung
                )

        original_log = api.log()
        generation = int(time.strftime("%Y%m%d%H%M"))
        for veh, vid, detail, _hint in resolved:
            vin = (veh.get("vin") or "").strip()
            config = detail.get("config") or {}
            if vin and config.get("vin") and config["vin"] != vin:
                log(f"WARNING: {veh['name']}: VIN in evcc ({config['vin']}) != configured ({vin})")

            payload = EvccApi.build_payload(
                detail, tokens["access_token"], tokens["refresh_token"],
                generation=generation,
                rotate_user_key=bool(opts.get("rotate_user_key", True)),
                nested=bool(opts.get("api_payload_nested", False)),
            )
            if opts.get("dry_run"):
                target = os.path.join(data_dir(), f"dryrun-vehicle-{vid}.json")
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(redact_payload(payload), fh, indent=2, ensure_ascii=False)
                log(f"DRY-RUN: {veh['name']}: PUT body (without tokens) is at {target}")
                continue

            if opts.get("api_verify_with_test", True):
                ok, info = api.test_vehicle(vid, payload)
                if not ok:
                    raise RuntimeError(
                        f"test of the new tokens for {veh['name']} failed: {info}"
                    )
                log(f"  - {veh['name']}: test config accepted by evcc")

            ok, info = api.update_vehicle(vid, payload)
            if not ok:
                raise RuntimeError(f"saving for {veh['name']} failed: {info}")
            log(f"  - {veh['name']}: new tokens stored in evcc (user -> {payload['user'] if 'user' in payload else (payload.get('config') or {}).get('user')})")

        if opts.get("dry_run"):
            state["last_dry_run_ts"] = time.time()
            state.pop("last_error", None)
            state["fails"] = 0
            state["next_attempt_ts"] = 0
            save_state(state)
            self.sup.notify(
                "PSA token refresh (dry run)",
                f"tokens fetched, PUT bodies written, evcc not changed.\n"
                f"Files: {data_dir()}/dryrun-vehicle-*.json",
            )
            return True

        dirty = api.config_dirty()
        restart_done = False
        if dirty and self.control.available():
            # evcc is running again now - a second start would be a no-op.
            # A configuration marked as dirty only becomes
            # effective after a real restart.
            if not self.control.restart(api):
                self.sup.notify(
                    "PSA token refresh: evcc does not start",
                    "Tokens were stored, but evcc does not report itself as started.",
                )
            restart_done = True
        elif dirty:
            log(f"WARNING: evcc requires a restart, but evcc is not controlled "
                f"(evcc_control: {self.control.resolve()}) - please restart evcc manually")
        else:
            log("evcc does not require a restart - vehicle was updated at runtime")

        return self._verify_api(api, state, reason, resolved, restart_done, original_log)

    def _verify_api(self, api: EvccApi, state: dict, reason: str,
                    resolved: list, restart_done: bool,
                    original_log: str = "") -> bool:
        delay = max(10, int(self.opts.get("verify_delay_seconds", 45)))
        if restart_done:
            time.sleep(delay)
        evcc_log = api.log()
        # Only count NEW errors: the restart regularly makes evcc
        # start once more with the old, used-up token ("invalid_grant")
        # - that is expected and must not paint the verification red.
        broken = [p for p in ("token expired", "invalid_grant",
                              "cannot create vehicle type 'template'")
                  if p in evcc_log and p not in original_log]
        if broken:
            raise RuntimeError(
                "evcc still reports token problems (" + ", ".join(broken) + "). "
                "Please check the evcc log."
            )
        state.pop("last_error", None)
        state.update({
            "last_success_ts": time.time(),
            "last_attempt_ts": time.time(),
            "last_reason": reason,
            "last_mode": "api",
            "last_version": VERSION,
            "last_error": None,
            "fails": 0,
            "next_attempt_ts": 0,
            "vehicles": [veh["name"] for veh, *_ in resolved],
        })
        save_state(state)
        log("refresh successfully verified (API mode)")
        self.sup.notify(
            "PSA tokens renewed",
            f"Reason: {reason}\nMode: evcc-API\n"
            f"Vehicles: {', '.join(veh['name'] for veh, *_ in resolved)}\n"
            + ("evcc restarted." if restart_done else "No restart needed.")
            + f"\nVersion {VERSION}",
        )
        return True

    # --- YAML mode (vehicles in evcc.yaml)

    def _run_yaml(self, state: dict, reason: str, force_stop: bool) -> bool:
        opts = self.opts
        control = self.control.resolve()
        if control != "supervisor" and not opts.get("dry_run"):
            # evcc.yaml only takes effect after a restart of the add-on; without a
            # Supervisor the add-on cannot restart it (an external evcc does not read the
            # file at all). Better to abort clearly than to patch a file nobody loads.
            raise RuntimeError(
                f"YAML mode requires a Supervisor (evcc as an add-on): evcc_control is "
                f"'{control}'. For an evcc running outside Home Assistant please import "
                "the vehicles into evcc and use 'mode: api'."
            )
        cfg_path = find_evcc_config(opts)
        if not (os.access(cfg_path, os.W_OK) and os.access(os.path.dirname(cfg_path), os.W_OK)):
            raise RuntimeError(
                f"{cfg_path} is not writable - the add-on lacks "
                "'all_addon_configs:rw' (map) or permissions are missing"
            )
        with open(cfg_path, encoding="utf-8") as fh:
            original = fh.read()
        log(f"evcc.yaml: {cfg_path}")

        vehicles = configured_vehicles(opts)
        if force_stop and self.sup.available and not opts.get("dry_run"):
            if not self.sup.stop_evcc():
                raise RuntimeError("evcc could not be stopped - aborting, file unchanged")
            self._evcc_stopped = True

        tokens = fetch_tokens(opts)

        generation = int(time.strftime("%Y%m%d%H%M"))
        patched, changes, warnings = patch_evcc_yaml(
            original,
            [{"name": v["name"], "vin": (v.get("vin") or "").strip(),
              "access": tokens["access_token"], "refresh": tokens["refresh_token"]}
             for v in vehicles],
            rotate_user_key=bool(opts.get("rotate_user_key", True)),
            generation=generation,
        )
        for warning in warnings:
            log(f"WARNING: {warning}")
        for change in changes:
            log(f"  - {change}")

        if opts.get("dry_run"):
            target = os.path.join(data_dir(), "dryrun-evcc.yaml")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(patched)
            log(f"DRY-RUN: patched file at {target}; evcc was NOT touched")
            self.sup.notify(
                "PSA token refresh (dry run, YAML)",
                f"Patched file: {target}\n\n" + "\n".join(changes),
            )
            state["last_dry_run_ts"] = time.time()
            state.pop("last_error", None)
            state["fails"] = 0
            state["next_attempt_ts"] = 0
            save_state(state)
            return True

        original_log = self.sup.evcc_log() if self.sup.available else ""
        backup = backup_file(cfg_path)
        if backup:
            log(f"Backup: {backup}")
        write_atomic(cfg_path, patched)
        log(f"{cfg_path} updated ({len(vehicles)} vehicle(s))")

        if self.sup.available:
            if self.sup.start_evcc():
                self._evcc_stopped = False
            else:
                self.sup.notify(
                    "PSA token refresh: evcc does not start",
                    f"evcc.yaml updated, but evcc does not start. Backup: {backup}",
                )
            time.sleep(max(10, int(opts.get("verify_delay_seconds", 45))))
            fresh_log = self.sup.evcc_log()
            still = [p for p in ("token expired", "invalid_grant",
                                 "cannot create vehicle type 'template'")
                     if p in fresh_log and p not in original_log]
            if still:
                raise RuntimeError(
                    "evcc still reports token problems after the restart ("
                    + ", ".join(still) + "). Please check the evcc log."
                )

        state.pop("last_error", None)
        state.update({
            "last_success_ts": time.time(),
            "last_attempt_ts": time.time(),
            "last_reason": reason,
            "last_mode": "yaml",
            "last_version": VERSION,
            "last_error": None,
            "fails": 0,
            "next_attempt_ts": 0,
            "vehicles": [v["name"] for v in vehicles],
        })
        save_state(state)
        log("refresh successfully verified (YAML mode)")
        self.sup.notify(
            "PSA tokens renewed",
            f"Reason: {reason}\nMode: evcc.yaml\n"
            f"Vehicles: {', '.join(v['name'] for v in vehicles)}\nBackup: {backup}",
        )
        return True

    # --- detection / diagnosis

    def backoff_remaining(self) -> tuple[float, str]:
        """Remaining wait after the last failure (seconds, last error)."""
        state = load_state()
        rest = float(state.get("next_attempt_ts") or 0) - time.time()
        return (rest if rest > 0 else 0.0), str(state.get("last_error") or "")

    def detect_reason(self) -> str | None:
        state = load_state()
        now = time.time()

        # After an update the wait should not get in the way
        if state.get("last_version") != VERSION:
            if float(state.get("next_attempt_ts") or 0) > now:
                log(f"Version {VERSION} clears the wait after the last failure "
                    f"(previously {state.get('last_version') or 'unknown'})")
            state["next_attempt_ts"] = 0
            state["last_version"] = VERSION
            save_state(state)

        if now < float(state.get("next_attempt_ts") or 0):
            rest, letzter_fehler = self.backoff_remaining()
            log(f"no action: after the last failure, still waiting "
                f"{int(rest / 60) + 1} min (until "
                f"{time.strftime('%H:%M', time.localtime(now + rest))})"
                + (f" - last error: {letzter_fehler}" if letzter_fehler else ""))
            return None

        evcc_log = ""
        try:
            api, _ = self.api_client()
            api.ensure_auth()      # builds the session (cookie) if needed, otherwise 401 on the log
            evcc_log = api.log()
        except EvccApiError:
            pass
        if not evcc_log and self.sup.available:
            evcc_log = self.sup.evcc_log()
        if evcc_log and ERROR_PATTERNS.search(evcc_log):
            return "token error detected in the evcc log"

        days = float(self.opts.get("proactive_refresh_days") or 0)
        last_success = float(state.get("last_success_ts") or 0)
        if days > 0 and (last_success == 0 or now - last_success > days * 86400):
            age = "never" if last_success == 0 else f"{(now - last_success) / 86400:.1f} days"
            return f"proactive renewal (last successful renewal: {age} old)"
        return None

    def probe_login(self, wait_seconds: int = 45) -> dict:
        """Visits the login chain without credentials and returns the page dump."""
        cmd = shlex.split(self.opts["helper_cmd"]) + [
            "-b", self.opts["brand"], "--probe", "--probe-wait", str(wait_seconds),
        ]
        log(f"login probe: visiting the chain for {self.opts['brand']} "
            "(without credentials, diagnosis only) ...")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=wait_seconds + 180, check=False)
        except subprocess.TimeoutExpired:
            return {"fehler": "probe hit a hard timeout"}
        diagnosis = helper_diagnosis() or {}
        log(f"login probe finished (exit {proc.returncode}) - "
            f"{diagnosis.get('deutung', 'no dump')}")
        return {
            "exit_code": proc.returncode,
            "deutung": diagnosis.get("deutung"),
            "url_verlauf": diagnosis.get("url_verlauf", []),
            "letzter_zustand": diagnosis.get("letzter_zustand", {}),
            "diagnose_datei": os.path.join(data_dir(), "psa-login-debug.json"),
            "hinweis": ("This probe does NOT log in - it only shows how far the "
                        "login chain gets in the browser."),
        }

    def diag(self, dump: bool = False) -> dict:
        opts = self.opts
        state = load_state()
        try:
            api, key_source = self.api_client()
        except EvccApiError as exc:
            api, key_source = EvccApi(opts["evcc_url"]), f"not available ({exc})"
        self.api_source = key_source
        result: dict = {
            "version": VERSION,
            "evcc_version": api.version() or "not reachable",
            "api_key_source": key_source,
            "psa_account_configured": bool(opts.get("account_user") and opts.get("account_password")),
            "mode_configured": opts.get("mode"),
            "brand": opts.get("brand"),
            "vehicles_configured": [v.get("name") for v in (opts.get("vehicles") or [])],
            "evcc_url": opts.get("evcc_url"),
            "evcc_addon_state": self.sup.state() if self.sup.available else "no Supervisor access",
            "addon_info": self.sup.self_info() if self.sup.available else {},
            "addon_diagnostics": self.sup.self_diagnostics(VERSION) if self.sup.available else [],
            "evcc_control_configured": opts.get("evcc_control"),
            "evcc_control_resolved": self.control.describe(),
            "evcc_restart_webhook": "set" if self.control.webhook() else "empty",
            "evcc_slug_configured": opts.get("evcc_slug"),
            "evcc_slug_resolved": (self.sup._slug or self.sup.resolve_slug())
                                  if self.sup.available else None,
            "supervisor_apps": [
                f"{a.get('name')} ({a.get('slug')}, {a.get('state')})"
                for a in (self.sup.list_apps() if self.sup.available else [])
            ],
            "state": state,
            "dry_run": bool(opts.get("dry_run")),
            "trigger_endpoint": (
                f"http://<HA-IP>:{opts['trigger_port']}/refresh"
                if opts.get("trigger_token") else "disabled"
            ),
        }
        try:
            result["charging"] = is_charging(opts)[1]
        except Exception as exc:  # noqa: BLE001
            result["charging"] = f"unknown ({exc})"
        try:
            result["evcc_api_auth"] = api.ensure_auth()
            result["evcc_config_dirty"] = api.config_dirty()
            result["mode_resolved"] = self._resolve_mode()
            entries = api.list_vehicles()
            result["evcc_db_vehicles"] = [
                {
                    "id": e.get("id"),
                    "name": e.get("name"),
                    "title": (e.get("config") or {}).get("title"),
                    "vin": (e.get("config") or {}).get("vin"),
                    "user": (e.get("config") or {}).get("user"),
                    "tokens_masked": EvccApi.masked_keys(e),
                }
                for e in entries
            ]
            if dump:
                result["helper_diagnosis"] = helper_diagnosis()
                details = {}
                for veh in (opts.get("vehicles") or []):
                    try:
                        vid, detail, hint = api.find_vehicle(veh["name"], veh.get("id"))
                        payload = EvccApi.build_payload(
                            detail, "<new-access>", "<new-refresh>",
                            generation=int(time.strftime("%Y%m%d%H%M")),
                            rotate_user_key=bool(opts.get("rotate_user_key", True)),
                            nested=bool(opts.get("api_payload_nested", False)),
                        )
                        details[veh["name"]] = {
                            "id": vid, "hint": hint,
                            "detail": detail,
                            "put_body_planned": payload,
                        }
                    except EvccApiError as exc:
                        details[veh["name"]] = {"error": str(exc)}
                result["vehicle_details"] = details
        except EvccApiError as exc:
            result["evcc_api_error"] = str(exc)
            result["mode_resolved"] = "yaml"
            try:
                result["evcc_yaml"] = find_evcc_config(opts)
            except Exception as inner:  # noqa: BLE001
                result["evcc_yaml"] = f"not found ({inner})"
        return result


# ------------------------------------------------------------- HTTP trigger

def start_trigger_server(refresher: Refresher, opts: dict) -> ThreadingHTTPServer | None:
    token = (opts.get("trigger_token") or "").strip()
    if not token:
        log("trigger_token empty - HTTP trigger is disabled (only scheduler active)")
        return None

    class Handler(BaseHTTPRequestHandler):
        server_version = f"{LOG_PREFIX}/{VERSION}"

        def _authorized(self) -> bool:
            given = self.headers.get("X-Trigger-Token", "")
            if not given and "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("token="):
                        given = part.split("=", 1)[1]
            return given == token

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(401, {"error": "invalid or missing trigger token"})
                return
            if self.path.startswith("/status"):
                self._send(200, refresher.diag())
            elif self.path.startswith("/dump"):
                self._send(200, refresher.diag(dump=True))
            else:
                self._send(404, {"error": "unknown path",
                                 "hint": "GET /status, GET /dump, POST /refresh, POST /probe"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(401, {"error": "invalid or missing trigger token"})
                return
            if self.path.startswith("/probe"):
                length = int(self.headers.get("Content-Length") or 0)
                wait = 45
                if length:
                    try:
                        wait = int((json.loads(self.rfile.read(length) or b"{}")
                                    .get("wait_seconds")) or wait)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self._send(200, refresher.probe_login(max(10, min(120, wait))))
                except Exception as exc:  # noqa: BLE001
                    self._send(500, {"error": str(exc)})
                return
            if not self.path.startswith("/refresh"):
                self._send(404, {"error": "unknown path",
                                 "hint": "POST /refresh | POST /probe | GET /status | GET /dump"})
                return
            if refresher.lock.locked():
                self._send(409, {"status": "busy", "message": "refresh already running"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            reason = "HA-Automation"
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    reason = body.get("reason") or reason
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=refresher.run, args=(reason,), daemon=True).start()
            self._send(202, {
                "status": "accepted",
                "reason": reason,
                "hint": "the result arrives as a notification in Home Assistant",
            })

        def log_message(self, fmt: str, *args) -> None:
            log(f"HTTP {self.address_string()} {fmt % args}")

    server = ThreadingHTTPServer(("0.0.0.0", int(opts["trigger_port"])), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"HTTP trigger active on port {opts['trigger_port']} "
        "(POST /refresh, POST /probe, GET /status, GET /dump)")
    return server


# ------------------------------------------------------------------- entry point

KNOWN_FLAGS = ("--version", "--check", "--status", "--dump", "--once")


def main() -> int:
    opts = load_options()

    if "--version" in sys.argv:
        print(VERSION)
        return 0

    unknown = [a for a in sys.argv[1:] if a.startswith("-") and a not in KNOWN_FLAGS]
    if unknown:
        log(f"ERROR: unknown option(s): {' '.join(unknown)}")
        log(f"allowed are: {' '.join(KNOWN_FLAGS)}")
        return 2

    log(f"{LOG_PREFIX} {VERSION} starting")
    if not (opts.get("account_user") and opts.get("account_password")):
        log("NOTE: PSA account still empty ('account_user'/'account_password') - "
            "the add-on waits for it instead of exiting")
    log(f"Mode: {opts.get('mode')} | Brand: {opts['brand']} | Account: {opts['account_user']} | "
        f"Vehicles: {[v.get('name') for v in (opts.get('vehicles') or [])]}")
    log(f"Interval: {opts['check_interval_minutes']} min | proactive after "
        f"{opts['proactive_refresh_days']} days | dry_run={opts['dry_run']}")

    supervisor = Supervisor(opts)
    for message in supervisor.self_diagnostics(VERSION):
        log(message)
    refresher = Refresher(opts, supervisor)

    if "--check" in sys.argv or "--status" in sys.argv:
        print(json.dumps(refresher.diag(), indent=2, ensure_ascii=False))
        return 0
    if "--dump" in sys.argv:
        print(json.dumps(refresher.diag(dump=True), indent=2, ensure_ascii=False))
        return 0
    if "--once" in sys.argv:
        return 0 if refresher.run("manuell (--once)") else 1

    start_trigger_server(refresher, opts)

    interval = max(1, int(opts["check_interval_minutes"])) * 60
    missing_warned = False
    while True:
        try:
            opts = refresher.reload()      # re-read options every round
            interval = max(1, int(opts["check_interval_minutes"])) * 60
            if not opts.get("account_user") or not opts.get("account_password"):
                if not missing_warned:
                    log("WAITING FOR CONFIGURATION: 'account_user' (PSA/MyPeugeot account) and "
                        "'account_password' are missing from the add-on options. The add-on keeps "
                        "running and picks up option changes without a restart.")
                    missing_warned = True
                time.sleep(interval)
                continue
            missing_warned = False
            reason = refresher.detect_reason()
            if reason:
                refresher.run(reason)
            elif not refresher.backoff_remaining()[0]:
                log("status ok - no refresh needed (no token error, "
                    "proactive interval not reached yet)")
        except Exception as exc:  # noqa: BLE001
            log(f"unexpected error in the loop: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("stopped")
