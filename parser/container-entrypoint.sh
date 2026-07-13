#!/usr/bin/env bash
set -Eeuo pipefail

# PROXY_URL is the single source of truth. An explicitly empty value disables
# every proxy variable, including defaults embedded in the image.
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

