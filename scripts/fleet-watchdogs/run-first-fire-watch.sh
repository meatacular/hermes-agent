#!/bin/bash
# First-fire watch: one plain-English message the first time the routing auditor
# blocks a card nobody planted, then it retires itself. Silent otherwise.
# Bridges the one gap left after arming assignee-mismatch-watch on planted cards.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/scripts/first-fire-watch.py"
