#!/bin/bash
# Is any profile's authored context past budget? Silent when every profile is inside it.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/hermes-agent/scripts/fleet-watchdogs/context-budget-watch.py"
