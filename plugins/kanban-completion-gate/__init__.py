"""Charter rule 3 as a PLUGIN, not a core patch.

Re-expresses ``tools/kanban_tools.py::_complete_uncommitted_work_rejection`` onto
upstream's ``pre_tool_call`` hook. See plugin.yaml for why this patch was chosen as
the proving run for the 2026-09-09 "stop patching core" decision.

Contract (hermes_cli/plugins.py):
    return {"action": "block", "message": "..."}   -> tool call refused
    return None                                    -> allowed

IMPORT DISCIPLINE — revised 2026-09-09 after reading the pinned upstream tree.
``kanban_db.connect`` is NOT defined in upstream's ``hermes_cli/kanban_db.py`` any
more; it is one of 1,148 names served by a temporary ``PLUGIN-COMPAT`` __getattr__
shim that ``COMPAT_MANIFEST.md`` says is **removed on 2026-09-14**, after which an
affected plugin is not loaded at all. So ``connect`` is imported from its new home
``hermes_cli.kanban_db_connect`` with a fallback to the old path for the current
(pre-merge) fleet tree. ``get_task`` is still genuinely defined in ``kanban_db``.

REPEAT ESCALATION — the core version recorded a ``completion_blocked_uncommitted_work``
event and escalated its message on a repeat refusal. The event's ONLY consumer was that
counter (verified: the marker is read nowhere outside ``kanban_tools.py``), and writing
it needs ``_append_event`` + ``write_txn`` — a private name that the compat layer
explicitly does NOT restore, plus one that has moved. So the counter is kept
**in-process** instead: a worker retrying ``kanban_complete`` in the same run is exactly
the case the escalation exists for. Cross-run counting and the board-visible forensic
trail are a named, accepted loss; the refusal is still logged to the gateway log.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["register", "on_pre_tool_call", "_pr_gate", "_evaluate_pr"]

logger = logging.getLogger(__name__)

GIT_TIMEOUT_S = 10
MAX_SHOWN = 15

# task_id -> consecutive refusals in THIS process. Replaces the core version's
# task_events trail; see the module docstring for why.
_REFUSALS: Dict[str, int] = {}


def _kanban_api():
    """``(connect, get_task)`` from wherever they live in this tree.

    New path first: after upstream's Sep-2026 decomposition ``connect`` lives in
    ``hermes_cli.kanban_db_connect``. The old path still resolves today, but only
    through a shim with a 2026-09-14 delete date, and resolving through it emits a
    ``HermesPluginCompatWarning`` — so use the real home only.
    """
    # 2026-09-11: the ``kb.connect`` fallback is gone. It resolved through the
    # PLUGIN-COMPAT shim (deleted upstream 2026-09-14) in an import form upstream's
    # own scanner cannot see; ``kanban_db_connect`` exists on every tree we run.
    from hermes_cli import kanban_db as kb  # noqa: PLC0415
    from hermes_cli.kanban_db_connect import connect  # noqa: PLC0415
    return connect, kb.get_task


def _uncommitted_tracked(workspace: str) -> Optional[List[str]]:
    """Tracked-but-uncommitted paths in ``workspace``.

    Returns [] when clean, a list when dirty, and **None on any uncertainty** —
    no path, not a repo, git missing, timeout. None means ALLOW: the whole point
    of the gate is to stop work being lost, and refusing a completion we cannot
    reason about loses the run instead.
    """
    if not workspace or not os.path.isdir(workspace):
        return None
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=workspace, capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:          # not a repo, or git refused
        return None
    return [ln[3:].strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _message(dirty: List[str]) -> str:
    shown = "\n".join(f"  - {p}" for p in dirty[:MAX_SHOWN])
    more = f"\n  ...and {len(dirty) - MAX_SHOWN} more" if len(dirty) > MAX_SHOWN else ""
    return (
        "kanban_complete rejected: this card uses a `dir` workspace and has "
        f"{len(dirty)} TRACKED file(s) modified but NOT COMMITTED:\n"
        f"{shown}{more}\n\n"
        "A `dir` workspace is a SHARED working tree with no branch of its own. On "
        "2026-09-04 four cards were built, reviewed and marked done this way and the "
        "commit exists in no branch and no reflog — the next card clobbered the edits.\n\n"
        "Commit your work before reporting done:\n"
        "  git -C <workspace> add -A && git -C <workspace> commit -m '<what you did>'\n"
        "then call kanban_complete again with the commit SHA in your handoff. Your task "
        "is still in-flight; nothing was changed."
    )


def _repeat_message(dirty: List[str]) -> str:
    return (
        "kanban_complete rejected again: tracked edits are still uncommitted in this "
        f"`dir` workspace ({len(dirty)} file(s)). Do NOT keep retrying the completion — "
        "the run closing with this work uncommitted is precisely how it gets lost. If "
        "you cannot commit (no branch, wrong base, conflicting tree, missing identity), "
        "call kanban_block with the reason and the file list so it routes to someone who "
        "can. Blocking is safe; completing is not."
    )


# ---------------------------------------------------------------------------
# 0.3.0 (P3-done-requires-pr, 2026-09-23): "done requires an OPEN, GREEN PR
# against the trunk" for BUILD-LANE worktree cards of a ci_gate tenant.
#
# PLATFORM-FINDINGS 09-20 22:19: a build card could be built, pushed, reviewed
# and marked done with NO PR at all, while every landing mechanism keys on a PR
# number. The kernel gate (tools/kanban_ci_gate.py) only fires for
# ``scope: release`` titles, so it never saw the build card. This block fills
# that hole from the plugin side and leaves ``[Release]`` cards to the kernel.
#
# Three requirements, each refused BY NAME with the command that fixes it:
#   1. an OPEN PR whose head is ``tasks.branch_name`` and whose base is the trunk;
#   2. the PR head SHA == the pushed tip (refs/remotes/origin/<branch>, falling
#      back to the worktree HEAD when the remote-tracking ref is absent);
#   3. the tenant's ``ci_gate.check_name`` on that head is completed/success
#      (read from ``statusCheckRollup`` exactly as the kernel gate does).
# Plus: the completion's ``result`` must carry ``PR #<n>``; the hook returns an
# ``action: modify`` directive that appends it (hermes_cli/plugins.py honours
# modify directives from pre_tool_call — see _resolve_pre_tool_call_directive).
#
# Escape hatch: a body line ``done-without-pr: <reason>``.
# Kill switch: HERMES_KANBAN_PR_GATE_DISABLE=1.
# Fails OPEN (with a warning) on gh/git/network faults, like the rest of this
# plugin. The report for this package argues whether that is the right call.
# ---------------------------------------------------------------------------

GH_TIMEOUT_S = 25
PR_GATE_KILL_SWITCH = "HERMES_KANBAN_PR_GATE_DISABLE"
PR_GATE_LOG = "kanban-completion-gate[pr]"
_RELEASE_TITLE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*\[release\]", re.I)
_WAIVER = re.compile(r"^\s*done-without-pr\s*:\s*(\S.*)$", re.I | re.M)
_GREEN = {"success"}
_NEUTRAL = {"neutral", "skipped"}


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    # .../.hermes/hermes-agent/plugins/kanban-completion-gate/__init__.py -> .hermes
    return Path(__file__).resolve().parents[3]


TENANTS_PATH: Optional[Path] = None  # tests override; None -> <HERMES_HOME>/kanban-tenants.json


def _tenant_cfg(tenant: Optional[str]) -> Optional[dict]:
    """The tenant's map entry, or None when unknown/unreadable (out of scope)."""
    if not tenant:
        return None
    path = TENANTS_PATH or (_hermes_home() / "kanban-tenants.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    cfg = data.get(tenant)
    return cfg if isinstance(cfg, dict) else None


def _gh_bin() -> Optional[str]:
    found = shutil.which("gh")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
        if os.path.exists(cand):
            return cand
    return None


def _run(argv: List[str], cwd: Optional[str], timeout: int):
    """``(rc, stdout, stderr)``; rc<0 means the process could not be run at all."""
    try:
        p = subprocess.run(argv, cwd=cwd or None, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -124, "", f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)


def _pushed_tip(workspace: str, branch: str):
    """``(sha, source)`` where source is 'origin' or 'HEAD'; ``(None, reason)`` when unknown."""
    rc, out, _ = _run(["git", "-C", workspace, "rev-parse", "--verify", "--quiet",
                       f"refs/remotes/origin/{branch}"], None, GIT_TIMEOUT_S)
    if rc == 0 and out:
        return out, "origin"
    if rc < 0:
        return None, "git unavailable"
    rc, out, _ = _run(["git", "-C", workspace, "rev-parse", "HEAD"], None, GIT_TIMEOUT_S)
    if rc == 0 and out:
        return out, "HEAD"
    return None, "no readable HEAD"


class _Verdict:
    __slots__ = ("block", "message", "pr_number", "fail_open")

    def __init__(self, block=False, message="", pr_number=None, fail_open=""):
        self.block, self.message, self.pr_number, self.fail_open = block, message, pr_number, fail_open


def _evaluate_pr(repo: str, check_name: str, trunk: str, branch: str, workspace: str) -> _Verdict:
    """The three checks. Any gh/git/JSON fault -> ``fail_open`` set, never a block."""
    gh = _gh_bin()
    if not gh:
        return _Verdict(fail_open="gh not found on PATH")
    rc, out, err = _run([gh, "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
                         "--limit", "5", "--json",
                         "number,baseRefName,headRefOid,statusCheckRollup,mergeStateStatus"],
                        workspace if os.path.isdir(workspace) else None, GH_TIMEOUT_S)
    if rc != 0:
        return _Verdict(fail_open=f"gh pr list rc={rc}: {err or out or 'no output'}")
    try:
        prs = json.loads(out or "[]")
        if not isinstance(prs, list):
            raise ValueError("not a list")
    except Exception as exc:  # noqa: BLE001
        return _Verdict(fail_open=f"gh pr list returned unparseable JSON ({exc})")

    # 1. an open PR from this branch INTO THE TRUNK.
    on_trunk = [p for p in prs if (p.get("baseRefName") or "") == trunk]
    if not on_trunk:
        elsewhere = ", ".join(f"#{p.get('number')} -> {p.get('baseRefName')}" for p in prs)
        hint = f" (open PR(s) exist but not against {trunk}: {elsewhere})" if prs else ""
        return _Verdict(True, (
            f"MISSING 1/3 — no OPEN pull request from `{branch}` into `{trunk}` on {repo}{hint}.\n"
            "Every landing mechanism keys on a PR number; a branch with no PR cannot be landed.\n"
            "Fix: push and open the PR against the trunk:\n"
            f"  git -C {workspace or '<worktree>'} push -u origin {branch}\n"
            f"  gh pr create --repo {repo} --head {branch} --base {trunk} --fill\n"
            "then call kanban_complete again with `PR #<n>` in your result."))
    pr = on_trunk[0]
    number = pr.get("number")
    head = (pr.get("headRefOid") or "").strip()

    # 2. PR head == the pushed tip.
    tip, source = _pushed_tip(workspace, branch)
    if tip is None:
        logger.warning("%s: cannot read the pushed tip for %s (%s); skipping head check",
                       PR_GATE_LOG, branch, source)
    elif head and tip != head:
        where = (f"refs/remotes/origin/{branch}" if source == "origin"
                 else f"the worktree HEAD (no refs/remotes/origin/{branch} — the branch may never "
                      "have been pushed from this checkout)")
        return _Verdict(True, (
            f"MISSING 2/3 — PR #{number} head {head[:10]} != {where} {tip[:10]}.\n"
            "The PR does not contain what this card built. Fix: push your tip (or fetch if the "
            "local ref is stale):\n"
            f"  git -C {workspace or '<worktree>'} push origin {branch}\n"
            f"  git -C {workspace or '<worktree>'} fetch origin {branch}\n"
            "then wait for CI on the new head and complete again."), pr_number=number)

    # 3. the named check is completed/success on that head — mirrors tools/kanban_ci_gate.py.
    rollup = pr.get("statusCheckRollup") or []
    named = [c for c in rollup if isinstance(c, dict) and (c.get("name") or c.get("context")) == check_name]
    watch = f"  gh pr checks {number} --repo {repo} --watch"
    if not named:
        seen = sorted({(c.get("name") or c.get("context") or "?") for c in rollup if isinstance(c, dict)})
        return _Verdict(True, (
            f"MISSING 3/3 — no check named {check_name!r} on PR #{number} head {head[:10]} "
            f"(present: {', '.join(seen) or 'none'}). CI has not started or the workflow is not on "
            f"this branch. Fix: wait for CI, then complete again:\n{watch}"), pr_number=number)
    check = named[0]
    status = (check.get("status") or "").lower()
    conclusion = (check.get("conclusion") or "").lower()
    if status and status != "completed":
        return _Verdict(True, (
            f"MISSING 3/3 — {check_name} is {status!r} on PR #{number} head {head[:10]}: still "
            f"running. Pending is not green. Fix: wait for CI, then complete again:\n{watch}"),
            pr_number=number)
    if conclusion in _GREEN:
        return _Verdict(False, pr_number=number)
    if conclusion in _NEUTRAL:
        return _Verdict(True, (
            f"MISSING 3/3 — {check_name} concluded {conclusion!r} on PR #{number}: the suite did "
            f"not run, which is not evidence of a passing build. Re-run it and wait:\n{watch}"),
            pr_number=number)
    return _Verdict(True, (
        f"MISSING 3/3 — {check_name} FAILED on PR #{number} head {head[:10]} (conclusion "
        f"{conclusion!r}). Read it with `gh run view --log-failed`, fix, push, and complete again "
        f"once it is green:\n{watch}"), pr_number=number)


def _pr_gate(task, task_id: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build-lane worktree card of a ci_gate tenant: require open+green PR on the trunk."""
    if os.environ.get(PR_GATE_KILL_SWITCH, "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    title = getattr(task, "title", None) or ""
    if _RELEASE_TITLE.search(title):
        return None                   # [Release] Land cards belong to the kernel CI gate
    cfg = _tenant_cfg(getattr(task, "tenant", None))
    ci = (cfg or {}).get("ci_gate")
    if not isinstance(ci, dict) or not ci.get("repo") or not ci.get("check_name"):
        return None                   # tenant has no CI gate: out of scope
    body = getattr(task, "body", None) or ""
    waiver = _WAIVER.search(body)
    if waiver:
        logger.info("%s: %s waived by body line done-without-pr: %s", PR_GATE_LOG, task_id,
                    waiver.group(1).strip())
        return None
    branch = (getattr(task, "branch_name", None) or "").strip()
    trunk = (cfg.get("trunk") or "main").strip()
    workspace = getattr(task, "workspace_path", None) or ""
    if not branch:
        logger.warning("%s: %s is a worktree card with no branch_name; allowing", PR_GATE_LOG, task_id)
        return None

    verdict = _evaluate_pr(ci["repo"], ci["check_name"], trunk, branch, workspace)
    if verdict.fail_open:
        logger.warning("%s: could not verify PR for %s (%s) — FAILING OPEN", PR_GATE_LOG, task_id,
                       verdict.fail_open)
        return None
    if verdict.block:
        logger.warning("%s: refusing kanban_complete on %s — %s", PR_GATE_LOG, task_id,
                       verdict.message.splitlines()[0])
        return {"action": "block", "message": (
            "kanban_complete rejected: done means an OPEN, GREEN pull request against the trunk, "
            "not a built branch.\n\n" + verdict.message + "\n\nEscape hatch for docs-only or spike "
            "cards: a body line `done-without-pr: <reason>`. Your task is still in-flight; nothing "
            "was changed.")}

    # All three satisfied: make sure the handoff names the PR so landing can key on it.
    tag = f"PR #{verdict.pr_number}"
    result = args.get("result") or ""
    summary = args.get("summary") or ""
    if tag in result or tag in summary:
        return None
    new_result = (result.rstrip() + ("\n" if result.strip() else "") + tag)
    logger.info("%s: %s satisfied; appending '%s' to result", PR_GATE_LOG, task_id, tag)
    return {"action": "modify", "args": {"result": new_result}}


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, str]]:
    """Block ``kanban_complete`` when a worker's `dir` workspace is dirty, or when a
    build-lane `worktree` card of a ci_gate tenant has no open, green PR on the trunk."""
    try:
        if payload.get("tool_name") != "kanban_complete":
            return None

        args = payload.get("args") or {}
        # Worker completions only. The orchestrator and the CLI pass through —
        # they are not the path that loses work. The card id comes from the
        # worker's environment: the hook payload's ``task_id`` is the AGENT's
        # effective task id (a fresh uuid4 for a `hermes chat -q` worker), never
        # the card id, so keying on it made this gate a silent no-op from
        # 2026-09-09 to 2026-09-11 (found by the catchup2 canary; fix manifest
        # gate-plugin-fix-20260911).
        task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
        if not task_id:
            return None
        arg_tid = args.get("task_id") or args.get("id") or ""
        if arg_tid and arg_tid != task_id:
            return None       # completing some other card: not this worker's workspace

        connect, get_task = _kanban_api()
        conn = connect()
        try:
            task = get_task(conn, task_id)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        if task is None:
            return None
        kind = getattr(task, "workspace_kind", None) or ""
        if kind == "worktree":
            # 0.3.0: build-lane worktree cards of a ci_gate tenant need an open,
            # green PR on the trunk. `scratch` has no repo; `dir` is handled below.
            return _pr_gate(task, task_id, args)
        if kind != "dir":
            return None

        dirty = _uncommitted_tracked(getattr(task, "workspace_path", "") or "")
        if not dirty:                     # [] clean, or None uncertain -> allow
            _REFUSALS.pop(task_id, None)  # a clean pass resets the streak
            return None

        n = _REFUSALS.get(task_id, 0) + 1
        _REFUSALS[task_id] = n
        logger.warning(
            "kanban-completion-gate: refusing kanban_complete on %s (refusal %d in this "
            "run) — %d tracked file(s) uncommitted in %s", task_id, n, len(dirty),
            getattr(task, "workspace_path", "?"),
        )
        msg = _message(dirty) if n == 1 else _repeat_message(dirty)
        return {"action": "block", "message": msg}

    except Exception:  # noqa: BLE001
        # A gate must never crash a worker turn. Closing the run is exactly how the
        # work gets lost, which is the thing this exists to prevent.
        logger.exception("kanban-completion-gate: unexpected error, allowing")
        return None


def register(ctx) -> None:
    """Register the pre_tool_call gate."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
