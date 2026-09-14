#!/bin/bash
# Charter §4 estimate at the mint path — the watchdog that replaced the reverted
# core patch 64ab4fc8a. Silent unless it wrote a placeholder.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/hermes-agent/scripts/fleet-watchdogs/points-mint-watch.py" --minutes 20
