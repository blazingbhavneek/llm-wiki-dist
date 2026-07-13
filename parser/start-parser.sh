#!/usr/bin/env bash
set -Eeuo pipefail

readonly MODEL_ROOT="/opt/mineru/models"
readonly MODEL_CONFIG="/opt/mineru/mineru.json"
readonly MODEL_MANIFEST="/opt/mineru/model-manifest.json"
readonly PIPELINE_REVISION="${MINERU_PIPELINE_REVISION:?missing MINERU_PIPELINE_REVISION}"
readonly VLM_REVISION="${MINERU_VLM_REVISION:?missing MINERU_VLM_REVISION}"

export MINERU_BACKEND="${MINERU_BACKEND:-hybrid-engine}"
export MINERU_METHOD="${MINERU_METHOD:-auto}"
export MINERU_EFFORT="${MINERU_EFFORT:-medium}"

python3 /usr/local/lib/model_bundle.py verify \
  --model-root "${MODEL_ROOT}" \
  --config "${MODEL_CONFIG}" \
  --manifest "${MODEL_MANIFEST}" \
  --pipeline-revision "${PIPELINE_REVISION}" \
  --vlm-revision "${VLM_REVISION}"

if [[ "${PARSER_REQUIRE_GPU:-1}" == "1" ]]; then
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "ERROR: NVIDIA runtime is unavailable. Start with: docker run --gpus all ..." >&2
    exit 1
  fi
  if ! nvidia-smi >/dev/null; then
    echo "ERROR: NVIDIA driver injection failed. Check nvidia-container-toolkit." >&2
    exit 1
  fi

fi

echo "Starting parser with MinerU ${MINERU_VERSION}; backend=${MINERU_BACKEND}; method=${MINERU_METHOD}; effort=${MINERU_EFFORT}"
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
