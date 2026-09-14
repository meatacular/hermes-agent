#!/bin/bash
# Per-skill defects the context budget cannot see: disabled-but-attached (crash risk),
# oversized SKILL.md, descriptions with no trigger, dangling references. Silent when clean.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "$HOME/.hermes/hermes-agent/venv/bin/python" \
     "$HOME/.hermes/hermes-agent/scripts/fleet-watchdogs/skill-hygiene-watch.py"
