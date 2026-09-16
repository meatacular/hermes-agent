"""Costscope regression tests: task title wins over shared cwd."""
import importlib.util
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as kdb

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "costscope" / "__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("costscope_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ledger(path):
    con = sqlite3.connect(path)
    con.execute("create table sessions(id text, title text, cwd text, estimated_cost_usd real)")
    con.execute("create table session_model_usage(session_id text, estimated_cost_usd real)")
    con.executemany("insert into sessions values (?, ?, ?, ?)", [
        ("own", "Work kanban task t_41d1a273 #1", "/shared", 0.0118),
        ("foreign", "Work kanban task t_foreign #1", "/shared", 4.05),
    ])
    con.commit(); con.close()


def test_ac1_shared_workspace_task_scope_excludes_foreign_sessions(tmp_path):
    db = tmp_path / "state.db"; _ledger(db)
    mod = _load(); mod.install()
    got = kdb._session_cost_in_db(str(db), prefix_for="/shared", task_id="t_41d1a273")
    assert got == 0.0118


def test_ac2_dedicated_workspace_task_scope_keeps_own_spend(tmp_path):
    db = tmp_path / "state.db"; _ledger(db)
    mod = _load(); mod.install()
    got = kdb._session_cost_in_db(str(db), prefix_for="/dedicated", task_id="t_41d1a273")
    assert got == 0.0118


def test_ac5_negative_control_current_kernel_overmatches_shared_cwd(tmp_path):
    db = tmp_path / "state.db"; _ledger(db)
    # Preserve proof that the unpatched kernel path is the defect this plugin fixes.
    got = kdb._session_cost_in_db(str(db), prefix_for="/shared", task_id=None)
    assert got > 4.0
