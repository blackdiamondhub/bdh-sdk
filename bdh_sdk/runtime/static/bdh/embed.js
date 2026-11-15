/* The app side of the hub's iframe contract.
 *
 * Messages to the hub are {bdh: 1, type, ...}, posted only to the hub origin
 * the proxy vouched for (<meta name="bdh-hub-origin">). Messages from anywhere
 * else, or from a window other than our parent, are ignored.
 *
 *   title         {title}               the navbar title for this page
 *   resize        {height}              content height in CSS px
 *   activity      {}                    the visitor touched the screen (screensaver)
 *   navigate      {name, args} | {path} open a hub page (URL name or same-origin path)
 *   toast         {message, level}      a short notice drawn by the hub
 *   theme         {background}          the page background colour (hex/rgb/rgba)
 *   open-overlay  {kind, ...}           ask the hub to open one of its overlays
 *
 * window.bdh: send(type, data), navigate(target), toast(message, level),
 *             on(type, fn) for messages from the hub.
 */
(function () {
    'use strict';
    if (window.bdh) return;

    var meta = document.querySelector('meta[name="bdh-hub-origin"]');
    var hubOrigin = '';
    try {
        var u = new URL(meta ? meta.content : '');
        if (u.protocol === 'http:' || u.protocol === 'https:') hubOrigin = u.origin;
    } catch (e) { /* not embedded by a known hub */ }
    var framed = window.parent && window.parent !== window;
    var handlers = {};

    function send(type, data) {
        if (!framed || !hubOrigin) return false;
        var msg = {bdh: 1, type: String(type)};
        if (data) for (var k in data) if (Object.prototype.hasOwnProperty.call(data, k) && k !== 'bdh' && k !== 'type') msg[k] = data[k];
        window.parent.postMessage(msg, hubOrigin);
        return true;
    }

    function navigate(target) {
        if (typeof target === 'string') target = {path: target};
        if (!send('navigate', target) && target.path) window.location.href = target.path;
    }

    function on(type, fn) { (handlers[type] = handlers[type] || []).push(fn); }

    window.addEventListener('message', function (e) {
        if (!hubOrigin || e.origin !== hubOrigin || e.source !== window.parent) return;
        var d = e.data;
        if (!d || d.bdh !== 1 || typeof d.type !== 'string') return;
        (handlers[d.type] || []).forEach(function (fn) {
            try { fn(d); } catch (err) { console.error(err); }
        });
    });

    window.bdh = {
        embedded: framed && !!hubOrigin,
        hubOrigin: hubOrigin,
        send: send,
        navigate: navigate,
        toast: function (message, level) { return send('toast', {message: String(message), level: level || 'info'}); },
        on: on,
    };
    if (!window.bdh.embedded) return;
    document.documentElement.classList.add('bdh-embedded');

    // Activity, at most every 5 s, so the hub's screensaver and idle timers see touches.
    var lastActivity = 0;
    function activity() {
        var now = Date.now();
        if (now - lastActivity < 5000) return;
        lastActivity = now;
        send('activity');
    }
    ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(function (ev) {
        window.addEventListener(ev, activity, {passive: true, capture: true});
    });

    // Height, whenever it changes.
    var lastHeight = -1, pending = false;
    function measure() {
        pending = false;
        var h = Math.ceil(document.documentElement.scrollHeight);
        if (h !== lastHeight) { lastHeight = h; send('resize', {height: h}); }
    }
    function schedule() { if (!pending) { pending = true; requestAnimationFrame(measure); } }

    // <a data-bdh-nav="url_name" data-bdh-args='["x"]'> or <a target="_top" href="/hub/path">.
    document.addEventListener('click', function (e) {
        var a = e.target.closest && e.target.closest('a[data-bdh-nav], a[target="_top"]');
        if (!a) return;
        var name = a.getAttribute('data-bdh-nav');
        if (name) {
            var args = [];
            try { args = JSON.parse(a.getAttribute('data-bdh-args') || '[]'); } catch (err) { /* none */ }
            e.preventDefault();
            navigate({name: name, args: args});
            return;
        }
        var href = a.getAttribute('href') || '';
        if (href.charAt(0) === '/' && href.charAt(1) !== '/') {
            e.preventDefault();
            navigate({path: href});
        }
    });

    // The page background, so the hub paints the same colour behind the frame
    // (no white flash on the next open or during fullscreen transitions).
    function transparent(c) { return !c || c === 'transparent' || /^rgba\(.*,\s*0\s*\)$/.test(c); }
    function sendTheme() {
        var c = document.body ? getComputedStyle(document.body).backgroundColor : '';
        if (transparent(c)) c = getComputedStyle(document.documentElement).backgroundColor;
        if (!transparent(c)) send('theme', {background: c});
    }

    function ready() {
        var t = document.getElementById('bdh-title');
        var title = t ? (t.content ? t.content.textContent : t.textContent).trim() : '';
        send('title', {title: title || document.title});
        sendTheme();
        if (window.ResizeObserver) new ResizeObserver(schedule).observe(document.documentElement);
        window.addEventListener('load', schedule);
        schedule();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
    else ready();
})();
