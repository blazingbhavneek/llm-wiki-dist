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

