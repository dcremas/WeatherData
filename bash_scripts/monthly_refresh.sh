#!/bin/zsh
# Monthly GHCNh refresh: load the month that just ended, then rebuild analytics.
#
# MANUAL FALLBACK ONLY - nothing schedules this any more.
#
# The nightly AWS pipeline supersedes it: EventBridge Scheduler runs
# ghcnhDownloadS3 at 07:30 and ghcnhPostgresqlUpdate at 07:45 America/Chicago,
# which rebuild the current and previous months in the EC2 warehouse and refresh
# obs_baro_impact and loc_subset. LaunchAgent com.dustincremascoli.weatherdata-monthly
# was unloaded and its plist deleted on 2026-08-19.
#
# Keep this for the cases AWS cannot cover:
#   - the Lambdas are broken or the account is unreachable, and the warehouse
#     needs a load from a laptop
#   - a month has to be reloaded from a locally built parquet
#   - refreshing the LOCAL warehouse, which the AWS pipeline deliberately does
#     not touch - it has no route back to this machine
#
# It still loads BOTH warehouses, so running it by hand also brings the EC2 copy
# in line. That is safe: it rewrites the same month slice the nightly job does.
#
# GHCNh rewrites the whole in-progress year in place rather than publishing
# deltas, so the download step fetches the entire year of the month being loaded.
# In January that is the previous calendar year, which `date -v-1m` handles.

set -euo pipefail

PROJECT="/Users/dustincremascoli/projects/WeatherData"
PY="${PROJECT}/env/bin/python"
PSQL="/usr/local/opt/postgresql@18/bin/psql"
LOG="${PROJECT}/logs/monthly_refresh.log"

mkdir -p "${PROJECT}/logs"
cd "${PROJECT}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG}"; }
fail() { log "FAILED: $*"; exit 1; }

# The month that just ended. `-v1d` first pins the day to the 1st, so the
# subtraction cannot land on a short month: from the 31st, `date -v-1m` alone is
# ambiguous across platforms.
YEAR="$(date -v1d -v-1m +%Y)"
MONTH="$(date -v1d -v-1m +%m)"

log "=== monthly refresh starting for ${YEAR}-${MONTH} ==="

# The warehouse is only reachable through the persistent SSH tunnel from
# LaunchAgent com.dustincremascoli.pgtunnel. Check it before doing any work, so a
# tunnel that is down produces one clear line instead of a Python traceback.
nc -z -G 5 127.0.0.1 15432 >/dev/null 2>&1 \
    || fail "tunnel on 127.0.0.1:15432 is down - is com.dustincremascoli.pgtunnel loaded?"

log "downloading GHCNh year ${YEAR}"
"${PY}" ghcnh_generate.py "${YEAR}" >> "${LOG}" 2>&1 \
    || fail "ghcnh_generate.py ${YEAR}"

# ghcnh_process refuses an empty or implausibly short batch before deleting
# anything, and verify_month_coverage raises if the loaded month stops short of
# the month end - which is what a stalled upstream source looks like.
log "loading ${YEAR}-${MONTH} into the remote warehouse"
"${PY}" ghcnh_process.py "${YEAR}" "${MONTH}" remote >> "${LOG}" 2>&1 \
    || fail "ghcnh_process.py ${YEAR} ${MONTH} remote"

log "loading ${YEAR}-${MONTH} into the local warehouse"
"${PY}" ghcnh_process.py "${YEAR}" "${MONTH}" local >> "${LOG}" 2>&1 \
    || fail "ghcnh_process.py ${YEAR} ${MONTH} local"

# Rebuild the analytics table the scatter_baro_impact app reads. The script is
# wrapped in a transaction, so readers keep the previous table until it commits.
# Credentials come from ~/.pgpass, which already has entries for both
# 127.0.0.1:15432 and localhost:5432. This is how run_psql.sh authenticates, so
# the script holds no password of its own.
log "rebuilding obs_baro_impact on remote"
"${PSQL}" --host=127.0.0.1 --port=15432 --username=dustincremascoli \
    --dbname=weatherdata -v ON_ERROR_STOP=1 -f sql/analytics_slp_decrease.sql \
    >> "${LOG}" 2>&1 \
    || fail "obs_baro_impact rebuild"

REMOTE_MAX="$("${PSQL}" --host=127.0.0.1 --port=15432 \
    --username=dustincremascoli --dbname=weatherdata \
    -tAc 'SELECT max(date) FROM observations')"

log "=== monthly refresh complete - warehouse now current to ${REMOTE_MAX} ==="
