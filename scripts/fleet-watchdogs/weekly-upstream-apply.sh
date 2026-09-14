#!/bin/bash
# Saturday 01:00 NZT, retrying every 30 min until 04:00. Freezes the board, applies
# the prepared plan, verifies, rolls itself back on any failure.
H="$HOME/.hermes"; exec >> "$H/logs/weekly-apply.log" 2>&1
echo "===================== apply $(date -Iseconds) ====================="
export HERMES_HOME="$H" AWS_EC2_METADATA_DISABLED=true
exec "$H/hermes-agent/venv/bin/python" "$H/scripts/upd_weekly_apply.py"
