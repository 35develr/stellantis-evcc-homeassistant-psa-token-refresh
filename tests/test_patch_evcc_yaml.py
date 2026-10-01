#!/usr/bin/env python3
"""Self-test for patch_evcc_yaml.py - runs without dependencies, without network.

Usage: python3 tests/test_patch_evcc_yaml.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "psa-token-refresh"))

from patch_evcc_yaml import PatchError, patch_evcc_yaml  # noqa: E402

SAMPLE = """\
# evcc.yaml - example with comments that must be preserved
site:
  title: Home

vehicles:
  # first car
  - name: auto1
    type: peugeot
    user: konto@example.org
    tokens:
      access: "old-access-1"   # can be newly generated at any time
      refresh: "old-refresh-1"
    vin: VF3AAAA11111
    capacity: 50

  - name: auto2
    type: peugeot
    user: konto@example.org
    tokens: {access: "old-access-2", refresh: "old-refresh-2"}
    vin: VF3BBBB22222

loadpoints:
  - title: Garage
    charger: wallbox
"""


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_block_form_and_comments():
    out, changes, warnings = patch_evcc_yaml(
        SAMPLE,
        [{"name": "auto1", "vin": "VF3AAAA11111", "access": "new-a1", "refresh": "new-r1"}],
        rotate_user_key=False,
    )
    check('access: "new-a1"' in out, "access not replaced")
    check('refresh: "new-r1"' in out, "refresh not replaced")
    check("# evcc.yaml - example" in out, "comment at the start of the file lost")
    check("# first car" in out, "comment in the block lost")
    check("old-access-1" not in out.split("auto2")[0], "old access token still present")
    check("user: konto@example.org" in out, "user unexpectedly changed")
    check("vin: VF3AAAA11111" in out, "vin lost")
    check(not warnings, f"unexpected warnings: {warnings}")
    return changes


def test_inline_form_untouched_other_vehicle():
    out, _changes, _warnings = patch_evcc_yaml(
        SAMPLE,
        [{"name": "auto2", "vin": "VF3BBBB22222", "access": "new-a2", "refresh": "new-r2"}],
        rotate_user_key=False,
    )
    check('tokens: {access: "new-a2", refresh: "new-r2"}' in out, "inline form not replaced")
    check("old-access-1" in out, "block of auto1 was modified too")


def test_user_rotation_is_stable():
    out1, changes1, _ = patch_evcc_yaml(
        SAMPLE, [{"name": "auto1", "access": "a", "refresh": "r"}], generation=7
    )
    check('user: "konto@example.org#7"' in out1, "user suffix not set")
    out2, changes2, _ = patch_evcc_yaml(out1, [{"name": "auto1", "access": "b", "refresh": "r"}],
                                       generation=7)
    check('user: "konto@example.org#7"' in out2, "suffix was appended twice")
    check(not any("user ->" in c for c in changes2), "user changed again (not idempotent)")


def test_second_run_same_tokens_is_noop():
    out1, _c1, _w = patch_evcc_yaml(
        SAMPLE, [{"name": "auto1", "access": "same", "refresh": "same2"}], rotate_user_key=False
    )
    out2, changes2, _w = patch_evcc_yaml(
        out1, [{"name": "auto1", "access": "same", "refresh": "same2"}], rotate_user_key=False
    )
    check(out1 == out2, "second run modified the file")
    check(changes2 == [], f"second run reports changes: {changes2}")


def test_missing_vehicle_raises():
    try:
        patch_evcc_yaml(SAMPLE, [{"name": "nonexistent", "access": "a", "refresh": "r"}])
    except PatchError as exc:
        check("nonexistent" in str(exc), "error message does not name the vehicle")
        return
    raise AssertionError("PatchError was not raised")


def test_vin_mismatch_warns():
    _out, _c, warnings = patch_evcc_yaml(
        SAMPLE,
        [{"name": "auto2", "vin": "VF3FALSCH", "access": "a", "refresh": "r"}],
        rotate_user_key=False,
    )
    check(any("VF3FALSCH" in w for w in warnings), "VIN conflict not reported")


def test_all_vehicles_at_once():
    out, changes, _w = patch_evcc_yaml(
        SAMPLE,
        [
            {"name": "auto1", "vin": "VF3AAAA11111", "access": "x1", "refresh": "y1"},
            {"name": "auto2", "vin": "VF3BBBB22222", "access": "x1", "refresh": "y1"},
        ],
        generation=42,
    )
    check(out.count('user: "konto@example.org#42"') == 2, "not both blocks converted")
    check('access: "x1"' in out, "access missing")
    check(len(changes) >= 4, f"too few changes reported: {changes}")
    # YAML must still be parseable
    try:
        import yaml  # optional, possibly not installed locally
    except ImportError:
        return
    doc = yaml.safe_load(out)
    names = [v["name"] for v in doc["vehicles"]]
    check(names == ["auto1", "auto2"], f"vehicle list broken: {names}")
    check(doc["vehicles"][1]["tokens"]["access"] == "x1", "tokens not set")


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
    raise SystemExit(main())
