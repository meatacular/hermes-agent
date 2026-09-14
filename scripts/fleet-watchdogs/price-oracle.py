#!/usr/bin/env python3
"""price-oracle.py — what the fleet is ACTUALLY billed per million tokens.

THE RULE THIS EXISTS TO ENFORCE (measured 2026-09-09): OpenRouter's published cache-read rates
are wrong for five of six fleet workloads, by 2x and more. Baidu bills v4-flash cache at
~$0.0063/M against a listed $0.0140. A ranker sorting on the published number would have moved
three profiles chasing a saving that does not exist. So: rank on our own invoices. The price list
is a trigger to re-measure, never a reason to switch.

METHOD, AND ITS LIMIT — READ THIS BEFORE QUOTING A PER-AXIS RATE.
Each usage row gives fresh/cached/output tokens and one billed cost: one equation, three unknowns.
Across many rows the mix varies, so least squares can fit all three — but on this fleet the three
columns are strongly correlated (cache ratio barely moves within a workload), so the system is
**near-degenerate**: many (in, cache, out) triples explain the same spend to 0.0%. Measured
2026-09-09 on v4-pro @ Baidu, two methods both fitting perfectly and disagreeing 4x on the cache
axis (0.0237 fixing listed in/out, 0.1016 by least squares); three other groups solved to a
NEGATIVE cache rate, which is physically impossible. **A 0.0% fit error here is evidence of
degeneracy, not of accuracy.**

So the per-axis rates are reported but flagged `identifiable`, and the RANKING METRIC is the pair
that is robust regardless of how cost splits across axes:
  * `effective_per_Mprompt` — billed $ per million prompt tokens at the workload's own cache mix
  * `vs_listed` — measured spend / spend predicted from the published rates on the SAME mix
`vs_listed` is the honest form of "is the price list right for us": it needs no separation of
axes, and it is what the waterfall compares. Only ever rank on that.

Writes  ~/.hermes/logs/price-actuals.jsonl   (one record per model+provider per run)
Prints  a report; --quiet prints only failures. Zero tokens, read-only.
"""
import argparse, glob, json, math, os, sqlite3, sys, time

H = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if os.path.basename(os.path.dirname(H)) == "profiles":
    H = os.path.dirname(os.path.dirname(H))
OUT = os.path.join(H, "logs", "price-actuals.jsonl")
SNAP = os.path.join(H, "state", "or-prices.json")

def ledgers():
    out = [(os.path.join(H, "state.db"), "root")]
    for p in sorted(glob.glob(os.path.join(H, "profiles", "*", "state.db"))):
        out.append((p, os.path.basename(os.path.dirname(p))))
    return [(p, n) for p, n in out if os.path.exists(p)]

# ModelArk (2026-09-11): the Coding Plan is a flat subscription, not an OpenRouter host. Its rows have
# no upstream provider_name and only a NOTIONAL cost, so they are neither "unattributed" OpenRouter
# calls nor spend to fit rates against — they are counted separately as "modelark subscription".
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import billing_labels as BL  # noqa: E402
MODELARK = {"calls": 0, "tokens": 0}

def collect(days):
    cut = time.time() - days * 86400
    rows, unattributed, total_calls = {}, 0, 0
    for db, prof in ledgers():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = c.execute("""
                SELECT model, COALESCE(NULLIF(provider_name,''),'') , COALESCE(task,'-'),
                       COALESCE(native_tokens_prompt, input_tokens, 0),
                       COALESCE(native_tokens_cached, cache_read_tokens, 0),
                       COALESCE(output_tokens, 0), COALESCE(cache_write_tokens, 0),
                       COALESCE(total_cost, actual_cost_usd, 0),
                       COALESCE(api_call_count, 0), last_seen, billing_base_url, cost_source
                FROM session_model_usage WHERE last_seen > ?""", (cut,))
        except sqlite3.Error as e:
            print(f"  !! {prof}: {e}", file=sys.stderr); continue
        for model, prov, task, pt, ct, ot, wt, cost, n, last_seen, base, src in cur:
            if BL.is_modelark(model, base, src):
                MODELARK["calls"] += n or 0; MODELARK["tokens"] += (pt or 0) + (ot or 0)
                continue
            total_calls += n or 0
            if not prov:
                unattributed += n or 0
                continue                      # excluded AND counted, never averaged in
            fresh = max((pt or 0) - (ct or 0), 0)
            if cost and cost > 0 and (fresh or ct or ot):
                rows.setdefault((model, prov), []).append(
                    (fresh, ct or 0, ot or 0, float(cost), prof, task, n or 0, wt or 0,
                     float(last_seen or 0)))
    return rows, unattributed, total_calls

def solve3(A, b):
    """Least squares via normal equations (A'A)x = A'b, 3x3 Gaussian elimination. stdlib only."""
    M = [[sum(A[k][i] * A[k][j] for k in range(len(A))) for j in range(3)] + [
          sum(A[k][i] * b[k] for k in range(len(A)))] for i in range(3)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(M[r][i]))
        if abs(M[p][i]) < 1e-12: return None
        M[i], M[p] = M[p], M[i]
        for r in range(3):
            if r == i: continue
            f = M[r][i] / M[i][i]
            for cc in range(i, 4): M[r][cc] -= f * M[i][cc]
    return [M[i][3] / M[i][i] for i in range(3)]

# Provider attribution was unreliable before the seam fix landed (card t_37ce064c,
# merged 2026-09-09). Rows written before this instant carry provider_name values
# that do not match who actually served the call: deepseek-v4-flash rows labelled
# "Baidu" price at 0.22-0.49x Baidu's listed rate on 05-08 Sep and at EXACTLY 1.000x
# on 10 Sep. The old rows are cheap providers' traffic wearing Baidu's name.
# Anything derived from provider-level billing before this is not evidence.
TRUST_EPOCH = float(os.environ.get("ORACLE_TRUST_EPOCH", "1788998400"))  # 2026-09-10 00:00 NZT

COND_MAX = 30.0   # on column-normalised A. Above this the three rates trade off freely.

def cond3(A):
    """Condition number of A with its columns normalised to unit length.

    The point of normalising is that we are testing COLLINEARITY, not units: raw
    columns differ by orders of magnitude (millions of cached tokens against
    hundreds of output tokens) and an unnormalised condition number just measures
    that scale gap. Returns None when a column is empty.

    This is the check the old `identifiable` flag was missing. That flag tested
    positivity, solver method and row count — none of which say anything about
    whether the columns are distinguishable — and `pivot < 1e-12` on the normal
    equations only catches near-exact singularity, since forming A'A squares the
    conditioning (cond ~1e5 in A becomes ~1e10 in A'A and still pivots fine).
    """
    n = len(A)
    if n < 3: return None
    norms = [math.sqrt(sum(A[k][j] ** 2 for k in range(n))) for j in range(3)]
    if any(v <= 0 for v in norms): return None
    # Gram matrix of the normalised columns == correlation-like matrix, symmetric 3x3
    G = [[sum(A[k][i] * A[k][j] for k in range(n)) / (norms[i] * norms[j])
          for j in range(3)] for i in range(3)]
    # closed-form symmetric 3x3 eigenvalues (Smith 1961)
    q = (G[0][0] + G[1][1] + G[2][2]) / 3.0
    p2 = sum((G[i][i] - q) ** 2 for i in range(3)) + 2 * (G[0][1]**2 + G[0][2]**2 + G[1][2]**2)
    p = math.sqrt(max(p2 / 6.0, 0.0))
    if p == 0: return 1.0                      # G is a multiple of the identity
    B = [[(G[i][j] - (q if i == j else 0)) / p for j in range(3)] for i in range(3)]
    detB = (B[0][0] * (B[1][1] * B[2][2] - B[1][2] * B[2][1])
            - B[0][1] * (B[1][0] * B[2][2] - B[1][2] * B[2][0])
            + B[0][2] * (B[1][0] * B[2][1] - B[1][1] * B[2][0]))
    phi = math.acos(max(-1.0, min(1.0, detB / 2.0))) / 3.0
    e1 = q + 2 * p * math.cos(phi)
    e3 = q + 2 * p * math.cos(phi + 2 * math.pi / 3)
    e2 = 3 * q - e1 - e3
    lo = max(min(e1, e2, e3), 1e-15); hi = max(e1, e2, e3)
    return math.sqrt(hi / lo)                  # cond(A) = sqrt(cond(A'A))

def listed(snapshot, model, provider):
    """Return (rate_dict, ambiguous, n_candidates).

    The ledger records `provider_name` ("OpenAI"), not the endpoint tag, so a provider selling
    several service tiers under one name cannot be resolved from usage data alone. Picking one
    silently mis-prices the workload by 2-4x — that is exactly how a 1.18x reading looked like
    2.39x on 2026-09-09. When there are several, price against the CHEAPEST (the optimistic
    bound) and flag it, so `vs_listed > 1` on an ambiguous row is read as "at most this bad"."""
    m = (snapshot.get("models") or {}).get(model) or {}
    norm = lambda s: s.lower().replace(".", "-")
    cands = [v for tag, v in (m.get("endpoints") or {}).items()
             if norm(v.get("provider_name", tag)) == norm(provider)]
    if not cands:
        return None, False, 0
    cands.sort(key=lambda v: (v.get("in") if v.get("in") is not None else 9e9))
    return cands[0], len(cands) > 1, len(cands)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-rows", type=int, default=6)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()

    snapshot = {}
    if os.path.exists(SNAP):
        try: snapshot = json.load(open(SNAP))
        except Exception: pass

    groups, unattributed, total_calls = collect(a.days)
    recs, worst_err, best_err = [], 0.0, None
    for (model, prov), rs in sorted(groups.items(), key=lambda kv: -sum(r[3] for kv2 in [kv] for r in kv[1])):
        F = [r[0] for r in rs]; C = [r[1] for r in rs]; O = [r[2] for r in rs]
        b = [r[3] for r in rs]; spend = sum(b)
        lst, lst_ambig, lst_n = listed(snapshot, model, prov)
        method, x = None, None
        if len(rs) >= a.min_rows:
            A = [[F[i] / 1e6, C[i] / 1e6, O[i] / 1e6] for i in range(len(rs))]
            x = solve3(A, b)
            if x and all(v >= -1e-9 for v in x):
                method = "least_squares"; x = [max(v, 0.0) for v in x]
            else:
                x = None
        if x is None and lst and lst.get("in") is not None and lst.get("out") is not None:
            # fallback: trust the listed fresh+output rates, solve cache analytically
            noncache = sum(F[i] * lst["in"] + O[i] * lst["out"] for i in range(len(rs))) / 1e6
            tc = sum(C)
            x = [lst["in"], ((spend - noncache) / tc * 1e6) if tc else 0.0, lst["out"]]
            method = "listed_in_out_solve_cache"
        if x is None:
            continue
        pred = sum(F[i] * x[0] + C[i] * x[1] + O[i] * x[2] for i in range(len(rs))) / 1e6
        err = abs(pred - spend) / spend * 100 if spend else 0.0
        # --- the robust metrics: independent of how cost splits across the three axes ---
        prompt_tok = sum(F) + sum(C)
        eff_prompt = spend / prompt_tok * 1e6 if prompt_tok else None
        eff_all = spend / (prompt_tok + sum(O)) * 1e6 if (prompt_tok + sum(O)) else None
        vs_listed = None
        W = [r[7] for r in rs]
        if lst and lst.get("in") is not None and lst.get("out") is not None:
            cr = lst.get("cache_read")
            cr = lst["in"] if cr is None else cr      # no cache price published => full input rate
            cw = lst.get("cache_write") or 0.0        # luna: $0.25/M, HIGHER than its input rate
            pred_listed = (sum(F) * lst["in"] + sum(C) * cr + sum(O) * lst["out"]
                           + sum(W) * cw) / 1e6
            if pred_listed > 0: vs_listed = spend / pred_listed
            rec_extra = {"cache_write_tokens": sum(W), "listed_cache_write": cw}
        # a rate below zero is impossible; near-degenerate columns are why
        cond = cond3([[r[0], r[1], r[2]] for r in rs])
        # WHY a row is not identifiable matters, and the three reasons mean
        # completely different things:
        #   collinear      -> the data cannot separate the axes (more data helps)
        #   negative_rate  -> a well-conditioned solve produced an impossible rate,
        #                     which means the THREE-COLUMN MODEL IS WRONG for this
        #                     group: some cost component (cache writes, a tier
        #                     discount, a promo) is not in the design at all.
        #                     More data will NOT fix this.
        #   thin           -> too few rows to solve at all
        reasons = []
        if not x: reasons.append("no_solution")
        if x and not all(v >= 0 for v in x): reasons.append("negative_rate")
        if len(rs) < 12: reasons.append("thin")
        if method != "least_squares": reasons.append("method:" + method)
        if cond is None: reasons.append("cond_unavailable")
        elif cond >= COND_MAX: reasons.append("collinear")
        identifiable = not reasons
        worst_err = max(worst_err, err); best_err = err if best_err is None else min(best_err, err)
        rec = {"at": time.time(), "window_days": a.days, "model": model, "provider": prov,
               "rows": len(rs), "calls": sum(r[6] for r in rs), "spend": round(spend, 6),
               "tokens": {"fresh": sum(F), "cached": sum(C), "output": sum(O),
                          "cache_write": sum(r[7] for r in rs)},
               "cache_pct": round(100 * sum(C) / max(sum(F) + sum(C), 1), 1),
               "effective_per_Mprompt": eff_prompt, "effective_per_Mtoken": eff_all,
               "vs_listed": (round(vs_listed, 3) if vs_listed is not None else None),
               # per_axis is published ONLY when identifiable. Persisting a
               # degenerate solve alongside a flag meant a consumer reading the
               # field got a number the code itself could not vouch for.
               "per_axis": ({"in": x[0], "cache_read": x[1], "out": x[2]}
                            if identifiable else None),
               "per_axis_withheld": (None if identifiable else
                                     {"reason": ",".join(reasons),
                                      "raw": ({"in": x[0], "cache_read": x[1], "out": x[2]}
                                              if x else None)}),
               "identifiable": identifiable,
               # provider attribution trust: see TRUST_EPOCH above
               "rows_pre_trust_epoch": sum(1 for r in rs if len(r) > 8 and r[8] < TRUST_EPOCH),
               "provider_attribution_trusted": all(
                   len(r) > 8 and r[8] >= TRUST_EPOCH for r in rs),
               "trust_epoch": TRUST_EPOCH,
               "condition_number": (round(cond, 1) if cond is not None else None),
               "not_identifiable_because": reasons or None,
               "condition_max": COND_MAX,
               "method": method, "fit_error_pct": round(err, 2),
               "profiles": sorted({r[4] for r in rs}),
               # per-profile split: without it, four profiles sharing one model+provider each
               # read back the COMBINED mix and every one of them looks like the whole fleet.
               "by_profile": {pf: {"fresh": sum(r[0] for r in rs if r[4] == pf),
                                   "cached": sum(r[1] for r in rs if r[4] == pf),
                                   "output": sum(r[2] for r in rs if r[4] == pf),
                                   "spend": round(sum(r[3] for r in rs if r[4] == pf), 6),
                                   "calls": sum(r[6] for r in rs if r[4] == pf)}
                              for pf in sorted({r[4] for r in rs})}}
        if lst:
            rec["listed"] = {k: lst.get(k) for k in ("in", "cache_read", "out", "cache_write", "discount")}
            rec["listed_ambiguous"] = lst_ambig
            rec["listed_candidates"] = lst_n
            if identifiable:
                for k, idx in (("in", 0), ("cache_read", 1), ("out", 2)):
                    lv = lst.get(k)
                    if lv: rec.setdefault("ratio", {})[k] = round(x[idx] / lv, 3)
        recs.append(rec)

    if not a.no_write and recs:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "a") as fh:
            for r in recs: fh.write(json.dumps(r) + "\n")

    if not a.quiet:
        print(f"price-oracle: {a.days}d, {len(recs)} model+provider groups, "
              f"${sum(r['spend'] for r in recs):.2f} attributed")
        pct = 100 * unattributed / total_calls if total_calls else 0
        print(f"  unattributed: {unattributed}/{total_calls} calls ({pct:.1f}%) — EXCLUDED, not averaged in")
        if MODELARK["calls"]:
            print(f"  {BL.LABEL}: {MODELARK['calls']} calls, {MODELARK['tokens']/1e6:.2f}M tokens — flat plan, "
                  "not an OpenRouter host, not fitted (not counted above)")
        print(f"  {'model @ provider':<44}{'spend':>8}{'cache%':>7}{'$/M prompt':>12}{'vs listed':>11}  axes?")
        for r in sorted(recs, key=lambda x: -x["spend"])[:18]:
            vl = r.get("vs_listed")
            vls = (f"{vl:.2f}x" + ("?" if r.get("listed_ambiguous") else "")) if vl is not None else "-"
            cn = r.get("condition_number")
            flag = "ok" if r["identifiable"] else "DEGENERATE"
            if not r["identifiable"]:
                flag = "DEGENERATE:" + ",".join(r.get("not_identifiable_because") or [])
            if cn is not None: flag += f" cond{cn:.0f}"
            ep = r.get("effective_per_Mprompt")
            print(f"  {(r['model']+' @ '+r['provider'])[:43]:<44}{r['spend']:>8.2f}{r['cache_pct']:>7.1f}"
                  f"{(ep if ep else 0):>12.4f}{vls:>11}  {flag}")
        print("\n  vs listed <1.00 = we are billed LESS than the published rates predict for this mix.")
        print("  A trailing '?' = that provider sells several service tiers under one name and the")
        print("  ledger records only the name, so this is priced against the CHEAPEST tier: read it")
        print("  as an upper bound on the ratio, not a measurement.")
        print("  'DEGENERATE' = the three per-axis rates are not separately identifiable from this")
        print("  data; the spend and $/M-prompt figures are still exact. per_axis is now WITHHELD")
        print("  (null) on these rows rather than merely labelled — the raw solve is kept under")
        print(f"  per_axis_withheld.raw for inspection only. 'cond' is the condition number of the")
        print(f"  column-normalised design matrix; identifiable requires cond < {COND_MAX:.0f}.")
        print(f"\n  GATE: best fit error {best_err if best_err is None else round(best_err,2)}% "
              f"(must be <5% on at least one group, or the oracle is not trustworthy)")
    # exit 1 if NO group fits within 5% — that is the gate failing, and it is loud.
    return 0 if (best_err is not None and best_err < 5.0) else 1

if __name__ == "__main__":
    sys.exit(main())
