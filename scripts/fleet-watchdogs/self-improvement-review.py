#!/usr/bin/env python3
"""self-improvement-review — charter §12 (build-list item 11), 2026-09-03.

Runs from Smith's cron off-peak. Zero tokens unless the board is quiet AND there
is something to review; then it spawns ONE Smith session with the numbers.

Precondition (charter §12): board quiet = zero running cards and nothing
dispatched in the last 30 minutes. Otherwise defer: log the deferral to
logs/self-improvement-review.jsonl and exit silently (the cron re-fires next
schedule; there is no retry storm).

Metrics (charter §2), each over 7 and 28 days, computed read-only from
kanban.db + logs/cost-ledger.jsonl and written to
logs/self-improvement-review-<date>.md:
  reliability   crash rate per run
  robustness    retry share (runs beyond the first, as share of runs)
  speed         median cycle time; runs per card
  cost          cost per card (mean/median); cap utilisation; uncapped cards
  quality       Rodge first-pass approval WITH denominator
  responsiveness block -> first triage action (median)
  estimation    points coverage; estimate-vs-actual
  changes       change manifests applied in the window (from ~/.hermes/changes)

Smith's brief (the spawned prompt): compare 7d vs 28d, name at most TWO changes,
each as a HELD card under himself with reason / metric / predicted delta /
cost-estimate placeholder / <= $1 PoC plan / manifest id / EXTENSION POINT, and a
one-line DM to Richie. Propose, never apply. If nothing clears the bar, say so in
one line.

The extension-point line was added 2026-09-12. Until then the brief asked for "the
change-manifest id it will use" and said nothing about WHERE a change may live —
which reads as an invitation to patch whatever needs patching. That is how a batch
of board-improvement cards came to rewrite tools/kanban_tools.py. The fix order is
now stated in the brief itself, because the brief is what Smith acts on; his SOUL
carries the same rule, but a SOUL is read by Smith alone and ~70% of cards are
minted by paths that never read one.

Flags: --dry-run (compute + write the report, do not spawn), --force (ignore the
quiet-board precondition; for the first attended run).
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent
DB = HERMES_HOME / "kanban.db"
LEDGER = HERMES_HOME / "logs" / "cost-ledger.jsonl"
CHANGES = HERMES_HOME / "changes"
LOG = HERMES_HOME / "logs" / "self-improvement-review.jsonl"
HERMES_BIN = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes"
QUIET_MIN = 30


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def quiet(c):
    running = c.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
    last = c.execute("SELECT MAX(started_at) FROM task_runs").fetchone()[0] or 0
    since = (time.time() - last) / 60
    return running == 0 and since >= QUIET_MIN, running, since


def window(c, ledger, days):
    cutoff = time.time() - days * 86400
    runs = c.execute("SELECT task_id, outcome, started_at, ended_at FROM task_runs WHERE started_at >= ?", (cutoff,)).fetchall()
    n_runs = len(runs)
    crashed = sum(1 for r in runs if (r[1] or "") in ("crashed", "timed_out", "spawn_failed"))
    per_card = {}
    for r in runs:
        per_card[r[0]] = per_card.get(r[0], 0) + 1
    retries = sum(v - 1 for v in per_card.values() if v > 1)
    cards = c.execute("SELECT id, created_at, completed_at, status FROM tasks WHERE created_at >= ?", (cutoff,)).fetchall()
    done = [r for r in cards if r[3] == "done" and r[2]]
    cycle = [(r[2] - r[1]) / 60 for r in done if r[1]]
    # first-pass approval: first review outcome per card
    first = {}
    for tid, kind in c.execute(
        "SELECT task_id, kind FROM task_events WHERE created_at >= ? AND kind IN ('accepted','changes_requested','completed') ORDER BY id",
        (cutoff,),
    ).fetchall():
        if tid in first:
            continue
        if kind == "changes_requested":
            first[tid] = False
        elif kind in ("accepted",):
            first[tid] = True
    reviewed = [tid for tid, in c.execute("SELECT DISTINCT task_id FROM task_events WHERE created_at >= ? AND kind='review_requested'", (cutoff,)).fetchall()]
    cr = sum(1 for t in reviewed if first.get(t) is False)
    fp_num, fp_den = len(reviewed) - cr, len(reviewed)
    led = [r for r in ledger.values() if (r.get("created_at") or 0) >= cutoff]
    costs = [r["actual_usd"] for r in led]
    util = [r["actual_usd"] / r["cap_usd"] for r in led if r.get("cap_usd")]
    lat = [r["block_to_triage_s"] for r in led if r.get("block_to_triage_s") is not None]
    pts = [r for r in led if r.get("points")]
    # est-mintpath-20260911: coverage alone cannot tell a real estimate from the
    # auto-points placeholder the mint path writes. The PoC's decision rule is
    # the share of placeholder cards a specifier replaced — a flat 100% coverage
    # with 0% replacement means the specifier step is missing and the change is
    # a hold, not a win.
    auto_pts = [r for r in led if r.get("has_auto_points")]
    est = [(r["estimate_usd"], r["actual_usd"]) for r in led if r.get("estimate_usd")]
    changes = []
    if CHANGES.exists():
        for p in CHANGES.glob("*.json"):
            try:
                m = json.loads(p.read_text())
                if m.get("applied_at") and datetime.fromisoformat(m["applied_at"]).timestamp() >= cutoff:
                    changes.append(f"{m['id']} ({m.get('status')})")
            except Exception:  # noqa: BLE001
                pass
    return {
        "days": days, "runs": n_runs, "crash_rate": (crashed / n_runs) if n_runs else None,
        "retry_share": (retries / n_runs) if n_runs else None,
        "cards": len(cards), "done": len(done), "median_cycle_min": _median(cycle),
        "runs_per_card": (n_runs / len(per_card)) if per_card else None,
        "cost_mean": (sum(costs) / len(costs)) if costs else None, "cost_median": _median(costs),
        "cap_util": (sum(util) / len(util)) if util else None,
        "uncapped": sum(1 for r in led if r.get("cap_usd") is None),
        "first_pass": (fp_num, fp_den),
        "block_to_triage_median_min": (_median(lat) / 60) if lat else None,
        "points_coverage": (len(pts), len(led)),
        "placeholder_replacement": (
            sum(1 for r in auto_pts if not r.get("points_auto")), len(auto_pts)
        ),
        "estimate_ratio": (sum(a for _, a in est) / sum(e for e, _ in est)) if est and sum(e for e, _ in est) else None,
        "changes_applied": changes,
    }


def fmt(w):
    def p(x):
        return "-" if x is None else f"{x*100:.1f}%"
    def f(x, d=2):
        return "-" if x is None else f"{x:.{d}f}"
    fp = w["first_pass"]
    return "\n".join([
        f"### last {w['days']} days — {w['cards']} cards created, {w['done']} done, {w['runs']} runs",
        f"- reliability: crash rate per run {p(w['crash_rate'])}",
        f"- robustness: retry share of runs {p(w['retry_share'])}; runs per card {f(w['runs_per_card'])}",
        f"- speed: median cycle {f(w['median_cycle_min'],0)} min",
        f"- cost: mean ${f(w['cost_mean'],4)} / median ${f(w['cost_median'],4)} per card; cap utilisation {p(w['cap_util'])}; uncapped {w['uncapped']}",
        f"- quality: Rodge first-pass approval {fp[0]}/{fp[1]}" + (f" = {fp[0]/fp[1]*100:.1f}%" if fp[1] else " (no reviews)"),
        f"- responsiveness: block -> first triage action median {f(w['block_to_triage_median_min'],0)} min",
        f"- estimation: points on {w['points_coverage'][0]}/{w['points_coverage'][1]} ledger cards; actual/estimate {f(w['estimate_ratio'])}",
        f"- estimation: auto-points placeholder replaced by a real estimate on "
        f"{w['placeholder_replacement'][0]}/{w['placeholder_replacement'][1]} cards"
        + (f" = {w['placeholder_replacement'][0]/w['placeholder_replacement'][1]*100:.0f}%"
           " (PoC: >=80% expand, <50% the specifier step is missing)"
           if w['placeholder_replacement'][1] else " (none minted yet)"),
        f"- changes applied: {', '.join(w['changes_applied']) or 'none'}",
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ok, running, since = quiet(c)
    rec = {"at": int(time.time()), "quiet": ok, "running": running, "min_since_dispatch": round(since)}
    if not ok and not a.force:
        rec["action"] = "deferred"
        LOG.parent.mkdir(exist_ok=True)
        LOG.open("a").write(json.dumps(rec) + "\n")
        return 0  # silent: the cron re-fires on schedule
    ledger = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                r = json.loads(line); ledger[r["card"]] = r
            except Exception:  # noqa: BLE001
                pass
    w7, w28 = window(c, ledger, 7), window(c, ledger, 28)
    date = datetime.now().strftime("%Y-%m-%d")
    report = HERMES_HOME / "logs" / f"self-improvement-review-{date}.md"
    report.write_text(
        f"# Self-improvement review — {date}\n\nCharter §2 metrics, 7-day vs 28-day baseline. "
        f"Board quiet: running={running}, {since:.0f} min since last dispatch.\n\n{fmt(w7)}\n\n{fmt(w28)}\n\n"
        "Baselines (FLEET-REVIEW-2026-09-01): crash 14.4% -> 0/22 after finalize guard; retry spend 38.6%; "
        "median cycle 15 min; 2.42 runs/card; $0.319 mean / $0.118 median; cap util 67%; first-pass 59/98 = 60.2%.\n"
    )
    rec.update({"action": "dry-run" if a.dry_run else "spawned", "report": str(report)})
    LOG.parent.mkdir(exist_ok=True)
    LOG.open("a").write(json.dumps(rec) + "\n")
    print(f"report: {report}")
    if a.dry_run:
        return 0
    prompt = (
        f"Self-improvement review (charter §12). Read {report} — it holds the §2 metrics for the last 7 "
        "and 28 days, computed mechanically. Compare 7d against 28d and against the baselines at the bottom. "
        "Decide whether ANY change to the system is justified by a metric moving the wrong way, or a clear "
        "opportunity to move one. Rules: propose at most TWO changes; each is a kanban card created HELD "
        "(kanban_create with hold=true) assigned to you, whose body states: the reason (which metric, what "
        "it reads now vs baseline), the metric it is predicted to move and by how much, a `cost-estimate:` line "
        "left for Steve-o to confirm, a proof-of-concept plan capped at $1, and the change-manifest id it will "
        "use (charter §12; `python3 ~/.hermes/scripts/change-manifest.py`). One variable per change.\n\n"
        "WHERE THE CHANGE GOES — state this explicitly in every card body as a line "
        "`extension-point: <plugin|watchdog|soul-or-skill|config>`, and justify it. Upstream's kanban is the "
        "SUBSTRATE: a card may NOT edit hermes_cli/, tools/, agent/ or gateway/. Take the first of these that "
        "fits: (1) a PLUGIN on an upstream hook — pre_tool_call is the fail-closed one, kind: backend so it "
        "reaches workers (see plugins/kanban-mint-guard); (2) a no_agent WATCHDOG in scripts/ plus a cron entry "
        "— zero tokens, reports rather than blocks (see scripts/fleet-watchdogs/); (3) a SOUL or SKILL rule when "
        "what you are fixing is a judgement rather than a mechanism; (4) config. If NONE of those fits, say so "
        "and hand the problem to Richie — do not propose a core patch as the fallback. On 2026-09-12 a batch of "
        "board-improvement cards patched the kernel instead: one merge rewrote tools/kanban_tools.py by "
        "+3891/-1945, silently dropped code another card had added ninety minutes earlier, and spawned three "
        "more cards to repair the damage. All of it was reverted. Never apply "
        "anything yourself in this session. Finish with a 3-line summary for Richie: what you proposed (card ids) "
        "or 'nothing clears the bar' with the one number that decided it. Keep the whole session under ten "
        "tool calls."
    )
    try:
        subprocess.Popen([str(HERMES_BIN), "-p", "default", "--cli", "chat", "-q", prompt],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        print("spawned Smith review session")
    except Exception as e:  # noqa: BLE001
        print(f"spawn failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
