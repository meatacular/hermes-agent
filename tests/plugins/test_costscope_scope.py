"""Costscope regression tests: task title wins over shared cwd."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kdb

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "costscope" / "__init__.py"


def _load(path=PLUGIN, name="costscope_test"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ORIGINAL_SESSION_COST = kdb._session_cost_in_db
_ORIGINAL_PREDICATE = getattr(kdb, "costscope_predicate", None)
_ORIGINAL_CAP = getattr(kdb, "costscope_cap_equivalent", None)


@pytest.fixture()
def clean_costscope(monkeypatch):
    monkeypatch.setattr(kdb, "_session_cost_in_db", _ORIGINAL_SESSION_COST)
    if _ORIGINAL_PREDICATE is None:
        monkeypatch.delattr(kdb, "costscope_predicate", raising=False)
    else:
        monkeypatch.setattr(kdb, "costscope_predicate", _ORIGINAL_PREDICATE, raising=False)
    if _ORIGINAL_CAP is None:
        monkeypatch.delattr(kdb, "costscope_cap_equivalent", raising=False)
    else:
        monkeypatch.setattr(kdb, "costscope_cap_equivalent", _ORIGINAL_CAP, raising=False)
    monkeypatch.setattr(kdb, "modelark_cap_equivalent", None, raising=False)
    monkeypatch.setattr(kdb, "_costscope_test_original", _ORIGINAL_SESSION_COST, raising=False)


@pytest.fixture(autouse=True)
def reset_costscope_seams(monkeypatch):
    monkeypatch.setattr(kdb, "_session_cost_in_db", _ORIGINAL_SESSION_COST)
    monkeypatch.delattr(kdb, "costscope_predicate", raising=False)
    monkeypatch.delattr(kdb, "costscope_cap_equivalent", raising=False)
    monkeypatch.delattr(kdb, "modelark_cap_equivalent", raising=False)
    yield
    monkeypatch.setattr(kdb, "_session_cost_in_db", _ORIGINAL_SESSION_COST)
    monkeypatch.delattr(kdb, "costscope_predicate", raising=False)
    monkeypatch.delattr(kdb, "costscope_cap_equivalent", raising=False)
    monkeypatch.delattr(kdb, "modelark_cap_equivalent", raising=False)


def _ledger(path, workspace="/shared"):
    con = sqlite3.connect(path)
    con.execute("create table sessions(id text, title text, cwd text, estimated_cost_usd real)")
    con.execute("create table session_model_usage(session_id text, estimated_cost_usd real)")
    con.executemany("insert into sessions values (?, ?, ?, ?)", [
        ("own", "Work kanban task t_41d1a273 #1", workspace, 0.0118),
        ("foreign", "Work kanban task t_foreign #1", "/shared", 4.05),
    ])
    con.commit(); con.close()


def test_ac1_shared_workspace_task_scope_excludes_foreign_sessions(tmp_path, clean_costscope):
    db = tmp_path / "state.db"; _ledger(db)
    _load().install()
    assert kdb._session_cost_in_db(str(db), prefix_for="/shared", task_id="t_41d1a273") == 0.0118


def test_ac2_dedicated_workspace_task_scope_keeps_own_spend(tmp_path, clean_costscope):
    db = tmp_path / "state.db"; _ledger(db, "/dedicated")
    _load().install()
    assert kdb._session_cost_in_db(str(db), prefix_for="/dedicated", task_id="t_41d1a273") == 0.0118


def test_ac5_negative_control_current_kernel_overmatches_shared_cwd(tmp_path):
    db = tmp_path / "state.db"; _ledger(db)
    assert kdb._session_cost_in_db(str(db), prefix_for="/shared", task_id=None) > 4.0


def test_ac4_flat_rate_card_still_counts_own_overspend(tmp_path, clean_costscope):
    db = tmp_path / "state.db"; _ledger(db)
    con = sqlite3.connect(db); con.execute("UPDATE sessions SET estimated_cost_usd = 1.08 WHERE id = 'own'"); con.commit(); con.close()
    _load().install()
    got = kdb._session_cost_in_db(str(db), prefix_for="/shared", task_id="t_41d1a273")
    assert got == 1.08 and got > 1.00


def test_d6_ledger_read_failure_delegates_to_kernel_authority(tmp_path):
    mod = _load(); calls = []
    def kernel(path, prefix_for=None, task_id=None):
        calls.append((path, prefix_for, task_id)); return 7.25
    wrapped = mod._wrap_session_cost(kernel, type("Kdb", (), {"_escape_like": staticmethod(lambda s: s)})())
    missing = str(tmp_path / "missing.db")
    assert wrapped(missing, prefix_for="/shared", task_id="t_card") == 7.25
    assert calls == [(missing, "/shared", "t_card")]


def _ark_ledger(path):
    con = sqlite3.connect(path)
    con.execute("create table sessions(id text, title text, cwd text, estimated_cost_usd real)")
    con.execute("create table session_model_usage(session_id text, model text, billing_base_url text, cost_source text, estimated_cost_usd real, api_call_count int, input_tokens int, output_tokens int, cache_read_tokens int, cache_write_tokens int)")
    con.execute("insert into sessions values ('s1', 'Work kanban task t_order #1', null, 0.05)")
    con.execute("insert into session_model_usage values ('s1','deepseek-v4-flash-ga-260731', ?, 'modelark subscription', 0, 1, 1000000, 1000000, 0, 0)", ("https://ark.ap-southeast.bytepluses.com/api/coding/v3",))
    con.commit(); con.close()


def test_ac3_install_order_is_independent(tmp_path, clean_costscope, monkeypatch):
    """Both backend load orders add the Ark cap-equivalent exactly once."""
    from agent import usage_pricing as up
    monkeypatch.setattr(up, "resolve_billing_route", up.resolve_billing_route)
    monkeypatch.setattr(up, "estimate_usage_cost", up.estimate_usage_cost)
    modelark = _load(ROOT / "plugins" / "modelark-pricing" / "__init__.py", "modelark_order_test")
    values = []
    original_installed = modelark._INSTALLED
    monkeypatch.setattr(modelark, "_INSTALLED", original_installed)
    modelark._INSTALLED = False
    for first in ("costscope", "modelark"):
        db = tmp_path / f"{first}.db"; _ark_ledger(db)
        costscope = _load(name=f"costscope_{first}")
        modelark._INSTALLED = False
        if first == "costscope":
            costscope.install(); modelark.install()
        else:
            modelark.install(); costscope.install()
        values.append(kdb._cumulative_session_cost(str(db), None, task_id="t_order", all_ledgers=False))
    assert values[0] == pytest.approx(values[1])
    assert values[0] > 0.05
