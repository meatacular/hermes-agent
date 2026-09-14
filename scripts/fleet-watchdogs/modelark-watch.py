#!/usr/bin/env python3
"""ModelArk Coding Plan watch — hourly, zero tokens, silent when healthy (2026-09-11).

Every DeepSeek slot now runs on a flat subscription with a quota that resets per cycle. When the
quota runs out, calls 429 and Hermes fails over to the configured fallback (m3 on OpenRouter) —
which IS billed. Nothing else would notice: the fleet keeps working, the bill quietly moves. This
reports, for the last hour: ModelArk calls (usage ledgers), failovers off ModelArk and auth/quota
errors (agent logs). It speaks only when failovers >= FAILOVER_ALERT, or on any auth error (a
rotated/removed key: re-run scripts/modelark-key-sync.sh, restart gateways). Exit 0 always.
"""
import json, os, re, sqlite3, sys, time
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if H.parent.name == "profiles":
    H = H.parent.parent
STATE = H / "state" / "modelark-watch.json"
FAILOVER_ALERT = int(os.environ.get("MODELARK_FAILOVER_ALERT", "5"))
ARK = "ark.ap-southeast.bytepluses.com"
ARK_MODEL = re.compile(r"deepseek-v4-(?:flash|pro)-(?:ga-)?26\d{4}")
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def ledger_calls(since):
    n = 0
    for db in [H / "state.db", *sorted((H / "profiles").glob("*/state.db"))]:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            n += con.execute("SELECT COALESCE(SUM(api_call_count),0) FROM session_model_usage "
                             "WHERE billing_base_url LIKE ? AND last_seen > ?", (f"%{ARK}%", since)).fetchone()[0]
            con.close()
        except sqlite3.Error:
            continue
    return n


def log_events(since):
    failovers, quota, auth = {}, 0, 0
    logs = [H / "logs" / "agent.log", *sorted((H / "profiles").glob("*/logs/agent.log"))]
    for f in logs:
        prof = "root" if f.parent.parent == H else f.parent.parent.name
        try:
            size = f.stat().st_size
            with open(f, errors="replace") as fh:
                fh.seek(max(0, size - 4_000_000))
                for line in fh:
                    m = TS.match(line)
                    if not m or time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) < since:
                        continue
                    if "Fallback activated:" in line and ARK_MODEL.search(line.split("→")[0]):
                        failovers[prof] = failovers.get(prof, 0) + 1
                    elif "API call failed" in line and ARK in line:
                        if re.search(r"RateLimit|429|quota", line, re.I): quota += 1
                        elif re.search(r"Authentication|401|403", line): auth += 1
        except OSError:
            continue
    return failovers, quota, auth


def main():
    since = time.time() - 3600
    calls = ledger_calls(since)
    failovers, quota, auth = log_events(since)
    nfo = sum(failovers.values())
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"at": int(time.time()), "calls_1h": calls, "failovers_1h": failovers,
                                 "quota_errors_1h": quota, "auth_errors_1h": auth}))
    if auth:
        print(f"*ModelArk: {auth} auth error(s) in the last hour.* The Coding Plan key may be rotated or "
              f"missing — `scripts/modelark-key-sync.sh`, then restart the gateways. Calls are failing over "
              f"to the billed fallback meanwhile ({nfo} failover(s)).")
    elif nfo >= FAILOVER_ALERT:
        where = ", ".join(f"{p} {n}" for p, n in sorted(failovers.items(), key=lambda kv: -kv[1]))
        print(f"*ModelArk failing over:* {nfo} failover(s) off the Coding Plan in the last hour ({where}); "
              f"{quota} quota/429 error(s), {calls} ModelArk call(s) recorded. Likely the plan's quota cycle is "
              f"exhausted — those calls now run on the fallback (minimax-m3 via OpenRouter) and are billed "
              f"until the quota restores.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"modelark-watch error: {e}", file=sys.stderr); sys.exit(0)
