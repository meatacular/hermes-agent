(() => {
  // src/index.jsx
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (SDK) {
    let tickLabel2 = function(t, b, step) {
      const d = new Date(t * 1e3);
      if (step >= 86400 || b >= 86400) return dayMon(d);
      if (d.getHours() === 0 && d.getMinutes() === 0) return wday(d);
      return hhmm(d);
    }, isTick2 = function(t, step) {
      const d = new Date(t * 1e3);
      if (step >= 86400) {
        if (d.getHours() !== 0 || d.getMinutes() !== 0) return false;
        const ord = Math.round(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 864e5);
        return ord % (step / 86400) === 0;
      }
      const sec = d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
      return sec % step === 0;
    }, rangeLabel2 = function(s, b, since, now) {
      const a = new Date(Math.max(s.t, since) * 1e3), z = new Date(Math.min(s.end, now) * 1e3);
      if (b >= 86400) return wday(a) + " " + dayMon(a) + (b > 86400 ? " \u2013 " + dayMon(new Date((s.end - 1) * 1e3)) : "") + (s.end > now ? " (so far)" : "");
      const pre = new Date(now * 1e3).toDateString() === a.toDateString() ? "" : wday(a) + " ";
      return pre + hhmm(a) + "\u2013" + (s.end > now ? "now" : hhmm(z));
    }, cls2 = function(...xs) {
      return xs.filter(Boolean).join(" ");
    }, Pill2 = function({ doc, alias, onRemove, onLeft, onRight, first, last, dim, compact }) {
      const m = (doc.models || {})[alias];
      const prov = m ? m.provider : "missing";
      return /* @__PURE__ */ h("span", { className: cls2("fm-pill", "fm-pill--" + prov, dim && "fm-pill--dim"), title: m ? `${m.id}${m.notes ? "\n\n" + m.notes : ""}` : "not in the registry" }, onLeft && !first ? /* @__PURE__ */ h("button", { className: "fm-pill-btn", onClick: onLeft, title: "move earlier" }, "\u2039") : null, /* @__PURE__ */ h("span", { className: "fm-pill-dot" }), /* @__PURE__ */ h("span", { className: "fm-pill-name" }, m ? m.short || alias : alias), m && m.billing === "subscription" ? /* @__PURE__ */ h("span", { className: "fm-tag fm-tag--sub" }, "SUB") : null, m && m.reasoning && !compact ? /* @__PURE__ */ h("span", { className: "fm-tag", title: "reasoning pin" }, m.reasoning) : null, onRight && !last ? /* @__PURE__ */ h("button", { className: "fm-pill-btn", onClick: onRight, title: "move later" }, "\u203A") : null, onRemove ? /* @__PURE__ */ h("button", { className: "fm-pill-btn fm-pill-x", onClick: onRemove, title: "remove this rung" }, "\xD7") : null);
    }, Chain2 = function({ doc, chain, empty }) {
      if (!chain || !chain.length) return /* @__PURE__ */ h("span", { className: "fm-muted" }, empty || "\u2014");
      return /* @__PURE__ */ h("span", { className: "fm-chain" }, chain.map((a, i) => /* @__PURE__ */ h("span", { key: a + i, className: "fm-link-step" }, i ? /* @__PURE__ */ h("span", { className: "fm-arrow" }, "\u2192") : null, /* @__PURE__ */ h(Pill2, { doc, alias: a, dim: i > 0, compact: true }))));
    }, RungPicker2 = function({ doc, chain, onChange, setDraft, filter, wantsVision }) {
      const [open, setOpen] = useState(false);
      const [q, setQ] = useState("");
      const [hits, setHits] = useState(null);
      const [busy, setBusy] = useState(false);
      const opts = Object.keys(doc.models || {}).filter((a) => !chain.includes(a) && (!filter || filter(doc.models[a])));
      const known = useMemo(() => new Set(Object.values(doc.models || {}).map((m) => m.id)), [doc.models]);
      useEffect(() => {
        if (!open) return void 0;
        const qs = new URLSearchParams({ tools: "true", limit: "40" });
        if (q.trim()) qs.set("q", q.trim());
        if (wantsVision) qs.set("vision", "true");
        const t = setTimeout(() => {
          setBusy(true);
          fetchJSON(`${API}/catalogue?${qs}`).then((r) => {
            setHits(r);
            setBusy(false);
          }).catch((e) => {
            setHits({ error: String(e.message || e), models: [] });
            setBusy(false);
          });
        }, 220);
        return () => clearTimeout(t);
      }, [open, q, wantsVision]);
      const addFromCatalogue = (m) => {
        const base = String(m.id).split("/").pop().toLowerCase().replace(/[^a-z0-9]+/g, "-");
        let alias = base, n = 2;
        while (doc.models && doc.models[alias]) {
          alias = base + "-" + n;
          n += 1;
        }
        setDraft((d) => {
          const nd = clone(d);
          nd.models[alias] = {
            id: m.id,
            short: String(m.id).split("/").pop(),
            provider: "openrouter",
            vendor: m.vendor || String(m.id).split("/")[0],
            billing: "metered",
            context: m.context || void 0,
            tools: !!m.tools,
            vision: !!m.vision,
            notes: `Added from the dashboard picker ${(/* @__PURE__ */ new Date()).toISOString().slice(0, 10)}. OpenRouter list at add time: $${m.prompt_per_m ?? "?"}/M in, $${m.completion_per_m ?? "?"}/M out` + (m.cache_read_per_m != null ? `, $${m.cache_read_per_m}/M cache read` : ", cache read not published") + "."
          };
          return nd;
        });
        onChange(chain.concat([alias]));
        setOpen(false);
        setQ("");
        setHits(null);
      };
      if (!open) {
        return /* @__PURE__ */ h("span", { className: "fm-rungpick" }, opts.length ? /* @__PURE__ */ h("select", { className: "fm-add", value: "", onChange: (e) => e.target.value && onChange(chain.concat([e.target.value])) }, /* @__PURE__ */ h("option", { value: "" }, "+ rung"), opts.map((a) => /* @__PURE__ */ h("option", { key: a, value: a }, doc.models[a].short || a))) : null, setDraft ? /* @__PURE__ */ h(
          "button",
          {
            className: "fm-link fm-rungpick-open",
            onClick: () => setOpen(true),
            title: "search every OpenRouter model and add one as a rung"
          },
          "+ search\u2026"
        ) : null);
      }
      const rows = hits && hits.models || [];
      return /* @__PURE__ */ h("div", { className: "fm-rungpick fm-rungpick--open" }, /* @__PURE__ */ h("div", { className: "fm-row" }, /* @__PURE__ */ h(
        "input",
        {
          autoFocus: true,
          className: "fm-rungpick-q",
          placeholder: "search OpenRouter \u2014 e.g. deepseek flash",
          value: q,
          onChange: (e) => setQ(e.target.value),
          onKeyDown: (e) => e.key === "Escape" && setOpen(false)
        }
      ), /* @__PURE__ */ h("button", { className: "fm-link", onClick: () => setOpen(false) }, "close")), /* @__PURE__ */ h("div", { className: "fm-rungpick-list" }, busy && !rows.length ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "searching\u2026") : null, hits && hits.error ? /* @__PURE__ */ h("div", { className: "fm-bad fm-small" }, hits.error) : null, !busy && hits && !rows.length && !hits.error ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "nothing matches") : null, rows.map((m) => {
        const already = known.has(m.id);
        return /* @__PURE__ */ h(
          "button",
          {
            key: m.id,
            className: "fm-rungpick-row",
            disabled: already,
            title: already ? "already in the registry \u2014 pick it from + rung" : "add to the registry and append as a rung",
            onClick: () => addFromCatalogue(m)
          },
          /* @__PURE__ */ h("span", { className: "fm-rungpick-id" }, m.id),
          /* @__PURE__ */ h("span", { className: "fm-rungpick-meta" }, m.context ? `${Math.round(m.context / 1e3)}k` : "\u2014", " \xB7 ", "$", m.prompt_per_m ?? "?", "/M in \xB7 $", m.completion_per_m ?? "?", "/M out", m.cache_read_per_m != null ? ` \xB7 $${m.cache_read_per_m}/M cache` : "", m.vision ? " \xB7 vision" : "", already ? " \xB7 in registry" : "")
        );
      })), hits && hits.total > rows.length ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, rows.length, " of ", hits.total, " \u2014 narrow the search") : null);
    }, ChainEditor2 = function({ doc, chain, onChange, filter, setDraft, wantsVision }) {
      const move = (i, d) => {
        const c = chain.slice();
        const [x] = c.splice(i, 1);
        c.splice(i + d, 0, x);
        onChange(c);
      };
      return /* @__PURE__ */ h("span", { className: "fm-chain fm-chain--edit" }, chain.map((a, i) => /* @__PURE__ */ h(Fragment, { key: a + i }, i ? /* @__PURE__ */ h("span", { className: "fm-arrow" }, "\u2192") : null, /* @__PURE__ */ h(
        Pill2,
        {
          doc,
          alias: a,
          first: i === 0,
          last: i === chain.length - 1,
          onLeft: () => move(i, -1),
          onRight: () => move(i, 1),
          onRemove: chain.length > 1 ? () => onChange(chain.filter((_, j) => j !== i)) : null
        }
      ))), /* @__PURE__ */ h(
        RungPicker2,
        {
          doc,
          chain,
          onChange,
          setDraft,
          filter,
          wantsVision
        }
      ));
    }, Stat2 = function({ label, value, sub, tone, title }) {
      return /* @__PURE__ */ h("div", { className: cls2("fm-stat", tone && "fm-stat--" + tone), title }, /* @__PURE__ */ h("div", { className: "fm-stat-label" }, label), /* @__PURE__ */ h("div", { className: "fm-stat-value" }, value), sub ? /* @__PURE__ */ h("div", { className: "fm-stat-sub" }, sub) : null);
    }, relAge2 = function(ts) {
      if (!ts) return null;
      const m = Math.round(Date.now() / 1e3 - ts) / 60;
      return m < 1 ? "just now" : m < 60 ? Math.round(m) + "m ago" : Math.round(m / 60) + "h ago";
    }, BalanceStats2 = function({ balances, T }) {
      if (!balances) return null;
      const or = balances.openrouter, ds = balances.deepseek, ma = balances.modelark;
      const spent = (k) => T && T.by_payer && T.by_payer[k] || null;
      const cards = [];
      if (or && or.balance_usd != null) {
        const low = or.runway_h != null && or.runway_h < 48;
        cards.push(
          /* @__PURE__ */ h(
            Stat2,
            {
              key: "or",
              tone: low ? "warn" : "or",
              label: "OpenRouter credit",
              value: money(or.balance_usd, 2) + " left",
              title: [
                or.credits_purchased_usd != null ? money(or.credits_purchased_usd, 2) + " bought, " + money(or.credits_used_usd, 2) + " used" : null,
                or.invoiced_today_usd != null ? "OpenRouter invoiced " + money(or.invoiced_today_usd, 2) + " today" : null,
                "burn " + money(or.burn_usd_per_h, 3) + "/h over " + or.window_hours + "h",
                or.source === "api" ? "read live from the OpenRouter API" : "from the budget-watch heartbeat " + (relAge2(or.at) || "")
              ].filter(Boolean).join(" \xB7 "),
              sub: [
                or.limit_remaining_usd != null ? money(or.limit_remaining_usd, 2) + " of " + money(or.limit_usd, 2) + " monthly" : null,
                or.runway_h != null ? Math.round(or.runway_h) + "h runway on the " + or.binding : null
              ].filter(Boolean).join(" \xB7 ") || "prepaid credit"
            }
          )
        );
      }
      if (ds) {
        const w = spent("deepseek");
        cards.push(
          /* @__PURE__ */ h(
            Stat2,
            {
              key: "ds",
              tone: "ds",
              label: "DeepSeek credit",
              value: ds.balance_usd != null ? money(ds.balance_usd, 2) + " left" : "\u2014",
              title: ds.cost_basis + (ds.at ? " \xB7 read " + relAge2(ds.at) : ""),
              sub: "metered \xB7 " + (w ? money(w.billed_usd, 4) + " estimated this window" : "no calls this window")
            }
          )
        );
      }
      if (ma) {
        cards.push(
          /* @__PURE__ */ h(
            Stat2,
            {
              key: "ma",
              tone: ma.exhausted ? "warn" : "sub",
              label: "ModelArk quota",
              value: ma.exhausted ? "Exhausted" : "Available",
              title: ma.why_no_balance + " \xB7 " + ma.cost_basis,
              sub: (ma.exhausted && ma.reset_at ? "resets " + ma.reset_at + " \xB7 " : "no balance to read \xB7 ") + num(ma.window_calls) + " calls in " + ma.quota_window_h + "h \xB7 " + money(ma.window_capeq_usd, 2) + " cap-equivalent"
            }
          )
        );
      }
      if (!cards.length) return null;
      return /* @__PURE__ */ h("div", { className: "fm-stats fm-stats--bal" }, cards);
    }, DecisionLog2 = function({ entries, title, empty }) {
      const rows = (entries || []).slice().sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
      return /* @__PURE__ */ h(
        Section2,
        {
          title: title || "Decision log",
          right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, "dated record \u2014 not live state")
        },
        !rows.length ? /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, empty || "No decisions recorded for this model yet.") : null,
        /* @__PURE__ */ h("ol", { className: "fm-dlog" }, rows.map((d, i) => /* @__PURE__ */ h("li", { key: i, className: "fm-dlog-item" }, /* @__PURE__ */ h("div", { className: "fm-dlog-meta" }, /* @__PURE__ */ h("span", { className: "fm-dlog-date" }, d.date || "undated"), d.by ? /* @__PURE__ */ h("span", { className: cls2("fm-dlog-by", "fm-dlog-by--" + String(d.by).replace(/[^a-z]/g, "")) }, d.by) : null), /* @__PURE__ */ h("div", { className: "fm-dlog-body" }, /* @__PURE__ */ h("div", { className: "fm-dlog-what" }, d.what), d.why ? /* @__PURE__ */ h("div", { className: "fm-dlog-why" }, d.why) : null))))
      );
    }, Badge2 = function({ tone, children, title }) {
      return /* @__PURE__ */ h("span", { className: cls2("fm-badge", tone && "fm-badge--" + tone), title }, children);
    }, Section2 = function({ title, right, children, className }) {
      return /* @__PURE__ */ h("section", { className: cls2("fm-section", className) }, title ? /* @__PURE__ */ h("header", { className: "fm-section-head" }, /* @__PURE__ */ h("h3", null, title), right) : null, children);
    }, PeriodBar2 = function({ win, setWin, usage, loading, failed }) {
      const ref = useRef(null);
      useEffect(() => {
        const box = ref.current, el = box && box.querySelector(".is-active");
        if (!el) return;
        const l = el.offsetLeft - box.offsetLeft, r = l + el.offsetWidth;
        if (l < box.scrollLeft) box.scrollLeft = l - 8;
        else if (r > box.scrollLeft + box.clientWidth) box.scrollLeft = r - box.clientWidth + 8;
      }, [win]);
      return /* @__PURE__ */ h("div", { className: "fm-period" }, /* @__PURE__ */ h("div", { className: "fm-period-chips", role: "group", "aria-label": "Time window", ref }, PERIODS.map(([k, w, name]) => /* @__PURE__ */ h(
        "button",
        {
          key: k,
          className: cls2("fm-chip fm-chip--sm", w === win && "is-active"),
          "aria-pressed": w === win,
          title: lastLabel(w),
          onClick: () => setWin(w)
        },
        k
      ))), /* @__PURE__ */ h("span", { className: cls2("fm-small fm-period-note", failed ? "fm-warn-line" : "fm-muted") }, failed ? "usage unavailable \u2014 retrying" + (usage ? " \xB7 showing " + ago(usage.generated_at) : "") : loading ? "updating\u2026" : usage ? `${bucketName(usage.bucket)} bars \xB7 ${ago(usage.generated_at)}` : "loading usage\u2026"));
    }, Spark2 = function({ values, label, height }) {
      const v = values || [];
      const max = Math.max(0, ...v);
      if (!v.length) return null;
      return /* @__PURE__ */ h("span", { className: "fm-spark", style: { height: (height || 22) + "px" }, "aria-label": label, title: label }, v.map((x, i) => /* @__PURE__ */ h("span", { key: i, style: { height: max ? Math.max(x > 0 ? 8 : 0, 100 * x / max) + "%" : "0%" } })));
    }, TimeChart2 = function({ usage, width }) {
      const s = usage && usage.series || [];
      const [sel, setSel] = useState(null);
      useEffect(() => {
        setSel(null);
      }, [usage && usage.window, usage && usage.bucket]);
      if (!s.length) return null;
      const b = usage.bucket, n = s.length;
      const pitch = Math.max(1, (width || 600) / n);
      const need = Math.ceil(MIN_LABEL_GAP / pitch) * b;
      const step = LABEL_STEPS.find((x) => x >= need && x % b === 0) || need;
      const maxB = Math.max(0, ...s.map((x) => x.billed_usd));
      const maxC = Math.max(0, ...s.map((x) => x.calls));
      const tot = s.reduce((a, x) => ({ b: a.b + x.billed_usd, c: a.c + x.calls, m: a.m + x.modelark_calls }), { b: 0, c: 0, m: 0 });
      const cur = sel != null && s[sel] ? s[sel] : null;
      const readout = cur ? { when: rangeLabel2(cur, b, usage.since, usage.now), b: cur.billed_usd, c: cur.calls, m: cur.modelark_calls } : { when: lastLabel(usage.window), b: tot.b, c: tot.c, m: tot.m };
      const gap = pitch < 7 ? 1 : pitch < 14 ? 2 : 3;
      return /* @__PURE__ */ h("div", { className: "fm-tc", onPointerLeave: (e) => {
        if (e.pointerType === "mouse") setSel(null);
      } }, /* @__PURE__ */ h("div", { className: "fm-tc-readout", "aria-live": "polite" }, /* @__PURE__ */ h("b", null, readout.when), /* @__PURE__ */ h("span", null, /* @__PURE__ */ h("span", { className: "fm-lg fm-lg--money" }), " ", money(readout.b, 3), " billed"), /* @__PURE__ */ h("span", null, /* @__PURE__ */ h("span", { className: "fm-lg fm-lg--calls" }), " ", num(Math.round(readout.c)), " calls"), /* @__PURE__ */ h("span", null, /* @__PURE__ */ h("span", { className: "fm-lg fm-lg--sub" }), " ", num(Math.round(readout.m)), " on subscription")), /* @__PURE__ */ h("div", { className: "fm-tc-plot", style: { gap: gap + "px" } }, /* @__PURE__ */ h("span", { className: "fm-tc-max fm-tc-max--money" }, maxB ? money(maxB, 3) : "$0"), /* @__PURE__ */ h("span", { className: "fm-tc-max fm-tc-max--calls" }, maxC ? num(Math.round(maxC)) + " calls" : "0 calls"), s.map((x, i) => {
        const tick = isTick2(x.t, step) && !(i === 0 && x.t < usage.since && n > 3);
        return /* @__PURE__ */ h(
          "button",
          {
            key: x.t,
            type: "button",
            className: cls2("fm-tc-col", x.partial && "is-partial", sel === i && "is-sel"),
            onPointerEnter: (e) => {
              if (e.pointerType === "mouse") setSel(i);
            },
            onFocus: () => setSel(i),
            onClick: () => setSel(i),
            "aria-label": `${rangeLabel2(x, b, usage.since, usage.now)}: ${money(x.billed_usd, 3)} billed, ${Math.round(x.calls)} calls`
          },
          /* @__PURE__ */ h("span", { className: "fm-tc-m" }, /* @__PURE__ */ h("span", { style: { height: maxB ? 100 * x.billed_usd / maxB + "%" : "0%" } })),
          /* @__PURE__ */ h("span", { className: "fm-tc-c" }, /* @__PURE__ */ h("span", { style: { height: maxC ? 100 * x.calls / maxC + "%" : "0%" } }, /* @__PURE__ */ h("span", { style: { height: x.calls ? 100 * x.modelark_calls / x.calls + "%" : "0%" } }))),
          /* @__PURE__ */ h("span", { className: "fm-tc-lbl" }, tick ? tickLabel2(x.t, b, step) : "")
        );
      })), !tot.c ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small fm-tc-empty" }, "No calls in this window.") : null);
    }, helperGroups2 = function(aux) {
      const groups = {};
      Object.entries(aux || {}).forEach(([t, s]) => {
        if (t === "vision") return;
        const k = JSON.stringify(chainOf(s)) + "|" + reasoningOf(s);
        (groups[k] = groups[k] || { chain: chainOf(s), reasoning: reasoningOf(s), tasks: [] }).tasks.push(t);
      });
      return Object.values(groups);
    }, LiveChain2 = function({ doc, live }) {
      if (!live) return /* @__PURE__ */ h("span", { className: "fm-muted" }, "not set");
      const byId = {};
      Object.entries(doc.models || {}).forEach(([a, m]) => {
        byId[m.provider + "|" + m.id] = a;
      });
      return /* @__PURE__ */ h("span", { className: "fm-chain" }, live.map(([prov, id], i) => /* @__PURE__ */ h(Fragment, { key: i }, i ? /* @__PURE__ */ h("span", { className: "fm-arrow" }, "\u2192") : null, byId[prov + "|" + id] ? /* @__PURE__ */ h(Pill2, { doc, alias: byId[prov + "|" + id], dim: true }) : /* @__PURE__ */ h("code", { className: "fm-code" }, prov, ":", id))));
    }, SlotRow2 = function({ doc, label, spec, live, onChange, nullable, nullLabel, withReasoning, filter, onDelete, hint, setDraft, wantsVision }) {
      const chain = chainOf(spec);
      const isNull = spec == null;
      const primary = Object.keys(doc.models || {}).find((a) => !filter || filter(doc.models[a]));
      return /* @__PURE__ */ h("div", { className: "fm-slot" }, /* @__PURE__ */ h("div", { className: "fm-slot-label" }, /* @__PURE__ */ h("div", null, label), hint ? /* @__PURE__ */ h("div", { className: "fm-slot-hint" }, hint) : null), /* @__PURE__ */ h("div", { className: "fm-slot-body" }, isNull ? /* @__PURE__ */ h("span", { className: "fm-muted" }, nullLabel, " ", /* @__PURE__ */ h("button", { className: "fm-link", onClick: () => onChange([primary]) }, "set a chain")) : /* @__PURE__ */ h(
        ChainEditor2,
        {
          doc,
          chain,
          filter,
          setDraft,
          wantsVision,
          onChange: (c) => onChange(withChain(spec, c))
        }
      ), /* @__PURE__ */ h("div", { className: "fm-slot-live" }, "live: ", /* @__PURE__ */ h(LiveChain2, { doc, live }))), /* @__PURE__ */ h("div", { className: "fm-slot-side" }, withReasoning && !isNull ? /* @__PURE__ */ h(
        "select",
        {
          value: reasoningOf(spec),
          title: "reasoning effort for this task",
          onChange: (e) => onChange(e.target.value ? { chain, reasoning: e.target.value } : chain)
        },
        REASONING.map((r) => /* @__PURE__ */ h("option", { key: r, value: r }, r ? "reasoning " + r : "reasoning \u2014"))
      ) : null, nullable && !isNull ? /* @__PURE__ */ h("button", { className: "fm-link", onClick: () => onChange(null), title: "remove this chain" }, "\u2192 ", nullLabel || "clear") : null, onDelete ? /* @__PURE__ */ h("button", { className: "fm-link fm-link--danger", onClick: onDelete }, "remove") : null));
    }, listInput2 = function(v) {
      return (v || []).join(", ");
    }, parseList2 = function(s) {
      return s.split(",").map((x) => x.trim()).filter(Boolean);
    }, AgentView2 = function({ state, draft, setDraft, p, setP }) {
      const a = draft.agents[p];
      const live = state.live[p] || {};
      const set = (fn) => setDraft((d) => {
        const n = clone(d);
        fn(n.agents[p], n);
        return n;
      });
      const aux = a.aux || {};
      const unusedTasks = AUX_TASKS.filter((t) => !(t in aux));
      const drift = (state.drift || {})[p];
      const visionOk = (m) => !!m.vision;
      const toolsOk = (m) => m.tools !== false;
      return /* @__PURE__ */ h("div", { className: "fm-agent" }, /* @__PURE__ */ h("nav", { className: "fm-agent-nav" }, state.profiles.map((q) => /* @__PURE__ */ h("button", { key: q, className: cls2("fm-agent-tab", q === p && "is-active", (state.drift || {})[q] && "has-drift"), onClick: () => setP(q) }, draft.agents[q].name || q, draft.agents[q].locked ? " \u{1F512}" : ""))), /* @__PURE__ */ h(Section2, { title: `${a.name || p} \u2014 ${a.role || ""}`, right: /* @__PURE__ */ h("span", { className: "fm-row" }, a.locked ? /* @__PURE__ */ h(Badge2, { tone: "warn" }, "locked") : null, drift ? /* @__PURE__ */ h(Badge2, { tone: "warn", title: drift.join("\n") }, "config drifted from models.yaml") : /* @__PURE__ */ h(Badge2, { tone: "ok" }, "config matches")) }, drift ? /* @__PURE__ */ h("div", { className: "fm-note fm-note--warn" }, drift.map((d, i) => /* @__PURE__ */ h("div", { key: i }, d)), /* @__PURE__ */ h("div", null, "Applying any change rewrites this profile from models.yaml.")) : null, /* @__PURE__ */ h("div", { className: "fm-slots-edit" }, /* @__PURE__ */ h(
        SlotRow2,
        {
          doc: draft,
          setDraft,
          label: "Main loop",
          hint: "primary \u2192 fallbacks",
          spec: a.main,
          live: live.main,
          filter: toolsOk,
          onChange: (c) => set((x) => {
            x.main = c;
          })
        }
      ), /* @__PURE__ */ h(
        SlotRow2,
        {
          doc: draft,
          setDraft,
          label: "Subagents",
          hint: "delegated children \u2014 their own chain",
          spec: a.subagents,
          live: live.subagents,
          nullable: true,
          nullLabel: "inherit main",
          filter: toolsOk,
          onChange: (c) => set((x) => {
            x.subagents = c;
          })
        }
      ), /* @__PURE__ */ h(
        SlotRow2,
        {
          doc: draft,
          setDraft,
          label: "Cron jobs",
          hint: "scheduled jobs on this profile",
          spec: a.cron,
          live: live.cron,
          nullable: true,
          nullLabel: "Hermes default",
          filter: toolsOk,
          onChange: (c) => set((x) => {
            x.cron = c;
          })
        }
      ), Object.keys(aux).sort((x, y) => x === "vision" ? -1 : y === "vision" ? 1 : x.localeCompare(y)).map((t) => /* @__PURE__ */ h(
        SlotRow2,
        {
          key: t,
          doc: draft,
          setDraft,
          label: TASK_LABEL[t] || t,
          hint: t === "vision" ? "images \u2014 every rung must accept them" : "auxiliary task",
          spec: aux[t],
          live: (live.aux || {})[t] ? live.aux[t].chain : null,
          withReasoning: true,
          filter: t === "vision" ? visionOk : null,
          wantsVision: t === "vision",
          onChange: (c) => set((x) => {
            x.aux[t] = c;
          }),
          onDelete: () => set((x) => {
            delete x.aux[t];
          })
        }
      )), unusedTasks.length ? /* @__PURE__ */ h("div", { className: "fm-slot fm-slot--add" }, /* @__PURE__ */ h("div", { className: "fm-slot-label" }, "Add helper"), /* @__PURE__ */ h("div", { className: "fm-slot-body" }, /* @__PURE__ */ h("select", { value: "", onChange: (e) => e.target.value && set((x) => {
        x.aux = x.aux || {};
        x.aux[e.target.value] = [e.target.value === "vision" ? "glm" in draft.models ? "glm" : Object.keys(draft.models)[0] : Object.keys(draft.models)[0]];
      }) }, /* @__PURE__ */ h("option", { value: "" }, "+ auxiliary task\u2026"), unusedTasks.map((t) => /* @__PURE__ */ h("option", { key: t, value: t }, TASK_LABEL[t] || t))), /* @__PURE__ */ h("span", { className: "fm-muted" }, " tasks left unset use Hermes' automatic routing"))) : null)), /* @__PURE__ */ h("div", { className: "fm-two" }, /* @__PURE__ */ h(Section2, { title: "Agent defaults" }, /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Default reasoning effort"), /* @__PURE__ */ h("select", { value: a.reasoning || "", onChange: (e) => set((x) => {
        x.reasoning = e.target.value || null;
      }) }, REASONING.map((r) => /* @__PURE__ */ h("option", { key: r, value: r }, r || "Hermes default"))), /* @__PURE__ */ h("small", null, "Per-model pins (Models & hosts) win \u2014 e.g. v4.1 always runs high.")), /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Why this setup"), /* @__PURE__ */ h("input", { value: a.why || "", onChange: (e) => set((x) => {
        x.why = e.target.value;
      }) }), /* @__PURE__ */ h("small", null, "Shown in Smith's SOUL model table.")), p === "root" ? /* @__PURE__ */ h("label", { className: "fm-check" }, /* @__PURE__ */ h("input", { type: "checkbox", checked: !!a.locked, onChange: (e) => set((x) => {
        x.locked = e.target.checked;
      }) }), " Locked (Smith is the overwatch \u2014 changes need an explicit unlock)") : null), /* @__PURE__ */ h(Section2, { title: "Profile default OpenRouter routing" }, /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, "Applies to this agent's OpenRouter calls for models without their own host pins. Models with pins (Models & hosts) override it wherever they run. ", /* @__PURE__ */ h("b", null, "data_collection: deny"), " is always on."), /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Prefer hosts (order)"), /* @__PURE__ */ h(
        "input",
        {
          defaultValue: listInput2((a.routing || {}).order),
          key: "o" + p + draft.revision,
          onBlur: (e) => set((x) => {
            x.routing = { ...x.routing || {}, order: parseList2(e.target.value) };
          })
        }
      )), /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Never use (ignore)"), /* @__PURE__ */ h(
        "input",
        {
          defaultValue: listInput2((a.routing || {}).ignore),
          key: "i" + p + draft.revision,
          onBlur: (e) => set((x) => {
            x.routing = { ...x.routing || {}, ignore: parseList2(e.target.value) };
          })
        }
      )), /* @__PURE__ */ h("label", { className: "fm-check" }, /* @__PURE__ */ h(
        "input",
        {
          type: "checkbox",
          checked: !!(a.routing || {}).require_parameters,
          onChange: (e) => set((x) => {
            x.routing = { ...x.routing || {}, require_parameters: e.target.checked };
          })
        }
      ), " Only hosts that support every request parameter (require_parameters)"))));
    }, usedBy2 = function(doc, alias) {
      const out = [];
      Object.entries(doc.agents || {}).forEach(([p, a]) => {
        ["main", "subagents", "cron"].forEach((s) => {
          const c = chainOf(a[s]);
          const i = c.indexOf(alias);
          if (i >= 0) out.push({ p, slot: s, pos: i });
        });
        Object.entries(a.aux || {}).forEach(([t, s]) => {
          const i = chainOf(s).indexOf(alias);
          if (i >= 0) out.push({ p, slot: t, pos: i });
        });
      });
      return out;
    }, HostTable2 = function({ model, market, onChange, onProbe, probes, minUptime }) {
      const hosts = model.hosts || {};
      const pinned = hosts.order || hosts.only || [];
      const restricted = !!(hosts.only && hosts.only.length);
      const eps = market && market.endpoints || [];
      const byTag = {};
      eps.forEach((e) => {
        byTag[e.tag] = e;
      });
      const find = (t) => byTag[t] || eps.find((e) => hostSlug(e.tag) === t);
      const rows = pinned.map((t) => ({ tag: t, ep: find(t), pinned: true })).concat(eps.filter((e) => !pinned.some((t) => t === e.tag || t === hostSlug(e.tag) && !byTag[t])).map((e) => ({ tag: e.tag, ep: e, pinned: false })));
      const write = (list, only) => onChange({ ...hosts, order: list.length ? list : void 0, only: only && list.length ? list : void 0 });
      const move = (i, d) => {
        const l = pinned.slice();
        const [x] = l.splice(i, 1);
        l.splice(i + d, 0, x);
        write(l, restricted);
      };
      const why = (ep) => {
        if (!ep) return "not listed right now \u2014 a pin that matches nothing fails SILENTLY";
        if (ep.tools === false) return "no tool calling";
        if (minUptime && ep.uptime_1d != null && ep.uptime_1d < minUptime) return `uptime ${pct(ep.uptime_1d)} < ${minUptime}%`;
        if (ep.status != null && ep.status < 0) return "degraded right now";
        return null;
      };
      return /* @__PURE__ */ h("div", { className: "fm-hosts" }, /* @__PURE__ */ h("div", { className: "fm-row fm-hosts-bar" }, /* @__PURE__ */ h("label", { className: "fm-check" }, /* @__PURE__ */ h("input", { type: "checkbox", checked: restricted, onChange: (e) => write(pinned, e.target.checked) }), " Only these hosts \u2014 unticked, the order is a preference and other hosts may serve"), /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, market ? market.source === "live" ? "live from OpenRouter \xB7 " + ago(market.fetched_at) : market.source === "snapshot" ? "nightly snapshot \xB7 " + ago(market.fetched_at) : "market data unavailable" : "loading\u2026")), /* @__PURE__ */ h("div", { className: "fm-table-wrap" }, /* @__PURE__ */ h("table", { className: "fm-table" }, /* @__PURE__ */ h("thead", null, /* @__PURE__ */ h("tr", null, /* @__PURE__ */ h("th", null, "#"), /* @__PURE__ */ h("th", null, "Host"), /* @__PURE__ */ h("th", null, "Quant"), /* @__PURE__ */ h("th", { className: "r" }, "In $/M"), /* @__PURE__ */ h("th", { className: "r" }, "Out $/M"), /* @__PURE__ */ h("th", { className: "r" }, "Cache $/M"), /* @__PURE__ */ h("th", { className: "r" }, "Uptime 1d"), /* @__PURE__ */ h("th", { className: "r" }, "30m"), /* @__PURE__ */ h("th", null, "Tools"), /* @__PURE__ */ h("th", { className: "r" }, "p50 ms"), /* @__PURE__ */ h("th", { className: "r" }, "tok/s"), /* @__PURE__ */ h("th", null))), /* @__PURE__ */ h("tbody", null, rows.map((r, i) => {
        const ep = r.ep || {};
        const warn = why(r.ep);
        const pr = probes[r.tag];
        return /* @__PURE__ */ h("tr", { key: r.tag, className: cls2(r.pinned ? "is-pinned" : "is-other", warn && r.pinned && "is-warn") }, /* @__PURE__ */ h("td", { className: "fm-order" }, r.pinned ? /* @__PURE__ */ h("span", { className: "fm-row" }, /* @__PURE__ */ h("b", null, i + 1), /* @__PURE__ */ h("button", { className: "fm-mini", disabled: i === 0, onClick: () => move(i, -1) }, "\u25B2"), /* @__PURE__ */ h("button", { className: "fm-mini", disabled: i === pinned.length - 1, onClick: () => move(i, 1) }, "\u25BC"), /* @__PURE__ */ h("button", { className: "fm-mini", title: "unpin", onClick: () => write(pinned.filter((t) => t !== r.tag), restricted) }, "\xD7")) : /* @__PURE__ */ h("button", { className: "fm-mini fm-mini--add", onClick: () => write(pinned.concat([r.tag]), restricted) }, "pin")), /* @__PURE__ */ h("td", null, /* @__PURE__ */ h("div", { className: "fm-host" }, ep.provider || hostSlug(r.tag)), /* @__PURE__ */ h("code", { className: "fm-code" }, r.tag), warn ? /* @__PURE__ */ h("div", { className: "fm-warn-line" }, warn) : null), /* @__PURE__ */ h("td", null, ep.quant && ep.quant !== "unknown" ? ep.quant : "\u2014"), /* @__PURE__ */ h("td", { className: "r" }, price(ep.in)), /* @__PURE__ */ h("td", { className: "r" }, price(ep.out)), /* @__PURE__ */ h("td", { className: "r" }, price(ep.cache_read)), /* @__PURE__ */ h("td", { className: cls2("r", ep.uptime_1d != null && ep.uptime_1d < (minUptime || 95) && "fm-bad") }, pct(ep.uptime_1d)), /* @__PURE__ */ h("td", { className: "r" }, pct(ep.uptime_30m)), /* @__PURE__ */ h("td", null, ep.tools == null ? "\u2014" : ep.tools ? "\u2713" : "\u2717"), /* @__PURE__ */ h("td", { className: "r" }, ep.latency_ms ? Math.round(ep.latency_ms) : "\u2014"), /* @__PURE__ */ h("td", { className: "r" }, ep.tps ? Math.round(ep.tps) : "\u2014"), /* @__PURE__ */ h("td", null, /* @__PURE__ */ h("button", { className: "fm-mini", onClick: () => onProbe(r.tag), disabled: pr === "\u2026", title: "one tiny call pinned to this host with fallbacks off \u2014 proves it routes" }, "probe"), pr && pr !== "\u2026" ? /* @__PURE__ */ h("div", { className: cls2("fm-small", pr.rate_limited ? "fm-warn-line" : pr.routable ? "fm-good" : "fm-bad"), title: pr.error || "" }, pr.rate_limited ? "busy now \xB7 pin matches" : pr.routable ? `served by ${pr.served_by} \xB7 ${pr.latency_ms}ms` : (pr.status || "") + " " + (pr.error || "not routable").slice(0, 60)) : pr === "\u2026" ? /* @__PURE__ */ h("div", { className: "fm-small fm-muted" }, "probing\u2026") : null));
      })))));
    }, ModelDetail2 = function({ draft, alias, setDraft, usage, win }) {
      const m = draft.models[alias];
      const [mk, setMk] = useState(null);
      const [probes, setProbes] = useState({});
      useEffect(() => {
        setMk(null);
        setProbes({});
        if (m && m.provider === "openrouter") fetchJSON(`${API}/market?model=${encodeURIComponent(m.id)}`).then(setMk).catch(() => setMk({ endpoints: [], source: "unavailable" }));
      }, [alias]);
      if (!m) return null;
      const set = (fn) => setDraft((d) => {
        const n = clone(d);
        fn(n.models[alias]);
        return n;
      });
      const probe = (tag) => {
        setProbes((p) => ({ ...p, [tag]: "\u2026" }));
        fetchJSON(`${API}/probe`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: m.id, host: tag }) }).then((r) => setProbes((p) => ({ ...p, [tag]: r }))).catch((e) => setProbes((p) => ({ ...p, [tag]: { routable: false, error: String(e.message || e) } })));
      };
      const uses = usedBy2(draft, alias);
      const rows = (usage && usage.rows || []).filter((r) => r.model === m.id || (m.served_as || []).includes(r.model));
      const hostUse = {};
      rows.forEach((r) => {
        const k = r.host || "?";
        hostUse[k] = hostUse[k] || { calls: 0, billed: 0 };
        hostUse[k].calls += r.calls;
        hostUse[k].billed += r.billed_usd;
      });
      const totalCalls = rows.reduce((a, r) => a + r.calls, 0);
      const ids = [m.id].concat(m.served_as || []);
      const bm = usage && usage.by_model || {};
      const spark = usage && usage.series ? usage.series.map((_, i) => ids.reduce((a, id) => a + (((bm[id] || {}).calls || [])[i] || 0), 0)) : null;
      const ce = m.cap_equivalent || {};
      return /* @__PURE__ */ h("div", { className: "fm-model" }, /* @__PURE__ */ h("header", { className: "fm-model-head" }, /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("h2", null, m.short || alias, " ", /* @__PURE__ */ h("span", { className: cls2("fm-prov", "fm-prov--" + m.provider) }, PROV_LABEL[m.provider] || m.provider + " \xB7 metered")), /* @__PURE__ */ h("code", { className: "fm-code" }, m.id)), /* @__PURE__ */ h("div", { className: "fm-row" }, m.vision ? /* @__PURE__ */ h(Badge2, null, "vision") : /* @__PURE__ */ h(Badge2, { tone: "dim" }, "text-only"), m.tools !== false ? /* @__PURE__ */ h(Badge2, null, "tools") : /* @__PURE__ */ h(Badge2, { tone: "warn" }, "no tools"), m.context ? /* @__PURE__ */ h(Badge2, null, num(m.context), " ctx") : null, /* @__PURE__ */ h(Badge2, null, m.vendor))), /* @__PURE__ */ h("div", { className: "fm-two" }, /* @__PURE__ */ h(Section2, { title: "Settings" }, /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Display name"), /* @__PURE__ */ h("input", { value: m.short || "", onChange: (e) => set((x) => {
        x.short = e.target.value;
      }) })), /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Reasoning pin"), /* @__PURE__ */ h("select", { value: m.reasoning || "", onChange: (e) => set((x) => {
        if (e.target.value) x.reasoning = e.target.value;
        else delete x.reasoning;
      }) }, REASONING.map((r) => /* @__PURE__ */ h("option", { key: r, value: r }, r || "none \u2014 agent default applies"))), /* @__PURE__ */ h("small", null, "Wins over every agent's default, on every surface this model runs (main, fallback, subagents, helpers).")), /* @__PURE__ */ h("div", { className: "fm-row" }, /* @__PURE__ */ h("label", { className: "fm-check" }, /* @__PURE__ */ h("input", { type: "checkbox", checked: !!m.vision, onChange: (e) => set((x) => {
        x.vision = e.target.checked;
      }) }), " accepts images"), /* @__PURE__ */ h("label", { className: "fm-check" }, /* @__PURE__ */ h("input", { type: "checkbox", checked: m.tools !== false, onChange: (e) => set((x) => {
        x.tools = e.target.checked;
      }) }), " tool calling")), /* @__PURE__ */ h("label", { className: "fm-field" }, /* @__PURE__ */ h("span", null, "Notes"), /* @__PURE__ */ h("textarea", { rows: 2, value: m.notes || "", onChange: (e) => set((x) => {
        x.notes = e.target.value;
      }) }), /* @__PURE__ */ h("small", null, "One line of standing fact. Anything dated \u2014 a measurement, a routing call, a trap \u2014 belongs in the decision log at the foot of this page, where it carries its date and who decided it."))), /* @__PURE__ */ h(
        Section2,
        {
          title: m.provider === "modelark" ? "Pricing \u2014 cap-equivalent" : "Usage \xB7 " + periodShort(win),
          right: spark && totalCalls ? /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, num(totalCalls), " calls \xB7 ", lastLabel(win).toLowerCase()) : null
        },
        m.provider === "modelark" ? /* @__PURE__ */ h(Fragment, null, /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, "The Coding Plan reports no cost, so calls record ", /* @__PURE__ */ h("b", null, '$0 "modelark subscription"'), ". The $1 card cap still counts these rates per million tokens, so a runaway worker trips it. Changes reach the cap on the next call \u2014 no deploy."), /* @__PURE__ */ h("div", { className: "fm-row" }, ["input", "output", "cache_read"].map((k) => /* @__PURE__ */ h("label", { key: k, className: "fm-field fm-field--num" }, /* @__PURE__ */ h("span", null, k.replace("_", " "), " $/M"), /* @__PURE__ */ h(
          "input",
          {
            type: "number",
            step: "0.001",
            min: "0",
            value: ce[k] == null ? "" : ce[k],
            onChange: (e) => set((x) => {
              x.cap_equivalent = { ...x.cap_equivalent || {}, [k]: e.target.value === "" ? null : Number(e.target.value) };
            })
          }
        ))))) : null,
        m.provider === "modelark" ? /* @__PURE__ */ h("div", { className: "fm-subhead" }, "Usage \xB7 ", periodShort(win), totalCalls ? " \xB7 " + num(totalCalls) + " calls" : "") : null,
        spark && totalCalls ? /* @__PURE__ */ h(Spark2, { values: spark, height: 30, label: `${m.short || alias}: calls over the last ${periodName(win)}` }) : null,
        /* @__PURE__ */ h("div", { className: "fm-hostuse" }, Object.entries(hostUse).sort((a, b) => b[1].calls - a[1].calls).map(([host, u]) => /* @__PURE__ */ h("div", { key: host, className: "fm-bar-row" }, /* @__PURE__ */ h("span", { className: "fm-bar-label" }, host), /* @__PURE__ */ h("span", { className: "fm-bar" }, /* @__PURE__ */ h("span", { style: { width: (totalCalls ? 100 * u.calls / totalCalls : 0) + "%" } })), /* @__PURE__ */ h("span", { className: "fm-bar-val" }, num(u.calls), " \xB7 ", money(u.billed, 3)))), !usage ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "Loading usage\u2026") : !totalCalls ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "No calls in this window.") : null)
      )), m.provider === "openrouter" ? /* @__PURE__ */ h(Section2, { title: "Hosts", right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, (m.rules || {}).first_host ? `rule: ${m.rules.first_host} first` : "", (m.rules || {}).min_uptime ? ` \xB7 later hosts \u2265 ${m.rules.min_uptime}% uptime` : "") }, /* @__PURE__ */ h(
        HostTable2,
        {
          model: m,
          market: mk,
          probes,
          onProbe: probe,
          minUptime: (m.rules || {}).min_uptime || (draft.policy || {}).min_host_uptime,
          onChange: (hosts) => set((x) => {
            const hh = { ...hosts };
            Object.keys(hh).forEach((k) => hh[k] === void 0 && delete hh[k]);
            x.hosts = hh;
          })
        }
      )) : null, /* @__PURE__ */ h(Section2, { title: `Used by ${uses.length} slot${uses.length === 1 ? "" : "s"}` }, /* @__PURE__ */ h("div", { className: "fm-uses" }, uses.map((u, i) => /* @__PURE__ */ h("span", { key: i, className: "fm-use" }, /* @__PURE__ */ h("b", null, draft.agents[u.p].name || u.p), " ", SLOT_LABEL[u.slot] || TASK_LABEL[u.slot] || u.slot, " ", /* @__PURE__ */ h("span", { className: "fm-muted" }, "#", u.pos + 1))), !uses.length ? /* @__PURE__ */ h("span", { className: "fm-muted" }, "Not in any waterfall. ", /* @__PURE__ */ h("button", { className: "fm-link fm-link--danger", onClick: () => setDraft((d) => {
        const n = clone(d);
        delete n.models[alias];
        return n;
      }) }, "Remove from registry")) : null)), /* @__PURE__ */ h(
        DecisionLog2,
        {
          entries: m.decisions,
          title: `Decision log \u2014 ${m.short || alias}`,
          empty: "Nothing recorded for this model yet. Entries are added in fleet/models.yaml under the model's `decisions:` list and travel with it through preview, apply and revert like any other change."
        }
      ));
    }, AddModel2 = function({ draft, setDraft, onAdded }) {
      const [id, setId] = useState("");
      const [info, setInfo] = useState(null);
      const [busy, setBusy] = useState(false);
      const look = () => {
        if (!id.trim()) return;
        setBusy(true);
        fetchJSON(`${API}/market?model=${encodeURIComponent(id.trim())}&fresh=true`).then((r) => {
          setInfo(r);
          setBusy(false);
        }).catch((e) => {
          setInfo({ error: String(e.message || e) });
          setBusy(false);
        });
      };
      const add = () => {
        const mid = id.trim();
        const alias = mid.split("/").pop().toLowerCase().replace(/[^a-z0-9]+/g, "-");
        const eps = info && info.endpoints || [];
        setDraft((d) => {
          const n = clone(d);
          n.models[alias in n.models ? alias + "-2" : alias] = {
            id: mid,
            short: mid.split("/").pop(),
            provider: "openrouter",
            vendor: mid.split("/")[0],
            billing: "metered",
            tools: eps.some((e) => e.tools),
            vision: /image/.test(info && info.modality || ""),
            context: Math.max(0, ...eps.map((e) => e.ctx || 0)) || void 0,
            notes: ""
          };
          return n;
        });
        onAdded(alias);
        setId("");
        setInfo(null);
      };
      return /* @__PURE__ */ h("div", { className: "fm-addmodel" }, /* @__PURE__ */ h("input", { placeholder: "OpenRouter model id, e.g. qwen/qwen3.8-27b", value: id, onChange: (e) => {
        setId(e.target.value);
        setInfo(null);
      }, onKeyDown: (e) => e.key === "Enter" && look() }), /* @__PURE__ */ h("button", { className: "fm-btn", onClick: look, disabled: busy || !id.trim() }, busy ? "checking\u2026" : "Look up"), info ? info.endpoints && info.endpoints.length ? /* @__PURE__ */ h("span", { className: "fm-row" }, /* @__PURE__ */ h("span", { className: "fm-good fm-small" }, info.endpoints.length, " hosts \xB7 ", info.modality), /* @__PURE__ */ h("button", { className: "fm-btn fm-btn--primary", onClick: add }, "Add to registry")) : /* @__PURE__ */ h("span", { className: "fm-bad fm-small" }, info.error || "no endpoints for that id") : null);
    }, ModelsView2 = function({ draft, setDraft, usage, win, sel, setSel }) {
      const aliases = Object.keys(draft.models || {});
      const cur = sel && draft.models[sel] ? sel : aliases[0];
      return /* @__PURE__ */ h("div", { className: "fm-models" }, /* @__PURE__ */ h("aside", { className: "fm-model-list" }, /* @__PURE__ */ h("div", { className: "fm-model-items", role: "tablist", "aria-label": "Models" }, aliases.map((a) => {
        const m = draft.models[a];
        return /* @__PURE__ */ h("button", { key: a, role: "tab", "aria-selected": a === cur, className: cls2("fm-model-item", a === cur && "is-active"), onClick: () => setSel(a) }, /* @__PURE__ */ h("span", { className: cls2("fm-pill-dot", "fm-dot--" + m.provider) }), /* @__PURE__ */ h("span", { className: "fm-model-item-name" }, m.short || a), /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, usedBy2(draft, a).length, " slots"));
      })), /* @__PURE__ */ h("details", { className: "fm-model-list-foot" }, /* @__PURE__ */ h("summary", null, "+ Add a model"), /* @__PURE__ */ h(AddModel2, { draft, setDraft, onAdded: setSel }))), /* @__PURE__ */ h("div", { className: "fm-model-main" }, cur ? /* @__PURE__ */ h(ModelDetail2, { key: cur, draft, alias: cur, setDraft, usage, win }) : null));
    }, hrs2 = function(v) {
      if (v == null) return "\u2014";
      if (v < 1) return Math.round(v * 60) + " min";
      if (v < 48) return v.toFixed(v < 10 ? 1 : 0) + "h";
      return Math.round(v / 24) + "d";
    }, HomeView2 = function({ state, win }) {
      const [d, setD] = useState(null);
      const [err, setErr] = useState(null);
      useEffect(() => {
        let live = true;
        setErr(null);
        fetchJSON(`${API}/home?window=${win}&tz=${encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC")}`).then((r) => {
          if (live) setD(r);
        }).catch((e) => {
          if (live) setErr(String(e));
        });
        return () => {
          live = false;
        };
      }, [win]);
      if (err) return /* @__PURE__ */ h("div", { className: "fm-note fm-note--warn" }, "Home could not load: ", err);
      if (!d) return /* @__PURE__ */ h("div", { className: "fm-muted" }, "Loading\u2026");
      const C = d.cards || {};
      const cap = d.cap_pressure || {};
      const bal = d.balances || {};
      const or = bal.openrouter || {}, ds = bal.deepseek || {}, ma = bal.modelark || {};
      const next = (cap.cards || []).find((c) => c.eta_h != null) || (cap.cards || [])[0];
      const pots = [["OpenRouter", or.runway_h, or.balance_usd], ["DeepSeek", null, ds.balance_usd]].filter((x) => x[1] != null);
      const soonest = pots.length ? pots.reduce((a, b) => b[1] < a[1] ? b : a) : null;
      return /* @__PURE__ */ h(Fragment, null, (d.warnings || []).length ? /* @__PURE__ */ h(Section2, { title: `Needs you \u2014 ${d.warnings.length}`, className: "fm-warnblock" }, d.warnings.map((w, i) => /* @__PURE__ */ h("div", { key: i, className: cls2("fm-note", w.level === "bad" ? "fm-note--warn" : "fm-note--warn") }, /* @__PURE__ */ h("b", null, w.what), " \u2014 ", w.detail))) : /* @__PURE__ */ h("div", { className: "fm-note fm-note--ok" }, "Nothing needs you. No rule breach, no drifted config, no card over its cap, no provider running dry."), /* @__PURE__ */ h("div", { className: "fm-stats" }, /* @__PURE__ */ h(
        Stat2,
        {
          label: "Money runs out in",
          tone: or.runway_h != null && or.runway_h < 48 ? "warn" : "or",
          value: hrs2(or.runway_h),
          title: or.binding ? `binding: the ${or.binding} \xB7 burn ${money(or.burn_usd_per_h, 3)}/h over ${or.window_hours}h` : "",
          sub: soonest ? `${money(soonest[2], 2)} on ${soonest[0]} \xB7 ${or.binding || "credit"}` : "no balance readable"
        }
      ), /* @__PURE__ */ h(
        Stat2,
        {
          label: "Nearest cap",
          tone: next && next.over ? "warn" : "sub",
          value: next ? next.over ? "over" : hrs2(next.eta_h) : "no card running",
          title: next ? `${next.id} \xB7 ${next.assignee} \xB7 ${money(next.spend_usd, 2)} of ${money(next.cap_usd, 2)}` : "",
          sub: next ? `${next.assignee} on ${next.id} \xB7 ${money(next.spend_usd, 2)} of ${money(next.cap_usd, 2)}` : `cap ${money(cap.base_usd, 2)} per worker, ${money(cap.ceiling_usd, 2)} extended`
        }
      ), /* @__PURE__ */ h(
        Stat2,
        {
          label: `Cost per card \xB7 ${periodShort(win)}`,
          tone: "money",
          value: C.avg_usd == null ? "\u2014" : money(C.avg_usd, 3),
          title: "mean across every card with a session in the window; subscription work counted at its cap-equivalent, not at the $0 it is invoiced",
          sub: C.cards ? `${num(C.cards)} cards \xB7 median ${money(C.median_usd, 3)}` : "no cards in this window"
        }
      ), /* @__PURE__ */ h(
        Stat2,
        {
          label: `Tokens per card \xB7 ${periodShort(win)}`,
          value: tok(C.avg_tokens),
          title: "input + output + cache reads. Cache reads dominate by design \u2014 that is the saving working, not waste.",
          sub: C.usd_per_mtok == null ? "\u2014" : money(C.usd_per_mtok, 4) + " per million tokens"
        }
      ), /* @__PURE__ */ h(
        Stat2,
        {
          label: `Most used \xB7 ${periodShort(win)}`,
          value: d.most_used_model ? d.most_used_model.split("/").pop() : "\u2014",
          title: d.most_used_model || "",
          sub: d.most_used_share_pct != null ? d.most_used_share_pct + "% of all calls" : ""
        }
      ), /* @__PURE__ */ h(
        Stat2,
        {
          label: "Most efficient",
          tone: "ok",
          value: d.best ? money(d.best.usd_per_moutput, 2) : "\u2014",
          title: d.best ? `${d.best.model} \u2014 ${money(d.best.spend_usd, 3)} for ${tok(d.best.output)} output tokens` : "",
          sub: d.best ? `${d.best.model.split("/").pop()} \xB7 per M output` : "not enough output to rank"
        }
      )), /* @__PURE__ */ h("div", { className: "fm-two" }, /* @__PURE__ */ h(
        Section2,
        {
          title: "Running now \u2014 cap pressure",
          right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, money(cap.base_usd, 2), " per worker \xB7 ", money(cap.ceiling_usd, 2), " with the one extension")
        },
        !(cap.cards || []).length ? /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, "No card is running.") : null,
        (cap.cards || []).map((c) => /* @__PURE__ */ h("div", { key: c.id, className: "fm-bar-row", title: c.title }, /* @__PURE__ */ h("span", { className: "fm-bar-label" }, c.assignee, " \xB7 ", c.id), /* @__PURE__ */ h("span", { className: "fm-bar" }, /* @__PURE__ */ h("span", { className: cls2(c.over && "is-over"), style: { width: Math.min(100, c.pct || 0) + "%" } })), /* @__PURE__ */ h("span", { className: "fm-bar-val" }, money(c.spend_usd, 2), " / ", money(c.cap_usd, 2), c.burn_usd_per_h == null ? /* @__PURE__ */ h("span", { className: "fm-muted" }, " \xB7 no spend recorded yet") : /* @__PURE__ */ h("span", { className: "fm-muted" }, " \xB7 ", money(c.burn_usd_per_h, 2), "/h \xB7 ", hrs2(c.eta_h))))),
        /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, "Measured against each card's own assignee ledger, which is what the cap gate itself reads \u2014 so this agrees with the gate rather than approximating it. A card showing no spend has no session row yet, which is not the same as costing nothing.")
      ), /* @__PURE__ */ h(Section2, { title: `This window's cards \xB7 ${periodShort(win)}` }, /* @__PURE__ */ h("div", { className: "fm-kv" }, /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("span", null, "Cards worked"), /* @__PURE__ */ h("b", null, num(C.cards))), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("span", null, "Cards with more than one worker"), /* @__PURE__ */ h("b", null, num(C.multi_worker_cards))), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("span", null, "Invoiced"), /* @__PURE__ */ h("b", null, money(C.billed_usd, 2))), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("span", null, "Subscription, at cap-equivalent"), /* @__PURE__ */ h("b", null, money(C.capeq_usd, 2))), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("span", null, "Mean per card"), /* @__PURE__ */ h("b", null, money(C.avg_usd, 3))), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("span", null, "Median per card"), /* @__PURE__ */ h("b", null, money(C.median_usd, 3)))), C.dearest ? /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, "Dearest card this window: ", /* @__PURE__ */ h("b", null, C.dearest.id), " at ", money(C.dearest.usd, 2), " across ", C.dearest.workers.join(", "), ".", C.dearest.workers.length > 1 ? " Multiple workers, so the per-worker cap applies to each of them separately \u2014 a pooled figure above $1 is not a breach." : "") : null, ma.exhausted ? /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, "ModelArk's 5-hour quota is exhausted", ma.reset_at ? `, resets ${ma.reset_at}` : "", " \u2014 flash traffic is falling through to DeepSeek direct.") : null)), /* @__PURE__ */ h(
        Section2,
        {
          title: `Efficiency \xB7 ${periodShort(win)}`,
          right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, "dollars per million OUTPUT tokens \u2014 lower is better")
        },
        /* @__PURE__ */ h("p", { className: "fm-muted fm-small" }, /* @__PURE__ */ h("b", null, "The calculation:"), " (invoiced + cap-equivalent) \xF7 output tokens \xD7 1,000,000. Output is the work; input and cache reads are what it cost to get there, so a model that reads a big cached prefix cheaply scores well for it \u2014 which is the behaviour worth rewarding. Two choices that change the ranking, stated so they can be argued with: a ", /* @__PURE__ */ h("b", null, "subscription rung is scored on its cap-equivalent, not the $0 it is invoiced"), " (at $0 it would be infinitely efficient and this column would mean nothing), and a model needs ", /* @__PURE__ */ h("b", null, tok(d.eff_min_output), " output tokens"), " in the window to be ranked at all, or three lucky calls beat a workhorse."),
        /* @__PURE__ */ h("table", { className: "fm-table" }, /* @__PURE__ */ h("thead", null, /* @__PURE__ */ h("tr", null, /* @__PURE__ */ h("th", null, "Model"), /* @__PURE__ */ h("th", { className: "fm-num" }, "$ / M output"), /* @__PURE__ */ h("th", { className: "fm-num" }, "Output"), /* @__PURE__ */ h("th", { className: "fm-num" }, "Calls"), /* @__PURE__ */ h("th", { className: "fm-num" }, "Cache hit"), /* @__PURE__ */ h("th", { className: "fm-num" }, "Spend"), /* @__PURE__ */ h("th", null, "Basis"))), /* @__PURE__ */ h("tbody", null, (d.efficiency || []).map((e) => /* @__PURE__ */ h("tr", { key: e.model, className: cls2(!e.ranked && "is-dim") }, /* @__PURE__ */ h("td", null, e.model), /* @__PURE__ */ h("td", { className: "fm-num" }, e.usd_per_moutput == null ? "\u2014" : money(e.usd_per_moutput, 2)), /* @__PURE__ */ h("td", { className: "fm-num" }, tok(e.output)), /* @__PURE__ */ h("td", { className: "fm-num" }, num(e.calls)), /* @__PURE__ */ h("td", { className: "fm-num" }, e.cache_hit_pct == null ? "\u2014" : e.cache_hit_pct + "%"), /* @__PURE__ */ h("td", { className: "fm-num" }, money(e.spend_usd, 3)), /* @__PURE__ */ h("td", null, /* @__PURE__ */ h("span", { className: cls2("fm-tag", e.basis === "cap-equivalent" && "fm-tag--sub") }, e.basis), !e.ranked ? /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, " \xB7 too little output to rank") : null)))))
      ), /* @__PURE__ */ h(
        DecisionLog2,
        {
          entries: state.doc && state.doc.decisions || [],
          title: "Fleet decisions",
          empty: "No fleet-level decisions recorded."
        }
      ));
    }, CostsView2 = function({ doc, usage, win, width, balances }) {
      if (!usage) return /* @__PURE__ */ h("div", { className: "fm-muted" }, "Loading usage\u2026");
      const rows = usage.rows || [];
      const byAgent = {};
      rows.forEach((r) => {
        (byAgent[r.profile] = byAgent[r.profile] || []).push(r);
      });
      const tot = rows.reduce((a, r) => ({ billed: a.billed + r.billed_usd, calls: a.calls + r.calls, ma: a.ma + (r.modelark ? r.calls : 0), cap: a.cap + r.cap_equivalent_usd, tin: a.tin + r.input, tout: a.tout + r.output }), { billed: 0, calls: 0, ma: 0, cap: 0, tin: 0, tout: 0 });
      const T = usage && usage.totals || {};
      const idToShort = {};
      Object.values(doc.models || {}).forEach((m) => {
        idToShort[m.id] = m.short;
        (m.served_as || []).forEach((s) => {
          idToShort[s] = m.short;
        });
      });
      return /* @__PURE__ */ h(Fragment, null, /* @__PURE__ */ h("div", { className: "fm-stats" }, /* @__PURE__ */ h(Stat2, { label: `Cost \xB7 ${periodShort(win)}`, value: money(tot.billed, 2), tone: "money", sub: costSub(T) }), /* @__PURE__ */ h(
        Stat2,
        {
          label: `Tokens \xB7 ${periodShort(win)}`,
          value: tok(T.tokens),
          sub: `${tok(T.input)} in \xB7 ${tok(T.output)} out \xB7 ${tok(T.cache_read)} cached`
        }
      ), /* @__PURE__ */ h(
        Stat2,
        {
          label: "Cache hit",
          value: T.cache_hit_pct == null ? "\u2014" : T.cache_hit_pct + "%",
          tone: "ok",
          sub: `${tok(T.cache_read)} of ${tok((T.input || 0) + (T.cache_read || 0))} prompt tokens`
        }
      ), /* @__PURE__ */ h(Stat2, { label: "ModelArk subscription", value: num(tot.ma) + " calls", tone: "sub", sub: "$0 \xB7 cap-equivalent " + money(tot.cap, 2) }), /* @__PURE__ */ h(Stat2, { label: "All calls", value: num(tot.calls), sub: payerSub(T) })), /* @__PURE__ */ h(BalanceStats2, { balances, T }), /* @__PURE__ */ h(Section2, { title: "Over time", right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, bucketName(usage.bucket), " bars \xB7 tap or hover a bar") }, /* @__PURE__ */ h(TimeChart2, { usage, width }), /* @__PURE__ */ h("p", { className: "fm-muted fm-small fm-tc-foot" }, "From all nine ledgers. Each session's usage is spread evenly between its first and last call, so short windows are close estimates. Faded bars are part-way through.")), /* @__PURE__ */ h("div", { className: "fm-two" }, /* @__PURE__ */ h(Section2, { title: `Spend by agent \xB7 ${periodShort(win)}`, right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, "bar = billed $ \xB7 teal = subscription calls") }, /* @__PURE__ */ h(BarList2, { rows: Object.entries(byAgent).map(([p, rs]) => ({
        label: (doc.agents[p] || {}).name || p,
        billed: rs.reduce((a, r) => a + r.billed_usd, 0),
        calls: rs.reduce((a, r) => a + r.calls, 0),
        ma: rs.reduce((a, r) => a + (r.modelark ? r.calls : 0), 0)
      })) })), /* @__PURE__ */ h(Section2, { title: `Spend by model \xB7 ${periodShort(win)}`, right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, "old OpenRouter DeepSeek ids are history from before 09-11") }, /* @__PURE__ */ h(BarList2, { rows: Object.values(rows.reduce((acc, r) => {
        const k = idToShort[r.model] || r.model;
        const a = acc[k] = acc[k] || { label: k, billed: 0, calls: 0, ma: 0 };
        a.billed += r.billed_usd;
        a.calls += r.calls;
        if (r.modelark) a.ma += r.calls;
        return acc;
      }, {})) }))), /* @__PURE__ */ h(Section2, { title: `By agent, model and host \xB7 ${periodShort(win)}` }, /* @__PURE__ */ h("div", { className: "fm-table-wrap" }, /* @__PURE__ */ h("table", { className: "fm-table" }, /* @__PURE__ */ h("thead", null, /* @__PURE__ */ h("tr", null, /* @__PURE__ */ h("th", null, "Agent"), /* @__PURE__ */ h("th", null, "Model"), /* @__PURE__ */ h("th", null, "Served by"), /* @__PURE__ */ h("th", null, "Where"), /* @__PURE__ */ h("th", { className: "r" }, "Calls"), /* @__PURE__ */ h("th", { className: "r" }, "In"), /* @__PURE__ */ h("th", { className: "r" }, "Out"), /* @__PURE__ */ h("th", { className: "r" }, "Cache read"), /* @__PURE__ */ h("th", { className: "r" }, "Hit %"), /* @__PURE__ */ h("th", { className: "r" }, "Cost"), /* @__PURE__ */ h("th", { className: "r" }, "Cap-equiv."))), /* @__PURE__ */ h("tbody", null, Object.entries(byAgent).map(([p, rs]) => rs.map((r, i) => /* @__PURE__ */ h("tr", { key: p + i }, /* @__PURE__ */ h("td", null, i === 0 ? /* @__PURE__ */ h("b", null, (doc.agents[p] || {}).name || p) : null), /* @__PURE__ */ h("td", null, idToShort[r.model] || /* @__PURE__ */ h("code", { className: "fm-code" }, r.model)), /* @__PURE__ */ h("td", null, r.modelark ? /* @__PURE__ */ h("span", { className: "fm-prov fm-prov--modelark" }, "modelark subscription") : r.host || (r.payer && r.payer !== "openrouter" ? /* @__PURE__ */ h("span", { className: cls2("fm-prov", "fm-prov--" + r.payer) }, r.payer, " direct") : "\u2014")), /* @__PURE__ */ h("td", { className: "fm-muted" }, r.task === "main" ? "main" : TASK_LABEL[r.task] || r.task), /* @__PURE__ */ h("td", { className: "r" }, num(r.calls)), /* @__PURE__ */ h("td", { className: "r" }, num(r.input)), /* @__PURE__ */ h("td", { className: "r" }, num(r.output)), /* @__PURE__ */ h("td", { className: "r" }, num(r.cache_read)), /* @__PURE__ */ h("td", { className: "r fm-muted" }, r.cache_hit_pct == null ? "\u2014" : r.cache_hit_pct + "%"), /* @__PURE__ */ h("td", { className: "r" }, r.modelark ? "$0" : /* @__PURE__ */ h(Fragment, null, money(r.billed_usd), r.invoiced === false ? /* @__PURE__ */ h("span", { className: "fm-tag--est", title: "metered, but the provider returns no per-call cost \u2014 Hermes estimates it from the published rate card" }, "est") : null)), /* @__PURE__ */ h("td", { className: "r fm-muted" }, r.modelark ? money(r.cap_equivalent_usd) : "")))))))));
    }, BarList2 = function({ rows }) {
      const sorted = rows.slice().sort((a, b) => b.billed - a.billed || b.calls - a.calls);
      const max = Math.max(1e-6, ...sorted.map((r) => r.billed));
      return /* @__PURE__ */ h("div", { className: "fm-barlist" }, sorted.map((r) => /* @__PURE__ */ h("div", { key: r.label, className: "fm-bl-row", title: `${r.label}: ${money(r.billed)} billed \xB7 ${r.calls} calls (${r.ma} on subscription)` }, /* @__PURE__ */ h("span", { className: "fm-bl-label" }, r.label), /* @__PURE__ */ h("span", { className: "fm-bl-track" }, /* @__PURE__ */ h("span", { className: "fm-bl-money", style: { width: 100 * r.billed / max + "%" } })), /* @__PURE__ */ h("span", { className: "fm-bl-val" }, /* @__PURE__ */ h("b", null, money(r.billed, 2)), " ", /* @__PURE__ */ h("span", { className: "fm-muted" }, "\xB7 ", num(r.calls), " calls", r.ma ? /* @__PURE__ */ h(Fragment, null, " \xB7 ", /* @__PURE__ */ h("span", { className: "fm-sub-txt" }, num(r.ma), " sub")) : null)))), !sorted.length ? /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "No calls in this window.") : null);
    }, DecisionsView2 = function({ state, draft, setDraft, onRevert }) {
      const pol = draft.policy || {};
      const [what, setWhat] = useState("");
      const [why, setWhy] = useState("");
      return /* @__PURE__ */ h("div", { className: "fm-two fm-two--wide" }, /* @__PURE__ */ h("div", null, /* @__PURE__ */ h(Section2, { title: "Standing rules" }, /* @__PURE__ */ h("div", { className: "fm-rule" }, /* @__PURE__ */ h(Badge2, { tone: "ok" }, "locked"), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("b", null, "data_collection: deny on every OpenRouter call"), /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "The no-training rule. Enforced by the compiler on every config and by the plugin on every helper call \u2014 not editable here."))), /* @__PURE__ */ h("div", { className: "fm-rule" }, /* @__PURE__ */ h(Badge2, { tone: "ok" }, "read-only"), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("b", null, "Cost caps"), " \u2014 ", Object.entries(state.caps || {}).map(([k, v]) => `${k.replace(/_/g, " ")} $${v}`).join(" \xB7 ") || "see config", /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "Richie's alone. Shown, never edited, from this tab."))), /* @__PURE__ */ h("div", { className: "fm-rule" }, /* @__PURE__ */ h(Badge2, null, "policy"), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("b", null, "Hosts after the rule-pinned first host need \u2265 "), /* @__PURE__ */ h(
        "input",
        {
          className: "fm-inline-num",
          type: "number",
          min: "50",
          max: "100",
          value: pol.min_host_uptime || 95,
          onChange: (e) => setDraft((d) => {
            const n = clone(d);
            n.policy = { ...n.policy || {}, min_host_uptime: Number(e.target.value) };
            return n;
          })
        }
      ), /* @__PURE__ */ h("b", null, "% uptime"), /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, "Binding where a model carries the rule (v4.1); advice elsewhere.")))), /* @__PURE__ */ h(Section2, { title: "Decisions" }, /* @__PURE__ */ h("ul", { className: "fm-decisions" }, (draft.decisions || []).map((d, i) => /* @__PURE__ */ h("li", { key: i }, /* @__PURE__ */ h("span", { className: "fm-date" }, String(d.date)), /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("b", null, d.what), /* @__PURE__ */ h("div", { className: "fm-muted" }, d.why)), /* @__PURE__ */ h("button", { className: "fm-mini", title: "remove", onClick: () => setDraft((x) => {
        const n = clone(x);
        n.decisions.splice(i, 1);
        return n;
      }) }, "\xD7")))), /* @__PURE__ */ h("div", { className: "fm-row fm-adddec" }, /* @__PURE__ */ h("input", { placeholder: "Decision", value: what, onChange: (e) => setWhat(e.target.value) }), /* @__PURE__ */ h("input", { placeholder: "Why", value: why, onChange: (e) => setWhy(e.target.value) }), /* @__PURE__ */ h("button", { className: "fm-btn", disabled: !what.trim(), onClick: () => {
        setDraft((x) => {
          const n = clone(x);
          n.decisions = [{ date: (/* @__PURE__ */ new Date()).toISOString().slice(0, 10), what: what.trim(), why: why.trim() }].concat(n.decisions || []);
          return n;
        });
        setWhat("");
        setWhy("");
      } }, "Add")))), /* @__PURE__ */ h(Section2, { title: "History", right: /* @__PURE__ */ h("span", { className: "fm-muted fm-small" }, "every apply keeps a pre-image of models.yaml, the 9 configs and the SOULs") }, /* @__PURE__ */ h("ul", { className: "fm-history" }, (state.history || []).map((hh) => /* @__PURE__ */ h("li", { key: hh.id }, /* @__PURE__ */ h("div", { className: "fm-row fm-between" }, /* @__PURE__ */ h("span", null, /* @__PURE__ */ h("b", null, hh.summary), " ", /* @__PURE__ */ h("span", { className: "fm-muted" }, "\xB7 ", hh.by, " \xB7 ", ago(hh.ts))), /* @__PURE__ */ h("button", { className: "fm-mini", onClick: () => onRevert(hh) }, "revert")), /* @__PURE__ */ h("div", { className: "fm-muted fm-small" }, hh.revision ? "revision " + hh.revision + " \xB7 " : "", (hh.changed || []).length, " config(s)", hh.reverts ? " \xB7 reverts " + hh.reverts : "", " \xB7 ", /* @__PURE__ */ h("code", null, hh.id)), hh.changes ? /* @__PURE__ */ h("details", null, /* @__PURE__ */ h("summary", { className: "fm-small" }, "what changed"), Object.entries(hh.changes).map(([p, ch]) => /* @__PURE__ */ h("div", { key: p, className: "fm-changes" }, /* @__PURE__ */ h("b", null, p), ch.map((c, i) => /* @__PURE__ */ h("div", { key: i }, /* @__PURE__ */ h("code", null, c)))))) : null)), !(state.history || []).length ? /* @__PURE__ */ h("li", { className: "fm-muted" }, "No applies yet.") : null)));
    }, PlanPanel2 = function({ plan, onClose, onApply, applying, needsUnlock, unlock, setUnlock, summary, setSummary }) {
      const changed = plan.changed || [];
      const [open, setOpen] = useState(null);
      return /* @__PURE__ */ h("div", { className: "fm-overlay", onClick: onClose }, /* @__PURE__ */ h("div", { className: "fm-panel", onClick: (e) => e.stopPropagation() }, /* @__PURE__ */ h("header", { className: "fm-panel-head" }, /* @__PURE__ */ h("h3", null, "Preview"), /* @__PURE__ */ h("button", { className: "fm-mini", onClick: onClose }, "close")), plan.errors && plan.errors.length ? /* @__PURE__ */ h("div", { className: "fm-note fm-note--bad" }, /* @__PURE__ */ h("b", null, "Can't apply:"), plan.errors.map((e, i) => /* @__PURE__ */ h("div", { key: i }, e))) : /* @__PURE__ */ h("div", { className: "fm-note fm-note--ok" }, changed.length ? `${changed.length} config file(s) will change: ${changed.join(", ")}.` : "Nothing in the configs changes (registry notes/decisions only).", " Running workers, gateways and cron pick it up on their next call \u2014 no restart."), needsUnlock && !(plan.errors && plan.errors.length) ? /* @__PURE__ */ h("div", { className: "fm-note fm-note--warn" }, /* @__PURE__ */ h("b", null, "Smith is locked."), " This change touches the overwatch agent \u2014 tick ", /* @__PURE__ */ h("b", null, "Unlock Smith for this apply"), " below to allow it.") : null, plan.warnings && plan.warnings.length ? /* @__PURE__ */ h("details", { className: "fm-note fm-note--warn" }, /* @__PURE__ */ h("summary", null, plan.warnings.length, " warning(s)"), plan.warnings.map((w, i) => /* @__PURE__ */ h("div", { key: i }, w))) : null, /* @__PURE__ */ h("div", { className: "fm-plan-list" }, Object.entries(plan.plan || {}).filter(([, v]) => v.changes.length).map(([p, v]) => /* @__PURE__ */ h("div", { key: p, className: "fm-plan-item" }, /* @__PURE__ */ h("button", { className: "fm-plan-toggle", onClick: () => setOpen(open === p ? null : p) }, /* @__PURE__ */ h("b", null, p), " \xB7 ", v.changes.length, " change(s) ", open === p ? "\u25BE" : "\u25B8"), open === p ? /* @__PURE__ */ h(Fragment, null, /* @__PURE__ */ h("div", { className: "fm-changes" }, v.changes.map((c, i) => /* @__PURE__ */ h("div", { key: i }, /* @__PURE__ */ h("code", null, c)))), /* @__PURE__ */ h("pre", { className: "fm-diff" }, v.diff.split("\n").map((l, i) => /* @__PURE__ */ h("span", { key: i, className: l.startsWith("+") && !l.startsWith("+++") ? "fm-add-l" : l.startsWith("-") && !l.startsWith("---") ? "fm-del-l" : "" }, l + "\n")))) : null))), !(plan.errors && plan.errors.length) ? /* @__PURE__ */ h("footer", { className: "fm-panel-foot" }, /* @__PURE__ */ h("input", { className: "fm-summary", placeholder: "What is this change? (goes in the history)", value: summary, onChange: (e) => setSummary(e.target.value) }), needsUnlock ? /* @__PURE__ */ h("label", { className: "fm-check fm-unlock" }, /* @__PURE__ */ h("input", { type: "checkbox", checked: unlock, onChange: (e) => setUnlock(e.target.checked) }), " Unlock Smith for this apply") : null, /* @__PURE__ */ h("button", { className: "fm-btn fm-btn--primary", disabled: applying || needsUnlock && !unlock, onClick: onApply }, applying ? "Applying\u2026" : "Apply to the fleet")) : null));
    }, ModelsPage2 = function() {
      const [state, setState] = useState(null);
      const [err, setErr] = useState(null);
      const [draft, setDraft] = useState(null);
      const [usage, setUsage] = useState(null);
      const [win, setWinState] = useState(readWin);
      const [rootEl, setRootEl] = useState(null);
      const [width, setWidth] = useState(900);
      const [uLoading, setULoading] = useState(false);
      const [uErr, setUErr] = useState(0);
      const retryT = useRef(null);
      const useq = useRef(0);
      const [tab, setTab] = useState("home");
      const [agent, setAgent] = useState("root");
      const [modelSel, setModelSel] = useState(null);
      const [plan, setPlan] = useState(null);
      const [busy, setBusy] = useState(false);
      const [unlock, setUnlock] = useState(false);
      const [summary, setSummary] = useState("");
      const [flash, setFlash] = useState(null);
      const baseRef = useRef(null);
      const load = useCallback(() => fetchJSON(`${API}/state`).then((s) => {
        setState(s);
        setErr(null);
        setDraft((d) => {
          if (!d || !baseRef.current || same(d, baseRef.current)) {
            baseRef.current = clone(s.doc);
            return clone(s.doc);
          }
          return d;
        });
        return s;
      }).catch((e) => setErr(String(e.message || e))), []);
      const setWin = (w) => {
        saveWin(w);
        setWinState(w);
      };
      useEffect(() => {
        if (!rootEl || typeof ResizeObserver === "undefined") return void 0;
        let t = null;
        const ro = new ResizeObserver((es) => {
          const w = Math.round(es[0].contentRect.width);
          clearTimeout(t);
          t = setTimeout(() => setWidth(w), 150);
        });
        ro.observe(rootEl);
        setWidth(Math.round(rootEl.getBoundingClientRect().width));
        return () => {
          ro.disconnect();
          clearTimeout(t);
        };
      }, [rootEl]);
      const chartW = Math.max(240, width - 36);
      const bucket = pickBucket(win, chartW);
      const loadUsage = useCallback(() => {
        const q = ++useq.current;
        setULoading(true);
        clearTimeout(retryT.current);
        return fetchJSON(`${API}/usage?window=${win}&bucket=${bucket}&tz=${encodeURIComponent(TZ)}`).then((u) => {
          if (q === useq.current) {
            setUsage(u);
            setULoading(false);
            setUErr(0);
          }
        }).catch(() => {
          if (q !== useq.current) return;
          setULoading(false);
          setUErr((n) => {
            if (n < 6) retryT.current = setTimeout(() => loadUsageRef.current(), 4e3 * (n + 1));
            return n + 1;
          });
        });
      }, [win, bucket]);
      const loadUsageRef = useRef(loadUsage);
      loadUsageRef.current = loadUsage;
      useEffect(() => {
        const onVis = () => {
          if (!document.hidden) {
            loadUsageRef.current();
            load();
          }
        };
        document.addEventListener("visibilitychange", onVis);
        return () => {
          document.removeEventListener("visibilitychange", onVis);
          clearTimeout(retryT.current);
        };
      }, [load]);
      useEffect(() => {
        load();
      }, []);
      useEffect(() => {
        loadUsage();
        const t = setInterval(() => {
          if (!document.hidden) loadUsage();
        }, win <= 10800 ? 3e4 : 6e4);
        return () => clearInterval(t);
      }, [loadUsage]);
      useEffect(() => {
        const t = setInterval(() => {
          if (!document.hidden) load();
        }, 15e3);
        return () => clearInterval(t);
      }, [load]);
      const base = baseRef.current;
      const dirty = !!(state && draft && base && !same(draft, base));
      const movedUnder = dirty && state.revision !== Number(base.revision || 0);
      const needsUnlock = !!(dirty && base.agents && base.agents.root && base.agents.root.locked && !same(base.agents.root, draft.agents.root));
      const say = (msg, tone) => {
        setFlash({ msg, tone });
        setTimeout(() => setFlash(null), 6e3);
      };
      const body = (isPreview) => JSON.stringify({
        doc: draft,
        base_revision: Number((baseRef.current || {}).revision || 0),
        unlock: (isPreview ? needsUnlock : unlock) ? ["root"] : [],
        summary
      });
      const preview = () => {
        setBusy(true);
        fetchJSON(`${API}/plan`, { method: "POST", headers: { "Content-Type": "application/json" }, body: body(true) }).then((r) => {
          setPlan(r);
          setBusy(false);
        }).catch((e) => {
          setBusy(false);
          say(String(e.message || e), "bad");
        });
      };
      const apply = () => {
        setBusy(true);
        fetchJSON(`${API}/apply`, { method: "POST", headers: { "Content-Type": "application/json" }, body: body(false) }).then((r) => {
          setBusy(false);
          if (r.ok) {
            setPlan(null);
            setSummary("");
            setUnlock(false);
            baseRef.current = null;
            setDraft(null);
            load().then(() => {
            });
            loadUsage();
            say(`Applied \u2014 ${r.changed.length} config(s) rewritten and verified${r.souls && r.souls.length ? ", SOULs refreshed" : ""}. Revert from History if needed.`, "ok");
          } else {
            setPlan({ ...plan || {}, errors: r.errors, warnings: r.warnings });
          }
        }).catch((e) => {
          setBusy(false);
          say(String(e.message || e), "bad");
        });
      };
      const revert = (hh) => {
        if (!window.confirm(`Put models.yaml, the configs and SOULs back as they were before \u201C${hh.summary}\u201D?`)) return;
        fetchJSON(`${API}/revert`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: hh.id }) }).then((r) => {
          if (r.ok) {
            baseRef.current = null;
            setDraft(null);
            load();
            say("Reverted \u2014 recorded as " + r.id, "ok");
          } else say((r.errors || []).join("; "), "bad");
        });
      };
      if (err && !state) return /* @__PURE__ */ h("div", { className: "fm-root", ref: setRootEl }, /* @__PURE__ */ h("div", { className: "fm-note fm-note--bad" }, "Fleet Models can't load: ", err));
      if (!state || !draft) return /* @__PURE__ */ h("div", { className: "fm-root", ref: setRootEl }, /* @__PURE__ */ h("div", { className: "fm-muted fm-loading" }, "Loading the fleet's model settings\u2026"));
      const stale = !!(usage && usage.window !== win);
      const shown = usage;
      const doc = draft;
      const TABS = [["home", "Home"], ["agent", "Agents"], ["models", "Models & hosts"], ["costs", "Costs"], ["decisions", "Rules & history"]];
      return /* @__PURE__ */ h("div", { className: "fm-root", ref: setRootEl }, /* @__PURE__ */ h("header", { className: "fm-head" }, /* @__PURE__ */ h("div", null, /* @__PURE__ */ h("h1", null, "Fleet Models"), /* @__PURE__ */ h("div", { className: "fm-sub" }, "Every agent's waterfall from one file \u2014 ", /* @__PURE__ */ h("code", null, "~/.hermes/fleet/models.yaml"), " \xB7 revision ", state.revision, " \xB7 ", state.doc.updated_by || "\u2014", " ", ago(state.doc.updated_at))), /* @__PURE__ */ h("div", { className: "fm-row" }, /* @__PURE__ */ h(Badge2, { tone: "ok", title: "data_collection: deny on every OpenRouter call" }, "no-training \xB7 deny"), /* @__PURE__ */ h(Badge2, { tone: state.runtime.aux_routing_seam ? "ok" : "warn", title: "helper calls follow each model's pins (fleet-models plugin)" }, state.runtime.aux_routing_seam ? "helper routing on" : "helper routing off"), state.validation.errors.length ? /* @__PURE__ */ h(Badge2, { tone: "bad", title: state.validation.errors.join("\n") }, state.validation.errors.length, " rule breach") : null, /* @__PURE__ */ h("button", { className: "fm-btn", onClick: () => {
        load();
        loadUsage();
      } }, "Refresh"))), /* @__PURE__ */ h("nav", { className: "fm-tabs" }, TABS.map(([k, l]) => /* @__PURE__ */ h("button", { key: k, className: cls2("fm-tab", tab === k && "is-active"), onClick: () => setTab(k) }, l))), movedUnder ? /* @__PURE__ */ h("div", { className: "fm-note fm-note--warn" }, "Someone applied a change while you were editing (now revision ", state.revision, "). Preview will refuse a stale edit \u2014 discard and redo it.") : null, tab === "home" || tab === "models" || tab === "costs" ? /* @__PURE__ */ h(PeriodBar2, { win, setWin, usage: shown, loading: uLoading, failed: uErr > 0 }) : null, /* @__PURE__ */ h("main", { className: cls2("fm-main", stale && "is-stale") }, tab === "home" ? /* @__PURE__ */ h(HomeView2, { state: { ...state, doc }, win: shown ? shown.window : win }) : null, tab === "agent" ? /* @__PURE__ */ h(AgentView2, { state, draft, setDraft, p: agent, setP: setAgent }) : null, tab === "models" ? /* @__PURE__ */ h(ModelsView2, { draft, setDraft, usage: shown, win: shown ? shown.window : win, sel: modelSel, setSel: setModelSel }) : null, tab === "costs" ? /* @__PURE__ */ h(CostsView2, { doc, usage: shown, win: shown ? shown.window : win, width: chartW, balances: state.balances }) : null, tab === "decisions" ? /* @__PURE__ */ h(DecisionsView2, { state, draft, setDraft, onRevert: revert }) : null), dirty ? /* @__PURE__ */ h("div", { className: "fm-dock" }, /* @__PURE__ */ h("span", null, /* @__PURE__ */ h("b", null, "Unsaved changes"), " ", /* @__PURE__ */ h("span", { className: "fm-muted" }, "\u2014 nothing reaches the fleet until you apply")), /* @__PURE__ */ h("span", { className: "fm-row" }, /* @__PURE__ */ h("button", { className: "fm-btn", onClick: () => {
        setDraft(clone(state.doc));
        baseRef.current = clone(state.doc);
      } }, "Discard"), /* @__PURE__ */ h("button", { className: "fm-btn fm-btn--primary", disabled: busy, onClick: preview }, busy ? "Checking\u2026" : "Preview & apply"))) : null, plan ? /* @__PURE__ */ h(
        PlanPanel2,
        {
          plan,
          onClose: () => setPlan(null),
          onApply: apply,
          applying: busy,
          needsUnlock,
          unlock,
          setUnlock,
          summary,
          setSummary
        }
      ) : null, flash ? /* @__PURE__ */ h("div", { className: cls2("fm-flash", "fm-flash--" + flash.tone) }, flash.msg) : null);
    };
    tickLabel = tickLabel2, isTick = isTick2, rangeLabel = rangeLabel2, cls = cls2, Pill = Pill2, Chain = Chain2, RungPicker = RungPicker2, ChainEditor = ChainEditor2, Stat = Stat2, relAge = relAge2, BalanceStats = BalanceStats2, DecisionLog = DecisionLog2, Badge = Badge2, Section = Section2, PeriodBar = PeriodBar2, Spark = Spark2, TimeChart = TimeChart2, helperGroups = helperGroups2, LiveChain = LiveChain2, SlotRow = SlotRow2, listInput = listInput2, parseList = parseList2, AgentView = AgentView2, usedBy = usedBy2, HostTable = HostTable2, ModelDetail = ModelDetail2, AddModel = AddModel2, ModelsView = ModelsView2, hrs = hrs2, HomeView = HomeView2, CostsView = CostsView2, BarList = BarList2, DecisionsView = DecisionsView2, PlanPanel = PlanPanel2, ModelsPage = ModelsPage2;
    const { React } = SDK;
    const h = React.createElement;
    const Fragment = React.Fragment;
    const { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks;
    const API = "/api/plugins/fleet-models";
    const fetchJSON = SDK.fetchJSON;
    const AUX_TASKS = [
      "vision",
      "compression",
      "title_generation",
      "background_review",
      "goal_judge",
      "kanban_decomposer",
      "web_extract",
      "session_search",
      "skills_hub",
      "approval",
      "flush_memories"
    ];
    const PROV_LABEL = {
      modelark: "ModelArk \xB7 subscription",
      openrouter: "OpenRouter \xB7 metered",
      deepseek: "DeepSeek direct \xB7 metered (estimated)"
    };
    const REASONING = ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"];
    const SLOT_LABEL = { main: "Main loop", subagents: "Subagents", cron: "Cron jobs" };
    const TASK_LABEL = {
      vision: "Vision",
      compression: "Compression",
      title_generation: "Titles",
      background_review: "Background review",
      goal_judge: "Goal judge",
      kanban_decomposer: "Decomposer",
      web_extract: "Web extract",
      session_search: "Session search",
      skills_hub: "Skills hub",
      approval: "Approval",
      flush_memories: "Memory flush"
    };
    const clone = (o) => JSON.parse(JSON.stringify(o));
    const tok = (n) => {
      const v = Number(n) || 0;
      if (v >= 1e9) return (v / 1e9).toFixed(v < 1e10 ? 2 : 1) + "B";
      if (v >= 1e6) return (v / 1e6).toFixed(v < 1e7 ? 1 : 0) + "M";
      if (v >= 1e3) return Math.round(v / 1e3) + "k";
      return String(v);
    };
    const costSub = (T) => {
      if (!T) return "";
      const parts = [];
      if (T.invoiced_usd) parts.push(money(T.invoiced_usd, 2) + " invoiced");
      if (T.estimated_usd) parts.push(money(T.estimated_usd, T.estimated_usd < 0.01 ? 4 : 2) + " estimated");
      if (T.subscription_calls) parts.push("subscription $0");
      return parts.join(" \xB7 ") || "no spend in this window";
    };
    const payerSub = (T) => {
      const bp = T && T.by_payer || {};
      return Object.keys(bp).sort().map((n) => `${num(bp[n].calls)} ${n}`).join(" \xB7 ");
    };
    const chainOf = (spec) => spec == null ? [] : Array.isArray(spec) ? spec : spec.chain || [];
    const reasoningOf = (spec) => spec && !Array.isArray(spec) ? spec.reasoning || "" : "";
    const withChain = (spec, chain) => spec && !Array.isArray(spec) ? { ...spec, chain } : chain;
    const money = (v, d = 4) => v == null ? "\u2014" : v === 0 ? "$0" : v < 1e-4 ? "<$0.0001" : "$" + Number(v).toFixed(v >= 10 ? 2 : d);
    const num = (v) => v == null ? "\u2014" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(1) + "k" : String(v);
    const pct = (v) => v == null ? "\u2014" : v.toFixed(v >= 99.95 ? 2 : 1) + "%";
    const price = (v) => v == null ? "\u2014" : "$" + (v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toFixed(4));
    const ago = (ts) => {
      if (!ts) return "\u2014";
      const s = Math.max(0, Date.now() / 1e3 - (typeof ts === "string" ? Date.parse(ts) / 1e3 : ts));
      return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + " min ago" : s < 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago";
    };
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const hostSlug = (t) => String(t || "").split("/")[0];
    const PERIODS = [
      ["15m", 900, "15 minutes"],
      ["30m", 1800, "30 minutes"],
      ["1h", 3600, "hour"],
      ["2h", 7200, "2 hours"],
      ["3h", 10800, "3 hours"],
      ["6h", 21600, "6 hours"],
      ["12h", 43200, "12 hours"],
      ["24h", 86400, "24 hours"],
      ["7d", 604800, "7 days"],
      ["14d", 1209600, "14 days"],
      ["30d", 2592e3, "30 days"]
    ];
    const NICE = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
    const LABEL_STEPS = NICE.concat([1209600]);
    const MIN_PITCH = 12;
    const MIN_LABEL_GAP = 36;
    const TZ = (() => {
      try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      } catch (e) {
        return "";
      }
    })();
    const naturalBucket = (w) => {
      const m = [[900, 60], [1800, 60], [3600, 120], [7200, 300], [10800, 300], [21600, 900], [43200, 1800], [86400, 3600], [604800, 21600]];
      const hit = m.find(([x]) => w <= x);
      return hit ? hit[1] : 86400;
    };
    const pickBucket = (w, width) => {
      const maxBars = Math.max(6, Math.floor(width / MIN_PITCH));
      let b = naturalBucket(w);
      while (w / b > maxBars) {
        const nx = NICE.find((x) => x > b);
        if (!nx) break;
        b = nx;
      }
      return b;
    };
    const periodName = (w) => {
      const p = PERIODS.find((x) => x[1] === w);
      return p ? p[2] : Math.round(w / 3600) + " hours";
    };
    const periodShort = (w) => {
      const p = PERIODS.find((x) => x[1] === w);
      return p ? p[0] : Math.round(w / 3600) + "h";
    };
    const lastLabel = (w) => w === 3600 ? "Last hour" : "Last " + periodName(w);
    const bucketName = (b) => b < 3600 ? b / 60 + "-minute" : b < 86400 ? b / 3600 === 1 ? "hourly" : b / 3600 + "-hour" : b === 86400 ? "daily" : b / 86400 + "-day";
    const WIN_KEY = "fleet-models.window";
    const readWin = () => {
      try {
        const v = Number(window.localStorage.getItem(WIN_KEY));
        return PERIODS.some((p) => p[1] === v) ? v : 604800;
      } catch (e) {
        return 604800;
      }
    };
    const saveWin = (v) => {
      try {
        window.localStorage.setItem(WIN_KEY, String(v));
      } catch (e) {
      }
    };
    const hhmm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const dayMon = (d) => d.toLocaleDateString([], { day: "numeric", month: "short" });
    const wday = (d) => d.toLocaleDateString([], { weekday: "short" });
    if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
      window.__HERMES_PLUGINS__.register("fleet-models", ModelsPage2);
    }
  }
  var tickLabel;
  var isTick;
  var rangeLabel;
  var cls;
  var Pill;
  var Chain;
  var RungPicker;
  var ChainEditor;
  var Stat;
  var relAge;
  var BalanceStats;
  var DecisionLog;
  var Badge;
  var Section;
  var PeriodBar;
  var Spark;
  var TimeChart;
  var helperGroups;
  var LiveChain;
  var SlotRow;
  var listInput;
  var parseList;
  var AgentView;
  var usedBy;
  var HostTable;
  var ModelDetail;
  var AddModel;
  var ModelsView;
  var hrs;
  var HomeView;
  var CostsView;
  var BarList;
  var DecisionsView;
  var PlanPanel;
  var ModelsPage;
})();
