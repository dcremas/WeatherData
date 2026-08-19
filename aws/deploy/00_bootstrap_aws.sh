#!/bin/zsh
# One-time AWS setup for the GHCNh pipeline: bucket, security group, IAM roles,
# and the SSM parameter holding the warehouse password.
#
# Idempotent - every step checks for what it is about to create, so re-running
# after a partial failure is safe. It touches nothing belonging to the
# apple_weatherkit pipeline; the GHCNh functions get their own bucket, their own
# security group and their own roles, so a change to one cannot break the other.
#
# Run 00 -> 01 -> 02 -> 03 in order. This script is the only one that needs
# permissions beyond Lambda and S3.

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


BUCKET="${GHCNH_BUCKET}"
LAMBDA_SG_NAME="ghcnh-lambda-sg"
DOWNLOAD_ROLE="ghcnhDownloadS3-role"
LOADER_ROLE="ghcnhPostgresqlUpdate-role"

say() { print -r -- "=== $* ==="; }

# ---------------------------------------------------------------------------
# S3 bucket
# ---------------------------------------------------------------------------
say "S3 bucket ${BUCKET}"
if aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
    echo "already exists"
else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${AWS_REGION}" \
        --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    echo "created"
fi

# Public access stays fully blocked: this holds NOAA-derived data that is public
# at source, but nothing here needs to be served from S3.
aws s3api put-public-access-block --bucket "${BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption --bucket "${BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# The staging copies and the raw per-station files are reproducible from NOAA at
# any time, so they are not worth storing indefinitely. The promoted combined
# parquet under ghcnh_parquet/ is left alone - the loader reads it every night.
aws s3api put-bucket-lifecycle-configuration --bucket "${BUCKET}" \
    --lifecycle-configuration '{
      "Rules": [
        {"ID": "expire-staging", "Status": "Enabled",
         "Filter": {"Prefix": "ghcnh_parquet_staging/"},
         "Expiration": {"Days": 7}},
        {"ID": "expire-raw", "Status": "Enabled",
         "Filter": {"Prefix": "ghcnh_files/"},
         "Expiration": {"Days": 30}}
      ]}'
echo "encryption, public-access block and lifecycle rules applied"

say "uploading the station roster"
aws s3 cp "${REPO}/metadata/stations.csv" "s3://${BUCKET}/metadata/stations.csv" --quiet
echo "s3://${BUCKET}/metadata/stations.csv"

# ---------------------------------------------------------------------------
# Security group for the loader's VPC attachment
# ---------------------------------------------------------------------------
say "security group ${LAMBDA_SG_NAME}"
LAMBDA_SG=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${LAMBDA_SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [[ "${LAMBDA_SG}" == "None" || -z "${LAMBDA_SG}" ]]; then
    LAMBDA_SG=$(aws ec2 create-security-group \
        --group-name "${LAMBDA_SG_NAME}" \
        --description "GHCNh ETL Lambda: egress to EC2 Postgres + S3 endpoint" \
        --vpc-id "${VPC_ID}" --query 'GroupId' --output text)
    echo "created ${LAMBDA_SG}"

    # A new security group starts with allow-all egress. Replacing it with these
    # two rules means the loader can reach the warehouse and the S3 gateway
    # endpoint and nothing else - it has no business talking to the internet,
    # and in this VPC (no NAT) it could not anyway.
    aws ec2 revoke-security-group-egress --group-id "${LAMBDA_SG}" \
        --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' >/dev/null 2>&1 || true

    aws ec2 authorize-security-group-egress --group-id "${LAMBDA_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,
            \"UserIdGroupPairs\":[{\"GroupId\":\"${EC2_PG_SG}\",\"Description\":\"EC2 Postgres\"}]}]" >/dev/null

    aws ec2 authorize-security-group-egress --group-id "${LAMBDA_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":443,\"ToPort\":443,
            \"PrefixListIds\":[{\"PrefixListId\":\"${S3_PREFIX_LIST}\",\"Description\":\"S3 gateway endpoint\"}]}]" >/dev/null
    echo "egress rules applied"
else
    echo "already exists: ${LAMBDA_SG}"
fi

say "ingress on the Postgres security group"
if aws ec2 describe-security-groups --group-ids "${EC2_PG_SG}" \
    --query "SecurityGroups[0].IpPermissions[?FromPort==\`5432\`].UserIdGroupPairs[].GroupId" \
    --output text | grep -q "${LAMBDA_SG}"; then
    echo "already allowed"
else
    aws ec2 authorize-security-group-ingress --group-id "${EC2_PG_SG}" \
        --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":5432,\"ToPort\":5432,
            \"UserIdGroupPairs\":[{\"GroupId\":\"${LAMBDA_SG}\",\"Description\":\"GHCNh ETL Lambda in VPC\"}]}]" >/dev/null
    echo "5432 opened from ${LAMBDA_SG}"
fi

# ---------------------------------------------------------------------------
# IAM roles
# ---------------------------------------------------------------------------
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

ensure_role() {
    local role_name="$1"
    if aws iam get-role --role-name "${role_name}" >/dev/null 2>&1; then
        echo "role ${role_name} already exists"
    else
        aws iam create-role --role-name "${role_name}" \
            --assume-role-policy-document "${TRUST}" \
            --description "GHCNh ETL pipeline" >/dev/null
        echo "role ${role_name} created"
    fi
}

say "IAM roles"
ensure_role "${DOWNLOAD_ROLE}"
ensure_role "${LOADER_ROLE}"

aws iam attach-role-policy --role-name "${DOWNLOAD_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam attach-role-policy --role-name "${LOADER_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam attach-role-policy --role-name "${LOADER_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole

# Scoped to this one bucket, rather than the AmazonS3FullAccess the
# apple_weatherkit roles carry. Same reasoning as the 2026-08-14 hardening that
# removed the static access keys: each function should reach exactly what it
# needs and no more.
#
# Both policies name prefixes rather than the whole bucket, specifically so that
# secrets/ is reachable by the loader alone. A blanket ${BUCKET}/* on the
# download role would let the extract half read the warehouse credential it has
# no use for.
say "inline S3 policies"
aws iam put-role-policy --role-name "${DOWNLOAD_ROLE}" \
    --policy-name "ghcnh-bucket-rw" \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],
       \"Resource\":[\"arn:aws:s3:::${BUCKET}/metadata/*\",
                     \"arn:aws:s3:::${BUCKET}/ghcnh_parquet/*\",
                     \"arn:aws:s3:::${BUCKET}/ghcnh_parquet_staging/*\",
                     \"arn:aws:s3:::${BUCKET}/ghcnh_files/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"s3:ListBucket\"],
       \"Resource\":\"arn:aws:s3:::${BUCKET}\"}]}"

aws iam put-role-policy --role-name "${LOADER_ROLE}" \
    --policy-name "ghcnh-bucket-ro" \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\"],
       \"Resource\":[\"arn:aws:s3:::${BUCKET}/ghcnh_parquet/*\",
                     \"arn:aws:s3:::${BUCKET}/secrets/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"s3:ListBucket\"],
       \"Resource\":\"arn:aws:s3:::${BUCKET}\"}]}"

# The loader used to read the credential from SSM directly, which this VPC
# cannot route to - see the note in 02_deploy_functions.sh. The grant that
# allowed it is dead weight now, and put-role-policy above would not remove it.
aws iam delete-role-policy --role-name "${LOADER_ROLE}" \
    --policy-name "ghcnh-pg-password" 2>/dev/null \
    && echo "removed the obsolete ssm:GetParameter grant" || true
echo "attached"

# ---------------------------------------------------------------------------
# Warehouse password
# ---------------------------------------------------------------------------
say "SSM parameter ${PG_PASSWORD_PARAM}"
if aws ssm get-parameter --name "${PG_PASSWORD_PARAM}" >/dev/null 2>&1; then
    echo "already set - leaving it alone"
else
    if [[ -z "${GHCNH_PG_PASSWORD:-}" ]]; then
        echo "GHCNH_PG_PASSWORD is not set." >&2
        echo "Generate one, create the matching Postgres role with" >&2
        echo "deploy/04_postgres_role.sh, then re-run:" >&2
        echo "  GHCNH_PG_PASSWORD='...' ./deploy/00_bootstrap_aws.sh" >&2
        exit 1
    fi
    aws ssm put-parameter --name "${PG_PASSWORD_PARAM}" --type SecureString \
        --value "${GHCNH_PG_PASSWORD}" \
        --description "Password for the ghcnh_etl Postgres role on lambda-playground-1" >/dev/null
    echo "stored as a SecureString"
fi

say "done"
echo "LAMBDA_SG=${LAMBDA_SG}"
echo "next: ./deploy/04_postgres_role.sh, then 01_build_layer.sh"
