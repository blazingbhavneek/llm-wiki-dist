#!/usr/bin/env bash
# One test round: set human_sync_mode in the project INI, run `sync --fast`, then report.
#
#   HC_PROJECT=<config name> tools/hc_live/round.sh MODE LABEL [extra sync args...]
#
# MODE is off | observe | apply. LABEL names the log file ($HC_LOGS/LABEL.log, default
# /tmp/hc-e2e-logs). Prints: exit code, time, model calls, result lines, whether HEAD equals
# refs/llm-wiki/last-good with clean durable paths, and `human status`. Set HC_SYNC="sync" to
# run the standard pipeline instead of `sync --fast`.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd); PROJECT=${HC_PROJECT:-hc-e2e-fast}; LOGS=${HC_LOGS:-/tmp/hc-e2e-logs}
MODE=$1; LABEL=$2; shift 2
mkdir -p "$LOGS"; cd "$ROOT"
sed -i "s/^human_sync_mode = .*/human_sync_mode = $MODE/" "configs/$PROJECT.ini"
DATA=$(.venv/bin/python -c "from common.settings import load_settings as l; s=l('$PROJECT'); print(s.data_root + '/' + s.target_name)")
start=$(date +%s)
# shellcheck disable=SC2086
.venv/bin/python main.py --project "$PROJECT" -v ${HC_SYNC:-sync --fast} "$@" > "$LOGS/$LABEL.log" 2>&1; rc=$?
echo "== $LABEL mode=$MODE rc=$rc elapsed=$(( $(date +%s) - start ))s model_calls=$(grep -c 'send_request_headers.started' "$LOGS/$LABEL.log")"
grep -E "^\{|ERROR" "$LOGS/$LABEL.log" | grep -v '"status": "unchanged"' | cut -c1-300 | head -20
same=$([ "$(git -C "$DATA" rev-parse HEAD)" = "$(git -C "$DATA" rev-parse refs/llm-wiki/last-good)" ] && echo yes || echo NO)
dirty=$(git -C "$DATA" status --porcelain -- .gitignore sources raw wiki metadata/state metadata/pipeline.json metadata/source-identities.json metadata/human-sync | wc -l)
echo "HEAD==last-good: $same  durable-dirty: $dirty"
.venv/bin/python main.py --project "$PROJECT" human status 2>&1 | tail -n 3 | cut -c1-400
