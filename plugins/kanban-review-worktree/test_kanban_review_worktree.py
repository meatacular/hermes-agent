"""Acceptance tests for review-lane worktree routing."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent
spec = importlib.util.spec_from_file_location("review_worktree_under_test", PLUGIN_DIR / "__init__.py")
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def _db(tmp_path: Path) -> Path:
    from hermes_cli import kanban_db_connect as kbc
    path = tmp_path / "kanban.db"
    kbc.init_db(db_path=path)
    return path


def _task(conn, task_id, *, status="review", kind="worktree", workspace="/base", branch=None):
    conn.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,workspace_kind,workspace_path,branch_name,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (task_id, "review", "", "rodge", status, kind, workspace, branch),
    )


def _claimed(conn, task_id, source="review"):
    conn.execute(
        "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,0)",
        (task_id, "claimed", '{"source_status": "' + source + '"}'),
    )


def test_ac1_review_claim_uses_sole_implementation_parent(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    with sqlite3.connect(db) as conn:
        _task(conn, "impl", status="done", workspace="/reviewed", branch="feature/reviewed")
        _task(conn, "review", workspace="/fresh", branch="review/card")
        conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES ('impl','review')")
        _claimed(conn, "review")
        conn.commit()

    result = plugin.route_review("review")
    assert result["action"] == "routed"
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT workspace_path,branch_name FROM tasks WHERE id='review'").fetchone()
    assert row == ("/reviewed", "feature/reviewed")


def test_ac2_missing_review_parent_branch_fails_open(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    with sqlite3.connect(db) as conn:
        _task(conn, "impl", status="done", workspace="/reviewed", branch=None)
        _task(conn, "review", workspace="/fresh", branch="review/card")
        conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES ('impl','review')")
        _claimed(conn, "review")
        conn.commit()
    result = plugin.route_review("review")
    assert result["action"] == "noop"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT workspace_path FROM tasks WHERE id='review'").fetchone()[0] == "/fresh"


@pytest.mark.parametrize("kind", ["scratch", "dir"])
def test_ac3_non_worktree_card_is_untouched(tmp_path, monkeypatch, kind):
    db = _db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    with sqlite3.connect(db) as conn:
        _task(conn, "review", kind=kind, workspace="/shared", branch="ignored")
        _claimed(conn, "review")
        conn.commit()
    assert plugin.route_review("review")["action"] == "noop"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT workspace_path FROM tasks WHERE id='review'").fetchone()[0] == "/shared"
