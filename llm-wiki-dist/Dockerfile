# syntax=docker/dockerfile:1

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TERM=xterm-256color

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
# App environment
# ------------------------------------------------------------
ENV APP_HOME=/home/seigyo/llm-wiki
ENV PATH="${APP_HOME}/.venv/bin:/usr/local/bin:${PATH}"

# llm-wiki runtime config — override at `docker run -e ...`
#   WIKI_DB_DIR     dir holding <db>.sqlite files (relative to APP_HOME)
#   WIKI_DEFAULT_DB db that bare /<prefix>/ redirects to
#   WIKI_PREFIX     reverse-proxy path prefix ("" = none)
ENV WIKI_DB_DIR=.wiki
ENV WIKI_DEFAULT_DB=wiki
ENV WIKI_PREFIX=/llm-wiki

ENV CHROME_BIN=/usr/local/bin/chrome-headless-shell
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
ENV UV_CACHE_DIR=/root/.cache/uv

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
# Enable APT cache
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
RUN --mount=type=cache,id=apt-lists,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache,target=/var/cache/apt,sharing=locked \
    set -eux; \
    apt-get update; \
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
# wget / git proxy
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      "use_proxy = on" \
      "http_proxy = ${PROXY_URL}" \
      "https_proxy = ${PROXY_URL}" \
      > /etc/wgetrc; \
    git config --system http.proxy "${PROXY_URL}"; \
    git config --system https.proxy "${PROXY_URL}"

# ------------------------------------------------------------
# Install Node.js
# ------------------------------------------------------------
RUN --mount=type=cache,id=apt-lists,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=apt-cache,target=/var/cache/apt,sharing=locked \
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
# npm proxy + retry settings
# ------------------------------------------------------------
RUN set -eux; \
    npm config set proxy "${PROXY_URL}" --global; \
    npm config set https-proxy "${PROXY_URL}" --global; \
    npm config set registry "https://registry.npmjs.org/" --global; \
    npm config set fetch-retries 10 --global; \
    npm config set fetch-retry-mintimeout 30000 --global; \
    npm config set fetch-retry-maxtimeout 300000 --global; \
    npm config set fetch-timeout 300000 --global

# ------------------------------------------------------------
# Install uv
# ------------------------------------------------------------
RUN --mount=type=cache,id=curl-cache-root,target=/root/.cache \
    set -eux; \
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh; \
    uv --version

# ------------------------------------------------------------
# Create user and SSH
# ------------------------------------------------------------
RUN set -eux; \
    useradd -m -s /bin/bash seigyo; \
    echo "seigyo:seigyo@rikiseisan" | chpasswd; \
    usermod -aG sudo seigyo; \
    mkdir -p /run/sshd /home/seigyo/.ssh; \
    chmod 700 /home/seigyo/.ssh; \
    chown -R seigyo:seigyo /home/seigyo/.ssh; \
    sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config; \
    sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config; \
    sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config; \
    ssh-keygen -A

# ------------------------------------------------------------
# App directory
# ------------------------------------------------------------
RUN set -eux; \
    mkdir -p ${APP_HOME}; \
    chown -R seigyo:seigyo /home/seigyo

WORKDIR ${APP_HOME}

# ------------------------------------------------------------
# Chrome Headless Shell
# ------------------------------------------------------------
RUN --mount=type=cache,id=chrome-download-cache,target=/root/.cache/chrome-download \
    set -eux; \
    CHROME_JSON="$(curl -fsSL https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json)"; \
    CHROME_VERSION="$(printf '%s' "${CHROME_JSON}" | jq -r '.channels.Stable.version')"; \
    CHROME_URL="$(printf '%s' "${CHROME_JSON}" | jq -r '.channels.Stable.downloads["chrome-headless-shell"][] | select(.platform=="linux64") | .url')"; \
    ZIP="/root/.cache/chrome-download/chrome-headless-shell-linux64-${CHROME_VERSION}.zip"; \
    echo "Chrome version: ${CHROME_VERSION}"; \
    echo "Chrome URL: ${CHROME_URL}"; \
    if [ ! -f "${ZIP}" ]; then \
      curl -fsSL "${CHROME_URL}" -o "${ZIP}"; \
    fi; \
    rm -rf /opt/chrome-headless-shell-linux64; \
    unzip -q "${ZIP}" -d /opt; \
    ln -sf /opt/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chrome-headless-shell; \
    chmod +x /opt/chrome-headless-shell-linux64/chrome-headless-shell; \
    chrome-headless-shell --version

# ------------------------------------------------------------
# Puppeteer config
# Important: force correct executablePath
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo puppeteer-config.json ${APP_HOME}/puppeteer-config.json

RUN set -eux; \
    cat > ${APP_HOME}/puppeteer-config.json <<'EOF'
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
RUN chown seigyo:seigyo ${APP_HOME}/puppeteer-config.json; \
    test -x /usr/local/bin/chrome-headless-shell; \
    /usr/local/bin/chrome-headless-shell --version

# ------------------------------------------------------------
# Install Mermaid CLI / mmdc with retry
# ------------------------------------------------------------
RUN --mount=type=cache,id=npm-cache-root-mermaid-v3,target=/root/.npm \
    set -eux; \
    npm config set proxy "${PROXY_URL}" --global; \
    npm config set https-proxy "${PROXY_URL}" --global; \
    npm config set registry "https://registry.npmjs.org/" --global; \
    npm config set fetch-retries 10 --global; \
    npm config set fetch-retry-mintimeout 30000 --global; \
    npm config set fetch-retry-maxtimeout 300000 --global; \
    npm config set fetch-timeout 300000 --global; \
    for i in 1 2 3 4 5; do \
      npm install -g --no-audit --no-fund @mermaid-js/mermaid-cli && break; \
      echo "npm install failed, retry ${i}/5"; \
      if [ "$i" = "5" ]; then exit 1; fi; \
      sleep 20; \
    done; \
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
      'PUPPETEER_CONFIG="/home/seigyo/llm-wiki/puppeteer-config.json"' \
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
    mmdc --version

# ------------------------------------------------------------
# Mermaid smoke test
# ------------------------------------------------------------
RUN set -eux; \
    printf '%s\n' \
      'graph TD' \
      '  A[Start] --> B{Works?}' \
      '  B -->|Yes| C[Success]' \
      '  B -->|No| D[Failure]' \
      > /tmp/test.mmd; \
    mmdc -i /tmp/test.mmd -o /tmp/test.svg; \
    test -s /tmp/test.svg; \
    rm -f /tmp/test.mmd /tmp/test.svg

# ------------------------------------------------------------
# Copy Python project files first for uv cache
# ------------------------------------------------------------
COPY --chown=seigyo:seigyo pyproject.toml ${APP_HOME}/pyproject.toml
COPY --chown=seigyo:seigyo uv.lock ${APP_HOME}/uv.lock
COPY --chown=seigyo:seigyo README.txt ${APP_HOME}/README.txt

# ------------------------------------------------------------
# Install Python dependencies
# ------------------------------------------------------------
RUN --mount=type=cache,id=uv-cache-root,target=/root/.cache/uv \
    set -eux; \
    mkdir -p /opt/uv/python /root/.cache/uv; \
    uv python install 3.13; \
    uv sync --locked --no-install-project; \
    mkdir -p ${APP_HOME}/.wiki /home/seigyo/.cache/uv; \
    chown -R seigyo:seigyo ${APP_HOME} /home/seigyo/.cache; \
    chmod -R a+rX /opt/uv/python

# ------------------------------------------------------------
# Frontend dependency cache
# ------------------------------------------------------------
RUN mkdir -p ${APP_HOME}/frontend

COPY --chown=seigyo:seigyo frontend/package*.json ${APP_HOME}/frontend/

WORKDIR ${APP_HOME}/frontend

RUN --mount=type=cache,id=npm-cache-root-frontend,target=/root/.npm \
    set -eux; \
    if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund; \
    else \
      npm install --no-audit --no-fund; \
    fi; \
    chown -R seigyo:seigyo ${APP_HOME}/frontend

# ------------------------------------------------------------
# Copy app source
# ------------------------------------------------------------
WORKDIR ${APP_HOME}

COPY --chown=seigyo:seigyo app.py ${APP_HOME}/app.py
COPY --chown=seigyo:seigyo mcp_server.py ${APP_HOME}/mcp_server.py
COPY --chown=seigyo:seigyo main.py ${APP_HOME}/main.py
COPY --chown=seigyo:seigyo graph/ ${APP_HOME}/graph/
COPY --chown=seigyo:seigyo frontend/ ${APP_HOME}/frontend/

# ------------------------------------------------------------
# Final uv sync after source copy
# ------------------------------------------------------------
RUN --mount=type=cache,id=uv-cache-root,target=/root/.cache/uv \
    set -eux; \
    uv sync --locked; \
    mkdir -p ${APP_HOME}/.wiki /home/seigyo/.cache/uv; \
    chown -R seigyo:seigyo ${APP_HOME}/.wiki /home/seigyo/.cache; \
    chmod -R a+rX /opt/uv/python

# ------------------------------------------------------------
# Frontend build
# ------------------------------------------------------------
WORKDIR ${APP_HOME}/frontend

RUN --mount=type=cache,id=npm-cache-root-frontend,target=/root/.npm \
    set -eux; \
    npm run build; \
    chown -R seigyo:seigyo ${APP_HOME}/frontend

# ------------------------------------------------------------
# Startup script
# ------------------------------------------------------------
USER root

WORKDIR ${APP_HOME}

RUN cat > /usr/local/bin/start-llm-wiki.sh <<'EOF'
#!/usr/bin/env bash
set -u

APP_HOME="/home/seigyo/llm-wiki"
SESSION_NAME="backend"

echo "============================================================"
echo "Starting container"
echo "============================================================"

mkdir -p /run/sshd
ssh-keygen -A >/dev/null 2>&1 || true

mkdir -p "${APP_HOME}/.wiki" "/home/seigyo/.cache/uv"
chown -R seigyo:seigyo "${APP_HOME}/.wiki" "/home/seigyo/.cache"

echo "Starting sshd..."
/usr/sbin/sshd -e || echo "WARNING: sshd failed to start"

echo "Starting backend tmux session..."

if runuser -u seigyo -- tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session already exists: ${SESSION_NAME}"
else
  runuser -u seigyo -- tmux new-session -d -s "${SESSION_NAME}" -c "${APP_HOME}" bash

  sleep 1

  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "cd ${APP_HOME}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export WIKI_DB_DIR=${WIKI_DB_DIR:-.wiki}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export WIKI_DEFAULT_DB=${WIKI_DEFAULT_DB:-wiki}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export WIKI_PREFIX=${WIKI_PREFIX-/llm-wiki}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export UV_CACHE_DIR=/home/seigyo/.cache/uv" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export CHROME_BIN=/usr/local/bin/chrome-headless-shell" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome-headless-shell" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_SKIP_DOWNLOAD=true" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}" "uv run uvicorn app:app --port 8000 --host 0.0.0.0" C-m
fi

if ! runuser -u seigyo -- tmux list-windows -t "${SESSION_NAME}" -F '#{window_name}' | grep -qx mcp; then
  echo "Starting MCP tmux window..."
  runuser -u seigyo -- tmux new-window -t "${SESSION_NAME}" -n mcp -c "${APP_HOME}" bash
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}:mcp" "export WIKI_DB_DIR=${WIKI_DB_DIR:-.wiki}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}:mcp" "export WIKI_DEFAULT_DB=${WIKI_DEFAULT_DB:-wiki}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}:mcp" "export WIKI_PREFIX=${WIKI_PREFIX-/llm-wiki}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}:mcp" "export MCP_BACKEND_ORIGIN=${MCP_BACKEND_ORIGIN:-http://127.0.0.1:8000}" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}:mcp" "export UV_CACHE_DIR=/home/seigyo/.cache/uv" C-m
  runuser -u seigyo -- tmux send-keys -t "${SESSION_NAME}:mcp" "uv run python mcp_server.py --port 8001 --host 0.0.0.0" C-m
fi

sleep 2

echo
echo "tmux sessions:"
runuser -u seigyo -- tmux ls || true

echo
echo "Processes:"
ps -ef | grep -E "sshd|tmux|uvicorn|mcp_server" | grep -v grep || true

echo
echo "Listening ports:"
ss -lntp || true

echo
echo "Mermaid CLI:"
mmdc --version || true

echo
echo "Chrome Headless Shell:"
chrome-headless-shell --version || true

echo
echo "============================================================"
echo "Container ready"
echo "SSH:"
echo "  ssh -p 51027 seigyo@<HOST>"
echo
echo "Backend:"
echo "  http://<HOST>:51028"
echo
echo "MCP:"
echo "  http://<HOST>:8001${WIKI_PREFIX-/llm-wiki}/{wiki}/mcp"
echo
echo "Attach backend:"
echo "  tmux attach -t backend"
echo
echo "Password:"
echo "  seigyo@rikiseisan"
echo "============================================================"

tail -f /dev/null
EOF

RUN chmod +x /usr/local/bin/start-llm-wiki.sh

EXPOSE 22 8000 8001

CMD ["/usr/local/bin/start-llm-wiki.sh"]
