"""The fleet privacy guardrail, authored ONCE and rendered into every profile's prompt.

WHY THIS IS A PLUGIN AND NOT EIGHT COPIES IN EIGHT SOULS
--------------------------------------------------------
As of 2026-09-13 this block was pasted into 8 of the 9 SOULs — 1,262 bytes each, ~10KB of
the fleet's soul budget — and it was MISSING ENTIRELY from ``brain``, the profile that
indexes Richie's own corpus and is therefore the one with the most to leak. A rule
described in its own first line as "UNBREAKABLE — ALL AGENTS, NEVER OVERRIDABLE" was in
fact absent from one agent and maintained by hand in seven others.

Copy-paste is not a mechanism for an invariant. A plugin section is: one file, rendered
into every profile including any added later, and impossible to drift between profiles
because there is only one of it.

extension-point: plugin. kind: backend, so it loads in every profile AND every kanban
worker — a user plugin in ~/.hermes/plugins is invisible to workers.

The text lives in GUARDRAIL.md next to this file, read at render time, so the wording is
reviewable as prose rather than buried in a Python string. If the file cannot be read the
section renders nothing rather than a half-rule: a partial privacy rule is worse than a
visibly absent one, and the SOUL copies are only removed after this is proven to render.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = ["register", "guardrail_text", "render"]

logger = logging.getLogger(__name__)

SECTION_ID = "fleet-privacy-guardrail"
_TEXT_FILE = Path(__file__).with_name("GUARDRAIL.md")
# Must exceed the file; the host rejects a section over MAX_SYSTEM_PROMPT_SECTION_CHARS (4000).
MAX_CHARS = 3000


def guardrail_text() -> Optional[str]:
    """The guardrail prose, or None if it cannot be read or looks truncated."""
    try:
        text = _TEXT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("fleet-privacy-guardrail: GUARDRAIL.md unreadable — rendering nothing")
        return None
    # Refuse to render a partial rule: all four numbered clauses must be present.
    if not text or not all(f"{n}." in text for n in (1, 2, 3, 4)):
        logger.warning("fleet-privacy-guardrail: GUARDRAIL.md looks incomplete — rendering nothing")
        return None
    return text


def render(_session_info: Mapping[str, Any]) -> str:
    """Section renderer. Returns "" rather than raising — a failure here must not cost a session."""
    try:
        return guardrail_text() or ""
    except Exception:  # noqa: BLE001
        logger.debug("fleet-privacy-guardrail failed open", exc_info=True)
        return ""


def register(ctx) -> None:
    ctx.register_system_prompt_section(
        SECTION_ID, render, position="after_memory", max_chars=MAX_CHARS)
