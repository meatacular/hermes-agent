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
