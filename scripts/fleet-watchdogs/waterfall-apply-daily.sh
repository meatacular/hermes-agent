#!/bin/bash
# Daily act-first waterfall pass. Honours state/waterfall-policy.json:
# report_only -> prints what it WOULD do; act_first -> applies + arms the monitor.
set -u
H="$HOME/.hermes"; PY="$H/hermes-agent/venv/bin/python"
export WATERFALL_WINDOW_DAYS=7
mkdir -p "$H/logs"
exec >>"$H/logs/waterfall-apply-$(date +%Y%m%d).log" 2>&1
echo "=== $(date -Iseconds) ==="
"$PY" "$H/scripts/waterfall-apply.py"
