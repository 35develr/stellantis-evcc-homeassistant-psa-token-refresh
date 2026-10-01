#!/usr/bin/env bash
# Emergency tool: remove a persisted PSA token from the evcc database.
#
# When is this needed?
#   Only if evcc keeps reporting "token expired" although fresh tokens were written and
#   evcc is older than 0.315.1. From 0.315.1 on, evcc itself drops a token the server
#   rejected (invalid_grant/invalid_token) and then uses the fresh token from the config.
#
# Run it in the "Advanced SSH & Web Terminal" add-on (protection mode OFF) on HAOS.
# CAUTION: verify the path first - the script looks for it on its own.

set -euo pipefail

EVCC_SLUG="${1:-evcc}"

find_db() {
    for base in /mnt/data/supervisor/addons/data /mnt/data/supervisor/apps/data \
                /usr/share/hassio/addons/data /addon_configs; do
        [ -d "$base" ] || continue
        for dir in "$base"/*"$EVCC_SLUG"*; do
            [ -f "$dir/evcc.db" ] && { echo "$dir/evcc.db"; return 0; }
        done
    done
    return 1
}

DB="$(find_db || true)"
if [ -z "${DB:-}" ]; then
    echo "evcc.db not found. Please locate it with 'find /mnt/data -name evcc.db'." >&2
    exit 1
fi

command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 fehlt (apk add sqlite)" >&2; exit 1; }

echo "Datenbank: $DB"
echo
echo "--- existing token keys (values truncated) ---"
sqlite3 "$DB" "select key, substr(value, 1, 60) || '...' from settings where key like 'psa.%';" || true

read -r -p "Delete these entries now? [yes/NO] " answer
if [ "${answer:-}" != "yes" ]; then
    echo "aborted - nothing changed"
    exit 0
fi

cp -a "$DB" "$DB.bak-$(date +%Y%m%d-%H%M%S)"
echo "Backup created."
sqlite3 "$DB" "delete from settings where key like 'psa.%';"
echo "Entries deleted. Restart the evcc add-on afterwards."
