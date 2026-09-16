"""Zero-delta rework guard, on upstream's ``pre_tool_call`` hook.

See plugin.yaml for why this is a PLUGIN and not the core patch the card first
considered. Contract (``hermes_cli/plugins.py``):

    return {"action": "block", "message": "..."}   -> tool call refused
    return {"action": "modify", "args": {...}}     -> tool input transformed
    return None                                    -> allowed

Imports NOTHING from ``hermes_cli`` at import time. The two store reads are narrow,
lazy, exception-guarded calls to the SAME functions the kernel uses —
``kanban_db_connect.connect_closing`` + ``kanban_db.get_task`` — which is what makes
this guard decision-exact rather than a second opinion. Same shape
``kanban-mint-guard`` (profiles.profile_exists) and ``kanban-project-link-guard``
(projects_db.get_project) already use.

THE DEFECT THIS CLOSES (card t_48b49faa, measured on card t_8dd715c6, 2026-09-16)
--------------------------------------------------------------------------------
Round-2 review requested changes at head ``4136e1e``. The rework round produced
``git diff --stat 4136e1e..HEAD`` -> 0 files, 0 insertions, 0 deletions, yet its
``review_requested`` handoff read "Implementation is present and verified with 5/5
focused briefing tests, 37/37 frontend tests, 25/25 backend briefing tests...".
Nothing at the routing layer compared the head the reviewer REJECTED with the head
being handed BACK, so a non-attempt was indistinguishable from a rework. Cost: one
worker round plus one review round on byte-identical code, and the reviewer only
caught it because a delta check occurred to them.

THE CHECK — two transitions, one variable
-----------------------------------------
  * ``kanban_request_changes`` (the reviewer's verdict) RECORDS the head of the
    card's worktree: "the sha this changes_requested was raised at". The reviewer's
    own process reads the same worktree the implementer wrote.
  * ``kanban_request_review`` (the implementer's handoff) REFUSES when that exact
    tree is handed back: ``git diff --stat <recorded>..<HEAD>`` is EMPTY.

Why the record is written by the VERDICT and not by the handoff: a handoff can fail
AFTER this hook (the pre-review ladder bounces, parents are unsatisfied), and a record
written then would make the immediate retry look like a repeat. The verdict is the only
writer, so the guard's answer never changes because a previous attempt failed.

Scope, deliberately narrow:
  * only ``workspace_kind == 'worktree'`` cards — a shared ``dir`` workspace has no
    per-card head, so there is nothing to compare and the guard stands down;
  * only when a changes_requested VERDICT is on record AND that verdict's run actually
    landed (``task_runs.outcome == 'changes_requested'``) — a stray verdict the kernel
    refused must not wedge a legitimate handoff;
  * zero delta means identical TREES: an empty commit moves HEAD without moving
    content and is still a zero-delta rework.

Escape hatch — ``delta-exempt: <reason>`` in the handoff's ``metadata`` or ``summary``.
The value must be a real token, never a bare marker or a placeholder: a card whose
deliverable genuinely has no commit (verification, audit, documentation) is real, and
so is the temptation to write the marker without saying why.

Fail-open in every other direction: no state, no worktree, an unreadable store, an
unresolvable sha, a non-git workspace, any exception at all -> the call proceeds exactly
as it does today. A guard that refuses a handoff the review lane would have accepted
stops the board, which is worse than the defect it prevents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "register", "on_pre_tool_call", "verdict", "refusal_reason", "delta_exempt",
    "state_path", "read_state", "git_head", "git_delta", "record_verdict",
]

logger = logging.getLogger(__name__)

_TOOL_VERDICT = "kanban_request_changes"
_TOOL_HANDOFF = "kanban_request_review"

GIT_TIMEOUT_S = 4
# Four seconds, and the number is a control, not a guess: this callback runs inside
# ``pre_tool_call``, which fails CLOSED on the hook timeout (30s here). Two git calls at
# 10s each put a wedged git one slow filesystem away from turning a legit handoff into a
# timeout refusal — the exact failure this guard exists to prevent. The calls are a local
# ``rev-parse`` and a local ``diff --stat``, milliseconds in practice.
STAT_CHARS = 1200
SHORT = 10

# A quoted placeholder is an illustration, not a reason: `delta-exempt: <reason>` is how
# this guard's own refusal message and its card write the marker down.
PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|\.\.\.|…|x|n/?a|tbd)$", re.I)
# The marker must CARRY a value (the mint-guard lesson of 2026-09-16: a bare marker is
# switched off by any body that merely quotes it). The key may be quoted — a metadata
# mapping serialised to JSON reads `{"delta-exempt": "..."}` — so a closing quote is
# tolerated between the key and its colon.
_EXEMPT = re.compile(r"delta[-_ ]exempt[\"']?[ \t]*[:=][ \t]*(?P<v>[^\s`<>{}]+)", re.I)
_EXEMPT_KEY = re.compile(r"^delta[-_ ]exempt$", re.I)


# --- state ------------------------------------------------------------------
# Keyed by TASK, one file per card: the writer (a reviewer's process) and the reader
# (the implementer's process) are different subprocesses, in different PROFILE HOMES
# (the dispatcher sets HERMES_HOME=profiles/<name> for axel/switch/brain/... workers),
# so the anchor must be the fleet ROOT, not HERMES_HOME as handed to us.
def _root_home() -> Path:
    raw = (os.environ.get("HERMES_HOME") or os.environ.get("HERMES_REAL_HOME") or "").strip()
    p = Path(raw).expanduser() if raw else (Path.home() / ".hermes")
    if p.parent.name == "profiles":          # profile-shaped -> the root is the grandparent
        p = p.parent.parent
    return p


def state_path(task_id: str) -> Path:
    """``<root>/state/review-delta/<task_id>.json`` — the reviewed-head record."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(task_id or "").strip())[:80]
    return _root_home() / "state" / "review-delta" / f"{safe}.json"


def read_state(task_id: str) -> Dict[str, Any]:
    """The card's record, or ``{}``. Never raises."""
    try:
        return json.loads(state_path(task_id).read_text())
    except Exception:                        # noqa: BLE001 -- absent/corrupt == no record
        return {}


def _write_state(task_id: str, record: Dict[str, Any]) -> None:
    """Atomic best-effort write: a guard that crashes the tool does more damage than a
    guard that forgets. A tmp file + ``os.replace`` so a reader never sees half a record."""
    try:
        path = state_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, sort_keys=True))
        os.replace(tmp, path)
    except Exception:                        # noqa: BLE001
        logger.debug("review-delta-guard: could not record state for %s", task_id, exc_info=True)


# --- git --------------------------------------------------------------------
def _run_argv(argv: List[str], cwd: Optional[str]) -> Optional[tuple[int, str]]:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception:                        # noqa: BLE001 -- missing git, bad path, timeout
        return None


def git_head(workspace: Optional[str]) -> Optional[str]:
    """``HEAD`` of the worktree, or ``None`` when it cannot be read (not a repo, gone)."""
    if not workspace or not os.path.isdir(workspace):
        return None
    got = _run_argv(["git", "-C", str(workspace), "rev-parse", "HEAD"], None)
    if not got:
        return None
    rc, out = got
    head = (out or "").strip().splitlines()[0].strip() if out.strip() else ""
    return head if rc == 0 and re.fullmatch(r"[0-9a-fA-F]{7,64}", head or "") else None


def git_delta(workspace: Optional[str], base: Optional[str], head: Optional[str]) -> Optional[str]:
    """``git diff --stat <base>..<head>`` in *workspace*.

    ``""``  -> the trees are identical (the zero delta this guard refuses; an empty
              commit lands here too, which is the point).
    text    -> a real delta; the stat is the proof attached to the handoff.
    ``None``-> undecidable (unknown sha, not a repo, git absent, timeout) -> ALLOW.
    """
    if not workspace or not base or not head or not os.path.isdir(workspace):
        return None
    got = _run_argv(["git", "-C", str(workspace), "diff", "--stat", f"{base}..{head}", "--"], None)
    if not got:
        return None
    rc, out = got
    if rc != 0:
        return None
    return out.strip()


# --- card resolution --------------------------------------------------------
def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def resolve_card(args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The card this call is about: ``{"id", "workspace", "workspace_kind", "assignee",
    "run_id"}``, or ``None`` (unknown, unreadable, or not a worktree card).

    The id comes from the args, then the worker's own ``HERMES_KANBAN_TASK`` — the same
    var ``kanban_tools._default_task_id`` resolves its id from — then a unique workspace
    match. The fallback is not decoration: a gateway session or CLI caller can reach these
    tools with no worker environment at all, and a delegated child has the kanban identity
    vars scrubbed (``agent.delegation_context.KANBAN_ENV_KEYS``), so an id is never assumed
    from the ambient environment alone.
    """
    tid = _clean(args.get("task_id")) or _clean(os.environ.get("HERMES_KANBAN_TASK"))
    workspace = _clean(os.environ.get("HERMES_KANBAN_WORKSPACE"))
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            row_id = tid
            if row_id is None:
                if not workspace:
                    return None
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE workspace_path = ? AND workspace_kind = 'worktree' "
                    "AND status NOT IN ('done', 'archived') ORDER BY id DESC",
                    (workspace,),
                ).fetchall()
                if len(rows) != 1:           # ambiguous path -> not our business
                    return None
                row_id = rows[0]["id"]
            task = kb.get_task(conn, row_id)
            if task is None or getattr(task, "workspace_kind", None) != "worktree":
                return None
            return {
                "id": task.id,
                "workspace": task.workspace_path,
                "workspace_kind": task.workspace_kind,
                "assignee": task.assignee,
                "run_id": task.current_run_id,
            }
    except Exception:                        # noqa: BLE001 -- fail OPEN, never block on our own error
        logger.debug("review-delta-guard: card lookup failed, allowing", exc_info=True)
        return None


def run_outcome(run_id: Any) -> Optional[str]:
    """``task_runs.outcome`` for *run_id*, or ``None`` (unknown/unreadable)."""
    if run_id is None:
        return None
    try:
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing() as conn:
            row = conn.execute(
                "SELECT outcome FROM task_runs WHERE id = ?", (int(run_id),)
            ).fetchone()
            return row["outcome"] if row is not None else None
    except Exception:                        # noqa: BLE001
        return None


# --- decisions (pure) -------------------------------------------------------
def delta_exempt(args: Dict[str, Any]) -> Optional[str]:
    """The declared exemption reason, or ``None``. Reads the handoff's ``summary`` and
    ``metadata`` — the two places a worker can speak — and honours only a real value.

    ``metadata`` is read STRUCTURALLY first: a dict key ``delta-exempt`` carries its
    reason whole, so `{"delta-exempt": "verification-only card"}` is a reason and not
    the first token of one. Prose (``summary``, and any nested string) falls back to
    the marker regex, where only a non-placeholder first token can satisfy the hatch.
    """
    metadata = args.get("metadata")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if isinstance(key, str) and _EXEMPT_KEY.match(key.strip()):
                reason = str(value or "").strip()
                if reason and not PLACEHOLDER.match(reason):
                    return reason
    candidates: List[str] = []
    summary = args.get("summary")
    if isinstance(summary, str):
        candidates.append(summary)
    if isinstance(metadata, (dict, list)):
        try:
            candidates.append(json.dumps(metadata))
        except Exception:                    # noqa: BLE001
            pass
    for text in candidates:
        for match in _EXEMPT.finditer(text or ""):
            value = match.group("v").strip().strip("`*_\"'").rstrip(".,;:")
            if value and not PLACEHOLDER.match(value):
                return value
    return None


def refusal_reason(
    *,
    rejected_head: Optional[str],
    head_now: Optional[str],
    stat: Optional[str],
    exempt: Optional[str],
) -> Optional[str]:
    """Why this handoff is refused, or ``None`` to allow. Pure — the caller does the I/O."""
    if exempt:
        return None
    if not rejected_head or not head_now:
        return None
    if stat is None:                          # undecidable sha / not a repo -> allow
        return None
    if stat.strip():                          # the head moved, with content -> allow
        return None
    return (f"HEAD {head_now[:SHORT]} carries the same tree as {rejected_head[:SHORT]}, "
            f"the head the last changes_requested was raised at")


def verdict(
    *,
    rejected_head: Optional[str],
    rejected_run_id: Any,
    head_now: Optional[str],
    stat: Optional[str],
    exempt: Optional[str],
    outcome_of_run: Optional[str],
) -> Optional[str]:
    """The full decision, with the verdict-landed check included.

    ``outcome_of_run`` is what the board says about the run that raised the verdict: a
    record whose run did NOT land as ``changes_requested`` is a stray call the kernel
    refused, and must not wedge a legitimate handoff.
    """
    if rejected_run_id is not None and outcome_of_run != "changes_requested":
        return None
    return refusal_reason(
        rejected_head=rejected_head, head_now=head_now, stat=stat, exempt=exempt,
    )


# --- messages ---------------------------------------------------------------
def _message(reason: str, card: Dict[str, Any], record: Dict[str, Any], head_now: str) -> str:
    base = str(record.get("rejected_head") or "")
    ws = str(card.get("workspace") or "")
    run_id = record.get("rejected_run_id")
    reviewer = record.get("reviewer") or "a reviewer"
    return (
        f"Refusing to hand this back to review: the reviewed tree is unchanged.\n\n"
        f"  card:                 {card.get('id')}\n"
        f"  changes requested at: {base[:SHORT]}  (run {run_id} by {reviewer}, the last "
        f"review verdict on this card)\n"
        f"  HEAD now:             {head_now[:SHORT]}\n"
        f"  git -C {ws} diff --stat {base[:SHORT]}..HEAD\n"
        f"      -> (no output: identical trees)\n\n"
        f"The review lane has already reviewed THIS tree and asked for changes. Handing it\n"
        f"back spends a worker round and a review round re-deriving the same findings, and\n"
        f"the handoff reads as a truthful completion (\"present and verified\") while not one\n"
        f"byte of the reviewed code has changed. That is the defect this guard exists for:\n"
        f"card t_8dd715c6, run 1828 — 0 files changed, 0 insertions, 0 deletions, handoff\n"
        f"\"Implementation is present and verified with 5/5 focused briefing tests\".\n\n"
        f"Three ways forward:\n"
        f"  1. MOVE THE HEAD — commit the work you have done:\n"
        f"       cd {ws}\n"
        f"       git status --porcelain            # uncommitted work is invisible to a\n"
        f"                                         # review that reads commits\n"
        f"       git add -A && git commit -m \"...\"\n"
        f"       git diff --stat {base[:SHORT]}..HEAD   # must be non-empty now\n"
        f"     then call kanban_request_review again.\n\n"
        f"  2. NAME THE BLOCKER, with the command output that proves it — if the code cannot\n"
        f"     change (the card is wrong, a dependency or a credential is missing):\n"
        f"       kanban_block(kind=\"needs_input\", reason=\"<why>\" + the output of\n"
        f"         `git -C {ws} diff --stat {base[:SHORT]}..HEAD`)\n\n"
        f"  3. DELIBERATE NO-COMMIT CARD — if this card's deliverable really is verification,\n"
        f"     audit or documentation, with no code to commit, say so:\n"
        f"       kanban_request_review(summary=..., metadata={{\"delta-exempt\": \"<reason>\"}})\n"
        f"     (a `delta-exempt: <reason>` line in the summary works too; the reason must be\n"
        f"     a real one, not the bare marker.)\n\n"
        f"Nothing was changed: your card is still in its lane and this refusal is not\n"
        f"counted as a failure. Detected by: {reason}."
    )


# --- hooks ------------------------------------------------------------------
def record_verdict(args: Dict[str, Any]) -> None:
    """A reviewer's ``kanban_request_changes``: remember the head they just rejected."""
    card = resolve_card(args)
    if not card:
        return
    head = git_head(card.get("workspace"))
    if not head:
        return
    run_id = card.get("run_id")
    _write_state(card["id"], {
        "task_id": card["id"],
        "rejected_head": head,
        # Only a real run id is stored: the read side treats a recorded run id as a promise
        # that the verdict can be verified on the board, and a non-integer would silently
        # disable that check instead of failing it.
        "rejected_run_id": run_id if isinstance(run_id, int) else None,
        "reviewer": card.get("assignee"),
        "workspace": card.get("workspace"),
        "recorded_at": int(time.time()),
    })


def _guard_handoff(args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An implementer's ``kanban_request_review``: refuse a zero-delta re-request, and
    otherwise hand the review the delta as proof."""
    card = resolve_card(args)
    if not card:
        return None
    record = read_state(card["id"])
    if not record or record.get("task_id") != card["id"]:
        return None
    rejected_head = _clean(record.get("rejected_head"))
    if not rejected_head:
        return None
    head_now = git_head(card.get("workspace"))
    if not head_now:
        return None
    exempt = delta_exempt(args)
    stat = None if exempt else git_delta(card.get("workspace"), rejected_head, head_now)
    outcome = run_outcome(record.get("rejected_run_id")) if not exempt else None
    reason = verdict(
        rejected_head=rejected_head, rejected_run_id=record.get("rejected_run_id"),
        head_now=head_now, stat=stat, exempt=exempt, outcome_of_run=outcome,
    )
    if reason:
        logger.warning("review-delta-guard: refusing %s for %s — %s",
                       _TOOL_HANDOFF, card["id"], reason)
        return {"action": "block", "message": _message(reason, card, record, head_now)}
    # Allowed. Bullet 1 of the fix: the handoff CARRIES the delta, so the reviewer sees
    # what changed since the verdict instead of re-deriving whether anything did.
    proof: Dict[str, Any] = {
        "review_delta_base": rejected_head,
        "review_delta_head": head_now,
    }
    if exempt:
        proof["review_delta_exempt"] = exempt
    elif stat is not None and stat.strip():
        proof["review_delta_stat"] = stat[:STAT_CHARS]
    else:
        return None                            # nothing to say (undecidable) -> untouched
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return None                            # kernel's shape; never touch a foreign type
    merged = {**(metadata or {}), **proof}
    return {"action": "modify", "args": {"metadata": merged}}


def on_pre_tool_call(**payload: Any) -> Optional[Dict[str, Any]]:
    try:
        tool = payload.get("tool_name")
        if tool not in (_TOOL_VERDICT, _TOOL_HANDOFF):
            return None
        args = payload.get("args")
        args = args if isinstance(args, dict) else {}
        if tool == _TOOL_VERDICT:
            record_verdict(args)
            return None                        # an observer here: never blocks a verdict
        return _guard_handoff(args)
    except Exception:                          # noqa: BLE001
        # Never stop a review from being requested because this guard had a bad day.
        logger.exception("review-delta-guard: unexpected error, allowing")
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
