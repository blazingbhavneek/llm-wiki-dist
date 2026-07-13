# syntax=docker/dockerfile:1.7

# Runtime portability comes from the NVIDIA driver ABI: the host needs a driver
# new enough for CUDA 13 plus nvidia-container-toolkit, not a matching host CUDA
# toolkit. This immutable base contains CUDA 13.0, torch 2.11.0 and vLLM 0.21.0.
ARG VLLM_BASE_IMAGE="vllm/vllm-openai@sha256:a230095847e93bd4df9888b33dab956fa9504537b828a23657d2b26fed57b5c9"
FROM ${VLLM_BASE_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
USER root

ARG MINERU_VERSION="3.4.4"
ARG MINERU_PIPELINE_REVISION="ed6b654c018d742e65a17671e379c5e6ecc87ec9"
ARG MINERU_VLM_REVISION="bff20d4ae2bf202df9f45284b4d43681555a97ed"
ARG NODE_VERSION="22.23.1"
ARG NODE_SHA256="9749e988f437343b7fa832c69ded82a312e41a03116d766797ac14f6f9eee578"
ARG CHROME_VERSION="150.0.7871.115"
ARG CHROME_SHA256="13ce11d150ab12f1a451f8555cc373eefe16476691eb99c21b4a6ed363c00dd8"
ARG APP_UID="1000"
ARG APP_GID="1000"
ARG TARGETARCH
ARG PROXY_URL="http://133.141.7.237:9515"
ARG NO_PROXY_VALUE="localhost,127.0.0.1,::1"

# Build-network default for the work network. Outside it, use:
#   docker build --build-arg PROXY_URL= ...
ENV http_proxy=${PROXY_URL} \
    https_proxy=${PROXY_URL} \
    HTTP_PROXY=${PROXY_URL} \
    HTTPS_PROXY=${PROXY_URL} \
    all_proxy=${PROXY_URL} \
    ALL_PROXY=${PROXY_URL} \
    no_proxy=${NO_PROXY_VALUE} \
    NO_PROXY=${NO_PROXY_VALUE}

LABEL org.opencontainers.image.title="LLM Wiki deterministic GPU document parser" \
      org.opencontainers.image.version="${MINERU_VERSION}" \
      io.llm-wiki.cuda="13.0" \
      io.llm-wiki.vllm="0.21.0" \
      io.llm-wiki.node="${NODE_VERSION}" \
      io.llm-wiki.chrome="${CHROME_VERSION}"

ENV DEBIAN_FRONTEND=noninteractive \
    APP_HOME=/home/seigyo/parser \
    HOME=/home/seigyo \
    CUDA_HOME=/usr/local/cuda \
    PATH=/home/seigyo/.local/bin:/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MINERU_VERSION=${MINERU_VERSION} \
    MINERU_MODEL_SOURCE=local \
    MINERU_TOOLS_CONFIG_JSON=/opt/mineru/mineru.json \
    MINERU_PIPELINE_REVISION=${MINERU_PIPELINE_REVISION} \
    MINERU_VLM_REVISION=${MINERU_VLM_REVISION} \
    HF_HOME=/home/seigyo/.cache/huggingface \
    HUGGINGFACE_HUB_CACHE=/home/seigyo/.cache/huggingface/hub \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    CHROME_BIN=/usr/local/bin/chrome-headless-shell \
    PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    GPU_TARGET_ALLOC_MB=7168 \
    PARSER_REQUIRE_GPU=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

# Chrome for Testing only publishes linux64 for this target. Fail explicitly
# instead of accidentally producing an amd64 image under another architecture.
RUN set -eux; test "${TARGETARCH:-amd64}" = "amd64"

COPY parser/ubuntu-snapshot.sources.list /etc/apt/sources.list
RUN --mount=type=cache,id=apt-lists-parser-344-snapshot-20260713,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache-parser-344,target=/var/cache/apt,sharing=locked \
    set -eux; \
    rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      unzip \
      fontconfig \
      fonts-liberation \
      fonts-noto-cjk \
      fonts-noto-color-emoji \
      fonts-noto-core \
      passwd \
      libasound2 \
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
      libxrandr2; \
    fc-cache -f

RUN --mount=type=cache,id=node-parser-22231,target=/root/.cache/node \
    set -eux; \
    archive="/root/.cache/node/node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    if [[ ! -f "${archive}" ]] || ! printf '%s  %s\n' "${NODE_SHA256}" "${archive}" | sha256sum -c -; then \
      curl -fsSL --retry 5 \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        -o "${archive}"; \
    fi; \
    printf '%s  %s\n' "${NODE_SHA256}" "${archive}" | sha256sum -c -; \
    tar -xJf "${archive}" -C /usr/local --strip-components=1; \
    node --version; \
    npm --version

RUN --mount=type=cache,id=chrome-parser-1500787115,target=/root/.cache/chrome \
    set -eux; \
    archive="/root/.cache/chrome/chrome-headless-shell-linux64-${CHROME_VERSION}.zip"; \
    if [[ ! -f "${archive}" ]] || ! printf '%s  %s\n' "${CHROME_SHA256}" "${archive}" | sha256sum -c -; then \
      curl -fsSL --retry 5 \
        "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-headless-shell-linux64.zip" \
        -o "${archive}"; \
    fi; \
    printf '%s  %s\n' "${CHROME_SHA256}" "${archive}" | sha256sum -c -; \
    unzip -q "${archive}" -d /opt; \
    ln -s /opt/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chrome-headless-shell; \
    chrome-headless-shell --version

RUN set -eux; \
    /usr/sbin/groupadd --gid "${APP_GID}" seigyo; \
    /usr/sbin/useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /bin/bash seigyo; \
    install -d -o seigyo -g seigyo \
      "${APP_HOME}" \
      "${APP_HOME}/cache" \
      "${APP_HOME}/temp" \
      /home/seigyo/.cache \
      /home/seigyo/.cache/huggingface \
      /home/seigyo/.cache/huggingface/hub

COPY parser/requirements.lock /opt/mineru/requirements.lock
# LMCache is an optional vLLM plugin, not a vLLM dependency. Its
# transformers>=5.4 requirement conflicts with MinerU's transformers<5.
# PyGObject is also broken-but-unused in the upstream base (missing pycairo).
RUN --mount=type=cache,id=pip-parser-344,target=/root/.cache/pip \
    set -eux; \
    export PIP_CACHE_DIR=/root/.cache/pip; \
    python3 -m pip uninstall -y lmcache PyGObject; \
    python3 -m pip install --break-system-packages -r /opt/mineru/requirements.lock; \
    python3 -m pip check; \
    python3 -c 'from importlib.metadata import version; assert version("mineru") == "3.4.4"; assert version("vllm") == "0.21.0"; assert version("torch") == "2.11.0+cu130"'

COPY parser/model_bundle.py /usr/local/lib/model_bundle.py
RUN --mount=type=cache,id=hf-parser-344,target=/tmp/huggingface,sharing=locked \
    set -eux; \
    rm -rf /tmp/huggingface/xet; \
    HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 HF_HUB_DISABLE_XET=1 \
      HF_HUB_DOWNLOAD_TIMEOUT=120 HF_HUB_ETAG_TIMEOUT=60 HF_HOME=/tmp/huggingface \
      python3 /usr/local/lib/model_bundle.py download \
        --pipeline-revision "${MINERU_PIPELINE_REVISION}" \
        --vlm-revision "${MINERU_VLM_REVISION}"; \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
      python3 /usr/local/lib/model_bundle.py verify --hashes \
        --pipeline-revision "${MINERU_PIPELINE_REVISION}" \
        --vlm-revision "${MINERU_VLM_REVISION}"; \
    chmod -R a=rX /opt/mineru/models; \
    chmod 0444 /opt/mineru/mineru.json /opt/mineru/model-manifest.json

COPY parser/mermaid-cli/package.json parser/mermaid-cli/package-lock.json /opt/mermaid-cli/
RUN --mount=type=cache,id=npm-mermaid-11160,target=/root/.npm \
    set -eux; \
    export npm_config_cache=/root/.npm; \
    cd /opt/mermaid-cli; \
    npm ci --omit=dev --ignore-scripts; \
    npm cache verify

COPY parser/mmdc-wrapper.sh /usr/local/bin/mmdc
COPY --chown=seigyo:seigyo parser/puppeteer-config.json ${APP_HOME}/puppeteer-config.json
RUN set -eux; chmod 0555 /usr/local/bin/mmdc; mmdc --version

COPY --chown=seigyo:seigyo parser/frontend/package.json parser/frontend/package-lock.json ${APP_HOME}/frontend/
RUN --mount=type=cache,id=npm-parser-frontend,target=/root/.npm \
    set -eux; \
    export npm_config_cache=/root/.npm; \
    cd "${APP_HOME}/frontend"; \
    npm ci; \
    chown -R seigyo:seigyo "${APP_HOME}/frontend"

COPY --chown=seigyo:seigyo parser/frontend/ ${APP_HOME}/frontend/
RUN --mount=type=cache,id=npm-parser-frontend,target=/root/.npm \
    set -eux; \
    export npm_config_cache=/root/.npm; \
    cd "${APP_HOME}/frontend"; \
    npm run build; \
    rm -rf node_modules; \
    chown -R seigyo:seigyo "${APP_HOME}/frontend"

COPY --chown=seigyo:seigyo parser/*.py ${APP_HOME}/
COPY --chown=seigyo:seigyo parser/test_utils/ ${APP_HOME}/test_utils/
COPY parser/start-parser.sh /usr/local/bin/start-parser
COPY parser/container-entrypoint.sh /usr/local/bin/container-entrypoint
RUN set -eux; \
    chmod 0555 /usr/local/bin/start-parser /usr/local/bin/container-entrypoint /usr/local/lib/model_bundle.py; \
    bash -n /usr/local/bin/start-parser /usr/local/bin/container-entrypoint /usr/local/bin/mmdc; \
    python3 -m compileall -q "${APP_HOME}" /usr/local/lib/model_bundle.py; \
    printf '%s\n' 'graph TD' '  A[offline] --> B[deterministic]' >/tmp/mermaid-smoke.mmd; \
    mmdc -i /tmp/mermaid-smoke.mmd -o /tmp/mermaid-smoke.svg; \
    test -s /tmp/mermaid-smoke.svg; \
    rm -f /tmp/mermaid-smoke.mmd /tmp/mermaid-smoke.svg

# Keep the work proxy as the runtime default even when the build proxy was
# disabled with --build-arg PROXY_URL=. Set -e PROXY_URL= to disable at runtime.
ENV PROXY_URL="http://133.141.7.237:9515" \
    MINERU_BACKEND="hybrid-engine" \
    MINERU_METHOD="auto" \
    MINERU_EFFORT="medium" \
    NO_PROXY_VALUE="localhost,127.0.0.1,::1" \
    http_proxy="http://133.141.7.237:9515" \
    https_proxy="http://133.141.7.237:9515" \
    HTTP_PROXY="http://133.141.7.237:9515" \
    HTTPS_PROXY="http://133.141.7.237:9515" \
    all_proxy="http://133.141.7.237:9515" \
    ALL_PROXY="http://133.141.7.237:9515" \
    no_proxy="localhost,127.0.0.1,::1" \
    NO_PROXY="localhost,127.0.0.1,::1"

WORKDIR ${APP_HOME}
USER seigyo

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/queue", timeout=3).read()' || exit 1

ENTRYPOINT ["/usr/local/bin/container-entrypoint"]
CMD ["/usr/local/bin/start-parser"]
