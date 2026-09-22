"""Worktree carry-over on retry + base-current at claim, on upstream's ``kanban_task_claimed`` hook.

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
  ``kanban_home()`` is ``$HERMES_KANBAN_HOME`` else the ROOT home, i.e. a
  profile's ``HERMES_HOME=<root>/profiles/<name>`` resolves BACK to ``<root>``
  because the board is shared across profiles by design.

Everything here is best-effort and fails open: a hook that raises or guesses
strangles the board, which is worse than the defect it prevents.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = ["register", "on_task_claimed", "carry_over", "plan", "base_current"]

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

def _native_hermes_home() -> Path:
    """Platform-default Hermes home — mirror of ``hermes_constants`` (no import)."""
    if sys.platform == "win32":
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _kanban_home() -> Path:
    """``HERMES_KANBAN_HOME`` else the ROOT home — mirror of ``kanban_db.kanban_home()``.

    The board is shared across profiles BY DESIGN, so the kernel resolves
    ``HERMES_HOME=<root>/profiles/<name>`` back to ``<root>`` through
    ``get_default_hermes_root()``. Returning ``HERMES_HOME`` verbatim made this
    plugin silently inert in every profile-owned dispatcher: it looked for the
    board inside the profile directory, found none, and reported "no board db
    holds this task" for a board it had never looked at. ``brain``, ``switch``
    and ``axel`` have each held the machine-global dispatcher lease, and
    non-root ownership is supported on purpose (``_should_seize_dispatcher``).
    """
    override = (os.environ.get("HERMES_KANBAN_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    native = _native_hermes_home()
    if not env_home:
        return native
    env_path = Path(env_home).expanduser()
    try:
        env_path.resolve(strict=False).relative_to(native.resolve(strict=False))
    except (ValueError, OSError):
        # Docker / custom root: <root>/profiles/<name> -> <root>, else as given.
        return env_path.parent.parent if env_path.parent.name == "profiles" else env_path
    return native


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
        # ref we already hold is proof the work reached the remote. Nothing
        # fetches it later either — carries from here are ref-only, local (AC2).
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
        # NO NETWORK HERE. ``plan()`` only offers ``remote:<name>`` when the
        # tracking ref is already local — the objects are in this repo already —
        # and this hook runs inline under the machine-global dispatch lock, where
        # a fetch is up to GIT_TIMEOUT of stall per candidate for no gain. The
        # branch is cut from the tracking ref we hold; a remote that has since
        # moved is not our problem to solve on a dispatch tick (t_2a901db6).
        remote = source.split(":", 1)[1]
        tracking = f"refs/remotes/{remote}/{branch}"
        if not _git_out(target_repo, "rev-parse", "--verify", tracking):
            return False
        made = _git(target_repo, "branch", branch, tracking)
        return made is not None and made.returncode == 0
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
# base-current at claim time (1.2.0)
# --------------------------------------------------------------------------
#
# ``kanban_db_workspace._ensure_git_worktree`` cuts a NEW branch with
# ``git worktree add -b <branch> <target> HEAD`` — from the primary clone's
# HEAD, unfetched. Whatever branch the live clone is parked on becomes the base
# of every new card, and any agent that runs ``git checkout`` there re-bases all
# later cards. When the branch already EXISTS locally the kernel reuses it
# (``worktree add <target> <branch>``). So the narrowest fix that needs no
# kernel change is to make the branch exist, pointed at the local
# remote-tracking trunk ref, before the kernel looks. No fetch (dispatch lock),
# no checkout, HEAD/index/worktrees untouched. The cron script
# ``~/.hermes/scripts/base-current-watch.py`` keeps ``refs/remotes/origin/*``
# fresh; this phase only WARNS when it looks stale.

#: Env kill switch for the base-current phase alone.
ENV_BASE_CURRENT = "HERMES_KANBAN_BASE_CURRENT"

#: Age (minutes) of ``.git/FETCH_HEAD`` past which the repo is called stale.
ENV_BASE_CURRENT_MAX_AGE = "HERMES_BASE_CURRENT_MAX_AGE_MIN"
DEFAULT_BASE_CURRENT_MAX_AGE_MIN = 30

#: Env override for the fleet tenant map (tests); default ``<kanban home>/kanban-tenants.json``.
ENV_TENANTS_PATH = "HERMES_KANBAN_TENANTS"

_TRUNK_REF_PREFIXES = ("refs/remotes/origin/", "origin/", "refs/heads/")


def _tenants_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get(ENV_TENANTS_PATH) or "").strip()
    if override:
        return Path(override).expanduser()
    return _kanban_home() / "kanban-tenants.json"


def _load_tenants(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and not k.startswith("_")}


def _realpath(path: Path) -> str:
    try:
        return os.path.realpath(str(Path(path).expanduser()))
    except OSError:
        return str(path)


def _tenant_for_repo(repo: Path, tenants: Mapping[str, Dict[str, Any]]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The tenant whose ``primary_path`` IS ``repo`` (realpath both). None when none."""
    want = _realpath(repo)
    for slug, tenant in tenants.items():
        primary = tenant.get("primary_path")
        if isinstance(primary, str) and primary.strip() and _realpath(Path(primary)) == want:
            return slug, tenant
    return None


def _strip_trunk_prefix(name: str) -> str:
    name = name.strip()
    for prefix in _TRUNK_REF_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _resolve_trunk(repo: Path, tenant: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
    """``(trunk, how)`` — tenant ``trunk`` key, else ``refs/remotes/origin/HEAD``'s
    target, else ``main``. The trunk is a bare branch name (``main``)."""
    if tenant is not None:
        trunk = tenant.get("trunk")
        if isinstance(trunk, str) and trunk.strip():
            return _strip_trunk_prefix(trunk), "tenant"
    head = _git_out(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head and head.startswith("refs/remotes/origin/"):
        return _strip_trunk_prefix(head), "origin/HEAD"
    return "main", "default"


def _git_common_dir(repo: Path) -> Optional[Path]:
    out = _git_out(repo, "rev-parse", "--git-common-dir")
    if not out:
        return None
    common = Path(out)
    return common if common.is_absolute() else (repo / common)


def _fetch_head_age_min(repo: Path) -> Optional[float]:
    """Minutes since ``FETCH_HEAD`` was written; None when never fetched / unreadable."""
    common = _git_common_dir(repo)
    if common is None:
        return None
    try:
        import time as _time
        mtime = (common / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return None
    return max(0.0, (_time.time() - mtime) / 60.0)


def _max_age_min(env: Mapping[str, str]) -> float:
    raw = (env.get(ENV_BASE_CURRENT_MAX_AGE) or "").strip()
    try:
        value = float(raw) if raw else float(DEFAULT_BASE_CURRENT_MAX_AGE_MIN)
    except ValueError:
        value = float(DEFAULT_BASE_CURRENT_MAX_AGE_MIN)
    return value


def _warn_if_stale(repo: Path, env: Mapping[str, str]) -> Optional[float]:
    age = _fetch_head_age_min(repo)
    limit = _max_age_min(env)
    if age is None:
        logger.warning(
            "kanban-worktree-carryover[base-current]: %s has no FETCH_HEAD — it has never been "
            "fetched here; refs/remotes/origin/* may be stale (base-current-watch.py keeps it fresh).",
            repo,
        )
    elif age > limit:
        logger.warning(
            "kanban-worktree-carryover[base-current]: %s last fetched %.0f min ago (limit %.0f); "
            "new branches are cut from a possibly stale refs/remotes/origin trunk "
            "(is the base-current-watch cron job enabled?).",
            repo, age, limit,
        )
    return age


def base_current(task_id: str, board: Optional[str] = None, *, db_path: Optional[Path] = None,
                 env: Optional[Mapping[str, str]] = None,
                 tenants_path: Optional[Path] = None) -> Dict[str, Any]:
    """Make the card's branch exist at the local remote-tracking trunk ref BEFORE
    the kernel cuts it from HEAD. Never raises; only ever CREATES a local ref."""
    env = os.environ if env is None else env
    verdict: Dict[str, Any] = {
        "action": "noop", "reason": "", "branch": None, "target_repo": None,
        "trunk": None, "trunk_source": None, "base": None, "tenant": None,
        "fetch_head_age_min": None,
    }
    try:
        switch = str(env.get(ENV_BASE_CURRENT, "1")).strip().lower()
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
        task = (_load_board(db, task_id, board) or {}).get("task")
        if not task:
            verdict["reason"] = "task row unreadable"
            return verdict
        if (task.get("workspace_kind") or "") != "worktree":
            verdict["reason"] = "not a worktree card"
            return verdict

        branch = (task.get("branch_name") or "").strip()
        if not branch:
            # The kernel would default to wt/<id>; that is its business, not ours.
            verdict["reason"] = "card has no branch_name"
            return verdict
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

        verdict["fetch_head_age_min"] = _warn_if_stale(target_repo, env)

        if _branch_exists(target_repo, branch):
            # A retry keeps its commits; the cross-clone case was carry-over's.
            verdict["reason"] = "branch already present in target repo"
            return verdict

        tenants = _load_tenants(Path(tenants_path) if tenants_path is not None else _tenants_path(env))
        found = _tenant_for_repo(target_repo, tenants)
        tenant = found[1] if found else None
        verdict["tenant"] = found[0] if found else None
        trunk, how = _resolve_trunk(target_repo, tenant)
        verdict["trunk"], verdict["trunk_source"] = trunk, how

        tracking = f"refs/remotes/origin/{trunk}"
        base = _git_out(target_repo, "rev-parse", "--verify", "-q", tracking)
        if not base:
            verdict["reason"] = f"{tracking} does not exist in target repo"
            logger.warning(
                "kanban-worktree-carryover[base-current]: task %s — %s has no %s; leaving the "
                "kernel to cut %r from HEAD (unfetched clone or wrong trunk?).",
                task_id, target_repo, tracking, branch,
            )
            return verdict
        verdict["base"] = base

        made = _git(target_repo, "branch", "--no-track", branch, tracking)
        if made is None or made.returncode != 0 or not _branch_exists(target_repo, branch):
            verdict["reason"] = "git branch failed"
            logger.warning(
                "kanban-worktree-carryover[base-current]: task %s — could not create %r at %s in %s: %s",
                task_id, branch, tracking, target_repo,
                ((made.stderr or made.stdout or "").strip()[:400] if made else "no result"),
            )
            return verdict

        verdict["action"] = "based"
        verdict["reason"] = f"branch created at {tracking}"
        logger.info(
            "kanban-worktree-carryover[base-current]: task %s — created %r at %s (%s, trunk via %s) "
            "in %s so the worktree is cut from the fetched trunk, not the clone's parked HEAD.",
            task_id, branch, tracking, base[:12], how, target_repo,
        )
        _append_event(db, task_id, "base_current", {
            "branch": branch,
            "target_repo": str(target_repo),
            "trunk": trunk,
            "trunk_source": how,
            "base": base,
            "tenant": verdict["tenant"],
        })
        return verdict
    except Exception:  # noqa: BLE001 — fail open
        logger.debug("kanban-worktree-carryover[base-current]: unexpected error", exc_info=True)
        verdict["action"] = "noop"
        verdict["reason"] = verdict.get("reason") or "unexpected error"
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
    # Base-current runs AFTER carry-over on EVERY claim (first run included):
    # carry-over may have just created the branch, in which case this is a noop.
    try:
        if task_id:
            base_current(str(task_id), board)
    except Exception:  # noqa: BLE001
        logger.exception("kanban-worktree-carryover[base-current]: unexpected error; claim left untouched")


def register(ctx) -> None:
    ctx.register_hook(HOOK, on_task_claimed)
