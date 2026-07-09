# syntax=docker/dockerfile:1

FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

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

# ------------------------------------------------------------
# CUDA toolkit environment
#
# cuda-toolkit install creates /usr/local/cuda-13.3 in your tested setup.
# Dockerfile also creates /usr/local/cuda symlink.
# ------------------------------------------------------------
ARG CUDA_TOOLKIT_VERSION="13.3"

ENV CUDA_HOME=/usr/local/cuda
ENV PATH="${APP_HOME}/.venv/bin:/usr/local/cuda/bin:/usr/local/bin:${PATH}"
ENV LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64"

ENV CHROME_BIN=/usr/local/bin/chrome-headless-shell
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

ENV UV_CACHE_DIR=/root/.cache/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python

ENV PYTHONDONTWRITEBYTECODE=1

# ------------------------------------------------------------
# Simplified HuggingFace model cache location.
# Host cache is copied into this same path at build time.
# ------------------------------------------------------------
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
    printf '%s\n' \
      "Acquire::http::Proxy \"${PROXY_URL}\";" \
      "Acquire::https::Proxy \"${PROXY_URL}\";" \
      "Acquire::ftp::Proxy \"${PROXY_URL}\";" \
      > /etc/apt/apt.conf.d/95proxies

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
RUN --mount=type=cache,id=apt-lists-parser,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache-parser,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
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
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-noto-cjk \
    libasound2t64 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
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
    xdg-utils

# ------------------------------------------------------------
# Install CUDA Toolkit / nvcc
#
# This integrates your working commands:
#   sudo apt update
#   sudo apt-get install -y cuda-toolkit
#   sudo ln -sfn /usr/local/cuda-13.3 /usr/local/cuda
#
# Also makes CUDA env available to non-interactive processes,
# not only bash shells.
# ------------------------------------------------------------
RUN --mount=type=cache,id=apt-lists-parser,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache-parser,target=/var/cache/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      cuda-toolkit; \
    if [ -d "/usr/local/cuda-${CUDA_TOOLKIT_VERSION}" ]; then \
      CUDA_DIR="/usr/local/cuda-${CUDA_TOOLKIT_VERSION}"; \
    else \
      CUDA_DIR="$(find /usr/local -maxdepth 1 -type d -name 'cuda-[0-9]*' | sort -V | tail -n 1)"; \
    fi; \
    test -n "${CUDA_DIR}"; \
    test -d "${CUDA_DIR}"; \
    ln -sfn "${CUDA_DIR}" /usr/local/cuda; \
    test -x /usr/local/cuda/bin/nvcc; \
    printf '%s\n' \
      'export CUDA_HOME=/usr/local/cuda' \
      'export PATH=/usr/local/cuda/bin:$PATH' \
      'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:$LD_LIBRARY_PATH' \
      > /etc/profile.d/cuda.sh; \
    chmod 0644 /etc/profile.d/cuda.sh; \
    /usr/local/cuda/bin/nvcc --version; \
    ls -lah /usr/local/cuda/bin/nvcc

# ------------------------------------------------------------
# wget proxy
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      "use_proxy = on" \
      "http_proxy = ${PROXY_URL}" \
      "https_proxy = ${PROXY_URL}" \
      > /etc/wgetrc

# ------------------------------------------------------------
# git proxy
# ------------------------------------------------------------
RUN set -eux; \
    git config --system http.proxy "${PROXY_URL}"; \
    git config --system https.proxy "${PROXY_URL}"

# ------------------------------------------------------------
# Install Node.js
# ------------------------------------------------------------
RUN --mount=type=cache,id=apt-lists-parser,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache-parser,target=/var/cache/apt,sharing=locked \
    set -eux; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      nodejs; \
    node --version; \
    npm --version

# ------------------------------------------------------------
# npm proxy
# ------------------------------------------------------------
RUN set -eux; \
    npm config set proxy "${PROXY_URL}" --global; \
    npm config set https-proxy "${PROXY_URL}" --global; \
    npm config set fetch-retries 5 --global; \
    npm config set fetch-retry-mintimeout 20000 --global; \
    npm config set fetch-retry-maxtimeout 120000 --global

# ------------------------------------------------------------
# Install uv
# ------------------------------------------------------------
RUN --mount=type=cache,id=curl-cache-root-parser,target=/root/.cache \
    set -eux; \
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh; \
    uv --version

# ------------------------------------------------------------
# Create user and configure SSH
# ------------------------------------------------------------
RUN useradd -m -s /bin/bash seigyo \
    && echo "seigyo:seigyo@rikiseisan" | chpasswd \
    && usermod -aG sudo seigyo \
    && mkdir -p /run/sshd \
    && mkdir -p /home/seigyo/.ssh \
    && chmod 700 /home/seigyo/.ssh \
    && chown -R seigyo:seigyo /home/seigyo/.ssh \
    && sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && ssh-keygen -A

# ------------------------------------------------------------
# Persist CUDA env for seigyo login shells too
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      '' \
      '# CUDA toolkit' \
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
#     -f parser/Dockerfile \
#     -t doc-parser-rikiseisan \
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
RUN --mount=type=cache,id=chrome-download-cache-parser,target=/root/.cache/chrome-download \
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
RUN --mount=type=cache,id=npm-cache-root-parser,target=/root/.npm \
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
# Copy requirements.txt first for Docker layer cache
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo requirements.txt ${APP_HOME}/requirements.txt

# ------------------------------------------------------------
# Backend dependency setup
# ------------------------------------------------------------
USER root

WORKDIR ${APP_HOME}

RUN --mount=type=cache,id=uv-cache-root-parser,target=/root/.cache/uv \
    set -eux; \
    mkdir -p /opt/uv/python /root/.cache/uv; \
    test -f requirements.txt; \
    uv python install 3.13; \
    uv venv .venv --python 3.13; \
    uv pip install --python .venv/bin/python -r requirements.txt --no-deps; \
    chown -R seigyo:seigyo ${APP_HOME}/.venv ${APP_HOME}/requirements.txt; \
    chmod -R a+rX /opt/uv/python

# ------------------------------------------------------------
# Copy parser backend source files
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo parser/*.py ${APP_HOME}/
COPY --chown=seigyo:seigyo parser/test_utils/ ${APP_HOME}/test_utils/

# ------------------------------------------------------------
# Validate test PDF exists at build time.
# No MinerU execution happens during docker build.
# ------------------------------------------------------------
RUN set -eux; \
    test -f "${APP_HOME}/test_utils/test.pdf"; \
    chown -R seigyo:seigyo "${APP_HOME}/test_utils"

# ------------------------------------------------------------
# Copy parser frontend package files first for Docker layer cache
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo parser/frontend/package*.json ${APP_HOME}/frontend/

# ------------------------------------------------------------
# Install parser frontend dependencies
# ------------------------------------------------------------
USER root

WORKDIR ${APP_HOME}/frontend

RUN --mount=type=cache,id=npm-cache-root-parser-frontend,target=/root/.npm \
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
# Build parser frontend
# ------------------------------------------------------------
RUN --mount=type=cache,id=npm-cache-root-parser-frontend,target=/root/.npm \
    set -eux; \
    npm run build; \
    if [ -d dist ]; then chown -R seigyo:seigyo dist; fi; \
    if [ -d build ]; then chown -R seigyo:seigyo build; fi; \
    if [ -d .next ]; then chown -R seigyo:seigyo .next; fi

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
USER root

WORKDIR ${APP_HOME}

RUN cat > /usr/local/bin/start-parser.sh <<'EOF'
#!/usr/bin/env bash
set -u

APP_HOME="/home/seigyo/parser"
SESSION_NAME="parser"
TARGET_MB="${GPU_TARGET_ALLOC_MB:-7168}"

export CUDA_HOME="/usr/local/cuda"
export PATH="/usr/local/cuda/bin:${PATH}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:${LD_LIBRARY_PATH:-}"

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
  echo "ERROR: nvcc not found."
  echo "ERROR: Expected /usr/local/cuda/bin/nvcc"
  ls -lah /usr/local/cuda /usr/local/cuda/bin 2>/dev/null || true
  exit 1
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
echo "Starting parser tmux session..."

if runuser -u seigyo -- tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session already exists: ${SESSION_NAME}"
else
  runuser -u seigyo -- tmux new-session -d -s "${SESSION_NAME}" -c "${APP_HOME}" bash

  sleep 1

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "cd ${APP_HOME}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "source .venv/bin/activate" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export HOME=/home/seigyo" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export CUDA_HOME=/usr/local/cuda" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PATH=/usr/local/cuda/bin:\$PATH" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:\${LD_LIBRARY_PATH:-}" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export XDG_CACHE_HOME=/home/seigyo/.cache" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export HF_HOME=/home/seigyo/.cache/huggingface" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export HUGGINGFACE_HUB_CACHE=/home/seigyo/.cache/huggingface/hub" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export CHROME_BIN=/usr/local/bin/chrome-headless-shell" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_SKIP_DOWNLOAD=true" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export GPU_MEMORY_UTILIZATION='${GPU_MEMORY_UTILIZATION}'" C-m

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "which nvcc && nvcc --version" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "python -m uvicorn server:app --host 0.0.0.0 --port 8000" C-m
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
