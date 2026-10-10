"""Per-worker cap: the allowance is personal, and so is the one extension.

Every test drives the real module against a real sqlite board — no mocked kernel — because the
thing under test is precisely how the plugin and the kernel's own cap interact.
"""
import importlib.util
import pathlib
import sqlite3

import pytest

_spec = importlib.util.spec_from_file_location(
    "cap_pw", pathlib.Path(__file__).with_name("__init__.py"))
cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cap)


class FakeKdb:
    """Only the surface the plugin touches."""
    def __init__(self, base=1.00, hard=1.50):
        self._b, self._h = base, hard
    def resolve_default_max_cost(self):
        return self._b
    def resolve_max_cost_hard_ceiling(self):
        return self._h


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, status TEXT, max_cost REAL)")
    c.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, body TEXT)")
    return c


def card(conn, tid, assignee, cap_=1.00, status="running"):
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", (tid, assignee, status, cap_))
    conn.commit()


def ext(conn, tid, who, old=1.00, new=1.50):
    conn.execute("INSERT INTO task_comments (task_id, body) VALUES (?,?)",
                 (tid, f"cost-extension: {who} ${old:.2f} -> ${new:.2f} by rodge"))
    conn.commit()


def cap_of(conn, tid):
    return conn.execute("SELECT max_cost FROM tasks WHERE id=?", (tid,)).fetchone()[0]


# ── the personal allowance ────────────────────────────────────────────────────────────────
def test_an_extension_does_not_travel_to_the_next_worker(conn):
    """The 2026-09-14 shape: rodge is extended, bob inherits the raise."""
    card(conn, "t_1", "rodge", 1.50)
    ext(conn, "t_1", "rodge")
    assert cap.extended_for(conn, "t_1", "rodge") is True
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_1") == 1.50, "the worker who WAS extended keeps their raise"

    conn.execute("UPDATE tasks SET assignee='bob' WHERE id='t_1'"); conn.commit()
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_1") == 1.00, "bob starts at his own base, not rodge's raise"


def test_each_worker_gets_their_own_extension(conn):
    card(conn, "t_2", "rodge", 1.00)
    ext(conn, "t_2", "rodge")
    conn.execute("UPDATE tasks SET assignee='bob' WHERE id='t_2'"); conn.commit()
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_2") == 1.00
    ext(conn, "t_2", "bob")
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_2") == 1.50, "bob's own extension applies to bob"


def test_a_worker_is_extended_only_once(conn):
    card(conn, "t_3", "rodge", 1.00)
    assert cap.extended_for(conn, "t_3", "rodge") is False
    ext(conn, "t_3", "rodge")
    assert cap.extended_for(conn, "t_3", "rodge") is True


def test_an_unnamed_legacy_extension_counts_for_nobody(conn):
    """Pre-2026-09-15 comments have no assignee token. Safe direction: base, not someone else's."""
    card(conn, "t_4", "rodge", 1.50)
    conn.execute("INSERT INTO task_comments (task_id, body) VALUES (?,?)",
                 ("t_4", "cost-extension: $1.00 -> $1.50 by rodge"))
    conn.commit()
    assert cap.extended_for(conn, "t_4", "rodge") is False
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_4") == 1.00


# ── things it must not touch ──────────────────────────────────────────────────────────────
def test_a_cap_above_the_hard_ceiling_is_left_alone(conn):
    """Only Richie can go past the ceiling; this must never undo that."""
    card(conn, "t_5", "bob", 5.00)
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_5") == 5.00


def test_only_running_cards_are_normalised(conn):
    card(conn, "t_6", "bob", 1.50, status="blocked")
    cap._normalise_caps(conn, FakeKdb())
    assert cap_of(conn, "t_6") == 1.50


def test_an_unassigned_card_is_not_extended_by_accident(conn):
    card(conn, "t_7", "", 1.00)
    assert cap.extended_for(conn, "t_7", "") is False


def test_case_is_not_a_way_past_the_once_rule(conn):
    card(conn, "t_8", "Rodge", 1.00)
    ext(conn, "t_8", "rodge")
    assert cap.extended_for(conn, "t_8", "Rodge") is True


# ── controls ──────────────────────────────────────────────────────────────────────────────
def test_control_the_normaliser_is_not_vacuous(conn):
    """If it did nothing, the first test would pass for the wrong reason."""
    card(conn, "t_9", "bob", 1.50)
    changed = cap._normalise_caps(conn, FakeKdb())
    assert changed and changed[0][0] == "t_9" and changed[0][2] == 1.00


def test_control_a_correct_cap_is_left_untouched(conn):
    card(conn, "t_10", "bob", 1.00)
    assert cap._normalise_caps(conn, FakeKdb()) == [], "no write when the cap is already right"


def test_enforce_wrapper_delegates_and_survives_a_broken_board():
    """A cap normaliser must never break the dispatcher tick."""
    calls = []
    class Boom:
        def execute(self, *a, **k):
            raise sqlite3.Error("simulated")
    orig = lambda conn, **kw: calls.append("delegated") or []
    wrapped = cap._wrap_enforce(orig, FakeKdb())
    assert wrapped(Boom()) == []
    assert calls == ["delegated"], "the kernel still ran"


def test_install_is_idempotent(monkeypatch):
    import types, sys
    fake = types.ModuleType("hermes_cli.kanban_db")
    fake.enforce_max_cost = lambda conn, **kw: []
    fake.set_task_max_cost = lambda *a, **k: 1.0
    fake.resolve_default_max_cost = lambda: 1.0
    fake.resolve_max_cost_hard_ceiling = lambda: 1.5
    pkg = types.ModuleType("hermes_cli"); pkg.kanban_db = fake
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake)
    first = cap.install()
    assert sorted(first) == ["enforce_max_cost", "set_task_max_cost"]
    assert cap.install() == [], "second install wraps nothing"
