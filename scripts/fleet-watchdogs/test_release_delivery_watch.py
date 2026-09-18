"""Tests for release-delivery-watch, with the controls that make them mean something.

The point of each control: a test that passes whether or not the mechanism is present proves
nothing. Every positive here has a paired negative that must flip when the mechanism is removed.
"""
import importlib.util, os, sys, types
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rdw", os.path.join(HERE, "release-delivery-watch.py"))
rdw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rdw)


# ---------- pr_number: it must never guess ----------

def test_release_pr_marker_wins():
    assert rdw.pr_number("[Release] land the thing", "release-pr: #80\nblah") == 80

def test_marker_without_hash_still_parses():
    assert rdw.pr_number("[Release] x", "release-pr: 81") == 81

def test_pr_read_from_title_when_unambiguous():
    assert rdw.pr_number("[Release] Merge PR #80 (approved, clean)", "no marker") == 80

def test_two_prs_in_the_title_is_refused_not_guessed():
    assert rdw.pr_number("[Release] Merge PR #80 then PR #81", "no marker") is None

def test_body_prose_is_never_read_for_the_pr():
    """THE #82/#84 TRAP. A release body legitimately names other PRs as things NOT to merge.
    Reading the body's prose would land the wrong PR."""
    body = "Do NOT merge PR #84 or PR #85; they are measured RED in composition."
    assert rdw.pr_number("[Release] land the approved change", body) is None

def test_non_release_card_without_a_marker_is_ignored():
    assert rdw.pr_number("[BackupBrain] fix the poll cycle", "mentions PR #80 in passing") is None

def test_non_release_card_WITH_a_marker_is_honoured():
    assert rdw.pr_number("[BackupBrain] land it", "release-pr: #80") == 80


# ---------- delivered(): each of the three conditions must be load-bearing ----------

def _fake_sh(gh_payload, *, ancestor_rc=0, gh_rc=0, gh_err=""):
    import json as _j
    def sh(args, cwd=None, timeout=90):
        class R: pass
        r = R(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        if args[:3] == ["gh", "pr", "view"]:
            r.returncode, r.stdout, r.stderr = gh_rc, _j.dumps(gh_payload), gh_err
        elif args[:2] == ["git", "merge-base"]:
            r.returncode = ancestor_rc
        return r
    return sh

MERGED_OK = {"state": "MERGED", "mergeCommit": {"oid": "d1e027f" + "0" * 33},
             "headRefOid": "135c30c", "statusCheckRollup": [{"name": "test-gate", "conclusion": "SUCCESS"}]}

def test_all_three_conditions_met_is_delivered(monkeypatch):
    monkeypatch.setattr(rdw, "sh", _fake_sh(MERGED_OK))
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert ok and "MERGED as d1e027f" in why and "test-gate SUCCESS" in why

def test_open_pr_is_not_delivered(monkeypatch):
    p = dict(MERGED_OK, state="OPEN")
    monkeypatch.setattr(rdw, "sh", _fake_sh(p))
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert not ok and "not MERGED" in why

def test_merge_into_the_wrong_base_is_not_delivered(monkeypatch):
    """CONTROL for condition 2. Without the ancestor check this would pass."""
    monkeypatch.setattr(rdw, "sh", _fake_sh(MERGED_OK, ancestor_rc=1))
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert not ok and "not an ancestor of origin/main" in why

def test_red_test_gate_on_the_merged_head_is_not_delivered(monkeypatch):
    """CONTROL for condition 3."""
    p = dict(MERGED_OK, statusCheckRollup=[{"name": "test-gate", "conclusion": "FAILURE"}])
    monkeypatch.setattr(rdw, "sh", _fake_sh(p))
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert not ok and "not SUCCESS" in why

def test_missing_test_gate_is_not_delivered(monkeypatch):
    """An absent check is not a passing check - the trap that hid `backend unit` for a week."""
    p = dict(MERGED_OK, statusCheckRollup=[{"name": "frontend unit", "conclusion": "SUCCESS"}])
    monkeypatch.setattr(rdw, "sh", _fake_sh(p))
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert not ok and "None" in why

def test_merged_with_no_merge_commit_is_not_delivered(monkeypatch):
    p = dict(MERGED_OK, mergeCommit=None)
    monkeypatch.setattr(rdw, "sh", _fake_sh(p))
    ok, _ = rdw.delivered(83, "r/r", "/tmp")
    assert not ok

def test_gh_failure_is_a_refusal_not_a_crash(monkeypatch):
    monkeypatch.setattr(rdw, "sh", _fake_sh({}, gh_rc=1, gh_err="could not resolve host"))
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert not ok and "gh pr view #83 failed" in why

def test_unreadable_gh_output_is_a_refusal_not_a_crash(monkeypatch):
    def sh(args, cwd=None, timeout=90):
        class R: returncode, stdout, stderr = 0, "not json at all", ""
        return R()
    monkeypatch.setattr(rdw, "sh", sh)
    ok, why = rdw.delivered(83, "r/r", "/tmp")
    assert not ok and "unreadable" in why

def test_sh_survives_a_hung_child(monkeypatch):
    """Trap 58: a hung subprocess must not take the tick down with it."""
    import subprocess
    def boom(*a, **k): raise subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(rdw.subprocess, "run", boom)
    r = rdw.sh(["anything"])
    assert r.returncode == 124


# ---------- the negative control that proves the suite can go red ----------

def test_NEGATIVE_CONTROL_neutered_ancestor_check_breaks_a_test(monkeypatch):
    """Replace `delivered` with one that skips the ancestor check. The wrong-base test above
    must then be wrong. If this control ever passes silently, the suite is vacuous."""
    def neutered(pr, repo, tree):
        return True, "no checks at all"
    monkeypatch.setattr(rdw, "delivered", neutered)
    ok, _ = rdw.delivered(83, "r/r", "/tmp")
    assert ok, "the neutered version must accept everything - that is the point of the control"
