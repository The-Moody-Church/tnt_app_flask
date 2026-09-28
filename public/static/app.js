/* TNT Tracker client helpers. No framework, no build step.
 *
 * tnt.post(url, body) — POST with the CSRF token; resolves with the JSON body only when the
 * server says {"ok": true}. Anything else (expired sign-in, stale page, network error,
 * validation error) shows a message and rejects, so a failed save is never mistaken for success.
 */
(function () {
    'use strict';

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // -- toasts ---------------------------------------------------------------
    function region() {
        var el = document.querySelector('.toast-region');
        if (!el) {
            el = document.createElement('div');
            el.className = 'toast-region';
            el.setAttribute('aria-live', 'polite');
            document.body.appendChild(el);
        }
        return el;
    }

    function toast(message, opts) {
        opts = opts || {};
        var el = document.createElement('div');
        el.className = 'toast' + (opts.type === 'error' ? ' toast--error' : '');
        el.textContent = message;
        if (opts.action) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'toast__action';
            btn.textContent = opts.action.label;
            btn.addEventListener('click', function () { el.remove(); opts.action.run(); });
            el.appendChild(btn);
        }
        region().appendChild(el);
        setTimeout(function () { el.remove(); }, opts.type === 'error' ? 7000 : 3500);
    }

    function sessionBanner(loginUrl) {
        if (document.querySelector('.session-banner')) return;
        var el = document.createElement('div');
        el.className = 'session-banner';
        el.setAttribute('role', 'alert');
        el.innerHTML = 'Your sign-in expired — that change was <strong>not saved</strong>. ';
        var a = document.createElement('a');
        a.href = loginUrl || ('/login?next=' + encodeURIComponent(location.pathname + location.search));
        a.textContent = 'Sign in';
        el.appendChild(a);
        document.body.appendChild(el);
    }

    function handleFailure(res, what) {
        var data = res.data || {};
        if (res.kind === 'auth') { sessionBanner(data.login_url); return; }
        if (res.kind === 'csrf') {
            toast('This page is out of date — reloading…', { type: 'error' });
            setTimeout(function () { location.reload(); }, 1500);
            return;
        }
        if (res.kind === 'forbidden') { alert(data.message || 'Not authorized.'); location.href = '/'; return; }
        if (res.kind === 'network' || res.kind === 'timeout') {
            toast('Couldn’t reach the server — ' + (what || 'that change') + ' may not have been saved. Check, then retry.',
                  { type: 'error', action: res.retry ? { label: 'Retry', run: res.retry } : null });
            return;
        }
        toast(data.message || ('Not saved (' + res.status + ')'), { type: 'error' });
    }

    // -- POST -----------------------------------------------------------------
    function post(url, body, opts) {
        opts = opts || {};
        var headers = { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'fetch', 'Accept': 'application/json' };
        var payload = body;
        if (!(body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            payload = JSON.stringify(body || {});
        }
        var ctrl = new AbortController();
        var timer = setTimeout(function () { ctrl.abort(); }, opts.timeoutMs || 10000);
        return fetch(url, { method: 'POST', body: payload, headers: headers, credentials: 'same-origin',
                            redirect: 'manual', signal: ctrl.signal })
            .then(function (resp) {
                clearTimeout(timer);
                if (resp.type === 'opaqueredirect') return { kind: 'auth', status: 0, data: {} };
                return resp.json().catch(function () { return {}; }).then(function (data) {
                    if (resp.ok && data.ok === true) return { kind: 'ok', status: resp.status, data: data };
                    var kind = 'app';
                    if (resp.status === 401) kind = 'auth';
                    else if (data.error === 'csrf_failed') kind = 'csrf';
                    else if (data.error === 'csrf_referrer') kind = 'app';
                    else if (resp.status === 403) kind = 'forbidden';
                    return { kind: kind, status: resp.status, data: data };
                });
            }, function (err) {
                clearTimeout(timer);
                return { kind: err && err.name === 'AbortError' ? 'timeout' : 'network', status: 0, data: {} };
            })
            .then(function (res) {
                if (res.kind === 'ok') return res.data;
                res.retry = opts.retry;
                if (!opts.silent) handleFailure(res, opts.what);
                var e = new Error(res.kind);
                e.result = res;
                throw e;
            });
    }

    // -- forms that post via fetch: <form data-json-form> ---------------------
    function initJsonForms() {
        document.querySelectorAll('form[data-json-form]').forEach(function (form) {
            var busy = false;
            form.addEventListener('submit', function (event) {
                event.preventDefault();
                if (busy) return;
                busy = true;
                var btn = form.querySelector('button[type="submit"]');
                if (btn) btn.classList.add('loading');
                post(form.action, new FormData(form), { what: form.getAttribute('data-what') || 'that change' })
                    .then(function (data) {
                        if (data.redirect) {
                            location.href = data.redirect;
                        } else if (data.message) {
                            toast(data.message);
                            busy = false;
                            if (btn) btn.classList.remove('loading');
                        } else {
                            location.reload();
                        }
                    })
                    .catch(function () {
                        busy = false;
                        if (btn) btn.classList.remove('loading');
                    });
            });
        });
        // Submit only once at least one toggle is on: <form data-require-one>
        document.querySelectorAll('form[data-require-one]').forEach(function (form) {
            var btn = form.querySelector('button[type="submit"]');
            var boxes = form.querySelectorAll('input[type="checkbox"]');
            function update() {
                if (btn) btn.disabled = !Array.prototype.some.call(boxes, function (b) { return b.checked; });
            }
            boxes.forEach(function (b) { b.addEventListener('change', update); });
            update();
        });
    }

    // -- small declarative actions --------------------------------------------
    document.addEventListener('click', function (event) {
        var el = event.target.closest('[data-action]');
        if (!el) return;
        var action = el.getAttribute('data-action');
        if (action === 'toggle-edit') {
            var target = document.getElementById(el.getAttribute('data-target') || 'attendanceDetails');
            if (target) target.classList.toggle('edit-mode');
        } else if (action === 'back') {
            history.back();
        } else if (action === 'reload') {
            location.reload();
        }
    });

    document.addEventListener('DOMContentLoaded', initJsonForms);

    window.tnt = { post: post, toast: toast, csrfToken: csrfToken, handleFailure: handleFailure };
})();
