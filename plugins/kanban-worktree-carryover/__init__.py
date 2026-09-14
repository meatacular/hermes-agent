"""Worktree carry-over on retry, on upstream's ``kanban_task_claimed`` hook.

See ``plugin.yaml`` for the defect this closes and why it is a plugin rather
than a kanban-core patch.

Contract used from upstream (read-only, nothing imported from ``hermes_cli``):

* ``kanban_task_claimed`` fires from ``kanban_db.claim_task`` / ``claim_review_task``
  AFTER the claim transaction commits and BEFORE the dispatcher calls
  ``_kbw._resolve_worktree_workspace`` (``kanban_db_dispatch._spawn_task``).
  Payload kwargs: ``task_id``, ``board``, ``assignee``, ``run_id``, ``profile_name``.
* Board layout, mirrored with stdlib only: ``<root>/kanban.db`` for the
  ``default`` board, ``<root>/kanban/boards/<slug>/kanban.db`` for a named one,
  metadata at ``<root>/kanban/boards/<slug>/board.json`` (``default_workdir``),
  ``<root>/kanban/current`` holding the active slug (absent = ``default``) —
  ``kanban_home()`` is ``$HERMES_KANBAN_HOME`` else ``~/.hermes``.

Everything here is best-effort and fails open: a hook that raises or guesses
strangles the board, which is worse than the defect it prevents.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = ["register", "on_task_claimed", "carry_over", "plan"]

logger = logging.getLogger(__name__)

HOOK = "kanban_task_claimed"

#: Env kill switch — set to a falsy value to disable without reverting the plugin.
ENV_KILL_SWITCH = "HERMES_KANBAN_WORKTREE_CARRYOVER"

#: Bound the search: the event history can name the same clone many times.
MAX_SOURCES = 4

#: git commands are short and local; never let one stall a dispatch tick.
GIT_TIMEOUT = 20


# --------------------------------------------------------------------------
# board / db discovery (stdlib mirror of kanban_db's layout)
# --------------------------------------------------------------------------

def _kanban_home() -> Path:
    override = (os.environ.get("HERMES_KANBAN_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if home:
        return Path(home).expanduser()
    return Path.home() / ".hermes"


def _slug_or_default(board: Optional[str]) -> str:
    slug = (board or "").strip()
    if slug:
        return slug
    marker = _kanban_home() / "kanban" / "current"
    try:
        if marker.exists():
            got = marker.read_text(encoding="utf-8").strip()
            if got:
                return got
    except OSError:
        pass
    return "default"


def _db_candidates(board: Optional[str]) -> List[Path]:
    """Candidate board DBs, most specific first."""
    home = _kanban_home()
    slug = _slug_or_default(board)
    out: List[Path] = []
    env_db = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if env_db:
        out.append(Path(env_db).expanduser())
    if slug != "default":
        out.append(home / "kanban" / "boards" / slug / "kanban.db")
    out.append(home / "kanban.db")
    out.append(home / "kanban" / "kanban.db")
    if slug != "default":
        out.append(home / "kanban" / "boards" / "default" / "kanban.db")
    seen, uniq = set(), []
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _find_db(task_id: str, board: Optional[str]) -> Optional[Path]:
    """The candidate DB that actually holds ``task_id``."""
    for path in _db_candidates(board):
        try:
            if not path.is_file():
                continue
            with _connect(path) as conn:
                row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                return path
        except Exception:
            continue
    return None


def _board_default_workdir(board: Optional[str]) -> Optional[str]:
    slug = _slug_or_default(board)
    meta = _kanban_home() / "kanban" / "boards" / slug / "board.json"
    try:
        raw = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("default_workdir")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def _git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except Exception:
        return None


def _git_out(cwd: Path, *args: str) -> Optional[str]:
    res = _git(cwd, *args)
    if res is None or res.returncode != 0:
        return None
    out = (res.stdout or "").strip()
    return out or None


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for(path: Path) -> Optional[Path]:
    """Toplevel of the git repo containing ``path`` (which need not exist)."""
    start = _nearest_existing(Path(path).expanduser())
    out = _git_out(start, "rev-parse", "--show-toplevel")
    if out:
        try:
            return Path(out).expanduser().resolve()
        except OSError:
            return Path(out).expanduser()
    # A path inside a not-yet-created .worktrees/<id> still belongs to its
    # parent repo; walk up looking for one.
    current = start
    while True:
        out = _git_out(current, "rev-parse", "--show-toplevel")
        if out:
            return Path(out).expanduser()
        if current == current.parent:
            return None
        current = current.parent


def _branch_exists(repo: Path, branch: str) -> bool:
    res = _git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    return res is not None and res.returncode == 0


def _same_repo(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


# --------------------------------------------------------------------------
# board reads
# --------------------------------------------------------------------------

def _load_board(db_path: Path, task_id: str, board: Optional[str]) -> Dict[str, Any]:
    """Task row + prior-anchor history + run count. Empty dict on any failure."""
    out: Dict[str, Any] = {"task": None, "anchors": [], "runs": 0}
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, status, assignee, workspace_kind, workspace_path, "
                "branch_name FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return out
            out["task"] = {k: row[k] for k in row.keys()}

            runs = conn.execute(
                "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
            out["runs"] = int(runs["n"]) if runs else 0

            seen, anchors = set(), []
            try:
                events = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id = ? "
                    "AND payload IS NOT NULL ORDER BY id DESC LIMIT 200",
                    (task_id,),
                ).fetchall()
            except sqlite3.Error:
                events = []
            for ev in events:
                try:
                    payload = json.loads(ev["payload"] or "{}")
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                for key in ("workspace_path", "previous_workspace_path", "old_workspace_path"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip() and value not in seen:
                        seen.add(value)
                        anchors.append(value)
    except Exception:
        return {"task": None, "anchors": [], "runs": 0}
    out["anchors"] = anchors
    return out


def _remotes(repo: Path) -> List[str]:
    out = _git_out(repo, "remote")
    return [r for r in (out or "").splitlines() if r.strip()]


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def plan(task_id: str, board: Optional[str] = None, *, db_path: Optional[Path] = None,
         env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Decide what, if anything, this claim needs. Never raises, never mutates."""
    env = os.environ if env is None else env
    verdict: Dict[str, Any] = {
        "action": "noop", "reason": "", "branch": None,
        "target_repo": None, "candidates": [],
    }

    switch = str(env.get(ENV_KILL_SWITCH, "1")).strip().lower()
    if switch in ("0", "false", "no", "off"):
        verdict["reason"] = "disabled by env"
        return verdict

    if not task_id:
        verdict["reason"] = "no task id"
        return verdict

    db = Path(db_path) if db_path is not None else _find_db(task_id, board)
    if db is None:
        verdict["reason"] = "no board db holds this task"
        return verdict

    state = _load_board(db, task_id, board)
    task = state.get("task")
    if not task:
        verdict["reason"] = "task row unreadable"
        return verdict
    if (task.get("workspace_kind") or "") != "worktree":
        verdict["reason"] = "not a worktree card"
        return verdict

    runs = int(state.get("runs") or 0)
    if runs < 2:
        # First provision: from base, exactly as before. (AC3.)
        verdict["reason"] = "first provision"
        return verdict

    branch = (task.get("branch_name") or "").strip() or f"wt/{task_id}"
    verdict["branch"] = branch

    ws_path = (task.get("workspace_path") or "").strip()
    anchor = ws_path or (_board_default_workdir(board) or "")
    if not anchor:
        verdict["reason"] = "no anchor (no workspace_path, no board default_workdir)"
        return verdict
    target_repo = _repo_root_for(Path(anchor))
    if target_repo is None:
        verdict["reason"] = "anchor is not inside a git repo"
        return verdict
    verdict["target_repo"] = str(target_repo)

    if _branch_exists(target_repo, branch):
        # The kernel reuses an existing branch; nothing to do. This is the
        # ordinary retry, and it is why commits normally survive.
        verdict["reason"] = "branch already present in target repo"
        return verdict

    # Branch absent -> the kernel is about to cut it from HEAD. Look for the
    # card's own earlier copy of it, newest anchor first, then origin.
    candidates: List[Tuple[str, Path]] = []
    for raw in state.get("anchors") or []:
        if len(candidates) >= MAX_SOURCES:
            break
        src = _repo_root_for(Path(raw))
        if src is None or _same_repo(src, target_repo):
            continue
        if not _branch_exists(src, branch):
            continue
        if not any(_same_repo(src, existing) for _, existing in candidates):
            candidates.append((str(raw), src))

    for remote in _remotes(target_repo)[:MAX_SOURCES]:
        if len(candidates) >= MAX_SOURCES:
            break
        # Only a branch ALREADY fetched locally is a candidate: the dispatcher
        # tick must not go to the network on every retry, and a remote-tracking
        # ref we already hold is proof the work reached the remote.
        if not _git_out(target_repo, "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"):
            continue
        if not any(name == f"remote:{remote}" for name, _ in candidates):
            candidates.append((f"remote:{remote}", target_repo))

    if not candidates:
        verdict["reason"] = "branch absent and no earlier copy found"
        return verdict

    verdict["candidates"] = [{"source": name, "repo": str(repo)} for name, repo in candidates]
    verdict["action"] = "carry"
    verdict["reason"] = "branch absent in target repo; earlier copy available"
    return verdict


def _fetch(target_repo: Path, source: str, repo: Path, branch: str) -> bool:
    """Bring ``branch`` into ``target_repo``. Only ever creates the ref.

    ``source`` is a human-readable label (the card's recorded anchor path, or
    ``remote:<name>``); ``repo`` is where the objects actually come from — the
    anchor's RESOLVED repo root, which is not the same string when the anchor is
    a ``.worktrees/<id>`` path that no longer exists.
    """
    ref = f"refs/heads/{branch}:refs/heads/{branch}"
    if source.startswith("remote:"):
        remote = source.split(":", 1)[1]
        res = _git(target_repo, "fetch", "--no-tags", remote, ref)
        if res is not None and res.returncode == 0 and _branch_exists(target_repo, branch):
            return True
        # Offline (or the remote pruned it): the remote-tracking ref we already
        # hold carries the same objects, so branch from it without the network.
        tracking = f"refs/remotes/{remote}/{branch}"
        if _git_out(target_repo, "rev-parse", "--verify", tracking):
            made = _git(target_repo, "branch", branch, tracking)
            if made is not None and made.returncode == 0:
                return True
        return False
    res = _git(target_repo, "fetch", "--no-tags", str(repo), ref)
    if res is None or res.returncode != 0:
        logger.debug(
            "kanban-worktree-carryover: fetch from %s (%s) failed: %s",
            source, repo, ((res.stderr or res.stdout or "").strip()[:400] if res else "no result"),
        )
        return False
    return _branch_exists(target_repo, branch)


def _append_event(db_path: Path, task_id: str, kind: str, payload: Dict[str, Any]) -> None:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            import time as _time
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (task_id, kind, json.dumps(payload), int(_time.time())),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def carry_over(task_id: str, board: Optional[str] = None, *, db_path: Optional[Path] = None,
               env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Plan, then perform, the carry-over. Never raises."""
    verdict = plan(task_id, board, db_path=db_path, env=env)
    if verdict.get("action") != "carry":
        return verdict

    branch = verdict["branch"]
    target_repo = Path(verdict["target_repo"])
    for candidate in verdict.get("candidates") or []:
        name = str(candidate.get("source") or "")
        if not name:
            continue
        try:
            if _fetch(target_repo, name, Path(str(candidate.get("repo") or target_repo)), branch):
                verdict["action"] = "carried"
                verdict["source"] = name
                head = _git_out(target_repo, "rev-parse", f"refs/heads/{branch}")
                verdict["head"] = head
                verdict["reason"] = f"branch carried into target repo from {name}"
                logger.warning(
                    "kanban-worktree-carryover: task %s — target repo %s had no branch %r; "
                    "carried it from %s (%s) before the worktree was materialised, so the "
                    "retry starts from the card's own work instead of a fresh cut from HEAD.",
                    task_id, target_repo, branch, name, (head or "?")[:12],
                )
                db = db_path if db_path is not None else _find_db(task_id, board)
                if db is not None:
                    _append_event(db, task_id, "worktree_carryover", {
                        "branch": branch,
                        "target_repo": str(target_repo),
                        "source": name,
                        "head": head,
                    })
                return verdict
        except Exception:
            continue

    verdict["action"] = "noop"
    verdict["reason"] = "every source failed to yield the branch"
    logger.warning(
        "kanban-worktree-carryover: task %s — branch %r is absent in %s and could not be "
        "recovered from %s; the retry will be cut from HEAD. Prior work may exist elsewhere.",
        task_id, branch, target_repo, [c.get("source") for c in verdict.get("candidates") or []],
    )
    return verdict


# --------------------------------------------------------------------------
# hook
# --------------------------------------------------------------------------

def on_task_claimed(task_id: Optional[str] = None, board: Optional[str] = None, **_kwargs: Any) -> None:
    """``kanban_task_claimed`` handler — dispatcher process, pre-provisioning."""
    try:
        if task_id:
            carry_over(str(task_id), board)
    except Exception:  # noqa: BLE001 — a hook must never break a claim
        logger.exception("kanban-worktree-carryover: unexpected error; claim left untouched")


def register(ctx) -> None:
    ctx.register_hook(HOOK, on_task_claimed)
