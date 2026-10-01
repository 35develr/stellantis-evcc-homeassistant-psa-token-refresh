#!/usr/bin/env bash
# Add-on entry point: starts Xvfb (virtual display for QtWebEngine) and then the
# orchestrator. Arguments are passed through, e.g. --dump / --check.
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"

if ! pgrep -x Xvfb >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
    sleep 2
fi

echo "[psa-token-refresh] Start (DISPLAY=$DISPLAY, Version $(python3 /app/orchestrator.py --version 2>/dev/null || echo '?'))"
exec python3 /app/orchestrator.py "$@"
