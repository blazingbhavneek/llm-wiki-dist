#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "/home/blaze/.venvs/llm-wiki-dist/bin/activate"

pkill -f "vllm serve cl-nagoya/ruri-v3-310m" 2>/dev/null || true
pkill -f "vllm serve cl-nagoya/ruri-v3-reranker-310m" 2>/dev/null || true
sleep 1

vllm serve Qwen/Qwen3-Embedding-0.6B --port 8081 --gpu-memory-utilization 0.5 --max-model-len 32000 > /tmp/vllm-embed.log 2>&1 &
vllm serve BAAI/bge-reranker-v2-m3 --port 8082 --gpu-memory-utilization 0.3  > /tmp/vllm-rerank.log 2>&1 &

echo "Embedding on 8081, Reranker on 8082"
wait
