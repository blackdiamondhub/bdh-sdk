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
 *             confirm(message, opts) -> Promise<bool>, alert(message),
 *             on(type, fn) for messages from the hub,
 *             stats(fn) adds app numbers to the hub's remote `frame state`.
 *
 * A {type: 'remote', id, cmd, args} message from the hub (only sent while the
 * hub's development remote is on) loads embed-remote.js, which runs a fixed set
 * of commands and answers with {type: 'remote-result', id, ok, value | error}.
 */
(function () {
    'use strict';
    if (window.bdh) return;
    var script = document.currentScript;

    // Page errors from the start, for the remote's `logs` (it loads later).
    var errors = [];
    function pageError(text) {
        errors.push({t: Date.now(), level: 'error', text: String(text).slice(0, 2000)});
        if (errors.length > 50) errors.shift();
    }
    window.addEventListener('error', function (e) { pageError(e.message + ' @ ' + e.filename + ':' + e.lineno); });
    window.addEventListener('unhandledrejection', function (e) { pageError('unhandled rejection: ' + (e.reason && (e.reason.stack || e.reason))); });

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

    var statsProviders = [];

    /* In-page confirm: the hub's iframe sandbox has no allow-modals, so the
     * browser ignores window.confirm/alert/prompt. Resolves true for OK.
     * opts: ok, cancel (null hides it), danger, near (element to sit by:
     * the frame is as tall as its content, so the viewport middle may be
     * off screen). */
    var dialogOpen = null;
    function confirmDialog(message, opts) {
        opts = opts || {};
        if (dialogOpen) dialogOpen(false);
        return new Promise(function (resolve) {
            var back = document.createElement('div');
            back.className = 'bdh-dialog-back';
            back.setAttribute('style', 'position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.55);');
            var box = document.createElement('div');
            box.className = 'bdh-dialog';
            box.setAttribute('role', 'alertdialog');
            box.setAttribute('aria-modal', 'true');
            box.setAttribute('style', 'position:absolute;left:50%;transform:translateX(-50%);width:min(440px,calc(100vw - 32px));' +
                'background:#18243a;color:#eef3fa;border:1px solid #26354f;border-radius:18px;padding:22px 22px 18px;' +
                'box-shadow:0 20px 60px rgba(0,0,0,.5);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;');
            var text = document.createElement('p');
            text.setAttribute('style', 'margin:0 0 18px;white-space:pre-line;');
            text.textContent = String(message);
            var row = document.createElement('div');
            row.setAttribute('style', 'display:flex;justify-content:flex-end;gap:10px;');
            function button(label, primary) {
                var b = document.createElement('button');
                b.type = 'button';
                b.textContent = label;
                b.setAttribute('style', 'min-height:44px;padding:0 18px;border-radius:12px;font:inherit;font-weight:600;cursor:pointer;' +
                    (primary ? 'border:0;color:#fff;background:' + (opts.danger ? '#c0392b' : '#2f7fd0') + ';'
                             : 'border:1px solid #33465f;color:#eef3fa;background:transparent;'));
                return b;
            }
            var cancel = opts.cancel === null ? null : button(opts.cancel || 'Cancel', false);
            var ok = button(opts.ok || 'OK', true);
            if (cancel) row.appendChild(cancel);
            row.appendChild(ok);
            box.appendChild(text);
            box.appendChild(row);
            back.appendChild(box);
            document.body.appendChild(back);

            var vh = window.innerHeight, h = box.offsetHeight, top = (vh - h) / 2;
            var near = opts.near && opts.near.getBoundingClientRect ? opts.near.getBoundingClientRect() : null;
            if (near && (near.top || near.bottom)) top = near.top + near.height / 2 - h / 2;
            box.style.top = Math.max(8, Math.min(top, vh - h - 8)) + 'px';

            var focusBack = document.activeElement;
            function done(value) {
                if (dialogOpen !== done) return;
                dialogOpen = null;
                document.removeEventListener('keydown', key, true);
                back.remove();
                if (focusBack && focusBack.focus) try { focusBack.focus({preventScroll: true}); } catch (e) { /* gone */ }
                resolve(value);
            }
            function key(e) {
                if (e.key === 'Escape') { e.preventDefault(); done(false); }
                else if (e.key === 'Enter') { e.preventDefault(); done(true); }
            }
            dialogOpen = done;
            ok.addEventListener('click', function () { done(true); });
            if (cancel) cancel.addEventListener('click', function () { done(false); });
            back.addEventListener('click', function (e) { if (e.target === back) done(false); });
            document.addEventListener('keydown', key, true);
            (cancel || ok).focus({preventScroll: true});
        });
    }

    /* <button data-bdh-confirm="Remove this feed?" data-bdh-confirm-ok="Remove"> (also on
     * links or anything clickable): asks first, then repeats the click (so a submit
     * button still sends its name/value). data-bdh-confirm-danger="0" for a non-destructive OK. */
    document.addEventListener('click', function (e) {
        var el = e.target.closest && e.target.closest('[data-bdh-confirm]');
        if (!el) return;
        if (el._bdhConfirmed) { el._bdhConfirmed = false; return; }
        e.preventDefault();
        e.stopImmediatePropagation();
        confirmDialog(el.getAttribute('data-bdh-confirm'), {
            ok: el.getAttribute('data-bdh-confirm-ok') || 'OK',
            danger: el.getAttribute('data-bdh-confirm-danger') !== '0',
            near: el,
        }).then(function (yes) {
            if (!yes) return;
            el._bdhConfirmed = true;
            el.click();
        });
    }, true);

    window.bdh = {
        embedded: framed && !!hubOrigin,
        hubOrigin: hubOrigin,
        send: send,
        navigate: navigate,
        toast: function (message, level) { return send('toast', {message: String(message), level: level || 'info'}); },
        confirm: confirmDialog,
        alert: function (message) { return confirmDialog(message, {cancel: null, danger: false}); },
        on: on,
        stats: function (fn) { if (typeof fn === 'function') statsProviders.push(fn); },
        _statsProviders: statsProviders,
        _errors: errors,
    };
    if (!window.bdh.embedded) return;
    document.documentElement.classList.add('bdh-embedded');

    var agent = null;
    function loadAgent() {
        if (!agent) agent = new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            s.src = new URL('embed-remote.js', script ? script.src : location.href).href;
            s.onload = function () { window.bdh._remote ? resolve(window.bdh._remote) : reject(new Error('remote agent missing')); };
            s.onerror = function () { agent = null; reject(new Error('could not load embed-remote.js')); };
            document.head.appendChild(s);
        });
        return agent;
    }
    on('remote', function (d) {
        loadAgent().then(function (run) { return run(d.cmd, d.args || {}); })
            .then(function (value) { send('remote-result', {id: d.id, ok: true, value: value}); },
                  function (err) { send('remote-result', {id: d.id, ok: false, error: String(err && (err.stack || err))}); });
    });

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
