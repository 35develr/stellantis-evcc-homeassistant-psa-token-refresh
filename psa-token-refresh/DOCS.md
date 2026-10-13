# Stellantis/PSA Token Refresh for evcc

Renews the PSA/Stellantis OAuth tokens for **evcc** when they become invalid -
automatically, without you having to start the PSA Token Helper by hand.

## How it works

1. **Detect** - the add-on reads the evcc log (`GET /api/system/log`, otherwise the
   Supervisor log API) and looks for `token expired` / `invalid_grant` /
   `cannot create vehicle type 'template'`. In addition it can renew preventively
   after `proactive_refresh_days` days. It can also be triggered from outside
   (HTTP, see below).
2. **Stop evcc** - PSA refresh tokens are single-use; while logging in, no second
   instance may work with the same token.
3. **Fetch tokens** - headless browser login via the built-in PSA token helper
   (Gigya login + consent) and PKCE code exchange. Output: `access_token`,
   `refresh_token`.
4. **Start evcc again** - right after fetching the tokens. The stop only protects
   the login window; the write-back runs over evcc's HTTP API and that needs a
   **running** evcc. Previously it wrote while still stopped - every call failed
   with `Connection refused`.
5. **Write back** - depending on where the vehicles are defined in evcc:
   * `mode: api` - vehicles from the evcc UI/DB (internally `db:<id>`): writing via the
     evcc configuration API (`PUT /api/config/devices/vehicle/<id>`), optionally with a
     test run (`POST /api/config/test/vehicle/merge/<id>`) *before* saving.
     If evcc afterwards requests a restart (`GET /api/config/dirty`), evcc is
     stopped **and** started - a `start` on a running add-on is a no-op and does not
     make the changed configuration effective.
   * `mode: yaml` - vehicles in `evcc.yaml`: targeted patching of the file
     (comments and formatting are preserved), backup beforehand; evcc stays stopped
     during it and is started afterwards.
   * `mode: auto` - works out for itself where the vehicles live.
6. **Verify** - after that the add-on reads the evcc log again and raises an alarm
   if the token error is still there. The result arrives as an HA notification.
   It compares against the log state *before* writing: an old error entry
   or the expected `invalid_grant` of the restart does not count as a failure.

Safety net: if a run ends while evcc is stopped, the add-on starts it again
in every case - even if the write path aborts with an error. evcc cannot
be left dead that way.

So that evcc does not reuse a dead token persisted in `evcc.db`,
the `user` field of the vehicles is set to a new generation
(`user@example.org#202609291200`). The key `psa.<brand>.<user>` in the
`settings` table is therefore new - the old, dead entry is never used again.
From evcc 0.315.1 on, evcc itself deletes a persisted token that the server rejects
with `invalid_grant`.

## HTTP interface security

The trigger listens on `trigger_port` (default 8909) on the host network. The only
protection is the `trigger_token` - whoever knows it can trigger a refresh, call
`/status` and `/dump` and start a probe run. Therefore:

- use a long random value (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`)
- treat the token like a password (not in chats, tickets or screenshots)
- if you suspect it leaked: change the token in the add-on (takes effect after the restart)

`/status` and `/dump` mask the values of sensitive options (`account_password`,
`evcc_admin_password`, `evcc_apikey`, `trigger_token`) as `***`; the key names stay
visible so you can see which option is set. Vehicle tokens are likewise
only output masked.

## HTTP interface (optional)

If `trigger_token` is set, the add-on listens on `trigger_port` (default 8909):

| Call | Effect |
| ---- | ------ |
| `POST /refresh` | starts a refresh (response 202, result via notification) |
| `POST /probe` | diagnosis without credentials: follows the login chain in the browser and returns the page dump (body optional `{"wait_seconds": 60}`) |
| `GET /status` | compact status (version, mode, vehicles, last runs) |
| `GET /dump` | like `/status`, plus the actual evcc vehicle configurations and the planned PUT body (tokens as placeholders) |

All calls need the header `X-Trigger-Token: <trigger_token>` (or `?token=`).
Without `trigger_token` the endpoint is disabled.

## FAQ: do I need the upstream helper?

No. The add-on is self-contained: `psa-browser-token.py` ships inside the image and is the
default for `helper_cmd`. It drives the login chain with QtWebEngine by polling the page
state, which is what works with the JavaScript-driven login pages PSA uses since 2026.

The older upstream "automated" script (`psa-token-helper-auto.py` from
[hurzhurz/psa-token-helper](https://github.com/hurzhurz/psa-token-helper)) is **not**
bundled here, for two reasons: that project has no licence file (so it may not be
redistributed), and it waits for QtWebEngine's `loadFinished` event, which no longer fires
on the current ForgeRock/Gigya chain - it typically ends in `timeout`.

If you still want to try it: fetch it from upstream yourself (and settle the licence
question with the author), then make it available inside the add-on either

* in a fork: add the file, extend the `COPY` line in `psa-token-refresh/Dockerfile` and set
  `helper_cmd: "python3 /app/<your-script>.py"`, or
* for a local test: copy it into the add-on's own `/data` directory through an SSH add-on
  (protection mode off; on the host that is
  `/mnt/data/supervisor/addons/data/<hash>_psa_token_refresh/`) and set
  `helper_cmd: "python3 /data/<your-script>.py"`.

`helper_cmd` is just an option, so a failing script is reported in the add-on log and cannot
break the rest of the add-on.

## Options (excerpt)

`evcc_ready_timeout` (default 150 s): wait time after an evcc restart until evcc
answers on its port again. During this time Home Assistant answers there itself (404).


| Option | Meaning |
| ------ | ------- |
| `mode` | `auto`, `api` or `yaml` |
| `brand`, `account_user`, `account_password`, `country` | PSA account (MyPeugeot/MyOpel/...) |
| `vehicles` | list of vehicles: `name` (title/name in evcc), `vin`, optionally `id` |
| `evcc_url` | evcc web interface, default `http://127.0.0.1:7070` (host_network) |
| `evcc_apikey` | ready-made API key (optional - otherwise it is created) |
| `evcc_admin_password` | evcc admin password; the add-on creates the API key from it |
| `evcc_apikey_auto` | create the key automatically and store it in `/data/evcc_apikey` |
| `evcc_set_admin_password` | `true` = create a missing evcc admin password from `evcc_admin_password` |
| `evcc_user` | only for the log/diagnosis |
| `evcc_slug` | internal slug of the evcc add-on; `auto` (default, the field must not be empty) = looked up automatically |
| `evcc_config_path` | path to `evcc.yaml` or `auto` (YAML mode) |
| `api_verify_with_test` | have evcc test the new tokens first, then save |
| `api_payload_nested` | only needed if evcc expects the PUT body nested |
| `check_interval_minutes` | check interval, default 60 |
| `proactive_refresh_days` | renew preventively after X days, `0` = off |
| `retry_backoff_minutes` | wait time after a failure |
| `rotate_user_key` | set the `user` field to a new generation (recommended) |
| `skip_while_charging` | do not renew while a car is charging |
| `helper_timeout` | timeout per login step (seconds); only the old helper uses this |
| `helper_deadline` | overall deadline of the new helper in seconds (default 240) |
| `helper_cmd` | program that fetches the tokens. Default: `python3 /app/psa-browser-token.py` |
| `trigger_token` | password for the HTTP interface, empty = disabled |
| `dry_run` | do everything, but do not change evcc (result in the log or as a file in `/data`) |
| `notify` | create notifications in Home Assistant |

## Which version is running? (self-diagnosis)

At startup the log shows (stderr, directly below the first line):

```
Add-on (per Supervisor): version 1.4.3 | hassio_api=True hassio_role=manager
homeassistant_api=True host_network=True protected=False
```

If the version of the file in the image differs from the version the Supervisor knows,
a warning follows: `WARNING: image reports version X, the Supervisor knows Y - the running image
does not match the state of the installation`. The cause is then almost always an update that was
not (fully) applied: in the add-on store "Check for updates", then press
**Update** on the add-on page (only checking installs nothing) and wait for the rebuild.
Missing permissions are also reported (`homeassistant_api is NOT granted ...`,
`hassio_role is 'default' ...`).

The container's first log line names the same version:
`[psa-token-refresh] Start (DISPLAY=:99, Version 1.4.3)`.

## When nothing happens after a failure

If a run fails, the add-on waits `retry_backoff_minutes` (default 30) until the
next attempt. The log then explicitly says:

    no action: after the last failure, still waiting 27 min (until 19:07)
    - last error: token helper failed (exit 1)

**Telling whether a run succeeded:** after a success the state holds
`last_success_ts` (real write) or `last_dry_run_ts` (dry run) - not
`last_success_ts`. From 1.4.10 on, `last_error` and `fails` are reset in the process, so an
old error does not look like a permanent condition.

Test immediately via the HTTP trigger (ignores the wait):

    curl -X POST -H "X-Trigger-Token: <token>" http://<host>:8909/refresh

If a new add-on version starts, a running wait expires automatically -
so after an update you do not have to wait.

## When writing returns "404 page not found"

With `dry_run: false` the add-on stops evcc for the login window and starts it
again afterwards. Right after the start **Home Assistant** still answers the port
itself - an unknown path then returns `404 page not found` (that is Go's standard
text, not evcc's). From 1.4.11 on the add-on waits for readiness (`GET /api/state`
until 200) and logs:

    evcc ready after 7 s (Version 0.316.1)

If instead `evcc does not respond after 150 s (...)` appears, evcc is not running -
then start evcc manually and repeat the run. The dry run is not affected by any of
this, because it never stops evcc.

## When the Supervisor returns 403

Stop/start of the evcc add-on go through the Supervisor API. If the Supervisor refuses
access (`HTTP 403`), check:

1. `hassio_api: true` (is in the add-on's `config.yaml`),
2. `hassio_role: manager` - without this role an add-on may not control other add-ons,
3. `evcc_slug` - exactly the slug of the evcc add-on (default `auto` = the add-on
   looks it up itself; other repos use e.g. `a0d7b954_evcc`).

The add-on states the status code in the log explicitly instead of failing silently.
The trace is also in the Supervisor log
(`Settings > System > Logs > Supervisor`).

## When the PSA login hangs (as of 10/2026)

Two places where it gets stuck in practice:

**1. Open consent checkboxes.** The Gigya page (`OPLoginPage.php`) regularly has a
checkbox for terms. If it stays empty, the login still goes through (`mode=afterLogin`),
but the next step (`am/oauth2c/OAuthProxy.jsp`) rejects the identity and the browser
lands on `am/XUI/#failedLogin` - then the chain starts over. The add-on therefore sets
checkboxes whose label looks like terms/conditions/privacy/consent before
submitting (log line `checkbox: ...`, in the dump `kaestchen_ergebnis` and
`checkbox_details`). Newsletter/marketing checkboxes are not touched.

**2. Server-side rejection.** If the log shows
`WARNING: login rejected by the server (#failedLogin)`, PSA itself discarded the login -
then only a new attempt at a later time helps (rate limit!), not more clicking.

**3. Two login pages, one valid code.** The chain contains **two** login pages that expect
the same credentials: the ForgeRock page `idpcvs.<brand>.com/am/XUI/#login/...` (an SPA that
renders with a delay and calls `/am/json/authenticate`) and the Gigya page
`id-dcr.<brand>.com/gigya/OPLoginPage.php`. The helper submits each page once (at most two
attempts, 5 s apart):

    [psa] login form: submit attempt 1 at https://id-dcr.peugeot.com/gigya/OPLoginPage.php?...
    [psa] login form: submit attempt 2 at https://idpcvs.peugeot.com/am/XUI/#login/&realm=...

After that comes `BrandIDproxyPage.php?mode=afterLogin`, and finally the redirect to the
brand scheme (`mymap://oauth2redirect/de?code=...`) - **that** code is the valid one. The
intermediate code at `.../am/oauth2c/OAuthProxy.jsp?code=...` belongs to the OIDC detour
and is deliberately ignored (it is listed as `uebersprungene_codes` in the dump).

## Background: the login chain and the helper

The helper walks the chain ForgeRock (idpcvs) -> Gigya login (OPLoginPage) -> OIDC
(id-dcr) -> **consent page** (`index/authorize-consentments`) -> redirect to the
brand scheme (`mymap://oauth2redirect/de?code=...`). On the consent page it presses the
consent button itself; if the page stays put, on the second attempt it sets open
checkboxes and presses again. The log lines for that:

    [psa] page: .../authorize-consentments?... | consent page from PSA - button is being pressed [Agree]
    [psa] consent page: attempt 1 (button)


Stellantis has rebuilt the login path. The chain today looks like this:

```
idpcvs.<brand>.com/am/oauth2/authorize      (from evcc parameters, PKCE)
  -> 301 /am/UI/Login  -> 302 /am/XUI/#login   ForgeRock interface (JavaScript!)
  -> RedirectCallback -> id-dcr.<brand>.com/oidc/op/.../authorize
  -> 302 BrandIDproxyPage.php                  loads the Gigya SDK
  -> JS -> OPLoginPage.php                     only here does the login page appear
```

The third-party helper shipped earlier only reacted to the browser's `loadFinished`
event and checked for `OPLoginPage.php` in the URL there. With JS redirects via the
ForgeRock interface this event does not fire reliably - so the helper ran into
its timeout (`timeout`, exit 1), regardless of the credentials.

From 1.4.0 on, a helper of its own (`psa-browser-token.py`) fetches the tokens:

* it **polls** the page state every 700 ms (URL, visible fields, buttons),
* fills the login page generically (first text/mail field + password field) and submits,
* confirms consent pages automatically,
* intercepts the redirect to the brand scheme and exchanges the code for tokens,
* on failure writes a dump to `/data/psa-login-debug.json` instead of only "timeout".

**Diagnosis without credentials** (does not log in, only shows how far the browser gets):

```bash
curl -sS -X POST -H "X-Trigger-Token: YOURTOKEN" \
     -H "Content-Type: application/json" -d '{"wait_seconds": 60}' \
     http://192.168.1.10:8909/probe
```

The response contains `deutung` (which step it is stuck in), `url_verlauf` and
`letzter_zustand` (visible fields/buttons). That lets you improve it in a targeted way
without sending credentials through the session. If it stays stuck on the login page,
a higher `helper_deadline` helps; if no page appears at all, PSA is probably blocking the
headless browser (then only manual token creation with `evcc token` remains).

## What is a slug?

A slug is an add-on's internal, technical identifier - not the display name.
The Supervisor always addresses add-ons by the slug (start, stop, log, info). The
slug is in the add-on's `config.yaml` (`slug: evcc`) and, for add-ons from
a repository, it additionally gets the identifier of the repository prepended, e.g.
`a1b2c3d4-evcc` (earlier with an underscore: `<hash>_evcc`).

That is why this add-on looks up the slug itself: it reads `GET /addons` and picks the
entry whose slug or name contains "evcc" (the exact slug `evcc` wins). An
explicitly set `evcc_slug` takes precedence but is checked and corrected
automatically if needed. The found list is in the log, and in the diagnosis
(`GET /dump`) additionally `evcc_slug_resolved` and all known add-ons.

If the slug is wrong, evcc is left untouched: the state cannot be determined,
the add-on aborts and reports that - it does not consider evcc stopped.

**The field must not be empty.** `evcc_slug` is declared optional (`str?`) in the add-on
definition - but an *empty* value in `options` still makes it a mandatory option for the
Supervisor. Leaving the field empty while saving produces

```
Speichern fehlgeschlagen: Missing required option 'evcc_slug' in
Stellantis/PSA Token Refresh for evcc (<hash>_psa_token_refresh)
```

and the form cannot be saved at all until something is in there. That is why `1.4.12`
ships `auto` as the default: same meaning as "empty" (the slug is looked up), but
saveable. The value itself is not a secret and not a login - it is only the name the
Supervisor uses to address the evcc add-on.

## Creating the evcc API key automatically

The key no longer has to be created by hand in evcc. If `evcc_apikey` is empty and
`evcc_admin_password` is set, the add-on fetches the key itself:

```
GET  /api/auth/status                     -> disabled | configured | missing(501) | locked
PUT  /api/auth/password {"new": "..."}    -> only if evcc has no password yet (option)
POST /api/auth/login {"password": "..."}  -> Set-Cookie: auth=<JWT>
POST /api/auth/apikey {"password": "..."} -> {"key": "..."}
```

**The order is mandatory:** evcc puts the key endpoints behind `EnsureAuthHandler`
(`server/http.go`: "API key endpoints require an authenticated session"). A call without
a prior login returns **401, before the password is even checked** - that then
looks like a wrong password but is not. The add-on therefore logs in first via
`POST /api/auth/login` and creates the key with the same session. If the
key creation fails, it continues with this session (and passes the admin password on
to the client, so no subsequent call runs into 401).

Order: key from the options -> stored key (`/data/evcc_apikey`, mode 600) ->
create new and store -> last resort session login. An existing, still working key is
**not** rotated (evcc knows only one API key; a new one
invalidates the old one). Error cases are named explicitly in the log and via HA
notification (401 = wrong password, 501 = no admin password exists in evcc yet).

## Permissions / technical details

- `host_network: true` - so that `127.0.0.1:7070` hits the evcc add-on.
- `hassio_api` + `hassio_role: manager` - stop/start of the evcc add-on and HA notifications.
- `homeassistant_api: true` - for notifications in Home Assistant
  (`POST /core/api/services/...`). Without this permission the Supervisor returns
  `HTTP 401`; the add-on points this out in the log.
- `map: all_addon_configs:rw` - only needed for YAML mode.
- `arch: amd64` - PyQt5/PyQtWebEngine are only available for x86_64 on PyPI.

## Limits

- Two-factor login on the PSA account -> the automatic login fails.
- Changes to the PSA/Gigya login page can break the helper (classic
  "timeout" in the log).
- The add-on needs your PSA password as an add-on option (it sits in
  `/data/options.json` in plain text, as is usual for HA add-ons).
- The helper is an upstream "proof of concept"; nothing here is bought or guaranteed.
