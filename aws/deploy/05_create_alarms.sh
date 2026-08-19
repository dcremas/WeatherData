#!/bin/zsh
# CloudWatch alarms for the nightly pipeline, delivered by email through SNS.
#
# Without these the pipeline is automated but not observed, and its failure modes
# are quiet by design: every guard refuses to write rather than writing something
# wrong, so a broken run leaves the warehouse serving perfectly good stale data.
# Nothing downstream looks different. The bokeh apps keep plotting. The only
# symptom is that `max(date)` stops advancing, which nobody watches.
#
# Three alarms, covering the two ways this goes wrong:
#
#   ghcnh-ghcnhDownloadS3-errors
#   ghcnh-ghcnhPostgresqlUpdate-errors
#                 the function ran and raised - a guard refused, NOAA was
#                 unreachable, the warehouse rejected the connection
#
#   ghcnh-pipeline-idle
#                 the loader did not run at all: schedule disabled or deleted,
#                 or EventBridge could not assume its role. An errors alarm
#                 cannot see this, because nothing ran to fail.
#
# Idempotent: put-metric-alarm and create-topic both replace rather than
# duplicate.

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


TOPIC_NAME="ghcnh-pipeline-alerts"
# Set to "none" to create the topic and alarms without subscribing anyone, which
# is the right call if you do not yet know the address you want alerts at - the
# alarms are created either way and a subscriber can be added at any time.
# config.env supplies this; GHCNH_ALERT_EMAIL overrides it for a one-off run.
# "none" creates the topic and alarms without subscribing anyone, which is right
# when you do not yet know the address you want alerts at.
ALERT_EMAIL="${GHCNH_ALERT_EMAIL:-${ALERT_EMAIL:-none}}"

say() { print -r -- "=== $* ==="; }

say "SNS topic ${TOPIC_NAME}"
TOPIC_ARN=$(aws sns create-topic --name "${TOPIC_NAME}" --region "${AWS_REGION}" \
    --query 'TopicArn' --output text)
echo "${TOPIC_ARN}"

# Subscribing sends a confirmation mail that has to be clicked before anything is
# delivered; until then the subscription sits as PendingConfirmation and the
# alarms fire into nothing. Re-subscribing an already-confirmed address is a
# no-op, so this is safe to re-run.
if [[ "${ALERT_EMAIL}" == "none" ]]; then
    echo "no subscriber requested - alarms will fire but deliver nowhere."
    echo "add one with:"
    echo "  aws sns subscribe --topic-arn ${TOPIC_ARN} \\"
    echo "      --protocol email --notification-endpoint you@example.com --region ${AWS_REGION}"
elif aws sns list-subscriptions-by-topic --topic-arn "${TOPIC_ARN}" --region "${AWS_REGION}" \
    --query "Subscriptions[?Endpoint=='${ALERT_EMAIL}'].SubscriptionArn" --output text \
    | grep -qv "^$"; then
    echo "already subscribed: ${ALERT_EMAIL}"
else
    aws sns subscribe --topic-arn "${TOPIC_ARN}" --protocol email \
        --notification-endpoint "${ALERT_EMAIL}" --region "${AWS_REGION}" >/dev/null
    echo "subscribed ${ALERT_EMAIL} - CONFIRM THE EMAIL or nothing is delivered"
fi

error_alarm() {
    local function_name="$1" description="$2"
    aws cloudwatch put-metric-alarm \
        --alarm-name "ghcnh-${function_name}-errors" \
        --alarm-description "${description}" \
        --namespace AWS/Lambda --metric-name Errors \
        --dimensions "Name=FunctionName,Value=${function_name}" \
        --statistic Sum --period 86400 --evaluation-periods 1 \
        --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
        --treat-missing-data notBreaching \
        --alarm-actions "${TOPIC_ARN}" --ok-actions "${TOPIC_ARN}" \
        --region "${AWS_REGION}"
    echo "ghcnh-${function_name}-errors"
}

say "error alarms"
error_alarm "ghcnhDownloadS3" \
    "GHCNh extract failed. Most likely NOAA has not republished, or coverage regressed upstream and the promote guard refused. The previously promoted parquet is still in place."
error_alarm "ghcnhPostgresqlUpdate" \
    "GHCNh load failed. The warehouse still holds its previous good data - every guard refuses before the DELETE. Check whether the source is short, stale, or the warehouse unreachable."

# The loader is the one that must run: if the download fails, the loader still
# reloads the last good parquet and the warehouse stays consistent. If the LOADER
# stops running, nothing advances and nothing complains.
say "liveness alarm"
aws cloudwatch put-metric-alarm \
    --alarm-name "ghcnh-pipeline-idle" \
    --alarm-description "The GHCNh loader has not run in 24 hours. The schedule is probably disabled or its IAM role broken - an errors alarm cannot catch this, because nothing ran to fail." \
    --namespace AWS/Lambda --metric-name Invocations \
    --dimensions "Name=FunctionName,Value=ghcnhPostgresqlUpdate" \
    --statistic Sum --period 86400 --evaluation-periods 1 \
    --threshold 1 --comparison-operator LessThanThreshold \
    --treat-missing-data breaching \
    --alarm-actions "${TOPIC_ARN}" \
    --region "${AWS_REGION}"
echo "ghcnh-pipeline-idle"

echo
aws cloudwatch describe-alarms --alarm-name-prefix "ghcnh" --region "${AWS_REGION}" \
    --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Metric:MetricName}' \
    --output table
