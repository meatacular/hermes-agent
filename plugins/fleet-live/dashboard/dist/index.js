/**
 * Hermes fleet — Live page.
 *
 * Two tabs. **Live** is one pane per active agent, each streaming that agent's chain of thought
 * as it is written, with the card's lane timeline, its hard dependencies and the actions you
 * would otherwise open the board to take. **Up next** is the queue behind them, ordered by what
 * the dispatcher can actually reach.
 *
 * Three rules shape the streaming half:
 *   1. Nothing loads until the tab is open. The socket opens on mount and closes on unmount, so
 *      an unvisited tab costs the dashboard nothing.
 *   2. Nothing streams that you cannot see. An IntersectionObserver drives the subscription set,
 *      so a pane scrolled off the bottom of a twenty-agent grid is unsubscribed server-side, not
 *      merely hidden. A pane subscribes only AFTER its backlog has landed, so it resumes from a
 *      known byte offset instead of replaying the whole log.
 *   3. Scrolling back pauses the tail. A pane you have scrolled up in holds its exact position
 *      while new content accumulates behind a counter; returning to the bottom re-follows.
 *
 * Writes go to the Kanban plugin's own API, never to a second one of our own: unblocking a card
 * here and unblocking it on the board must be the same code path, or the two surfaces will drift.
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
  var KANBAN = "/api/plugins/kanban";
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
  // A cap is a round number the reader already knows; "$3" reads faster than "$3.00" in a tile
  // narrow enough to ellipsise.
  function cap(v) {
    var n = Number(v) || 0;
    return "$" + (n % 1 === 0 ? String(n) : n.toFixed(2));
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
    if (v < 86400) return Math.floor(v / 3600) + "h " + Math.floor((v % 3600) / 60) + "m";
    return Math.floor(v / 86400) + "d " + Math.floor((v % 86400) / 3600) + "h";
  }
  function ago(s) { return (s === null || s === undefined) ? "—" : dur(s) + " ago"; }
  function since(epoch) {
    if (!epoch) return "—";
    return dur(Date.now() / 1000 - Number(epoch)) + " ago";
  }
  function list(xs) { return xs.join(", "); }

  // ── board writes ─────────────────────────────────────────────────────────────────────────
  // Deliberately thin wrappers over the Kanban plugin. `fetchJSON` throws "<status>: <body>" on a
  // refusal, and the board's 409 for `ready` names the parents still holding the card — which is
  // exactly the sentence a person needs, so it is surfaced verbatim rather than replaced.
  function jsonReq(url, method, body) {
    return SDK.fetchJSON(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }
  function patchTask(taskId, body) { return jsonReq(KANBAN + "/tasks/" + encodeURIComponent(taskId), "PATCH", body); }
  function commentTask(taskId, text) {
    return jsonReq(KANBAN + "/tasks/" + encodeURIComponent(taskId) + "/comments", "POST",
      { body: text, author: "dashboard" });
  }
  function tidyError(err) {
    var msg = (err && err.message) || String(err);
    // fetchJSON gives "<status>: <json body>"; the useful part is the detail inside.
    try {
      var body = msg.slice(msg.indexOf(":") + 1).trim();
      var parsed = JSON.parse(body);
      if (parsed && parsed.detail) return String(parsed.detail);
    } catch (e) { /* not JSON — show it as it came */ }
    return msg;
  }

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
      if (frame === null) frame = (window.requestAnimationFrame || window.setTimeout)(flush, 16);
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

  // ── small shared pieces ───────────────────────────────────────────────────────────────────
  function StatusBadge(props) {
    var s = props.status || "—";
    return h("span", { className: "fl-badge fl-badge--" + s, title: "column: " + s }, s);
  }

  /** The card's road: where it has been, where it is, what is still to come. */
  function Timeline(props) {
    var tl = props.timeline;
    if (!tl || !tl.pipeline) return null;
    var doneSet = {};
    for (var i = 0; i < tl.done.length; i++) doneSet[tl.done[i]] = true;
    var stations = tl.pipeline.map(function (col) {
      var state = col === tl.current ? "now" : (doneSet[col] ? "done" : "todo");
      return h("span", { key: col, className: "fl-step fl-step--" + state,
                         title: state === "done" ? col + " — passed"
                              : state === "now" ? col + " — here now" : col + " — still to come" },
        col);
    });
    var caption = tl.count + (tl.count === 1 ? " step taken" : " steps taken");
    if (tl.remaining.length) caption += " · " + list(tl.remaining) + " to come";
    else caption += " · nothing left";
    return h("div", { className: "fl-timeline" },
      h("div", { className: "fl-steps" }, stations),
      h("div", { className: "fl-steps-caption" },
        tl.detour ? h("span", { className: "fl-badge fl-badge--" + tl.detour }, tl.detour) : null,
        h("span", null, caption)));
  }

  /** Hard dependencies. A chip is a door: it opens that card. */
  function Deps(props) {
    var deps = props.deps;
    var onOpen = props.onOpen;
    if (!deps || (!deps.parents.length && !(deps.children || []).length)) return null;
    var waiting = deps.parents.filter(function (p) { return p.status !== "done"; });
    var childCount = deps.children ? deps.children.length : (deps.children_count || 0);
    return h("div", { className: "fl-deps" },
      deps.parents.length
        ? h("div", { className: "fl-deprow" },
            h("span", { className: cls("fl-deplabel", waiting.length && "is-blocking") },
              waiting.length ? "waiting on " + waiting.length : "depends on"),
            deps.parents.map(function (p) {
              return h("button", {
                key: p.id,
                className: cls("fl-dep", p.status !== "done" && "is-open"),
                title: p.title + "  ·  " + p.status + (p.assignee ? "  ·  @" + p.assignee : ""),
                onClick: function () { onOpen(p.id); }
              }, h("span", { className: "fl-dep-dot" }), p.title);
            }))
        : null,
      childCount
        ? h("div", { className: "fl-deprow" },
            h("span", { className: "fl-deplabel" }, "blocks"),
            h("span", { className: "fl-dep fl-dep--count" },
              childCount + (childCount === 1 ? " card" : " cards")))
        : null);
  }

  /** Unblock / ready / triage / comment, straight onto the board. */
  function Actions(props) {
    var task = props.task;
    var onDone = props.onDone;
    var onComment = props.onComment;
    var busyState = useState("");
    var busy = busyState[0];
    var setBusy = busyState[1];
    if (!task || !task.id) return null;

    function run(label, body) {
      setBusy(label);
      patchTask(task.id, body)
        .then(function () { onDone(null, label + " ✓"); })
        .catch(function (err) { onDone(tidyError(err), null); })
        .then(function () { setBusy(""); }, function () { setBusy(""); });
    }
    var st = task.status;
    var buttons = [];
    if (st === "blocked" || st === "scheduled") {
      buttons.push({ key: "unblock", label: "unblock", body: { status: "ready" }, primary: true,
                     title: "re-open this card into ready" });
    }
    if (st !== "ready" && st !== "blocked" && st !== "scheduled") {
      buttons.push({ key: "ready", label: "ready", body: { status: "ready" },
                     title: "move to ready so the dispatcher can pick it up" });
    }
    if (st !== "triage") {
      buttons.push({ key: "triage", label: "triage", body: { status: "triage" },
                     title: "send back to triage" });
    }
    return h("div", { className: "fl-actions" },
      buttons.map(function (b) {
        return h("button", {
          key: b.key, title: b.title, disabled: !!busy,
          className: cls("fl-act", b.primary && "is-primary", busy === b.label && "is-busy"),
          onClick: function () { run(b.label, b.body); }
        }, busy === b.label ? "…" : b.label);
      }),
      h("button", { className: "fl-act", onClick: onComment, title: "add a comment to this card" }, "comment"));
  }

  function Composer(props) {
    var textState = useState("");
    var text = textState[0];
    var setText = textState[1];
    var busyState = useState(false);
    var busy = busyState[0];
    var setBusy = busyState[1];
    var ref = useRef(null);
    useEffect(function () { if (props.autoFocus && ref.current) ref.current.focus(); }, [props.autoFocus]);

    function send() {
      var body = text.trim();
      if (!body || busy) return;
      setBusy(true);
      commentTask(props.taskId, body)
        .then(function () { setText(""); props.onDone(null, "comment added ✓"); })
        .catch(function (err) { props.onDone(tidyError(err), null); })
        .then(function () { setBusy(false); }, function () { setBusy(false); });
    }
    return h("div", { className: "fl-composer" },
      h("textarea", {
        ref: ref, value: text, rows: 3, placeholder: "add a comment to this card…",
        onChange: function (e) { setText(e.target.value); },
        onKeyDown: function (e) { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send(); }
      }),
      h("div", { className: "fl-composer-foot" },
        h("span", { className: "fl-hint" }, "⌘↵ to post"),
        h("button", { className: "fl-act is-primary", disabled: busy || !text.trim(), onClick: send },
          busy ? "posting…" : "post comment")));
  }

  // ── the card modal ────────────────────────────────────────────────────────────────────────
  function CardModal(props) {
    var taskId = props.taskId;
    var onClose = props.onClose;
    var onOpen = props.onOpen;
    var onBack = props.onBack;
    var canBack = props.canBack;
    var notify = props.notify;
    var cardState = useState(null);
    var card = cardState[0];
    var setCard = cardState[1];
    var errState = useState("");
    var err = errState[0];
    var setErr = errState[1];
    var nonceState = useState(0);
    var nonce = nonceState[0];
    var reload = nonceState[1];

    useEffect(function () {
      var cancelled = false;
      setCard(null);
      setErr("");
      SDK.fetchJSON(API + "/cards/" + encodeURIComponent(taskId))
        .then(function (r) { if (!cancelled) setCard(r.card); })
        .catch(function (e) { if (!cancelled) setErr(tidyError(e)); });
      return function () { cancelled = true; };
    }, [taskId, nonce, setCard, setErr]);

    useEffect(function () {
      function onKey(e) { if (e.key === "Escape") onClose(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [onClose]);

    function after(errMsg, ok) {
      notify(errMsg, ok);
      if (!errMsg) reload(function (n) { return n + 1; });
    }

    return h("div", { className: "fl-modal-backdrop", onMouseDown: function (e) {
      if (e.target === e.currentTarget) onClose();
    } },
      h("div", { className: "fl-modal", role: "dialog", "aria-modal": "true" },
        h("header", { className: "fl-modal-head" },
          canBack ? h("button", { className: "fl-icon-btn", onClick: onBack, title: "back" }, "‹") : null,
          h("div", { className: "fl-modal-title" }, card ? card.title : taskId),
          h("button", { className: "fl-icon-btn", onClick: onClose, title: "close" }, "×")),
        err ? h("div", { className: "fl-modal-body" }, h("div", { className: "fl-error" }, err)) : null,
        !card && !err ? h("div", { className: "fl-modal-body" }, h("div", { className: "fl-empty" }, "loading card…")) : null,
        card ? h("div", { className: "fl-modal-body" },
          h("div", { className: "fl-chips" },
            h(StatusBadge, { status: card.status }),
            h("span", { className: "fl-chip fl-chip--agent" }, "@" + (card.assignee || "unassigned")),
            h("span", { className: "fl-chip" }, card.id),
            card.tenant ? h("span", { className: "fl-chip" }, card.tenant) : null,
            card.branch_name ? h("span", { className: "fl-chip" }, card.branch_name) : null,
            h("span", { className: "fl-chip" }, "created " + since(card.created_at))),
          h(Timeline, { timeline: card.timeline }),
          h(Deps, { deps: card.deps, onOpen: onOpen }),
          h(Actions, { task: card, onDone: after, onComment: function () {
            var el = document.querySelector(".fl-modal .fl-composer textarea");
            if (el) { el.focus(); el.scrollIntoView({ block: "center" }); }
          } }),
          h("h3", { className: "fl-modal-h" }, "brief"),
          h("pre", { className: "fl-raw" }, card.body || "(no body)"),
          card.runs && card.runs.length
            ? h("div", null,
                h("h3", { className: "fl-modal-h" }, card.runs.length + " attempt" + (card.runs.length === 1 ? "" : "s")),
                h("ul", { className: "fl-runs" }, card.runs.map(function (r) {
                  return h("li", { key: r.id },
                    h("span", { className: "fl-badge fl-badge--" + (r.outcome || r.status) }, r.outcome || r.status),
                    h("span", { className: "fl-run-meta" }, "@" + (r.profile || "?") + " · " + since(r.started_at)),
                    r.summary ? h("span", { className: "fl-run-sum", title: r.summary }, r.summary) : null);
                })))
            : null,
          card.comments && card.comments.length
            ? h("div", null,
                h("h3", { className: "fl-modal-h" }, "comments"),
                card.comments.map(function (c) {
                  return h("div", { key: c.id, className: "fl-comment" },
                    h("div", { className: "fl-comment-head" }, "@" + (c.author || "?") + " · " + since(c.created_at)),
                    h("div", { className: "fl-comment-body" }, c.body));
                }))
            : null,
          h("h3", { className: "fl-modal-h" }, "add a comment"),
          h(Composer, { taskId: card.id, onDone: after, autoFocus: false })) : null));
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
      function go() { if (!cancelled) stream.setVisible(paneId, true); }
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

  // ── the stat tiles ────────────────────────────────────────────────────────────────────────
  function Stat(props) {
    // A cap bar and the number under it are not redundant: the bar shows the shape of the budget
    // at a glance, the text says what the budget actually is. At 2% of a $3 cap the bar alone is
    // a dot.
    return h("div", { className: "fl-stat fl-stat--" + props.tone, title: props.barTitle || "" },
      h("div", { className: "fl-stat-label" }, props.label),
      h("div", { className: "fl-stat-value" }, props.value),
      (props.bar !== null && props.bar !== undefined)
        ? h("div", { className: "fl-bar" },
            h("i", { style: { width: props.bar + "%" }, className: props.bar > 85 ? "is-hot" : "" }))
        : null,
      h("div", { className: "fl-stat-sub" }, props.sub || ""));
  }

  // ── one pane ──────────────────────────────────────────────────────────────────────────────
  function Pane(props) {
    var pane = props.pane;
    var store = props.store;
    var stream = props.stream;
    var wide = props.wide;
    var onWide = props.onWide;
    var onOpenCard = props.onOpenCard;
    var notify = props.notify;

    var hostRef = useRef(null);
    var visRef = useRef(false);
    var visState = useState(false);
    var setVis = visState[1];
    var filterState = useState("all");
    var filter = filterState[0];
    var setFilter = filterState[1];
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
        h("div", { className: "fl-pane-title" }, pane.title),
        h("button", {
          className: "fl-icon-btn", onClick: function () { onWide(wide ? null : pane.id); },
          title: wide ? "restore to the grid" : "expand across the grid"
        }, wide ? "⤡" : "⤢")),
      h("div", { className: "fl-chips" },
        h("span", { className: "fl-chip fl-chip--agent" }, "@" + (pane.agent || "?")),
        st.model ? h("span", { className: "fl-chip", title: st.model }, st.model) : null,
        pane.task_id
          ? h("button", { className: "fl-chip fl-chip--link", title: "open this card",
                          onClick: function () { onOpenCard(pane.task_id); } }, pane.task_id)
          : null,
        pane.tenant ? h("span", { className: "fl-chip" }, pane.tenant) : null,
        pane.surface ? h("span", { className: "fl-chip" }, pane.surface) : null,
        pane.attempt > 1 ? h("span", { className: "fl-chip fl-chip--warn" }, "attempt " + pane.attempt) : null),
      h("div", { className: "fl-stats" },
        h(Stat, {
          tone: "cost", label: "cost", value: money(st.cost_usd),
          sub: (st.cost_status === "estimated" ? "est" : (st.cost_status || "")) +
               (pane.cost_cap ? " · " + cap(pane.cost_cap) + " cap" : ""),
          bar: capPct,
          barTitle: pane.cost_cap ? money(st.cost_usd) + " of a " + money(pane.cost_cap) + " cap" : null
        }),
        h(Stat, {
          tone: "tokens", label: "tokens", value: tok((tokens.input_tokens || 0) + (tokens.output_tokens || 0)),
          sub: tok(tokens.cache_read_tokens) + " cached"
        }),
        h(Stat, { tone: "steps", label: "tool calls", value: String(st.tool_calls || 0),
                  sub: (st.api_calls || 0) + " api calls" }),
        h(Stat, {
          tone: "time", label: "elapsed",
          value: dur(st.elapsed_s !== undefined ? st.elapsed_s : pane.elapsed_s),
          sub: (pane.heartbeat_age_s !== null && pane.heartbeat_age_s !== undefined)
            ? "beat " + ago(pane.heartbeat_age_s) : ""
        })),
      h(Timeline, { timeline: pane.timeline }),
      h(Deps, { deps: pane.deps, onOpen: onOpenCard }),
      pane.task_id
        ? h(Actions, {
            task: { id: pane.task_id, status: pane.status },
            onDone: function (err, ok) { notify(err, ok); stream.refreshPanes(); },
            onComment: function () { onOpenCard(pane.task_id); }
          })
        : null,
      h("nav", { className: "fl-tabs" }, tabs.map(function (t) {
        return h("button", {
          key: t, className: cls("fl-tab", filter === t && "is-active"),
          onClick: function () { setFilter(t); }
        }, t);
      })),
      filter === "card"
        ? h(PaneCard, { paneId: pane.id })
        : visState[0]
          ? h(Feed, { paneId: pane.id, store: store, stream: stream, filter: filter })
          : h("div", { className: "fl-feed-wrap" },
              h("div", { className: "fl-feed fl-feed--idle" },
                h("div", { className: "fl-empty" }, "scroll into view to stream"))));
  }

  function PaneCard(props) {
    var cardState = useState(null);
    var card = cardState[0];
    var setCard = cardState[1];
    useEffect(function () {
      var cancelled = false;
      SDK.fetchJSON(API + "/panes/" + encodeURIComponent(props.paneId) + "/card")
        .then(function (r) { if (!cancelled) setCard(r.card); })
        .catch(function () { if (!cancelled) setCard({ body: "card unavailable" }); });
      return function () { cancelled = true; };
    }, [props.paneId, setCard]);
    return h("div", { className: "fl-card-body" },
      h("pre", { className: "fl-raw" }, (card && card.body) || "loading brief…"));
  }

  // ── Up next ───────────────────────────────────────────────────────────────────────────────
  function UpNext(props) {
    var onOpenCard = props.onOpenCard;
    var notify = props.notify;
    var rowsState = useState(null);
    var rows = rowsState[0];
    var setRows = rowsState[1];
    var nonceState = useState(0);
    var nonce = nonceState[0];
    var reload = nonceState[1];

    useEffect(function () {
      var cancelled = false;
      SDK.fetchJSON(API + "/queue")
        .then(function (r) { if (!cancelled) setRows(r.queue || []); })
        .catch(function () { if (!cancelled) setRows([]); });
      var t = window.setInterval(function () { reload(function (n) { return n + 1; }); }, 20000);
      return function () { cancelled = true; window.clearInterval(t); };
    }, [nonce, setRows, reload]);

    if (rows === null) return h("div", { className: "fl-blank" }, "reading the queue…");
    if (!rows.length) return h("div", { className: "fl-blank" }, "Nothing waiting. The board is clear.");

    return h("div", { className: "fl-queue" },
      h("div", { className: "fl-queue-note" },
        rows.length + " card" + (rows.length === 1 ? "" : "s") + " waiting — nothing-blocking first, " +
        "then by how close the column is to a worker, then newest first"),
      rows.map(function (c) {
        var blocked = c.blocked_by || [];
        return h("article", { key: c.id, className: cls("fl-qrow", blocked.length && "is-blocked") },
          h("div", { className: "fl-qmain" },
            h("button", { className: "fl-qtitle", onClick: function () { onOpenCard(c.id); },
                          title: "open this card" }, c.title),
            h("div", { className: "fl-qmeta" },
              h(StatusBadge, { status: c.status }),
              h("span", { className: "fl-chip fl-chip--agent" }, "@" + (c.assignee || "unassigned")),
              h("span", { className: "fl-chip" }, c.id),
              c.tenant ? h("span", { className: "fl-chip" }, c.tenant) : null,
              h("span", { className: "fl-chip" }, "created " + since(c.created_at)),
              c.deps.children_count
                ? h("span", { className: "fl-chip" }, "blocks " + c.deps.children_count) : null),
            blocked.length
              ? h("div", { className: "fl-deprow" },
                  h("span", { className: "fl-deplabel is-blocking" }, "waiting on " + blocked.length),
                  blocked.map(function (p) {
                    return h("button", { key: p.id, className: "fl-dep is-open",
                                         title: p.title + " · " + p.status,
                                         onClick: function () { onOpenCard(p.id); } },
                      h("span", { className: "fl-dep-dot" }), p.title);
                  }))
              : h("div", { className: "fl-deprow" },
                  h("span", { className: "fl-deplabel is-clear" }, "nothing blocking"))),
          h(Actions, {
            task: c,
            onDone: function (err, ok) { notify(err, ok); reload(function (n) { return n + 1; }); },
            onComment: function () { onOpenCard(c.id); }
          }));
      }));
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
    var viewState = useState("live");
    var view = viewState[0];
    var setView = viewState[1];
    var agentState = useState("all");
    var agent = agentState[0];
    var pausedState = useState(false);
    var paused = pausedState[0];
    var wideState = useState(null);
    var stackState = useState([]);           // modal history, so a dependency chain is walkable
    var stack = stackState[0];
    var setStack = stackState[1];
    var toastState = useState(null);
    var toast = toastState[0];
    var setToast = toastState[1];

    var onPanes = useCallback(function (list) { setPanes(list); setSeen(true); }, [setPanes, setSeen]);
    var streamPair = useStream(store, onPanes);
    var stream = streamPair[0];
    var connected = streamPair[1];

    useEffect(function () { stream.setPaused(paused); }, [stream, paused]);

    var notify = useCallback(function (err, ok) {
      setToast({ text: err || ok, bad: !!err, at: Date.now() });
    }, [setToast]);
    useEffect(function () {
      if (!toast) return;
      var t = window.setTimeout(function () { setToast(null); }, toast.bad ? 9000 : 3500);
      return function () { window.clearTimeout(t); };
    }, [toast, setToast]);

    var openCard = useCallback(function (id) {
      setStack(function (s) { return s.concat([id]); });
    }, [setStack]);
    var closeCard = useCallback(function () { setStack([]); }, [setStack]);
    var backCard = useCallback(function () { setStack(function (s) { return s.slice(0, -1); }); }, [setStack]);

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

    return h("div", { className: "fl-root" },
      h("header", { className: "fl-head" },
        h("div", { className: "fl-head-text" },
          h("h1", null, "Live"),
          h("div", { className: "fl-sub" },
            visible.length + (visible.length === 1 ? " agent" : " agents") + " working · " +
            money(totalCost) + " spent on these runs")),
        h("div", { className: "fl-controls" },
          h("select", {
            className: "fl-select", value: agent, "aria-label": "filter by agent",
            onChange: function (e) { agentState[1](e.target.value); }
          }, agents.map(function (a) {
            return h("option", { key: a, value: a }, a === "all" ? "all agents" : "@" + a);
          })),
          h("button", {
            className: cls("fl-btn", paused && "is-active"),
            onClick: function () { pausedState[1](!paused); },
            title: paused ? "resume every feed" : "stop streaming without leaving the page"
          }, paused ? "resume" : "pause"),
          h("span", { className: cls("fl-conn", connected ? "is-on" : "is-off") },
            connected ? (paused ? "paused" : "streaming") : "reconnecting…"))),

      h("nav", { className: "fl-viewtabs" },
        h("button", { className: cls("fl-viewtab", view === "live" && "is-active"),
                      onClick: function () { setView("live"); } },
          "Live", h("span", { className: "fl-count" }, visible.length)),
        h("button", { className: cls("fl-viewtab", view === "next" && "is-active"),
                      onClick: function () { setView("next"); } }, "Up next")),

      h("div", { className: "fl-viewnote" }, view === "live"
        ? "Thoughts appear as they are written. Scroll back in any pane and it holds your place until you return to the tail."
        : "Everything the dispatcher has not started yet."),

      view === "next"
        ? h(UpNext, { onOpenCard: openCard, notify: notify })
        : !visible.length
          ? h("div", { className: "fl-blank" }, seen
              ? "No agent is running right now. A pane appears the moment the dispatcher spawns one."
              : "Looking for active agents…")
          : h("div", { className: "fl-grid" }, visible.map(function (p) {
              return h(Pane, {
                key: p.id, pane: p, store: store, stream: stream,
                wide: wideState[0] === p.id, onWide: wideState[1],
                onOpenCard: openCard, notify: notify
              });
            })),

      stack.length
        ? h(CardModal, {
            taskId: stack[stack.length - 1], onClose: closeCard, onBack: backCard,
            canBack: stack.length > 1, onOpen: openCard, notify: notify
          })
        : null,
      toast ? h("div", { className: cls("fl-toast", toast.bad && "is-bad") }, toast.text) : null);
  }

  REG.register("fleet-live", Page);
})();
