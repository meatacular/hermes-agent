"""Tests for kanban-archive-guard, replaying the real 2026-09-18 incident.

Each positive has a paired control. The suite's own negative control is at the bottom: neuter
the mechanism and a test must go red, otherwise the suite proves nothing.
"""
import importlib.util, os, sqlite3
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ag", os.path.join(HERE, "__init__.py"))
ag = importlib.util.module_from_spec(spec); spec.loader.exec_module(ag)


@pytest.fixture
def board():
    c = sqlite3.connect(":memory:")
    c.executescript("""
      CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, body TEXT);
      CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
    """)
    return c


def add(c, cid, status, body=""):
    c.execute("INSERT INTO tasks(id,status,body) VALUES(?,?,?)", (cid, status, body))

def link(c, parent, child):
    c.execute("INSERT INTO task_links(parent_id,child_id) VALUES(?,?)", (parent, child))


# ---------- the incident, replayed ----------

def test_THE_INCIDENT_archiving_the_duplicate_would_ungate_its_child(board):
    """t_69f8ec57 archived as a duplicate of t_d9dcd7e9; t_34dd83a9 was parked behind it and
    promoted itself the moment the parent went archived."""
    add(board, "t_69f8ec57", "blocked"); add(board, "t_d9dcd7e9", "blocked")
    add(board, "t_34dd83a9", "todo"); add(board, "t_f967f9ac", "done")
    link(board, "t_69f8ec57", "t_34dd83a9")
    link(board, "t_f967f9ac", "t_34dd83a9")          # its only other parent is DONE
    assert ag.orphaned_children(board, "t_69f8ec57") == ["t_34dd83a9"]
    msg = ag.verdict("t_69f8ec57", conn=board)
    assert msg and "t_34dd83a9" in msg and "superseded-by" in msg

def test_CONTROL_a_child_with_a_live_other_parent_is_not_ungated(board):
    """If another parent is still non-terminal, the child stays gated and the archive is fine.
    Without this the guard would block every archive of any parent."""
    add(board, "t_dup", "blocked"); add(board, "t_live", "ready"); add(board, "t_child", "todo")
    link(board, "t_dup", "t_child"); link(board, "t_live", "t_child")
    assert ag.orphaned_children(board, "t_dup") == []
    assert ag.verdict("t_dup", conn=board) is None

def test_CONTROL_a_terminal_child_is_not_ungated(board):
    add(board, "t_dup", "blocked"); add(board, "t_child", "done")
    link(board, "t_dup", "t_child")
    assert ag.verdict("t_dup", conn=board) is None

def test_CONTROL_a_childless_card_archives_freely(board):
    add(board, "t_alone", "done")
    assert ag.verdict("t_alone", conn=board) is None


# ---------- the escape hatch ----------

def test_superseded_by_in_the_reason_allows_the_archive(board):
    add(board, "t_dup", "blocked"); add(board, "t_child", "todo")
    link(board, "t_dup", "t_child")
    assert ag.verdict("t_dup", reason="superseded-by: t_d9dcd7e9", conn=board) is None

def test_superseded_by_in_the_body_allows_the_archive(board):
    add(board, "t_dup", "blocked", body="header\nsuperseded-by: t_d9dcd7e9\nmore")
    add(board, "t_child", "todo"); link(board, "t_dup", "t_child")
    assert ag.verdict("t_dup", body="superseded-by: t_d9dcd7e9", conn=board) is None

def test_the_marker_must_name_a_card_id_not_just_any_words(board):
    """`superseded-by: a newer approach` is not a successor. Naming the card is the safe act."""
    add(board, "t_dup", "blocked"); add(board, "t_child", "todo")
    link(board, "t_dup", "t_child")
    assert ag.verdict("t_dup", reason="superseded-by: a newer approach", conn=board) is not None


# ---------- fail-open, always ----------

def test_unreadable_board_allows(monkeypatch):
    monkeypatch.setattr(ag, "_db_path", lambda: "/nonexistent/kanban.db")
    assert ag.verdict("t_x") is None

def test_a_broken_query_allows(board):
    board.execute("DROP TABLE task_links")
    assert ag.verdict("t_dup", conn=board) is None

def test_empty_card_id_allows(board):
    assert ag.verdict("", conn=board) is None


# ---------- the hook surface: trap 43 ----------

def test_hook_ignores_tools_that_are_not_archive():
    assert ag.on_pre_tool_call(tool_name="kanban_create", args={"task_id": "t_x"}) is None

def test_hook_accepts_the_upstream_kwarg_shape(board, monkeypatch, tmp_path):
    """Upstream dispatches invoke_hook('pre_tool_call', tool_name=..., args=...).
    kanban-skill-guard shipped INERT with 19 passing tests because it took `tool_input`."""
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(str(db))
    c.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, body TEXT);"
                    "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);")
    c.execute("INSERT INTO tasks VALUES('t_dup','blocked','')")
    c.execute("INSERT INTO tasks VALUES('t_child','todo','')")
    c.execute("INSERT INTO task_links VALUES('t_dup','t_child')")
    c.commit(); c.close()
    monkeypatch.setattr(ag, "_db_path", lambda: str(db))
    out = ag.on_pre_tool_call(tool_name="kanban_archive", args={"task_id": "t_dup"})
    assert out and out["action"] == "block" and "t_child" in out["message"]

def test_hook_handles_a_list_of_ids(board, monkeypatch, tmp_path):
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(str(db))
    c.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, body TEXT);"
                    "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);")
    c.execute("INSERT INTO tasks VALUES('t_ok','done','')")
    c.execute("INSERT INTO tasks VALUES('t_dup','blocked','')")
    c.execute("INSERT INTO tasks VALUES('t_child','todo','')")
    c.execute("INSERT INTO task_links VALUES('t_dup','t_child')")
    c.commit(); c.close()
    monkeypatch.setattr(ag, "_db_path", lambda: str(db))
    out = ag.on_pre_tool_call(tool_name="kanban_archive", args={"task_ids": ["t_ok", "t_dup"]})
    assert out and out["action"] == "block"

def test_hook_fails_open_on_an_unfamiliar_payload_shape():
    assert ag.on_pre_tool_call(tool_name="kanban_archive", args="not a dict") is None
    assert ag.on_pre_tool_call(tool_name="kanban_archive") is None


# ---------- the suite's own negative control ----------

def test_NEGATIVE_CONTROL_neutering_orphaned_children_makes_the_incident_test_pass_wrongly(board, monkeypatch):
    """With the detection removed, the incident case must stop being detected. If this control
    fails, `verdict` is not actually using `orphaned_children` and the suite is vacuous."""
    add(board, "t_69f8ec57", "blocked"); add(board, "t_34dd83a9", "todo")
    link(board, "t_69f8ec57", "t_34dd83a9")
    assert ag.verdict("t_69f8ec57", conn=board) is not None      # detected while intact
    monkeypatch.setattr(ag, "orphaned_children", lambda conn, cid: [])
    assert ag.verdict("t_69f8ec57", conn=board) is None          # and not, once neutered


# ---------- the malformed-marker diagnostic (found by running, not by reading) ----------

def test_a_malformed_marker_is_reported_not_silently_ignored(board):
    """Found 2026-09-18 by the behavioural probe: `superseded-by: t_new` produced the generic
    refusal, which reads as "the guard ignored my marker" rather than "your marker did not
    parse". `new` is not hex, so the strict regex correctly refused it - but silently."""
    add(board, "t_dup", "blocked"); add(board, "t_child", "todo")
    link(board, "t_dup", "t_child")
    msg = ag.verdict("t_dup", reason="superseded-by: t_new", conn=board)
    assert msg is not None
    assert "does not name a card id" in msg and "'t_new'" in msg

def test_a_valid_marker_still_has_no_diagnostic(board):
    """CONTROL: the note must appear ONLY when a marker is present and malformed."""
    add(board, "t_dup", "blocked"); add(board, "t_child", "todo")
    link(board, "t_dup", "t_child")
    assert ag.verdict("t_dup", reason="superseded-by: t_d9dcd7e9", conn=board) is None

def test_no_marker_at_all_gets_no_diagnostic(board):
    """CONTROL: an archive with no marker gets the plain refusal, not a confusing note about
    a marker the operator never wrote."""
    add(board, "t_dup", "blocked"); add(board, "t_child", "todo")
    link(board, "t_dup", "t_child")
    msg = ag.verdict("t_dup", conn=board)
    assert msg is not None and "does not name a card id" not in msg
