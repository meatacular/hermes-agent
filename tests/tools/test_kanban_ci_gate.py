from __future__ import annotations

import subprocess

from tools import kanban_ci_gate as gate


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("base\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "base")
    _git(path, "branch", "-f", "origin/main", "HEAD")
    return path


def test_ac1_committed_work_without_pr_stays_blocked(tmp_path, monkeypatch):
    path = _repo(tmp_path)
    (path / "tracked.txt").write_text("changed\n")
    _git(path, "commit", "-am", "change")
    monkeypatch.setattr(gate, "_tenant_ci", lambda _tenant: {"check_name": "test-gate", "repo": "x/y", "base_ref": "origin/main"})
    monkeypatch.setattr(gate, "_gh", lambda *args, **kwargs: (1, "", "no PR"))

    result = gate.evaluate(str(path), "backupbrain")

    assert result.blocked is True
    assert "no open pull request" in result.reason


def test_ac1_zero_commits_ahead_allows_with_named_reason(tmp_path, monkeypatch):
    path = _repo(tmp_path)
    monkeypatch.setattr(gate, "_tenant_ci", lambda _tenant: {"check_name": "test-gate", "repo": "x/y", "base_ref": "origin/main"})

    result = gate.evaluate(str(path), "backupbrain")

    assert result.blocked is False
    assert result.reason == "this card's branch carries no commit of its own; there is no artifact for CI to verify"


def test_ac3_zero_commits_with_tracked_work_is_blocked(tmp_path, monkeypatch):
    path = _repo(tmp_path)
    (path / "tracked.txt").write_text("uncommitted\n")
    monkeypatch.setattr(gate, "_tenant_ci", lambda _tenant: {"check_name": "test-gate", "repo": "x/y", "base_ref": "origin/main"})
    monkeypatch.setattr(gate, "_gh", lambda *args, **kwargs: (1, "", "no PR"))

    result = gate.evaluate(str(path), "backupbrain")

    assert result.blocked is True
    assert "tracked file(s) modified but not committed" in result.reason


def test_ac2_out_of_scope_tenant_unchanged(tmp_path):
    path = _repo(tmp_path)

    result = gate.evaluate(str(path), "someothertenant")

    assert result.blocked is False
    assert result.detail == "tenant not in scope for the CI gate"


def test_release_scope_exempts_non_release_cards(tmp_path, monkeypatch):
    """D-CI (2026-09-17): with ci_gate.scope == 'release', a build/verify/dir card passes straight through;
    a [Release]/merge/deploy card is still gated (and blocks here because there is no PR)."""
    monkeypatch.setattr(gate, "_tenant_ci", lambda tenant: {"check_name": "test-gate", "repo": "x/y", "scope": "release"})
    ws = tmp_path / "ws"; ws.mkdir()
    for title in ("[WP-A2] Per-context intake policy", "[Verify] Gmail polling on the deployed backend", "[Platform] the gate is wrong", "fix the thing"):
        r = gate.evaluate(str(ws), "backupbrain", title=title)
        assert r.blocked is False and "not a release-lane" in r.detail, title
    for title in ("[Release] Merge PR #80", "Release: land the hermetic regress gate", "[BackupBrain] [Release] Land PR #81", "Deploy: the thing", "Merge PR #82 (approved)"):
        r = gate.evaluate(str(ws), "backupbrain", title=title)
        assert r.blocked is True, title          # still gated: the scratch ws has no PR


def test_release_scope_control_all_scope_and_no_title_keep_every_card_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_tenant_ci", lambda tenant: {"check_name": "test-gate", "repo": "x/y"})
    ws = tmp_path / "ws"; ws.mkdir()
    assert gate.evaluate(str(ws), "backupbrain", title="[Verify] anything").blocked is True
    monkeypatch.setattr(gate, "_tenant_ci", lambda tenant: {"check_name": "test-gate", "repo": "x/y", "scope": "release"})
    assert gate.evaluate(str(ws), "backupbrain").blocked is True          # no title -> today's behaviour
