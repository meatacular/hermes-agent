"""kanban-completion-gate 0.3.0 — build-lane worktree cards need an OPEN, GREEN PR on the trunk.

Run: venv/bin/python -m pytest -q plugins/kanban-completion-gate/test_done_requires_pr.py
Hermetic: `gh` and `git` are faked by monkeypatching subprocess.run inside the module.
"""
import importlib.util
import json
import logging
import subprocess
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TID = "t_build0001"
BRANCH = "feat/t_build0001-thing"
SHA = "a" * 40


def _load():
    spec = importlib.util.spec_from_file_location("kcg_pr_under_test", HERE / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(tmp_path, monkeypatch):
    m = _load()
    tenants = tmp_path / "kanban-tenants.json"
    tenants.write_text(json.dumps({
        "weroll-app": {"id": "p_weroll_app", "ci_gate": {"check_name": "ci", "repo": "meatacular/weroll-app",
                                                          "scope": "release"}},
        "no-ci": {"id": "p_noci"},
    }))
    m.TENANTS_PATH = tenants
    m._gh_bin = lambda: "/fake/gh"
    monkeypatch.setenv("HERMES_KANBAN_TASK", TID)
    monkeypatch.delenv("HERMES_KANBAN_PR_GATE_DISABLE", raising=False)
    return m


def _task(m, tmp_path, *, title="Build the thing", tenant="weroll-app", body="", kind="worktree"):
    ws = tmp_path / "wt"; ws.mkdir(exist_ok=True)
    task = types.SimpleNamespace(id=TID, title=title, body=body, tenant=tenant, workspace_kind=kind,
                                 workspace_path=str(ws), branch_name=BRANCH)
    m._kanban_api = lambda: (lambda: types.SimpleNamespace(close=lambda: None), lambda conn, tid: task)
    return task


def _fake_run(m, monkeypatch, prs, *, origin_sha=SHA, head_sha=SHA, raise_exc=None):
    """Fake gh/git. `prs` is what `gh pr list --json` prints.

    Patched via monkeypatch so the REAL subprocess.run is restored after each test —
    the module imports `subprocess` itself, so `m.subprocess` is the global module.
    """
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if raise_exc is not None and argv[0] == "/fake/gh":
            raise raise_exc
        if argv[0] == "/fake/gh":
            assert argv[1:3] == ["pr", "list"] and "--state" in argv and argv[argv.index("--state") + 1] == "open"
            assert argv[argv.index("--head") + 1] == BRANCH
            assert kw.get("timeout") == m.GH_TIMEOUT_S
            return subprocess.CompletedProcess(argv, 0, json.dumps(prs), "")
        if argv[0] == "git" and "refs/remotes/origin/" + BRANCH in argv:
            return (subprocess.CompletedProcess(argv, 0, origin_sha + "\n", "") if origin_sha
                    else subprocess.CompletedProcess(argv, 1, "", ""))
        if argv[0] == "git" and argv[-1] == "HEAD":
            return subprocess.CompletedProcess(argv, 0, head_sha + "\n", "")
        raise AssertionError(f"unexpected subprocess {argv}")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _pr(status="COMPLETED", conclusion="SUCCESS", base="main", head=SHA, number=42, name="ci"):
    return {"number": number, "baseRefName": base, "headRefOid": head, "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"name": name, "status": status, "conclusion": conclusion}]}


def _complete(m, result="did the thing"):
    return m.on_pre_tool_call(tool_name="kanban_complete", args={"task_id": TID, "result": result})


# (a) no PR
def test_no_pr_is_refused_naming_open_a_pr(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [])
    out = _complete(mod)
    assert out and out["action"] == "block"
    assert "MISSING 1/3" in out["message"] and "gh pr create" in out["message"] and "--base main" in out["message"]


def test_pr_against_wrong_base_counts_as_no_pr(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr(base="develop")])
    out = _complete(mod)
    assert out and out["action"] == "block" and "MISSING 1/3" in out["message"] and "develop" in out["message"]


# (b) negative control: open PR, head matches, ci success -> passes (and result gets PR #n)
def test_open_green_pr_passes_and_tags_result(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr()])
    out = _complete(mod)
    assert out == {"action": "modify", "args": {"result": "did the thing\nPR #42"}}


def test_open_green_pr_with_pr_already_in_result_is_untouched(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr()])
    assert _complete(mod, result="did the thing, see PR #42") is None


def test_negative_control_success_becomes_refusal_when_check_is_red(mod, tmp_path, monkeypatch):
    """Prove the pass path is not vacuous: the SAME setup with a failed check is refused."""
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr(conclusion="FAILURE")])
    out = _complete(mod)
    assert out and out["action"] == "block" and "FAILED" in out["message"]


# (c) head mismatch
def test_head_mismatch_is_refused_naming_push(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr()], origin_sha="b" * 40)
    out = _complete(mod)
    assert out and out["action"] == "block"
    assert "MISSING 2/3" in out["message"] and f"git -C {tmp_path / 'wt'} push origin {BRANCH}" in out["message"]
    assert "refs/remotes/origin/" + BRANCH in out["message"]


def test_head_mismatch_without_remote_ref_compares_head_and_says_so(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr()], origin_sha=None, head_sha="c" * 40)
    out = _complete(mod)
    assert out and out["action"] == "block" and "worktree HEAD" in out["message"]


# (d) ci in progress
def test_ci_in_progress_is_refused_naming_wait(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr(status="IN_PROGRESS", conclusion="")])
    out = _complete(mod)
    assert out and out["action"] == "block"
    assert "MISSING 3/3" in out["message"] and "wait" in out["message"] and "gh pr checks 42" in out["message"]


def test_ci_missing_check_is_refused(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [_pr(name="lint")])
    out = _complete(mod)
    assert out and out["action"] == "block" and "no check named 'ci'" in out["message"]


# (e) waiver
def test_done_without_pr_waiver_passes(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path, body="Docs only.\ndone-without-pr: README edit, no code\n")
    calls = _fake_run(mod, monkeypatch, [])
    assert _complete(mod) is None and calls == []


def test_kill_switch_passes(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path); calls = _fake_run(mod, monkeypatch, [])
    monkeypatch.setenv("HERMES_KANBAN_PR_GATE_DISABLE", "1")
    assert _complete(mod) is None and calls == []


# (f) land card untouched
def test_release_land_card_is_left_to_the_kernel_gate(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path, title="[Release] Land PR #41 thing")
    calls = _fake_run(mod, monkeypatch, [])
    assert _complete(mod) is None and calls == []


# (g) tenant without ci_gate untouched
def test_tenant_without_ci_gate_is_untouched(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path, tenant="no-ci"); calls = _fake_run(mod, monkeypatch, [])
    assert _complete(mod) is None and calls == []


def test_scratch_and_other_worker_are_untouched(mod, tmp_path, monkeypatch):
    _task(mod, tmp_path, kind="scratch"); calls = _fake_run(mod, monkeypatch, [])
    assert _complete(mod) is None and calls == []
    _task(mod, tmp_path); monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other")
    assert _complete(mod) is None and calls == []


# (h) gh raises / times out -> fail open with warning
@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(cmd="gh", timeout=25), OSError("boom")])
def test_gh_failure_fails_open_with_warning(mod, tmp_path, caplog, exc, monkeypatch):
    _task(mod, tmp_path); _fake_run(mod, monkeypatch, [], raise_exc=exc)
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        assert _complete(mod) is None
    assert any("FAILING OPEN" in r.getMessage() for r in caplog.records)


def test_gh_nonzero_rc_fails_open(mod, tmp_path, caplog, monkeypatch):
    _task(mod, tmp_path)
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 4, "", "gh: not logged in"))
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        assert _complete(mod) is None
    assert any("FAILING OPEN" in r.getMessage() for r in caplog.records)
