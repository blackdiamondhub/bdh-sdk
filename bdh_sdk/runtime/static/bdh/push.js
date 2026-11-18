/* Live updates from the hub for an app page.
 *
 *   <script src="{% static 'bdh/push.js' %}"></script>
 *   BDHPush.live({topics: ["cameras/#"], refresh: loadEvents});
 *
 * ``refresh`` is the page's own reload (it may return a Promise). It is called
 * on a slow poll while the push socket is healthy, on a fast poll while it
 * isn't, and soon after any invalidation for ``topics``. Calls never overlap:
 * an invalidation that lands mid-refresh queues exactly one more.
 *
 * Server messages (JSON): {t:"hello", patterns}, {t:"inv", topics},
 * {t:"resync"}, {t:"ping"}. The client answers pings and sends {t:"stats"}.
 */
(function () {
  "use strict";

  var INFO = "/_bdh/push/info";
  var SOCKET = "/_bdh/push";
  var PROTO = 1;

  function matches(pattern, topic) {
    var p = pattern.split("/"), t = topic.split("/");
    for (var i = 0; i < p.length; i++) {
      if (p[i] === "#") return true;
      if (i >= t.length) return false;
      if (p[i] !== "+" && p[i] !== t[i]) return false;
    }
    return p.length === t.length;
  }

  function live(opts) {
    var topics = opts.topics || ["#"];
    var refresh = opts.refresh;
    var pollMs = opts.pollMs || 30000;
    var fallbackMs = opts.fallbackMs || 300000;
    var debounceMs = opts.debounceMs == null ? 250 : opts.debounceMs;
    var healthyMs = opts.healthyMs || 60000;
    var reprobeMs = opts.reprobeMs || 600000;
    var deadMs = opts.deadMs || 45000;
    var onState = opts.onState || function () {};

    var stats = {state: "probing", connects: 0, closes: 0, lastClose: null, invalidations: 0,
                 resyncs: 0, refreshes: 0, refreshErrors: 0, polls: 0, since: Date.now()};
    var ws = null, backoff = 1000, healthy = false, stopped = false;
    var pollTimer = null, debounceTimer = null, healthyTimer = null, deadTimer = null,
        retryTimer = null, statsTimer = null;
    var running = false, again = false;

    function setState(s) {
      if (stats.state === s) return;
      stats.state = s;
      try { onState(s, stats); } catch (e) { /* page callback */ }
    }

    function doRefresh() {
      if (running) { again = true; return; }
      running = true;
      stats.refreshes++;
      var done = function () {
        running = false;
        if (again) { again = false; doRefresh(); }
      };
      try {
        Promise.resolve(refresh()).then(done, function () { stats.refreshErrors++; done(); });
      } catch (e) { stats.refreshErrors++; done(); }
    }

    function soon() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(doRefresh, debounceMs);
    }

    function schedulePoll() {
      clearInterval(pollTimer);
      pollTimer = setInterval(function () { stats.polls++; doRefresh(); },
                              healthy ? fallbackMs : pollMs);
    }

    function setHealthy(h) {
      if (healthy === h) return;
      healthy = h;
      schedulePoll();
    }

    function armDead() {
      clearTimeout(deadTimer);
      deadTimer = setTimeout(function () { if (ws) ws.close(4000, "silent"); }, deadMs);
    }

    function send(msg) {
      if (ws && ws.readyState === 1) {
        try { ws.send(JSON.stringify(msg)); } catch (e) { /* closing */ }
      }
    }

    function retry(ms) {
      clearTimeout(retryTimer);
      retryTimer = setTimeout(probe, ms);
    }

    function connect() {
      var scheme = location.protocol === "https:" ? "wss://" : "ws://";
      try {
        ws = new WebSocket(scheme + location.host + SOCKET);
      } catch (e) {
        retry(reprobeMs);
        return;
      }
      setState("connecting");
      ws.onopen = function () {
        stats.connects++;
        backoff = 1000;
        setState("open");
        armDead();
        // Anything published while we were away was missed.
        if (stats.connects > 1) soon();
        clearTimeout(healthyTimer);
        healthyTimer = setTimeout(function () { setHealthy(true); }, healthyMs);
      };
      ws.onmessage = function (ev) {
        armDead();
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.t === "ping") { send({t: "pong"}); return; }
        if (msg.t === "resync") { stats.resyncs++; soon(); return; }
        if (msg.t === "inv") {
          var hit = (msg.topics || []).some(function (topic) {
            return topics.some(function (p) { return matches(p, topic); });
          });
          if (hit) { stats.invalidations++; soon(); }
        }
      };
      ws.onclose = function (ev) {
        ws = null;
        stats.closes++;
        stats.lastClose = ev.code;
        clearTimeout(healthyTimer);
        clearTimeout(deadTimer);
        setHealthy(false);
        if (stopped) return;
        if (ev.code === 4401 || ev.code === 4403 || ev.code === 4404) {
          // Not allowed (or push off for this app): poll, and ask again later.
          setState("off");
          retry(reprobeMs);
          return;
        }
        setState("retrying");
        var wait = ev.code === 4429 ? 30000 : backoff;
        backoff = Math.min(backoff * 2, 30000);
        clearTimeout(retryTimer);
        retryTimer = setTimeout(connect, wait / 2 + Math.random() * wait / 2);
      };
    }

    function probe() {
      if (stopped) return;
      setState("probing");
      fetch(INFO, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (info) {
        if (stopped) return;
        if (info && info.proto === PROTO) connect();
        else { setState("off"); retry(reprobeMs); }
      }, function () {
        if (!stopped) { setState("off"); retry(reprobeMs); }
      });
    }

    schedulePoll();
    statsTimer = setInterval(function () {
      send({t: "stats", s: {connects: stats.connects, closes: stats.closes, lastClose: stats.lastClose,
                            invalidations: stats.invalidations, resyncs: stats.resyncs,
                            refreshes: stats.refreshes, refreshErrors: stats.refreshErrors,
                            polls: stats.polls}});
    }, 60000);
    probe();

    return {
      stats: function () { return Object.assign({healthy: healthy}, stats); },
      refresh: doRefresh,
      stop: function () {
        stopped = true;
        [pollTimer, statsTimer].forEach(clearInterval);
        [debounceTimer, healthyTimer, deadTimer, retryTimer].forEach(clearTimeout);
        if (ws) ws.close(1000);
      },
    };
  }

  window.BDHPush = {live: live, matches: matches};
})();
