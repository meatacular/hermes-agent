#!/usr/bin/env python3
"""Kanban board integrity watchdog.

`no_agent` cron script. Zero LLM cost. Watchdog convention: silent unless
something is wrong. Empty stdout = silent tick.

Four checks, each of which has already bitten this fleet once:

  1. STRANDED — a card sits `ready` with no worker for too long. On 2026-08-24 a
     governance card sat 9.1h because its assignee was "agent-smith", which is
     not a profile (root's profile id is `default`). The dispatcher skips
     unknown assignees SILENTLY.

  2. UNKNOWN ASSIGNEE — same root cause, caught before it strands.

  3. PRIORITY INVERSION — `decompose_triage_task()` inserts children without a
     priority column, so they take the SQL default 0. Dispatch is strictly
     `ORDER BY priority DESC, created_at ASC`, so on a board whose live band is
     above 0 an auto-decomposed subtree queues behind every hand-made card and
     never runs. Confirmed live on this board 2026-08-25: five children at 0
     under a root at 14. There is no `hermes kanban` verb to repair a priority,
     so this script does it directly.

  4. REVIEW CONTRACT DISAGREEMENT — Rodge must emit BOTH the fixed template and
     kanban metadata, and they must agree. A review whose prose lists blockers
     while `review_outcome` says approved (or vice versa) is a defect.

  5. UNASSESSED BLOCK — a card sits `blocked` with no assessor activity for too
     long. A blocked card is a handoff to assess (and usually unblock), not a
     terminal rest state; this flags the dead-letter case where it blocked once
     and no agent or human ever looked at it again. The escalation rule this
     enforces: blocked → escalate to assessor (Jobsy for scope/AC, Smith for
     runtime/env) → keep escalating until resolved; only critical blocks stop
     at Richie.

     Check 5 only catches *untouched* blocks. A subtler failure is the one that
     bit 2026-08-31 (BackupBrain mobile handbook t_5f980bd6): the orchestrator
     touched the card, diagnosed the real root cause, but handed the decision
     (`can pangea touch-reorder and body-scroll both live?`) to the builder as a
     prose hedge ("fork a Karl card if those can't coexist") instead of spawning
     a decision-shaped triage/Karl card then. Check 5 saw assessor activity and
     stayed silent while the pipeline still waited on a decision nobody owned.

  6. DECISION PARASITISM — an active card whose comments hedge at a decision it
     has not spawned a decision-shaped child for. A decision-shaped finding
     (conflict between two locked intents, a spec gap, an ambiguous AC) must
     become a typed, parent-gated triage/design card — never a paragraph in a
     handoff for the next reader to rediscover. This flags the case where the
     orchestrator routed by prose instead of by card.

Usage:
    kanban-integrity-watch.py            # report only (cron default)
    kanban-integrity-watch.py --fix      # also repair priority inversions
    kanban-integrity-watch.py --audit    # verbose, always prints
"""

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
try:
    from comms_style import clean
except Exception:  # noqa: BLE001
    def clean(s):
        return s

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if not os.path.isdir(os.path.join(HERMES_HOME, "profiles")):
    _up = os.path.dirname(os.path.dirname(HERMES_HOME))
    if os.path.isdir(os.path.join(_up, "profiles")):
        HERMES_HOME = _up

KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")

STRANDED_SECONDS = 1800          # 30 min in `ready` with no run is a problem
BLOCKED_UNASSESSED_SECONDS = 1800  # 30 min in `blocked` with no activity is a dead letter
ACTIVE = ("ready", "todo", "running", "review", "blocked")


def valid_profiles():
    """Exactly what the dispatcher can spawn: `default` plus each profile dir."""
    names = {"default"}
    try:
        for e in os.scandir(PROFILES_DIR):
            if e.is_dir() and not e.name.startswith("."):
                names.add(e.name)
    except FileNotFoundError:
        pass
    return names


def main():
    fix = "--fix" in sys.argv
    audit = "--audit" in sys.argv

    if not os.path.exists(KANBAN_DB):
        print(f"⚠️ kanban watchdog: {KANBAN_DB} not found — the board cannot be checked.")
        return 0

    try:
        con = sqlite3.connect(KANBAN_DB, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ kanban watchdog: cannot open {KANBAN_DB} — {exc}")
        return 0

    now = int(time.time())
    problems = []
    valid = valid_profiles()

    tasks = [dict(r) for r in con.execute("SELECT * FROM tasks")]
    links = [dict(r) for r in con.execute("SELECT * FROM task_links")]
    parent_of = {}
    for l in links:
        parent_of.setdefault(l["child_id"], []).append(l["parent_id"])
    by_id = {t["id"]: t for t in tasks}

    # ---- 2. unknown assignee -------------------------------------------------
    for t in tasks:
        a = t.get("assignee")
        if t["status"] in ACTIVE and a and a not in valid:
            problems.append(
                f"UNKNOWN ASSIGNEE  {t['id']}  @{a}  \"{t['title'][:60]}\"\n"
                f"    '{a}' is not a spawnable profile. Valid: {', '.join(sorted(valid))}.\n"
                f"    The dispatcher skips unknown assignees silently — this card will never run.")

    # ---- 1. stranded ---------------------------------------------------------
    run_counts = {}
    for r in con.execute("SELECT task_id, COUNT(*) c FROM task_runs GROUP BY task_id"):
        run_counts[r["task_id"]] = r["c"]
    for t in tasks:
        if t["status"] != "ready":
            continue
        if run_counts.get(t["id"]):
            continue
        age = now - (t.get("created_at") or now)
        if age > STRANDED_SECONDS:
            problems.append(
                f"STRANDED  {t['id']}  @{t.get('assignee')}  \"{t['title'][:60]}\"\n"
                f"    ready for {age/3600:.1f}h with no worker ever spawned.")

    # ---- 3. priority inversion ----------------------------------------------
    # Compare against the whole dependency TREE, not the immediate parent.
    # `task_links` is a dependency edge, not a hierarchy: after decomposition the
    # root is recorded as a *child* of every subtask (it depends on them), so an
    # immediate-parent comparison sees 0-vs-0 and misses the real fault — a
    # subtree inserted at priority 0 beneath a root ranked in the live band.
    adj = {}
    for l in links:
        adj.setdefault(l["parent_id"], set()).add(l["child_id"])
        adj.setdefault(l["child_id"], set()).add(l["parent_id"])

    seen, components = set(), []
    for tid in by_id:
        if tid in seen:
            continue
        stack, comp = [tid], []
        seen.add(tid)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in adj.get(n, ()):
                if m not in seen and m in by_id:
                    seen.add(m)
                    stack.append(m)
        if len(comp) > 1:
            components.append(comp)

    inversions = []
    for comp in components:
        top = max((by_id[c].get("priority") or 0) for c in comp)
        if top == 0:
            continue
        for c in comp:
            t = by_id[c]
            if t["status"] not in ACTIVE:
                continue
            mine = t.get("priority") or 0
            if mine < top:
                inversions.append((t["id"], mine, top, t["title"][:50]))

    if inversions:
        if fix:
            for tid, mine, top, _ in inversions:
                con.execute("UPDATE tasks SET priority=? WHERE id=?", (top, tid))
            con.commit()
        lines = "\n".join(
            f"    {tid}  priority {mine} -> {top}   \"{title}\"" for tid, mine, top, title in inversions)
        verb = "REPAIRED" if fix else "PRIORITY INVERSION"
        problems.append(
            f"{verb}  {len(inversions)} child card(s) ranked below their parent:\n{lines}\n"
            f"    Dispatch is ORDER BY priority DESC — these queue behind every higher card.\n"
            + ("    Fixed in place." if fix else "    Re-run with --fix to repair."))

    # ---- 4. review contract disagreement -------------------------------------
    import json as _json
    for r in con.execute("SELECT task_id, summary, metadata FROM task_runs WHERE outcome IS NOT NULL"):
        md = r["metadata"]
        if not md:
            continue
        try:
            m = _json.loads(md)
        except Exception:  # noqa: BLE001
            continue
        outcome = m.get("review_outcome") or m.get("verdict")
        if not outcome:
            continue
        blockers = m.get("blockers")
        if blockers is None:
            continue
        approved = str(outcome).lower() in {"approved", "approve", "pass"}
        # 2026-09-06: a reviewer wrote blockers as prose ("2 Critical: ...") and
        # int() crashed the whole watchdog (root cron read `error` from 21:21).
        # Take the leading integer; anything else is itself a contract breach.
        if not isinstance(blockers, int):
            import re as _re
            _m = _re.match(r"\s*(\d+)", str(blockers))
            if _m:
                blockers = int(_m.group(1))
            else:
                problems.append(
                    f"REVIEW CONTRACT  {r['task_id']}  metadata.blockers is not a number: {str(blockers)[:80]!r}\n"
                    f"    Rodge's template is a machine contract; the watchdog cannot count prose.")
                continue
        if approved and int(blockers) > 0:
            problems.append(
                f"REVIEW CONTRACT  {r['task_id']}  says '{outcome}' but reports {blockers} blocker(s).\n"
                f"    The prose verdict and the metadata disagree. One of them is wrong.")
        if (not approved) and int(blockers) == 0:
            problems.append(
                f"REVIEW CONTRACT  {r['task_id']}  says '{outcome}' but reports 0 blockers.\n"
                f"    Changes were requested with nothing itemised for the watchdog to see.")

    # ---- 5. unassessed block -------------------------------------------------
    # A blocked card whose latest event is itself the `blocked` transition has
    # had zero assessor activity since it blocked — the dead-letter case the
    # escalation rule is meant to eliminate. Any later unblock/comment/claim
    # means someone is on it, so we skip it. Stale + untouched = flag.
    unassessed = []
    for t in tasks:
        if t["status"] != "blocked":
            continue
        row = con.execute(
            "SELECT kind, created_at FROM task_events "
            "WHERE task_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        if row is None or row["kind"] != "blocked":
            continue  # untouched by anyone since block, OR already acted on
        age = now - (row["created_at"] or now)
        if age > BLOCKED_UNASSESSED_SECONDS:
            kind = t.get("block_kind") or "untyped"
            unassessed.append((t["id"], t.get("assignee"), kind, t["title"][:50], age))

    if unassessed:
        lines = "\n".join(
            f"    {tid}  @{a}  [{kind}]  \"{title}\"  untouched for {age/3600:.1f}h"
            for tid, a, kind, title, age in unassessed)
        problems.append(
            f"UNASSESSED BLOCK  {len(unassessed)} card(s):\n{lines}\n"
            f"    Escalate to assessor: Jobsy=scope/AC, Smith=runtime/env; only "
            f"critical blocks stop at Richie.")

    # ---- 6. decision parasitism --------------------------------------------
    # An active card whose comments hedge at a decision it has NOT spawned a
    # decision-shaped child for. A blocked/handoff finding that sits behind
    # "escalate if real" / "those can't coexist" / "that's a decision card" —
    # without a typed triage/design card parent-gated under it — is a pipeline
    # waiting on a decision nobody owns. Flag it so it becomes a card.
    # Decision-shaped children = status 'triage', OR a jobsy/karl assignee with
    # a decision verb in the title (matches the auto-decomposer regex family).
    DECISION_HEDGE = (
        "escalate if", "if those can't coexist", "if it can't coexist",
        "fork a karl", "fork a karle", "karl decision", "jobsy decision",
        "decision card", "that's a decision", "design card",
        "before implementing", "needs a spec", "spec change needed",
        "can't both", "cannot both", "that is a decision",
    )
    import re as _re
    _DECISION_TITLE = _re.compile(
        r"(?:decide|approve|spec the|amend|ratify|design the|review the "
        r"coexistence|detail the)\b", _re.I)
    decision_shaped_by_id = set()
    for t in tasks:
        astatus = t.get("status")
        a = (t.get("assignee") or "").lower()
        title = t.get("title") or ""
        if astatus == "triage":
            decision_shaped_by_id.add(t["id"])
            continue
        if (astatus == "blocked" or astatus in ("ready", "todo")) and (
                a in ("jobsy", "karl") and _DECISION_TITLE.search(title)):
            decision_shaped_by_id.add(t["id"])

    # child_of only (direction: task_links parent_id->child_id). A card has
    # spawned a decision child if one of its descendants (direct child or
    # deeper) is typed as a decision shape.
    children_of = {}
    for l in links:
        children_of.setdefault(l["parent_id"], []).append(l["child_id"])

    def subtree_has_decision(tid, _seen=None):
        if _seen is None:
            _seen = set()
        if tid in _seen:
            return False
        _seen.add(tid)
        for k in children_of.get(tid, ()):
            if k in decision_shaped_by_id:
                return True
            if subtree_has_decision(k, _seen):
                return True
        return False

    hedged_ids = {}
    for r in con.execute(
            "SELECT task_id, author, body, created_at FROM task_comments "
            "ORDER BY id DESC"):
        b = r["body"] or ""
        hit = next((h for h in DECISION_HEDGE if h in b.lower()), None)
        if not hit:
            continue
        t = by_id.get(r["task_id"])
        if not t or t["status"] not in ACTIVE:
            continue
        # freshness: only recent hedges matter (peer to the block window)
        if now - (r["created_at"] or now) > 7 * 86400:
            continue
        if subtree_has_decision(r["task_id"]):
            continue
        # co-author: if the hedge is the ASSIGNEE themselves testing, don't flag
        hedged_ids.setdefault(
            r["task_id"],
            (t.get("assignee"), t["title"][:55], hit))

    if hedged_ids:
        lines = "\n".join(
            f"    {tid}  @{a}  \"{title}\"\n"
            f"        comment hedges a decision (\"{hedge}\") "
            f"but no triage/design child exists."
            for tid, (a, title, hedge) in hedged_ids.items())
        problems.append(
            f"DECISION PARASITISM  {len(hedged_ids)} card(s) hedge a decision in "
            f"prose without spawning a decision-shaped child:\n{lines}\n"
            f"    A decision-shaped finding must become a typed triage/design card "
            f"(Jobsy scope/AC or Karl design) — never a paragraph for the next "
            f"reader to rediscover. Split: decision→assessor, residual→impl.")

    # ---- 7. UNCOMMITTED SOURCE WORK (dir/worktree drift) ---------------------
    # The 2026-09-04 BackupBrain DnD loss: B1/B2/B3 (build cards) ran in a shared
    # `dir` workspace — edits landed on the repo working tree and were verified
    # live by Rodge, then clobbered and NEVER committed. No branch contained the
    # migration; the served app shipped without it. This is "invisible until the
    # next wipe" — the opposite of a loud failure. Guard here:
    #   a) a `dir`-kind SOURCE card open on a git repo with uncommitted tracked
    #      changes = work that can be silently lost. Flag it.
    #   b) a `worktree` source card whose HEAD base has diverged from the
    #      canonical trunk (`origin/main`) = the wrong-base churn class. Flag.
    # Detection only — zero writes — so it can never compound a bad state.
    import subprocess as _sp

    drift = []
    for t in tasks:
        if t["status"] not in ("ready", "todo", "running", "review"):
            continue
        kind = t.get("workspace_kind")
        wsp = t.get("workspace_path")
        if kind not in ("dir", "worktree") or not wsp:
            continue
        # (a) dir workspace: any uncommitted tracked change in the tree
        if kind == "dir" and os.path.isdir(wsp):
            try:
                p = _sp.run(["git", "-C", wsp, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=20)
                if p.returncode == 0:
                    changed = [ln for ln in p.stdout.splitlines()
                               if ln and not ln.startswith("??")]
                    if changed:
                        drift.append(
                            f"UNCOMMITTED  {t['id']}  @{t.get('assignee')}  "
                            f"dir-workspace  \"{t['title'][:50]}\" — "
                            f"{len(changed)} tracked file(s) dirty in the shared "
                            f"tree. Source work on a `dir` card is not committed "
                            f"and CAN be silently lost (the 09-04 B1/B2/B3 DnD wipe).")
            except Exception:  # noqa: BLE001
                pass
        # (b) worktree: base diverged from canonical trunk (origin/main)
        if kind == "worktree":
            try:
                # origin/main must be an ancestor of the worktree HEAD — i.e.
                # the card is built ON the canonical trunk (possibly with its
                # own newer commits), never off a drift branch missing it.
                q = _sp.run(["git", "-C", wsp, "merge-base", "--is-ancestor",
                             "origin/main", "HEAD"],
                            capture_output=True, text=True, timeout=20)
                if q.returncode not in (0, 1):
                    continue  # safe, exit code is the answer
                if q.returncode == 1:
                    head = ""
                    try:
                        p = _sp.run(["git", "-C", wsp, "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True, timeout=20)
                        head = p.stdout.strip() if p.returncode == 0 else "?"
                    except Exception:  # noqa: BLE001
                        pass
                    drift.append(
                        f"DIVERGED BASE  {t['id']}  @{t.get('assignee')}  "
                        f"worktree HEAD {head or '?'} is built off a base that "
                        f"lacks origin/main — the wrong-base churn class. Rebase "
                        f"onto origin/main (the canonical trunk), not a drift branch.")
            except Exception:  # noqa: BLE001
                pass

    if drift:
        problems.append(
            f"SOURCE-WORKSCAPE DRIFT  {len(drift)} card(s) risk uncommitted loss "
            f"or diverged base:\n" + "\n".join("    " + d for d in drift))

    # ---- 7. MISCLASSIFIED TIME-WAIT -----------------------------------------
    # A card waiting on a DATE must be `scheduled`, never `blocked`. `blocked`
    # is what every watchdog reads as "something went wrong", so a card
    # correctly waiting for a calendar date is flagged by stalled-card-watch
    # every 15 minutes for as long as it waits. That is how t_d770dc07 nagged
    # Richie from 2026-09-03 while behaving perfectly (see charter §6, added
    # 2026-09-05). The board has no due-date column, so the wake date is carried
    # as a `scheduled-until:` comment and scheduled-sweep promotes it hourly.
    #
    # Reporting only. This never blocks, moves or edits a card — misfiling is a
    # classification error, not a fault, and an over-eager auto-move could park
    # a genuinely broken card where no watchdog looks at it.
    import re as _re
    from datetime import date as _date
    _today = _date.today().isoformat()
    _ISO = _re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
    _RESUMABLE = (None, "", "transient", "retryable", "resumable")
    timewait = []
    for t in tasks:
        if t["status"] != "blocked" or (t.get("block_kind") or "") not in _RESUMABLE:
            continue
        texts = []
        try:
            for r in con.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
                "ORDER BY id DESC LIMIT 3", (t["id"],)):
                texts.append(r["payload"] or "")
            for r in con.execute(
                "SELECT body FROM task_comments WHERE task_id=? "
                "ORDER BY created_at DESC LIMIT 5", (t["id"],)):
                texts.append(r["body"] or "")
        except Exception:  # noqa: BLE001
            continue
        blob = "\n".join(texts)
        future = sorted({d for d in _ISO.findall(blob) if d > _today})
        if not future:
            continue
        marked = "scheduled-until:" in blob.lower()
        timewait.append(
            f"MISCLASSIFIED TIME-WAIT  {t['id']}  @{t.get('assignee') or '-'}  "
            f"\"{(t['title'] or '')[:55]}\"\n"
            f"    blocked, but its block reason names a FUTURE date: {', '.join(future[:3])}.\n"
            f"    A card waiting on a date belongs in `scheduled` with a "
            f"`scheduled-until:` marker (charter §6); scheduled-sweep then wakes it.\n"
            f"    Left in `blocked` it alerts stalled-card-watch every 15 min until then.\n"
            f"    Fix: hermes kanban comment {t['id']} 'scheduled-until: {future[0]}T09:00:00+12:00'"
            f" && hermes kanban schedule {t['id']} 'waiting on the calendar'"
            + ("" if not marked else "\n    (it already carries a scheduled-until: marker — "
                                     "it only needs moving to `scheduled`)"))
    problems.extend(timewait)

    # ---- 8. AUTO-POINTS PLACEHOLDER NOT REPLACED ----------------------------
    # The auto-points placeholder (written by points-mint-watch at mint time)
    # must be replaced with a real estimate during Jobsy's triage pass. Cards in
    # todo/ready/running with the placeholder still in a comment are cases where
    # the triage step was skipped — they count against the auto-points-replaced
    # metric and Jobsy should fix them.
    #
    # Silent when count < 5 (the metric target is >=50%; a handful is within
    # noise and does not warrant a report).
    import re as _auto_re
    _AUTO_PTS = _auto_re.compile(r"auto-points:\s*§4\s+placeholder", _auto_re.I)
    auto_placeholder = []
    for t in tasks:
        if t["status"] not in ("todo", "ready", "running"):
            continue
        bodies = [
            r["body"] or ""
            for r in con.execute(
                "SELECT body FROM task_comments WHERE task_id=?", (t["id"],))
        ]
        if any(_AUTO_PTS.search(b) for b in bodies):
            auto_placeholder.append((t["id"], t.get("assignee"), t["title"][:60]))
    if len(auto_placeholder) >= 5:
        lines = "\n".join(
            f"    {tid}  @{a}  \"{title}\""
            for tid, a, title in auto_placeholder)
        problems.append(
            f"AUTO-POINTS STALE  {len(auto_placeholder)} card(s) in todo/ready/running "
            f"still carry the auto-points placeholder:\\n{lines}\\n"
            f"    The auto-points placeholder (from points-mint-watch) should be replaced "
            f"with a real point estimate during Jobsy's triage pass. These cards bypassed "
            f"that step. The fleet target is >=50% replaced; this report means the rate "
            f"is still below that threshold. Re-run with --audit to see the full list.")
    elif audit:
        problems.append(
            f"AUTO-POINTS STALE  {len(auto_placeholder)} card(s) — below report threshold. "
            f"Clean enough for a silent tick.")

    if audit:
        print(f"board            : {KANBAN_DB}")
        print(f"tasks            : {len(tasks)}  active={sum(1 for t in tasks if t['status'] in ACTIVE)}")
        print(f"valid profiles   : {', '.join(sorted(valid))}")
        print(f"priority inversions: {len(inversions)}")
        print(f"problems         : {len(problems)}")
        for p in problems:
            print("  " + p.replace("\n", "\n  "))
        return 0

    if problems:
        print(clean("Kanban board integrity — %d issue(s):\n\n" % len(problems) + "\n\n".join(problems)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
