#!/bin/bash
# Friday 22:00 NZT. Board runs normally; the live fleet is only READ.
H="$HOME/.hermes"; exec >> "$H/logs/weekly-prepare.log" 2>&1
echo "===================== prepare $(date -Iseconds) ====================="
export HERMES_HOME="$H" AWS_EC2_METADATA_DISABLED=true
exec "$H/hermes-agent/venv/bin/python" "$H/scripts/upd_weekly_prepare.py"
