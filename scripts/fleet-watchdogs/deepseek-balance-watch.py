#!/usr/bin/env python3
"""deepseek-balance-watch — the ACTUAL cost of the DeepSeek rung, not our estimate of it.

Richie, 2026-09-15: "deepseek api is not subscription, it costs — the dash should not report or
monitor as sub. Is it possible to use the deepseek api to report costs? Do you need another
management key to do so?"

Answers, both measured:

  * **No second key.** The same `DEEPSEEK_API_KEY` reads `GET /user/balance`. Verified 2026-09-15.
  * **Yes, costs are reportable** — as a balance DELTA, not per call. DeepSeek returns no cost in
    the chat-completions response, which is why Hermes prices it from the published rate card and
    records `cost_status='estimated'`, `actual_cost_usd=0`. The balance is the invoice side, and
    it moves: $22.00 -> $21.97 across the first probes.

So the estimate is what every per-card control has to use (it is the only per-call number), and
this watchdog supplies the truth to check it against. A drift beyond the tolerance means the rate
card has moved — most likely peak vs off-peak, which is a 2x step on this provider (peak is
Mon-Fri 01:00-04:00 and 06:00-10:00 UTC) — and the published rates in fleet/models.yaml need
re-reading.

Exit is ALWAYS 0. A finding is not a failure; a non-zero exit every tick grows failure_streak
until the scheduler disables the job.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.request

H = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
if H.parent.name == "profiles":
    H = H.parent.parent
STATE = H / "state" / "deepseek-balance.json"
LEDGER = H / "logs" / "deepseek-balance.jsonl"
BASE = "https://api.deepseek.com"
DRIFT_TOLERANCE = 0.25          # 25% — below a peak/off-peak step, above ordinary rounding
MIN_SPEND_TO_JUDGE = 0.02       # do not cry drift over fractions of a cent


def _key() -> str:
    for p in (H / ".env",):
        try:
            for line in p.read_text().splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def balance(key: str):
    req = urllib.request.Request(BASE + "/user/balance")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    infos = d.get("balance_infos") or []
    usd = next((i for i in infos if (i.get("currency") or "").upper() == "USD"), None)
    if not usd:
        return None, d
    return float(usd["total_balance"]), d


def estimated_since(ts: float) -> float:
    """What Hermes THINKS the DeepSeek rung cost since ``ts``."""
    total = 0.0
    for db in [H / "state.db"] + sorted((H / "profiles").glob("*/state.db")):
        if not db.is_file():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            row = c.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd),0) FROM session_model_usage "
                "WHERE last_seen > ? AND COALESCE(billing_provider,'') = 'deepseek'", (ts,)
            ).fetchone()
            total += float(row[0] or 0.0)
            c.close()
        except sqlite3.Error:
            continue
    return total


def main() -> int:
    key = _key()
    if not key:
        print("deepseek-balance-watch: no DEEPSEEK_API_KEY in the fleet .env — nothing to read",
              file=sys.stderr)
        return 0
    try:
        bal, raw = balance(key)
    except Exception as exc:  # noqa: BLE001 — a balance read must never fail a tick
        print(f"deepseek-balance-watch: balance unreadable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0
    if bal is None:
        print("deepseek-balance-watch: no USD balance in the response", file=sys.stderr)
        return 0

    now = time.time()
    prev = {}
    try:
        prev = json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        pass

    out = {"at": now, "balance_usd": bal, "available": bool(raw.get("is_available"))}
    lines = []

    if prev.get("balance_usd") is not None:
        spent = round(float(prev["balance_usd"]) - bal, 6)
        est = round(estimated_since(float(prev.get("at") or 0)), 6)
        out.update({"window_spent_usd": spent, "window_estimated_usd": est,
                    "since": prev.get("at")})
        # cumulative, so the dashboard and the weekly review have a real number to quote
        out["cumulative_spent_usd"] = round(float(prev.get("cumulative_spent_usd") or 0) + max(spent, 0.0), 6)
        try:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with open(LEDGER, "a") as f:
                f.write(json.dumps({"at": now, "balance_usd": bal, "spent_usd": spent,
                                    "estimated_usd": est}) + "\n")
        except OSError:
            pass
        if spent >= MIN_SPEND_TO_JUDGE and est > 0:
            ratio = spent / est
            if abs(ratio - 1.0) > DRIFT_TOLERANCE:
                lines.append(
                    f"DEEPSEEK COST DRIFT — billed ${spent:.4f} against an estimate of ${est:.4f} "
                    f"({ratio:.2f}x) since {time.strftime('%H:%M', time.localtime(float(prev['at'])))}."
                )
                lines.append("  The per-call number Hermes records is an ESTIMATE from the published "
                             "rate card; this is the invoice side. A ~2x step is peak pricing "
                             "(Mon-Fri 01:00-04:00 and 06:00-10:00 UTC); anything else means the "
                             "rates in fleet/models.yaml need re-reading.")
    else:
        out["cumulative_spent_usd"] = 0.0
        lines.append(f"DEEPSEEK BALANCE baseline set: ${bal:.2f}. Spend is reported from the next tick.")

    if bal < 5.0:
        lines.append(f"DEEPSEEK BALANCE LOW — ${bal:.2f} left. The rung fails over to OpenRouter when it empties.")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(out, indent=1))
    if lines:
        print("\n".join(lines), file=sys.stderr)
        print(json.dumps({"watchdog": "deepseek-balance-watch", **{k: out[k] for k in
                          ("balance_usd", "window_spent_usd", "window_estimated_usd")
                          if k in out}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
