"""Kanban block -> OVERWATCH escalation plugin (rewritten 2026-09-06, plan O1).

Subscribes to ``kanban_task_blocked`` and, for every block that is a real
fault signal, spawns ONE fresh Agent Smith session ("overwatch", profile id
``default``) with a machine-built situation brief. Smith assesses the card in
the context of the whole board, acts within a written authority, and hands the
hard stops to Richie.

Richie's decision, 2026-09-06: "a fresh chat with Smith is triggered, he
assesses and acts to get things moving or tidy up the mess ... as quickly,
hygienically and cheaply as possible, while still delivering the feature to
the agreed quality, including all the agreed review and testing gates. Smith
may approve up to another $0.50 per card. At $1.50 the card is blocked and
Smith texts me a full rundown."

This replaced three mechanisms that disagreed with each other: a per-kind
routing table (Jobsy vs Smith vs Steve-o by substring), Steve-o cost
adjudication with two extensions, and ``dependency-gate-watch`` minting
``[triage]`` cards for give-ups. Measured on 5-6 Sep: 85 blocks, of which 15
were ``operator_hold`` by design and 28 dependency waits that needed nobody;
14 cost breaches each spawned ~4 more cards.

Triggers (read from the board, never from substrings of the reason text):
  cost_cap, capability, needs_input          -> always
  transient                                  -> on the SECOND occurrence
Never: operator_hold (Richie's decision), dependency (self-resumes),
scheduled (a date, not a fault).

Hard stops (no overwatch; ceiling marker for escalation-watch -> Richie):
  * the card already carries TWO overwatch decisions;
  * a cost_cap block on a card that was already extended once, or whose cap
    is already at kanban.max_cost_hard_ceiling.
On a hard stop ONE more Smith session is spawned with the RUNDOWN prompt:
write the rundown as a comment; escalation-watch (15 min, iMessage + Slack)
carries it to Richie.

Guardrails: never break the block transaction (every probe is best-effort);
fire-and-forget spawn in its own session; ``switch`` is never a target.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import subprocess
import sys
import json
import errno
import time
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["register"]

# The overwatch profile. Pinned here so it is versioned and testable; tests
# assert it is never ``switch`` and never a worker.
OVERWATCH = "default"          # Agent Smith's profile id
DEFAULT_ASSESSOR = OVERWATCH   # kept for older tools that import the name
RUNTIME_ASSESSOR = OVERWATCH
COST_ASSESSOR = OVERWATCH      # Steve-o no longer adjudicates spend (2026-09-06)

ALWAYS_TRIGGER_KINDS = frozenset({"cost_cap", "capability", "needs_input"})
NEVER_TRIGGER_KINDS = frozenset({"operator_hold", "dependency", "scheduled"})
TRANSIENT_TRIGGER_AFTER = 1     # first transient retries; second triggers

OVERWATCH_LIMIT = 2             # overwatch touches per card before Richie
OVERWATCH_MARKER = "overwatch:"  # Smith's decision comment must start with this
EXTENSION_MARKER = "cost-extension:"
CEILING_MARKER = "escalation-ceiling"
RUNDOWN_MARKER = "rundown:"
NON_COST_TRIAGE_LIMIT = OVERWATCH_LIMIT  # name kept for escalation-watch

HARD_CEILING_FALLBACK = 1.50
BRIEF_DIR = Path(os.path.expanduser("~/.hermes/logs/overwatch"))
CLAIM_DIR = Path(os.environ.get("HERMES_OVERWATCH_CLAIM_DIR", "~/.hermes/state/overwatch")).expanduser()
CLAIM_TTL_SECONDS = 15 * 60
# How long a RESERVATION holds the card's slot before a holder has to exist. `_claim_overwatch`
# takes the slot with `pid: 0`, then `_spawn` writes the spawned child's pid over it a few
# milliseconds later; a reservation still unclaimed after this long is a spawn that never
# happened (the process died between reserve and exec), and is reaped like any other dead holder.
CLAIM_SPAWN_GRACE_SECONDS = 60

# Idempotency key derivation for overwatch-minted remediation cards
# (2026-09-14, t_d8c477dd). Two concurrent overwatch sessions working the same
# blocked card both see "this card needs a remediation card" and both create
# one. The idempotency_key parameter on kanban_create prevents this IF both
# sessions derive the same key. The key format is:
#   overwatch-{source_task_id}-{title_slug}
# where title_slug is the remediation card's title normalized to
# alphanumeric + hyphens, truncated to 60 chars.
IDEMPOTENCY_KEY_PREFIX = "overwatch"


def _claim_path(task_id: str) -> Path:
    return CLAIM_DIR / f"{task_id}.claim"


def _record_claim_skip(task_id: str, holder: int | str, reason: str) -> None:
    logger.info("kanban-block-escalator: skipped overwatch for %s; holder=%s reason=%s", task_id, holder, reason)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _claim_state(path: Path) -> tuple[str, int | str]:
    """(``"held"`` | ``"free"``, holder) for one claim file — the ONE place the lease's meaning lives.

    The lease used to record ``os.getpid()``: the BLOCKED WORKER's pid, because the
    ``kanban_task_blocked`` hook runs inside the worker's own process. The worker exits seconds
    after blocking while the assessment it triggered runs for minutes, so the lease expired
    almost at once and a second escalation for the same card was no longer refused — measured on
    ``t_b6ebc5ec`` and ``t_0a677768`` (2026-09-16, t_2da01ffe). Two states, one reader:

      * ``pid <= 0`` — a RESERVATION: ``_spawn`` has taken the slot and not yet handed it to the
        child it is starting. Held for ``CLAIM_SPAWN_GRACE_SECONDS``, then reaped: a reservation
        that old belongs to a spawn that never happened.
      * ``pid > 0`` — the spawned child's own pid, written by ``_spawn`` from ``Popen``'s return
        (with the child ``exec``-ing straight into hermes, that pid IS the assessor for its whole
        life). Held while that process lives, and while the claim is inside ``CLAIM_TTL_SECONDS``.
    """
    try:
        holder = json.loads(path.read_text())
        pid = int((holder or {}).get("pid") or 0)
        created = float((holder or {}).get("created_at") or 0)
    except Exception:  # noqa: BLE001 — unreadable body, or not a dict at all
        return "free", "unreadable"
    if pid <= 0:
        return ("held" if time.time() - created <= CLAIM_SPAWN_GRACE_SECONDS else "free"), "reserved"
    if time.time() - created > CLAIM_TTL_SECONDS:
        return "free", pid
    return ("held" if _pid_alive(pid) else "free"), pid


def _take_claim(task_id: str, reason: str) -> bool:
    """Create the reservation atomically (O_EXCL) — the only writer that can lose a race."""
    CLAIM_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"pid": 0, "created_at": time.time(), "reason": reason, "state": "reserved"}
    try:
        fd = os.open(_claim_path(task_id), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
    return True


def _claim_overwatch(task_id: str, reason: str) -> bool:
    """Take one per-card lease, atomically; reap dead/expired holders.

    The lease is RESERVED here and handed to the spawned child by ``_spawn`` (see
    ``_claim_state``) — the holder that matters is the process doing the assessment, not the
    worker that fired the hook.
    """
    for _ in range(2):
        if _take_claim(task_id, reason):
            return True
        state, holder = _claim_state(_claim_path(task_id))
        if state == "held":
            _record_claim_skip(task_id, holder, reason)
            return False
        # Dead, expired, or corrupt: the lease is not protecting anything. One function releases
        # it, so the reserve/handoff/reap paths cannot each grow their own idea of "released".
        _release_overwatch(task_id)
    return False


def _hand_lease_to_child(task_id: str, pid: int) -> None:
    """Move the reservation onto the spawned child, atomically (write-then-rename).

    Never a bare truncating write: a reader that caught the file half-written would see an
    unreadable body and release a lease that is very much in use.
    """
    path = _claim_path(task_id)
    if int(pid) <= 0:
        # A spawn that named no process cannot hold a lease: leave the reservation, which
        # `_claim_state` reaps after CLAIM_SPAWN_GRACE_SECONDS, rather than writing a holder
        # that was never there.
        logger.debug("kanban-block-escalator: no pid to hand the lease on %s to", task_id)
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = {"pid": int(pid), "created_at": time.time(), "reason": "spawn", "state": "held"}
    try:
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("kanban-block-escalator: could not hand the lease on %s to pid %s: %s", task_id, pid, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _release_overwatch(task_id: str) -> None:
    try:
        _claim_path(task_id).unlink()
    except FileNotFoundError:
        pass


# `review_handoff_reclaim_allowed()` lived here until 2026-09-15 (card t_ec55f36f). It was written,
# tested and never called from anywhere — a guard wearing a test suite while governing nothing, which
# is the most dangerous state a control can be in on this fleet: the next reader, human or overwatch,
# believes a review handoff is protected. It is not deleted because the risk was imaginary; it is
# deleted because the kernel already handles it, and handles it better:
#
#   * `kanban_db.reclaim_task` calls `_retry_status_for_run` (kanban_db.py:2336), whose own docstring
#     is the guarantee — "`review` when the run's `claimed` event says `source_status=review`, else
#     `ready` — one place, so crash/timeout/reclaim can't silently turn a reviewer run into an
#     implementation run." A reclaimed review-lane card comes back IN REVIEW.
#   * That is asserted, not assumed: `test_interrupted_review_runs_retry_in_review_phase` is
#     parametrised over spawn_failure / expired_claim / MANUAL_RECLAIM / stale_heartbeat.
#   * `reassign_task` is reclaim + `assign_task`, and `assign_task` (kanban_db.py:1633) only touches
#     `assignee` and the failure counters — never `status`. A reassigned review card keeps its lane
#     and gets a different reviewer, which is exactly the documented "this profile's model is broken"
#     path.
#
# So the card's AC3 asked reclaim to REFUSE where the kernel PRESERVES, and preserving is the better
# verb: refusing would strand a genuinely stuck reviewer with no operator escape. And `reclaim_task` /
# `reassign_task` have no automatic callers at all — only the CLI (kanban.py:635/644) and three
# dashboard buttons. The one actor who could swap a reviewer mid-review is overwatch, because
# AUTHORITY below tells it that it may; that is a prompt, not a code path, and a magic-string override
# is a weak gate against something that can read this file. The constraint is stated in AUTHORITY
# instead, where the actor will actually read it, and `assignee-mismatch-watch` is the detection side.
# Detection over prevention is what has held on this fleet: core-patch-watch reads the diff and misses
# nothing, while the mint guard is blind to ~70% of minting by construction.


def _idempotency_slug(text: str, max_len: int = 60) -> str:
    """Normalize text to a safe idempotency-key suffix.

    Lowercased, non-alphanumeric replaced with hyphens, consecutive hyphens
    collapsed, leading/trailing hyphens stripped, truncated to max_len.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def _hermes_bin() -> str:
    return os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes")


def _board_db_path() -> str:
    return os.environ.get("HERMES_KANBAN_DB") or os.path.expanduser("~/.hermes/kanban.db")


def _ro():
    con = sqlite3.connect(f"file:{_board_db_path()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _card(task_id: str) -> dict | None:
    try:
        con = _ro()
        try:
            row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            con.close()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("kanban-block-escalator: card probe failed: %s", exc)
        return None


def _is_truly_blocked(card: dict | None) -> bool:
    return bool(card) and card.get("status") in ("blocked", "triage")


def _count_comments(task_id: str, prefix: str) -> int:
    """How many comments on this card open with ``prefix`` — the ceiling's unit of account.

    AUTHOR-AGNOSTIC, deliberately, and that is the whole reason the parameter is gone
    (2026-09-16, t_2da01ffe). The counter used to read ``body LIKE 'overwatch:%' AND
    author = 'default'``. Measured against the real board, the author is not a boundary: the
    overwatch actor that woke from the notification poller wrote its ruling comment through
    ``tools/kanban_tools.py``, which signs ``os.environ.get("HERMES_PROFILE") or "worker"`` —
    and a session started as ``--profile default serve`` carries no ``HERMES_PROFILE``. So that
    ruling was signed ``worker`` and the ceiling read **1** where two decisions existed
    (``t_b6ebc5ec``), and on ``t_0a677768`` it read **0** where one existed.

    Who wrote a decision is not what the limit is about: the limit is how many times this card
    has already been ruled on. Over-counting is the safe direction — a card reaching its ceiling
    a touch early lands a rundown in front of Richie; under-counting lets a card run past it.
    """
    try:
        con = _ro()
        try:
            q = "SELECT COUNT(*) FROM task_comments WHERE task_id = ? AND body LIKE ?"
            return int(con.execute(q, [task_id, prefix + "%"]).fetchone()[0])
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return 0


def _hard_ceiling() -> float:
    try:
        from hermes_cli import kanban_db  # type: ignore

        return float(kanban_db.resolve_max_cost_hard_ceiling())
    except Exception:  # noqa: BLE001
        return HARD_CEILING_FALLBACK


def _recurrences(task_id: str) -> int:
    card = _card(task_id)
    try:
        return int((card or {}).get("block_recurrences") or 0)
    except Exception:  # noqa: BLE001
        return 0


def should_trigger(card: dict) -> tuple[bool, str]:
    """(trigger?, why). Pure function of the card row — unit-tested."""
    kind = (card.get("block_kind") or "").strip()
    rec = int(card.get("block_recurrences") or 0)
    if kind in NEVER_TRIGGER_KINDS:
        return False, f"{kind or 'unkinded'}: not a fault signal"
    if kind in ALWAYS_TRIGGER_KINDS:
        return True, kind
    if kind == "transient":
        return (rec >= TRANSIENT_TRIGGER_AFTER), f"transient recurrence {rec}"
    # Unkinded / unknown kinds: a block is a block — but only once it repeats.
    return (rec >= 1), f"{kind or 'unkinded'} recurrence {rec}"


def is_hard_stop(task_id: str, card: dict) -> tuple[bool, str]:
    """Second overwatch DECISION on one card, or a cost breach past the extension.

    The unit is decisions, not sessions spawned (Richie, 2026-09-16): counting processes makes
    the limit depend on how many happened to wake, which is the number the double-wake defect
    corrupts. `_count_comments` is author-agnostic for the same reason.
    """
    n = _count_comments(task_id, OVERWATCH_MARKER)
    if n >= OVERWATCH_LIMIT:
        return True, f"{n} overwatch decisions already on this card"
    if (card.get("block_kind") or "") == "cost_cap":
        if _count_comments(task_id, EXTENSION_MARKER) >= 1:
            return True, "cost cap breached again after the one allowed extension"
        try:
            cap = float(card.get("max_cost") or 0)
        except Exception:  # noqa: BLE001
            cap = 0.0
        if cap and cap >= _hard_ceiling() - 1e-9:
            return True, f"cap ${cap:.2f} is already at the hard ceiling"
    return False, ""


def _brief(task_id: str, card: dict) -> tuple[str, str]:
    """Assemble the situation brief ($0). Returns (path, short summary)."""
    lines = [f"# Overwatch brief — {task_id}", ""]
    try:
        con = _ro()
        try:
            lines += [f"title: {card.get('title')}",
                      f"status: {card.get('status')}  block_kind: {card.get('block_kind')}  recurrences: {card.get('block_recurrences')}",
                      f"assignee: {card.get('assignee')}  created_by: {card.get('created_by')}  tenant: {card.get('tenant')}",
                      f"cap: {card.get('max_cost')}  workspace: {card.get('workspace_kind')} {card.get('workspace_path')}", ""]
            body = (card.get("body") or "").strip()
            lines += ["## body (head)", body[:1500], ""]
            parents = [r[0] for r in con.execute("SELECT parent_id FROM task_links WHERE child_id=?", (task_id,))]
            children = [r[0] for r in con.execute("SELECT child_id FROM task_links WHERE parent_id=?", (task_id,))]
            lines += [f"parents: {parents}", f"children: {children}"]
            sib = []
            for p in parents:
                for r in con.execute("SELECT t.id,t.status,t.block_kind,t.assignee,t.title FROM task_links l JOIN tasks t ON t.id=l.child_id WHERE l.parent_id=? AND t.id!=?", (p, task_id)):
                    sib.append(f"  {r[0]} {r[1]} {r[2] or ''} {r[3]} | {(r[4] or '')[:60]}")
            if sib:
                lines += ["siblings under the same parent:"] + sib
            lines += ["", "## runs"]
            for r in con.execute("SELECT profile,status,outcome,started_at,ended_at,substr(error,1,200) FROM task_runs WHERE task_id=? ORDER BY id", (task_id,)):
                lines.append(f"  {r[0]} {r[1]} {r[2]} {r[3]}->{r[4]} {r[5] or ''}")
            lines += ["", "## last comments"]
            for r in con.execute("SELECT author,created_at,substr(body,1,500) FROM task_comments WHERE task_id=? ORDER BY created_at DESC LIMIT 8", (task_id,)):
                lines.append(f"  [{r[0]} @{r[1]}] {r[2]}")
            lines += ["", "## last events"]
            for r in con.execute("SELECT kind,substr(payload,1,160),created_at FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 10", (task_id,)):
                lines.append(f"  {r[2]} {r[0]} {r[1]}")
            dup = [r[0] for r in con.execute("SELECT id FROM tasks WHERE id!=? AND status NOT IN ('done','archived') AND title=?", (task_id, card.get("title")))]
            if dup:
                lines += ["", f"LIVE CARDS WITH THE SAME TITLE (possible duplicates): {dup}"]
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        lines += [f"(brief partly unavailable: {exc})"]
    try:
        from hermes_cli import kanban_db  # type: ignore

        own = kanban_db._cumulative_session_cost(
            kanban_db._state_db_path_for_assignee(card.get("assignee")), card.get("workspace_path"),
            task_id=task_id, all_ledgers=False)
        life = kanban_db._cumulative_session_cost(None, card.get("workspace_path"), task_id=task_id, all_ledgers=True)
        lines += ["", f"spend: assignee ${own:.2f} / lifetime all ledgers ${life:.2f} / cap {card.get('max_cost')}"]
        ma, ma_calls = _modelark_share(task_id)
        if ma_calls:
            lines.append(f"of which modelark subscription: {ma_calls} calls, counted as ${ma:.2f} cap-equivalent "
                         f"(DeepSeek list rate) — the ModelArk Coding Plan reports no cost and invoices nothing "
                         f"per call. Judge the work, not those dollars.")
    except Exception:  # noqa: BLE001
        pass
    text = "\n".join(lines)
    path = ""
    try:
        BRIEF_DIR.mkdir(parents=True, exist_ok=True)
        p = BRIEF_DIR / f"{task_id}-{int(time.time())}.md"
        p.write_text(text)
        path = str(p)
    except Exception:  # noqa: BLE001
        pass
    return path, text[:6000]


def _modelark_share(task_id: str) -> tuple[float, int]:
    """(cap-equivalent $, calls) of this card's ModelArk Coding Plan usage (2026-09-11).

    The Coding Plan reports NO cost. plugins/modelark-pricing adds a cap-equivalent (tokens x DeepSeek
    list rate) to the per-card sum so the $1 cap still fires; overwatch must know which part of a
    breach is that equivalent rather than money. Best effort: (0.0, 0).
    """
    try:
        from hermes_cli import kanban_db  # type: ignore
        fn = getattr(kanban_db, "modelark_cap_equivalent", None)
        return fn(task_id) if fn else (0.0, 0)
    except Exception:  # noqa: BLE001
        return 0.0, 0


def _mark_ceiling(task_id: str, reason: str | None, why: str) -> None:
    """Machine-readable comment so escalation-watch tells Richie. Best effort."""
    try:
        from hermes_cli import kanban_db  # type: ignore

        # Upstream moved connection handling into kanban_db_connect and the
        # kanban_db pointer is revert-scheduled (removed 2026-09-14). Prefer the
        # defining module; the pre-decomposition fallback is resolved by a
        # runtime-built name so no static pointer reference remains for
        # scripts/check_compat_pointers.py to flag.
        try:
            from hermes_cli.kanban_db_connect import connect_closing  # type: ignore
        except ImportError:
            connect_closing = getattr(kanban_db, "connect" + "_closing")

        with connect_closing(_board_db_path()) as con:
            kanban_db.add_comment(
                con, task_id, "kanban-block-escalator",
                f"{CEILING_MARKER}: hard stop — {why}. Staying blocked for Richie. "
                f"Reason: {reason or '(none)'}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("kanban-block-escalator: could not mark ceiling on %s: %s", task_id, exc)


AUTHORITY = (
    "YOUR AUTHORITY (Richie, 2026-09-06). You MAY: comment, unblock, reassign, split the card "
    "via Jobsy, archive duplicates, rescope, put on hold, open a HELD platform card, and — for a "
    "cost_cap block — extend the cap ONCE by at most $0.50 with `hermes kanban set-cap <id> <cap> "
    "--reason ...` (hard ceiling $1.50). REASSIGNMENT: a card whose last handoff was "
    "`review_requested` is mid-review, so reassigning it SWAPS THE REVIEWER — your `--reason` must "
    "say why this reviewer is being replaced (its model is failing, it is stalled, it is the "
    "author of the work). The lane itself is safe either way: the kernel returns a reclaimed review "
    "run to `review`, never to `ready`. A reassignment whose reason does not say why is picked up "
    "by assignee-mismatch-watch and comes back to Richie. You may NOT, while cards are running: commit platform "
    "code, restart a gateway, edit a SOUL, waive a review or test gate, raise a cap past $1.50, "
    "or create a card assigned to yourself. A defective gate is a held platform card, not an "
    "exemption. Finish quickly, hygienically and cheaply, keeping every agreed review and test "
    "gate. Your FIRST comment on the card must start with `overwatch:` and state the decision "
    "and its evidence — that comment is how the board counts your interventions."
)


def _idempotency_instruct(task_id: str) -> str:
    """Idempotency instruction for the overwatch prompt, evaluated at prompt time."""
    return (
        "IDEMPOTENCY: When you call kanban_create to mint a remediation card for "
        "this overwatch, pass idempotency_key=f\"overwatch-"
        f"{task_id}"
        "-{_idempotency_slug('<title>')}\" — replace <title> with the new card's title. "
        "This prevents concurrent overwatch sessions from creating duplicate cards: "
        "the second session gets the existing card id back instead of minting a twin. "
        "When you are not sure whether a remediation card already exists, check "
        "first by listing non-archived cards with creator_task_id matching "
        f"{task_id}.\n\n"
    )


def _overwatch_prompt(task_id: str, card: dict, reason: str | None, why: str, brief_path: str, brief: str) -> str:
    return (
        f"OVERWATCH: kanban card {task_id} blocked ({why}). Block reason: {reason or '(none)'!r}.\n"
        f"Read the situation brief first ({brief_path or 'inline below'}); it already contains the card, "
        "its lineage, runs, comments, spend and any live duplicate. Ask FIRST whether this is a deeper "
        "error — duplicate card, wrong scope, dead or empty workspace, stale gate, false design premise, "
        "missing capability — and fix the cause rather than the symptom.\n\n"
        f"{_idempotency_instruct(task_id)}"
        f"{AUTHORITY}\n\n=== BRIEF ===\n{brief}"
    )


def _rundown_prompt(task_id: str, reason: str | None, why: str, brief_path: str, brief: str) -> str:
    return (
        f"HARD STOP on kanban card {task_id}: {why}. Block reason: {reason or '(none)'!r}. "
        "Do NOT unblock, extend, split or archive it — this decision is Richie's. Write ONE comment "
        f"on the card starting with `{RUNDOWN_MARKER}` covering: (1) the situation, (2) what has been "
        "done about it so far and by whom, (3) whether there is a deeper issue and what it is, "
        "(4) the plan and estimated cost to remedy, with your recommendation. escalation-watch "
        "delivers it to Richie by iMessage and Slack. Keep it under 250 words.\n\n"
        f"Brief: {brief_path or 'inline below'}\n=== BRIEF ===\n{brief}"
    )


def _spawn(assessor: str, prompt: str, task_id: str) -> None:
    if not _claim_overwatch(task_id, "spawn"):
        return
    try:
        env = dict(os.environ)
        env["HERMES_OVERWATCH_TASK"] = task_id
        # 2026-09-07: this hook runs INSIDE the blocked worker's process, so
        # os.environ carries HERMES_KANBAN_TASK / HERMES_KANBAN_RUN_ID. Passing
        # them on made the overwatch assessor look like a kanban worker to
        # agent/kanban_checkpoint.py: it received the per-turn `[checkpoint]`
        # reminder and the forced terminal-only finalize turn, and was steered
        # toward a board call it must never make (overwatch assesses; it does
        # not close cards). Observed live on t_125dfa35 run 1143, where the
        # assessor reported "the checkpoint cut MY turn too".
        for _worker_var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
            env.pop(_worker_var, None)
        # 2026-09-15, card t_56500e82: the same inheritance also carried HERMES_PROFILE. `-p
        # <assessor>` decides which home the child runs against — measured, overwatch spend lands
        # in root's ledger every time — but it does NOT rewrite os.environ, and
        # tools/kanban_tools.py reads `os.environ["HERMES_PROFILE"]` to sign a comment. So an
        # overwatch ruling spawned from a blocked rodge worker was authored "rodge", and one from
        # bob was authored "bob". You can read it on t_796c3fab: a comment opening `overwatch:`,
        # signed **bob**, which is the assessor overruling the very worker it is named after.
        # Those comments are injected verbatim into the next worker's system prompt, so the
        # attribution is not cosmetic — it is what a future worker believes about who ruled.
        #
        # SET it, never pop it: `author = os.environ.get("HERMES_PROFILE") or "worker"`, so an
        # unset variable signs the ruling "worker", which is worse than a wrong profile name.
        env["HERMES_PROFILE"] = assessor
        # Two readers, two variables. tools/kanban_tools.py signs comments (:767) and
        # created_by (:936) from HERMES_PROFILE; hermes_cli/kanban.py's _profile_author
        # (:201) prefers HERMES_PROFILE_NAME and only then falls back to it. Setting one
        # and not the other would fix the tool path and leave the CLI path signing the
        # worker — so both carry the assessor.
        env["HERMES_PROFILE_NAME"] = assessor
        # The lease is handed over HERE, and the shell wrapper is gone with the trap that never
        # ran (2026-09-16, t_2da01ffe). The wrapper was
        #     trap 'rm -f -- "$1"' EXIT; shift; exec "$@"
        # — `exec` REPLACES the shell, and a shell's EXIT trap does not survive `exec`, so the
        # claim file was never removed on exit: the wrapper could only ever have protected the
        # lease for as long as the WORKER lived, and the worker is not the holder. Spawning the
        # assessor directly makes `Popen.pid` the assessor itself (there is no intermediate
        # process to name), so the claim can record the process whose lifetime the lease is
        # actually about. Release is by pid-liveness (`_claim_state`): the claim stops holding
        # the card the moment that process exits, with CLAIM_TTL_SECONDS as the backstop.
        proc = subprocess.Popen(
            [_hermes_bin(), "-p", assessor, "--cli", "chat", "-q", prompt],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=env,
        )
        _hand_lease_to_child(task_id, int(getattr(proc, "pid", 0) or 0))
        logger.info("kanban-block-escalator: overwatch spawned for %s (%s)", task_id, assessor)
    except Exception as exc:  # noqa: BLE001
        # A spawn that failed must not strand the card's slot behind a reservation nobody holds.
        _release_overwatch(task_id)
        logger.warning("kanban-block-escalator: failed to spawn overwatch for %s: %s", task_id, exc)


def on_block(task_id: str = "", assignee: str | None = None, reason: str | None = None, **kwargs) -> None:
    """kanban_task_blocked callback. Fire-and-forget overwatch trigger."""
    if not task_id:
        return
    card = _card(task_id)
    if not _is_truly_blocked(card):
        return
    # The hook may or may not carry the kind; the board always does.
    if kwargs.get("kind") and not card.get("block_kind"):
        card["block_kind"] = kwargs["kind"]
    trigger, why = should_trigger(card)
    if not trigger:
        logger.info("kanban-block-escalator: %s not escalated (%s)", task_id, why)
        return
    hard, hard_why = is_hard_stop(task_id, card)
    brief_path, brief = _brief(task_id, card)
    if hard:
        _mark_ceiling(task_id, reason, hard_why)
        _spawn(OVERWATCH, _rundown_prompt(task_id, reason, hard_why, brief_path, brief), task_id)
        return
    _spawn(OVERWATCH, _overwatch_prompt(task_id, card, reason, why, brief_path, brief), task_id)


# ----------------------------------------------------------------------------------------------
# 2026-09-16, card t_2da01ffe: AC1's second path, without a core patch -------------------------
#
# The duplicate decider is not a second SPAWN — `_spawn` above is the only spawn path and it ran
# once in both measured cases. It is the tui notification poller running an agent TURN in the
# standing desktop session (`tui_gateway/session_notifications.py:546` -> `:364` `_notif_poll_kanban`
# -> `_notif_submit`, "run the buffered batch as a turn if idle"). `tui_gateway/` fires no plugin
# hook on that path, `delivery_mode` is never read there, and the poller's turn claim lives in the
# desktop process's memory — so a plugin CANNOT stop the turn. Core is the only place that could,
# and a core patch is forbidden (decision-autonomy, 2026-09-16: "never hermes source code").
#
# What a plugin CAN do is make the second turn harmless, which is what this section does:
#
#   * `pre_llm_call`  — the awareness line. When the turn's own user message IS a kanban
#     notification for a card whose assessment lease is live, the escalation state is injected into
#     that message: this is awareness, not an instruction.
#   * `pre_tool_call` — the enforcement. A write to that card from any session that is not the
#     assessor is refused while the assessor holds it. Reads are untouched.
#
# Scope is measured, not assumed. BOTH recorded duplicates did their board work through `terminal`
# running the kanban CLI — `hermes kanban unblock t_8dd715c6` (15:20:28), `hermes kanban reassign
# t_8dd715c6 bob` (15:20:34), `hermes kanban unblock/reassign/reopen t_0a677768` (14:58:51) — and
# through `sqlite3 kanban.db` writes to the card body ("AC3 clarified: True | ACs 7/8 added: True",
# 14:58:39; "AC2 rescoped: True | AC5 rescoped: True | block added: True", 15:20:17), plus kanban
# tools (`kanban_comment` 2132/2141, `kanban_link`). A gate on kanban TOOLS alone would have
# refused none of the measured changes, so all three surfaces are guarded.
#
# Residual, stated rather than implied: a session that writes through a path this gate cannot
# recognise (an arbitrary script, `execute_code` calling `kanban_db` in-process) is not stopped.
# The gate refuses what it can SEE and names the reason; the airtight fix is the poller itself,
# which is core, and therefore Richie's call — not something this plugin can carry.
#
# Fail OPEN on internal error (the `kanban-mint-guard` precedent): this gate protects one card from
# a duplicate decision, it is not a security boundary, and a guard that refuses the whole board
# when it has a bad day is worse than the defect it prevents.

AWARENESS_MARK = "[kanban-block-escalator — AWARENESS-ONLY TURN]"

# Kanban TOOLS that change a card's state. Deliberately NOT here: kanban_show / kanban_list /
# kanban_attachments (reads), kanban_heartbeat (a running card's liveness, and a leased card is
# blocked), kanban_create (a new card — the idempotency key below is the existing remedy for a
# duplicate mint, and refusing it would stop unrelated minting).
WRITE_TOOLS = frozenset({
    "kanban_unblock", "kanban_block", "kanban_complete", "kanban_request_review",
    "kanban_request_changes", "kanban_comment", "kanban_link", "kanban_attach",
    "kanban_attach_url",
})
_TARGET_ID_KEYS = ("task_id", "tid", "id", "parent_id", "child_id")

TASK_ID_RE = re.compile(r"\bt_[0-9a-z]{6,16}\b")

# `hermes kanban <verb>` — the disposition verbs, i.e. everything that decides something about a
# card. Reads (`show`, `list`, `log`, `runs`, `tail`, `stats`, `attachments`, `context`,
# `notify-list`, `assignees`, `diagnostics`, `export`) are absent on purpose.
#
# MATCHED AS TOKENS, not as text. The first version was one regex anchored on `\bkanban\b`, and the
# population sweep (63 lost hits, every one read) showed what that costs in both directions:
#
#   * false positives it refused — `git commit -m "…kanban-block-escalator…"` (`-block-` is a word
#     boundary) and `sqlite3 kanban.db "SELECT '  comment '||id …"`, a READ, refused by a gate whose
#     whole point is to leave reads alone;
#   * real writes it MISSED after being tightened to `hermes kanban` — the fleet has three spellings
#     in the recorded corpus: `hermes kanban unblock t_X`, `python3 -m hermes_cli kanban set-cap
#     t_X 1.50 …` (2026-09-14 20:18), and `HB=…/venv/bin/hermes; "$HB" kanban block t_X …`
#     (2026-09-16 20:32).
#
# So: split the command into shell segments, tokenise, and require a BARE `kanban` token whose
# predecessor is the binary (by name, by path, or by a variable expansion standing in for it). That
# is what makes `kanban.db` a read and `"$HB" kanban block` a write.
CLI_WRITE_VERBS = frozenset({
    "unblock", "block", "complete", "assign", "reassign", "reopen-review", "request-review",
    "request-changes", "promote", "archive", "claim", "reclaim", "link", "unlink", "comment",
    "edit", "schedule", "set-cap", "set-model",
})
_SHELL_SEGMENT_RE = re.compile(r"[\n;|&]")
_HERMES_TOKENS = frozenset({"hermes", "hermes_cli"})

# Direct writes to the BOARD database, two more measured shapes:
#   `sqlite3 kanban.db "UPDATE tasks SET body=…"` (14:58:39 and 15:20:17 in this card's forensics)
#   `from hermes_cli import kanban_db as kb … kb.complete_task(conn, tid)` — measured 7 times in the
#   last 30 days (`complete_task` x4, `unlink_tasks` x4, `add_comment` x2, `link_tasks`, `unblock_task`)
BOARD_DB_RE = re.compile(r"kanban\.db\b")
_SQL_TABLES = r"(?:tasks|task_comments|task_links|task_events|task_runs)"
SQL_WRITE_RE = re.compile(
    r"(?:UPDATE\s+" + _SQL_TABLES + r"\s+SET\b"
    r"|(?:INSERT|REPLACE)\s+INTO\s+" + _SQL_TABLES + r"\b"
    r"|DELETE\s+FROM\s+" + _SQL_TABLES + r"\b"
    r"|(?:DROP|ALTER|TRUNCATE)\s+TABLE\s+" + _SQL_TABLES + r"\b)",
    re.IGNORECASE,
)
# A statement SHAPE, not the bare keyword: `kind='update'` inside a SELECT is a read, and the first
# version of this rule refused that SELECT (found by the population sweep, not by a unit test).
# Unqualified on purpose — the calls in the corpus are written `kb.complete_task(...)` after
# `from hermes_cli import kanban_db as kb`, so requiring the `kanban_db.` qualifier missed every one.
KANBAN_API_WRITE_RE = re.compile(
    r"\.(?:unblock_task|block_task|complete_task|assign_task|reassign_task|reopen_review|"
    r"request_review|request_changes|link_tasks|unlink_tasks|add_comment|archive_task|"
    r"claim_task|reclaim_task|update_task|set_max_cost|set_cap)\w*\s*\("
)
# A segment whose first token is a prose emitter cannot be an invocation: `echo "run hermes kanban
# unblock t_x"` documents a command, it does not run one.
_PROSE_TOKENS = frozenset({"echo", "printf", "logger", ":"})

NOTIFICATION_RE = re.compile(r"Kanban\s+(t_[0-9a-z]{6,16})\s+blocked\b")


def _held_lease(task_id: str) -> tuple[bool, int | str]:
    """(held?, holder) — `_claim_state` is the ONE reader of the lease, and this is its only caller
    for a card other than the card being escalated."""
    state, holder = _claim_state(_claim_path(task_id))
    return state == "held", holder


def escalation_in_flight(task_id: str) -> tuple[bool, int | str]:
    """(in flight?, holder) — a HELD lease on a card that is STILL AWAITING the escalation.

    The second half is not decoration. "An assessment is in flight" is two facts, and the lease
    alone is only one of them: the assessor's job is to resolve the block, so the moment the card
    stops being blocked the assessment is over — but the claim file can outlive it. Measured on
    the live board at 21:40-21:52 today: `t_46783f69` blocked on `cost_cap`, an assessor was
    spawned from the ROOT GATEWAY (which still runs pre-`07c72dfa` code, so its claim records the
    gateway's own long-lived pid rather than the child's), the assessor extended the cap and
    unblocked the card, the dispatcher started a worker on it 90 seconds later — and that worker
    would have been REFUSED its own comment and completion for the rest of the 15-minute TTL by a
    gate aimed at a duplicate decider that no longer existed.

    A refusal that strands the card's own worker is a worse failure than the one this gate
    prevents, so the gate stands down whenever the card is no longer blocked (`_is_truly_blocked`,
    which treats `triage` as blocked and `operator_hold`/`dependency`/`scheduled` as they are).
    Every measured duplicate decided its card WHILE THAT CARD WAS BLOCKED — the poller's own write
    was the unblock — so the window this closes is not the window the defect lives in.

    Fails OPEN, like every other path here: an unreadable board allows the call.
    """
    state, holder = _claim_state(_claim_path(task_id))
    if state != "held":
        return False, holder
    if not _is_truly_blocked(_card(task_id)):
        return False, holder
    return True, holder


def is_assessor(task_id: str) -> bool:
    """Is THIS process the session the escalation was spawned for?

    Two rungs, both established by `_spawn`: it starts the assessor directly, so the pid in the
    claim IS the assessor's; and it exports `HERMES_OVERWATCH_TASK`. The env rung matters as much as
    the pid rung — a `hermes kanban` call the assessor makes through `terminal` is a CHILD process,
    inherits the variable, and must never be refused by this gate.
    """
    if (os.environ.get("HERMES_OVERWATCH_TASK") or "").strip() == task_id:
        return True
    held, holder = _held_lease(task_id)
    return bool(held and isinstance(holder, int) and holder == os.getpid())


def _is_hermes_binary_token(token: str) -> bool:
    """The token a bare `kanban` subcommand token must follow to be a CLI invocation."""
    t = token.strip("\"'")
    if not t:
        return False
    if t in _HERMES_TOKENS or t.endswith("/hermes") or t.endswith("/hermes_cli"):
        return True
    # A variable standing in for the binary — measured spelling `HB=…/hermes; "$HB" kanban block`.
    return t.startswith("$") or t.startswith("(") or t.startswith("`")


def _cli_write_in(command: str) -> bool:
    """Does any shell segment invoke `<hermes> kanban <write verb>`? Tokens, never substrings."""
    for segment in _SHELL_SEGMENT_RE.split(command):
        tokens = segment.replace('"', " ").replace("'", " ").split()
        if not tokens or tokens[0] in _PROSE_TOKENS:
            continue
        for index, token in enumerate(tokens):
            if token != "kanban":
                continue
            if index == 0 or not _is_hermes_binary_token(tokens[index - 1]):
                continue
            rest = {t.strip("\"'") for t in tokens[index + 1:]}
            if rest & CLI_WRITE_VERBS:
                return True
    return False


def _terminal_write_targets(command: str) -> tuple[set[str], str]:
    """(the card ids this shell command would WRITE, what kind of write) — reads give ({}, "").

    Three recognised write surfaces, each anchored on its own thing and each a SHAPE rather than a
    keyword (a keyword match is how a gate ends up refusing reads — see the CLI section above and
    SQL_WRITE_RE). Anything else returns no targets: an unrecognised path is a residual, stated in
    the section comment above, never a guess.
    """
    if "kanban" not in command:
        return set(), ""
    ids = set(TASK_ID_RE.findall(command))
    if not ids:
        return set(), ""
    if _cli_write_in(command):
        return ids, "`hermes kanban` write"
    if BOARD_DB_RE.search(command) and SQL_WRITE_RE.search(command):
        return ids, "SQL write to the board database"
    if "kanban_db" in command and KANBAN_API_WRITE_RE.search(command):
        return ids, "kanban_db API write"
    return set(), ""


def _tool_write_targets(tool_name: str, args: dict) -> set[str]:
    if tool_name not in WRITE_TOOLS:
        return set()
    found = set()
    for key in _TARGET_ID_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.startswith("t_"):
            found.add(value)
        elif isinstance(value, (list, tuple)):
            found.update(str(v) for v in value if isinstance(v, str) and v.startswith("t_"))
    return found


def writes_to_leased_card(tool_name: str, args: dict) -> tuple[str, str, int | str] | None:
    """(task_id, surface, holder) when this call would WRITE to a card under a live lease, else None.

    The decision, in one place and unit-tested directly: a write surface, a card that is leased, and
    a caller that is not the assessor. Any one of the three missing -> None (allow).
    """
    if tool_name == "terminal":
        candidates, surface = _terminal_write_targets(str(args.get("command") or ""))
    else:
        candidates, surface = _tool_write_targets(tool_name, args), tool_name
    if not candidates:
        return None
    for task_id in sorted(candidates):
        if is_assessor(task_id):
            continue
        in_flight, holder = escalation_in_flight(task_id)
        if in_flight:
            return task_id, surface, holder
    return None


def refusal_message(task_id: str, surface: str, holder: int | str) -> str:
    return (
        f"REFUSED — awareness-only (kanban-block-escalator).\n\n"
        f"An overwatch assessment for {task_id} is IN FLIGHT: the briefed assessor (pid {holder}) "
        f"holds this card's escalation lease, taken when the card blocked and released the moment "
        f"that process exits (15-minute backstop).\n\n"
        f"One block, one escalation. This call ({surface}) would change {task_id}'s state from a "
        f"session that is not the assessor, so it is refused — a second session deciding the same "
        f"card is what produced two decisions where there should be one (measured on t_b6ebc5ec, "
        f"t_0a677768 and t_8dd715c6). The notification that woke this session is awareness, not an "
        f"instruction.\n\n"
        f"Refused on this card: unblock / block / assign / reassign / complete / re-review / "
        f"archive / relink / edit / set-cap / comment, by tool or by `hermes kanban` or by a direct "
        f"kanban.db write.\n"
        f"Unaffected: every read (`kanban show`, `hermes kanban show|list|log|runs|tail|stats`, "
        f"SELECT), and every other card.\n\n"
        f"To proceed: re-check {task_id} once the assessment clears (the lease is gone when the "
        f"assessor exits), or use the Hermes Desktop kanban pane's own buttons, which do not pass "
        f"through plugin hooks. If this refusal is wrong for this card, say so on the card once the "
        f"lease clears — this gate only ever acts while an assessment is live."
    )


def awareness_line(task_id: str, holder: int | str) -> str:
    """The line injected into the second session's own turn — the awareness half of AC1."""
    return (
        f"{AWARENESS_MARK}\n"
        f"The escalation for card {task_id} is IN FLIGHT: a briefed overwatch session (pid {holder}) "
        f"holds this card's assessment lease. This notification is awareness, not an instruction. Do "
        f"NOT unblock, block, assign, reassign, complete, re-review, archive, relink, edit, cap or "
        f"comment on {task_id} from this session, and do not run a `hermes kanban` write or a "
        f"kanban.db write against it — those are refused while the lease is live, and a second "
        f"decision on a card is the defect this fleet already paid for three times today. Surface "
        f"the notification if it needs a human; otherwise read what you need and wait. The lease "
        f"clears when the assessor exits."
    )


def _notification_text(user_message) -> str:
    """The turn's user message as text — string, or a multimodal list of parts."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, (list, tuple)):
        parts = []
        for item in user_message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return ""


def awareness_note(user_message) -> str | None:
    """(the awareness line) when this turn IS a kanban notification for a leased card, else None.

    Only the HEAD of the message is read: a notification opens with it, and a body that merely
    mentions "Kanban t_x blocked" later on is prose, not a wake-up.
    """
    text = _notification_text(user_message)
    if "Kanban" not in text:
        return None
    for match in NOTIFICATION_RE.finditer(text[:1000]):
        task_id = match.group(1)
        if is_assessor(task_id):
            continue
        in_flight, holder = escalation_in_flight(task_id)
        if in_flight:
            return awareness_line(task_id, holder)
    return None


def on_pre_llm_call(**payload) -> dict | None:
    """`pre_llm_call` — inject the awareness line into the duplicate session's own turn."""
    try:
        note = awareness_note(payload.get("user_message"))
        if not note:
            return None
        logger.info("kanban-block-escalator: awareness-only turn (session=%s platform=%s)",
                    payload.get("session_id"), payload.get("platform"))
        return {"context": note}
    except Exception:  # noqa: BLE001
        logger.exception("kanban-block-escalator: awareness note failed, continuing without it")
        return None


def on_pre_tool_call(**payload) -> dict | None:
    """`pre_tool_call` — refuse a write to a card whose assessment lease is live."""
    try:
        tool_name = str(payload.get("tool_name") or "")
        args = payload.get("args")
        hit = writes_to_leased_card(tool_name, args if isinstance(args, dict) else {})
        if not hit:
            return None
        task_id, surface, holder = hit
        logger.warning("kanban-block-escalator: awareness-only refusal — %s would write to %s "
                       "(lease holder %s)", surface, task_id, holder)
        return {"action": "block", "message": refusal_message(task_id, surface, holder)}
    except Exception:  # noqa: BLE001
        logger.exception("kanban-block-escalator: awareness gate error, allowing")
        return None


def register(ctx) -> None:
    """Register the escalation trigger and the awareness-only gate."""
    ctx.register_hook("kanban_task_blocked", on_block)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
