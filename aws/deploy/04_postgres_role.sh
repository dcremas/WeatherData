#!/bin/zsh
# Create the least-privilege ghcnh_etl role on the EC2 warehouse and let the VPC
# reach it, mirroring how awk_etl is set up for the apple_weatherkit pipeline.
#
# Two things have to be true before the loader can connect:
#
#   1. a role exists with rights on exactly the tables it rebuilds
#   2. pg_hba.conf has a hostssl line for that role, that database and the VPC
#      CIDR - today it only has one, for awk_etl against apple_weatherkit, so
#      the loader would be rejected before it ever authenticated
#
# hostssl, not host: matching awk_etl's entry means TLS is required rather than
# merely offered, so the password never crosses the VPC in the clear. The Lambda
# sets PGSSLMODE=require to match.
#
# The password reaches the EC2 box over the SSH session's stdin, never as an
# argument - an argument would be visible in `ps` on that host for as long as
# the command ran.
#
# Idempotent. Run it before 00_bootstrap_aws.sh so the same password can be fed
# into the SSM parameter the Lambda reads.

set -euo pipefail


# Every identifier this pipeline needs lives in config.env, which is gitignored
# because this repository is public. Copy config.env.example and fill it in.
HERE="${0:A:h}"
if [[ ! -f "${HERE}/config.env" ]]; then
    echo "aws/deploy/config.env not found." >&2
    echo "  cp ${HERE}/config.env.example ${HERE}/config.env && \$EDITOR ${HERE}/config.env" >&2
    exit 1
fi
source "${HERE}/config.env"


DB_NAME="${PG_DB}"

if [[ -z "${GHCNH_PG_PASSWORD:-}" ]]; then
    echo "GHCNH_PG_PASSWORD is not set. Generate one first, for example:" >&2
    echo "  export GHCNH_PG_PASSWORD=\$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)" >&2
    exit 1
fi

echo "=== creating ${PG_ROLE} on ${DB_NAME}@${EC2_HOST} ==="

REMOTE_SCRIPT=$(cat <<REMOTE
set -euo pipefail

PG_ROLE='${PG_ROLE}'
DB_NAME='${DB_NAME}'
VPC_CIDR='${VPC_CIDR}'
PG_PASSWORD='${GHCNH_PG_PASSWORD}'

# Role. Created without a password first, so that re-running only rotates the
# password and never fails on an already-existing role.
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '\$PG_ROLE'" | grep -q 1; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE ROLE \$PG_ROLE LOGIN;"
    echo "role \$PG_ROLE created"
else
    echo "role \$PG_ROLE already exists"
fi

sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER ROLE \$PG_ROLE PASSWORD '\$PG_PASSWORD';"
echo "password set"

# Rights: the loader rewrites observations, and rebuilds obs_baro_impact and
# loc_subset. analytics_slp_decrease.sql DROPs and re-CREATEs obs_baro_impact,
# so the role needs CREATE on the schema and has to own that table - a non-owner
# cannot drop it. It gets nothing it does not use: locations and regions are
# only ever read.
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "\$DB_NAME" <<SQL
GRANT CONNECT ON DATABASE \$DB_NAME TO \$PG_ROLE;
GRANT USAGE, CREATE ON SCHEMA public TO \$PG_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE ON observations TO \$PG_ROLE;
GRANT USAGE, SELECT ON SEQUENCE observations_id_seq TO \$PG_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE ON loc_subset TO \$PG_ROLE;
GRANT SELECT ON locations TO \$PG_ROLE;
GRANT SELECT ON regions TO \$PG_ROLE;
ALTER TABLE obs_baro_impact OWNER TO \$PG_ROLE;
SQL
echo "grants applied"

# pg_hba: one line, scoped to this role and this database, from the VPC only.
HBA=\$(sudo -u postgres psql -tAc "SHOW hba_file;")
if sudo grep -qE "^hostssl[[:space:]]+\$DB_NAME[[:space:]]+\$PG_ROLE[[:space:]]" "\$HBA"; then
    echo "pg_hba already has an entry for \$PG_ROLE"
else
    echo "hostssl \$DB_NAME  \$PG_ROLE  \$VPC_CIDR  scram-sha-256" | sudo tee -a "\$HBA" >/dev/null
    sudo -u postgres psql -c "SELECT pg_reload_conf();" >/dev/null
    echo "pg_hba entry added, configuration reloaded"
fi

echo "--- hostssl entries now present ---"
sudo grep -E "^hostssl" "\$HBA" || true
REMOTE
)

printf '%s' "${REMOTE_SCRIPT}" | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -i "${SSH_KEY}" "ec2-user@${EC2_HOST}" 'bash -s'

echo
echo "=== done ==="
echo "Now store the same password for the Lambda:"
echo "  GHCNH_PG_PASSWORD='<same value>' ./deploy/00_bootstrap_aws.sh"
