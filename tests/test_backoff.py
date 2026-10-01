"""Tests for the wait (backoff) after a failure and its resolution
by an update.

Background: after a failed run the add-on waits
`retry_backoff_minutes` (default 30) until the next attempt. The log said only
"status ok - no update needed", which looked like a healthy system -
although in truth a wait was running (log of 30.09.2026, 18:41).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="backoff-test-")
os.environ["DATA_DIR"] = TMP
sys.path.insert(0, os.path.join(ROOT, "psa-token-refresh"))

import orchestrator as o  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def write_state(**kw):
    with open(os.path.join(TMP, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(kw, fh)


def read_state():
    with open(os.path.join(TMP, "state.json"), encoding="utf-8") as fh:
        return json.load(fh)


class Sup:
    """Supervisor substitute: not available, so that no network access occurs."""

    available = False

    def evcc_log(self):
        return ""


def refresher():
    """Refresher without __init__ (no real options/network access needed)."""
    r = o.Refresher.__new__(o.Refresher)
    r.opts = {"proactive_refresh_days": 30, "retry_backoff_minutes": 30}
    r.sup = Sup()
    r.data = TMP

    def boom():
        raise o.EvccApiError("no evcc in the test")

    r.api_client = boom
    return r


def capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn()
    return result, buf.getvalue()


def test_waiting_is_reported_instead_of_status_ok():
    write_state(last_version=o.VERSION, next_attempt_ts=time.time() + 600,
                last_error="token helper failed (exit 1)")
    reason, out = capture(refresher().detect_reason)
    check(reason is None, "nothing may be started during the wait")
    check("after the last failure" in out, f"wait not reported: {out[:120]}")
    check("last error: token helper failed" in out, "error cause missing in the message")


def test_backoff_remaining_reports_seconds_and_error():
    write_state(last_version=o.VERSION, next_attempt_ts=time.time() + 300,
                last_error="HTTP 401")
    rest, err = refresher().backoff_remaining()
    check(0 < rest <= 300, f"remaining time wrong: {rest}")
    check(err == "HTTP 401", f"last error wrong: {err}")


def test_expired_backoff_does_not_block():
    write_state(last_version=o.VERSION, next_attempt_ts=time.time() - 5)
    rest, _ = refresher().backoff_remaining()
    check(rest == 0.0, f"expired wait must not block: {rest}")


def test_new_version_clears_the_backoff():
    write_state(last_version="1.0.0", next_attempt_ts=time.time() + 900)
    reason, out = capture(refresher().detect_reason)
    check("clears the wait after the last failure" in out,
          f"clear not reported: {out[:160]}")
    check(read_state().get("next_attempt_ts") == 0, "wait was not reset")
    check(read_state().get("last_version") == o.VERSION, "version not recorded in the state")
    check(reason and "proactive" in reason, f"after the clear it must check, got: {reason}")


def test_same_version_keeps_the_backoff():
    write_state(last_version=o.VERSION, next_attempt_ts=time.time() + 900)
    reason, _ = capture(refresher().detect_reason)
    check(reason is None, "the same version must not clear the wait")
    check(read_state()["next_attempt_ts"] > time.time(), "wait was wrongly deleted")


if __name__ == "__main__":
    test_waiting_is_reported_instead_of_status_ok()
    test_backoff_remaining_reports_seconds_and_error()
    test_expired_backoff_does_not_block()
    test_new_version_clears_the_backoff()
    test_same_version_keeps_the_backoff()
    print()
    if FAILS:
        print(f"{len(FAILS)} test(s) failed")
        sys.exit(1)
    print("All backoff tests passed.")
