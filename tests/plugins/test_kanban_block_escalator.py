import importlib.util
import json
import os
import signal
import sqlite3
import time
from pathlib import Path


PLUGIN = Path(__file__).parents[2] / "plugins" / "kanban-block-escalator" / "__init__.py"
spec = importlib.util.spec_from_file_location("kanban_block_escalator", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_ac1_concurrent_claim_allows_one_and_records_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path)
    assert mod._claim_overwatch("t_card", "cost_cap") is True
    assert mod._claim_overwatch("t_card", "needs_input") is False
    claim = json.loads((tmp_path / "t_card.claim").read_text())
    # pid 0 = a RESERVATION (2026-09-16, t_2da01ffe): _claim_overwatch takes the slot, _spawn
    # hands it to the child it starts. Holding it here as os.getpid() is what made the old lease
    # name the blocked WORKER, whose life is seconds long.
    assert claim["pid"] == 0
    assert claim["state"] == "reserved"
    assert claim["reason"] == "cost_cap"


def test_ac2_dead_holder_is_reaped_on_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path)
    path = tmp_path / "t_card.claim"
    path.write_text(json.dumps({"pid": 99999999, "created_at": time.time()}))
    assert mod._claim_overwatch("t_card", "transient") is True
    assert json.loads(path.read_text())["pid"] == 0


def test_ac1_skip_is_recorded_with_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path)
    path = tmp_path / "t_card.claim"
    path.write_text(json.dumps({"pid": os.getpid(), "created_at": time.time(), "reason": "cost_cap"}))
    recorded = []
    monkeypatch.setattr(mod, "_record_claim_skip", lambda task_id, holder, reason: recorded.append((task_id, holder, reason)))
    assert mod._claim_overwatch("t_card", "needs_input") is False
    assert recorded == [("t_card", os.getpid(), "needs_input")]


def test_no_dead_review_handoff_guard_remains():
    """2026-09-15, card t_ec55f36f. `review_handoff_reclaim_allowed` was defined here and tested
    here and called from NOWHERE — a guard that reads as installed and governs nothing. It is gone,
    the kernel's `_retry_status_for_run` is what actually keeps a reclaimed review run in the review
    phase, and the reassignment constraint now lives in AUTHORITY where overwatch reads it.

    This test replaces the three assertions that used to exercise the dead function, and asserts the
    two things that must stay true instead: the function has not crept back, and AUTHORITY still
    carries the constraint that replaced it."""
    assert not hasattr(mod, "review_handoff_reclaim_allowed"), (
        "the dead guard is back — if it is needed it must have a CALL SITE, not just a test")
    assert "REASSIGNMENT:" in mod.AUTHORITY
    assert "review_requested" in mod.AUTHORITY
    assert "assignee-mismatch-watch" in mod.AUTHORITY


# --- 2026-09-16, card t_2da01ffe: one escalation per block -----------------------
#
# Richie's ruling ("option A" on all three sub-decisions): the ceiling counts DECISIONS (not
# sessions spawned) and it is AUTHOR-AGNOSTIC; the per-card lease must name the process that
# actually holds it. The tests below are the ones that fail on the code as it stood before this
# change — they assert the DEFECT was real, which is the only way a fix is evidence of anything
# (fleet-control-change rule 7). The counter tests live here rather than beside the other
# escalator contract tests because `tests/hermes_cli/test_kanban_block_escalator.py` is claimed
# by the unapplied `costscope-20260916` change; two writers on one file is how a fix disappears.


def _board(tmp_path, rows, comments=()):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, block_kind TEXT, "
                "block_recurrences INTEGER, assignee TEXT, created_by TEXT, tenant TEXT, max_cost REAL, "
                "workspace_kind TEXT, workspace_path TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, error TEXT)")
    con.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER)")
    for r in rows:
        con.execute("INSERT INTO tasks (id,title,status,block_kind,block_recurrences,assignee,max_cost) VALUES (?,?,?,?,?,?,?)", r)
    for c in comments:
        con.execute("INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,0)", c)
    con.commit()
    con.close()
    return db


def test_ceiling_counts_mis_signed_decisions(monkeypatch, tmp_path):
    """AC2. The measured case is `t_b6ebc5ec`: the poller-woken actor ruled on the card, wrote
    its ruling with a tool that signs `os.environ.get("HERMES_PROFILE") or "worker"`, and the old
    counter — `body LIKE 'overwatch:%' AND author = 'default'` — read 1 where two decisions
    existed. A ceiling that can be dodged by a missing environment variable is not a ceiling."""
    db = _board(tmp_path, [("t_sig", "x", "blocked", "needs_input", 0, "bob", 1.0)],
                comments=[("t_sig", "worker", "overwatch: unblocked, reassigned bob, review reopened"),
                          ("t_sig", "worker", "overwatch: extended the cap by $0.50")])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    hard, why = mod.is_hard_stop("t_sig", {"block_kind": "needs_input"})
    assert hard, "two `overwatch:` decisions are two decisions, whoever the comment is signed by"
    assert "2 overwatch decisions" in why


def test_one_mis_signed_decision_is_not_a_hard_stop(monkeypatch, tmp_path):
    """Negative control for AC2's widening: the limit is still TWO decisions, so one ruling —
    mis-signed or not — leaves the ordinary overwatch path open."""
    db = _board(tmp_path, [("t_one", "x", "blocked", "needs_input", 0, "bob", 1.0)],
                comments=[("t_one", "worker", "overwatch: reassigned to rodge")])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    assert mod.is_hard_stop("t_one", {"block_kind": "needs_input"})[0] is False


def test_marker_must_open_the_comment(monkeypatch, tmp_path):
    """The other edge of AC2. `overwatch:` counts by PREFIX, not by mention — two comments that
    merely discuss the marker are not two decisions. Widening the author filter must not widen
    this too, or every card whose thread quotes the convention reaches its ceiling."""
    db = _board(tmp_path, [("t_quote", "x", "blocked", "needs_input", 0, "bob", 1.0)],
                comments=[("t_quote", "worker", "the `overwatch:` marker is how the board counts"),
                          ("t_quote", "default", "  overwatch: indented, so not a ruling")])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    assert mod.is_hard_stop("t_quote", {"block_kind": "needs_input"})[0] is False


def test_lease_holder_is_the_spawned_child_and_is_released_when_it_exits(monkeypatch, tmp_path):
    """AC3, the whole point of the lease, tested against a real process.

    `on_block` runs INSIDE the blocked worker, so `_claim_overwatch` used to record
    `os.getpid()` — the worker's pid. The worker is reaped seconds later while the assessment it
    triggered runs for minutes, so the claim stopped protecting the card almost immediately
    (`t_b6ebc5ec`: worker dead at 13:51:55, its overwatch still working). A lease whose holder is
    not the process doing the work protects nothing. Red before this change on both assertions:
    the pid was the test process's, and the slot never came back while that process lived.
    """
    db = _board(tmp_path, [("t_lease", "Build X", "blocked", "capability", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path / "claims")
    sleeper = tmp_path / "fake-hermes.sh"
    sleeper.write_text("#!/bin/sh\nsleep 2\n")
    sleeper.chmod(0o755)
    monkeypatch.setattr(mod, "_hermes_bin", lambda: str(sleeper))
    real_popen = mod.subprocess.Popen
    procs = []

    def recording_popen(argv, **kw):
        proc = real_popen(argv, **kw)
        procs.append(proc)
        return proc

    monkeypatch.setattr(mod.subprocess, "Popen", recording_popen)
    mod.on_block(task_id="t_lease", assignee="bob", reason="workspace is empty")
    assert len(procs) == 1
    child = procs[0]
    claim = json.loads((tmp_path / "claims" / "t_lease.claim").read_text())
    assert claim["pid"] == child.pid, "the lease must name the spawned child, not the worker"
    assert claim["pid"] != os.getpid()
    # While the child lives the card's slot is HELD: a second fault-signal block is refused.
    assert mod._claim_overwatch("t_lease", "needs_input") is False
    child.wait(timeout=30)
    # And the lease ends when its holder ends — not 15 minutes later on the TTL.
    assert mod._claim_overwatch("t_lease", "needs_input") is True


def test_failed_spawn_releases_the_lease(monkeypatch, tmp_path):
    """AC3's other half: `_release_overwatch` is called from the paths that need it.

    A spawn that raised used to leave its claim behind, so the card's slot was held by a worker
    pid for the rest of the worker's life — the card could not be escalated again by anything.
    """
    db = _board(tmp_path, [("t_fail", "Build X", "blocked", "capability", 0, "bob", 1.0)])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path / "claims")

    def exploding_popen(argv, **kw):
        raise OSError("cannot execute hermes")

    monkeypatch.setattr(mod.subprocess, "Popen", exploding_popen)
    mod.on_block(task_id="t_fail", assignee="bob", reason="workspace is empty")
    assert not (tmp_path / "claims" / "t_fail.claim").exists(), "a failed spawn leaked its claim"
    assert mod._claim_overwatch("t_fail", "capability") is True


def test_reservation_is_reaped_once_its_grace_expires(monkeypatch, tmp_path):
    """A reservation whose child never arrived (the process died between reserve and spawn) must
    not hold a card's slot for the full 15-minute TTL — but must hold it long enough for the
    handoff, which is the reason it cannot simply be ignored."""
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path / "claims")
    (tmp_path / "claims").mkdir()
    path = tmp_path / "claims" / "t_res.claim"
    now = time.time()
    path.write_text(json.dumps({"pid": 0, "created_at": now, "reason": "spawn", "state": "reserved"}))
    assert mod._claim_overwatch("t_res", "capability") is False, "a fresh reservation is held"
    path.write_text(json.dumps({"pid": 0, "created_at": now - mod.CLAIM_SPAWN_GRACE_SECONDS - 1,
                                "reason": "spawn", "state": "reserved"}))
    assert mod._claim_overwatch("t_res", "capability") is True, "a stale reservation is reaped"
