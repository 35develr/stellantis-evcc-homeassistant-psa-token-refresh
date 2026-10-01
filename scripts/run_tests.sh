#!/usr/bin/env bash
# Runs all self-tests of the package (no dependencies, no network, no Docker).
set -uo pipefail

cd "$(dirname "$0")/.."

fail=0
for t in tests/test_patch_evcc_yaml.py tests/test_evcc_api.py tests/test_orchestrator_api.py \
         tests/test_supervisor_api.py tests/test_psa_browser_token.py \
         tests/test_api_mode_evcc_stop.py tests/test_backoff.py; do
    echo "=============================================================="
    echo "== $t"
    echo "=============================================================="
    python3 "$t" || fail=1
done

echo
if [ "$fail" -eq 0 ]; then
    echo "All test files passed."
else
    echo "There were failures - see above."
fi
exit "$fail"
