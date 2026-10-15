#!/usr/bin/env bash
# Add-on entry point: provide the virtual display when the legacy Qt engine is used
# (the default Playwright engine runs headless), then start the orchestrator.
# Arguments are passed through, e.g. --dump / --check.
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"

# Xvfb is only shipped in the amd64 image (qt backend). Without it the helper must not
# try to open a display: the Playwright backend does not need one.
if command -v Xvfb >/dev/null 2>&1 && ! pgrep -x Xvfb >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
    sleep 2
fi

echo "[psa-token-refresh] Start (DISPLAY=$DISPLAY, Version $(python3 /app/orchestrator.py --version 2>/dev/null || echo '?'), Backend ${PSA_BROWSER:-auto})"
exec python3 /app/orchestrator.py "$@"
