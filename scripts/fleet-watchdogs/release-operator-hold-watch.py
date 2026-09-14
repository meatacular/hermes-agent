#!/usr/bin/env python3
"""release-operator-hold-watch — the guard rail on `operator_hold` auto-release.

Extension point: watchdog (no_agent cron, every 5m, zero tokens unless releasing).

WHY THE DEFAULT IS "HOLD"
-------------------------
`block_kind='operator_hold'` is the fleet's HUMAN GATE, and every other control in
the fleet treats it as one:

  * `kanban_db.recompute_ready` never auto-promotes it — "sticky by definition …
    only a human unblock ends it" (hermes_cli/kanban_db.py, 2026-09-03);
  * `kanban-block-escalator` never escalates it (`NEVER_TRIGGER_KINDS`);
  * charter §8 mints every approved-but-undeployed platform card as
    `blocked`/`operator_hold` with a parent — `_maybe_create_deploy_followup`:
    "approval precedes deploy … Do not unblock it yourself";
  * the SOUL's scheduled-vs-blocked rule reserves it for "a decision only
    Richie can make".

A watchdog that clears that status on a signal the gate's owner never gave makes
every `operator_hold` advisory. So this script HOLDS by default and releases only
what a card explicitly declares to be a dependency wait.

THE MARKER — machine-readable, set by the card that knows the gate is human
-------------------------------------------------------------------------
A line in the card BODY (canonical: the minting card writes it) or in the newest
`blocked` reason (a human re-blocking by hand):

    operator-hold: dependency-wait

means "my hold exists only because my dependency parents had not finished;
release me when they do". The explicit opposite:

    operator-hold: manual

means "a human must release this". It is never released, and it WINS when a card
carries both markers (fail-safe). The marker must START a line (after list,
quote, heading or emphasis punctuation) so prose that merely mentions it — "do
NOT add `operator-hold: dependency-wait` to this card" — cannot arm a release.

RULES, IN EVALUATION ORDER — every emitted line names its rule
--------------------------------------------------------------
  1. `design_approval`  assignee `karl` or title `[Karl]`        -> hold
  2. `manual_hold`      `operator-hold: manual`                  -> hold
  3. `no_parents`       standalone hold                          -> RELEASE + announce
  4. `parents_pending`  some dependency parent not done          -> hold
  5. `no_marker`        parents done, no dependency-wait marker   -> RELEASE + announce
  6. `dependency_wait`  marker + all parents done                -> RELEASE

CHANGED 2026-09-15 (Richie). Rules 3 and 5 used to HOLD: "I don't want any mandatory holds added
back to the system, I just want reporting to flow so I am aware. I don't want work to continue to
stop and wait on my approval apart from design review or cost cap or turn reviews." The only holds
left here are rule 1 (design approval, which he kept) and rule 2 (a card that says
`operator-hold: manual` in so many words). Everything else releases AND says so — the announcement
is what replaced the gate. Cost-cap and turn-cap blocks carry a different block_kind and this
watchdog never sees them, so those two gates are untouched by construction.

The watchdog never infers intent from prose it does not parse. Rules 1-4 are
routine and silent (fleet convention: a watchdog is silent unless something is
wrong); a release, a rule-5 hold, or a failed unblock prints a line naming the
card AND the rule, and cron delivers it to the watchdog channel. A rule-5 hold is
announced ONCE per card per rule, not every tick.

HISTORY
-------
2026-09-14 (Richie): held cards whose parents were all done were being released
by hand. The first version of this script automated that by releasing EVERY
`operator_hold` whose parents were done, exempting only design cards. It released
`t_b0ac4e9d` — a deploy card held for Richie's attended root restart — three
seconds after its review parent completed, and would have done the same to every
auto-minted deploy follow-up (§8). The pattern it was written for is real (a PM
decomposition minting its children held as sequencing waits), which is why the
marker exists rather than a blanket hold: `dependency-wait` keeps that case
released, and nothing else is. See card `t_aa3bcf6c`.

STATE
-----
`~/.hermes/state/release-operator-hold-watch.json` — append-only list of runs
(last 100), each `{"run_at", "released": [...], "skipped": [...],
"announced": [...]}`. Every entry carries its `rule` (skipped entries also keep
the older `reason` key, same value).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = Path(os.environ.get("RELEASE_HOLD_DB") or HERMES_HOME / "kanban.db")
STATE = Path(os.environ.get("RELEASE_HOLD_STATE")
             or HERMES_HOME / "state" / "release-operator-hold-watch.json")
HERMES_BIN = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes"
DRYRUN = os.environ.get("RELEASE_HOLD_DRYRUN") == "1"

# Design-approval patterns — cards matching these stay held for manual release
# (unchanged exemption; checked first so it can never be weakened by a marker).
DESIGN_ASSIGNEES = {"karl"}
DESIGN_TITLE_PREFIX = "[Karl]"

# Rule names. A rule is the only thing that decides a card's fate, and it is
# what every line and every state entry reports.
RULE_DESIGN = "design_approval"
RULE_MANUAL = "manual_hold"
RULE_NO_PARENTS = "no_parents"
RULE_PENDING = "parents_pending"
RULE_NO_MARKER = "no_marker"
RULE_RELEASE = "dependency_wait"
RULE_RE_BLOCKED = "re_blocked"
RULE_UNBLOCK_FAILED = "unblock_failed"

MARKER_DEPENDENCY = "dependency-wait"
MARKER_MANUAL = "manual"

# The marker must open a line: optional whitespace, list/heading/quote/emphasis
# punctuation, then the token. Anchoring is the point — an unanchored substring
# search would let a card's own prose ("do not add operator-hold: manual")
# release a gate.
_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]*|#{1,6}[ \t]+)*"
    r"(?:\*\*|__|`)?[ \t]*operator-hold[ \t]*:[ \t]*(?:\*\*|__|`)?[ \t]*"
    r"(manual|dependency-wait)\b",
    re.IGNORECASE | re.MULTILINE,
)


def find_marker(*texts):
    """The marker a card declares, or None.

    ``manual`` wins over ``dependency-wait`` when a card carries both: the
    fail-safe direction is to keep the human gate.
    """
    found = set()
    for text in texts:
        if not text:
            continue
        for match in _MARKER_RE.finditer(str(text)):
            found.add(match.group(1).lower())
    if MARKER_MANUAL in found:
        return MARKER_MANUAL
    if MARKER_DEPENDENCY in found:
        return MARKER_DEPENDENCY
    return None


def latest_block_reason(cur, tid):
    """The reason on the newest ``blocked`` event, or "".

    Read so a human re-blocking a card by hand can declare the marker in the
    reason as well as in the body.
    """
    row = cur.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    if row is None:
        return ""
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("reason") or "")


def load_history():
    """The state file's run list (append-only; tolerant of a corrupt file)."""
    if not STATE.exists():
        return []
    try:
        data = json.loads(STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def log_state(history, released, skipped, announced):
    """Append this run and keep the last 100 — never rewrite an older entry."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    history.append({
        "run_at": datetime.now().isoformat(),
        "released": released,
        "skipped": skipped,
        "announced": announced,
    })
    STATE.write_text(json.dumps(history[-100:], indent=2))


def announced_before(history, tid, rule):
    """True when the newest run that mentions this card already announced it
    under the same rule — so a card waiting on Richie is announced once, not
    every five minutes. Derived from the run history, so the state file stays
    append-only (no mutable 'already seen' section to corrupt)."""
    for run in reversed(history):
        if not isinstance(run, dict):
            continue
        for bucket in ("released", "announced", "skipped"):
            for entry in run.get(bucket) or []:
                if isinstance(entry, dict) and entry.get("id") == tid:
                    return entry.get("rule") == rule
    return False


RELEASE_LOOP_WINDOW_H = 24


def released_before(history, tid, within_hours=RELEASE_LOOP_WINDOW_H):
    """When this watchdog released this card in the last `within_hours`, return that run's
    timestamp; otherwise None.

    Why this exists (2026-09-15, t_796c3fab): under the no-mandatory-holds rule this watchdog
    releases a standalone hold. If the card then comes straight back as `operator_hold` — because
    a worker hit it, blocked, and overwatch re-held it — releasing it again just runs the same
    worker into the same wall and bills for it. That is the churn Richie is trying to stop, and it
    is the loop the kernel already breaks elsewhere by routing repeat blocks to triage.

    So: release once. A card that comes BACK is not a stalled hold, it is a card the board is
    telling us it cannot do. Hold it, say so once, and let Richie decide. This is a loop breaker,
    not a re-introduced approval gate — it can only ever fire on a card this watchdog itself
    already released, and it expires after a day so an unrelated later block is not caught by it.
    """
    cutoff = datetime.now() - timedelta(hours=within_hours)
    for run in reversed(history):
        if not isinstance(run, dict):
            continue
        try:
            when = datetime.fromisoformat(run.get("run_at") or "")
        except ValueError:
            continue
        if when < cutoff:
            break                       # history is append-only, so everything older is older
        for entry in run.get("released") or []:
            if isinstance(entry, dict) and entry.get("id") == tid and not entry.get("dry_run"):
                return run.get("run_at")
    return None


def _skip(tid, title, rule, detail, skipped):
    """Record a hold. ``reason`` is kept alongside ``rule`` for the older
    state-file format; both are the same value."""
    skipped.append({
        "id": tid,
        "title": title,
        "reason": rule,
        "rule": rule,
        "detail": detail,
    })


def _do_release(tid, title, parents, rule, why, released, skipped, lines):
    """The single release path. Rules 3, 5 and 6 all land here so there is one implementation
    of "unblock and record it", not three that can drift."""
    lines.append(f"RELEASE [{rule}] {tid} — {title} ({why})")
    if DRYRUN:
        released.append({"id": tid, "title": title, "parents": parents,
                         "rule": rule, "dry_run": True})
        return
    # 2026-09-15: `unblock` with no --reason writes an event whose payload is None — no author, no
    # mechanism. Overwatch found exactly that on t_796c3fab and could not tell what had released a
    # card that said it was held ("unblocked at event 17803 with payload=None"). The CLI records a
    # --reason as an `UNBLOCK: …` comment authored by the running profile, so the board itself now
    # carries the answer instead of it living only in a Slack message.
    result = subprocess.run(
        [str(HERMES_BIN), "kanban", "unblock", "--reason",
         f"release-operator-hold-watch [{rule}]: {why}", tid],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0:
        released.append({"id": tid, "title": title, "parents": parents, "rule": rule})
        return
    detail = (result.stderr or "").strip() or (result.stdout or "").strip()
    if detail:
        lines.pop()
        _skip(tid, title, RULE_UNBLOCK_FAILED, detail, skipped)
        lines.append(f"FAILED [{RULE_UNBLOCK_FAILED}] {tid} — {title}: {detail}")
    else:
        # No stderr: most likely unblocked by another process between the query and this call.
        released.append({"id": tid, "title": title, "parents": parents,
                         "rule": rule, "note": "may already be unblocked"})


def main():
    if not DB.exists():
        print(f"release-operator-hold-watch: DB not found: {DB}")
        sys.exit(0)

    history = load_history()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT id, title, assignee, block_kind, body FROM tasks "
        "WHERE status = 'blocked' AND block_kind = 'operator_hold'"
    )
    held = cur.fetchall()

    released = []
    skipped = []
    announced = []
    lines = []

    for card in held:
        tid = card["id"]
        title = card["title"]
        assignee = card["assignee"] or ""
        marker = find_marker(card["body"], latest_block_reason(cur, tid))

        # 1. design approval — Richie's manual sign-off, never auto-released
        if assignee in DESIGN_ASSIGNEES or title.startswith(DESIGN_TITLE_PREFIX):
            _skip(tid, title, RULE_DESIGN,
                  f"assignee={assignee}, title_prefix_match="
                  f"{title.startswith(DESIGN_TITLE_PREFIX)}", skipped)
            continue

        # 2. the card names the human gate explicitly
        if marker == MARKER_MANUAL:
            _skip(tid, title, RULE_MANUAL,
                  "explicit `operator-hold: manual` — a human must release this",
                  skipped)
            continue

        # 2b. this watchdog already released this card and it came back blocked — loop breaker.
        #     Checked BEFORE the parent rules so no release path can bypass it.
        prior = released_before(history, tid)
        if prior:
            _skip(tid, title, RULE_RE_BLOCKED,
                  f"released at {prior} and blocked again — not releasing a second time", skipped)
            if not announced_before(history, tid, RULE_RE_BLOCKED):
                announced.append({"id": tid, "title": title, "rule": RULE_RE_BLOCKED,
                                  "detail": f"released at {prior}, came back blocked"})
                lines.append(f"HOLD [{RULE_RE_BLOCKED}] {tid} — {title} "
                             f"(released at {prior}, came straight back blocked; "
                             f"a second release would just re-run the same failure)")
            continue

        # 3. dependency parents
        parents = [row["parent_id"] for row in cur.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (tid,))]
        if not parents:
            # 2026-09-15: a standalone hold has nothing to wait FOR, so under the
            # no-mandatory-holds rule it is not a gate, it is a stall. Release and announce.
            _do_release(tid, title, [], RULE_NO_PARENTS,
                        "standalone hold — no dependency to wait for",
                        released, skipped, lines)
            continue

        placeholders = ",".join("?" for _ in parents)
        parent_rows = {
            row["id"]: row["status"]
            for row in cur.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
                parents)
        }
        all_done = all(parent_rows.get(pid) == "done" for pid in parents)

        # 4. still waiting on a dependency
        if not all_done:
            pending = [pid for pid in parents if parent_rows.get(pid) != "done"]
            _skip(tid, title, RULE_PENDING, f"pending parents: {pending}", skipped)
            continue

        # 5. parents done, but the card never declared itself a dependency wait:
        #    `operator_hold` means a human gate, so it stays held and is
        #    announced once. This is the 2026-09-14 defect's exact shape.
        if marker != MARKER_DEPENDENCY:
            # 2026-09-15: this used to HOLD and announce once. A card whose dependency parents
            # are ALL done is waiting for nothing; the marker is no longer what earns a release.
            _do_release(tid, title, parents, RULE_NO_MARKER,
                        f"all {len(parents)} parents done (no marker needed since 2026-09-15)",
                        released, skipped, lines)
            continue

        # 6. the card declares itself a dependency wait — release it
        _do_release(tid, title, parents, RULE_RELEASE,
                    f"all {len(parents)} parents done; marker: operator-hold: dependency-wait",
                    released, skipped, lines)

    conn.close()
    log_state(history, released, skipped, announced)

    if lines:
        print("\n".join(lines))
        print(f"release-operator-hold-watch: {len(released)} released, "
              f"{len(skipped)} held, {len(announced)} awaiting a human — "
              f"rules: {sorted({l.split('[')[1].split(']')[0] for l in lines})}")


if __name__ == "__main__":
    main()
