#!/bin/zsh
# Build and publish the ghcnh_dependencies Lambda layer.
#
# Both functions use this one layer. It carries pyarrow, SQLAlchemy and
# psycopg2, plus shared_funcs.py copied straight out of the WeatherData repo so
# the GHCNh -> observations mapping has exactly one implementation: the Lambdas
# run the same transform, converters, quality-code rejects and COPY writer that
# `python ghcnh_process.py` runs on the laptop.
#
# Wheels are fetched for manylinux/x86_64 rather than being built here - a
# macOS-native pyarrow would import fine locally and fail in Lambda with
# "invalid ELF header".

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


LAYER_NAME="ghcnh_dependencies"
PY_VERSION="3.12"
HERE="${0:A:h}"
ROOT="${HERE:h}"
BUILD="${ROOT}/layer/build"
BUCKET="${GHCNH_BUCKET:-noaa-ghcnh-weatherdata}"

echo "=== building ${LAYER_NAME} for python${PY_VERSION} (manylinux x86_64) ==="

rm -rf "${BUILD}"
mkdir -p "${BUILD}/python"

python3 -m pip install \
    --requirement "${ROOT}/layer/requirements.txt" \
    --target "${BUILD}/python" \
    --platform manylinux2014_x86_64 \
    --python-version "${PY_VERSION}" \
    --implementation cp \
    --only-binary=:all: \
    --upgrade \
    --quiet

# The repo's shared_funcs.py IS the transform. Copying rather than vendoring a
# fork means a fix made on the laptop reaches the pipeline by rebuilding this
# layer, and there is never a second copy to drift.
cp "${REPO}/shared_funcs.py" "${BUILD}/python/shared_funcs.py"

# Trim what Lambda will never execute. Untrimmed this comes to 230 MB against a
# 250 MB hard limit for all layers plus function code combined, so the trim is
# what makes the layer viable rather than a tidy-up.
find "${BUILD}/python" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}/python" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}/python" -type d -name "include" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}/python" -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}/python" -type f \( -name "*.pyx" -o -name "*.pxd" -o -name "*.a" \) -delete 2>/dev/null || true
rm -rf "${BUILD}/python/numpy/f2py" "${BUILD}/python/sqlalchemy/testing" 2>/dev/null || true

# Arrow Flight, its gRPC transport: 20 MB that nothing here can reach. It is
# safe to drop because libarrow_flight.so is a hard DT_NEEDED of exactly one
# other object, libarrow_python_flight.so, which goes with it.
#
# Judge these by the ELF NEEDED graph, not by whether the Python side imports
# them. libarrow_substrait.so looks equally unreachable - nothing here imports
# pyarrow.substrait - but the wheel links it into lib, _compute, _parquet,
# _dataset and fifteen others, so removing it breaks `import pyarrow` outright
# with "libarrow_substrait.so.1700: cannot open shared object file". Same for
# dataset and acero, which pyarrow.parquet imports at module scope.
#
# To re-check after a pyarrow upgrade:
#   readelf -d layer/build/python/pyarrow/*.so* | grep NEEDED
rm -f "${BUILD}/python/pyarrow/libarrow_flight.so"* \
      "${BUILD}/python/pyarrow/libarrow_python_flight.so" \
      "${BUILD}/python/pyarrow/_flight."*.so 2>/dev/null || true

UNZIPPED_MB=$(du -sm "${BUILD}/python" | cut -f1)
echo "unzipped layer size: ${UNZIPPED_MB} MB (Lambda's hard limit is 250 MB for"
echo "all layers plus function code combined)"
if (( UNZIPPED_MB > 230 )); then
    echo "ERROR: ${UNZIPPED_MB} MB leaves no headroom for the function packages." >&2
    exit 1
fi

cd "${BUILD}"
rm -f "${ROOT}/layer/${LAYER_NAME}.zip"
zip -qr "${ROOT}/layer/${LAYER_NAME}.zip" python

ZIP_MB=$(du -sm "${ROOT}/layer/${LAYER_NAME}.zip" | cut -f1)
echo "zipped: ${ZIP_MB} MB"

# Layers over 50 MB cannot be uploaded inline and must come from S3.
echo "=== publishing ${LAYER_NAME} ==="
aws s3 cp "${ROOT}/layer/${LAYER_NAME}.zip" \
    "s3://${BUCKET}/layers/${LAYER_NAME}.zip" --region "${AWS_REGION}" --quiet

LAYER_ARN=$(aws lambda publish-layer-version \
    --layer-name "${LAYER_NAME}" \
    --description "pyarrow + SQLAlchemy + psycopg2 + WeatherData shared_funcs" \
    --content "S3Bucket=${BUCKET},S3Key=layers/${LAYER_NAME}.zip" \
    --compatible-runtimes "python${PY_VERSION}" \
    --compatible-architectures x86_64 \
    --region "${AWS_REGION}" \
    --query 'LayerVersionArn' --output text)

echo "published: ${LAYER_ARN}"
echo "${LAYER_ARN}" > "${ROOT}/layer/.layer-arn"
echo "(written to layer/.layer-arn, which 02_deploy_functions.sh reads)"
