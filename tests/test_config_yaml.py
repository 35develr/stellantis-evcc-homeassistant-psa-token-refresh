#!/usr/bin/env python3
"""Self-test for the add-on definition (config.yaml).

Guards the trap that made the configuration form impossible to save: the Supervisor
turns every option with an *empty* value (`null`) in `options` into a MANDATORY option,
even when the schema marks it as optional with `?`. That is how
`Missing required option 'evcc_slug' in Stellantis/PSA Token Refresh for evcc
(<hash>_psa_token_refresh)` happened on the very first save (report of 2026-10-13).

Also: every option needs a schema entry, otherwise the Supervisor refuses to save
("unknown option"), and the add-on version must match the CHANGELOG, because the
Supervisor only updates when the version was raised.

No PyYAML in the image - the parser below only reads the blocks it needs.

Usage: python3 tests/test_config_yaml.py
"""

import os
import re
import sys

ADDON_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "psa-token-refresh")
)
CONFIG = os.path.join(ADDON_DIR, "config.yaml")
CHANGELOG = os.path.join(ADDON_DIR, "CHANGELOG.md")

TOP_RE = re.compile(r"^([A-Za-z_][\w-]*):(?:\s*(.*))?$")
SUB_RE = re.compile(r"^\s{2}([A-Za-z_][\w-]*):(\s*(.*?))\s*$")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def parse_config(path=CONFIG):
    """Reads top-level keys and their direct children.

    Returns (top, blocks): top = {key: value} for single-line keys, blocks =
    {key: {child: value|None}} for block keys (e.g. options). None means: empty value
    (null). An empty value with children (indent > 2, e.g. `vehicles:`) is nested, not null.
    """
    lines = _read(path)
    top, blocks = {}, {}
    current = None
    for idx, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            m = TOP_RE.match(line)
            if not m:
                current = None
                continue
            key, value = m.group(1), (m.group(2) or "").strip()
            if value:
                top[key] = value
                current = None
            else:
                current = key
                blocks[key] = {}
            continue
        if current is None:
            continue
        m = SUB_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(3)
        if raw:
            blocks[current][key] = raw
            continue
        child = False
        for nxt in lines[idx + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            child = (len(nxt) - len(nxt.lstrip(" "))) > 2
            break
        blocks[current][key] = "<nested>" if child else None
    return top, blocks


def get_config():
    top, blocks = parse_config()
    for need in ("version", "slug"):
        check(need in top, f"config.yaml: key '{need}' is missing")
    for need in ("options", "schema"):
        check(need in blocks and blocks[need], f"config.yaml: section '{need}' missing/empty")
    return top, blocks


def test_no_option_is_empty():
    """An empty value in `options` -> the Supervisor makes it a mandatory option."""
    _, blocks = get_config()
    empty = sorted(k for k, v in blocks["options"].items() if v is None)
    check(not empty,
          "options without a value (the Supervisor treats them as mandatory and refuses "
          f"to save): {empty}")


def test_evcc_slug_has_default_value():
    """Regression for the bug report: evcc_slug must ship pre-filled."""
    _, blocks = get_config()
    value = blocks["options"].get("evcc_slug")
    check(value is not None,
          "evcc_slug has no value - that is exactly what made the form unsaveable")
    check(value.strip() != "", "evcc_slug is empty - the form cannot be saved like this")


def test_every_option_has_a_schema_entry():
    _, blocks = get_config()
    missing = sorted(k for k in blocks["options"] if k not in blocks["schema"])
    check(not missing, f"options without a schema entry (Supervisor: unknown option): {missing}")


def test_every_schema_key_is_an_option():
    _, blocks = get_config()
    extra = sorted(k for k in blocks["schema"] if k not in blocks["options"])
    check(not extra, f"schema entries without a default in options: {extra}")


def test_optional_schema_keys_are_marked():
    """A mandatory field without '?' would block the first save."""
    _, blocks = get_config()
    schema = blocks["schema"]
    for key in ("evcc_slug", "evcc_url", "brand", "account_user", "mode"):
        check(key in schema, f"{key} missing from the schema")
    check(schema["evcc_slug"].endswith("?"),
          "evcc_slug is declared mandatory in the schema - it should stay optional")
    check(schema["evcc_apikey"].endswith("?"),
          "evcc_apikey should be optional (password?)")


def test_version_matches_changelog():
    """Without a raised version there is no update in the app store."""
    top, _ = get_config()
    version = top["version"].strip().strip('"')
    match = re.search(r"^#\s*(\d+\.\d+\.\d+)", "\n".join(_read(CHANGELOG)), re.MULTILINE)
    check(match is not None, "CHANGELOG has no heading of the form '# x.y.z'")
    check(match.group(1) == version,
          f"config.yaml version={version}, CHANGELOG starts with {match.group(1)}")


def test_running_image_reports_the_config_version():
    """The version inside the image decides the self-diagnosis, so it must not drift.

    A stale constant in orchestrator.py once made the freshly built 1.6.0 image announce
    itself as 1.5.0 - the log then claims "the running image does not match the state of
    the installation", which looks like a broken update.
    """
    top, _ = get_config()
    version = top["version"].strip().strip('"')
    source = "\n".join(_read(os.path.join(ADDON_DIR, "orchestrator.py")))
    check(re.search(r'^VERSION = "\d+\.\d+\.\d+"', source, re.MULTILINE) is None,
          "orchestrator.py must not hardcode VERSION - the image provides /app/VERSION")
    fallback = re.search(r'^VERSION_FALLBACK = "([\d.]+)"', source, re.MULTILINE)
    check(fallback is not None, "orchestrator.py has no VERSION_FALLBACK constant")
    check(fallback.group(1) == version,
          f"VERSION_FALLBACK={fallback.group(1)} but config.yaml says {version}")
    check("VERSION = _read_version()" in source,
          "orchestrator.py must read the version from /app/VERSION")
    check('with_name' not in source, "keep using os.path - the source imports no pathlib")
    dockerfile = "\n".join(_read(os.path.join(ADDON_DIR, "Dockerfile")))
    check(re.search(r'> /app/VERSION', dockerfile),
          "the Dockerfile must write BUILD_VERSION into /app/VERSION")
    arg = re.search(r'^ARG BUILD_VERSION="([\d.]+)"$', dockerfile, re.MULTILINE)
    check(arg is not None and arg.group(1) == version,
          f"Dockerfile ARG BUILD_VERSION={arg and arg.group(1)} vs config.yaml {version}")


def test_slug_and_arch():
    top, _ = get_config()
    check(top["slug"].strip().strip('"') == "psa_token_refresh",
          "slug changed - the Supervisor would then find the add-on under another name")
    text = "\n".join(_read(CONFIG))
    check(re.search(r"^arch:\n(?:  - \S+\n)*  - amd64\n", text, re.MULTILINE),
          "arch must contain amd64")
    # aarch64 has been buildable since the Playwright backend exists; without the entry
    # the Supervisor reports the app as 'not available' on a Raspberry Pi (issue #3)
    check(re.search(r"^  - aarch64$", text, re.MULTILINE),
          "arch must contain aarch64 (Playwright backend), otherwise ARM cannot install it")


def test_browser_backend_option():
    """The new engine switch: it has to ship with a value and a schema entry."""
    _, blocks = get_config()
    value = blocks["options"].get("browser_backend")
    check(value is not None and value.strip() != "",
          "browser_backend has no value - the Supervisor would make it mandatory")
    check(value.strip() == "auto",
          f"browser_backend should default to auto (Playwright first), is {value!r}")
    schema = blocks["schema"].get("browser_backend", "")
    check(schema.startswith("list("), f"browser_backend needs a choice list, is {schema!r}")
    for choice in ("auto", "playwright", "qt"):
        check(choice in schema, f"{choice} is missing from the browser_backend schema")
    text = "\n".join(_read(ADDON_DIR + os.sep + "Dockerfile"))
    check("BUILD_ARCH" in text and "python:3.12-bookworm" in text,
          "the Dockerfile must build per architecture (BUILD_ARCH) on a Debian base")


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
    sys.exit(main())
