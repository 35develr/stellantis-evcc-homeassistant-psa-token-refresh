# Releasing

This repository is the **workbench**. What users add in Home Assistant is the published
repository:

<https://github.com/35develr/stellantis-evcc-homeassistant-psa-token-refresh>

Home Assistant always uses the state of that repository's **default branch**. The Supervisor
clones the default branch only - there is no way to select a branch or a tag for an add-on
repository - so "released" simply means "on `main` there".

## Remotes

| remote | repository | role |
| ------ | ---------- | ---- |
| `origin` or `workbench` | the Forgejo repository this workbench lives in | development, test channel |
| `github` | `35develr/stellantis-evcc-homeassistant-psa-token-refresh` | release |

The workbench branch is kept a direct descendant of `github/main`, so a release is a
fast-forward. Never merge `main` into the workbench branch in the other direction.

## Release

1. Develop and test on the workbench. A second instance can run in parallel to the release
   installation: the Supervisor keys apps as `<repository-hash>_psa_token_refresh`, so two
   repositories with the same `slug` result in two independent apps (own container, own
   data directory, own options). For a parallel test set `trigger_port` to a free port
   (8909 is taken by the release instance) and `evcc_control: none` in the test instance,
   and remember that two instances refreshing the same PSA account invalidate each other's
   refresh token - only one of them should be active.
2. Raise `version:` in `psa-token-refresh/config.yaml` **and** put the same version at the
   top of `psa-token-refresh/CHANGELOG.md`. Home Assistant only updates an installed add-on
   when the version is higher, and `tests/test_config_yaml.py` fails when the two disagree.
   The version inside the built image is taken from the Supervisor's build argument, so
   nothing else has to be bumped (a hardcoded version in the code did exactly this damage
   in 1.6.0).
3. Run `bash scripts/run_tests.sh` (offline, no dependencies, no Docker).
4. Release:
   ```
   git push github HEAD:main
   ```
5. Users receive it via Settings → Apps (update), automatically when add-on auto-update is
   enabled. Nothing else needs publishing: the add-on ships no prebuilt image, the
   Supervisor builds it locally from `psa-token-refresh/Dockerfile`.

## CI

`.github/workflows/tests.yml` runs the self-tests (and a syntax check) on every push. It is
prepared on the branch `ci-workflow` instead of `main`, because GitHub refuses a push that
creates or updates a workflow file unless the pushing token carries the `workflow` scope
(`refusing to allow a Personal Access Token to create or update workflow ... without
'workflow' scope`). Either extend the token scope and merge that branch, or run step 3 by
hand before releasing.

## Compatibility rules (an update has to work for existing installations)

- **`slug` never changes.** A new slug is a new app: the old installation stays, and every
  setting has to be entered again.
- **New options need a non-empty default** in `options:`. An empty value for a required
  option blocks saving the configuration ("Missing required option" - the 1.4.12 bug).
- **Keep `arch` listing every architecture users can have** (amd64, aarch64). Dropping one
  turns the update into "not available" for those users. armv7/i386 never worked (there is
  no Chromium build for them) and must not be added.
- **`options:` and `schema:` stay in sync** - a value without a schema entry (and the other
  way round) is a hard error; `tests/test_config_yaml.py` checks both directions.
- **The version inside the image must match `config.yaml`.** The start-up self-diagnosis
  compares the two and warns otherwise ("the running image does not match the state of the
  installation"), which reads like a broken update even when the build is fine.
- **Dockerfile arguments:** the Supervisor passes `BUILD_VERSION` and `BUILD_ARCH` for every
  architecture it builds. `BUILD_ARCH` is what keeps Qt out of the aarch64 image; the
  amd64 image currently carries both browser engines (Playwright is the default, Qt the
  fallback), which costs roughly 250-300 MB of image size.
