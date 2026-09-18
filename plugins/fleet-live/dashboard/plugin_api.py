"""Fleet Live — backend. Mounted at /api/plugins/fleet-live/ behind the dashboard's own auth.

One page that answers "what is every agent thinking, right now". Two feeds, one shape:

* **kanban workers** — the dispatcher spawns each worker as its own CLI process and tees its
  stdout to ``~/.hermes/kanban/logs/<task_id>.log``. That file carries the full TUI transcript,
  reasoning boxes included, and it is written as the tokens arrive (measured: 4-10 KB per 20 s on
  a live card). Tailing it by byte offset is the only sub-second source of chain of thought on
  this fleet, and it costs nothing — a ``stat`` and a ``pread`` per poll.
* **everything else** (desktop / CLI / Slack sessions that are not card work) — no log file
  exists, so the feed is the ``messages`` table of that profile's ``state.db``, tailed by rowid.
  Rows land per tool round rather than per token, so it is seconds-granular, not typewriter.

Both are normalised into the same append-only op stream (``add`` / ``app`` / ``end``) carrying a
resumable offset, so the page can scroll back, hold its place, and re-attach to the live tail
without re-reading what it already has.

Read-only by construction: every sqlite handle is opened ``mode=ro`` and no route writes
anything. Termination and reclaim already live on the kanban plugin; this page does not
duplicate them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status as http_status

log = logging.getLogger(__name__)
router = APIRouter()

# ── optional imports; the plugin must still load in a bare-FastAPI test harness ────────────────
try:
    import psutil as _psutil
except Exception:  # pragma: no cover - psutil ships with Hermes
    _psutil = None  # type: ignore[assignment]

try:
    from hermes_constants import get_default_hermes_root as _default_root
except Exception:  # pragma: no cover
    def _default_root() -> Path:  # type: ignore[misc]
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


# ── paths ──────────────────────────────────────────────────────────────────────────────────────
ROOT_PROFILE = "root"
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_TASK = re.compile(r"^t_[A-Za-z0-9]{4,32}$")
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _root() -> Path:
    return Path(_default_root())


def _profile_home(profile: str) -> Path:
    """Each profile is its own HERMES_HOME; root's is the fleet root itself."""
    if profile in ("", ROOT_PROFILE, "default"):
        return _root()
    return _root() / "profiles" / profile


def _profiles() -> List[str]:
    out = [ROOT_PROFILE]
    pdir = _root() / "profiles"
    try:
        out.extend(sorted(p.name for p in pdir.iterdir() if p.is_dir() and _SAFE_PROFILE.match(p.name)))
    except OSError:
        pass
    return out


def _state_db(profile: str) -> Path:
    return _profile_home(profile) / "state.db"


def _kanban_db() -> Path:
    """The active board. Derived from the fleet root rather than asked of ``kanban_db`` so that
    every path this module touches hangs off one resolvable root — which is what makes the
    plugin testable against a temporary home, and what keeps a client-supplied pane id from ever
    selecting a different file. Boards other than the active one are out of this page's scope."""
    env = os.environ.get("HERMES_KANBAN_DB")
    return Path(env) if env else _root() / "kanban.db"


def _worker_log(task_id: str) -> Path:
    """Where the dispatcher tees a worker's stdout. Same layout as ``kanban_db.worker_log_path``
    for the active board; see ``_kanban_db`` for why it is derived rather than imported."""
    return _root() / "kanban" / "logs" / f"{task_id}.log"


# ── read-only sqlite, one cached handle per file ───────────────────────────────────────────────
_CONNS: Dict[str, sqlite3.Connection] = {}
_CONN_LOCK = threading.Lock()


def _ro(path: Path) -> Optional[sqlite3.Connection]:
    """A cached read-only handle. ``mode=ro`` is what kanban_db itself uses to read a worker's
    ledger while that worker is writing it, so it is proven safe against the live WAL."""
    key = str(path)
    with _CONN_LOCK:
        conn = _CONNS.get(key)
        if conn is not None:
            return conn
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=2.0)
            conn.row_factory = sqlite3.Row
            _CONNS[key] = conn
            return conn
        except sqlite3.Error as exc:
            log.warning("fleet-live: cannot open %s read-only: %s", path, exc)
            return None


def _rows(path: Path, sql: str, args: Tuple = ()) -> List[sqlite3.Row]:
    conn = _ro(path)
    if conn is None:
        return []
    try:
        with _CONN_LOCK:
            return list(conn.execute(sql, args).fetchall())
    except sqlite3.Error as exc:
        # A schema this build does not know about must degrade to an empty pane, never a 500.
        log.debug("fleet-live: query failed on %s: %s", path, exc)
        return []


# ── liveness ───────────────────────────────────────────────────────────────────────────────────
def _pid_alive(pid: Optional[int], start: Optional[float]) -> bool:
    """pid + create_time, so a recycled pid cannot resurrect a dead agent."""
    if not pid:
        return False
    if _psutil is None:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        proc = _psutil.Process(int(pid))
        if start and abs(proc.create_time() - float(start)) > 2.0:
            return False
        return proc.is_running()
    except Exception:
        return False


def _leases(profile: str) -> List[dict]:
    """``<home>/runtime/active_sessions.json`` — the only source that knows about a session with
    no transcript row yet. Parsed, never rewritten: the registry's own snapshot helper prunes on
    read, and a dashboard must not mutate a lease file it does not own."""
    path = _profile_home(profile) / "runtime" / "active_sessions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in (data.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("session_id") or "")
        if not _SAFE_SESSION.match(sid):
            continue
        if not _pid_alive(entry.get("pid"), entry.get("process_start_time")):
            continue
        out.append({
            "profile": profile, "session_id": sid, "pid": entry.get("pid"),
            "surface": entry.get("surface") or "", "started_at": entry.get("started_at"),
        })
    return out


_SESSION_COLS = (
    "id, title, model, source, started_at, ended_at, last_activity_at, last_activity_description, "
    "message_count, tool_call_count, api_call_count, input_tokens, output_tokens, cache_read_tokens, "
    "cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, profile_name"
)


def _session_row(profile: str, session_id: str) -> dict:
    rows = _rows(_state_db(profile), f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?", (session_id,))
    return dict(rows[0]) if rows else {}


def _session_stats(row: dict) -> dict:
    """Cost is shown as what it is. ``actual_cost_usd`` is usually NULL on this fleet and
    ``cost_status`` says ``estimated`` — a page that prints one number without that flag turns an
    estimate into an invoice."""
    tokens = {k: int(row.get(k) or 0) for k in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")}
    actual = row.get("actual_cost_usd")
    est = row.get("estimated_cost_usd")
    return {
        "cost_usd": float(actual if actual else (est or 0.0)),
        "cost_status": row.get("cost_status") or ("actual" if actual else "estimated"),
        "tokens": tokens,
        "tokens_total": tokens["input_tokens"] + tokens["output_tokens"],
        "messages": int(row.get("message_count") or 0),
        "tool_calls": int(row.get("tool_call_count") or 0),
        "api_calls": int(row.get("api_call_count") or 0),
        "model": row.get("model") or "",
        "last_activity_at": row.get("last_activity_at"),
        "last_activity": row.get("last_activity_description") or "",
        "ended_at": row.get("ended_at"),
    }


# ── pane discovery ─────────────────────────────────────────────────────────────────────────────
def _kanban_workers() -> List[dict]:
    """Open runs with a live worker pid on a running card — the same shape the kanban plugin's
    ``/workers/active`` returns, read straight from the board so the page needs no second hop."""
    return [dict(r) for r in _rows(_kanban_db(), (
        "SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at AS run_started_at, "
        "r.last_heartbeat_at, r.claim_expires, t.title, t.assignee, t.status, t.tenant, t.project_id, "
        "t.max_cost, t.branch_name, t.consecutive_failures, t.body "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.ended_at IS NULL AND r.worker_pid IS NOT NULL AND t.status = 'running' "
        "ORDER BY r.started_at ASC"))]


def _attempt_count(task_id: str) -> int:
    rows = _rows(_kanban_db(), "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?", (task_id,))
    return int(rows[0]["n"]) if rows else 0


# ── the lane timeline ──────────────────────────────────────────────────────────────────────────
# The board has eight columns, but only six of them are a card's road: `blocked` and `scheduled`
# are detours a card is pushed into and comes back from, not stations it passes through. So the
# pipeline below is what a pane draws as "where this card has been and what is still to come",
# and a detour is reported separately rather than inserted into the road.
PIPELINE = ["triage", "todo", "ready", "running", "review", "done"]
DETOURS = ["blocked", "scheduled"]
BOARD_COLUMNS = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]

# task_events is append-only and is the only record of how a card actually moved. These are the
# event kinds that mean "the card changed column"; everything else (heartbeat, commented,
# attached, …) is noise for this purpose.
_EVENT_COLUMN = {
    "promoted": "ready",
    "unblocked": "ready",
    "claimed": "running",
    "review_requested": "review",
    "changes_requested": "ready",
    "review_reopened": "ready",
    "completed": "done",
    "blocked": "blocked",
    "dependency_wait": "scheduled",
    "scheduled": "scheduled",
    "archived": "archived",
    "gave_up": "blocked",
}


def _timeline(task_id: str, status: Optional[str] = None) -> dict:
    """Where this card has been, in order, and what is still ahead of it.

    ``steps`` is the de-duplicated column history — consecutive events landing in the same column
    (four claims in a row, say) are one step, because a re-claim is a retry, not progress.
    """
    rows = _rows(_kanban_db(), (
        "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind IN ('created','promoted','unblocked','claimed','review_requested','changes_requested',"
        "'review_reopened','completed','blocked','dependency_wait','scheduled','archived','gave_up','status') "
        "ORDER BY id ASC"), (task_id,))
    steps: List[dict] = []
    for row in rows:
        kind = row["kind"]
        if kind in ("created", "status"):
            try:
                col = (json.loads(row["payload"] or "{}") or {}).get("status")
            except ValueError:
                col = None
            if kind == "created" and not col:
                col = "triage"
        else:
            col = _EVENT_COLUMN.get(kind)
        if not col or (steps and steps[-1]["col"] == col):
            continue
        steps.append({"col": col, "at": row["created_at"], "kind": kind})
    if status and (not steps or steps[-1]["col"] != status):
        steps.append({"col": status, "at": None, "kind": "current"})

    current = status or (steps[-1]["col"] if steps else None)
    # What is left of the road is measured from where the card IS, not from the furthest it has
    # ever been: a card sent back from review to ready has to pass review again, and saying
    # otherwise would promise a step it is not going to skip. The furthest point is used only
    # when the card is on a detour — `blocked` is not a step backwards, so a blocked card keeps
    # the ground it made.
    reached = [s["col"] for s in steps if s["col"] in PIPELINE]
    if current in PIPELINE:
        furthest = PIPELINE.index(current)
    else:
        furthest = max((PIPELINE.index(c) for c in reached), default=-1)
    return {
        "steps": steps,
        "count": len(steps),
        "current": current,
        "detour": current if current in DETOURS else None,
        "done": [c for c in PIPELINE[: furthest + 1]],
        "remaining": PIPELINE[furthest + 1:],
        "pipeline": PIPELINE,
    }


def _deps(task_id: str) -> dict:
    """Hard dependencies, both directions. A parent that is not ``done`` is what actually holds a
    card out of ``ready`` (``_parents_blocking_ready`` in the kanban plugin uses the same rule),
    so ``unmet`` is the number the page should put in front of someone."""
    parents = [dict(r) for r in _rows(_kanban_db(), (
        "SELECT t.id, t.title, t.status, t.assignee FROM tasks t JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ? ORDER BY t.status, t.id"), (task_id,))]
    children = [dict(r) for r in _rows(_kanban_db(), (
        "SELECT t.id, t.title, t.status, t.assignee FROM tasks t JOIN task_links l ON l.child_id = t.id "
        "WHERE l.parent_id = ? ORDER BY t.status, t.id"), (task_id,))]
    return {
        "parents": parents,
        "children": children,
        "unmet": sum(1 for p in parents if p["status"] != "done"),
    }


def _session_for_pid(profile: str, pid: Optional[int]) -> Optional[dict]:
    """worker_pid -> that profile's lease -> session id. Verified exact on live workers; the
    ``task_runs.metadata.worker_session_id`` stamp is only written at completion, so it is
    useless while the run is the thing you are watching."""
    if not pid:
        return None
    for lease in _leases(profile):
        if lease.get("pid") == pid:
            return lease
    return None


def build_panes() -> List[dict]:
    """Every pane the page can show, newest work first. Two kinds:

    ``kanban``  a card being worked — purpose is the card title, feed is the worker log.
    ``session`` a live chat/CLI/desktop session that is not card work — feed is its transcript.
    """
    panes: List[dict] = []
    claimed: set = set()
    now = time.time()

    for w in _kanban_workers():
        task_id = str(w.get("task_id") or "")
        if not _SAFE_TASK.match(task_id):
            continue
        profile = str(w.get("profile") or w.get("assignee") or ROOT_PROFILE)
        lease = _session_for_pid(profile, w.get("worker_pid"))
        session_id = lease.get("session_id") if lease else None
        if session_id:
            claimed.add((profile, session_id))
        row = _session_row(profile, session_id) if session_id else {}
        hb = w.get("last_heartbeat_at")
        panes.append({
            "id": f"k:{task_id}",
            "kind": "kanban",
            "feed": "log",
            "title": w.get("title") or task_id,
            "subtitle": (w.get("body") or "").strip().splitlines()[:1],
            "task_id": task_id,
            "run_id": w.get("run_id"),
            "agent": w.get("assignee") or profile,
            "profile": profile,
            "tenant": w.get("tenant") or "",
            "branch": w.get("branch_name") or "",
            "session_id": session_id,
            "pid": w.get("worker_pid"),
            "alive": _pid_alive(w.get("worker_pid"), None),
            "started_at": w.get("run_started_at"),
            "elapsed_s": int(now - float(w.get("run_started_at") or now)),
            "heartbeat_age_s": int(now - float(hb)) if hb else None,
            "claim_expires_in_s": int(float(w["claim_expires"]) - now) if w.get("claim_expires") else None,
            "attempt": _attempt_count(task_id),
            "failures": int(w.get("consecutive_failures") or 0),
            "cost_cap": w.get("max_cost"),
            "stats": _session_stats(row),
            "has_log": _worker_log(task_id).exists(),
            "status": w.get("status"),
            "timeline": _timeline(task_id, w.get("status")),
            "deps": _deps(task_id),
        })

    for profile in _profiles():
        for lease in _leases(profile):
            key = (profile, lease["session_id"])
            if key in claimed:
                continue
            row = _session_row(profile, lease["session_id"])
            if row.get("source") == "kanban" and not row.get("ended_at"):
                # A worker whose card already left `running`; the kanban pane above owns it.
                continue
            started = row.get("started_at") or lease.get("started_at") or now
            panes.append({
                "id": f"s:{profile}:{lease['session_id']}",
                "kind": "session",
                "feed": "db",
                "title": row.get("title") or f"{lease.get('surface') or 'session'} · {profile}",
                "subtitle": [],
                "task_id": None,
                "run_id": None,
                "agent": profile,
                "profile": profile,
                "tenant": "",
                "branch": "",
                "session_id": lease["session_id"],
                "pid": lease.get("pid"),
                "alive": True,
                "surface": lease.get("surface") or "",
                "started_at": started,
                "elapsed_s": int(now - float(started)),
                "heartbeat_age_s": int(now - float(row["last_activity_at"])) if row.get("last_activity_at") else None,
                "claim_expires_in_s": None,
                "attempt": 0,
                "failures": 0,
                "cost_cap": None,
                "stats": _session_stats(row),
                "has_log": False,
                "status": None,
                "timeline": None,
                "deps": None,
            })

    panes.sort(key=lambda p: (p["kind"] != "kanban", -(p.get("started_at") or 0)))
    return panes


def pane_stats(pane_id: str) -> dict:
    """The numbers only — refreshed far more often than the pane list itself."""
    kind, _, rest = pane_id.partition(":")
    if kind == "k":
        for p in _kanban_workers():
            if p.get("task_id") == rest:
                profile = str(p.get("profile") or p.get("assignee") or ROOT_PROFILE)
                lease = _session_for_pid(profile, p.get("worker_pid"))
                row = _session_row(profile, lease["session_id"]) if lease else {}
                hb = p.get("last_heartbeat_at")
                now = time.time()
                out = _session_stats(row)
                out.update({
                    "elapsed_s": int(now - float(p.get("run_started_at") or now)),
                    "heartbeat_age_s": int(now - float(hb)) if hb else None,
                    "cost_cap": p.get("max_cost"),
                    "alive": _pid_alive(p.get("worker_pid"), None),
                })
                return out
        return {"gone": True}
    if kind == "s":
        profile, _, sid = rest.partition(":")
        row = _session_row(profile, sid)
        if not row:
            return {"gone": True}
        out = _session_stats(row)
        out["elapsed_s"] = int(time.time() - float(row.get("started_at") or time.time()))
        return out
    return {"gone": True}


# ── the transcript parser ──────────────────────────────────────────────────────────────────────
# The worker log is the CLI's own TUI transcript with the ANSI already stripped: reasoning and
# assistant turns arrive inside box-drawn frames, tool calls on a `┊` gutter, everything else
# (command output, diffs) flat. The grammar below is deliberately forgiving — anything it does
# not recognise becomes a `raw` event and is still shown, so a rendering change upstream degrades
# the page's polish and never its completeness.
_BOX_OPEN = re.compile(r"^[┌╭]─+\s*(.*?)\s*─*[┐╮]\s*$")
_BOX_CLOSE = re.compile(r"^[└╰]─+[┘╯]\s*$")
_GUTTER = re.compile(r"^\s*┊\s?(.*?)\s*$")
_DURATION = re.compile(r"\s{2,}([\d.]+m?s)\s*$")
# A line at or past the wrap column was broken by the renderer, not by the model. Shorter lines
# end a real paragraph. Measured against live logs: content wraps at 78-86 characters.
_WRAP_MIN = 76


class Parser:
    """Turns a byte stream into append-only ops. One instance per pane, kept across polls so a
    reasoning block that spans several reads streams as it is written rather than appearing whole
    when it closes."""

    def __init__(self, seq: int = 0) -> None:
        self.seq = seq
        self.buf = ""          # incomplete trailing line
        self.open: Optional[dict] = None   # the event currently being filled
        self.last_len = 0      # length of the previous content line, for unwrapping
        self.diff_next = False

    # -- event helpers -------------------------------------------------------------------------
    def _add(self, ops: List[dict], kind: str, label: str = "", text: str = "", **extra) -> dict:
        self.seq += 1
        ev = {"op": "add", "id": self.seq, "t": kind, "label": label, "text": text, "at": time.time()}
        ev.update(extra)
        ops.append(ev)
        self.open = {"id": self.seq, "t": kind}
        self.last_len = 0
        return ev

    def _end(self, ops: List[dict]) -> None:
        if self.open is not None:
            ops.append({"op": "end", "id": self.open["id"]})
            self.open = None
            self.last_len = 0

    def _append(self, ops: List[dict], text: str) -> None:
        if not text:
            return
        ops.append({"op": "app", "id": self.open["id"], "text": text})

    # -- the line grammar ----------------------------------------------------------------------
    def _content(self, ops: List[dict], line: str) -> None:
        """A line inside a reasoning / assistant frame, unwrapped back into prose."""
        if not line.strip():
            if self.open and self.last_len:
                self._append(ops, "\n\n")
                self.last_len = 0
            return
        if self.open is None:
            self._add(ops, "raw", text=line)
            self.last_len = len(line)
            return
        joiner = "" if (self.last_len >= _WRAP_MIN or line.startswith(" ")) else ("\n" if self.last_len else "")
        self._append(ops, joiner + line)
        self.last_len = len(line)

    def _tool(self, ops: List[dict], body: str) -> None:
        self._end(ops)
        dur = ""
        m = _DURATION.search(body)
        if m:
            dur = m.group(1)
            body = body[: m.start()].rstrip()
        icon = ""
        parts = body.split(None, 1)
        if parts and not parts[0].isalnum() and len(parts[0]) <= 3:
            icon, body = parts[0], (parts[1] if len(parts) > 1 else "")
        self._add(ops, "tool", label=icon, text=re.sub(r"\s{2,}", " ", body).strip(), dur=dur)
        self.diff_next = body.strip().lower().startswith("review diff")
        self._end(ops)

    def feed(self, chunk: str) -> List[dict]:
        ops: List[dict] = []
        self.buf += chunk.replace("\r\n", "\n").replace("\r", "\n")
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            open_m = _BOX_OPEN.match(line)
            if open_m:
                self._end(ops)
                label = open_m.group(1).strip() or "Output"
                kind = "thought" if label.lower().startswith("reason") else "say"
                self._add(ops, kind, label=label)
                self.diff_next = False
                continue
            if _BOX_CLOSE.match(line):
                self._end(ops)
                continue
            gutter = _GUTTER.match(line)
            if gutter:
                self._tool(ops, gutter.group(1))
                continue
            if self.open is not None and self.open["t"] in ("thought", "say"):
                self._content(ops, line)
                continue
            # Outside a frame: command output, diffs, stack traces. Collected verbatim into one
            # rolling `raw` event so a 400-line pytest dump is one collapsible block, not 400.
            if not line.strip():
                if self.open is not None and self.open["t"] == "raw":
                    self._append(ops, "\n")
                continue
            if self.open is not None and self.open["t"] == "raw":
                self._append(ops, "\n" + line)
            else:
                self._add(ops, "raw", label=("diff" if self.diff_next else ""), text=line)
                self.diff_next = False
        return ops


# ── log tailing ────────────────────────────────────────────────────────────────────────────────
_READ_CAP = 256 * 1024          # per poll; a worker dumping a huge test log cannot flood one frame
_BACKLOG_DEFAULT = 48 * 1024
_BACKLOG_CAP = 512 * 1024


def _read_from(path: Path, offset: int, cap: int = _READ_CAP) -> Tuple[bytes, int, bool]:
    """Bytes from ``offset``, the new offset, and whether the file was truncated under us (the
    worker log rotates at 2 MiB, at which point every held offset is meaningless)."""
    try:
        size = path.stat().st_size
    except OSError:
        return b"", offset, False
    if size < offset:
        return b"", 0, True          # rotated — caller restarts from the top
    if size == offset:
        return b"", offset, False
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(min(cap, size - offset))
    return data, offset + len(data), False


class LogFeed:
    """A byte-offset tail of one worker log, with a parser that survives across polls."""

    def __init__(self, path: Path, offset: int = 0, seq: int = 0) -> None:
        self.path = path
        self.offset = offset
        self.parser = Parser(seq)
        self.carry = b""

    def poll(self) -> Tuple[List[dict], int, bool]:
        data, offset, rotated = _read_from(self.path, self.offset)
        if rotated:
            self.offset, self.carry = 0, b""
            return [], 0, True
        if not data:
            return [], self.offset, False
        blob = self.carry + data
        # Never split a multi-byte character across two frames.
        self.carry = b""
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            text = blob[: exc.start].decode("utf-8", "replace")
            self.carry = blob[exc.start:]
        self.offset = offset
        return self.parser.feed(text), offset, False


class DbFeed:
    """A rowid tail of one session's transcript, for panes with no log file."""

    def __init__(self, profile: str, session_id: str, offset: int = 0, seq: int = 0) -> None:
        self.db = _state_db(profile)
        self.session_id = session_id
        self.offset = offset
        self.seq = seq

    def poll(self) -> Tuple[List[dict], int, bool]:
        rows = _rows(self.db, (
            "SELECT id, role, content, reasoning, reasoning_content, tool_name, timestamp "
            "FROM messages WHERE session_id = ? AND id > ? ORDER BY id LIMIT 120"),
            (self.session_id, self.offset))
        ops: List[dict] = []
        for row in rows:
            self.offset = int(row["id"])
            think = (row["reasoning_content"] or row["reasoning"] or "").strip()
            if think:
                self.seq += 1
                ops.append({"op": "add", "id": self.seq, "t": "thought", "label": "Reasoning",
                            "text": think, "at": row["timestamp"]})
                ops.append({"op": "end", "id": self.seq})
            role = (row["role"] or "").lower()
            body = (row["content"] or "").strip()
            if role == "assistant" and body:
                self.seq += 1
                ops.append({"op": "add", "id": self.seq, "t": "say", "label": "Hermes",
                            "text": body, "at": row["timestamp"]})
                ops.append({"op": "end", "id": self.seq})
            elif role == "tool":
                self.seq += 1
                ops.append({"op": "add", "id": self.seq, "t": "tool", "label": "🔧",
                            "text": (row["tool_name"] or "tool"), "dur": "", "at": row["timestamp"]})
                ops.append({"op": "end", "id": self.seq})
                if body:
                    self.seq += 1
                    ops.append({"op": "add", "id": self.seq, "t": "raw", "label": "",
                                "text": body[:4000], "at": row["timestamp"]})
                    ops.append({"op": "end", "id": self.seq})
            elif role == "user" and body:
                self.seq += 1
                ops.append({"op": "add", "id": self.seq, "t": "user", "label": "You",
                            "text": body[:4000], "at": row["timestamp"]})
                ops.append({"op": "end", "id": self.seq})
        return ops, self.offset, False


def _feed_for(pane_id: str, offset: int = 0, seq: int = 0):
    kind, _, rest = pane_id.partition(":")
    if kind == "k":
        if not _SAFE_TASK.match(rest):
            raise HTTPException(status_code=400, detail="bad task id")
        return LogFeed(_worker_log(rest), offset, seq)
    if kind == "s":
        profile, _, sid = rest.partition(":")
        if not (_SAFE_PROFILE.match(profile) and _SAFE_SESSION.match(sid)):
            raise HTTPException(status_code=400, detail="bad session id")
        return DbFeed(profile, sid, offset, seq)
    raise HTTPException(status_code=400, detail="unknown pane")


# ── HTTP ───────────────────────────────────────────────────────────────────────────────────────
@router.get("/panes")
def get_panes():
    """Every live agent, with the numbers each pane's header needs."""
    panes = build_panes()
    return {
        "panes": panes,
        "count": len(panes),
        "at": time.time(),
        "cost_usd": round(sum(p["stats"].get("cost_usd") or 0 for p in panes), 4),
    }


@router.get("/panes/{pane_id}/tail")
def get_tail(pane_id: str,
             bytes_: int = Query(_BACKLOG_DEFAULT, alias="bytes", ge=1024, le=_BACKLOG_CAP),
             before: Optional[int] = Query(None, ge=0, description="byte offset to read backwards from")):
    """Backlog for a pane entering the viewport: the last ``bytes`` of the feed, already parsed,
    plus the offset the live tail should resume from. ``before`` walks further back for scroll-up.

    The first line of a backwards read is dropped — an arbitrary byte offset lands mid-line and a
    half-line rendered as prose is worse than a missing one.
    """
    kind, _, rest = pane_id.partition(":")
    if kind == "s":
        feed = _feed_for(pane_id)
        profile, _, sid = rest.partition(":")
        rows = _rows(_state_db(profile), "SELECT MIN(id) AS lo FROM (SELECT id FROM messages "
                                         "WHERE session_id = ? ORDER BY id DESC LIMIT 40)", (sid,))
        feed.offset = (int(rows[0]["lo"]) - 1) if rows and rows[0]["lo"] else 0
        ops, offset, _ = feed.poll()
        return {"pane": pane_id, "ops": ops, "offset": offset, "seq": feed.seq, "start": 0, "more": False}

    path = _worker_log(rest) if kind == "k" else None
    if path is None:
        raise HTTPException(status_code=400, detail="unknown pane")
    if not path.exists():
        return {"pane": pane_id, "ops": [], "offset": 0, "seq": 0, "start": 0, "more": False}
    size = path.stat().st_size
    end = min(before, size) if before is not None else size
    start = max(0, end - bytes_)
    with path.open("rb") as fh:
        fh.seek(start)
        blob = fh.read(end - start)
    text = blob.decode("utf-8", "replace")
    if start > 0:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    parser = Parser(0)
    ops = parser.feed(text + "\n")
    return {"pane": pane_id, "ops": ops, "offset": end, "seq": parser.seq, "start": start, "more": start > 0}


@router.get("/panes/{pane_id}/stats")
def get_stats(pane_id: str):
    return {"pane": pane_id, "stats": pane_stats(pane_id), "at": time.time()}


_CARD_COLS = ("id, title, body, assignee, status, priority, tenant, project_id, branch_name, max_cost, "
              "created_at, started_at, completed_at, consecutive_failures, block_kind, last_failure_error")


def _card_payload(task_id: str) -> dict:
    """One card, everything a reader needs to act on it: the brief, where it has been, what it is
    waiting on, what is waiting on it, its attempt history and the last of the conversation."""
    path = _kanban_db()
    rows = _rows(path, f"SELECT {_CARD_COLS} FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="card not found")
    card = dict(rows[0])
    card["runs"] = [dict(r) for r in _rows(path, (
        "SELECT id, profile, status, outcome, started_at, ended_at, summary FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 12"), (task_id,))]
    card["comments"] = [dict(r) for r in _rows(path, (
        "SELECT id, author, body, created_at FROM task_comments WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 20"), (task_id,))][::-1]
    card["timeline"] = _timeline(task_id, card.get("status"))
    card["deps"] = _deps(task_id)
    return card


@router.get("/panes/{pane_id}/card")
def get_pane_card(pane_id: str):
    """The card behind a kanban pane — its body is the agent's actual brief."""
    kind, _, task_id = pane_id.partition(":")
    if kind != "k" or not _SAFE_TASK.match(task_id):
        raise HTTPException(status_code=404, detail="no card for this pane")
    return {"card": _card_payload(task_id)}


@router.get("/cards/{task_id}")
def get_card(task_id: str):
    """Any card by id — what a dependency chip opens. Separate from the pane route because a
    dependency is usually a card nothing is working on, so it has no pane."""
    if not _SAFE_TASK.match(task_id):
        raise HTTPException(status_code=400, detail="bad task id")
    return {"card": _card_payload(task_id)}


# Statuses that mean "not finished, not currently being worked" — the queue's population.
_QUEUE_STATUSES = ("ready", "todo", "triage", "scheduled", "blocked", "review")
# Rank inside the queue: how close a card is to a worker picking it up.
_QUEUE_RANK = {"ready": 0, "review": 1, "todo": 2, "triage": 3, "scheduled": 4, "blocked": 5}


def build_queue(limit: int = 60) -> dict:
    """Up next: what the dispatcher will reach for, in the order it becomes reachable.

    Ordered by whether anything still blocks the card, then by how close its column is to a
    worker, then newest first — so a freshly minted card with nothing in front of it sits at the
    top, and a card waiting on three unfinished parents sits at the bottom whatever its column.

    A plain function, not just a route body, so it can be called directly from a script checking
    the real board without FastAPI resolving its defaults.
    """
    placeholders = ",".join("?" * len(_QUEUE_STATUSES))
    rows = _rows(_kanban_db(), (
        f"SELECT {_CARD_COLS} FROM tasks WHERE status IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 400"), _QUEUE_STATUSES)
    if not rows:
        return {"queue": [], "count": 0, "at": time.time()}

    # Dependencies for the whole queue in two queries rather than two per card: a board with a
    # few hundred open cards would otherwise make this endpoint quadratic in link count.
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    parents: Dict[str, List[dict]] = {}
    for link in _rows(_kanban_db(), (
            f"SELECT l.child_id, t.id, t.title, t.status, t.assignee FROM task_links l "
            f"JOIN tasks t ON t.id = l.parent_id WHERE l.child_id IN ({marks})"), tuple(ids)):
        parents.setdefault(link["child_id"], []).append(
            {"id": link["id"], "title": link["title"], "status": link["status"], "assignee": link["assignee"]})
    child_counts: Dict[str, int] = {}
    for link in _rows(_kanban_db(), (
            f"SELECT parent_id, COUNT(*) AS n FROM task_links WHERE parent_id IN ({marks}) "
            "GROUP BY parent_id"), tuple(ids)):
        child_counts[link["parent_id"]] = int(link["n"])

    out = []
    for row in rows:
        card = dict(row)
        mine = parents.get(card["id"], [])
        blocked_by = [p for p in mine if p["status"] != "done"]
        card["deps"] = {"unmet": len(blocked_by), "parents": mine,
                        "children_count": child_counts.get(card["id"], 0)}
        card["blocked_by"] = blocked_by
        out.append(card)
    out.sort(key=lambda c: (1 if c["deps"]["unmet"] else 0,
                            _QUEUE_RANK.get(c["status"], 9),
                            -(c.get("created_at") or 0)))
    return {"queue": out[: int(limit)], "count": len(out), "at": time.time()}


@router.get("/queue")
def get_queue(limit: int = Query(60, ge=1, le=300)):
    return build_queue(limit)


# ── WebSocket ──────────────────────────────────────────────────────────────────────────────────
_POLL_S = 0.25            # feed poll; the page's "as it happens" budget
_STATS_EVERY = 12         # poll ticks between stat refreshes  (~3 s)
_PANES_EVERY = 20         # poll ticks between pane-list refreshes (~5 s)
_MAX_SUBS = 24            # a 3x8 grid of visible panes is already more than a screen holds


def _ws_authorized(ws: WebSocket) -> bool:
    """The dashboard's canonical WS gate (``?token=`` / ``?ticket=`` / ``?internal=``). HTTP
    middleware does not run for an upgrade, so this endpoint would otherwise be open; accepts
    when the dashboard is not importable, which is the bare-FastAPI test harness."""
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


@router.websocket("/stream")
async def stream(ws: WebSocket):
    if not _ws_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()

    loop = asyncio.get_running_loop()
    feeds: Dict[str, Any] = {}
    tick = 0

    async def off(fn, *a):
        """Every read here touches the filesystem or sqlite; none of it belongs on the event loop."""
        return await loop.run_in_executor(None, fn, *a)

    try:
        await ws.send_json({"type": "panes", "panes": await off(build_panes), "at": time.time()})
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=_POLL_S)
                if msg.get("type") == "websocket.disconnect":
                    return
                body = json.loads(msg.get("text") or "{}")
                for pane_id in (body.get("unsub") or []):
                    feeds.pop(pane_id, None)
                for pane_id in (body.get("sub") or []):
                    if pane_id in feeds or len(feeds) >= _MAX_SUBS:
                        continue
                    start = (body.get("from") or {}).get(pane_id) or {}
                    feeds[pane_id] = _feed_for(pane_id, int(start.get("offset") or 0), int(start.get("seq") or 0))
                if body.get("panes"):
                    await ws.send_json({"type": "panes", "panes": await off(build_panes), "at": time.time()})
            except asyncio.TimeoutError:
                pass
            except (ValueError, KeyError, HTTPException):
                continue            # a malformed client frame is ignored, never fatal

            tick += 1
            for pane_id, feed in list(feeds.items()):
                try:
                    ops, offset, rotated = await off(feed.poll)
                except Exception as exc:
                    log.debug("fleet-live: feed %s failed: %s", pane_id, exc)
                    continue
                if rotated:
                    await ws.send_json({"type": "reset", "pane": pane_id})
                    continue
                if ops:
                    await ws.send_json({"type": "feed", "pane": pane_id, "offset": offset,
                                        "seq": getattr(feed, "parser", feed).seq, "ops": ops})
            if tick % _STATS_EVERY == 0:
                for pane_id in list(feeds):
                    await ws.send_json({"type": "stats", "pane": pane_id,
                                        "stats": await off(pane_stats, pane_id)})
            if tick % _PANES_EVERY == 0:
                await ws.send_json({"type": "panes", "panes": await off(build_panes), "at": time.time()})
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except Exception as exc:        # never take the dashboard worker down with the page
        log.warning("fleet-live stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
