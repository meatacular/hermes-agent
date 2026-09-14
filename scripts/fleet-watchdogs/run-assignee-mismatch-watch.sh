#!/bin/bash
# Assignee-mismatch auditor (fix-C2, script-only — the part of the 2026-09-12
# batch that was ALIGNED with plugins-not-core and survived the rollback).
#
# WRITE MODE, enabled 2026-09-13 after the apply path was proved live:
#   --apply        comment + block an actionable mis-routed card for Jobsy
#   --max-apply 3  blast-radius cap: if MORE than 3 cards would be blocked,
#                  it writes NOTHING and reports. Many at once means the
#                  auditor is wrong, not the board. Do not raise this without
#                  reading why (BACKLOG 10).
# It never touches a running card, a terminal card, or one already held.
# Silent unless it finds something.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/hermes-agent/scripts/fleet-watchdogs/assignee-mismatch-watch.py" \
     --days 1 --apply --max-apply 3
