"""Tests for the Fleet Live dashboard plugin (plugins/fleet-live/dashboard/plugin_api.py).

The plugin mounts at /api/plugins/fleet-live/ inside the dashboard's FastAPI app; here its
router is attached to a bare FastAPI instance so the REST surface can be exercised without the
dashboard.

Most of the risk in this plugin is in one place: it reads a worker's TUI transcript, which is a
rendering, not a protocol. So the parser tests below are written against text captured verbatim
from a live worker log (reasoning frames, the `┊` tool gutter, a diff), and they assert the two
properties that matter — that hard-wrapped prose comes back out as prose, and that nothing the
grammar fails to recognise is ever dropped.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "fleet-live" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_fleet_live_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


PLUGIN = _load()


# A verbatim slice of ~/.hermes/kanban/logs/<task>.log, CR line endings and all.
SAMPLE = (
    "┌─ Reasoning ──────────────────────────────────────────────────────────────────┐\r\n"
    "Confirmed: ids are `failure0`/`failure1` for the AC5 tests — so the PR body's claim\r\n"
    " \"with readable parameter IDs\" is false. m2 was not done.\r\n"
    "\r\n"
    "Let me patch the probe.\r\n"
    "└──────────────────────────────────────────────────────────────────────────────┘\r\n"
    "  ┊ 💻 preparing terminal…\r\n"
    "  ┊ 💻 $         sed -n '1,30p' backend/app/auth.py  0.1s\r\n"
    "  ┊ review diff\r\n"
    "a//tmp/probe_api.py → b//tmp/probe_api.py\r\n"
    "@@ -12,8 +12,10 @@\r\n"
    "-from backend.app import main  # noqa: E402\r\n"
    "+from backend.app import main, auth  # noqa: E402\r\n"
    "╭─ ☤ Hermes ───────────────────────────────────────────────────────────────────╮\r\n"
    "Both findings measured with the instrument proven able to fire.\r\n"
    "╰──────────────────────────────────────────────────────────────────────────────╯\r\n"
)


def _events(ops):
    """Collapse an op stream into finished events, the way the page does."""
    out, index = [], {}
    for op in ops:
        if op["op"] == "add":
            ev = {"id": op["id"], "t": op["t"], "label": op["label"], "text": op["text"],
                  "dur": op.get("dur", ""), "open": True}
            out.append(ev)
            index[op["id"]] = ev
        elif op["op"] == "app":
            index[op["id"]]["text"] += op["text"]
        elif op["op"] == "end":
            index[op["id"]]["open"] = False
    return out


# ── the parser ─────────────────────────────────────────────────────────────────────────────────

def test_parser_recognises_every_frame_in_a_real_transcript():
    events = _events(PLUGIN.Parser().feed(SAMPLE))
    kinds = [e["t"] for e in events]
    assert kinds.count("thought") == 1
    assert kinds.count("say") == 1
    assert kinds.count("tool") == 3          # preparing terminal, the sed, review diff
    assert kinds.count("raw") == 1           # the diff, collected as one block


def test_parser_unwraps_hard_wrapped_prose_but_keeps_real_breaks():
    thought = [e for e in _events(PLUGIN.Parser().feed(SAMPLE)) if e["t"] == "thought"][0]
    # The renderer broke this sentence at column 80; it must come back as one sentence.
    assert "the PR body's claim \"with readable parameter IDs\" is false" in thought["text"]
    # A blank line in the transcript is a real paragraph break and must survive as one.
    assert "\n\nLet me patch the probe." in thought["text"]


def test_parser_reads_the_tool_gutter():
    tools = [e for e in _events(PLUGIN.Parser().feed(SAMPLE)) if e["t"] == "tool"]
    sed = [t for t in tools if "sed" in t["text"]][0]
    assert sed["label"] == "💻"
    assert sed["dur"] == "0.1s"
    assert sed["text"] == "$ sed -n '1,30p' backend/app/auth.py"


def test_parser_never_drops_unrecognised_lines():
    raw = [e for e in _events(PLUGIN.Parser().feed(SAMPLE)) if e["t"] == "raw"][0]
    for line in ("a//tmp/probe_api.py", "@@ -12,8 +12,10 @@", "+from backend.app import main, auth"):
        assert line in raw["text"]


def test_parser_closes_each_frame():
    assert all(not e["open"] for e in _events(PLUGIN.Parser().feed(SAMPLE)))


def test_parser_streams_a_frame_that_is_still_being_written():
    """A reasoning block that has not closed yet must already be visible, and marked open —
    that is the difference between "streaming" and "appears when the agent finishes thinking"."""
    parser = PLUGIN.Parser()
    head = SAMPLE.split("Let me patch")[0]
    events = _events(parser.feed(head))
    assert events[0]["t"] == "thought" and events[0]["open"] is True
    assert "m2 was not done" in events[0]["text"]


def test_parser_is_identical_whatever_the_chunk_boundaries():
    whole = _events(PLUGIN.Parser().feed(SAMPLE))
    parser = PLUGIN.Parser()
    ops = []
    for i in range(0, len(SAMPLE), 37):            # a prime, so boundaries land mid-line
        ops.extend(parser.feed(SAMPLE[i:i + 37]))
    ops.extend(parser.feed("\n"))
    split = _events(ops)
    assert [(e["t"], e["text"]) for e in split] == [(e["t"], e["text"]) for e in whole]


# ── the log tail ───────────────────────────────────────────────────────────────────────────────

def test_log_feed_advances_by_byte_offset(tmp_path):
    path = tmp_path / "t_abcd1234.log"
    path.write_text(SAMPLE, encoding="utf-8")
    feed = PLUGIN.LogFeed(path)
    ops, offset, rotated = feed.poll()
    assert ops and not rotated
    assert offset == path.stat().st_size
    assert feed.poll() == ([], offset, False)      # nothing new, nothing re-sent

    with path.open("a", encoding="utf-8") as fh:
        fh.write("  ┊ 💻 $         pytest -q  2.0s\r\n")
    ops2, offset2, _ = feed.poll()
    assert [e["t"] for e in _events(ops2)] == ["tool"]
    assert offset2 > offset


def test_log_feed_reports_rotation_rather_than_reading_garbage(tmp_path):
    """The worker log rotates at 2 MiB. Every held offset is meaningless afterwards, so the
    feed must say so instead of seeking past the end of a fresh file."""
    path = tmp_path / "t_abcd1234.log"
    path.write_text(SAMPLE, encoding="utf-8")
    feed = PLUGIN.LogFeed(path)
    feed.poll()
    path.write_text("short\r\n", encoding="utf-8")
    ops, offset, rotated = feed.poll()
    assert rotated is True and offset == 0 and ops == []


def test_log_feed_survives_a_multibyte_character_split_across_two_reads(tmp_path):
    path = tmp_path / "t_abcd1234.log"
    blob = SAMPLE.encode("utf-8")
    cut = blob.index("—".encode("utf-8")) + 1       # mid-em-dash
    path.write_bytes(blob[:cut])
    feed = PLUGIN.LogFeed(path)
    feed.poll()
    path.write_bytes(blob)
    feed.poll()
    feed.poll()
    text = "".join(e["text"] for e in _events(PLUGIN.Parser().feed(blob.decode("utf-8"))))
    assert "—" in text


# ── the HTTP surface ───────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "kanban" / "logs").mkdir(parents=True)
    (home / "runtime").mkdir(parents=True)
    monkeypatch.setattr(PLUGIN, "_default_root", lambda: home)
    app = FastAPI()
    app.include_router(PLUGIN.router, prefix="/api/plugins/fleet-live")
    return TestClient(app), home


def test_panes_is_empty_and_calm_on_a_fleet_with_nothing_running(client):
    api, _ = client
    body = api.get("/api/plugins/fleet-live/panes").json()
    assert body["panes"] == [] and body["count"] == 0


def test_panes_ignores_a_lease_whose_process_is_gone(client):
    api, home = client
    (home / "runtime" / "active_sessions.json").write_text(json.dumps({"entries": [
        {"session_id": "20260919_002802_de1be2", "pid": 999999, "process_start_time": 1.0,
         "surface": "cli", "started_at": 1.0}]}), encoding="utf-8")
    assert api.get("/api/plugins/fleet-live/panes").json()["panes"] == []


def test_tail_returns_parsed_backlog_and_a_resumable_offset(client):
    api, home = client
    log = home / "kanban" / "logs" / "t_abcd1234.log"
    log.write_text(SAMPLE, encoding="utf-8")
    body = api.get("/api/plugins/fleet-live/panes/k:t_abcd1234/tail").json()
    assert body["offset"] == log.stat().st_size
    assert body["start"] == 0 and body["more"] is False
    assert any(e["t"] == "thought" for e in _events(body["ops"]))


def test_tail_walking_backwards_drops_the_partial_first_line(client):
    api, home = client
    log = home / "kanban" / "logs" / "t_abcd1234.log"
    log.write_text(SAMPLE * 6, encoding="utf-8")
    size = log.stat().st_size
    body = api.get(f"/api/plugins/fleet-live/panes/k:t_abcd1234/tail?bytes=1024&before={size}").json()
    assert body["start"] > 0 and body["more"] is True
    text = "".join(e["text"] for e in _events(body["ops"]))
    assert "�" not in text          # a byte-aligned cut must not leak a replacement char


def test_tail_of_a_task_that_never_spawned_is_empty_not_an_error(client):
    api, _ = client
    body = api.get("/api/plugins/fleet-live/panes/k:t_deadbeef/tail").json()
    assert body["ops"] == [] and body["offset"] == 0


@pytest.mark.parametrize("pane_id", [
    "k:../../etc/passwd", "k:not-a-task", "k:t_../../x", "s:../x:y", "s:bob:../../secret", "x:1", "",
])
def test_a_pane_id_is_never_a_path(pane_id):
    """Pane ids are the only client-controlled input that reaches the filesystem, so the
    validator is asserted directly rather than through a URL the client library may normalise."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        PLUGIN._feed_for(pane_id)
    assert exc.value.status_code == 400


def test_card_endpoint_refuses_a_non_kanban_pane(client):
    api, _ = client
    assert api.get("/api/plugins/fleet-live/panes/s:bob:20260919_002802_de1be2/card").status_code == 404


# ── the WebSocket gate ─────────────────────────────────────────────────────────────────────────

def test_the_stream_socket_is_gated_by_the_dashboard_auth_check(monkeypatch, client):
    """HTTP middleware does not run for a WS upgrade, so this endpoint carries its own gate.
    If that ever stops being called, the page becomes an unauthenticated transcript firehose."""
    api, _ = client
    monkeypatch.setattr(PLUGIN, "_ws_authorized", lambda ws: False)
    with pytest.raises(Exception):
        with api.websocket_connect("/api/plugins/fleet-live/stream"):
            pass


def test_the_stream_socket_opens_with_the_gate_satisfied(monkeypatch, client):
    api, _ = client
    monkeypatch.setattr(PLUGIN, "_ws_authorized", lambda ws: True)
    with api.websocket_connect("/api/plugins/fleet-live/stream") as ws:
        first = ws.receive_json()
        assert first["type"] == "panes" and first["panes"] == []


def test_the_stream_delivers_what_a_worker_writes_after_the_subscription(monkeypatch, client):
    """End to end on the transport: subscribe to a pane, append to its log, read the thought
    back off the socket. This is the behaviour the page exists for."""
    api, home = client
    monkeypatch.setattr(PLUGIN, "_ws_authorized", lambda ws: True)
    log = home / "kanban" / "logs" / "t_abcd1234.log"
    log.write_text("", encoding="utf-8")
    with api.websocket_connect("/api/plugins/fleet-live/stream") as ws:
        ws.receive_json()                                    # the opening pane list
        ws.send_json({"sub": ["k:t_abcd1234"], "from": {"k:t_abcd1234": {"offset": 0, "seq": 0}}})
        log.write_text(SAMPLE, encoding="utf-8")
        for _ in range(40):                                  # ~10s of 250ms polls, in practice one
            msg = ws.receive_json()
            if msg.get("type") == "feed":
                break
        assert msg["type"] == "feed" and msg["pane"] == "k:t_abcd1234"
        assert any(e["t"] == "thought" for e in _events(msg["ops"]))
        assert msg["offset"] == log.stat().st_size


# ── the board: timeline, dependencies, the queue ───────────────────────────────────────────────

_BOARD_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
  priority INTEGER DEFAULT 0, tenant TEXT, project_id TEXT, branch_name TEXT, max_cost REAL,
  created_at INTEGER, started_at INTEGER, completed_at INTEGER, consecutive_failures INTEGER DEFAULT 0,
  block_kind TEXT, last_failure_error TEXT);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id));
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
  kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, status TEXT,
  outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT, worker_pid INTEGER,
  claim_expires INTEGER, last_heartbeat_at INTEGER);
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT,
  body TEXT, created_at INTEGER);
"""


@pytest.fixture
def board(client):
    """A small board on the temporary home, so these tests never read the live fleet's."""
    import sqlite3

    api, home = client
    conn = sqlite3.connect(home / "kanban.db")
    conn.executescript(_BOARD_SCHEMA)

    def task(tid, title, status, created, assignee="bob"):
        conn.execute("INSERT INTO tasks (id,title,body,assignee,status,created_at,tenant) "
                     "VALUES (?,?,?,?,?,?,'weroll')", (tid, title, "the brief for " + tid, assignee, status, created))

    task("t_aaaa1111", "Parent still open", "running", 1000)
    task("t_bbbb2222", "Parent already finished", "done", 900)
    task("t_cccc3333", "Child waiting on one open parent", "todo", 3000)
    task("t_dddd4444", "Ready and unblocked, newest", "ready", 5000)
    task("t_eeee5555", "Triage, older", "triage", 2000)
    task("t_ffff6666", "Ready but blocked by the open parent", "ready", 4000)
    conn.execute("INSERT INTO task_links VALUES ('t_aaaa1111','t_cccc3333')")
    conn.execute("INSERT INTO task_links VALUES ('t_bbbb2222','t_cccc3333')")
    conn.execute("INSERT INTO task_links VALUES ('t_aaaa1111','t_ffff6666')")

    # A card's real history, including a repeated claim that must collapse to one step.
    for kind, payload, at in [
        ("created", json.dumps({"status": "todo"}), 1000),
        ("dependency_wait", json.dumps({"reason": "parent_not_done"}), 1010),
        ("promoted", None, 1100),
        ("claimed", json.dumps({"run_id": 1}), 1200),
        ("heartbeat", None, 1260),                              # noise, never a step
        ("claimed", json.dumps({"run_id": 2}), 1300),           # a retry, not progress
        ("review_requested", json.dumps({"summary": "ready"}), 1400),
        ("changes_requested", None, 1450),                      # sent back — a real step
        ("claimed", json.dumps({"run_id": 3}), 1500),
    ]:
        conn.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES ('t_aaaa1111',?,?,?)",
                     (kind, payload, at))
    conn.execute("INSERT INTO task_comments (task_id,author,body,created_at) "
                 "VALUES ('t_aaaa1111','rodge','needs a test',1500)")
    conn.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at,summary) "
                 "VALUES ('t_aaaa1111','bob','done','completed',1200,1400,'first attempt')")
    conn.commit()
    conn.close()
    return api


def test_timeline_collapses_a_retry_into_one_step(board):
    tl = board.get("/api/plugins/fleet-live/cards/t_aaaa1111").json()["card"]["timeline"]
    cols = [s["col"] for s in tl["steps"]]
    # The second claim is a retry in the column the card is already in and must not read as
    # progress; being sent back from review to ready is a real move and must.
    assert cols == ["todo", "scheduled", "ready", "running", "review", "ready", "running"]
    assert tl["count"] == 7


def test_timeline_knows_what_is_still_to_come(board):
    tl = board.get("/api/plugins/fleet-live/cards/t_aaaa1111").json()["card"]["timeline"]
    assert tl["current"] == "running"
    assert tl["remaining"] == ["review", "done"]
    assert tl["pipeline"] == ["triage", "todo", "ready", "running", "review", "done"]


def test_a_detour_does_not_cost_a_card_its_place(board):
    """A blocked card has not gone backwards — its remaining road is measured from the furthest
    point it reached, and the detour is reported beside the road rather than inside it."""
    tl = PLUGIN._timeline("t_aaaa1111", "blocked")
    assert tl["detour"] == "blocked"
    assert "blocked" not in tl["pipeline"]
    assert tl["remaining"] == ["done"]                # measured from `review`, the furthest reached


def test_a_card_sent_back_must_pass_the_stations_again(board):
    """The opposite of a detour: `changes_requested` really is a step backwards, so review is
    owed again. Measuring from the furthest point ever reached would promise a skipped step."""
    tl = PLUGIN._timeline("t_aaaa1111", "ready")
    assert tl["remaining"] == ["running", "review", "done"]


def test_dependencies_count_only_the_unfinished_parents(board):
    card = board.get("/api/plugins/fleet-live/cards/t_cccc3333").json()["card"]
    assert {p["id"] for p in card["deps"]["parents"]} == {"t_aaaa1111", "t_bbbb2222"}
    assert card["deps"]["unmet"] == 1                 # the `done` parent does not hold it up
    assert card["deps"]["children"] == []


def test_a_card_knows_what_is_waiting_on_it(board):
    card = board.get("/api/plugins/fleet-live/cards/t_aaaa1111").json()["card"]
    assert {c["id"] for c in card["deps"]["children"]} == {"t_cccc3333", "t_ffff6666"}


def test_a_card_carries_its_brief_comments_and_attempts(board):
    card = board.get("/api/plugins/fleet-live/cards/t_aaaa1111").json()["card"]
    assert card["body"].endswith("t_aaaa1111")
    assert [c["author"] for c in card["comments"]] == ["rodge"]
    assert [r["outcome"] for r in card["runs"]] == ["completed"]


def test_an_unknown_card_is_404_and_a_malformed_id_is_400(board):
    assert board.get("/api/plugins/fleet-live/cards/t_99999999").status_code == 404
    assert board.get("/api/plugins/fleet-live/cards/not-a-task").status_code == 400


def test_the_queue_puts_unblocked_work_first_then_newest(board):
    """Ordering is the whole product here: nothing-blocking first, then how close the column is
    to a worker, then most recent."""
    q = board.get("/api/plugins/fleet-live/queue").json()["queue"]
    ids = [c["id"] for c in q]
    # t_dddd4444 (ready, unblocked, newest) beats t_eeee5555 (triage, unblocked);
    # both beat t_ffff6666 and t_cccc3333, which are waiting on an unfinished parent.
    assert ids.index("t_dddd4444") < ids.index("t_eeee5555")
    assert ids.index("t_eeee5555") < ids.index("t_ffff6666")
    assert ids.index("t_eeee5555") < ids.index("t_cccc3333")


def test_the_queue_names_what_is_blocking_each_card(board):
    q = {c["id"]: c for c in board.get("/api/plugins/fleet-live/queue").json()["queue"]}
    assert [p["id"] for p in q["t_ffff6666"]["blocked_by"]] == ["t_aaaa1111"]
    assert q["t_dddd4444"]["blocked_by"] == []
    assert q["t_cccc3333"]["deps"]["children_count"] == 0


def test_the_queue_excludes_work_already_running_or_finished(board):
    q = [c["id"] for c in board.get("/api/plugins/fleet-live/queue").json()["queue"]]
    assert "t_aaaa1111" not in q          # running
    assert "t_bbbb2222" not in q          # done
