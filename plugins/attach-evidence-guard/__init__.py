"""attach-evidence-guard — refuse a blank image at the attach seam.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

Narrow on purpose: it inspects only the decoded bytes of a ``kanban_attach`` call. A PNG
whose IHDR says width or height 0, a JPEG/GIF/WebP whose header dimensions read 0, or any
0-byte payload is refused with a message that says what to attach instead. Anything the
parser cannot read (not an image, truncated header, undecodable base64 — that one the
kernel already rejects) is ALLOWED: this guard exists to stop a placeholder passing as
evidence, not to second-guess real files.
"""
from __future__ import annotations

import base64
import binascii
import logging
import struct
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["image_dimensions", "on_pre_tool_call", "register"]


def image_dimensions(data: bytes) -> Optional[tuple]:
    """(width, height) for PNG/GIF/JPEG/WebP headers, else None (unknown format)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h)
                seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seglen
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            if data[12:16] == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little"); h = 1 + int.from_bytes(data[27:30], "little")
                return (w, h)
            if data[12:16] == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0]); h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return (w, h)
            if data[12:16] == b"VP8 ":
                w = struct.unpack("<H", data[26:28])[0] & 0x3FFF; h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return (w, h)
    except Exception:  # noqa: BLE001
        return None
    return None


def _verdict(filename: str, data: bytes) -> Optional[str]:
    if len(data) == 0:
        return f"`{filename}` is 0 bytes"
    dims = image_dimensions(data)
    if dims is not None and (dims[0] == 0 or dims[1] == 0):
        return f"`{filename}` is a {dims[0]}x{dims[1]} image ({len(data)} bytes) — a placeholder, not a capture"
    return None


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    try:
        if payload.get("tool_name") != "kanban_attach":
            return None
        args = payload.get("args") or {}
        raw = args.get("content_base64")
        if not isinstance(raw, str) or not raw:
            return None                      # the kernel rejects a missing payload itself
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None                      # the kernel's own rejection names this one
        why = _verdict(str(args.get("filename") or "attachment"), data)
        if why is None:
            return None
        logger.warning("attach-evidence-guard: refusing kanban_attach — %s", why)
        return {"action": "block", "message": (
            f"kanban_attach refused: {why}. A blank image cannot satisfy an evidence acceptance "
            "criterion. Attach the real capture (e.g. `hermes kanban attach <task_id> <path>` with "
            "the PNG the browser actually produced), or, if the capture cannot be produced, say so "
            "in a comment and block with kind=capability rather than attaching a stub.")}
    except Exception:  # noqa: BLE001
        logger.exception("attach-evidence-guard: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
