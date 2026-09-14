#!/usr/bin/env python3
"""waterfall-compare.py — rank capability-passing endpoints per profile. REPORT ONLY.

Never edits config. Nothing auto-switches until the weekly counterfactual has shown the ranking
is right (Richie, 2026-09-09).

THREE RULES, each learned the hard way:

1. RANK ON OUR OWN BILLED HISTORY, NOT THE PRICE LIST. Per-axis token rates are NOT identifiable
   from usage data (three unknowns, one number per row, near-collinear columns: two methods both
   fit at 0.0% and disagree 4x on the cache axis). A candidate is scored on cost-at-our-mix, and
   one we have never billed is a PROBE, never a switch.

2. RANK ON THE POST-PROMO PRICE, NOT TODAY'S. Measured 2026-09-10: v4-pro repriced across the
   market overnight — Baidu 1.320 -> 0.579, which reads as a 56% win until you see
   `discount: 0.561` and realise it reverts. Alibaba (0.581) and DeepSeek (0.660) carry
   `discount: 0` and are genuinely that price. Ranking on today's number chases a promo and then
   sits on the most expensive host the moment it lapses. Every candidate is scored twice:
   `now` and `post_promo` = price / (1 - discount).

3. A PROMO END DATE IS NOT KNOWABLE. `promotion_message` has already been wrong once —
   glm-5.3-flash was still at its promo rate 12h past its stated 16:00 UTC expiry. So this never
   schedules a switch for a date. It reports which incumbents ride a promo and where they should
   go when it ends; the daily snapshot detects the revert.

Capability gate: tool support, uptime >= 99%, not on the profile's `ignore` list.
"""
import glob, json, os, re, sys, time

H = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if os.path.basename(os.path.dirname(H)) == "profiles":
    H = os.path.dirname(os.path.dirname(H))
SNAP = os.path.join(H, "state", "or-prices.json")
ACTUALS = os.path.join(H, "logs", "price-actuals.jsonl")
OUT = os.path.join(H, "state", "waterfall.json")
QUALIFY = 0.15
MIN_UPTIME = 99.0

def _list_under(block, key):
    m = re.search(r"^\s+%s:\s*$" % key, block, re.M)
    if not m: return []
    rest = block[m.end():]
    out = []
    for line in rest.splitlines():
        s = line.strip()
        if s.startswith("- "): out.append(s[2:].strip())
        elif s: break
    return out

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import billing_labels as BL  # noqa: E402 — ModelArk slots are a flat subscription, never ranked

def profiles():
    out = {}
    items = [("root", os.path.join(H, "config.yaml"))] + [
        (os.path.basename(os.path.dirname(x)), x)
        for x in sorted(glob.glob(os.path.join(H, "profiles", "*", "config.yaml")))]
    for name, path in items:
        try: txt = open(path).read()
        except OSError: continue
        mm = re.search(r"^model:\s*$(.*?)(?=^\S)", txt, re.S | re.M)
        dm = re.search(r"^\s+default:\s*(\S+)", mm.group(1), re.M) if mm else None
        pm = re.search(r"^provider_routing:\s*$(.*?)(?=^\S)", txt, re.S | re.M)
        blk = pm.group(1) if pm else ""
        if dm:
            out[name] = {"model": dm.group(1), "order": _list_under(blk, "order"),
                         "ignore": _list_under(blk, "ignore")}
    return out

WINDOW_DAYS = int(os.environ.get("WATERFALL_WINDOW_DAYS", "7"))

def measured():
    """Latest record per model+provider, restricted to a CONSISTENT window.

    price-actuals.jsonl interleaves runs of different --days. Mixing them made a
    1-day probe run silently reset every profile's spend to a day's worth and
    pushed all of them under the writer's churn floor. Pin the window."""
    best = {}
    try:
        for line in open(ACTUALS):
            try: r = json.loads(line)
            except Exception: continue
            if r.get("window_days") != WINDOW_DAYS: continue
            k = (r["model"], r["provider"])
            t = r.get("tokens") or {}
            tot = t.get("fresh", 0) + t.get("cached", 0)
            if k not in best or r["at"] > best[k]["at"]:
                prev = best.get(k, {}).get("_prompt_tokens", 0)
                best[k] = r; best[k]["_prompt_tokens"] = prev + tot
            else:
                best[k]["_prompt_tokens"] = best[k].get("_prompt_tokens", 0) + tot
    except OSError: pass
    return best

def norm(s): return (s or "").lower().replace(".", "-").replace(" ", "-")

def slug(tag, provider_name):
    """The join key for order/ignore lists. Those carry OpenRouter SLUGS ("open-inference"),
    while provider_name is a display string ("OpenInference") that normalises to something
    different and silently fails to match — the same family as the documented z.ai/z-ai trap.
    The slug is the tag's leading segment ("open-inference/fp8" -> "open-inference")."""
    if tag and "/" in tag: return norm(tag.split("/")[0])
    return norm(tag or provider_name)

def main():
    if not os.path.exists(SNAP):
        print("no price snapshot — run or-price-snapshot.py first", file=sys.stderr); return 2
    snap = json.load(open(SNAP)); meas = measured(); profs = profiles()
    report = {"at": time.time(), "profiles": {}}
    print("waterfall-compare — REPORT ONLY, nothing is changed\n")
    report["subscription"] = {}

    for prof, cfg in sorted(profs.items()):
        model = cfg["model"]
        if BL.is_modelark(model):
            report["subscription"][prof] = {"model": model, "billing": BL.LABEL}
            print(f"{prof}: {model} — {BL.LABEL} (flat ModelArk Coding Plan): not ranked against OpenRouter hosts\n")
            continue
        own = [r for (m, p), r in meas.items() if m == model]
        if not own: continue
        F = C = O = 0; spend = 0.0; shared = False
        for r in own:
            bp = (r.get("by_profile") or {}).get(prof)
            if bp:
                F += bp["fresh"]; C += bp["cached"]; O += bp["output"]; spend += bp["spend"]
            elif prof in (r.get("profiles") or []):
                shared = True
                F += r["tokens"]["fresh"]; C += r["tokens"]["cached"]
                O += r["tokens"]["output"]; spend += r["spend"]
        if not (F + C): continue
        inc_prov = max(own, key=lambda r: r["spend"])["provider"]
        eps = (snap.get("models", {}).get(model) or {}).get("endpoints", {})
        ignore = {norm(x) for x in cfg["ignore"]}
        cands = []
        for tag, e in eps.items():
            if e.get("in") is None or e.get("out") is None: continue
            if not e.get("tools"): continue
            if (e.get("uptime") or 0) < MIN_UPTIME: continue
            if slug(tag, e.get("provider_name")) in ignore or norm(e.get("provider_name")) in ignore:
                continue
            cr = e.get("cache_read"); cr = e["in"] if cr is None else cr
            now = (F * e["in"] + C * cr + O * e["out"]) / 1e6
            disc = e.get("discount") or 0
            post = now / ((1 - disc) if 0 < disc < 1 else 1.0)
            cands.append({"tag": tag, "provider": e.get("provider_name"), "now": now,
                          "post_promo": post, "discount": disc,
                          "measured": (model, e.get("provider_name")) in meas,
                          "measured_tokens": (meas.get((model, e.get("provider_name"))) or {})
                                             .get("_prompt_tokens", 0),
                          "uptime": e.get("uptime"), "quant": e.get("quant")})
        if not cands: continue
        cands.sort(key=lambda c: c["post_promo"])
        inc = next((c for c in cands if norm(c["provider"]) == norm(inc_prov)), None)
        best_now = min(cands, key=lambda c: c["now"])
        best_post = cands[0]

        print(f"## {prof}  {model}")
        note = "  (SHARED mix — oracle has no per-profile split for this pair)" if shared else ""
        print(f"   billed ${spend:.2f} on {F+C:,} prompt tokens, incumbent {inc_prov}{note}")
        print(f"   {'endpoint':<26}{'@our mix':>10}{'post-promo':>12}{'disc':>7}{'billed?':>9}")
        show = cands[:5]
        if inc and inc not in show: show = show + [inc]      # the incumbent is always shown
        for c in show:
            star = "  <- incumbent" if inc and c["tag"] == inc["tag"] else ""
            print(f"   {c['tag'][:25]:<26}{c['now']:>10.2f}{c['post_promo']:>12.2f}"
                  f"{(c['discount'] or 0):>7}{('yes' if c['measured'] else 'PROBE'):>9}{star}")
        if inc:
            print(f"   {'(actually billed)':<26}{spend:>10.2f}"
                  f"{'':>12}{'':>7}{'':>9}   <- reality; ranking above is listed-vs-listed")
        v = []
        if inc and inc["discount"]:
            v.append(f"INCUMBENT ON A PROMO ({inc['discount']:.0%}): reverts to ${inc['post_promo']:.2f}. "
                     f"Best non-promo is {best_post['tag']} at ${best_post['post_promo']:.2f}.")
        if inc and best_post["tag"] != inc["tag"]:
            g = 1 - best_post["post_promo"] / inc["post_promo"]
            if g >= QUALIFY:
                v.append(f"POST-PROMO: {best_post['tag']} is {g:.0%} better — "
                         + ("QUALIFIES" if best_post["measured"] else "PROBE FIRST, never billed there"))
                if best_post["now"] > spend * 1.02:
                    v.append(f"   !! BUT it costs ${best_post['now']:.2f} at our mix against the "
                             f"${spend:.2f} we ACTUALLY pay — the incumbent bills below its listed "
                             f"rate, so this 'saving' is a price rise today. Do not switch on it.")
        if inc and best_now["tag"] != inc["tag"]:
            g2 = 1 - best_now["now"] / inc["now"]
            if g2 >= QUALIFY:
                v.append(f"TODAY: {best_now['tag']} is {g2:.0%} cheaper right now")
        print("   " + ("\n   ".join(v) if v else "no change indicated"))
        print()
        # None = unknown (records predate the flag itself), False = known contaminated
        flags = [r.get("provider_attribution_trusted") for r in own]
        trusted = None if any(f is None for f in flags) else all(flags)
        report["profiles"][prof] = {"model": model, "incumbent": inc_prov, "spend": spend,
                                    "provider_attribution_trusted": trusted,
                                    "rows_pre_trust_epoch": sum(
                                        r.get("rows_pre_trust_epoch") or 0 for r in own),
                                    "mix": {"fresh": F, "cached": C, "output": O},
                                    "candidates": (cands[:8] + [inc] if inc and inc not in cands[:8] else cands[:8]),
                                    "verdict": v}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(report, open(OUT, "w"), indent=1)
    # append-only history so waterfall-counterfactual.py can replay the
    # recommendation stream later. One line per run.
    hist = os.path.join(H, "logs", "waterfall-history.jsonl")
    os.makedirs(os.path.dirname(hist), exist_ok=True)
    with open(hist, "a") as fh:
        fh.write(json.dumps(report) + "\n")
    print(f"written: {OUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
