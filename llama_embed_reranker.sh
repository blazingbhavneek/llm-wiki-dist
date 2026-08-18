#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LLAMA_SERVER="/home/seigyo/c_repo/bhavneek/llama.cpp/build/bin/llama-server"

EMBED_MODEL_NAME="Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL_NAME="BAAI/bge-reranker-v2-m3"

EMBED_MODEL_PATH="/home/seigyo/c_repo/bhavneek/models/Qwen3-Embedding-0.6B-Q8_0.gguf"
RERANK_MODEL_PATH="/home/seigyo/c_repo/bhavneek/models/bge-reranker-v2-m3-Q8_0.gguf"

EMBED_PORT=10001
RERANK_PORT=10002

EMBED_LOG="/tmp/llama-embed.log"
RERANK_LOG="/tmp/llama-rerank.log"

# Use all visible CUDA GPUs.
# If you want to restrict GPUs, run like:
# CUDA_VISIBLE_DEVICES=0 ./run_llama_embed_rerank.sh
# CUDA_VISIBLE_DEVICES=0,1 ./run_llama_embed_rerank.sh

if [[ ! -x "$LLAMA_SERVER" ]]; then
  echo "Missing or non-executable llama-server: $LLAMA_SERVER" >&2
  exit 1
fi

if [[ ! -f "$EMBED_MODEL_PATH" ]]; then
  echo "Missing embedding model: $EMBED_MODEL_PATH" >&2
  exit 1
fi

if [[ ! -f "$RERANK_MODEL_PATH" ]]; then
  echo "Missing reranker model: $RERANK_MODEL_PATH" >&2
  exit 1
fi

echo "Stopping old llama.cpp embedding/reranker servers..."

pkill -f "llama-server.*${EMBED_PORT}" 2>/dev/null || true
pkill -f "llama-server.*${RERANK_PORT}" 2>/dev/null || true
pkill -f "$EMBED_MODEL_PATH" 2>/dev/null || true
pkill -f "$RERANK_MODEL_PATH" 2>/dev/null || true

sleep 1

rm -f "$EMBED_LOG" "$RERANK_LOG"

echo "Starting embedding server..."
echo "  Model: $EMBED_MODEL_PATH"
echo "  Port:  $EMBED_PORT"
echo "  Log:   $EMBED_LOG"

"$LLAMA_SERVER" \
  --model "$EMBED_MODEL_PATH" \
  --alias "$EMBED_MODEL_NAME" \
  --host 0.0.0.0 \
  --port "$EMBED_PORT" \
  --embedding \
  --pooling last \
  --n-gpu-layers 999 \
  --split-mode layer \
  --threads 1 \
  --threads-batch 1 \
  --ctx-size 8192 \
  --batch-size 8192 \
  --ubatch-size 1024 \
  > "$EMBED_LOG" 2>&1 &

EMBED_PID=$!

sleep 2

echo "Starting reranker server..."
echo "  Model: $RERANK_MODEL_PATH"
echo "  Port:  $RERANK_PORT"
echo "  Log:   $RERANK_LOG"

"$LLAMA_SERVER" \
  --model "$RERANK_MODEL_PATH" \
  --alias "$RERANK_MODEL_NAME" \
  --host 0.0.0.0 \
  --port "$RERANK_PORT" \
  --reranking \
  --pooling rank \
  --n-gpu-layers 999 \
  --split-mode layer \
  --threads 1 \
  --threads-batch 1 \
  --ctx-size 8192 \
  --batch-size 8192 \
  --ubatch-size 1024 \
  > "$RERANK_LOG" 2>&1 &

RERANK_PID=$!

echo
echo "Embedding server running:"
echo "  URL:   http://0.0.0.0:${EMBED_PORT}"
echo "  Model: ${EMBED_MODEL_NAME}"
echo "  PID:   ${EMBED_PID}"
echo "  Log:   ${EMBED_LOG}"
echo
echo "Reranker server running:"
echo "  URL:   http://0.0.0.0:${RERANK_PORT}"
echo "  Model: ${RERANK_MODEL_NAME}"
echo "  PID:   ${RERANK_PID}"
echo "  Log:   ${RERANK_LOG}"
echo
echo "Follow logs:"
echo "  tail -f ${EMBED_LOG} ${RERANK_LOG}"
echo
echo "GPU usage:"
echo "  watch -n 1 nvidia-smi"
echo

wait