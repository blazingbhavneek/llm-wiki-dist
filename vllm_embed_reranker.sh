#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT_DIR/.venv/bin/activate"

EMBED_MODEL_NAME="Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL_NAME="BAAI/bge-reranker-v2-m3"
EMBED_MODEL_PATH="Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL_PATH="BAAI/bge-reranker-v2-m3"

# for model_path in "$EMBED_MODEL_PATH" "$RERANK_MODEL_PATH"; do
#   if [[ ! -f "$model_path/config.json" ]]; then
#     echo "Missing local model: $model_path" >&2
#     exit 1
#   fi
# done

pkill -f "vllm serve $EMBED_MODEL_NAME" 2>/dev/null || true
pkill -f "vllm serve $RERANK_MODEL_NAME" 2>/dev/null || true
pkill -f "vllm serve $EMBED_MODEL_PATH" 2>/dev/null || true
pkill -f "vllm serve $RERANK_MODEL_PATH" 2>/dev/null || true
sleep 1

vllm serve "$EMBED_MODEL_PATH" \
  --served-model-name "$EMBED_MODEL_NAME" \
  --port 8000 \
  --gpu-memory-utilization 0.5 \
  > /tmp/vllm-embed.log 2>&1 &

vllm serve "$RERANK_MODEL_PATH" \
  --served-model-name "$RERANK_MODEL_NAME" \
  --port 8001 \
  --gpu-memory-utilization 0.4 \
  > /tmp/vllm-rerank.log 2>&1 &

echo "Embedding on 8000, Reranker on 8001"
echo "Logs: /tmp/vllm-embed.log and /tmp/vllm-rerank.log"
wait
