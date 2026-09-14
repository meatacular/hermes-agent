#!/usr/bin/env python3
"""or-price-snapshot.py — scrape OpenRouter per-model pages for the fields the API omits.

Why the page and not the API (measured 2026-09-09):
  * `discount` (numeric, per endpoint)      -> API: absent
  * `promotion_message` (carries the END DATE) -> API: absent. `expiration_date` in the API is a
    DEPRECATION sentinel (glm-5.3-flash reads 2098-12-31), not a price expiry.
  * `dataPolicy` retentionDays / canPublish -> API: absent. Lets us machine-check
    `data_collection: deny` instead of discovering it as a 404.
The LISTING pages are useless: ?order=discount-high-to-low and ?order=pricing-low-to-high return
byte-identical HTML (sort is client-side, ~16 models embedded). Per-model pages only.

Writes  ~/.hermes/state/or-prices.json          (full snapshot, overwritten)
Appends ~/.hermes/logs/pricing-findings.jsonl   (only when something CHANGED)
Exit 0 always unless the fetch itself is broken — watchdog convention: silent unless wrong.
"""
import json, os, re, sys, codecs, time, urllib.request, urllib.error

H = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
if os.path.basename(os.path.dirname(H)) == "profiles":
    H = os.path.dirname(os.path.dirname(H))
STATE = os.path.join(H, "state", "or-prices.json")
FINDINGS = os.path.join(H, "logs", "pricing-findings.jsonl")
UA = {"User-Agent": "Mozilla/5.0"}
MOVE = 0.25  # a >=25% move on a pinned endpoint is a promo event whether or not we knew a date

def pinned_models():
    """Every OpenRouter model the fleet can land on: the fleet's single model file (fleet/models.json,
    plugins/fleet-models) plus a scan of the live configs — never a hardcoded list, or it goes stale silently."""
    out = set()
    try:
        fm = json.load(open(os.path.join(H, "fleet", "models.json")))
        out |= {m["id"] for m in (fm.get("models") or {}).values() if m.get("provider") == "openrouter" and m.get("id")}
    except (OSError, ValueError, KeyError):
        pass
    cfgs = [os.path.join(H, "config.yaml")]
    pdir = os.path.join(H, "profiles")
    if os.path.isdir(pdir):
        cfgs += [os.path.join(pdir, p, "config.yaml") for p in sorted(os.listdir(pdir))]
    pat = re.compile(r"^\s*(?:default|fallback_model|model):\s*([A-Za-z0-9._-]+/[A-Za-z0-9._:-]+)\s*$")
    for c in cfgs:
        try:
            for line in open(c):
                m = pat.match(line)
                if m and "/" in m.group(1):
                    out.add(m.group(1).split(":")[0] if m.group(1).endswith(":floor") else m.group(1))
        except OSError:
            pass
    return sorted(out)

def fetch_page(model):
    url = "https://openrouter.ai/" + model
    req = urllib.request.Request(url, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf8", "replace")
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    if not chunks:
        return None
    return codecs.decode("".join(chunks), "unicode_escape")

def endpoints_api(model):
    """Per-endpoint pricing WITH the tag. The page payload carries no `tag` field — only this
    API does — and the tag is the only thing that separates a provider's service tiers
    (openai/flex $0.100 vs openai $0.200 vs openai/fast $0.400, all named "OpenAI")."""
    url = f"https://openrouter.ai/api/v1/models/{model}/endpoints"
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60))["data"]
    out = {}
    for e in d.get("endpoints", []):
        p = e.get("pricing", {})
        def f(k):
            v = p.get(k)
            try: return float(v) * 1e6
            except (TypeError, ValueError): return None
        tag = e.get("tag") or e.get("provider_name")
        out[tag] = {"provider_name": e.get("provider_name"), "tag": tag,
                    "in": f("prompt"), "out": f("completion"),
                    "cache_read": f("input_cache_read"), "cache_write": f("input_cache_write"),
                    "ctx": e.get("context_length"), "quant": e.get("quantization"),
                    "uptime": e.get("uptime_last_30m"), "status": e.get("status"),
                    "tools": "tools" in (e.get("supported_parameters") or [])}
    return out


def endpoints_from(blob):
    """Brace-match JSON objects carrying provider_name + pricing, keyed by endpoint TAG.

    Regex windows mis-associate fields across endpoints; brace matching does not. And the key
    must be the tag: a provider can expose several service tiers under one name."""
    out = {}
    for m in re.finditer(r'\{"id":"', blob):
        s = m.start(); d = 0
        for i in range(s, min(s + 30000, len(blob))):
            ch = blob[i]
            if ch == "{": d += 1
            elif ch == "}":
                d -= 1
                if d == 0:
                    try: o = json.loads(blob[s:i+1])
                    except Exception: pass
                    else:
                        if isinstance(o, dict) and o.get("provider_name") and isinstance(o.get("pricing"), dict):
                            p = o["pricing"]
                            def f(k):
                                v = p.get(k)
                                try: return float(v) * 1e6
                                except (TypeError, ValueError): return None
                            # KEY BY TAG, NOT provider_name. OpenAI exposes three endpoints all
                            # named "OpenAI" (openai/flex $0.100, openai $0.200, openai/fast
                            # $0.400) — keying by name silently keeps one and mis-prices the
                            # workload by 2-4x. Cost me a wrong 2.39x reading on 2026-09-09.
                            out[o.get("tag") or o["provider_name"]] = {
                                "provider_name": o["provider_name"],
                                "in": f("prompt"), "out": f("completion"),
                                "cache_read": f("input_cache_read"), "cache_write": f("input_cache_write"),
                                "discount": p.get("discount", 0),
                                "overrides": p.get("overrides"),
                                "ctx": o.get("context_length"), "quant": o.get("quantization"),
                                "uptime": o.get("uptime_last_30m"), "status": o.get("status"),
                                "tools": bool((o.get("supports_tool_choice") or {}).get("auto")),
                                "tag": o.get("tag"),
                            }
                    break
    return out

def data_policies(blob):
    return {m.group(1): {"retentionDays": m.group(2), "canPublish": m.group(3)}
            for m in re.finditer(r'"slug":"([^"]+)".{0,3000}?"retentionDays":(\w+),"canPublish":(\w+)', blob, re.S)}

def main():
    models = pinned_models()
    extra = os.environ.get("PRICE_WATCH_EXTRA", "")
    models = sorted(set(models) | {x.strip() for x in extra.split(",") if x.strip()})
    prev = {}
    if os.path.exists(STATE):
        try: prev = json.load(open(STATE)).get("models", {})
        except Exception: prev = {}

    snap, findings, errors = {}, [], []
    for mid in models:
        try:
            blob = fetch_page(mid)
        except Exception as e:
            errors.append(f"{mid}: {e}"); continue
        if not blob:
            errors.append(f"{mid}: no embedded payload"); continue
        try:
            eps = endpoints_api(mid)            # authoritative pricing, tagged
        except Exception as e:
            errors.append(f"{mid}: endpoints api: {e}"); eps = {}
        page_eps = endpoints_from(blob)         # page-only fields: discount
        if not eps:
            errors.append(f"{mid}: no tagged endpoints from the API"); continue
        # attach discount by provider_name. Where a provider sells several tiers the page cannot
        # tell them apart either, so the discount is marked shared rather than invented per tier.
        by_name = {}
        for _k, v in page_eps.items():
            by_name.setdefault(v.get("provider_name") or _k, []).append(v)
        for tag, v in eps.items():
            cands = by_name.get(v["provider_name"]) or []
            if cands:
                v["discount"] = cands[0].get("discount", 0)
                v["discount_shared_across_tiers"] = len(cands) > 1
        promos = sorted({x for x in re.findall(r'"promotion_message":"([^"]{5,300})"', blob)})
        snap[mid] = {"endpoints": eps, "promotion_messages": promos,
                     "data_policies": data_policies(blob), "scraped_at": time.time()}

        # --- change detection: a >=25% move on ANY endpoint, and promo appear/disappear ---
        old = prev.get(mid, {})
        for prov, now in eps.items():
            was = (old.get("endpoints") or {}).get(prov)
            if not was: continue
            for axis in ("in", "out", "cache_read"):
                a, b = was.get(axis), now.get(axis)
                if a and b and abs(b - a) / a >= MOVE:
                    findings.append({"kind": "price_move", "model": mid, "provider": prov,
                                     "axis": axis, "from": a, "to": b,
                                     "pct": round(100 * (b - a) / a, 1)})
            if bool(was.get("discount")) != bool(now.get("discount")):
                findings.append({"kind": "discount_change", "model": mid, "provider": prov,
                                 "from": was.get("discount"), "to": now.get("discount")})
        oldp = set(old.get("promotion_messages") or [])
        if promos and set(promos) != oldp:
            findings.append({"kind": "promotion_message", "model": mid, "messages": promos})
        if oldp and not promos:
            findings.append({"kind": "promotion_ended", "model": mid, "was": sorted(oldp)})

    out = {"generated_at": time.time(), "models": snap, "errors": errors}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"; json.dump(out, open(tmp, "w"), indent=1); os.replace(tmp, STATE)

    if findings:
        os.makedirs(os.path.dirname(FINDINGS), exist_ok=True)
        with open(FINDINGS, "a") as fh:
            for f in findings:
                f["at"] = time.time(); fh.write(json.dumps(f) + "\n")

    # Watchdog convention: silent unless something is wrong or something changed.
    if errors:
        print("PRICE SNAPSHOT ERRORS:")
        for e in errors: print("  " + e)
    for f in findings:
        print("CHANGE: " + json.dumps(f))
    if not errors and not findings and "-v" in sys.argv:
        print(f"ok: {len(snap)} models, no change")
    # A scrape that parsed NOTHING is a broken scraper, not a quiet day.
    return 2 if (models and not snap) else 0

if __name__ == "__main__":
    sys.exit(main())
