#!/bin/bash
# restart-debt sweeper, ROOT's half: it may restart AXEL and SWITCH.
#
# Root's cron runs this, so it must never target root — a gateway that restarts
# itself kills the scheduler running the script. Root's own debt is cleared by
# the twin of this file on AXEL's cron
# (profiles/axel/scripts/restart-debt-sweep-root.sh).
#
# The python also derives its own gateway from HERMES_HOME and drops it from the
# target set regardless of what --gateways says, so this split is belt AND
# braces rather than the only thing standing between us and a dead scheduler.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/scripts/restart-debt-sweeper.py" --gateways axel,switch,brain
