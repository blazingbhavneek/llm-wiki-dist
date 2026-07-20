# syntax=docker/dockerfile:1.7

# CUDA 13 base with torch and vLLM preinstalled. The host only needs an NVIDIA
# driver new enough for CUDA 13 plus nvidia-container-toolkit.
FROM vllm/vllm-openai:v0.21.0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
USER root

# Build-network default for the work network. Outside it, use:
#   docker build --build-arg PROXY_URL= ...
ARG PROXY_URL="http://133.141.7.237:9515"
ARG NO_PROXY_VALUE="localhost,127.0.0.1,::1"
ENV http_proxy=${PROXY_URL} \
    https_proxy=${PROXY_URL} \
    HTTP_PROXY=${PROXY_URL} \
    HTTPS_PROXY=${PROXY_URL} \
    all_proxy=${PROXY_URL} \
    ALL_PROXY=${PROXY_URL} \
    no_proxy=${NO_PROXY_VALUE} \
    NO_PROXY=${NO_PROXY_VALUE}

ENV DEBIAN_FRONTEND=noninteractive \
    APP_HOME=/home/seigyo/parser \
    HOME=/home/seigyo \
    PYTHONUNBUFFERED=1 \
    MINERU_MODEL_SOURCE=local \
    MINERU_TOOLS_CONFIG_JSON=/opt/mineru/mineru.json \
    HF_HOME=/home/seigyo/.cache/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    CHROME_BIN=/usr/local/bin/chrome-headless-shell \
    PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell \
    PUPPETEER_SKIP_DOWNLOAD=true \
    GPU_TARGET_ALLOC_MB=7168 \
    PARSER_REQUIRE_GPU=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

# Fonts (CJK needed for wiki content) plus headless-chrome runtime libraries.
RUN --mount=type=cache,id=apt-cache,target=/var/cache/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl unzip passwd openssh-server util-linux \
      fontconfig fonts-liberation fonts-noto-cjk fonts-noto-color-emoji fonts-noto-core \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 libdbus-1-3 \
      libdrm2 libexpat1 libgbm1 libgl1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
      libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
      libxfixes3 libxkbcommon0 libxrandr2 && \
    fc-cache -f && \
    rm -rf /var/lib/apt/lists/*

# Node, for mermaid-cli and the frontend build.
RUN curl -fsSL https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-x64.tar.xz \
      | tar -xJ -C /usr/local --strip-components=1

# Headless Chrome for mermaid rendering.
RUN curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/150.0.7871.115/linux64/chrome-headless-shell-linux64.zip" \
      -o /tmp/chrome.zip && \
    unzip -q /tmp/chrome.zip -d /opt && \
    ln -s /opt/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chrome-headless-shell && \
    rm /tmp/chrome.zip

RUN groupadd --gid 1000 seigyo && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash seigyo && \
    echo 'seigyo:seigyo@rikiseisan' | chpasswd && \
    install -d -m 0755 -o seigyo -g seigyo \
      "${APP_HOME}" "${APP_HOME}/cache" "${APP_HOME}/temp" \
      /home/seigyo/.cache \
      /home/seigyo/.cache/flashinfer \
      /home/seigyo/.cache/huggingface \
      /home/seigyo/.cache/vllm && \
    install -d -m 0755 -o root -g root /run/sshd /etc/ssh/sshd_config.d && \
    printf '%s\n' \
      'PasswordAuthentication yes' \
      'PermitRootLogin no' \
      > /etc/ssh/sshd_config.d/parser.conf && \
    rm -f /etc/ssh/ssh_host_*

# lmcache's transformers>=5.4 requirement conflicts with MinerU's
# transformers<5. PyGObject is broken-but-unused in the upstream base.
# mineru stays pinned: model_bundle.py's expected model layout tracks it.
RUN --mount=type=cache,id=pip-cache,target=/root/.cache/pip,sharing=locked \
    python3 -m pip uninstall -y lmcache PyGObject && \
    python3 -m pip install --break-system-packages \
      "mineru[core]==3.4.4" \
      requests \
      python-dotenv \
      langchain-openai \
      pydantic \
      fastapi \
      "uvicorn[standard]" \
      python-multipart

# Bake the MinerU models so the container runs offline.
COPY parser/model_bundle.py /usr/local/lib/model_bundle.py
RUN --mount=type=cache,id=hf-model-cache,target=/tmp/hf,sharing=locked \
    HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 HF_HOME=/tmp/hf \
      python3 /usr/local/lib/model_bundle.py download && \
    chmod -R a=rX /opt/mineru/models

RUN mkdir -p /opt/mermaid-cli && \
    cd /opt/mermaid-cli && \
    npm install --omit=dev @mermaid-js/mermaid-cli && \
    npm cache clean --force

# Wrapper injects the sandboxless puppeteer config unless the caller passes one.
COPY <<'EOF' /usr/local/bin/mmdc
#!/usr/bin/env bash
set -Eeuo pipefail

for argument in "$@"; do
  case "${argument}" in
    -p|--puppeteerConfig|--puppeteer-config|--puppeteerConfigFile|--puppeteer-config-file|\
    -p=*|--puppeteerConfig=*|--puppeteer-config=*|--puppeteerConfigFile=*|--puppeteer-config-file=*)
      exec /opt/mermaid-cli/node_modules/.bin/mmdc "$@"
      ;;
  esac
done

exec /opt/mermaid-cli/node_modules/.bin/mmdc \
  -p /home/seigyo/parser/puppeteer-config.json "$@"
EOF

COPY --chown=seigyo:seigyo <<'EOF' ${APP_HOME}/puppeteer-config.json
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

COPY --chown=seigyo:seigyo parser/ ${APP_HOME}/
RUN cd "${APP_HOME}/frontend" && \
    npm ci && \
    npm run build && \
    rm -rf node_modules /root/.npm && \
    chown -R seigyo:seigyo "${APP_HOME}/frontend"


COPY <<'EOF' /usr/local/bin/start-parser
#!/usr/bin/env bash
set -Eeuo pipefail

export MINERU_BACKEND="${MINERU_BACKEND:-hybrid-engine}"
export MINERU_METHOD="${MINERU_METHOD:-auto}"
export MINERU_EFFORT="${MINERU_EFFORT:-medium}"

python3 /usr/local/lib/model_bundle.py verify

mkdir -p /run/sshd
chmod 0755 /run/sshd
ssh-keygen -A
/usr/sbin/sshd

if [[ "${PARSER_REQUIRE_GPU:-1}" == "1" ]]; then
  if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null; then
    echo "ERROR: NVIDIA runtime is unavailable. Start with: docker run --gpus all ..." >&2
    exit 1
  fi
fi

echo "Starting parser: backend=${MINERU_BACKEND}; method=${MINERU_METHOD}; effort=${MINERU_EFFORT}"
exec runuser -u seigyo -- python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
EOF

# PROXY_URL is the single source of truth at runtime. An explicitly empty
# value (docker run -e PROXY_URL=) disables every proxy variable.
COPY <<'EOF' /usr/local/bin/container-entrypoint
#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -n "${PROXY_URL:-}" ]]; then
  export http_proxy="${PROXY_URL}"
  export https_proxy="${PROXY_URL}"
  export HTTP_PROXY="${PROXY_URL}"
  export HTTPS_PROXY="${PROXY_URL}"
  export all_proxy="${PROXY_URL}"
  export ALL_PROXY="${PROXY_URL}"
else
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
fi

export no_proxy="${NO_PROXY_VALUE:-localhost,127.0.0.1,::1}"
export NO_PROXY="${no_proxy}"

exec "$@"
EOF

RUN chmod 0555 /usr/local/bin/start-parser /usr/local/bin/container-entrypoint /usr/local/bin/mmdc

# Keep the work proxy as the runtime default even when the build proxy was
# disabled with --build-arg PROXY_URL=.
ENV PROXY_URL="http://133.141.7.237:9515" \
    NO_PROXY_VALUE="localhost,127.0.0.1,::1" \
    GPU_MEMORY_UTILIZATION="0.05"

WORKDIR ${APP_HOME}
USER root

EXPOSE 22 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/queue", timeout=3).read()' || exit 1

ENTRYPOINT ["/usr/local/bin/container-entrypoint"]
CMD ["/usr/local/bin/start-parser"]
