#!/bin/bash
# Cron wrapper: charter §4 pre-flight in watchdog mode — silent when GREEN, posts
# a RED/UNKNOWN once per distinct signature (state/fleet-preflight.json).
exec python3 "$HOME/.hermes/scripts/fleet-preflight.py" --quiet
