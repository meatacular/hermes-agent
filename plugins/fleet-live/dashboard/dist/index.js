/**
 * Hermes fleet — Live page.
 *
 * One pane per active agent, each streaming that agent's chain of thought as it is written.
 *
 * Three rules shape the whole file:
 *   1. Nothing loads until the tab is open. The socket opens on mount and closes on unmount, so
 *      an unvisited tab costs the dashboard nothing.
 *   2. Nothing streams that you cannot see. An IntersectionObserver drives the subscription set,
 *      so a pane scrolled off the bottom of a twenty-agent grid is unsubscribed server-side, not
 *      merely hidden. Its header keeps updating; its feed does not. A pane also subscribes only
 *      AFTER its backlog has landed, so it resumes from a known byte offset instead of replaying
 *      the whole log.
 *   3. Scrolling back pauses the tail. A pane you have scrolled up in holds its exact position
 *      while new content accumulates behind a counter; returning to the bottom re-follows.
 *
 * Hand-authored IIFE against the host React (SDK contract 1.1.0) — no build step, no bundled
 * React, no ESM. Keep it that way: this dist file IS the source.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var REG = window.__HERMES_PLUGINS__;
  if (!SDK || !REG) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useLayoutEffect = React.useLayoutEffect || SDK.hooks.useEffect;
  var useRef = SDK.hooks.useRef;
  var useCallback = SDK.hooks.useCallback;
  var useMemo = SDK.hooks.useMemo;

  var API = "/api/plugins/fleet-live";
  var MAX_EVENTS = 500;     // per pane, trimmed only while following the tail
  var BOTTOM_SLACK = 28;    // px from the bottom that still counts as "at the tail"
  var BACKLOG = 49152;      // bytes of transcript fetched per scroll-back page

  // ── formatting ───────────────────────────────────────────────────────────────────────────
  function cls() {
    var out = [];
    for (var i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]);
    return out.join(" ");
  }
  function money(v) {
    var n = Number(v) || 0;
    if (n === 0) return "$0";
    return n < 0.01 ? "$" + n.toFixed(4) : "$" + n.toFixed(2);
  }
  function tok(n) {
    var v = Number(n) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return Math.round(v / 1e3) + "k";
    return String(v);
  }
  function dur(s) {
    var v = Math.max(0, Math.round(Number(s) || 0));
    if (v < 60) return v + "s";
    if (v < 3600) return Math.floor(v / 60) + "m " + (v % 60) + "s";
    return Math.floor(v / 3600) + "h " + Math.floor((v % 3600) / 60) + "m";
  }
  function ago(s) { return (s === null || s === undefined) ? "—" : dur(s) + " ago"; }

  // ── the feed store ────────────────────────────────────────────────────────────────────────
  // Deltas arrive four times a second across up to two dozen panes. Holding that in React state
  // would re-render the whole grid on every token, so events live here, each pane subscribes to
  // its own slice, and notifications are coalesced onto an animation frame.
  function createStore() {
    var panes = {};
    var listeners = {};
    var pending = {};
    var frame = null;

    function slot(id) {
      if (!panes[id]) {
        panes[id] = { events: [], index: {}, offset: 0, seq: 0, start: null, more: false,
                      loaded: false, following: true, stats: null };
      }
      return panes[id];
    }
    function flush() {
      frame = null;
      var ids = Object.keys(pending);
      pending = {};
      for (var i = 0; i < ids.length; i++) {
        var fns = listeners[ids[i]] || [];
        for (var j = 0; j < fns.length; j++) fns[j]();
      }
    }
    function touch(id) {
      pending[id] = true;
      if (frame === null) {
        frame = (window.requestAnimationFrame || window.setTimeout)(flush, 16);
      }
    }

    var store = {
      get: slot,
      subscribe: function (id, fn) {
        listeners[id] = (listeners[id] || []).concat(fn);
        return function () {
          listeners[id] = (listeners[id] || []).filter(function (f) { return f !== fn; });
        };
      },
      apply: function (id, ops, offset, seq, allowTrim) {
        var s = slot(id);
        for (var i = 0; i < ops.length; i++) {
          var op = ops[i];
          if (op.op === "add") {
            var ev = { key: id + "#" + op.id, id: op.id, t: op.t, label: op.label || "",
                       text: op.text || "", dur: op.dur || "", at: op.at, open: true };
            s.events.push(ev);
            s.index[op.id] = ev;
          } else if (op.op === "app") {
            if (s.index[op.id]) s.index[op.id].text += op.text;
          } else if (op.op === "end") {
            if (s.index[op.id]) s.index[op.id].open = false;
          }
        }
        if (offset !== undefined && offset !== null) s.offset = offset;
        if (seq) s.seq = Math.max(s.seq, seq);
        // Trim only while the reader is at the tail. Dropping the head under someone who has
        // scrolled up would yank the page out from under them.
        if (allowTrim && s.events.length > MAX_EVENTS) {
          var dropped = s.events.splice(0, s.events.length - MAX_EVENTS);
          for (var k = 0; k < dropped.length; k++) delete s.index[dropped[k].id];
          s.more = true;
        }
        if (ops.length) touch(id);
      },
      seed: function (id, ops, offset, seq, start, more) {
        var s = slot(id);
        s.events = [];
        s.index = {};
        s.offset = 0;
        s.seq = 0;
        store.apply(id, ops, offset, seq, false);
        for (var i = 0; i < s.events.length; i++) s.events[i].open = false;
        s.start = start;
        s.more = !!more;
        s.loaded = true;
        touch(id);
      },
      prepend: function (id, ops, start, more) {
        var s = slot(id);
        var built = [];
        var idx = {};
        for (var i = 0; i < ops.length; i++) {
          var op = ops[i];
          if (op.op === "add") {
            var ev = { key: id + "@" + start + "#" + op.id, id: "b" + start + "_" + op.id, t: op.t,
                       label: op.label || "", text: op.text || "", dur: op.dur || "", at: op.at, open: false };
            built.push(ev);
            idx[op.id] = ev;
          } else if (op.op === "app" && idx[op.id]) {
            idx[op.id].text += op.text;
          }
        }
        s.events = built.concat(s.events);
        s.start = start;
        s.more = !!more;
        touch(id);
      },
      reset: function (id) {
        var s = slot(id);
        s.events = [];
        s.index = {};
        s.offset = 0;
        s.seq = 0;
        s.loaded = false;
        touch(id);
      },
      setStats: function (id, stats) { slot(id).stats = stats; touch(id); }
    };
    return store;
  }

  // ── the socket ────────────────────────────────────────────────────────────────────────────
  // One socket for the page. Subscriptions are diffed against what the server already holds, so
  // a pane scrolling in and out of view costs one small frame, never a reconnect.
  function useStream(store, onPanes) {
    var sockRef = useRef(null);
    var wantRef = useRef({});
    var haveRef = useRef({});
    var aliveRef = useRef(true);
    var readyRef = useRef(false);
    var attemptRef = useRef(0);
    var pausedRef = useRef(false);
    var onPanesRef = useRef(onPanes);
    onPanesRef.current = onPanes;
    var connState = useState(false);
    var setConnected = connState[1];

    var sync = useCallback(function () {
      var sock = sockRef.current;
      if (!sock || !readyRef.current) return;
      var want = pausedRef.current ? {} : wantRef.current;
      var have = haveRef.current;
      var sub = [], unsub = [], from = {}, id;
      for (id in want) {
        if (!have[id]) {
          sub.push(id);
          var s = store.get(id);
          from[id] = { offset: s.offset, seq: s.seq };
        }
      }
      for (id in have) if (!want[id]) unsub.push(id);
      if (!sub.length && !unsub.length) return;
      try { sock.send(JSON.stringify({ sub: sub, unsub: unsub, from: from })); }
      catch (err) { return; }
      var next = {};
      for (id in want) next[id] = true;
      haveRef.current = next;
    }, [store]);

    var connect = useCallback(function () {
      if (!aliveRef.current) return;
      var retry = function () {
        if (!aliveRef.current) return;
        window.setTimeout(connect, Math.min(15000, 500 * Math.pow(2, attemptRef.current++)));
      };
      SDK.buildWsUrl(API + "/stream").then(function (url) {
        if (!aliveRef.current) return;
        var sock = new WebSocket(url);
        sockRef.current = sock;
        sock.onopen = function () {
          readyRef.current = true;
          attemptRef.current = 0;
          haveRef.current = {};
          setConnected(true);
          sync();
        };
        sock.onmessage = function (e) {
          var msg;
          try { msg = JSON.parse(e.data); } catch (err) { return; }
          if (msg.type === "panes") onPanesRef.current(msg.panes || []);
          else if (msg.type === "feed") {
            store.apply(msg.pane, msg.ops || [], msg.offset, msg.seq, !!store.get(msg.pane).following);
          } else if (msg.type === "stats") store.setStats(msg.pane, msg.stats);
          else if (msg.type === "reset") {
            store.reset(msg.pane);
            delete haveRef.current[msg.pane];
            sync();
          }
        };
        sock.onclose = function () {
          readyRef.current = false;
          sockRef.current = null;
          haveRef.current = {};
          setConnected(false);
          retry();
        };
        sock.onerror = function () { try { sock.close(); } catch (err) { /* onclose retries */ } };
      }).catch(retry);
    }, [store, sync, setConnected]);

    useEffect(function () {
      aliveRef.current = true;
      connect();
      return function () {
        aliveRef.current = false;
        readyRef.current = false;
        var sock = sockRef.current;
        sockRef.current = null;
        if (sock) { try { sock.close(); } catch (err) { /* already gone */ } }
      };
    }, [connect]);

    // Stable identity: panes mount observers against these callbacks, so the object must not be
    // rebuilt on every render or every pane would re-observe on every token.
    var api = useMemo(function () {
      return {
        setVisible: function (id, on) {
          if (on) wantRef.current[id] = true; else delete wantRef.current[id];
          sync();
        },
        setPaused: function (on) { pausedRef.current = !!on; haveRef.current = {}; sync(); },
        refreshPanes: function () {
          var sock = sockRef.current;
          if (sock && readyRef.current) { try { sock.send(JSON.stringify({ panes: true })); } catch (err) { /**/ } }
        }
      };
    }, [sync]);

    return [api, connState[0]];
  }

  // ── event rendering ───────────────────────────────────────────────────────────────────────
  var KIND_LABEL = { thought: "thinking", say: "says", user: "brief" };

  function EventRow(props) {
    var ev = props.ev;
    if (ev.t === "tool") {
      return h("div", { className: "fl-ev fl-ev--tool", title: ev.text },
        h("span", { className: "fl-tool-icon" }, ev.label || "›"),
        h("span", { className: "fl-tool-text" }, ev.text),
        ev.dur ? h("span", { className: "fl-tool-dur" }, ev.dur) : null);
    }
    if (ev.t === "raw") {
      var lines = ev.text.split("\n");
      var long = lines.length > 8;
      var shown = (long && props.collapsed) ? lines.slice(0, 8).join("\n") : ev.text;
      return h("div", { className: "fl-ev fl-ev--raw" },
        h("pre", { className: "fl-raw" }, shown),
        long ? h("button", { className: "fl-more", onClick: props.onToggle },
          props.collapsed ? "show " + (lines.length - 8) + " more lines" : "collapse") : null);
    }
    return h("div", { className: cls("fl-ev", "fl-ev--" + ev.t, ev.open && "is-open") },
      h("div", { className: "fl-ev-label" }, KIND_LABEL[ev.t] || ev.label || ""),
      h("div", { className: "fl-ev-text" }, ev.text,
        ev.open ? h("span", { className: "fl-caret" }) : null));
  }

  // ── one pane's feed ───────────────────────────────────────────────────────────────────────
  function Feed(props) {
    var paneId = props.paneId;
    var store = props.store;
    var stream = props.stream;
    var filter = props.filter;

    var scrollRef = useRef(null);
    var followRef = useRef(true);
    var collapsedRef = useRef({});
    var lastCountRef = useRef(0);
    var loadingRef = useRef(false);
    var anchorRef = useRef(null);
    var tickState = useState(0);
    var bump = tickState[1];
    var unreadState = useState(0);
    var setUnread = unreadState[1];
    var followState = useState(true);
    var setFollow = followState[1];

    var slot = store.get(paneId);
    slot.following = followRef.current;

    useEffect(function () {
      return store.subscribe(paneId, function () { bump(function (n) { return n + 1; }); });
    }, [paneId, store, bump]);

    // Backlog first, subscription second. Subscribing before the backlog lands would make the
    // server replay the transcript from byte zero, and the seed would then throw it away.
    useEffect(function () {
      var cancelled = false;
      function go() {
        if (cancelled) return;
        stream.setVisible(paneId, true);
      }
      if (slot.loaded) {
        go();
      } else {
        SDK.fetchJSON(API + "/panes/" + encodeURIComponent(paneId) + "/tail?bytes=" + BACKLOG)
          .then(function (r) {
            if (cancelled) return;
            store.seed(paneId, r.ops || [], r.offset, r.seq, r.start, r.more);
            followRef.current = true;
            setFollow(true);
            go();
          })
          .catch(function () {
            if (cancelled) return;
            slot.loaded = true;          // show the empty state rather than spin forever
            bump(function (n) { return n + 1; });
            go();
          });
      }
      return function () {
        cancelled = true;
        stream.setVisible(paneId, false);
      };
    }, [paneId, slot, store, stream, bump, setFollow]);

    var events = slot.events;
    var shown = events;
    if (filter === "thoughts") {
      shown = [];
      for (var i = 0; i < events.length; i++) if (events[i].t === "thought" || events[i].t === "say") shown.push(events[i]);
    } else if (filter === "tools") {
      shown = [];
      for (var j = 0; j < events.length; j++) if (events[j].t === "tool" || events[j].t === "raw") shown.push(events[j]);
    }

    // Follow the tail, or hold position. This is the whole point of the page.
    useLayoutEffect(function () {
      var el = scrollRef.current;
      if (!el) return;
      if (anchorRef.current !== null) {
        // A scroll-back page was just prepended: keep the reader's eye on the same line.
        el.scrollTop = el.scrollHeight - anchorRef.current;
        anchorRef.current = null;
        lastCountRef.current = shown.length;
        return;
      }
      if (followRef.current) {
        el.scrollTop = el.scrollHeight;
        lastCountRef.current = shown.length;
      } else if (shown.length > lastCountRef.current) {
        var added = shown.length - lastCountRef.current;
        lastCountRef.current = shown.length;
        setUnread(function (n) { return n + added; });
      }
    });

    var loadMore = useCallback(function () {
      if (loadingRef.current || !slot.more || !slot.start) return;
      loadingRef.current = true;
      var el = scrollRef.current;
      anchorRef.current = el ? (el.scrollHeight - el.scrollTop) : null;
      SDK.fetchJSON(API + "/panes/" + encodeURIComponent(paneId) + "/tail?bytes=" + BACKLOG + "&before=" + slot.start)
        .then(function (r) { store.prepend(paneId, r.ops || [], r.start, r.more); })
        .catch(function () { anchorRef.current = null; })
        .then(function () { loadingRef.current = false; }, function () { loadingRef.current = false; });
    }, [paneId, slot, store]);

    var onScroll = useCallback(function () {
      var el = scrollRef.current;
      if (!el) return;
      var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_SLACK;
      if (atBottom !== followRef.current) {
        followRef.current = atBottom;
        slot.following = atBottom;
        setFollow(atBottom);
        if (atBottom) setUnread(0);
      }
      if (el.scrollTop < 48) loadMore();
    }, [slot, setFollow, setUnread, loadMore]);

    var toTail = useCallback(function () {
      followRef.current = true;
      slot.following = true;
      setFollow(true);
      setUnread(0);
      var el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, [slot, setFollow, setUnread]);

    var body;
    if (!slot.loaded) body = h("div", { className: "fl-empty" }, "loading transcript…");
    else if (!shown.length) body = h("div", { className: "fl-empty" }, "nothing on this feed yet");
    else {
      body = shown.map(function (ev) {
        return h(EventRow, {
          key: ev.key,
          ev: ev,
          collapsed: collapsedRef.current[ev.key] !== false,
          onToggle: function () {
            collapsedRef.current[ev.key] = collapsedRef.current[ev.key] === false;
            bump(function (n) { return n + 1; });
          }
        });
      });
    }

    return h("div", { className: "fl-feed-wrap" },
      (slot.more && slot.start) ? h("button", { className: "fl-loadmore", onClick: loadMore }, "load earlier") : null,
      h("div", { className: "fl-feed", ref: scrollRef, onScroll: onScroll }, body),
      !followState[0]
        ? h("button", { className: "fl-tailpill", onClick: toTail },
            unreadState[0] > 0 ? unreadState[0] + " new · jump to live ↓" : "held · jump to live ↓")
        : null);
  }

  // ── one pane ──────────────────────────────────────────────────────────────────────────────
  function Pane(props) {
    var pane = props.pane;
    var store = props.store;
    var stream = props.stream;
    var wide = props.wide;
    var onWide = props.onWide;

    var hostRef = useRef(null);
    var visRef = useRef(false);
    var visState = useState(false);
    var setVis = visState[1];
    var filterState = useState("all");
    var filter = filterState[0];
    var setFilter = filterState[1];
    var cardState = useState(null);
    var card = cardState[0];
    var setCard = cardState[1];
    var tickState = useState(0);
    var bump = tickState[1];

    useEffect(function () {
      return store.subscribe(pane.id, function () { bump(function (n) { return n + 1; }); });
    }, [pane.id, store, bump]);

    // Rule 2: what you can see is what streams. 300px of margin means a pane is already running
    // by the time it reaches the fold.
    useEffect(function () {
      var el = hostRef.current;
      if (!el || typeof IntersectionObserver === "undefined") { setVis(true); return; }
      var obs = new IntersectionObserver(function (entries) {
        var on = !!(entries[0] && entries[0].isIntersecting);
        if (on === visRef.current) return;
        visRef.current = on;
        setVis(on);
      }, { rootMargin: "300px 0px" });
      obs.observe(el);
      return function () { obs.disconnect(); };
    }, [setVis]);

    useEffect(function () {
      if (filter !== "card" || card || pane.kind !== "kanban") return;
      var cancelled = false;
      SDK.fetchJSON(API + "/panes/" + encodeURIComponent(pane.id) + "/card")
        .then(function (r) { if (!cancelled) setCard(r.card); })
        .catch(function () { if (!cancelled) setCard({ body: "card unavailable" }); });
      return function () { cancelled = true; };
    }, [filter, card, setCard, pane.id, pane.kind]);

    var live = store.get(pane.id).stats || {};
    var st = {};
    var key;
    for (key in (pane.stats || {})) st[key] = pane.stats[key];
    for (key in live) if (live[key] !== undefined && live[key] !== null) st[key] = live[key];
    var tokens = st.tokens || {};
    var stale = pane.heartbeat_age_s !== null && pane.heartbeat_age_s !== undefined && pane.heartbeat_age_s > 150;
    var dead = pane.alive === false || st.alive === false;
    var capPct = pane.cost_cap ? Math.min(100, ((st.cost_usd || 0) / pane.cost_cap) * 100) : null;
    var tabs = pane.kind === "kanban" ? ["all", "thoughts", "tools", "card"] : ["all", "thoughts", "tools"];

    return h("section", { className: cls("fl-pane", wide && "is-wide"), ref: hostRef },
      h("header", { className: "fl-pane-head" },
        h("span", {
          className: cls("fl-dot", dead ? "is-dead" : stale ? "is-stale" : "is-live"),
          title: dead ? "worker process is gone" : stale ? "no heartbeat for " + dur(pane.heartbeat_age_s) : "live"
        }),
        h("div", { className: "fl-pane-title", title: pane.title }, pane.title),
        h("button", {
          className: "fl-icon-btn", onClick: function () { onWide(wide ? null : pane.id); },
          title: wide ? "restore to the grid" : "expand across the grid"
        }, wide ? "⤡" : "⤢")),
      h("div", { className: "fl-chips" },
        h("span", { className: "fl-chip fl-chip--agent" }, "@" + (pane.agent || "?")),
        st.model ? h("span", { className: "fl-chip", title: st.model }, st.model) : null,
        pane.task_id ? h("a", { className: "fl-chip fl-chip--link", href: "/kanban", title: "open the board" }, pane.task_id) : null,
        pane.tenant ? h("span", { className: "fl-chip" }, pane.tenant) : null,
        pane.surface ? h("span", { className: "fl-chip" }, pane.surface) : null,
        pane.attempt > 1 ? h("span", { className: "fl-chip fl-chip--warn" }, "attempt " + pane.attempt) : null),
      h("div", { className: "fl-stats" },
        h(Stat, {
          label: "cost", value: money(st.cost_usd),
          sub: (st.cost_status === "estimated" ? "est" : (st.cost_status || "")) +
               (pane.cost_cap ? " · cap " + money(pane.cost_cap) : ""),
          bar: capPct,
          barTitle: pane.cost_cap ? money(st.cost_usd) + " of a " + money(pane.cost_cap) + " cap" : null
        }),
        h(Stat, {
          label: "tokens", value: tok((tokens.input_tokens || 0) + (tokens.output_tokens || 0)),
          sub: tok(tokens.cache_read_tokens) + " cached"
        }),
        h(Stat, {
          label: "steps", value: (st.tool_calls || 0) + " tools", sub: (st.api_calls || 0) + " calls"
        }),
        h(Stat, {
          label: "elapsed",
          value: dur(st.elapsed_s !== undefined ? st.elapsed_s : pane.elapsed_s),
          sub: (pane.heartbeat_age_s !== null && pane.heartbeat_age_s !== undefined)
            ? "beat " + ago(pane.heartbeat_age_s) : ""
        })),
      h("nav", { className: "fl-tabs" }, tabs.map(function (t) {
        return h("button", {
          key: t, className: cls("fl-tab", filter === t && "is-active"),
          onClick: function () { setFilter(t); }
        }, t);
      })),
      filter === "card"
        ? h("div", { className: "fl-card-body" }, h("pre", { className: "fl-raw" }, (card && card.body) || "loading brief…"))
        : visState[0]
          ? h(Feed, { paneId: pane.id, store: store, stream: stream, filter: filter })
          : h("div", { className: "fl-feed-wrap" },
              h("div", { className: "fl-feed fl-feed--idle" },
                h("div", { className: "fl-empty" }, "scroll into view to stream"))));
  }

  function Stat(props) {
    // A cap bar and the number under it are not redundant: the bar shows the shape of the
    // budget at a glance, the text says what the budget actually is. At 2% of a $3 cap the bar
    // alone is a dot.
    return h("div", { className: "fl-stat", title: props.barTitle || "" },
      h("div", { className: "fl-stat-label" }, props.label),
      h("div", { className: "fl-stat-value" }, props.value),
      (props.bar !== null && props.bar !== undefined)
        ? h("div", { className: "fl-bar" },
            h("i", { style: { width: props.bar + "%" }, className: props.bar > 85 ? "is-hot" : "" }))
        : null,
      h("div", { className: "fl-stat-sub" }, props.sub || ""));
  }

  // ── the page ──────────────────────────────────────────────────────────────────────────────
  function Page() {
    var store = useMemo(createStore, []);
    var panesState = useState([]);
    var panes = panesState[0];
    var setPanes = panesState[1];
    var seenState = useState(false);
    var seen = seenState[0];
    var setSeen = seenState[1];
    var colsState = useState(3);
    var cols = colsState[0];
    var agentState = useState("all");
    var agent = agentState[0];
    var pausedState = useState(false);
    var paused = pausedState[0];
    var wideState = useState(null);

    var onPanes = useCallback(function (list) { setPanes(list); setSeen(true); }, [setPanes, setSeen]);
    var streamPair = useStream(store, onPanes);
    var stream = streamPair[0];
    var connected = streamPair[1];

    useEffect(function () { stream.setPaused(paused); }, [stream, paused]);

    var agents = useMemo(function () {
      var names = {};
      for (var i = 0; i < panes.length; i++) names[panes[i].agent] = true;
      return ["all"].concat(Object.keys(names).sort());
    }, [panes]);

    var visible = useMemo(function () {
      if (agent === "all") return panes;
      return panes.filter(function (p) { return p.agent === agent; });
    }, [panes, agent]);

    var totalCost = 0;
    for (var i = 0; i < visible.length; i++) {
      var s = store.get(visible[i].id).stats || visible[i].stats || {};
      totalCost += s.cost_usd || 0;
    }

    return h("div", { className: "fl-root", style: { "--fl-cols": String(cols) } },
      h("header", { className: "fl-head" },
        h("div", { className: "fl-head-text" },
          h("h1", null, "Live"),
          h("div", { className: "fl-sub" },
            visible.length + (visible.length === 1 ? " agent" : " agents") + " working · " +
            money(totalCost) + " spent on these runs · thoughts appear as they are written; " +
            "scroll back in any pane and it holds your place until you return to the tail")),
        h("div", { className: "fl-controls" },
          h("select", {
            className: "fl-select", value: agent, "aria-label": "filter by agent",
            onChange: function (e) { agentState[1](e.target.value); }
          }, agents.map(function (a) {
            return h("option", { key: a, value: a }, a === "all" ? "all agents" : "@" + a);
          })),
          h("div", { className: "fl-seg" }, [1, 2, 3].map(function (n) {
            return h("button", {
              key: n, className: cls("fl-segbtn", cols === n && "is-active"),
              onClick: function () { colsState[1](n); },
              title: n + " column" + (n > 1 ? "s" : "")
            }, String(n));
          })),
          h("button", {
            className: cls("fl-btn", paused && "is-active"),
            onClick: function () { pausedState[1](!paused); },
            title: paused ? "resume every feed" : "stop streaming without leaving the page"
          }, paused ? "resume" : "pause"),
          h("span", { className: cls("fl-conn", connected ? "is-on" : "is-off") },
            connected ? (paused ? "paused" : "streaming") : "reconnecting…"))),
      !visible.length
        ? h("div", { className: "fl-blank" }, seen
            ? "No agent is running right now. A pane appears the moment the dispatcher spawns one."
            : "Looking for active agents…")
        : h("div", { className: "fl-grid" }, visible.map(function (p) {
            return h(Pane, {
              key: p.id, pane: p, store: store, stream: stream,
              wide: wideState[0] === p.id, onWide: wideState[1]
            });
          })));
  }

  REG.register("fleet-live", Page);
})();
