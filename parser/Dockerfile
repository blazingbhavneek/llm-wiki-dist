# syntax=docker/dockerfile:1

ARG HTTP_PROXY=http://133.141.7.237:9515
ARG HTTPS_PROXY=http://133.141.7.237:9515
ARG NO_PROXY=localhost,127.0.0.1

FROM node:22-bookworm-slim AS frontend

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    no_proxy=${NO_PROXY}

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./

ARG URL_PREFIX=
RUN prefix="${URL_PREFIX%/}"; \
    case "$prefix" in \
      "") ;; \
      /*) ;; \
      *) prefix="/$prefix" ;; \
    esac; \
    printf '%s\n' \
      '// Generated during the container build. Do not edit by hand.' \
      "const BUILD_URL_PREFIX = '$prefix'" \
      "export const URL_PREFIX = BUILD_URL_PREFIX || (typeof window !== 'undefined' ? window.location.pathname.replace(/\\/+$/, '') : '')" \
      > src/url-prefix.js
RUN npm run build


FROM python:3.13-slim-bookworm

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    no_proxy=${NO_PROXY}

# Pandoc handles DOCX; the LibreOffice components handle DOCX/PPTX/XLSX
# conversion and Japanese font fallback. MinerU itself remains an external API.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        fonts-noto-cjk \
        libreoffice-calc \
        libreoffice-core \
        libreoffice-draw \
        libreoffice-impress \
        libreoffice-writer \
        pandoc \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN python -m pip install --no-cache-dir uv==0.12.16 \
    && uv sync --frozen --no-dev --no-install-project \
    && rm -rf /root/.cache

RUN groupadd --system --gid 10001 docparser \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \
        --shell /usr/sbin/nologin docparser

# This deployment intentionally includes the repository .env as the image's
# default configuration; process/container environment variables still win.
COPY --chown=docparser:docparser . .
COPY --from=frontend --chown=docparser:docparser /build/static ./static

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read()"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
