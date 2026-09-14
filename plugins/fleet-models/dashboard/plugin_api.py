"""Fleet Models page — backend. Mounted at /api/plugins/fleet-models/ behind the dashboard's own auth.

Reads: models.yaml (the source of truth), the nine live configs (what Hermes will actually do — drift is
shown, never hidden), every profile's state.db (real usage, billed dollars, which host served), and
OpenRouter's public endpoints API (prices, uptime, status per host; cached 10 min, falls back to the
nightly snapshot). Writes go through core.apply only: validate → pre-image → compile → verify → history.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
_HERE = Path(__file__).resolve().parent


_CORE_LOCK = threading.Lock()


def _core():
    """core.py, loaded once per process. The dashboard serves /state and /usage on parallel threads, and the
    page asks for both at once: without the lock, the second thread found the half-executed module in
    sys.modules and failed with "no attribute 'fleet_root'" (a 500 on the first load after every restart)."""
    name = "fleet_models_core"
    mod = sys.modules.get(name)
    if mod is not None and getattr(mod, "_fleet_models_ready", False):
        return mod
    with _CORE_LOCK:
        mod = sys.modules.get(name)
        if mod is None or not getattr(mod, "_fleet_models_ready", False):
            spec = importlib.util.spec_from_file_location(name, _HERE.parent / "core.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            mod._fleet_models_ready = True
    return mod


def _root() -> Path:
    return _core().fleet_root()


# ── OpenRouter market data ───────────────────────────────────────────────────────────────────
_MARKET: Dict[str, tuple] = {}
_MARKET_TTL = 600
_UA = {"User-Agent": "hermes-fleet-models/1.0", "Accept": "application/json"}


def _pct(v):
    return round(float(v), 2) if isinstance(v, (int, float)) else None


def _per_m(v):
    try:
        return round(float(v) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _normalise_api(model_id: str, data: dict) -> dict:
    eps = []
    for e in (data.get("endpoints") or []):
        pr = e.get("pricing") or {}
        params = e.get("supported_parameters") or []
        eps.append({
            "tag": e.get("tag"), "provider": e.get("provider_name"), "quant": e.get("quantization"),
            "in": _per_m(pr.get("prompt")), "out": _per_m(pr.get("completion")),
            "cache_read": _per_m(pr.get("input_cache_read")), "cache_write": _per_m(pr.get("input_cache_write")),
            "discount": pr.get("discount") or 0, "ctx": e.get("context_length"),
            "uptime_1d": _pct(e.get("uptime_last_1d")), "uptime_30m": _pct(e.get("uptime_last_30m")),
            "status": e.get("status"), "tools": "tools" in params, "reasoning": "reasoning" in params,
            "latency_ms": (e.get("latency_last_30m") or {}).get("p50") if isinstance(e.get("latency_last_30m"), dict) else e.get("latency_last_30m"),
            "tps": (e.get("throughput_last_30m") or {}).get("p50") if isinstance(e.get("throughput_last_30m"), dict) else e.get("throughput_last_30m"),
        })
    return {"model": model_id, "name": data.get("name"), "modality": (data.get("architecture") or {}).get("modality"),
            "endpoints": eps, "source": "live", "fetched_at": time.time()}


def _snapshot_market(model_id: str) -> Optional[dict]:
    root = _root()
    try:
        snap = json.loads((root / "state" / "or-prices.json").read_text())
        m = (snap.get("models") or {}).get(model_id)
        if m:
            eps = [{"tag": t, "provider": e.get("provider_name"), "quant": e.get("quant"), "in": e.get("in"), "out": e.get("out"),
                    "cache_read": e.get("cache_read"), "cache_write": e.get("cache_write"), "discount": e.get("discount") or 0,
                    "ctx": e.get("ctx"), "uptime_1d": _pct(e.get("uptime")), "uptime_30m": None, "status": e.get("status"),
                    "tools": e.get("tools"), "reasoning": None, "latency_ms": None, "tps": None}
                   for t, e in (m.get("endpoints") or {}).items()]
            return {"model": model_id, "endpoints": eps, "source": "snapshot", "fetched_at": snap.get("generated_at")}
    except (OSError, ValueError):
        pass
    return None


def market(model_id: str, fresh: bool = False) -> dict:
    hit = _MARKET.get(model_id)
    if hit and not fresh and time.time() - hit[0] < _MARKET_TTL:
        return hit[1]
    try:
        url = "https://openrouter.ai/api/v1/models/" + model_id + "/endpoints"
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=8) as r:
            data = json.loads(r.read().decode()).get("data") or {}
        out = _normalise_api(model_id, data)
    except Exception as exc:  # noqa: BLE001
        out = _snapshot_market(model_id) or {"model": model_id, "endpoints": [], "source": "unavailable", "error": str(exc)[:200]}
    _MARKET[model_id] = (time.time(), out)
    return out


def _uptime_snapshot(doc: dict) -> dict:
    """{model id: {endpoints: {tag: {uptime}}}} for validation of host rules (cached market data only)."""
    out = {}
    for m in (doc.get("models") or {}).values():
        if m.get("provider") != "openrouter":
            continue
        mk = market(m["id"]) if (m.get("rules") or {}).get("min_uptime") else (_MARKET.get(m["id"], (0, None))[1] or _snapshot_market(m["id"]))
        if mk and mk.get("endpoints"):
            out[m["id"]] = {"endpoints": {e["tag"]: {"uptime": e.get("uptime_1d")} for e in mk["endpoints"] if e.get("tag")}}
    return out


# ── usage from every ledger ──────────────────────────────────────────────────────────────────
# A ledger row (session_model_usage) is one session × model × host × task, with the time of its first and last
# call. Short windows need more than "when did it end": each row's usage is spread evenly across its active span
# (first_seen → last_seen) and pro-rated into the window and into each bucket. Measured 2026-09-12: ~70% of calls
# sit in rows spanning 1–15 min and ~12% in rows over an hour, so 15-minute views are close estimates, not guesses.
# Buckets align to the VIEWER's local clock (IANA tz from the browser) — hours on the hour, days at local midnight,
# DST-safe — so "24 h" reads 09:00, 10:00… in Auckland rather than on UTC boundaries.
WINDOW_MIN, WINDOW_MAX = 900, 90 * 86400
NICE_BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)
MAX_BUCKETS = 400
_TZ_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_+-/")


def natural_bucket(window: int) -> int:
    """The bucket a window reads best in when there is room: 24 h by the hour, 7 days by 6 h, 30 days by day."""
    for w, b in ((900, 60), (1800, 60), (3600, 120), (7200, 300), (10800, 300), (21600, 900), (43200, 1800),
                 (86400, 3600), (7 * 86400, 21600)):
        if window <= w:
            return b
    return 86400


def _tzinfo(name: Optional[str]):
    from datetime import timezone
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover
        ZoneInfo = None
    cands = [name] if name else []
    cands.append(os.environ.get("TZ"))
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            cands.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for c in cands:
        if c and ZoneInfo and len(c) <= 64 and set(c) <= _TZ_OK:
            try:
                return ZoneInfo(c), c
            except Exception:  # noqa: BLE001
                continue
    loc = __import__("datetime").datetime.now().astimezone().tzinfo
    return loc or timezone.utc, str(loc or "UTC")


def _floor_local(ts: float, b: int, tz) -> float:
    from datetime import datetime, timedelta
    dt = datetime.fromtimestamp(ts, tz)
    mid = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if b >= 86400:
        return (mid - timedelta(days=dt.date().toordinal() % (b // 86400))).timestamp()
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second
    return (mid + timedelta(seconds=secs - secs % b)).timestamp()


def bucket_edges(since: float, now: float, b: int, tz) -> List[float]:
    """Local-clock bucket boundaries covering [since, now]. Stepping 1.5 buckets and re-flooring keeps days at
    midnight across a 23- or 25-hour DST day, and hours on the hour after the clocks change."""
    edges = [_floor_local(since, b, tz)]
    while edges[-1] < now and len(edges) <= MAX_BUCKETS + 2:
        cur = edges[-1]
        nxt = _floor_local(cur + 1.5 * b, b, tz)
        edges.append(nxt if nxt > cur else cur + b)
    return edges


def _ledgers(root: Path):
    out = [("root", root / "state.db")]
    out += [(p.parent.name, p) for p in sorted((root / "profiles").glob("*/state.db"))]
    return [(n, p) for n, p in out if p.exists()]


def _usage_rows(root: Path, since: float, now: float):
    """(profile, model, host, base, task, billing_provider, calls, in, out, cache_read, cache_write,
    cost, source, first, last)."""
    for prof, db in _ledgers(root):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            q = ("SELECT model, COALESCE(provider_name,''), COALESCE(billing_base_url,''), COALESCE(task,''), "
                 "COALESCE(billing_provider,''), "
                 "COALESCE(api_call_count,0), COALESCE(input_tokens,0), COALESCE(output_tokens,0), "
                 "COALESCE(cache_read_tokens,0), COALESCE(cache_write_tokens,0), "
                 "CASE WHEN COALESCE(actual_cost_usd,0) > 0 THEN actual_cost_usd WHEN COALESCE(total_cost,0) > 0 THEN total_cost "
                 "ELSE COALESCE(estimated_cost_usd,0) END, COALESCE(cost_source,''), "
                 "COALESCE(first_seen, last_seen), last_seen "
                 "FROM session_model_usage WHERE last_seen >= ? AND COALESCE(first_seen, last_seen) <= ?")
            for r in c.execute(q, (since, now)):
                yield (prof,) + tuple(r)
            c.close()
        except sqlite3.Error:
            continue


def usage(window: int = 7 * 86400, bucket: Optional[int] = None, tz: Optional[str] = None,
          now: Optional[float] = None, root: Optional[Path] = None) -> dict:
    import bisect
    core = _core(); root = root or _root()
    window = max(WINDOW_MIN, min(int(window), WINDOW_MAX))
    b = int(bucket) if bucket in NICE_BUCKETS else natural_bucket(window)
    while window / b > MAX_BUCKETS:
        b = next((x for x in NICE_BUCKETS if x > b), b * 2)
    tzi, tzname = _tzinfo(tz)
    now = float(now if now is not None else time.time())
    since = now - window
    try:
        doc = core.load_doc(root)
    except Exception:  # noqa: BLE001
        doc = {"models": {}}
    rates = {}
    for m in (doc.get("models") or {}).values():
        if m.get("provider") == "modelark":
            ce = m.get("cap_equivalent") or {}
            for n in [m.get("id"), *(m.get("served_as") or [])]:
                rates[str(n).lower()] = (float(ce.get("input") or 0), float(ce.get("output") or 0), float(ce.get("cache_read") or 0))
    edges = bucket_edges(since, now, b, tzi)
    nb = len(edges) - 1
    # 2026-09-15 (Richie): "add to the dash token tracking, and track input and output tokens"
    # and "if possible, enable tracking of cache hits on all providers". Every field below was
    # already being READ off session_model_usage and then dropped on the floor; nothing new has to
    # be instrumented, only carried. DeepSeek populates prompt_cache_hit_tokens natively,
    # OpenRouter prompt_tokens_details.cached_tokens, and both land in cache_read_tokens.
    _Z = lambda: {"billed_usd": 0.0, "calls": 0.0, "modelark_calls": 0.0,
                  "input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    ser = [_Z() for _ in range(nb)]
    by_p: Dict[str, dict] = {}
    by_m: Dict[str, dict] = {}
    agg: Dict[tuple, dict] = {}

    def idx(t):
        return min(nb - 1, max(0, bisect.bisect_right(edges, t) - 1))

    for (prof, model, host, base, task, bprov, n, inp, out, cr, cw, cost, src, fs, ls) in _usage_rows(root, since, now):
        fs = float(fs if fs is not None else ls); ls = float(ls)
        span = ls - fs
        lo, hi = max(fs, since), min(ls, now)
        if span <= 1.0:
            parts = [(idx(ls), 1.0)] if since <= ls <= now else []
        elif hi > lo:
            parts = []
            for i in range(idx(lo), idx(hi) + 1):
                ov = min(hi, edges[i + 1]) - max(lo, edges[i])
                if ov > 0:
                    parts.append((i, ov / span))
        else:
            parts = []
        frac = sum(f for _, f in parts)
        if frac <= 0:
            continue
        ark = "bytepluses.com" in base or src in ("modelark subscription", "modelark-proxy")
        r = rates.get((model or "").lower()) or ((0.66, 1.98, 0.022) if "pro" in (model or "") else (0.15, 0.60, 0.003))
        capeq = ((inp + cw) * r[0] + out * r[1] + cr * r[2]) / 1e6 if ark else 0.0
        billed = 0.0 if ark else float(cost or 0)  # 1.x "modelark-proxy" rows stored the cap-equivalent AS cost; never invoiced
        calls = float(n or 0)
        _zs = lambda: {"calls": [0.0] * nb, "billed": [0.0] * nb, "input": [0.0] * nb,
                       "output": [0.0] * nb, "cache_read": [0.0] * nb}
        pp = by_p.setdefault(prof, _zs())
        mm = by_m.setdefault(model or "?", _zs())
        for i, f in parts:
            s_ = ser[i]
            s_["calls"] += calls * f; s_["billed_usd"] += billed * f
            s_["input"] += inp * f; s_["output"] += out * f
            s_["cache_read"] += cr * f; s_["cache_write"] += cw * f
            if ark:
                s_["modelark_calls"] += calls * f
            pp["calls"][i] += calls * f; pp["billed"][i] += billed * f
            pp["input"][i] += inp * f; pp["output"][i] += out * f; pp["cache_read"][i] += cr * f
            mm["calls"][i] += calls * f; mm["billed"][i] += billed * f
            mm["input"][i] += inp * f; mm["output"][i] += out * f; mm["cache_read"][i] += cr * f
        # Who is actually being paid, and is the number an invoice or an estimate? Three payers
        # now, not two: the ModelArk subscription ($0), a DIRECT provider like DeepSeek (metered,
        # but it returns no per-call cost so Hermes prices it from the published table — real
        # money, ESTIMATED), and OpenRouter (metered, and it returns the billed figure).
        payer = "modelark" if ark else (str(bprov or "").strip().lower() or "openrouter")
        if payer not in ("modelark", "openrouter"):
            pass                 # a direct provider: deepseek, and anything added later
        elif payer == "openrouter" and not host:
            payer = "openrouter"
        k = (prof, model, host or ("ModelArk" if ark else ""), task or "main", ark, payer)
        a = agg.setdefault(k, {"profile": prof, "model": model, "host": k[2], "task": k[3], "modelark": ark,
                               "payer": payer, "invoiced": payer == "openrouter", "calls": 0.0,
                               "input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0,
                               "billed_usd": 0.0, "cap_equivalent_usd": 0.0})
        a["calls"] += calls * frac; a["input"] += inp * frac; a["output"] += out * frac
        a["cache_read"] += cr * frac; a["cache_write"] += cw * frac
        a["billed_usd"] += billed * frac; a["cap_equivalent_usd"] += capeq * frac

    rows = []
    for a in agg.values():
        for x in ("calls", "input", "output", "cache_read", "cache_write"):
            a[x] = int(round(a[x]))
        # Cache HIT share of the prompt this route actually sent. input_tokens on a cached call is
        # only the uncached remainder, so the denominator is input + cache_read, never input alone
        # — the mistake that makes a 99% hit look like 0%.
        _den = a["input"] + a["cache_read"]
        a["cache_hit_pct"] = round(100.0 * a["cache_read"] / _den, 1) if _den else None
        a["tokens"] = a["input"] + a["output"] + a["cache_read"]
        a["billed_usd"] = round(a["billed_usd"], 6); a["cap_equivalent_usd"] = round(a["cap_equivalent_usd"], 6)
        if a["calls"] or a["billed_usd"]:
            rows.append(a)
    series = [{"t": edges[i], "end": edges[i + 1], "partial": edges[i] < since or edges[i + 1] > now,
               "calls": round(s_["calls"], 3), "modelark_calls": round(s_["modelark_calls"], 3),
               "billed_usd": round(s_["billed_usd"], 6),
               "input": int(round(s_["input"])), "output": int(round(s_["output"])),
               "cache_read": int(round(s_["cache_read"])), "cache_write": int(round(s_["cache_write"]))}
              for i, s_ in enumerate(ser)]
    rnd = lambda d: {k: {"calls": [round(v, 3) for v in x["calls"]], "billed": [round(v, 6) for v in x["billed"]],
                         "input": [int(round(v)) for v in x["input"]],
                         "output": [int(round(v)) for v in x["output"]],
                         "cache_read": [int(round(v)) for v in x["cache_read"]]} for k, x in d.items()}
    tot = {k: sum(r[k] for r in rows) for k in ("calls", "input", "output", "cache_read", "cache_write")}
    _den = tot["input"] + tot["cache_read"]
    tot["cache_hit_pct"] = round(100.0 * tot["cache_read"] / _den, 1) if _den else None
    tot["tokens"] = tot["input"] + tot["output"] + tot["cache_read"]
    tot["billed_usd"] = round(sum(r["billed_usd"] for r in rows), 6)
    tot["cap_equivalent_usd"] = round(sum(r["cap_equivalent_usd"] for r in rows), 6)
    # Split the money by what kind of number it is. An OpenRouter figure is what the invoice will
    # say; a direct provider's is Hermes' own estimate from the published rate card, because
    # DeepSeek returns no per-call cost. Presenting them as one number is how a metered provider
    # starts looking like a free one.
    tot["invoiced_usd"] = round(sum(r["billed_usd"] for r in rows if r.get("invoiced")), 6)
    tot["estimated_usd"] = round(sum(r["billed_usd"] for r in rows
                                     if not r.get("invoiced") and not r["modelark"]), 6)
    tot["subscription_calls"] = sum(r["calls"] for r in rows if r["modelark"])
    by_payer = {}
    for r in rows:
        b = by_payer.setdefault(r.get("payer") or "openrouter",
                                {"calls": 0, "billed_usd": 0.0, "input": 0, "output": 0,
                                 "cache_read": 0, "invoiced": bool(r.get("invoiced"))})
        b["calls"] += r["calls"]; b["billed_usd"] += r["billed_usd"]
        b["input"] += r["input"]; b["output"] += r["output"]; b["cache_read"] += r["cache_read"]
    for b in by_payer.values():
        b["billed_usd"] = round(b["billed_usd"], 6)
        d = b["input"] + b["cache_read"]
        b["cache_hit_pct"] = round(100.0 * b["cache_read"] / d, 1) if d else None
    tot["by_payer"] = by_payer
    return {"window": window, "bucket": b, "since": since, "now": now, "tz": tzname, "days": round(window / 86400, 4),
            "method": "spread", "rows": sorted(rows, key=lambda r: (-r["billed_usd"], -r["calls"])), "series": series,
            "by_profile": rnd(by_p), "by_model": rnd(by_m), "totals": tot, "generated_at": time.time()}


def _caps(root: Path) -> dict:
    """Cost caps are Richie's alone — shown read-only."""
    try:
        cfg = _core()._load_plain(root / "config.yaml")
        k = cfg.get("kanban") or {}
        return {x: k.get(x) for x in ("default_max_cost", "max_cost_ceiling", "max_cost_hard_ceiling") if x in k}
    except Exception:  # noqa: BLE001
        return {}


# ── provider balances ────────────────────────────────────────────────────────────────────────
# 2026-09-15 (Richie): "deepseek direct api costs will be different to openrouter costs. can we
# add balances on openrouter and modelark similar to the card you have added for deepseek? only
# add if possible." Two of the three are possible and both are here. The third is not, and says
# so in its own payload rather than inventing a number — see _modelark_quota.
_BAL_TTL = 300
_BAL: Dict[str, Any] = {"at": 0.0, "data": None}
_BAL_LOCK = threading.Lock()


def _env_key(name: str) -> Optional[str]:
    """os.environ first, then ~/.hermes/.env via Hermes' own loader. The dashboard process does not
    always inherit the gateway's environment."""
    v = os.environ.get(name)
    if v:
        return v.strip()
    try:
        from hermes_cli.env_loader import load_hermes_dotenv  # type: ignore
        load_hermes_dotenv()
        v = os.environ.get(name)
        return v.strip() if v else None
    except Exception:  # noqa: BLE001
        return None


def _provider_spend(root: Path, hours: float) -> Dict[str, dict]:
    """Trailing spend per BILLING provider, from the same ledgers and the same classification the
    charts use. `billed_usd` is money actually invoiced; `capeq_usd` is what a subscription rung
    would have cost on the metered route (zero for metered providers, where the two are the same
    thing)."""
    now = time.time()
    since = now - hours * 3600
    try:
        doc = _core().load_doc(root)
    except Exception:  # noqa: BLE001
        doc = {"models": {}}
    rates = {}
    for m in (doc.get("models") or {}).values():
        if m.get("provider") == "modelark":
            ce = m.get("cap_equivalent") or {}
            for n in [m.get("id"), *(m.get("served_as") or [])]:
                rates[str(n).lower()] = (float(ce.get("input") or 0), float(ce.get("output") or 0),
                                         float(ce.get("cache_read") or 0))
    _Z = lambda: {"billed_usd": 0.0, "capeq_usd": 0.0, "calls": 0.0, "input": 0.0,
                  "output": 0.0, "cache_read": 0.0}
    out: Dict[str, dict] = {}
    for (prof, model, host, base, task, bprov, n, inp, output, cr, cw, cost, src, fs, ls) in _usage_rows(root, since, now):
        b = base or ""
        if "bytepluses.com" in b or src in ("modelark subscription", "modelark-proxy"):
            who = "modelark"
        elif "api.deepseek.com" in b:
            who = "deepseek"
        elif "openrouter.ai" in b:
            who = "openrouter"
        else:
            who = (bprov or "other").lower() or "other"
        d = out.setdefault(who, _Z())
        if who == "modelark":
            r = rates.get((model or "").lower()) or ((0.66, 1.98, 0.022) if "pro" in (model or "") else (0.15, 0.60, 0.003))
            d["capeq_usd"] += ((inp + cw) * r[0] + output * r[1] + cr * r[2]) / 1e6
        else:
            d["billed_usd"] += float(cost or 0)
        d["calls"] += float(n or 0); d["input"] += inp; d["output"] += output; d["cache_read"] += cr
    return out


def _or_get(path: str, key: str) -> dict:
    req = urllib.request.Request("https://openrouter.ai/api/v1/" + path,
                                 headers={"Authorization": f"Bearer {key}", **_UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode()).get("data") or {}


def _openrouter_balance(root: Path, spend: Dict[str, dict]) -> dict:
    """Prepaid credit and an optional monthly limit, read live from the same key the fleet routes
    on — no management key required. Nothing here is hardcoded: Richie raises the limit and tops up
    credit as capacity requires, and a watchdog that is confidently wrong about money is worse than
    none. Whichever pot empties first is the one reported, which is the same rule budget-watch has
    used since 2026-08-28 (on 28 Aug the monthly limit had $45.31 left while credit had $20.22)."""
    out: dict = {"billing": "metered", "cost_basis": "invoiced per call by OpenRouter; balance is prepaid credit"}
    key = _env_key("OPENROUTER_API_KEY")
    credits = used = limit = limit_remaining = None
    if key:
        try:
            c = _or_get("credits", key); k = _or_get("key", key)
            credits, used = c.get("total_credits"), c.get("total_usage")
            limit, limit_remaining = k.get("limit"), k.get("limit_remaining")
            # OpenRouter's own invoiced total for today, next to our telemetry estimate. The two
            # were reconciled to 99.9% over five weeks on 2026-08-28; showing both is what would
            # catch that agreement breaking down.
            out["invoiced_today_usd"] = k.get("usage_daily")
            out["invoiced_month_usd"] = k.get("usage_monthly")
            out["limit_reset"] = k.get("limit_reset")
            out["source"] = "api"
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:160]
    else:
        out["error"] = "OPENROUTER_API_KEY not available to the dashboard process"
    balance = (credits - used) if (credits is not None and used is not None) else None
    if balance is None:
        # budget-watch reads the same two endpoints hourly and records what it saw. Falling back to
        # its heartbeat keeps the card honest during an OpenRouter outage instead of showing blank.
        try:
            hb = json.loads((root / "state" / "budget-watch-heartbeat.json").read_text())
            if hb.get("balance") is not None:
                balance = float(hb["balance"]); out["source"] = "budget-watch heartbeat"
                out["at"] = hb.get("at")
        except Exception:  # noqa: BLE001
            pass
    else:
        out["at"] = time.time()
    pots = [(n, v) for n, v in (("credit balance", balance), ("monthly limit", limit_remaining)) if v is not None]
    binding, headroom = min(pots, key=lambda p: p[1]) if pots else (None, None)
    per_h = (spend.get("openrouter", {}).get("billed_usd", 0.0)) / 72.0
    out.update({
        "balance_usd": balance, "credits_purchased_usd": credits, "credits_used_usd": used,
        "limit_usd": limit, "limit_remaining_usd": limit_remaining,
        "binding": binding, "headroom_usd": headroom,
        "burn_usd_per_h": round(per_h, 4),
        "runway_h": (round(headroom / per_h, 1) if (headroom is not None and per_h > 0) else None),
        "window_spent_usd": round(spend.get("openrouter", {}).get("billed_usd", 0.0), 4),
        "window_hours": 72,
    })
    return out


def _modelark_quota(root: Path, spend: Dict[str, dict]) -> dict:
    """ModelArk has no balance, and that is a finding rather than an omission.

    The Coding Plan is a flat subscription: marginal cost is $0, so there is no pot to drain and no
    dollar figure that would be true. Probed on 2026-09-15 against the live key:
    `/api/coding/v3/usage`, `/quota` and `/subscription` all answer 200 with an EMPTY body — and so
    does a nonsense path, which is what proves they do not exist; a real completion comes back with
    no ratelimit or quota headers of any kind. Volcengine's billing OpenAPI would answer this, but
    it needs an AK/SK pair signed per request and the fleet holds only an `ark-` API key (1Password
    carries no access-key item). Inventing a balance here would be the exact failure budget-watch
    was written to prevent.

    What IS knowable is reported instead: whether the 5-hour rolling bucket is currently exhausted
    (modelark-quota-watch parses the reset time straight out of the 429), and what the subscription
    served in the last 5 hours valued at the metered rate it displaced."""
    out = {
        "billing": "subscription",
        "balance_usd": None,
        "balance_available": False,
        "why_no_balance": ("flat subscription — BytePlus exposes no quota or balance endpoint on an "
                           "ark- key (probed 2026-09-15), and the billing OpenAPI needs an AK/SK pair "
                           "the fleet does not hold"),
        "cost_basis": "not invoiced per call; the dollar figure shown is cap-equivalent — what this traffic would have cost on the metered route",
        "quota_window_h": 5,
    }
    st = {}
    try:
        st = json.loads((root / "state" / "modelark-quota.json").read_text()) or {}
    except Exception:  # noqa: BLE001
        pass
    out["exhausted"] = bool(st.get("exhausted"))
    out["reset_at"] = st.get("reset_at")
    out["last_event_at"] = st.get("at") or st.get("last_seen")
    w = _provider_spend(root, 5).get("modelark", {})
    out.update({
        "window_calls": int(w.get("calls", 0)),
        "window_capeq_usd": round(w.get("capeq_usd", 0.0), 4),
        "window_input": int(w.get("input", 0)),
        "window_output": int(w.get("output", 0)),
        "window_cache_read": int(w.get("cache_read", 0)),
        "day_capeq_usd": round(spend.get("modelark", {}).get("capeq_usd", 0.0), 2),
    })
    try:
        hb = json.loads((root / "state" / "modelark-watch.json").read_text())
        out.setdefault("last_event_at", hb.get("at"))
    except Exception:  # noqa: BLE001
        pass
    return out


def _deepseek_balance(root: Path, spend: Dict[str, dict]) -> Optional[dict]:
    """DeepSeek returns no per-call cost, so every Hermes number for that rung is an estimate from
    the published rate card. `/user/balance` — same key, no second credential — is the invoice side,
    and scripts/deepseek-balance-watch.py records it. Surfacing both is what stops a metered
    provider reading as a free one."""
    try:
        st = json.loads((root / "state" / "deepseek-balance.json").read_text())
    except Exception:  # noqa: BLE001
        return None
    return {
        "billing": "metered",
        "balance_usd": st.get("balance_usd"),
        "cumulative_spent_usd": st.get("cumulative_spent_usd"),
        "window_spent_usd": st.get("window_spent_usd"),
        "window_estimated_usd": st.get("window_estimated_usd"),
        "at": st.get("at"),
        "available": st.get("available"),
        "day_estimated_usd": round(spend.get("deepseek", {}).get("billed_usd", 0.0), 4),
        "cost_basis": "estimated per call from the published rate card; balance is the invoice side",
    }


def _direct_provider_balances(root: Path) -> dict:
    """All three providers in one shape, cached briefly — /state is polled by the open page and two
    of these make outbound calls. Each entry says its own billing model, because the three are
    genuinely different: prepaid credit, metered postpay, and a flat subscription with no balance
    at all. A provider that cannot be read is omitted rather than shown as zero."""
    with _BAL_LOCK:
        if _BAL["data"] is not None and (time.time() - _BAL["at"]) < _BAL_TTL:
            return _BAL["data"]
    out: dict = {}
    try:
        spend = _provider_spend(root, 72)
    except Exception:  # noqa: BLE001
        spend = {}
    for name, fn in (("openrouter", _openrouter_balance), ("deepseek", _deepseek_balance),
                     ("modelark", _modelark_quota)):
        try:
            v = fn(root, spend)
            if v:
                out[name] = v
        except Exception:  # noqa: BLE001
            continue
    with _BAL_LOCK:
        _BAL["at"] = time.time(); _BAL["data"] = out
    return out


def _runtime_status() -> dict:
    try:
        from agent import auxiliary_client as ac
        aux = bool(getattr(ac._build_call_kwargs, "_fleet_models_wrapped", False))
    except Exception:  # noqa: BLE001
        aux = False
    return {"aux_routing_seam": aux}


# ── routes ───────────────────────────────────────────────────────────────────────────────────
@router.get("/state")
def get_state():
    core = _core(); root = _root()
    try:
        doc = core.load_doc(root, fresh=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    live = {}
    drift = {}
    for p in core.PROFILES:
        try:
            cfg = core._load_plain(core.cfg_path(p, root))
            eff = core.effective(cfg)
            live[p] = {k: eff[k] for k in ("main", "subagents", "cron", "aux", "routing", "reasoning", "reasoning_overrides")}
            probs = core.verify_profile(doc, p, cfg)
            if probs:
                drift[p] = probs
        except Exception as exc:  # noqa: BLE001
            drift[p] = [f"config unreadable: {exc}"]
    errors, warnings = core.validate(doc)
    return {"doc": doc, "revision": int(doc.get("revision") or 0), "live": live, "drift": drift,
            "validation": {"errors": errors, "warnings": warnings}, "history": core.history(root, 40),
            "caps": _caps(root), "runtime": _runtime_status(), "profiles": core.PROFILES,
            "balances": _direct_provider_balances(root),
            "generated_at": time.time()}


@router.get("/usage")
def get_usage(window: Optional[int] = None, bucket: Optional[int] = None, tz: Optional[str] = None, days: Optional[int] = None):
    """window = seconds (15 min … 90 days); bucket = one of NICE_BUCKETS (else the window's natural bucket);
    tz = the viewer's IANA zone. `days` is the 1.0.x form, kept for old tabs still open."""
    if window:
        return usage(int(window), bucket=bucket, tz=tz)
    # a 1.0.4 page still open in a browser: daily UTC buckets under the old "daily" key
    u = usage(int(days or 7) * 86400, bucket=86400, tz="UTC")
    u["daily"] = [{"day": int(x["t"] // 86400), "billed_usd": x["billed_usd"], "calls": int(round(x["calls"])),
                   "modelark_calls": int(round(x["modelark_calls"]))} for x in u["series"]]
    return u


@router.get("/market")
def get_market(model: str, fresh: bool = False):
    if not model or len(model) > 120 or ".." in model:
        raise HTTPException(400, "bad model id")
    return market(model, fresh=fresh)


class Change(BaseModel):
    doc: Dict[str, Any]
    base_revision: Optional[int] = None
    unlock: List[str] = []
    summary: str = ""


@router.post("/plan")
def post_plan(ch: Change):
    core = _core()
    r = core.apply(ch.doc, by="dashboard", root=_root(), base_revision=ch.base_revision,
                   unlock=tuple(ch.unlock), dry_run=True, snapshot=_uptime_snapshot(ch.doc))
    return r


@router.post("/apply")
def post_apply(ch: Change):
    core = _core()
    return core.apply(ch.doc, by="dashboard", summary=ch.summary[:200], root=_root(),
                      base_revision=ch.base_revision, unlock=tuple(ch.unlock), snapshot=_uptime_snapshot(ch.doc))


# ── the model catalogue, for the "add any OpenRouter model to a waterfall" picker ─────────────
# Richie, 2026-09-15: "add to the dash the ability to find and search, and add any open router
# model dynamically to the waterfall. Do this via a dropdown menu for each waterfall."
#
# Adding the model is already possible: /plan and /apply take the WHOLE document, so a new
# registry entry and a new rung are an ordinary doc edit. What was missing was the list to pick
# FROM. This is that list — OpenRouter's public catalogue, filtered, with the fields the picker
# needs to show a sane row and the fields models.yaml needs to accept it.
_CAT: Dict[str, Any] = {"at": 0.0, "models": []}
_CAT_TTL = 900
_CAT_LOCK = threading.Lock()


def _catalogue(fresh: bool = False) -> List[dict]:
    with _CAT_LOCK:
        if not fresh and _CAT["models"] and (time.time() - _CAT["at"]) < _CAT_TTL:
            return _CAT["models"]
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                     headers={"User-Agent": "hermes-fleet-models"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — a picker that cannot reach the catalogue shows the cache
        return _CAT["models"]
    out = []
    for m in data.get("data") or []:
        pr = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        sup = m.get("supported_parameters") or []
        mods = arch.get("input_modalities") or []
        out.append({
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "vendor": str(m.get("id") or "").split("/")[0],
            "context": m.get("context_length"),
            # $/M, the unit the rest of this page speaks
            "prompt_per_m": _per_m(pr.get("prompt")),
            "completion_per_m": _per_m(pr.get("completion")),
            "cache_read_per_m": _per_m(pr.get("input_cache_read")),
            "cache_write_per_m": _per_m(pr.get("input_cache_write")),
            "tools": "tools" in sup or "tool_choice" in sup,
            "reasoning": "reasoning" in sup or "include_reasoning" in sup,
            "vision": "image" in mods,
            "modalities": mods,
        })
    out.sort(key=lambda x: (x["vendor"] or "", x["id"] or ""))
    with _CAT_LOCK:
        _CAT["models"] = out
        _CAT["at"] = time.time()
    return out


@router.get("/catalogue")
def get_catalogue(q: Optional[str] = None, tools: Optional[bool] = None,
                  vision: Optional[bool] = None, limit: int = 200, fresh: bool = False):
    """Searchable OpenRouter catalogue for the per-waterfall picker.

    `q` matches id, name or vendor, case-insensitively, on every whitespace-separated term — so
    "deep flash" finds deepseek flash models without needing the exact slug. A rung must be able
    to run an agent loop, so the picker can filter on tool calling; `vision` filters the aux.vision
    chains, whose every rung has to accept images (core.py validates that and would refuse an
    apply otherwise — better to not offer it than to be refused).
    """
    rows = _catalogue(fresh=fresh)
    if q:
        terms = [t for t in str(q).lower().split() if t]
        rows = [m for m in rows
                if all(t in f"{m['id']} {m['name']} {m['vendor']}".lower() for t in terms)]
    if tools is not None:
        rows = [m for m in rows if bool(m["tools"]) is bool(tools)]
    if vision is not None:
        rows = [m for m in rows if bool(m["vision"]) is bool(vision)]
    total = len(rows)
    return {"total": total, "shown": min(total, max(1, int(limit))),
            "stale": bool(_CAT["at"] and (time.time() - _CAT["at"]) > _CAT_TTL),
            "fetched_at": _CAT["at"], "models": rows[:max(1, int(limit))]}


class Revert(BaseModel):
    id: str


@router.post("/revert")
def post_revert(rv: Revert):
    return _core().revert(rv.id, by="dashboard", root=_root())


class Probe(BaseModel):
    model: str
    host: str


_PROBES: List[float] = []
_PROBE_LOCK = threading.Lock()


@router.post("/probe")
def post_probe(pb: Probe):
    """routable(): one tiny call pinned to ONE host with fallbacks off. An unmatched slug in `order` fails
    SILENTLY on OpenRouter, so a host pin is only trusted once a call has actually been served by it."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        try:
            from hermes_cli.env_loader import load_hermes_dotenv  # type: ignore
            load_hermes_dotenv(); key = os.environ.get("OPENROUTER_API_KEY")
        except Exception:  # noqa: BLE001
            key = None
    if not key:
        raise HTTPException(503, "OPENROUTER_API_KEY not available to the dashboard process")
    with _PROBE_LOCK:
        now = time.time()
        _PROBES[:] = [t for t in _PROBES if now - t < 3600]
        if len(_PROBES) >= 30:
            raise HTTPException(429, "probe budget: 30 per hour")
        _PROBES.append(now)
    body = {"model": pb.model, "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "max_tokens": 64, "provider": {"only": [pb.host], "allow_fallbacks": False, "data_collection": "deny"},
            "usage": {"include": True}}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", **_UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        served = data.get("provider")
        u = data.get("usage") or {}
        return {"ok": True, "host": pb.host, "served_by": served, "latency_ms": int((time.time() - t0) * 1000),
                "cost_usd": u.get("cost"), "routable": bool(served)}
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        # 429 = the pin MATCHED a host that is busy right now (OpenRouter moves on to the next pinned host);
        # 404 "no endpoints" = the pin matches nothing and would be dropped SILENTLY in a real request.
        busy = e.code == 429
        return {"ok": False, "host": pb.host, "routable": busy, "rate_limited": busy, "status": e.code,
                "error": "busy right now — the pin matches; requests move on to the next host" if busy else msg,
                "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "host": pb.host, "routable": False, "error": str(exc)[:300]}
