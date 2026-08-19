#!/bin/zsh
# Package and deploy both GHCNh Lambdas. Creates them on first run, updates code
# and configuration on every run after that.
#
# The split between the two is not stylistic, it is forced by the network:
#
#   ghcnhDownloadS3        NO VPC.  Needs the public internet to reach
#                          noaa-ghcnh-pds, which lives in us-east-1. A Lambda
#                          attached to this VPC has neither a NAT gateway nor a
#                          public address, and the S3 gateway endpoint only
#                          covers us-east-2 - so in-VPC it could not download
#                          anything at all.
#
#   ghcnhPostgresqlUpdate  IN VPC.  Needs ${PG_HOST}, the private address of
#                          the Postgres on lambda-playground-1, and reaches its
#                          own us-east-2 bucket over the gateway endpoint.
#
# That is the same shape as awkApiCallS3 / awkPostgresqlUpdate.

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


RUNTIME="python3.12"
BUCKET="${GHCNH_BUCKET}"
HERE="${0:A:h}"
ROOT="${HERE:h}"

LAYER_ARN=$(cat "${ROOT}/layer/.layer-arn")
echo "using layer ${LAYER_ARN}"

LAMBDA_SG=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=ghcnh-lambda-sg" \
    --query 'SecurityGroups[0].GroupId' --output text)



# Copy the warehouse credential from its canonical home in SSM into the
# pipeline's own bucket, which is the only place the loader can read it from.
#
# Reading SSM from inside the function was the first design, and this VPC cannot
# support it: there is no NAT gateway, and the sole endpoint is the S3 *gateway*
# endpoint, so ssm.us-east-2.amazonaws.com is simply unreachable - the loader
# hung for 97 seconds and timed out. An *interface* endpoint would fix that at
# roughly $21/month across these three subnets, to serve one 32-byte string once
# a day.
#
# A Lambda environment variable is the other obvious option and is what
# awkPostgresqlUpdate does. It is free and it works, but the value is then
# readable by anyone who can call GetFunctionConfiguration and it shows in the
# console's environment pane. The bucket costs nothing extra, keeps the value
# out of the function's configuration, and narrows read access to the loader's
# role alone - the bucket already has default encryption and blocks public
# access.
#
# SSM stays the single source of truth: rotate there, re-run this script.
echo "syncing the warehouse credential to s3://${BUCKET}/${PG_SECRET_KEY}"
aws ssm get-parameter --name "${PG_PASSWORD_PARAM}" --with-decryption \
        --region "${AWS_REGION}" --query 'Parameter.Value' --output text \
    | tr -d '\n' \
    | aws s3 cp - "s3://${BUCKET}/${PG_SECRET_KEY}" \
        --region "${AWS_REGION}" --sse AES256 --quiet
echo "synced"

package() {
    local source_dir="$1" zip_path="$2"
    rm -rf "${ROOT}/build" && mkdir -p "${ROOT}/build"
    cp "${source_dir}/lambda_function.py" "${ROOT}/build/"
    # The loader runs the repo's own analytics SQL rather than a copy pasted
    # into Python, so obs_baro_impact is rebuilt by the same script
    # bash_scripts/run_psql.sh runs by hand.
    if [[ "${source_dir}" == *postgresql_update ]]; then
        mkdir -p "${ROOT}/build/sql"
        cp "${REPO}/sql/analytics_slp_decrease.sql" "${ROOT}/build/sql/"
        cp "${REPO}/sql/extend_loc_subset.sql" "${ROOT}/build/sql/"
    fi
    rm -f "${zip_path}"
    (cd "${ROOT}/build" && zip -qr "${zip_path}" .)
    rm -rf "${ROOT}/build"
}

deploy() {
    local name="$1" source_dir="$2" role_name="$3" memory="$4" timeout="$5"
    local env_vars="$6" vpc_config="$7"
    local zip_path="/tmp/${name}.zip"
    local role_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${role_name}"

    package "${source_dir}" "${zip_path}"
    print -r -- "=== ${name} ($(du -h "${zip_path}" | cut -f1)) ==="

    if aws lambda get-function --function-name "${name}" --region "${AWS_REGION}" >/dev/null 2>&1; then
        aws lambda update-function-code --function-name "${name}" \
            --zip-file "fileb://${zip_path}" --region "${AWS_REGION}" >/dev/null
        aws lambda wait function-updated --function-name "${name}" --region "${AWS_REGION}"

        # shellcheck disable=SC2086
        aws lambda update-function-configuration --function-name "${name}" \
            --runtime "${RUNTIME}" --handler lambda_function.lambda_handler \
            --role "${role_arn}" --memory-size "${memory}" --timeout "${timeout}" \
            --layers "${LAYER_ARN}" --environment "${env_vars}" ${=vpc_config} \
            --region "${AWS_REGION}" >/dev/null
        echo "updated"
    else
        # shellcheck disable=SC2086
        aws lambda create-function --function-name "${name}" \
            --runtime "${RUNTIME}" --handler lambda_function.lambda_handler \
            --role "${role_arn}" --memory-size "${memory}" --timeout "${timeout}" \
            --layers "${LAYER_ARN}" --environment "${env_vars}" ${=vpc_config} \
            --architectures x86_64 --zip-file "fileb://${zip_path}" \
            --region "${AWS_REGION}" >/dev/null
        echo "created"
    fi
    aws lambda wait function-updated --function-name "${name}" --region "${AWS_REGION}"
    rm -f "${zip_path}"
}

# 2048 MB is about CPU, not headroom: Lambda scales vCPU with memory, and the
# work here is 112 downloads plus a parquet rewrite. 600s matches awkApiCallS3;
# a normal run finishes in well under a minute.
deploy "ghcnhDownloadS3" "${ROOT}/ghcnh_download" "ghcnhDownloadS3-role" \
    2048 600 \
    "Variables={GHCNH_BUCKET=${BUCKET},GHCNH_DOWNLOAD_WORKERS=16}" \
    ""

# The loader streams the combined parquet one row group at a time, so it needs
# far less than the 4.4 GB the old materialise-everything transform peaked at.
# PGSSLMODE is read by libpq itself and must be set here rather than in the URL,
# because pg_hba matches this role with hostssl and rejects a plaintext attempt.
deploy "ghcnhPostgresqlUpdate" "${ROOT}/ghcnh_postgresql_update" \
    "ghcnhPostgresqlUpdate-role" 2048 900 \
    "Variables={GHCNH_BUCKET=${BUCKET},PG_GHCNH_DIALECT=postgresql,PG_GHCNH_HOST=${PG_HOST},PG_GHCNH_PORT=5432,PG_GHCNH_DB=weatherdata,PG_GHCNH_USERNAME=ghcnh_etl,PG_GHCNH_PASSWORD_S3_KEY=${PG_SECRET_KEY},PGSSLMODE=require}" \
    "--vpc-config SubnetIds=${SUBNET_IDS},SecurityGroupIds=${LAMBDA_SG}"

echo
echo "=== deployed ==="
aws lambda list-functions --region "${AWS_REGION}" \
    --query "Functions[?starts_with(FunctionName,'ghcnh')].{Name:FunctionName,Runtime:Runtime,Mem:MemorySize,Timeout:Timeout}" \
    --output table
