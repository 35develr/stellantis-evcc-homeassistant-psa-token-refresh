#!/usr/bin/env python3
"""Access to the evcc REST API (standard library only).

Important: In evcc vehicles can be configured in two ways:
  a) configured in evcc.yaml (file path, see patch_evcc_yaml.py)
  b) configured via the UI -> stored in the evcc.db (internally named "db:<id>")

For (b) there is the internal configuration API branch /api/config/..., which the
evcc UI itself uses:

  GET    /api/config/devices/vehicle              list of all vehicles
  GET    /api/config/devices/vehicle/{id}         details (secrets masked as "***")
  POST   /api/config/test/vehicle/merge/{id}      test configuration, store nothing
  PUT    /api/config/devices/vehicle/{id}         store configuration (sets dirty=restart required)
  GET    /api/config/dirty                        is a restart required?
  GET    /api/system/log                          evcc log
  GET    /api/state                               status (for "am I currently charging?")

Authentication: "Authorization: Bearer <API-Key>" or session cookie via
POST /api/auth/login {"password": "<admin password>"}.

Create the API key automatically (what EvccCredentials below does):
  GET  /api/auth/status    -> "true" = auth off | "false" = password set, not logged in
                              | 501 = no admin password set yet | 403 = demo mode
  PUT  /api/auth/password  {"current": "...", "new": "..."} - change password;
                           without a set password {"new": "..."} is enough (response 201)
  POST /api/auth/apikey    {"password": "<admin password>"} -> {"key": "..."}
                           (401 = wrong password, 501 = no admin password set)

Masked values ("***") in the PUT body are kept by evcc (mergeMasked).
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request

__all__ = ["EvccApi", "EvccApiError", "EvccCredentials"]

USER_SUFFIX_RE = re.compile(r"^(?P<base>.*?)#(?P<gen>\d+)$")
MASK = "***"


class EvccApiError(Exception):
    """Network, auth or API error."""


class EvccApi:
    def __init__(self, base_url: str, apikey: str = "", password: str = "",
                 timeout: int = 20):
        self.base = (base_url or "").rstrip("/")
        self.apikey = (apikey or "").strip()
        self.password = (password or "").strip()
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    # ------------------------------------------------------------ Low Level

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: int | None = None) -> tuple[int, str]:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.apikey:
            req.add_header("Authorization", f"Bearer {self.apikey}")
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise EvccApiError(f"evcc at {url} not reachable: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001
            raise EvccApiError(f"error during {method} {url}: {exc}") from exc

    def _json(self, method: str, path: str, body: dict | None = None,
              timeout: int | None = None, ok=(200,)) -> object:
        status, text = self.request(method, path, body, timeout)
        if status not in ok:
            raise EvccApiError(
                f"{method} {path} -> HTTP {status}: {text.strip()[:300]}"
                + (" (API key missing/invalid?)" if status in (401, 403) else "")
            )
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvccApiError(f"{method} {path}: response is not JSON ({exc})") from exc

    # ------------------------------------------------------------ Auth/Status

    def wait_until_ready(self, timeout: int = 150,
                         interval: float = 3.0) -> tuple[bool, str]:
        """Waits until evcc itself responds.

        After being started by the supervisor it takes a few seconds until evcc binds
        its port. During this time Home Assistant still answers on the port itself and
        returns foreign responses - e.g. 404 "404 page not found". Write operations in this
        window fail seemingly for no reason (run of 01.10.2026 with dry_run=false).

        Probe: GET /api/state requires no authentication and only responds with 200
        from a running evcc.
        """
        beginn = time.time()
        letzter_fehler = "no response"
        while time.time() - beginn < timeout:
            try:
                status, text = self.request("GET", "/api/state", timeout=10)
                if status == 200:
                    dauer = int(time.time() - beginn)
                    try:
                        version = str((json.loads(text) or {}).get("version") or "")
                    except Exception:  # noqa: BLE001
                        version = ""
                    return True, (f"evcc ready after {dauer} s"
                                  + (f" (Version {version})" if version else ""))
                letzter_fehler = f"HTTP {status}"
            except Exception as exc:  # noqa: BLE001
                letzter_fehler = str(exc)
            time.sleep(interval)
        return False, (f"evcc does not respond after {timeout} s ({letzter_fehler}) - "
                       "is evcc running? Otherwise the port is answered by Home Assistant")

    def version(self) -> str:
        """Version of the running evcc instance (empty if not reachable)."""
        try:
            status, text = self.request("GET", "/api/state", timeout=10)
            if status == 200:
                return str((json.loads(text) or {}).get("version") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def login(self) -> bool:
        """Fetch a session cookie with the admin password (only if no API key is set)."""
        if not self.password:
            return False
        status, text = self.request("POST", "/api/auth/login", {"password": self.password})
        return status in (200, 204, 201)

    def ensure_auth(self) -> str:
        """Checks access to the configuration API.

        Returns: 'apikey', 'session' or 'without authentication'.
        """
        status, text = self.request("GET", "/api/config/dirty")
        if status == 200:
            return "apikey" if self.apikey else "without authentication"
        if status not in (401, 403):
            raise EvccApiError(f"GET /api/config/dirty -> HTTP {status}: {text.strip()[:200]}")
        if self.password and self.login():
            status, text = self.request("GET", "/api/config/dirty")
            if status == 200:
                return "session"
        raise EvccApiError(
            "Access to the evcc configuration API not possible (HTTP "
            f"{status}). Please provide an API key as 'evcc_apikey' or the "
            "evcc admin password as 'evcc_admin_password' - the add-on creates the "
            "key from it itself."
        )

    def state(self) -> dict:
        return self._json("GET", "/api/state") or {}

    # ------------------------------------------------------------ Admin/API-Key

    def auth_state(self) -> str:
        """Authentication state according to GET /api/auth/status."""
        status, text = self.request("GET", "/api/auth/status")
        body = text.strip().lower()
        if status == 200:
            if body.startswith("true"):
                return "disabled"      # authentication switched off
            return "configured"        # admin password set, not logged in
        if status == 501:
            return "missing"           # no admin password in evcc yet
        if status == 403:
            return "locked"            # demo/locked mode
        return f"unknown({status}: {text.strip()[:80]})"

    def generate_api_key(self, password: str) -> str:
        """Creates (or rotates) the API key. Raises EvccApiError with a clear cause."""
        if not password:
            raise EvccApiError("no evcc admin password given - API key cannot be created")
        status, text = self.request("POST", "/api/auth/apikey", {"password": password})
        if status == 200:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise EvccApiError(f"API key response not readable: {exc}") from exc
            key = data.get("key") or data.get("apikey") or ""
            if not key:
                raise EvccApiError("evcc did not return a key")
            return key
        if status == 401:
            raise EvccApiError(
                "evcc rejected the admin password (HTTP 401). "
                "Please correct 'evcc_admin_password'."
            )
        if status == 501:
            raise EvccApiError(
                "evcc has not set an admin password yet (HTTP 501) - without a password "
                "no API key can be created. Set a password in evcc under Settings "
                "or enable 'evcc_set_admin_password: true' (sets the password from "
                "'evcc_admin_password' in evcc)."
            )
        raise EvccApiError(f"API key creation failed (HTTP {status}): {text.strip()[:200]}")

    def set_admin_password(self, new_password: str, current_password: str = "") -> str:
        """Sets the evcc admin password (only needed if none exists yet)."""
        if not new_password:
            raise EvccApiError("no new admin password given")
        body: dict = {"new": new_password}
        if current_password:
            body["current"] = current_password
        status, text = self.request("PUT", "/api/auth/password", body)
        if status == 201:
            return "created"
        if status == 202:
            return "updated"
        if status == 400:
            raise EvccApiError("evcc rejected the current admin password (HTTP 400)")
        raise EvccApiError(f"setting password failed (HTTP {status}): {text.strip()[:200]}")

    def log(self, max_bytes: int = 80000) -> str:
        status, text = self.request("GET", "/api/system/log")
        if status != 200:
            return ""
        return text[-max_bytes:]

    def config_dirty(self) -> bool | None:
        try:
            res = self._json("GET", "/api/config/dirty")
        except EvccApiError:
            return None
        if isinstance(res, dict):
            return bool(res.get("dirty"))
        return bool(res)

    # ------------------------------------------------------------ Vehicles

    def list_vehicles(self) -> list[dict]:
        res = self._json("GET", "/api/config/devices/vehicle")
        return res if isinstance(res, list) else []

    def get_vehicle(self, vehicle_id: int) -> dict:
        res = self._json("GET", f"/api/config/devices/vehicle/{int(vehicle_id)}")
        return res if isinstance(res, dict) else {}

    def find_vehicle(self, name: str, vehicle_id: int | None = None
                     ) -> tuple[int, dict, str]:
        """Finds a vehicle by ID, name or title. Returns: (id, detail, hint)."""
        if vehicle_id:
            return int(vehicle_id), self.get_vehicle(int(vehicle_id)), "by ID"

        entries = self.list_vehicles()
        if not entries:
            raise EvccApiError(
                "no vehicles configured via UI/DB found - "
                "they are probably defined in evcc.yaml (use mode 'yaml')"
            )

        lower = name.lower().strip()
        for entry in entries:
            config = entry.get("config") or {}
            candidates = {
                str(entry.get("name") or "").lower(),
                str(config.get("title") or "").lower(),
                str(config.get("name") or "").lower(),
            }
            if lower in candidates:
                vid = entry.get("id")
                if vid is None:
                    break
                return int(vid), self.get_vehicle(int(vid)), (
                    f"by name/title '{name}' (internal ID {vid})"
                )

        known = ", ".join(
            f"{e.get('name')}({(e.get('config') or {}).get('title') or '-'})"
            for e in entries
        )
        raise EvccApiError(
            f"vehicle '{name}' not found via UI/DB. Available: {known}"
        )

    # ------------------------------------------------------------ Writing

    @staticmethod
    def build_payload(detail: dict, access: str, refresh: str,
                      generation: int | None = None,
                      rotate_user_key: bool = True,
                      nested: bool = False) -> dict:
        """Builds the PUT body: flat (like the evcc UI) or nested."""
        top = {k: v for k, v in detail.items() if k != "id"}
        config = top.pop("config", None) or {}
        if nested:
            payload = dict(top)
            inner = dict(config)
            target = inner
            payload["config"] = inner
        else:
            payload = dict(top)
            payload.update(config)
            target = payload

        if generation is not None and rotate_user_key and target.get("user"):
            base = str(target["user"])
            match = USER_SUFFIX_RE.match(base)
            target["user"] = f"{match.group('base') if match else base}#{generation}"

        target["accessToken"] = access
        target["refreshToken"] = refresh
        return payload

    def test_vehicle(self, vehicle_id: int, payload: dict,
                     timeout: int = 120) -> tuple[bool, str]:
        """Checks the new configuration against the PSA API without storing it."""
        status, text = self.request(
            "POST", f"/api/config/test/vehicle/merge/{int(vehicle_id)}",
            body=payload, timeout=timeout,
        )
        if status != 200:
            return False, (f"HTTP {status} on POST /api/config/test/vehicle/merge/"
                           f"{int(vehicle_id)}: "
                           f"{text.strip()[:300]}"
                           + (" (404 = evcc not ready? The port is then answered by Home "
                              "Assistant)" if status == 404 else ""))
        return True, text.strip()[:800]

    def update_vehicle(self, vehicle_id: int, payload: dict,
                       timeout: int = 120) -> tuple[bool, str]:
        status, text = self.request(
            "PUT", f"/api/config/devices/vehicle/{int(vehicle_id)}",
            body=payload, timeout=timeout,
        )
        if status not in (200, 201, 202, 204):
            return False, f"HTTP {status}: {text.strip()[:400]}"
        return True, text.strip()[:400]

    # ------------------------------------------------------------ Masking

    @staticmethod
    def masked_keys(detail: dict) -> list[str]:
        config = detail.get("config") or {}
        return [k for k, v in config.items() if isinstance(v, str) and v == MASK]


class EvccCredentials:
    """Obtains a working evcc API key and stores it permanently.

    Order:
      1. `evcc_apikey` from the add-on options (if set and valid)
      2. stored key from `store_path` (from an earlier run)
      3. create a new one with the admin password (POST /api/auth/apikey) and store it
      4. optional: create an admin password in evcc if none exists yet
      5. last resort: session login with the admin password (cookie)

    `log` is a function for status messages (default: silent).
    """

    def __init__(self, base_url: str, store_path: str, *, apikey: str = "",
                 admin_password: str = "", account_user: str = "", auto: bool = True,
                 create_admin_password: bool = False, timeout: int = 20, log=None):
        self.base_url = (base_url or "").rstrip("/")
        self.store_path = store_path
        self.apikey = (apikey or "").strip()
        self.admin_password = (admin_password or "").strip()
        self.account_user = (account_user or "").strip()
        self.auto = bool(auto)
        self.create_admin_password = bool(create_admin_password)
        self.timeout = timeout
        self.log = log or (lambda msg: None)

    # -------------------------------------------------------------- Helpers

    def _api(self, key: str = "") -> EvccApi:
        return EvccApi(self.base_url, apikey=key, timeout=self.timeout)

    def _check_key(self, key: str) -> bool:
        if not key:
            return False
        try:
            self._api(key)._json("GET", "/api/config/dirty")
            return True
        except EvccApiError:
            return False

    def load_stored(self) -> str:
        try:
            with open(self.store_path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def store(self, key: str) -> None:
        try:
            import os
            os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
            with open(self.store_path, "w", encoding="utf-8") as fh:
                fh.write(key)
            os.chmod(self.store_path, 0o600)
            self.log(f"new evcc API key stored ({self.store_path})")
        except OSError as exc:
            self.log(f"WARNING: API key could not be stored: {exc}")

    # -------------------------------------------------------------- Flow

    def resolve(self) -> tuple[str, str]:
        """Returns: (key, source). Raises EvccApiError if nothing works."""
        if self.apikey:
            if self._check_key(self.apikey):
                return self.apikey, "key from the add-on options"
            self.log("the API key configured in the options is rejected by evcc")

        stored = self.load_stored()
        if stored:
            if self._check_key(stored):
                return stored, "stored key"
            self.log("the stored API key is rejected by evcc (possibly deleted in evcc)")

        if not self.auto:
            raise EvccApiError(
                "no valid evcc API key available and automatic creation is "
                "disabled ('evcc_apikey_auto: false')."
            )

        if not self.admin_password:
            raise EvccApiError(
                "no valid evcc API key available and no 'evcc_admin_password' "
                "set - without a password the key cannot be created. "
                "Either create an API key in evcc under Settings and enter it as "
                "'evcc_apikey', or provide the evcc admin password as "
                "'evcc_admin_password'."
            )

        # One client for all steps - the session (cookie) must be preserved
        api = EvccApi(self.base_url, password=self.admin_password, timeout=self.timeout)
        state = api.auth_state()
        self.log(f"evcc authentication: {state}")

        if state == "disabled":
            self.log("evcc runs without authentication - an API key is not needed")
            return "", "no authentication active"

        if state == "locked":
            raise EvccApiError("evcc runs in locked demo mode - API access not possible")

        if state == "missing":
            if not self.create_admin_password:
                raise EvccApiError(
                    "evcc has no admin password yet. Please set one in evcc under Settings "
                    "- or enable the option 'evcc_set_admin_password: true', "
                    "then the add-on creates the password from 'evcc_admin_password' in evcc."
                )
            self.log("evcc has no admin password yet - creating it from the options")
            result = api.set_admin_password(self.admin_password)
            self.log(f"admin password in evcc {('created' if result == 'created' else 'updated')}")

        # Important: evcc puts POST /api/auth/apikey behind EnsureAuthHandler
        # (server/http.go: "API key endpoints require an authenticated session").
        # Without a cookie evcc responds with 401 before the password is even checked.
        if not api.login():
            raise EvccApiError(
                "Login to evcc failed (POST /api/auth/login -> HTTP 401). "
                "Please check 'evcc_admin_password' - without a session evcc also "
                "refuses API key creation."
            )
        self.log("logged in to evcc (session established for key creation)")

        try:
            key = api.generate_api_key(self.admin_password)
        except EvccApiError as exc:
            self.log(f"API key not created ({exc}) - working with the session")
            return "", "session (admin password)"

        if not self._check_key(key):
            raise EvccApiError("the newly created API key is not accepted by evcc")
        self.store(key)
        return key, "newly generated (from the admin password)"
