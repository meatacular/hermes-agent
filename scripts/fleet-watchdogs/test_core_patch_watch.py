"""core-patch-watch against a synthetic repo — hermetic, and every positive paired
with the control that proves it can fail."""
import importlib.util
import json
import pathlib
import subprocess

import pytest

_spec = importlib.util.spec_from_file_location(
    "cpw", pathlib.Path(__file__).with_name("core-patch-watch.py"))
cpw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpw)


def sh(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout


def commit(repo, path, body, msg):
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    sh(repo, "add", "-A")
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", msg)
    return sh(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    sh(r, "init", "-q", "-b", "fleet")
    base = commit(r, "README.md", "x", "base")
    sh(r, "tag", "upd-catchup-applied-test")          # the attended base
    monkeypatch.setattr(cpw, "REPO", r)
    monkeypatch.setattr(cpw, "STATE", tmp_path / "state.json")
    return r, base


def run(args):
    import io, contextlib, sys
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["core-patch-watch.py", *args]
    try:
        with contextlib.redirect_stdout(buf):
            cpw.main()
    finally:
        sys.argv = argv
    return buf.getvalue()


def test_flags_a_kernel_commit(repo):
    r, base = repo
    commit(r, "hermes_cli/kanban_db.py", "print(1)", "feat: patch archive_task")
    out = run(["--since", base, "--no-state"])
    assert "KERNEL" in out and "hermes_cli/kanban_db.py" in out


def test_control_a_scripts_only_commit_is_SILENT(repo):
    """The real cf1e16e4c: the C2 auditor merge touched only scripts/. It was the one
    commit of that batch that was correctly shaped, and must not be flagged."""
    r, base = repo
    commit(r, "scripts/fleet-watchdogs/x.py", "print(1)", "feat: a watchdog")
    assert run(["--since", base, "--no-state"]) == ""


def test_control_a_plugin_commit_is_SILENT(repo):
    r, base = repo
    commit(r, "plugins/kanban-mint-guard/__init__.py", "print(1)", "feat: a plugin")
    assert run(["--since", base, "--no-state"]) == ""


def test_a_revert_is_not_a_patch(repo):
    r, base = repo
    commit(r, "hermes_cli/kanban_db.py", "print(1)", "revert(kanban): drop the core patch")
    assert run(["--since", base, "--no-state"]) == ""


def test_an_approved_kernel_change_is_allowed(repo):
    r, base = repo
    commit(r, "tools/kanban_tools.py", "print(1)",
           "feat: kernel change\n\ncore-patch-approved: Richie 2026-09-13")
    assert run(["--since", base, "--no-state"]) == ""


def test_tests_alone_are_not_a_kernel_change(repo):
    r, base = repo
    commit(r, "tests/hermes_cli/test_x.py", "print(1)", "test: add coverage")
    assert run(["--since", base, "--no-state"]) == ""


def test_first_run_baselines_silently_then_reports_what_comes_next(repo, tmp_path):
    r, base = repo
    commit(r, "hermes_cli/kanban_db.py", "print(1)", "feat: before the baseline")
    assert run([]) == "", "a first run must not dump all of history"
    assert json.loads((tmp_path / "state.json").read_text())["head"]
    commit(r, "tools/kanban_tools.py", "print(2)", "feat: after the baseline")
    out = run(["--no-state"])
    assert "tools/kanban_tools.py" in out
    assert "before the baseline" not in out


def test_it_leaves_no_index_lock(repo):
    """A stale .git/index.lock blocks the fleet's own merges. Learned 2026-09-12."""
    r, base = repo
    commit(r, "hermes_cli/kanban_db.py", "print(1)", "feat: patch")
    run(["--since", base, "--no-state"])
    assert not (r / ".git" / "index.lock").exists()


# --- working-tree arm (2026-09-14, runfix-20260914) --------------------------
# The commit scan cannot see a kernel edit that was never committed. On 2026-09-13 that
# was not hypothetical: t_dcaf62c1 left hermes_cli/profiles.py modified and uncommitted,
# this watchdog's state recorded an empty range, and it reported nothing.

def test_an_uncommitted_kernel_edit_is_reported(repo):
    r, base = repo
    (r / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (r / "hermes_cli" / "profiles.py").write_text("x = 1\n")
    out = run([])
    assert "UNCOMMITTED kernel change" in out
    assert "hermes_cli/profiles.py" in out


def test_the_first_porcelain_line_is_not_mangled(repo):
    """git() strips its output, eating the leading space of the FIRST status line.

    A fixed line[3:] parse therefore drops exactly one character from exactly one path —
    the first one — and the file silently vanishes from the report. This is the regression
    test for that: with only ONE dirty file there is nothing else to hide behind.
    """
    r, base = repo
    commit(r, "hermes_cli/profiles.py", "orig\n", "seed")
    (r / "hermes_cli" / "profiles.py").write_text("modified\n")
    assert cpw.uncommitted_kernel_files() == ["M hermes_cli/profiles.py"]


def test_control_a_dirty_NON_kernel_file_is_silent(repo):
    r, base = repo
    (r / "scripts").mkdir(parents=True, exist_ok=True)
    (r / "scripts" / "watch.py").write_text("x = 1\n")
    (r / "plugins").mkdir(parents=True, exist_ok=True)
    (r / "plugins" / "p.py").write_text("x = 1\n")
    out = run([])
    assert "UNCOMMITTED" not in out, out
    assert cpw.uncommitted_kernel_files() == []


def test_control_a_clean_tree_is_silent(repo):
    assert cpw.uncommitted_kernel_files() == []


def test_a_renamed_kernel_file_reports_its_destination(repo):
    r, base = repo
    commit(r, "hermes_cli/old.py", "x\n", "seed")
    sh(r, "mv", "hermes_cli/old.py", "hermes_cli/new.py")
    hits = cpw.uncommitted_kernel_files()
    assert any("hermes_cli/new.py" in h for h in hits), hits


def test_the_working_tree_arm_fires_on_a_FIRST_run_too(repo, tmp_path):
    """A live condition, not an event in a range: baselining must not swallow it."""
    r, base = repo
    (tmp_path / "state.json").unlink(missing_ok=True)
    (r / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (r / "hermes_cli" / "profiles.py").write_text("x = 1\n")
    out = run([])
    assert "UNCOMMITTED kernel change" in out, out
