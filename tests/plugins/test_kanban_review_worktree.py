"""Behavioral coverage for the bundled review-worktree mint guard."""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "kanban-review-worktree" / "__init__.py"


def _plugin():
    spec = importlib.util.spec_from_file_location("review_worktree_plugin", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(path), "branch", "feature/reviewed"], check=True)


def _board(db: Path, repo: Path) -> None:
    from hermes_cli import kanban_db_connect as kbc
    kbc.init_db(db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks (id,title,assignee,status,workspace_kind,workspace_path,branch_name,created_at) "
            "VALUES ('impl','impl','bob','done','worktree',?,?,0)",
            (str(repo), "feature/reviewed"),
        )
        conn.commit()


def test_ac1_pre_tool_hook_routes_review_mint_to_parent_worktree(tmp_path, monkeypatch):
    """AC1/AC2: real pre-tool hook returns the reviewed parent workspace."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    db = tmp_path / "kanban.db"
    _board(db, repo)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    plugin = _plugin()
    result = plugin.on_pre_tool_call(tool_name="kanban_create", args={
        "title": "Review implementation", "assignee": "rodge", "parents": ["impl"]})
    assert result == {"action": "modify", "args": {"workspace_kind": "dir", "workspace_path": str(repo)}}


def test_ac3_controls_fail_open_and_explicit_workspace_wins(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    db = tmp_path / "kanban.db"
    _board(db, repo)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    plugin = _plugin()
    base = {"title": "Review implementation", "assignee": "rodge", "parents": ["impl"]}
    assert plugin.verdict({**base, "workspace_kind": "worktree"}) is None
    assert plugin.verdict({**base, "parents": ["missing"]}) is None
    assert plugin.verdict({"title": "Build implementation", "assignee": "bob", "parents": ["impl"]}) is None
    assert plugin.verdict({**base, "parents": ["impl", "other"]}) is None


def test_ac4_missing_parent_worktree_is_logged_and_untouched(tmp_path, monkeypatch, caplog):
    db = tmp_path / "kanban.db"
    from hermes_cli import kanban_db_connect as kbc
    kbc.init_db(db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks (id,title,assignee,status,workspace_kind,workspace_path,branch_name,created_at) "
            "VALUES ('impl','impl','bob','done','worktree','/gone','feature/reviewed',0)"
        )
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    plugin = _plugin()
    with caplog.at_level("INFO"):
        assert plugin.on_pre_tool_call(tool_name="kanban_create", args={
            "title": "Review implementation", "assignee": "rodge", "parents": ["impl"]}) is None
    assert "implementation worktree is missing" in caplog.text
