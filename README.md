# stellantis-evcc-homeassistant-psa-token-refresh

Stellantis / PSA token refresh for **evcc** - a Home Assistant app.

A Home Assistant add-on that keeps the PSA/Stellantis OAuth tokens for your
Peugeot / Opel / Citroën / DS vehicles valid so that **evcc renews them by itself**
when they expire - including stopping and restarting evcc and verifying that it
really works afterwards.

## Features

- **Detect** expired PSA tokens in the evcc log (`token expired`, `invalid_grant`,
  `cannot create vehicle type 'template'`) and renew them; optionally renew
  proactively after a configurable number of days.
- **Both vehicle layouts**: vehicles configured through the evcc UI/database
  (updated via the evcc configuration API) and vehicles defined in `evcc.yaml`
  (updated by a comment-preserving patcher).
- **Headless browser login** through the built-in PSA token helper (Gigya login +
  consent page, PKCE code exchange).
- **Safe evcc handling**: evcc is stopped only for the login window and is
  guaranteed to be running again afterwards - even if a run fails.
- **evcc API key created automatically** from the evcc admin password (no manual
  key creation needed).
- **HTTP trigger** for Home Assistant automations, plus masked `/status` and
  `/dump` diagnostic endpoints.
- Home Assistant notifications, a dry-run mode and a retry backoff.
- **Self-tests** that need neither network nor Docker.

## Requirements

| Requirement | Why |
| ----------- | --- |
| Home Assistant OS / Supervised with Supervisor | add-ons and the stop/start API only exist there |
| **amd64** (x86_64) | PyQt5/PyQtWebEngine exist on PyPI only for x86_64 - the image cannot be built on a Raspberry Pi |
| evcc as an **add-on** | the package stops/starts exactly this add-on |
| PSA account **without two-factor login** | the automatic login cannot do 2FA |
| about 700 MB free disk | Python + QtWebEngine + Xvfb |

## Repository layout

```
stellantis-public/
├── README.md                     <- this guide
├── repository.yaml               <- add-on repository file
├── psa-token-refresh/            <- the add-on
│   ├── config.yaml               add-on definition (options, schema, permissions)
│   ├── Dockerfile                image (Python + Xvfb + QtWebEngine)
│   ├── run.sh                    starts Xvfb and the orchestrator
│   ├── orchestrator.py           flow control (detect, stop, fetch, write, verify)
│   ├── evcc_api.py               evcc configuration API (vehicles from the DB/UI)
│   ├── patch_evcc_yaml.py        YAML mode (comments are preserved)
│   ├── psa-browser-token.py      token helper (login chain, QtWebEngine)
│   ├── DOCS.md / CHANGELOG.md
├── homeassistant/
│   ├── rest_command.yaml         rest_command + REST sensor
│   ├── automation.yaml           automations
│   └── scripts.yaml              manual button
├── scripts/
│   ├── run_tests.sh              all self-tests
│   └── evcc_psa_token_reset.sh   emergency: remove a dead token from evcc.db
└── tests/
    ├── test_patch_evcc_yaml.py     (YAML patcher)
    ├── test_evcc_api.py            (API calls, payload, auth, errors)
    ├── test_orchestrator_api.py    (full flow against a fake evcc)
    ├── test_supervisor_api.py      (Supervisor integration, slug lookup)
    ├── test_psa_browser_token.py   (helper logic)
    ├── test_api_mode_evcc_stop.py  (stop/start path with a real Supervisor stub)
    └── test_backoff.py             (retry backoff)
```

## Installation

**Option A - add-on repository (recommended for versioning):**

1. From Git server (**RECOMMENDED**).
2. In Home Assistant: **Settings > Apps > App Store > Repositories** and add the
   repository URL (e.g. `https://github.com/35develr/stellantis-evcc-homeassistant-psa-token-refresh`).
3. "Check for updates", then install **Stellantis/PSA Token Refresh for evcc** from the store.

If the Supervisor does not show the add-on (it does not appear in the list), the
reason is in the Supervisor log - usually a typo or an indentation error in
`config.yaml`.

**Option B - local add-on folder (no Git needed):**

1. Go to the Supervisor data folder. On **Home Assistant Supervised** (Debian +
   Docker, the default layout) it is `/usr/share/hassio/addons`; the real path is
   shown by
   `docker inspect hassio_supervisor --format '{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ "\n" }}{{ end }}' | grep -iE 'addons|srv'`.
2. Copy the folder **`psa-token-refresh`** there, i.e. `…/addons/psa-token-refresh/…`
   (`sudo cp -r`).
3. Add-on store -> three-dot menu -> "Check for updates". Under "Local add-ons"
   the entry **Stellantis/PSA Token Refresh for evcc** appears.
4. Install it (the image is built on the HA machine; this takes a few minutes).


## Configuration

In the add-on tab **Configuration**:

```yaml
mode: auto                 # auto detects API/DB vehicles by itself
brand: peugeot
account_user: "user@example.org"   # MyPeugeot account
account_password: "…"
country: de
vehicles:
  - name: e3008            # title or name as in evcc
    vin: "VF3…"            # optional, only for cross-checking
  - name: e5008
    vin: "VF3…"
evcc_url: "http://127.0.0.1:7070"
evcc_apikey: ***            # leave empty = key is created automatically
evcc_admin_password: "…"   # evcc admin password; the API key is derived from it
evcc_apikey_auto: true     # create the key automatically and store it in /data/evcc_apikey
evcc_set_admin_password: false  # true = create a missing evcc admin password
evcc_slug: "auto"          # auto = slug is looked up automatically (must not be empty)
check_interval_minutes: 60
proactive_refresh_days: 30
trigger_token: "…"         # long random value, enables the HTTP interface
notify: true
dry_run: true              # <<< for the first test
```

Do **not** start it yet - run the dry run first (see below).

The most important options:

| Option | Meaning |
| ------ | ------- |
| `mode` | `auto`, `api` or `yaml` |
| `brand`, `account_user`, `account_password`, `country` | PSA account (MyPeugeot/MyOpel/…) |
| `vehicles` | list of vehicles: `name` (title/name in evcc), `vin`, optionally `id` |
| `evcc_url` | evcc web interface, default `http://127.0.0.1:7070` (host_network) |
| `evcc_apikey` | ready-made API key (optional - otherwise it is created) |
| `evcc_admin_password` | evcc admin password; the add-on creates the API key from it |
| `evcc_apikey_auto` | create the key automatically and store it in `/data/evcc_apikey` |
| `evcc_set_admin_password` | `true` = create a missing evcc admin password from `evcc_admin_password` |
| `evcc_slug` | internal slug of the evcc add-on; `auto` (default) = looked up automatically, the field must not be empty |
| `evcc_config_path` | path to `evcc.yaml` or `auto` (YAML mode) |
| `api_verify_with_test` | have evcc test the new tokens first, then save |
| `check_interval_minutes` | check interval, default 60 |
| `proactive_refresh_days` | renew preventively after X days, `0` = off |
| `retry_backoff_minutes` | wait time after a failure |
| `rotate_user_key` | set the `user` field to a new generation (recommended) |
| `skip_while_charging` | do not renew while a car is charging |
| `helper_deadline` | overall deadline of the helper in seconds (default 240) |
| `trigger_token` | password for the HTTP interface, empty = disabled |
| `dry_run` | do everything, but do not change evcc (result in the log or as a file in `/data`) |
| `notify` | create notifications in Home Assistant |

### The evcc API key is created automatically

You no longer need to create the API key by hand. Enter the evcc **admin
password** in the add-on (`evcc_admin_password`); the add-on derives the API key
from it and stores it permanently under `/data/evcc_apikey` (mode 600):

```
POST /api/auth/apikey  {"password": "<admin password>"}  ->  {"key": "..."}
```

Order inside the add-on (in this sequence):

1. `evcc_apikey` from the options - if set and valid, it is used.
2. Stored key from `/data/evcc_apikey` - so the key survives an add-on restart or
   an update.
3. Otherwise: create a new one with the admin password and store it.
4. Last resort: session login with the admin password (cookie), if evcc refuses
   key creation.

Worth knowing:

- **evcc has only one API key.** A newly created key invalidates the old one - the
  add-on therefore only creates a new one if the existing one is rejected.
- If your evcc does **not** yet have an admin password (the status query answers
  with HTTP 501), the add-on reports this in the log. Either set one in evcc - or
  enable the option `evcc_set_admin_password: true` and the add-on sets the
  password from `evcc_admin_password` in evcc (this changes your evcc installation!).
- Wrong password -> clear message `evcc rejected the admin password (HTTP 401)`.
- Alternatively you can still create the key yourself in evcc and enter it as
  `evcc_apikey`; then nothing automatic happens.

## How it works

1. **Detect** - the add-on reads the evcc log (`GET /api/system/log`, otherwise the
   Supervisor log API) and looks for `token expired` / `invalid_grant` /
   `cannot create vehicle type 'template'`. It can also renew proactively after
   `proactive_refresh_days` days, and it can be triggered from outside (HTTP, see below).
2. **Stop evcc** - PSA refresh tokens are single-use; while logging in, no second
   instance may use the same token.
3. **Fetch tokens** - headless browser login via the built-in helper (Gigya login +
   consent page) and PKCE code exchange.
4. **Start evcc again** - right after fetching the tokens. The stop only protects
   the login window; the write-back runs over evcc's HTTP API, which needs a
   **running** evcc.
5. **Write back** - `mode: api` updates UI/DB vehicles via the evcc configuration
   API (optionally testing the new configuration first); `mode: yaml` patches
   `evcc.yaml` (comments preserved, backup taken); `mode: auto` detects where the
   vehicles live. If evcc wants a restart afterwards, it is stopped **and** started.
6. **Verify** - the add-on reads the log again and raises an alarm if the token
   error is still there, comparing against the state *before* the write. The
   result arrives as an HA notification.

So that evcc does not reuse a dead token persisted in `evcc.db`, the `user` field
of the vehicles is set to a new generation (`user@example.org#202609291200`); the
old entry is never used again. From evcc 0.315.1 on, evcc deletes a token the
server rejects with `invalid_grant` by itself.

### Dry run (`dry_run: true`)

Start the add-on with `dry_run: true`. Then:

- vehicles are looked up in the evcc DB, tokens are fetched,
- the planned `PUT` body is written to `/data/dryrun-vehicle-<id>.json` (tokens truncated),
- evcc is left **untouched**.

You can also fetch the diagnosis (with the `trigger_token`):

```bash
# from the SSH add-on or from a machine on the LAN
curl -s -H "X-Trigger-Token: YOURTOKEN" http://192.168.1.10:8909/status | python3 -m json.tool
curl -s -H "X-Trigger-Token: YOURTOKEN" http://192.168.1.10:8909/dump   | python3 -m json.tool
```

`/dump` shows the vehicle list from the evcc DB, the recognised masked fields
(`accessToken`, `refreshToken`) and exactly the body the add-on would send. If that
looks plausible, the wiring is fine.

### Going live

1. Set `dry_run: false`, save the options, restart the add-on.
2. Wait for the first real run in the add-on log (`--once` does not happen
   automatically; the scheduler checks every `check_interval_minutes`) - or trigger
   it immediately:
   ```bash
   curl -s -X POST -H "X-Trigger-Token: YOURTOKEN" \
        -H "Content-Type: application/json" -d '{"reason":"first run"}' \
        http://192.168.1.10:8909/refresh
   ```
3. Expected sequence in the log:
   ```
   starting refresh - reason: first run
   fetching new tokens (peugeot) - this takes 30-90 s ...
   tokens received (access 1xx characters, refresh 1xx characters, expires_in 3599)
   - e3008: test config accepted by evcc
   - e3008: new tokens stored in evcc (user -> user@example.org#202609291215)
   starting evcc add-on ... evcc is running again
   refresh successfully verified (API mode)
   ```
   At the end a notification "PSA tokens renewed" arrives in Home Assistant.

If you do not want the test run of the new configuration before saving
(`POST /api/config/test/vehicle/merge/<id>` queries the car and can take 30-60 s),
set `api_verify_with_test: false`.

### Afterwards: let the automation run

- The add-on checks the evcc log itself every `check_interval_minutes` and also
  renews preventively after `proactive_refresh_days` days. In the normal case
  there is **nothing more to do**.
- To additionally react to an HA event, copy the contents of
  `homeassistant/rest_command.yaml` and `homeassistant/automation.yaml` into your
  `configuration.yaml` / `automations.yaml` and adjust the IP and the entity IDs
  of the vehicle sensors there (`sensor.evcc_e3008_soc` may be named differently -
  Developer Tools -> States -> filter by `evcc`).
- `secrets.yaml` needs the same value as `trigger_token`:
  `psa_trigger_token: "…"`.

## HTTP trigger

If `trigger_token` is set, the add-on listens on `trigger_port` (default 8909):

| Call | Effect |
| ---- | ------ |
| `POST /refresh` | starts a refresh (response 202, result via notification) |
| `POST /probe` | diagnosis without credentials: follows the login chain in the browser and returns the page dump (body optional `{"wait_seconds": 60}`) |
| `GET /status` | compact status (version, mode, vehicles, last runs) |
| `GET /dump` | like `/status`, plus the actual evcc vehicle configurations and the planned PUT body (tokens as placeholders) |

All calls need the header `X-Trigger-Token: <trigger_token>` (or `?token=`).
Without `trigger_token` the endpoint is disabled.

## Security

The trigger listens on `trigger_port` (default 8909) on the host network. Its only
protection is the `trigger_token` - whoever knows it can trigger a refresh, call
`/status` and `/dump` and start a probe run. Therefore:

- Use a long random value
  (`python3 -c "import secrets;print(secrets.token_urlsafe(24))"`).
- **Treat `trigger_token` like a password** - not in chats, tickets or screenshots.
- If you suspect it leaked, change the token in the add-on (it takes effect after
  the restart).

`/status` and `/dump` mask the values of sensitive options (`account_password`,
`evcc_admin_password`, `evcc_apikey`, `trigger_token`) as `***`; the key names stay
visible so you can still see which option is set. Vehicle tokens are also only
output masked.

Note that add-on options, including passwords, sit in plain text in the add-on's
`/data/options.json` (as is usual for HA add-ons).

## Self-tests

```bash
bash scripts/run_tests.sh
```

The tests need neither network nor Docker: they exercise the YAML patcher, the
structure of the evcc API calls and the complete flow against a local fake evcc
plus a stub token helper. All test files must pass.

## Troubleshooting / Documentation

Full documentation lives in **[`psa-token-refresh/DOCS.md`](psa-token-refresh/DOCS.md)**.

| Log symptom | Cause / fix |
| ----------- | ----------- |
| `cannot access …` / `HTTP 401` | `evcc_apikey` missing or wrong |
| `token helper failed … 'timeout'` | since 09/2026 mostly the **new ForgeRock/Gigya login chain** (see below) - the old helper missed the JS redirect. Since 1.4.0 a helper of its own, polling, is used; otherwise raise `helper_deadline` |
| `no vehicles configured via UI/DB found` | vehicles are in `evcc.yaml` after all -> set `mode: yaml` |
| `vehicle 'x' not found via UI/DB` | adjust the names (the error message lists all existing vehicles with their titles) |
| `evcc could not be stopped` | check `hassio_api` / `hassio_role: manager`, verify `evcc_slug` |
| `App a1b2c3d4-evcc does not exist` | the slug was wrong - since 1.3.0 the add-on looks it up itself (`GET /addons`). Manually: enter the name from the log line "Supervisor knows N add-on(s): …" as `evcc_slug` |
| `Missing required option 'evcc_slug'` while saving | the field was empty - an empty value in `options` is a mandatory option for the Supervisor. Since 1.4.12 it ships as `auto` (the slug is looked up) |
| `notification failed (401)` | the add-on is missing `homeassistant_api: true` (included in `config.yaml` since 1.3.0) |
| `evcc still reports token problems` | check the evcc log; evcc < 0.315.1: see the emergency tool below |
| `test of the new tokens … failed` | the freshly fetched tokens were not accepted by PSA - usually the login itself is the problem |

### PSA login: diagnosis without credentials

If token recovery gets stuck at the login, `POST /probe` (with `trigger_token`)
shows how far the browser gets - without any credentials being needed:

```bash
curl -sS -X POST -H "X-Trigger-Token: YOURTOKEN" -H "Content-Type: application/json" \
     -d '{"wait_seconds": 60}' http://192.168.1.10:8909/probe
```

The response contains `deutung` (which step is stuck), `url_verlauf` and `letzter_zustand`
(visible fields/buttons). The dump is also written to `/data/psa-login-debug.json`,
and on a failed run the add-on names the same cause in the log.

**Emergency tool** (only for evcc < 0.315.1, or if the persisted token is stuck):
run `scripts/evcc_psa_token_reset.sh` in the "Advanced SSH & Web Terminal" add-on
(protection mode off). It shows the `psa.*` entries in the `settings` table of
`evcc.db` and deletes them after confirmation - including a backup.

## Limits and risks - please read

- **Two factors on the PSA account:** then the automatic login fails permanently.
  The add-on is then useless; the manual route (Token Helper with a browser) remains.
- **Single-use refresh tokens:** this is why the add-on stops evcc before the login
  and starts it again afterwards. If a second token consumer runs in parallel
  (e.g. a second evcc instance or the PSA Car Controller add-on), the account can
  be locked out.
- **Charging:** `skip_while_charging` prevents stopping in the middle of a charge;
  if it happens anyway, evcc continues to regulate after the restart as set at the
  wallbox before.
- **Rate limits / locks:** frequent full logins are conspicuous. Do not test every
  minute; do not set `check_interval_minutes` below 30.
- **The password sits in plain text** in the add-on's `/data/options.json` (usual
  for HA add-ons).
- **Legal note:** the login chain (Gigya login form, consent page, custom-scheme redirect
  with PKCE) and the brand app credentials were derived from
  [hurzhurz/psa-token-helper](https://github.com/hurzhurz/psa-token-helper), which at the
  time of writing ships **no licence file** - legally "all rights reserved". No file from
  that project is bundled here and the implementation is our own, but if you need a fully
  clean chain of rights, ask the upstream author for a licence or explicit permission.
- **Maintenance:** PSA/Gigya change the login page from time to time. The script
  then has to be adapted - the automation cannot heal that itself.

## Credentials: what is in this repository and what is not

**No personal data.** The repository contains no account name, no password, no vehicle
identification number, no access or refresh token and no trigger token. Everything
user-specific is entered in the add-on options in Home Assistant at runtime and stays
there (`/data` of the add-on, mode 600 for the API key file).

**The only keys in the code** are the OAuth *client credentials of the brand apps*
(`client_id` / `client_secret` for Peugeot, Citroen, DS and Opel). They identify the
**application**, not a user - they are not linked to any account, vehicle or personal
data. A mobile app cannot keep a secret, so these values are public by design, and they
are exactly the ones the upstream projects (evcc and hurzhurz/psa-token-helper) publish.

**Is publishing them a risk?**

* For you as the add-on user: no. Your cars are linked to your PSA account (e-mail and
  password), which never leaves your system. Publishing the app credentials reveals
  nothing about your vehicles.
* The real, shared risk: if the brand ever rotates these credentials, this add-on *and*
  evcc stop working until they are updated. Using them with an unofficial client may also
  conflict with the brand's terms of use - the same situation every PSA automation project
  is in.

**Prefer to use your own credentials?** Extract them from your own copy of the app and
override the built-in ones without touching the code:

```bash
PSA_CLIENT_ID_PEUGEOT=...      PSA_CLIENT_SECRET_PEUGEOT=...
PSA_CLIENT_ID_CITROEN=...      PSA_CLIENT_SECRET_CITROEN=...
PSA_CLIENT_ID_DS=...           PSA_CLIENT_SECRET_DS=...
PSA_CLIENT_ID_OPEL=...         PSA_CLIENT_SECRET_OPEL=...
```

There is no public developer programme that issues these for personal use; extracting
them from the app is the usual route - which is why working defaults ship in the code.

## Credits / Upstream

- **[hurzhurz/psa-token-helper](https://github.com/hurzhurz/psa-token-helper)** - the
  login chain (Gigya login form, consent page, custom-scheme redirect with PKCE) and the
  brand app credentials were derived from this project. No file from it is bundled here;
  see the legal note above.
- **[evcc](https://evcc.io)** - the PSA/Stellantis vehicle integration whose tokens this
  add-on keeps alive, and whose configuration API and log are used.

## Licence

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 35develr.

You may use, modify and redistribute this software, including commercially, provided the
copyright notice and the licence text stay with it. The software is provided without any
warranty.
