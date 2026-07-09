# syntax=docker/dockerfile:1

# MinerU's official Docker docs use vllm/vllm-openai as the GPU base.
# This keeps vLLM/CUDA runtime pieces prebuilt instead of installing the
# full cuda-toolkit stack again inside this app image.
FROM vllm/vllm-openai:v0.21.0
# If your host driver needs the CUDA 12.9-compatible vLLM image, use:
# FROM vllm/vllm-openai:v0.21.0-cu129

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENTRYPOINT []
USER root

ENV DEBIAN_FRONTEND=noninteractive
ENV TERM=xterm-256color

# ------------------------------------------------------------
# NVIDIA runtime defaults
# ------------------------------------------------------------
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

# ------------------------------------------------------------
# Proxy configuration
# ------------------------------------------------------------
ARG PROXY_URL="http://133.141.7.237:9515"
ARG NO_PROXY_VALUE="localhost,127.0.0.1,::1"

ENV http_proxy="${PROXY_URL}"
ENV https_proxy="${PROXY_URL}"
ENV HTTP_PROXY="${PROXY_URL}"
ENV HTTPS_PROXY="${PROXY_URL}"
ENV all_proxy="${PROXY_URL}"
ENV ALL_PROXY="${PROXY_URL}"
ENV no_proxy="${NO_PROXY_VALUE}"
ENV NO_PROXY="${NO_PROXY_VALUE}"

# ------------------------------------------------------------
# Parser app environment
# ------------------------------------------------------------
ENV APP_HOME=/home/seigyo/parser
ENV CUDA_HOME=/usr/local/cuda
ENV PATH="${APP_HOME}:${APP_HOME}/.local/bin:/usr/local/cuda/bin:/usr/local/bin:${PATH}"
ENV LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:${LD_LIBRARY_PATH}"

ENV CHROME_BIN=/usr/local/bin/chrome-headless-shell
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

ENV PYTHONDONTWRITEBYTECODE=1
ENV MINERU_MODEL_SOURCE=local

# Host cache is copied into this same path at build time.
ENV XDG_CACHE_HOME=/home/seigyo/.cache
ENV HF_HOME=/home/seigyo/.cache/huggingface
ENV HUGGINGFACE_HUB_CACHE=/home/seigyo/.cache/huggingface/hub

# Target MinerU allocation: 7 GiB = 7168 MiB
ENV GPU_TARGET_ALLOC_MB=7168

ARG NODE_MAJOR=22

# ------------------------------------------------------------
# Persist proxy settings
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      "http_proxy=${PROXY_URL}" \
      "https_proxy=${PROXY_URL}" \
      "HTTP_PROXY=${PROXY_URL}" \
      "HTTPS_PROXY=${PROXY_URL}" \
      "all_proxy=${PROXY_URL}" \
      "ALL_PROXY=${PROXY_URL}" \
      "no_proxy=${NO_PROXY_VALUE}" \
      "NO_PROXY=${NO_PROXY_VALUE}" \
      >> /etc/environment; \
    printf '%s\n' \
      "export http_proxy=${PROXY_URL}" \
      "export https_proxy=${PROXY_URL}" \
      "export HTTP_PROXY=${PROXY_URL}" \
      "export HTTPS_PROXY=${PROXY_URL}" \
      "export all_proxy=${PROXY_URL}" \
      "export ALL_PROXY=${PROXY_URL}" \
      "export no_proxy=${NO_PROXY_VALUE}" \
      "export NO_PROXY=${NO_PROXY_VALUE}" \
      > /etc/profile.d/proxy.sh; \
    chmod 0644 /etc/profile.d/proxy.sh; \
    if [ -n "${PROXY_URL}" ]; then \
      printf '%s\n' \
        "Acquire::http::Proxy \"${PROXY_URL}\";" \
        "Acquire::https::Proxy \"${PROXY_URL}\";" \
        "Acquire::ftp::Proxy \"${PROXY_URL}\";" \
        > /etc/apt/apt.conf.d/95proxies; \
    else \
      rm -f /etc/apt/apt.conf.d/95proxies; \
    fi

# ------------------------------------------------------------
# Enable APT package cache
# ------------------------------------------------------------
RUN set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    printf '%s\n' \
      'Binary::apt::APT::Keep-Downloaded-Packages "true";' \
      'APT::Keep-Downloaded-Packages "true";' \
      > /etc/apt/apt.conf.d/keep-cache

# ------------------------------------------------------------
# Base packages
# ------------------------------------------------------------
RUN --mount=type=cache,id=apt-lists-parser-reduced,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache-parser-reduced,target=/var/cache/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    if apt-cache show libasound2t64 >/dev/null 2>&1; then \
      ALSA_PKG=libasound2t64; \
    else \
      ALSA_PKG=libasound2; \
    fi; \
    apt-get install -y --no-install-recommends \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      ca-certificates \
      curl \
      wget \
      gnupg \
      unzip \
      jq \
      openssh-server \
      sudo \
      bash \
      git \
      nano \
      tmux \
      iproute2 \
      fontconfig \
      fonts-liberation \
      fonts-noto-core \
      fonts-noto-color-emoji \
      fonts-noto-cjk \
      "${ALSA_PKG}" \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libcairo2 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libexpat1 \
      libgbm1 \
      libgl1 \
      libglib2.0-0 \
      libgtk-3-0 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      xdg-utils; \
    command -v ssh-keygen; \
    fc-cache -fv

# ------------------------------------------------------------
# CUDA env for shells.
# vLLM base provides the CUDA/vLLM runtime; this reduced Dockerfile
# intentionally does not apt-install cuda-toolkit.
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      'export CUDA_HOME=/usr/local/cuda' \
      'export PATH=/usr/local/cuda/bin:$PATH' \
      'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:$LD_LIBRARY_PATH' \
      > /etc/profile.d/cuda.sh; \
    chmod 0644 /etc/profile.d/cuda.sh; \
    if command -v nvcc >/dev/null 2>&1; then nvcc --version; else echo "WARNING: nvcc not found in base image"; fi

# ------------------------------------------------------------
# wget proxy
# ------------------------------------------------------------
RUN set -eux; \
    if [ -n "${PROXY_URL}" ]; then \
      printf '%s\n' \
        "use_proxy = on" \
        "http_proxy = ${PROXY_URL}" \
        "https_proxy = ${PROXY_URL}" \
        > /etc/wgetrc; \
    fi

# ------------------------------------------------------------
# git proxy
# ------------------------------------------------------------
RUN set -eux; \
    if [ -n "${PROXY_URL}" ]; then \
      git config --system http.proxy "${PROXY_URL}"; \
      git config --system https.proxy "${PROXY_URL}"; \
    else \
      git config --system --unset http.proxy || true; \
      git config --system --unset https.proxy || true; \
    fi

# ------------------------------------------------------------
# Install Node.js
# ------------------------------------------------------------
RUN --mount=type=cache,id=node-download-cache-parser-reduced,target=/root/.cache/node \
    set -eux; \
    cd /tmp; \
    NODE_DIST_BASE="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"; \
    curl -fsSL "${NODE_DIST_BASE}/SHASUMS256.txt" -o node-shasums.txt; \
    NODE_TARBALL="$(awk '$2 ~ /node-v.*-linux-x64\.tar\.xz$/ { print $2; exit }' node-shasums.txt)"; \
    test -n "${NODE_TARBALL}"; \
    curl -fsSL "${NODE_DIST_BASE}/${NODE_TARBALL}" -o "${NODE_TARBALL}"; \
    grep "  ${NODE_TARBALL}$" node-shasums.txt | sha256sum -c -; \
    mkdir -p node-dist; \
    python3 -c "import tarfile; tarfile.open('${NODE_TARBALL}', 'r:xz').extractall('node-dist')"; \
    NODE_DIR="$(find node-dist -mindepth 1 -maxdepth 1 -type d | head -n 1)"; \
    test -n "${NODE_DIR}"; \
    cp -a "${NODE_DIR}/." /usr/local/; \
    rm -rf node-shasums.txt "${NODE_TARBALL}" node-dist; \
    node --version; \
    npm --version

# ------------------------------------------------------------
# npm proxy
# ------------------------------------------------------------
RUN set -eux; \
    if [ -n "${PROXY_URL}" ]; then \
      npm config set proxy "${PROXY_URL}" --global; \
      npm config set https-proxy "${PROXY_URL}" --global; \
    else \
      npm config delete proxy --global || true; \
      npm config delete https-proxy --global || true; \
    fi; \
    npm config set fetch-retries 5 --global; \
    npm config set fetch-retry-mintimeout 20000 --global; \
    npm config set fetch-retry-maxtimeout 120000 --global

# ------------------------------------------------------------
# Create user and configure SSH
# ------------------------------------------------------------
RUN set -eux; \
    useradd -m -s /bin/bash seigyo; \
    echo "seigyo:seigyo@rikiseisan" | chpasswd; \
    usermod -aG sudo seigyo; \
    mkdir -p /run/sshd /etc/ssh /etc/ssh/sshd_config.d; \
    touch /etc/ssh/sshd_config; \
    mkdir -p /home/seigyo/.ssh; \
    chmod 700 /home/seigyo/.ssh; \
    chown -R seigyo:seigyo /home/seigyo/.ssh; \
    if ! grep -q '^Include /etc/ssh/sshd_config.d/\*.conf' /etc/ssh/sshd_config; then \
      printf '%s\n' 'Include /etc/ssh/sshd_config.d/*.conf' >> /etc/ssh/sshd_config; \
    fi; \
    if ! grep -q '^Subsystem[[:space:]]\+sftp' /etc/ssh/sshd_config; then \
      printf '%s\n' 'Subsystem sftp /usr/lib/openssh/sftp-server' >> /etc/ssh/sshd_config; \
    fi; \
    sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config; \
    sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config; \
    sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config; \
    if ! grep -q '^PasswordAuthentication ' /etc/ssh/sshd_config; then \
      printf '%s\n' 'PasswordAuthentication yes' >> /etc/ssh/sshd_config; \
    fi; \
    if ! grep -q '^PermitRootLogin ' /etc/ssh/sshd_config; then \
      printf '%s\n' 'PermitRootLogin no' >> /etc/ssh/sshd_config; \
    fi; \
    if ! grep -q '^PubkeyAuthentication ' /etc/ssh/sshd_config; then \
      printf '%s\n' 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config; \
    fi; \
    ssh-keygen -A

# ------------------------------------------------------------
# Persist CUDA env for seigyo login shells too
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      '' \
      '# CUDA runtime' \
      'export CUDA_HOME=/usr/local/cuda' \
      'export PATH=/usr/local/cuda/bin:$PATH' \
      'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:$LD_LIBRARY_PATH' \
      >> /home/seigyo/.bashrc; \
    chown seigyo:seigyo /home/seigyo/.bashrc

# ------------------------------------------------------------
# Create parser app/cache directories
# ------------------------------------------------------------
RUN mkdir -p \
      ${APP_HOME} \
      /home/seigyo/.cache \
      /home/seigyo/.cache/huggingface \
      /home/seigyo/.cache/huggingface/hub \
    && chown -R seigyo:seigyo /home/seigyo

# ------------------------------------------------------------
# Copy host HuggingFace cache/model files into image.
#
# Build with:
#   docker buildx build \
#     --build-context hf_cache=/home/seigyo/.cache/huggingface \
#     -f parser/Dockerfile.reduced \
#     -t doc-parser-rikiseisan:reduced \
#     --load \
#     .
# ------------------------------------------------------------
COPY --from=hf_cache --chown=seigyo:seigyo . /home/seigyo/.cache/huggingface/

RUN set -eux; \
    chown -R seigyo:seigyo /home/seigyo/.cache/huggingface; \
    chmod -R a+rX /home/seigyo/.cache/huggingface

WORKDIR ${APP_HOME}

# ------------------------------------------------------------
# Download Chrome Headless Shell
# ------------------------------------------------------------
RUN --mount=type=cache,id=chrome-download-cache-parser-reduced,target=/root/.cache/chrome-download \
    set -eux; \
    CHROME_URL="$(curl -fsSL https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json \
      | jq -r '.channels.Stable.downloads["chrome-headless-shell"][] | select(.platform=="linux64") | .url')"; \
    echo "Downloading Chrome Headless Shell from: ${CHROME_URL}"; \
    curl -fsSL "${CHROME_URL}" -o /tmp/chrome-headless-shell-linux64.zip; \
    unzip /tmp/chrome-headless-shell-linux64.zip -d /opt; \
    rm /tmp/chrome-headless-shell-linux64.zip; \
    ln -sf /opt/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chrome-headless-shell; \
    chmod +x /opt/chrome-headless-shell-linux64/chrome-headless-shell; \
    chrome-headless-shell --version

# ------------------------------------------------------------
# Puppeteer config for Mermaid CLI / mmdc
# ------------------------------------------------------------
RUN cat > ${APP_HOME}/puppeteer-config.json <<'EOF'
{
  "executablePath": "/usr/local/bin/chrome-headless-shell",
  "args": [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer"
  ]
}
EOF

RUN chown seigyo:seigyo ${APP_HOME}/puppeteer-config.json

# ------------------------------------------------------------
# Install Mermaid CLI
# ------------------------------------------------------------
RUN --mount=type=cache,id=npm-cache-root-parser-reduced,target=/root/.npm \
    set -eux; \
    npm install -g @mermaid-js/mermaid-cli; \
    MMDC_ORIG="$(command -v mmdc)"; \
    echo "Original mmdc path: ${MMDC_ORIG}"; \
    if [ "${MMDC_ORIG}" = "/usr/local/bin/mmdc" ]; then \
      mv /usr/local/bin/mmdc /usr/local/bin/mmdc.real; \
      MMDC_REAL="/usr/local/bin/mmdc.real"; \
    else \
      MMDC_REAL="${MMDC_ORIG}"; \
    fi; \
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -e' \
      '' \
      'REAL_MMDC="'"${MMDC_REAL}"'"' \
      'PUPPETEER_CONFIG="/home/seigyo/parser/puppeteer-config.json"' \
      '' \
      'for arg in "$@"; do' \
      '  case "$arg" in' \
      '    --help|-h|--version|-V)' \
      '      exec "$REAL_MMDC" "$@"' \
      '      ;;' \
      '  esac' \
      'done' \
      '' \
      'for arg in "$@"; do' \
      '  case "$arg" in' \
      '    -p|--puppeteerConfig|--puppeteer-config|--puppeteerConfigFile|--puppeteer-config-file)' \
      '      exec "$REAL_MMDC" "$@"' \
      '      ;;' \
      '    -p=*|--puppeteerConfig=*|--puppeteer-config=*|--puppeteerConfigFile=*|--puppeteer-config-file=*)' \
      '      exec "$REAL_MMDC" "$@"' \
      '      ;;' \
      '  esac' \
      'done' \
      '' \
      'exec "$REAL_MMDC" -p "$PUPPETEER_CONFIG" "$@"' \
      > /usr/local/bin/mmdc; \
    chmod +x /usr/local/bin/mmdc; \
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      'exec mmdc "$@"' \
      > /usr/local/bin/mermaid-render; \
    chmod +x /usr/local/bin/mermaid-render; \
    mmdc --version

# ------------------------------------------------------------
# Quick Mermaid CLI smoke test
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      'graph TD' \
      '  A[Parser calls mmdc] --> B[mmdc wrapper injects config]' \
      '  B --> C[Chrome Headless Shell renders SVG]' \
      > /tmp/mermaid-test.mmd; \
    mmdc -i /tmp/mermaid-test.mmd -o /tmp/mermaid-test.svg; \
    test -s /tmp/mermaid-test.svg; \
    rm -f /tmp/mermaid-test.mmd /tmp/mermaid-test.svg

# ------------------------------------------------------------
# Backend dependency setup
# ------------------------------------------------------------
# PIP_CACHE_DIR overrides XDG_CACHE_HOME, which points at seigyo-owned
# /home/seigyo/.cache; pip disables a cache dir not owned by uid 0 when run as root.
RUN --mount=type=cache,id=pip-cache-parser-reduced,target=/root/.cache/pip \
    set -eux; \
    export PIP_CACHE_DIR=/root/.cache/pip; \
    python3 -m pip install --upgrade --break-system-packages \
      'mineru[all]>=3.4.0' \
      fastapi \
      'uvicorn[standard]' \
      requests \
      python-dotenv \
      python-multipart; \
    command -v mineru; \
    python3 -c "import fastapi, requests, dotenv, uvicorn"

# ------------------------------------------------------------
# Copy parser backend source files
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo parser/*.py ${APP_HOME}/
COPY --chown=seigyo:seigyo parser/test_utils/ ${APP_HOME}/test_utils/

# ------------------------------------------------------------
# Copy parser frontend package files first for Docker layer cache
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo parser/frontend/package*.json ${APP_HOME}/frontend/

# ------------------------------------------------------------
# Install parser frontend dependencies
# ------------------------------------------------------------
WORKDIR ${APP_HOME}/frontend

RUN --mount=type=cache,id=npm-cache-root-parser-reduced-frontend,target=/root/.npm \
    set -eux; \
    rm -rf node_modules; \
    if [ -f package-lock.json ]; then \
      npm ci; \
    else \
      npm install; \
    fi

# ------------------------------------------------------------
# Copy parser frontend source after npm deps are installed
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo parser/frontend/ ${APP_HOME}/frontend/

# ------------------------------------------------------------
# Build parser frontend, then drop node_modules from the final image
# ------------------------------------------------------------
RUN --mount=type=cache,id=npm-cache-root-parser-reduced-frontend,target=/root/.npm \
    set -eux; \
    npm run build; \
    if [ -d dist ]; then chown -R seigyo:seigyo dist; fi; \
    if [ -d build ]; then chown -R seigyo:seigyo build; fi; \
    if [ -d .next ]; then chown -R seigyo:seigyo .next; fi; \
    rm -rf node_modules

# ------------------------------------------------------------
# Startup script
#
# GPU_MEMORY_UTILIZATION behavior:
#   - Always calculated from total GPU VRAM.
#   - Formula:
#       GPU_MEMORY_UTILIZATION = 7168 / total_gpu_memory_mb
#   - Fails container startup if:
#       nvidia-smi missing
#       total GPU VRAM < 7168 MB
#
# MinerU models:
#   - Expected to already exist in:
#       /home/seigyo/.cache/huggingface
#   - Copied from host at build time using named build context hf_cache.
# ------------------------------------------------------------
WORKDIR ${APP_HOME}

RUN cat > /usr/local/bin/start-parser.sh <<'EOF'
#!/usr/bin/env bash
set -u

APP_HOME="/home/seigyo/parser"
SESSION_NAME="parser"
TARGET_MB="${GPU_TARGET_ALLOC_MB:-7168}"

export HOME="/home/seigyo"
export CUDA_HOME="/usr/local/cuda"
export PATH="/usr/local/cuda/bin:/usr/local/bin:${PATH}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:${LD_LIBRARY_PATH:-}"
export MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-local}"

echo "============================================================"
echo "Starting parser container"
echo "============================================================"

mkdir -p /run/sshd
ssh-keygen -A >/dev/null 2>&1 || true

echo "Starting sshd..."
/usr/sbin/sshd -e || echo "WARNING: sshd failed to start"

echo
echo "CUDA check:"
echo "  CUDA_HOME=${CUDA_HOME}"
echo "  PATH=${PATH}"
echo "  LD_LIBRARY_PATH=${LD_LIBRARY_PATH}"

if command -v nvcc >/dev/null 2>&1; then
  echo "nvcc found:"
  command -v nvcc
  nvcc --version || true
else
  echo "WARNING: nvcc not found. This image relies on the prebuilt vLLM/MinerU runtime."
fi

echo
echo "GPU check:"
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ERROR: nvidia-smi not found."
  echo "ERROR: Run this container with GPU access, for example:"
  echo "  docker run --gpus all ..."
  exit 1
fi

nvidia-smi || true

GPU_TOTAL_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -d ' ')"

if [ -z "${GPU_TOTAL_MB}" ]; then
  echo "ERROR: Could not read total GPU memory from nvidia-smi."
  exit 1
fi

if [ "${GPU_TOTAL_MB}" -lt "${TARGET_MB}" ]; then
  echo "ERROR: GPU total VRAM is ${GPU_TOTAL_MB} MB."
  echo "ERROR: Parser requires at least ${TARGET_MB} MB total VRAM to allocate 7 GiB."
  exit 1
fi

GPU_MEMORY_UTILIZATION="$(awk -v target="${TARGET_MB}" -v total="${GPU_TOTAL_MB}" 'BEGIN { printf "%.6f", target / total }')"
export GPU_MEMORY_UTILIZATION

echo
echo "GPU allocation:"
echo "  GPU total memory: ${GPU_TOTAL_MB} MB"
echo "  Target allocation: ${TARGET_MB} MB"
echo "  GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION}"

echo
echo "Preparing HuggingFace cache directory..."
mkdir -p /home/seigyo/.cache/huggingface /home/seigyo/.cache/huggingface/hub
chown -R seigyo:seigyo /home/seigyo/.cache

echo
echo "HuggingFace cache:"
du -sh /home/seigyo/.cache/huggingface 2>/dev/null || true
find /home/seigyo/.cache/huggingface -maxdepth 3 -type d 2>/dev/null | head -n 30 || true

echo
echo "MinerU:"
command -v mineru || true
mineru --help >/dev/null 2>&1 || true

echo
echo "Starting parser tmux session..."

if runuser -u seigyo -- tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session already exists: ${SESSION_NAME}"
else
  runuser -u seigyo -- tmux new-session -d -s "${SESSION_NAME}" -c "${APP_HOME}" bash

  sleep 1

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "cd ${APP_HOME}" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export HOME=/home/seigyo" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export CUDA_HOME=/usr/local/cuda" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PATH=/usr/local/cuda/bin:/usr/local/bin:\$PATH" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:\${LD_LIBRARY_PATH:-}" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export XDG_CACHE_HOME=/home/seigyo/.cache" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export HF_HOME=/home/seigyo/.cache/huggingface" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export HUGGINGFACE_HUB_CACHE=/home/seigyo/.cache/huggingface/hub" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export MINERU_MODEL_SOURCE='${MINERU_MODEL_SOURCE}'" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export CHROME_BIN=/usr/local/bin/chrome-headless-shell" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_SKIP_DOWNLOAD=true" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export GPU_MEMORY_UTILIZATION='${GPU_MEMORY_UTILIZATION}'" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "which nvcc && nvcc --version || true" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "python3 -m uvicorn server:app --host 0.0.0.0 --port 8000" C-m
fi

sleep 2

echo
echo "tmux sessions:"
runuser -u seigyo -- tmux ls || true

echo
echo "Processes:"
ps -ef | grep -E "sshd|tmux|uvicorn" | grep -v grep || true

echo
echo "Listening ports:"
ss -lntp || true

echo
echo "GPU status:"
nvidia-smi || true

echo
echo "GPU_MEMORY_UTILIZATION:"
echo "${GPU_MEMORY_UTILIZATION:-unset}"

echo
echo "CUDA:"
echo "  CUDA_HOME=${CUDA_HOME}"
command -v nvcc || true
nvcc --version || true

echo
echo "Mermaid CLI:"
mmdc --version || true

echo
echo "mmdc path:"
command -v mmdc || true

echo
echo "Chrome Headless Shell:"
chrome-headless-shell --version || true

echo
echo "============================================================"
echo "Parser container ready"
echo
echo "Parser API:"
echo "  http://<HOST>:8000"
echo
echo "SSH:"
echo "  ssh -p 2222 seigyo@<HOST>"
echo
echo "Attach parser:"
echo "  tmux attach -t parser"
echo
echo "Mermaid CLI:"
echo "  mmdc -i diagram.mmd -o diagram.svg"
echo
echo "Password:"
echo "  seigyo@rikiseisan"
echo "============================================================"

tail -f /dev/null
EOF

RUN chmod +x /usr/local/bin/start-parser.sh

EXPOSE 22 8000

CMD ["/usr/local/bin/start-parser.sh"]
