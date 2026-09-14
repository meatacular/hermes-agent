"""Regression tests for the user-installed respawn guard plugin."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "respawn-guard-review-lane" / "__init__.py"
_spec = importlib.util.spec_from_file_location("respawn_guard_review_lane", _PLUGIN)
assert _spec is not None and _spec.loader is not None
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)


def _db(assignee="bob", skills=None, changes_requested=False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, skills TEXT);"
        "CREATE TABLE task_events (task_id TEXT, kind TEXT);"
    )
    import json
    conn.execute("INSERT INTO tasks VALUES ('t1', ?, ?)", (assignee, json.dumps(skills) if skills is not None else None))
    if changes_requested:
        conn.execute("INSERT INTO task_events VALUES ('t1', 'changes_requested')")
    conn.commit()
    return conn


def _active_pr(_conn, _task_id, *, lane="ready"):
    return "active_pr"


def test_ac1_ordinary_card_keeps_active_pr_guard():
    assert _plugin.wrap_check_respawn_guard(_active_pr)(_db(), "t1") == "active_pr"


def test_plugin_install_wraps_dispatcher_global_and_suppresses_reviewer():
    from hermes_cli import kanban_db_dispatch

    original = kanban_db_dispatch.check_respawn_guard
    try:
        kanban_db_dispatch.check_respawn_guard = _active_pr
        _plugin.install()
        installed = kanban_db_dispatch.check_respawn_guard
        assert getattr(installed, _plugin._MARKER, False)
        assert installed(_db(assignee="rodge"), "t1") is None
    finally:
        kanban_db_dispatch.check_respawn_guard = original


def test_ac2_reviewer_assignee_suppresses_active_pr():
    assert _plugin.wrap_check_respawn_guard(_active_pr)(_db(assignee="rodge"), "t1") is None


def test_ac3_review_skill_suppresses_active_pr():
    assert _plugin.wrap_check_respawn_guard(_active_pr)(_db(skills=["sdlc-review"]), "t1") is None


def test_ac4_changes_requested_suppresses_active_pr():
    assert _plugin.wrap_check_respawn_guard(_active_pr)(_db(changes_requested=True), "t1") is None


def test_ac5_predicate_failure_is_fail_closed_and_install_is_idempotent():
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    wrapped = _plugin.wrap_check_respawn_guard(broken)
    assert wrapped(_db(), "t1") == "active_pr"
    assert _plugin.wrap_check_respawn_guard(wrapped) is wrapped


def test_other_guard_reasons_are_delegated_unchanged():
    def other(_conn, _task_id, *, lane="ready"):
        return "recent_success"

    assert _plugin.wrap_check_respawn_guard(other)(_db(assignee="rodge"), "t1") == "recent_success"
