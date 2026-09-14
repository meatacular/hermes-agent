#!/bin/bash
# Weekly counterfactual report. Mondays 07:00.
set -u
H="$HOME/.hermes"; PY="$H/hermes-agent/venv/bin/python"
export WATERFALL_WINDOW_DAYS=7 CF_DAYS=7
mkdir -p "$H/logs"
exec >>"$H/logs/waterfall-weekly-$(date +%Y%m%d).log" 2>&1
echo "=== $(date -Iseconds) ==="
"$PY" "$H/scripts/waterfall-counterfactual.py"
