#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT_DIR/.venv/bin/activate"

pkill -f "vllm serve cl-nagoya/ruri-v3-310m" 2>/dev/null || true
pkill -f "vllm serve cl-nagoya/ruri-v3-reranker-310m" 2>/dev/null || true
sleep 1

vllm serve cl-nagoya/ruri-v3-310m --port 8081 --gpu-memory-utilization 0.2 > /tmp/vllm-embed.log 2>&1 &
vllm serve cl-nagoya/ruri-v3-reranker-310m --port 8082 --gpu-memory-utilization 0.2 > /tmp/vllm-rerank.log 2>&1 &

echo "Embedding on 8081, Reranker on 8082"
wait
