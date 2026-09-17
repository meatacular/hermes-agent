"""kanban-completion-gate — the gate must block a dirty `dir` workspace for ITS OWN worker only.

Run: venv/bin/python -m pytest -q plugins/kanban-completion-gate/test_kanban_completion_gate.py
"""
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location("kcg_under_test", HERE / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    _git(r, "init", "-q"); (r / "a.txt").write_text("1\n"); _git(r, "add", "a.txt"); _git(r, "commit", "-q", "-m", "init")
    return r


def _install_fake_api(mod, kind, path):
    task = types.SimpleNamespace(workspace_kind=kind, workspace_path=str(path))
    mod._kanban_api = lambda: (lambda: types.SimpleNamespace(close=lambda: None), lambda conn, tid: task)


def test_dirty_dir_workspace_is_blocked_for_its_own_worker(repo, monkeypatch):
    mod = _load(); _install_fake_api(mod, "dir", repo)
    (repo / "a.txt").write_text("2\n")           # tracked, uncommitted
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test0001")
    out = mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"})
    assert out and out["action"] == "block" and "NOT COMMITTED" in out["message"]


def test_clean_dir_workspace_is_allowed(repo, monkeypatch):
    mod = _load(); _install_fake_api(mod, "dir", repo)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test0001")
    assert mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"}) is None


def test_untracked_scratch_never_traps_the_card(repo, monkeypatch):
    mod = _load(); _install_fake_api(mod, "dir", repo)
    (repo / "scratch.log").write_text("x")       # untracked only
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test0001")
    assert mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"}) is None


def test_non_worker_and_other_card_pass_through(repo, monkeypatch):
    mod = _load(); _install_fake_api(mod, "dir", repo)
    (repo / "a.txt").write_text("2\n")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"}) is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other")
    assert mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"}) is None


def test_worktree_kind_is_out_of_scope(repo, monkeypatch):
    mod = _load(); _install_fake_api(mod, "worktree", repo)
    (repo / "a.txt").write_text("2\n")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test0001")
    assert mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"}) is None


def test_negative_control_neutered_detector_goes_green(repo, monkeypatch):
    """If the detector is neutered the block test would pass vacuously — prove it can go red."""
    mod = _load(); _install_fake_api(mod, "dir", repo)
    (repo / "a.txt").write_text("2\n")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test0001")
    mod._uncommitted_tracked = lambda ws: []
    assert mod.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": "t_test0001"}) is None
