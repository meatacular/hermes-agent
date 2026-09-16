"""Card-scoped cost seam shared by enforcement and pricing plugins."""
from __future__ import annotations

import functools
import logging
import sqlite3

logger = logging.getLogger(__name__)
_MARK = "_costscope_wrapped"


def scope_predicate(kdb, workspace=None, task_id=None):
    """Return one SQL predicate and params for session alias ``s``.

    A card id is authoritative for enforcement; cwd is retained only for
    reporting callers that do not provide a card id.
    """
    if task_id:
        return "s.title LIKE ? ESCAPE '\\'", ["%" + kdb._escape_like(str(task_id)) + "%"]
    prefix = str(workspace).rstrip("/\\") if workspace else ""
    if prefix:
        return "(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\')", [prefix, kdb._escape_like(prefix) + "/%"]
    return "", []


def _wrap_session_cost(orig, kdb):
    @functools.wraps(orig)
    def session_cost(state_db_path, prefix_for=None, task_id=None):
        if not task_id:
            return orig(state_db_path, prefix_for=prefix_for, task_id=task_id)
        try:
            where, params = scope_predicate(kdb, prefix_for, task_id)
            conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=10)
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(s.estimated_cost_usd), 0) FROM sessions s WHERE " + where,
                    params,
                ).fetchone()
                total = float(row[0] or 0.0)
                if total <= 0:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(u.estimated_cost_usd), 0) FROM session_model_usage u "
                        "JOIN sessions s ON s.id = u.session_id WHERE " + where,
                        params,
                    ).fetchone()
                    total = float(row[0] or 0.0)
                cap = getattr(kdb, "costscope_cap_equivalent", None)
                if cap is not None:
                    try:
                        extra, _ = cap(conn, where, params)
                        total += float(extra)
                    except Exception as exc:  # optional add-on must not erase base cost
                        logger.warning("costscope: cap-equivalent unavailable: %s", exc)
                return total
            finally:
                conn.close()
        except Exception as exc:  # the kernel remains authoritative when scope cannot be computed
            logger.warning("costscope: scoped read unavailable; deferring to kernel: %s", exc)
            return orig(state_db_path, prefix_for=prefix_for, task_id=task_id)
    setattr(session_cost, _MARK, True)
    return session_cost


def install():
    from hermes_cli import kanban_db as kdb
    if not getattr(kdb._session_cost_in_db, _MARK, False):
        kdb._session_cost_in_db = _wrap_session_cost(kdb._session_cost_in_db, kdb)
    kdb.costscope_predicate = lambda workspace=None, task_id=None: scope_predicate(kdb, workspace, task_id)
    return kdb._session_cost_in_db


def register(ctx):  # plugin loader entry point
    try:
        install()
    except Exception as exc:  # backend discovery must remain fail-open
        logger.warning("costscope: seam not installed: %s", exc)
