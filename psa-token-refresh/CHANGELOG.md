# 1.4.12 (2026-10-13)

- **Bugfix: the configuration form can be saved again without inventing an `evcc_slug`.**
  An option entry with an empty value (`evcc_slug:` with nothing after it) is not "empty"
  for the Supervisor, it is a **mandatory option**: saving only worked after *something*
  had been entered, otherwise it answered
  `Speichern fehlgeschlagen: Missing required option 'evcc_slug' in Stellantis/PSA Token
  Refresh for evcc (<hash>_psa_token_refresh)`. `evcc_slug` therefore now defaults to
  **`auto`**, which is exactly what the previously empty field did (the slug is looked up
  automatically via `GET /addons`). The field ships pre-filled and can be saved right
  away; the schema entry stays `str?`.
  The same applies to every other option: an empty value in `options` makes it mandatory,
  no matter what the schema says.
- New self-test `tests/test_config_yaml.py` (wired into `scripts/run_tests.sh`) pins this
  down: no option with an empty value, a schema entry for every option, a default value
  for `evcc_slug`, and the version in `config.yaml` must match the CHANGELOG (otherwise
  there is no update in the app store).

# 1.4.11 (2026-10-01)

- **Bugfix: after the evcc restart the add-on now waits for readiness.** With `dry_run: false`
  the add-on stops evcc for the login window, fetches the tokens, starts evcc again -
  and wrote immediately. Between the start and evcc claiming the port, **Home
  Assistant** answers the port itself (also a Go server) and replies to unknown paths
  with `404 page not found`. The test/write call therefore failed seemingly for no reason
  (run of 2026-10-01, `fails=7`, "test of the new tokens for e3008 failed: HTTP 404").
  In the dry run this does not happen, because evcc is never stopped there - which is why the
  dry run passed and the real write did not.
  Now: `EvccApi.wait_until_ready()` polls `GET /api/state` (allowed without authentication)
  for up to 150 s; only once a genuine evcc response (200) arrives is anything
  written. If the port does not answer as evcc after 150 s, the run aborts with a clear
  message (and evcc is started again by the safety net).
- Error messages of the write calls now name the method and path and, on 404, point to
  Home Assistant taking over the port.
- `/status` shows `evcc_version` - so it is immediately visible which evcc instance the
  add-on actually reaches.
- New test that reproduces the port window (first 404, then 200 with version) and checks
  that nothing is written beforehand.

# 1.4.10 (2026-10-01)

- **Success clears the error state** (in the dry run too): `last_error` is removed,
  `fails` set to 0 and any wait time (`next_attempt_ts`) set to 0. Previously, after a
  successful dry run the old error and its wait time stayed in the state - that looked
  like a stuck run and misled the success check (finding of
  2026-10-01: the run was successful, `last_dry_run_ts` was set, but the state still
  held the previous day's error).
- New test `test_dry_run_success_clears_previous_failure`: simulates an old
  failure in the state and checks that a successful dry run removes it completely.

# 1.4.9 (2026-10-01)

Analysis of the run of 2026-10-01, 10:41 (1.4.8): the chain ran
`am/XUI` (passed) -> Gigya login (submitted, `mode=afterLogin` reached, 10:41:10)
-> back to `am/XUI` -> **`am/XUI/#failedLogin`** (10:41:41) -> OIDC again -> Gigya.
The consent button was not involved (on `BrandIDproxyPage` there was none, and
the strict detection correctly did not click); on the ForgeRock page nothing was
submitted (`login_seiten` has only one entry) - so `#failedLogin` came from the server.

Noticeable: the Gigya login page has an **open checkbox**
(`checkboxes_unchecked: 1`) that was ignored on submit. Many login pages
require consent to terms, otherwise the login stays incomplete and the
next step (here the `OAuthProxy`, which takes over the Gigya identity) fails.

- **New: set consent checkboxes before submitting** (`TERMS_JS`). Deliberately narrow:
  only checkboxes whose label/name suggests terms, conditions, privacy, terms,
  consent or accept - newsletter/marketing are left untouched.
  The result is in the log and in the dump (`kaestchen_ergebnis`).
- **Checkbox labels in the dump** (`checkbox_details` per page): you now see
  which checkboxes were open and what they are called - instead of just the count.
- **`#failedLogin` is highlighted**: server-side rejections land in the log
  ("WARNING: login rejected by the server") and in the dump (`server_fehler`).
- Tests extended (TERMS_JS patterns, checkbox labels, marketing separation).

# 1.4.8 (2026-10-01)

Analysis of the run of 2026-10-01, 09:59 (first run with 1.4.7): the flow got as far as
`BrandIDproxyPage.php?mode=afterLogin`, then ForgeRock delivered the code to
`idpcvs.peugeot.com/am/oauth2c/OAuthProxy.jsp?code=...` - and the browser landed back
on the ForgeRock login page (`/am/XUI/#login/...`, loads `_Password.html`, calls
`/am/json/authenticate`), where it stayed until the timeout.

- **Bugfix: one login per page instead of a single one.** `login_submitted` was a one-off flag -
  after the Gigya login (OPLoginPage.php) the helper refused the second login on the
  ForgeRock page, although both expect the same credentials. Now: one submit attempt
  per page, at most 2 attempts, at least 5 s apart. Every attempt is in the log with
  the page URL and in the dump (`login_seiten`).
- **Bugfix: consent page no longer confused.** The detection now checks only
  **path and title** - before, the query parameters counted too, and the Gigya intermediate step
  carries a base64-encoded `context` that can contain "consent". As a result the helper
  clicked on the BrandIDproxyPage instead of the real consent page.
  Gigya/OPLoginPage/XUI pages are explicitly excluded.
- Diagnosis: `login_seiten` in the dump, every login attempt logged individually.
- 2 new tests (login per page incl. budget and deadline, consent detection without
  query parameters) - the test file now has 17 tests.

# 1.4.7 (2026-10-01)

- **Security fix**: `/status` and `/dump` output the add-on options **in plain text** -
  including `account_password`, `evcc_admin_password`, `evcc_apikey` and `trigger_token`
  (finding of 2026-10-01). The Supervisor delivers them unfiltered under `/addons/self/info`;
  the add-on passed them on unchanged. Now the values of sensitive options are
  masked (`***`), the key names stay visible. Affects `addon_info` in both
  endpoints.
- **Consent page is handled several times**: the first run after the fix (01.10., 08:28)
  got stuck again - the page has a `CONTINUE` button, but after the first click
  it was permanently "wait". Now: up to 4 attempts 4 s apart, from the second
  attempt on open checkboxes are set and the button is pressed again.
- **More realistic click**: full mouse event chain (`mouseover/mousedown/mouseup/click`)
  incl. walking up to the next clickable parent element and `form.submit()` for
  submit buttons - some pages do not react to synthetic `click()` calls.
- **Diagnosis via /dump**: `/dump` now additionally delivers the helper's diagnosis
  (`helper_diagnosis`), including a **page inventory** (button labels, iframes,
  open checkboxes, actions per page) and the intercepted requests. That makes
  troubleshooting possible without access to the container.
- Snapshot reports iframes (the PSA interface can live there).

# 1.4.6 (2026-09-30)

- **Bugfix: the consent page is now handled.** The run of 2026-09-30, 18:59 ran
  240 s into the timeout because the helper stayed on
  `id-dcr.<brand>.com/index/authorize-consentments?...`. Cause: the
  snapshot did not collect the consent buttons at all (`consent_buttons` was missing),
  so `decide_action()` could never return `consent` - the click was never attempted.
- Snapshot now reports `consent_page`, `consent_buttons`, `consent_labels` and the
  number of open checkboxes; the interpretation in the log names the button labels.
- Detection more relaxed: consent words are searched anywhere in the text (previously only at
  the start of a word), plus `input[type=button]` and `aria-label`.
- Second attempt: if the page stays put, from the second click on open checkboxes are
  set (`CHECKBOX_JS`) and pressed again - many PSA pages require both. Every
  attempt is in the log with its result.
- 2 new tests (consent page is detected and handled, escalation to checkboxes) plus
  checks that the snapshot really delivers `consent_buttons`.

# 1.4.5 (2026-09-30)

- **Clarity instead of "status ok"**: if the wait time is still running after a failure
  (`retry_backoff_minutes`, default 30), the add-on now reports that explicitly with
  remaining time, end time and the cause of the last failure. Before, it said
  "status ok - no refresh needed", which looked like a healthy system
  although in truth a wait was running.
- **Update resolves the wait time**: if a new add-on version starts, a
  running wait is discarded (noted in the log). So after an update you do not have to wait
  up to 30 minutes for the next attempt. The version is carried in the state.
- New test file `tests/test_backoff.py` (5 tests): wait time is reported, expired
  wait does not block, new version resolves it, same version does not.

# 1.4.4 (2026-09-30)

- **Bugfix (cause of "token exchange failed")**: the new helper grabbed the
  authorization code from *every* URL with `code=`. In the new flow, however, the
  intermediate steps also carry a code (Gigya/OIDC detour on `id-dcr.<brand>.com`), so the
  exchange ran with the wrong code. The old version correctly allowed only the
  brand scheme. Now: code **only** from `mymap://`/`mymopsdk://`/... and
  additionally only with a matching `state`.
- New: the helper sends a `state` of its own and accepts only the redirect that
  returns it. That way no foreign or old redirect can match any more.
- Diagnosis extended: `request_verlauf` (all intercepted requests) and
  `uebersprungene_codes` (intermediate redirects deliberately ignored) are in the dump; the log
  names the cause from diagnosis in plain text on a failure.
- 2 new tests (brand scheme only, `state` check) + test for the state parameters of the
  authorize URL; test file now 13 tests.

# 1.4.3 (2026-09-30)

- **Self-diagnosis at startup**: the add-on reads `GET /addons/self/info` and logs
  version, `hassio_api`, `hassio_role`, `homeassistant_api`, `host_network`, `protected`.
  If the image version differs from the version the Supervisor knows, there is an
  explicit warning - so "which version is actually running here?" can be answered
  immediately from the log (bad images from updates that were not applied stand out).
- Warnings on missing permissions (`homeassistant_api`, `hassio_api`, `hassio_role`)
  in plain text instead of silent 401/403.
- The same information is in `--check` / `GET /status` (`addon_info`, `addon_diagnostics`).
- 4 new tests for the self-diagnosis (permissions, version conflict, missing Supervisor).

# 1.4.2 (2026-09-30)

- **Bugfix**: the new helper aborted immediately with `AttributeError: 'Namespace' object has no
  attribute 'country'` - the option is called `--countrycode`, but `args.country` was used.
  The country code is now set via `dest="country"`.
- **Error class eliminated**: `build_arg_parser()` and `check_args()` are functions of their own;
  a test statically checks via the syntax tree that every `args.<name>` used in the source
  exists after parsing - exactly the error that occurred here.
- Additionally: a test that the add-on's argument list (`-b -u -p -c -t --deadline`) fills all
  fields, that `--probe` runs without credentials, that missing credentials are
  rejected and that the JS templates can be rendered.
- Test file now 10 tests.

# 1.4.1 (2026-09-30)

- **Bugfix (critical)**: in `mode: api`, evcc was stopped, the tokens fetched and
  then written via evcc's HTTP API - **while evcc was still stopped**. Every
  call failed with `[Errno 111] Connection refused`, and evcc was left stopped
  (charge control was out of action afterwards). The stop only protects the
  login window; evcc is now started again right after fetching the tokens, before
  anything is written.
- **Bugfix**: an evcc reported as `dirty` was "answered" with a `start` - a no-op with
  a running evcc. A changed configuration is now made effective by a real restart
  (stop+start).
- **Safety net**: `run()` starts evcc again in every case if a run leaves it
  stopped - in API mode as well as YAML mode, even on errors in the write path.
- **Bugfix**: verification in API mode now checks the evcc log against the state
  *before* writing (like YAML mode). Old error lines and the expected
  `invalid_grant` of the restart no longer count as a failure.
- **Test gap closed**: `tests/test_api_mode_evcc_stop.py` emulates the Supervisor
  and makes the fake evcc genuinely unreachable during the stop (listener closed).
  Before, no test set `SUPERVISOR_TOKEN`, the stop path was never entered and
  the suite was green although the production path was broken.
- 4 new tests; against the old version 3 of them are red.

# 1.4.0 (2026-09-29)

- **New token helper `psa-browser-token.py`** (our own version, default): polls the
  page state instead of waiting for `loadFinished`, fills the login page generically, confirms
  consent pages and exchanges the code for tokens. Cause: Stellantis put a ForgeRock interface
  in front of the Gigya page (`/am/XUI/#login`), whose JS redirect
  the old helper missed -> `timeout`.
- **Diagnosis**: on failure `/data/psa-login-debug.json` is written (URL history,
  title, fields, buttons, interpretation); the log names the cause in plain text.
- **`POST /probe`**: probe the login chain without credentials.
- New options `helper_cmd` (default = new helper) and `helper_deadline` (240 s).
- 7 new tests for the helper logic (PKCE, authorize URL, code detection, state machine,
  token exchange against a fake IdP).

# 1.3.1 (2026-09-29)

- **Bugfix (cause of "HTTP 401" despite a correct password)**: evcc puts
  `POST /api/auth/apikey` behind `EnsureAuthHandler` - without a logged-in session
  evcc answers 401 **before** the password is checked. The add-on now logs in first
  via `POST /api/auth/login` and creates the key with the same session.
- **Bugfix**: if key creation failed, the client previously got no credentials -
  all further calls ran into 401 ("API mode not available") although the login
  worked. Now the admin password is passed on to the client and the run
  continues via the session.
- Two regression tests that reproduce exactly these two cases
  (`test_apikey_requires_session_like_evcc`, `test_session_fallback_keeps_api_mode_working`).

# 1.3.0 (2026-09-29)

- **Slug lookup**: the internal name of the evcc add-on (`evcc`, `a1b2c3d4-evcc`, ...) is
  determined via `GET /addons` itself; a wrongly set `evcc_slug` is corrected.
  The log line names all add-ons found, the diagnosis `evcc_slug_resolved`.
- **Bugfix**: an add-on state that cannot be determined (e.g. HTTP 404 due to a wrong slug)
  previously counted as "evcc is stopped" and the run continued. Now it aborts without
  touching evcc (`state()` returns None, `stop_evcc()` checks beforehand).
- **Bugfix**: in the dry run evcc is no longer stopped (previously it was stopped and
  not started again).
- **Notifications**: `homeassistant_api: true` added; on 401/403 the log
  names the cause.
- `SUPERVISOR_URL` as an environment variable (for tests).
- 11 new tests for the Supervisor integration (slug lookup, stop/start, notifications).

# 1.2.0 (2026-09-29)

- **evcc API key is created automatically**: with `evcc_admin_password` the add-on fetches the
  key itself (`POST /api/auth/apikey`), checks it and stores it under `/data/evcc_apikey`
  (mode 600). Order: option -> stored key -> create new -> session login.
- Optional `evcc_set_admin_password: true`: creates a missing evcc admin password.
- Clear error messages for 401 (wrong password) and 501 (no admin password in evcc).
- Diagnosis shows `api_key_source` and `psa_account_configured`.
- If `account_user`/`account_password` (PSA account) are missing, the add-on no longer exits,
  but waits and re-reads the options every round (changes without a restart).
- `GET /api/auth/status` as auth diagnosis; `ensure_auth()` also detects open evcc instances.
- 13 integration tests for the orchestrator (incl. key creation, reuse,
  rotation, wrong password, missing admin password).

# 1.1.0 (2026-09-29)

- **API mode added**: vehicles configured via the evcc UI/DB
  ("db:<id>") are updated via the evcc configuration API
  (`GET/PUT /api/config/devices/vehicle/<id>`), no longer only via YAML.
- Mode `auto|api|yaml` in the options; `auto` works out for itself where the vehicles live.
- Optional test run of the new configuration before saving
  (`POST /api/config/test/vehicle/merge/<id>`), can be switched off via `api_verify_with_test`.
- Vehicle lookup by title/name/ID incl. a clear error message with the list of existing vehicles.
- Restart only if evcc requests it (`GET /api/config/dirty`).
- Diagnostic run `--check` / `--dump` and HTTP endpoints `GET /status`, `GET /dump`.
- Log lines go to stderr, diagnosis JSON to stdout.
- Unknown command-line options abort with exit 2 (no accidental daemon start).

# 1.0.0 (2026-09-29)

- First package: scheduler (log observation + proactive interval), HTTP trigger for
  Home Assistant automations, headless token login, YAML mode with backup and a
  comment-preserving patcher, verification via the evcc log, notifications.
