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
    assert claim["pid"] == os.getpid()
    assert claim["reason"] == "cost_cap"


def test_ac2_dead_holder_is_reaped_on_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CLAIM_DIR", tmp_path)
    path = tmp_path / "t_card.claim"
    path.write_text(json.dumps({"pid": 99999999, "created_at": time.time()}))
    assert mod._claim_overwatch("t_card", "transient") is True
    assert json.loads(path.read_text())["pid"] == os.getpid()


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
