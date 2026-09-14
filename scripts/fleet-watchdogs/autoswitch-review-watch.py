#!/usr/bin/env python3
"""autoswitch-review-watch.py — weekly nudge to review the waterfall and decide on auto-switch.

Richie, 2026-09-10: "report only, but remind me to review and enable a one click switch in two
weeks, and then every week ongoing until i act."

So: silent until 2026-09-24, then once a week until `state/waterfall-policy.json` says the mode
is no longer report_only. Acting on it — either enabling or explicitly declining — stops it.
Zero tokens.
"""
import json, os, subprocess, sys, time, datetime

H = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if os.path.basename(os.path.dirname(H)) == "profiles":
    H = os.path.dirname(os.path.dirname(H))
POLICY = os.path.join(H, "state", "waterfall-policy.json")
START = datetime.date(2026, 9, 24)          # two weeks from the decision

def main():
    if datetime.date.today() < START:
        return 0                             # silent until the review window opens
    mode = "report_only"
    try: mode = json.load(open(POLICY)).get("mode", "report_only")
    except OSError: pass
    if mode != "report_only":
        return 0                             # he acted; stop nagging

    lines = ["Waterfall review — still REPORT ONLY, your decision is outstanding.", ""]
    try:
        out = subprocess.run([sys.executable, os.path.join(H, "scripts", "waterfall-compare.py")],
                             capture_output=True, text=True, timeout=300).stdout
        verdicts = [l.strip() for l in out.splitlines()
                    if l.strip().startswith(("POST-PROMO:", "TODAY:", "INCUMBENT ON A PROMO", "!!"))]
        lines += (["What it would have changed this week:"] + ["  " + v for v in verdicts[:8]]
                  if verdicts else ["No change was indicated this week."])
    except Exception as e:
        lines.append(f"(compare failed: {e})")
    lines += ["",
              "Enable act-first with opt-out:  bash ~/.hermes/scripts/enable-autoswitch.sh",
              "Keep it report-only: reply and I will stop this reminder.",
              "Full report: ~/.hermes/state/waterfall.json"]
    msg = "\n".join(lines)
    try:
        subprocess.run([sys.executable, os.path.join(H, "scripts", "fleet_notify.py"),
                        "--text", msg], timeout=60)
    except Exception:
        print(msg)                            # cron captures stdout if delivery is unavailable
    return 0

if __name__ == "__main__":
    sys.exit(main())
