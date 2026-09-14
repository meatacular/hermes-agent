#!/usr/bin/env python3
"""Per-card cost ledger and periodic review — WeRoll cost policy, 2026-09-02.

Richie's requirement: "all cards must be given cost estimates and have their
lifetime real costs tracked and logged and periodically reviewed."

The board stores the cap but never the actual spend — `enforce_max_cost`
recomputes it every tick from the assignee profile's `state.db` and throws the
number away. So a card's real lifetime cost is unrecoverable once its sessions
age out. This writes it down.

Two modes:
  (default)  append/refresh one JSONL row per terminal card into
             ~/.hermes/logs/cost-ledger.jsonl. Idempotent — a card already
             recorded with the same spend is not rewritten. Silent.
  --review   print an estimate-vs-actual review over a window (default 7 days):
             which cards overran, by how much, and how good the estimates are.
             This is the periodic review; run it weekly on cron.

Charter §6/§9 fields added 2026-09-03 (build-list item 6): `points` and
`estimate_usd` from Steve-o's `points-estimate` / `cost-estimate` comments,
`extensions` (count of `cost-extension` comments), and `block_to_triage_s`
(median seconds from each `blocked` event to the first non-dispatcher action
that followed it — the Responsiveness metric in charter §2). `--review` reports
estimate-vs-actual by points value and cap utilisation per points bucket, which
is what POINTS-ALGORITHM §5 calibrates from.

ModelArk (2026-09-11, Richie): every DeepSeek v4-flash/v4-pro slot runs on the ModelArk Coding
Plan, a flat subscription that REPORTS NO COST ($0, "modelark subscription"). The $1 cap still counts
a cap-equivalent for it (plugins/modelark-pricing: tokens x DeepSeek list rate), which is not money.
Each row records `actual_usd` = invoiced spend only, `modelark_calls` / `modelark_notional_usd` (the
cap-equivalent) for the subscription share, and `cap_basis_usd` (what enforce_max_cost saw = both).
`over_cap` is judged on cap_basis. Reports print the ModelArk
share as "modelark subscription (N calls)", never as dollars. Rows written before 09-11 have no
ModelArk share (they predate it), so their actual_usd is unchanged in meaning.

`no_agent`, read-only against every database, zero LLM tokens. Exit 0 always.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import billing_labels as BL  # noqa: E402 — shared "modelark subscription" labelling

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_HOME.parent.name == "profiles":
    HERMES_HOME = HERMES_HOME.parent.parent

DB = HERMES_HOME / "kanban.db"
LEDGER = HERMES_HOME / "logs" / "cost-ledger.jsonl"
TERMINAL = ("done", "archived", "blocked")


def _profile_costs(card_ids: set) -> dict:
    """Spend per card id across every profile ledger, split invoiced vs ModelArk subscription.
    Sessions match on the worker title ("Work kanban task t_xxxxxxxx #3") — see billing_labels."""
    return BL.card_costs(HERMES_HOME, card_ids)


EST_RE = re.compile(r"cost-estimate[^0-9$]*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
PTS_RE = re.compile(r"points-estimate[^0-9]*([0-9]+)", re.I)
MACHINE_AUTHORS = ("dispatcher", "worker", "system", "kanban-block-escalator", "pre-review-gate")


def _card_meta(conn, tid: str) -> dict:
    """points / estimate / extensions from comments; block->triage latency from events."""
    meta = {"points": None, "points_auto": None, "has_auto_points": False,
            "estimate_usd": None, "extensions": 0, "block_to_triage_s": None,
            "blocks": 0}
    try:
        for (body,) in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,)
        ).fetchall():
            b = body or ""
            m = PTS_RE.search(b)
            if m:
                meta["points"] = int(m.group(1))       # last one wins (Steve-o corrects)
                # Charter §4 at the mint path (est-mintpath-20260911): every card
                # is minted with an auto-points placeholder that the specifier is
                # meant to replace. Record whether the WINNING estimate is still
                # that placeholder, and whether one was ever written, so the
                # review can compute placeholder_replacement_rate — the PoC's
                # decision rule. `points` alone cannot tell a real estimate from
                # a placeholder, and a metric nobody computes is inert.
                meta["points_auto"] = "auto-points" in b
            if "auto-points" in b:
                meta["has_auto_points"] = True
            m = EST_RE.search(b)
            if m:
                meta["estimate_usd"] = float(m.group(1))
            if "cost-extension" in b:
                meta["extensions"] += 1
    except Exception:
        pass
    try:
        events = conn.execute(
            "SELECT kind, payload, created_at FROM task_events WHERE task_id=? ORDER BY id", (tid,)
        ).fetchall()
        lat = []
        for i, (kind, payload, ts) in enumerate(events):
            if kind != "blocked":
                continue
            meta["blocks"] += 1
            for k2, p2, t2 in events[i + 1:]:
                if k2 in ("unblocked", "spawned", "claimed", "archived", "completed"):
                    lat.append(t2 - ts); break
                if k2 == "commented":
                    try:
                        author = (json.loads(p2 or "{}").get("author") or "").lower()
                    except Exception:
                        author = ""
                    if author and author not in MACHINE_AUTHORS:
                        lat.append(t2 - ts); break
        if lat:
            meta["block_to_triage_s"] = int(sorted(lat)[len(lat) // 2])
    except Exception:
        pass
    return meta


def _load_ledger() -> dict:
    rows = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                r = json.loads(line)
                rows[r["card"]] = r
            except Exception:
                continue
    return rows


def cmd_record() -> int:
    if not DB.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cards = conn.execute(
            "SELECT id,title,assignee,status,block_kind,max_cost,created_at,completed_at,"
            "consecutive_failures,tenant,created_by FROM tasks WHERE status IN (?,?,?)",
            TERMINAL,
        ).fetchall()
    except Exception as e:
        print(f"cost-ledger ERROR: {e}")
        return 0

    ids = {r["id"] for r in cards}
    costs = _profile_costs(ids)
    existing = _load_ledger()
    new = []
    for r in cards:
        c = costs.get(r["id"])
        if not c:
            continue
        spend = round(c["spend"], 6)                 # cap basis (notional ModelArk included)
        billed = round(c["billed"], 6)
        prev = existing.get(r["id"])
        if (prev and abs(prev.get("cap_basis_usd", prev.get("actual_usd", 0)) - spend) < 1e-9
                and "block_to_triage_s" in prev and "cap_basis_usd" in prev):
            continue  # already recorded, unchanged
        meta = _card_meta(conn, r["id"])
        new.append({
            **meta,
            "card": r["id"], "title": (r["title"] or "")[:120], "assignee": r["assignee"],
            "status": r["status"], "block_kind": r["block_kind"], "tenant": r["tenant"],
            "created_by": r["created_by"], "cap_usd": r["max_cost"],
            "actual_usd": billed, "cap_basis_usd": spend,
            "modelark_notional_usd": round(c["modelark_notional"], 6),
            "modelark_calls": c["modelark_calls"], "modelark_tokens": c["modelark_tokens"],
            "sessions": c["sessions"], "tool_calls": c["tools"],
            "output_tokens": c["output_tokens"], "by_profile": c["billed_by_profile"],
            "modelark_calls_by_profile": c["modelark_calls_by_profile"],
            "models": sorted(c["models"]),
            "over_cap": (r["max_cost"] is not None and spend > r["max_cost"]),
            "created_at": r["created_at"], "completed_at": r["completed_at"],
            "recorded_at": int(time.time()),
        })
    if not new:
        return 0
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    # Rewrite whole file so a refreshed row replaces its predecessor.
    merged = {**existing, **{r["card"]: r for r in new}}
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(v) for v in merged.values()) + "\n")
    tmp.replace(LEDGER)
    return 0


def cmd_review(days: int) -> int:
    rows = list(_load_ledger().values())
    cutoff = time.time() - days * 86400
    rows = [r for r in rows if (r.get("created_at") or 0) >= cutoff]
    if not rows:
        print(f"cost-ledger review: no cards in the last {days} days.")
        return 0
    for r in rows:  # rows recorded before 09-11 carry no ModelArk share
        r.setdefault("cap_basis_usd", r["actual_usd"]); r.setdefault("modelark_calls", 0)
        r.setdefault("modelark_notional_usd", 0.0)
    rows.sort(key=lambda r: -r["cap_basis_usd"])
    total = sum(r["actual_usd"] for r in rows)
    ma_calls = sum(r["modelark_calls"] for r in rows)
    ma_cards = sum(1 for r in rows if r["modelark_calls"])
    capped = [r for r in rows if r["cap_usd"] is not None]
    over = [r for r in rows if r["over_cap"]]
    uncapped = [r for r in rows if r["cap_usd"] is None]

    print(f"# Cost review — last {days} days ({len(rows)} cards, ${total:.2f} billed"
          + (f" + {BL.LABEL}: {ma_calls} calls on {ma_cards} cards" if ma_calls else "") + ")\n")
    if ma_calls:
        print(f"  ({BL.LABEL} = flat ModelArk Coding Plan; it reports no cost. The cap counts a cap-equivalent"
              " for it so it can fire — cap figures below include that, the $ billed figures do not)\n")
    print(f"  median card      ${sorted(r['actual_usd'] for r in rows)[len(rows)//2]:.4f}")
    print(f"  mean card        ${total/len(rows):.4f}")
    print(f"  capped           {len(capped)}/{len(rows)}"
          + (f"   ** {len(uncapped)} UNCAPPED — policy breach **" if uncapped else ""))
    print(f"  broke their cap  {len(over)}")
    if capped:
        util = [r["cap_basis_usd"] / r["cap_usd"] for r in capped if r["cap_usd"]]
        print(f"  mean cap use     {sum(util)/len(util)*100:.0f}%  "
              f"(low = estimates too generous, >100% = under-estimated)")
    print("\n  Most expensive:")
    for r in rows[:8]:
        cap = f"${r['cap_usd']:.2f}" if r["cap_usd"] is not None else "NONE"
        flag = "  <== OVER CAP" if r["over_cap"] else ""
        ma = f" + {BL.LABEL} ({r['modelark_calls']} calls, cap-basis ${r['cap_basis_usd']:.4f})" if r["modelark_calls"] else ""
        print(f"    ${r['actual_usd']:7.4f} billed / cap {cap:>6}  {r['sessions']:2} sess  "
              f"{r['assignee'] or '-':8} {r['title'][:52]}{flag}{ma}")
    if uncapped:
        print("\n  Uncapped cards (should be none):")
        for r in uncapped[:10]:
            print(f"    {r['card']} {r['created_by'] or '?':14} {r['title'][:60]}")
    pointed = [r for r in rows if r.get("points")]
    print(f"\n  Points (charter §4): {len(pointed)}/{len(rows)} cards carry a points-estimate"
          + ("" if pointed else "   ** none — Steve-o is not estimating **"))
    # est-mintpath-20260911: the placeholder is not an estimate. The PoC's
    # decision rule is the share of placeholder cards a specifier replaced
    # (>=80% expand, <50% the specifier step is missing and this is a hold).
    auto = [r for r in rows if r.get("has_auto_points")]
    if auto:
        replaced = [r for r in auto if not r.get("points_auto")]
        print(f"  auto-points placeholder on {len(auto)} card(s); replaced by a real "
              f"estimate on {len(replaced)} = {len(replaced)/len(auto)*100:.0f}% "
              "(target >=80%)")
    if pointed:
        by_pts = {}
        for r in pointed:
            by_pts.setdefault(r["points"], []).append(r)
        print("    pts  n   mean $   est $   util   cycle")
        for pts in sorted(by_pts):
            g = by_pts[pts]
            mean = sum(x["actual_usd"] for x in g) / len(g)
            ests = [x["estimate_usd"] for x in g if x.get("estimate_usd")]
            est = (sum(ests) / len(ests)) if ests else None
            utils = [x["cap_basis_usd"] / x["cap_usd"] for x in g if x.get("cap_usd")]
            util = (sum(utils) / len(utils) * 100) if utils else None
            cyc = [(x["completed_at"] - x["created_at"]) / 60 for x in g if x.get("completed_at") and x.get("created_at")]
            cyc_m = (sorted(cyc)[len(cyc) // 2]) if cyc else None
            print(f"    {pts:3} {len(g):3}  {mean:7.4f}  {('%.2f' % est) if est else '   -'}   "
                  f"{('%3.0f%%' % util) if util is not None else '  - '}   {('%.0f min' % cyc_m) if cyc_m else '-'}")
    lat = [r["block_to_triage_s"] for r in rows if r.get("block_to_triage_s") is not None]
    if lat:
        lat.sort()
        print(f"\n  Block -> first triage action (charter §2 Responsiveness): median {lat[len(lat)//2]/60:.0f} min, "
              f"p90 {lat[int(len(lat)*0.9)]/60:.0f} min, over {len(lat)} blocked cards")
    ext = sum(r.get("extensions", 0) for r in rows)
    if ext:
        print(f"  cost-extensions granted: {ext} (each is a mis-estimate; check `under: F#` notes)")
    print("\n  Spend by profile (billed $; ModelArk shown as calls, it is a flat subscription):")
    byp, bym = {}, {}
    for r in rows:
        for p, v in (r.get("by_profile") or {}).items():
            byp[p] = byp.get(p, 0.0) + v
        for p, n in (r.get("modelark_calls_by_profile") or {}).items():
            bym[p] = bym.get(p, 0) + n
    for p in sorted(set(byp) | set(bym), key=lambda k: -byp.get(k, 0.0)):
        v = byp.get(p, 0.0)
        pct = f"({v/total*100:4.1f}%)" if total else ""
        print(f"    {p:9} ${v:8.4f}  {pct}" + (f"   + {BL.LABEL}: {bym[p]} calls" if bym.get(p) else ""))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    sys.exit(cmd_review(a.days) if a.review else cmd_record())
