#!/bin/bash
# Did anything patch upstream's kernel behind our back? Silent when clean.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/hermes-agent/scripts/fleet-watchdogs/core-patch-watch.py"
