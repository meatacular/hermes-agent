#!/usr/bin/env python3
"""48h post-switch variance monitor for the price waterfall.

For each switch in state/waterfall-switches.json younger than 48h, compares the
billed $/M prompt tokens SINCE the switch against the pre-switch baseline
recorded by waterfall-apply.py. If the switch made things worse by more than
TOLERANCE — with enough traffic to be a real signal, not noise — it rolls the
change manifest back and marks the endpoint so the writer will not pick it again.

Runs hourly. Silent unless something happened.
"""
import json, os, subprocess, sys, time, datetime

H = os.path.expanduser("~/.hermes")
LEDGER  = os.path.join(H, "state", "waterfall-switches.json")
DENY    = os.path.join(H, "state", "waterfall-denylist.json")
ACTUALS = os.path.join(H, "logs", "price-actuals.jsonl")
ORACLE  = os.path.join(H, "scripts", "price-oracle.py")
CM      = os.path.join(H, "scripts", "change-manifest.py")
PY      = os.path.join(H, "hermes-agent", "venv", "bin", "python")

WINDOW_H  = 48
TOLERANCE = 1.15    # >15% worse than baseline = revert
MIN_TOK   = 200_000 # below this the sample is noise; wait for more traffic
CHECKS_H  = (6, 24, 48)

def load(p, d=None):
    try:
        with open(p) as fh: return json.load(fh)
    except Exception: return d

def observed(prof, model, since):
    """Billed $/M prompt for this profile+model from oracle records written since the switch."""
    spend = tok = 0.0
    try:
        with open(ACTUALS) as fh:
            for line in fh:
                try: r = json.loads(line)
                except Exception: continue
                if r.get("at", 0) < since or r.get("model") != model: continue
                bp = (r.get("by_profile") or {}).get(prof)
                if not bp: continue
                spend += bp.get("spend", 0.0)
                tok   += bp.get("fresh", 0) + bp.get("cached", 0)
    except FileNotFoundError:
        return None, 0
    if tok <= 0: return None, 0
    return spend / (tok / 1e6), int(tok)

def main():
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    led = load(LEDGER, {}) or {}
    if not led: return 0
    deny = load(DENY, {}) or {}
    out, dirty = [], False
    now = time.time()

    for prof, s in list(led.items()):
        age_h = (now - s["at"]) / 3600.0
        if age_h > WINDOW_H:
            if not s.get("closed"):
                s["closed"] = True; dirty = True
                out.append(f"{prof}: 48h window closed on {s['from']} -> {s['to']} — switch stands")
            continue
        due = [c for c in CHECKS_H if age_h >= c and c not in s.get("checks", [])]
        if not due: continue
        cp = max(due)

        obs, tok = observed(prof, s["model"], s["at"])
        s.setdefault("checks", []).extend(due); dirty = True
        if obs is None or tok < MIN_TOK:
            out.append(f"{prof}: {cp}h check — only {tok:,} prompt tokens since the switch, "
                       f"below the {MIN_TOK:,} signal floor. No verdict yet.")
            continue
        base = s["baseline_per_Mprompt"]
        ratio = obs / base if base else 1.0
        s.setdefault("observations", []).append(
            {"at_h": cp, "per_Mprompt": obs, "vs_baseline": ratio, "tokens": tok})
        verdict = f"{prof}: {cp}h check — ${obs:.3f}/M vs ${base:.3f}/M baseline ({ratio:.2f}x) on {tok:,} tokens"

        if ratio > TOLERANCE:
            out.append(verdict + f"  >> ADVERSE (>{TOLERANCE:.2f}x). Reverting.")
            if not dry:
                r = subprocess.run([PY, CM, "rollback", s["cid"]], capture_output=True, text=True)
                ok = r.returncode == 0
                out.append(f"   rollback {s['cid']}: {'OK' if ok else 'FAILED — ' + r.stderr.strip()[:200]}")
                if ok:
                    deny.setdefault(prof, [])
                    entry = {"slug": s["to_slug"], "model": s["model"], "at": now,
                             "reason": f"reverted at {cp}h: {ratio:.2f}x baseline"}
                    deny[prof].append(entry)
                    s["reverted"] = {"at": now, "at_h": cp, "ratio": ratio}
        else:
            out.append(verdict + "  ok")

    if dirty and not dry:
        json.dump(led, open(LEDGER, "w"), indent=1)
        json.dump(deny, open(DENY, "w"), indent=1)
    if out:
        print(f"waterfall-variance {datetime.datetime.now().isoformat(timespec='seconds')}"
              + ("  DRY-RUN" if dry else ""))
        for l in out: print("  " + l)
    return 0

if __name__ == "__main__":
    sys.exit(main())
