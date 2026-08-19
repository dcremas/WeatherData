#!/bin/zsh
# EventBridge Scheduler entries that make the pipeline run itself, once a day.
#
# Cadence: GHCNh republishes the whole in-progress year daily, with roughly a
# two-day lag, so a daily cycle is exactly the rate at which new data actually
# appears - anything faster re-reads the same file.
#
# 07:30 and 07:45 America/Chicago. The offset is what makes the two functions a
# pipeline without any orchestration between them: the download promotes a fresh
# combined parquet, and fifteen minutes later the loader reads whatever is under
# ghcnh_parquet/. A download that overruns or fails leaves the PREVIOUS
# combined parquet in place, so the loader reloads yesterday's data over the same
# window rather than writing anything wrong - and the freshness check then fails
# loudly, because the newest observation will have gone stale.
#
# That is the same 15-minute staging-then-load offset as awkApiCallS3 (cron(30
# */4 ...)) and awkPostgresqlUpdate (cron(45 */4 ...)), and 07:30 is the hour the
# retired monthly LaunchAgent used.
#
# Idempotent: updates the schedules if they already exist.

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


TIMEZONE="America/Chicago"
ROLE_NAME="ghcnh-scheduler-invoke-role"
ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"

# The scheduler needs its own role to invoke Lambda on your behalf. The
# apple_weatherkit schedules use two console-generated ones; this pipeline gets a
# single explicit role covering both of its functions.
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
    aws iam create-role --role-name "${ROLE_NAME}" \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{
            "Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},
            "Action":"sts:AssumeRole"}]}' \
        --description "EventBridge Scheduler -> GHCNh ETL Lambdas" >/dev/null
    echo "created ${ROLE_NAME}"
    sleep 10   # IAM role propagation, or the first put-schedule 400s
fi

aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name "invoke-ghcnh" \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{
        \"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":[
        \"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:ghcnhDownloadS3\",
        \"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:ghcnhPostgresqlUpdate\"]}]}"

schedule() {
    local name="$1" expression="$2" function_name="$3" description="$4"
    local target="{\"Arn\":\"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${function_name}\",
        \"RoleArn\":\"${ROLE_ARN}\",
        \"RetryPolicy\":{\"MaximumRetryAttempts\":3,\"MaximumEventAgeInSeconds\":3600}}"

    local verb="create-schedule"
    if aws scheduler get-schedule --name "${name}" --region "${AWS_REGION}" >/dev/null 2>&1; then
        verb="update-schedule"
    fi

    # A flexible window lets AWS spread invocations; 5 minutes is ample here and
    # keeps the 15-minute gap between the two stages intact. The awk schedules
    # use 15 minutes, which would let the two stages overlap at a daily cadence.
    aws scheduler "${verb}" --name "${name}" \
        --schedule-expression "${expression}" \
        --schedule-expression-timezone "${TIMEZONE}" \
        --flexible-time-window '{"Mode":"FLEXIBLE","MaximumWindowInMinutes":5}' \
        --target "${target}" \
        --description "${description}" \
        --state ENABLED --region "${AWS_REGION}" >/dev/null
    echo "${verb%%-*}d ${name}: ${expression} ${TIMEZONE} -> ${function_name}"
}

# MaximumRetryAttempts is 3, not the 185 the awk schedules carry. 185 retries
# over 24 hours made sense for a four-hourly job against a flaky third-party API;
# here a failure means NOAA has not published or the warehouse is unreachable,
# and retrying all day only delays the CloudWatch alarm.
schedule "ghcnhDownloadS3" "cron(30 7 * * ? *)" "ghcnhDownloadS3" \
    "Daily GHCNh extract: 112 station parquets -> combined parquet in S3"

schedule "ghcnhPostgresqlUpdate" "cron(45 7 * * ? *)" "ghcnhPostgresqlUpdate" \
    "Daily GHCNh load: transform S3 parquet -> observations, rebuild analytics"

echo
aws scheduler list-schedules --region "${AWS_REGION}" \
    --query "Schedules[?starts_with(Name,'ghcnh')].{Name:Name,State:State}" \
    --output table
