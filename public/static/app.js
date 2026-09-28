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
        el.addEventListener('click', function (e) { if (!e.target.closest('.toast__action')) el.remove(); });
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


    // -- club night: tap-to-toggle chips ---------------------------------------
    // <button class="chip-toggle" data-field="has_bible" aria-pressed="false"> inside
    // <li class="kid-row" data-url="/api/attendance/<d>/<cid>" data-name="Ezra A.">
    // Each tap sends the absolute new value (idempotent). Only that field is reconciled from the
    // response, so two quick taps on different chips can't clobber each other.
    function setPressed(el, on) {
        el.setAttribute('aria-pressed', on ? 'true' : 'false');
    }

    function initAttendanceChips() {
        document.addEventListener('click', function (event) {
            var chip = event.target.closest('.chip-toggle');
            if (!chip || chip.disabled) return;
            event.preventDefault();
            if (chip.dataset.busy) return;
            var row = chip.closest('[data-url]');
            var field = chip.getAttribute('data-field');
            var was = chip.getAttribute('aria-pressed') === 'true';
            var body = { set: {} };
            body.set[field] = !was;
            var seq = String(Number(chip.dataset.seq || 0) + 1);
            chip.dataset.seq = seq;
            var what = (chip.getAttribute('data-label') || 'that') + ' for ' + (row.getAttribute('data-name') || 'this kid');
            var send = function () {
                // A Retry from an old toast does nothing once the chip has been tapped again.
                if (chip.dataset.seq !== seq || chip.dataset.busy) return;
                chip.dataset.busy = '1';
                chip.classList.add('is-pending');
                setPressed(chip, !was);
                return post(row.getAttribute('data-url'), body, { what: what, retry: send })
                    .then(function (data) {
                        if (data.row && field in data.row) setPressed(chip, !!data.row[field]);
                    })
                    .catch(function () { if (chip.dataset.seq === seq) setPressed(chip, was); })
                    .then(function () {
                        delete chip.dataset.busy;
                        chip.classList.remove('is-pending');
                    });
            };
            send();
        });
    }

    // -- mark present (kid wasn't checked in at the kiosk) ----------------------
    function initMarkPresent() {
        document.addEventListener('click', function (event) {
            var btn = event.target.closest('[data-mark-present]');
            if (!btn || btn.disabled || btn.dataset.busy) return;
            event.preventDefault();
            var row = btn.closest('[data-url]');
            var on = btn.getAttribute('data-mark-present') === 'on';
            var name = row.getAttribute('data-name') || 'this kid';
            var question = on ? 'Mark ' + name + ' present?\nOnly if they’re here but weren’t checked in at the kiosk.'
                              : 'Undo marking ' + name + ' present? Their items for tonight are cleared too.';
            if (!confirm(question)) return;
            btn.dataset.busy = '1';
            post(row.getAttribute('data-url'), { set: { manual_present: on } }, { what: 'present for ' + name })
                .then(function () { location.reload(); })
                .catch(function () { delete btn.dataset.busy; });
        });
    }

    // -- tonight's section: one-tap Main / Silver / Gold -----------------------
    // <button class="credit-chip" data-url="/api/sections/<d>/<cid>" data-section="1.5"
    //         data-field="silver_credit" data-state="none|today|earlier">
    function applyCreditStates(section, states) {
        var sel = '[data-section="' + CSS.escape(section) + '"]';
        document.querySelectorAll('.credit-chip' + sel).forEach(function (el) {
            var st = states[el.getAttribute('data-field')];
            if (!st) return;
            el.setAttribute('data-state', st.state);
            el.setAttribute('aria-pressed', st.state === 'today' ? 'true' : 'false');
            el.disabled = st.state === 'earlier';
            var label = el.querySelector('.credit-chip__when');
            if (label) label.textContent = st.label || '';
        });
        // Keep the same section's row in the full list below in step.
        document.querySelectorAll('.mini-chip' + sel).forEach(function (el) {
            var st = states[el.getAttribute('data-field')];
            if (!st) return;
            el.classList.toggle('is-on', st.state !== 'none');
            if (st.state === 'none') el.textContent = el.getAttribute('data-off');
            else if (st.state === 'earlier' && st.label && el.getAttribute('data-field') === 'section_complete')
                el.textContent = st.label;                       // "✓ 9/16": earned on another night
            else el.textContent = el.getAttribute('data-on');
        });
    }

    function initCreditChips() {
        document.addEventListener('click', function (event) {
            var chip = event.target.closest('.credit-chip');
            if (!chip || chip.disabled || chip.dataset.busy) return;
            event.preventDefault();
            var was = chip.getAttribute('data-state') === 'today';
            var section = chip.getAttribute('data-section');
            var body = { section: section, field: chip.getAttribute('data-field'), value: !was };
            var seq = String(Number(chip.dataset.seq || 0) + 1);
            chip.dataset.seq = seq;
            var send = function () {
                if (chip.dataset.seq !== seq || chip.dataset.busy) return;
                chip.dataset.busy = '1';
                chip.classList.add('is-pending');
                chip.setAttribute('data-state', was ? 'none' : 'today');
                return post(chip.getAttribute('data-url'), body,
                            { what: (chip.getAttribute('data-label') || 'that credit') + ' for ' + section, retry: send })
                    .then(function (data) { applyCreditStates(section, data.states || {}); })
                    .catch(function (e) {
                        var st = e.result && e.result.data && e.result.data.states;
                        if (st) applyCreditStates(section, st);          // e.g. already earned on another night
                        else if (chip.dataset.seq === seq) chip.setAttribute('data-state', was ? 'today' : 'none');
                    })
                    .then(function () {
                        delete chip.dataset.busy;
                        chip.classList.remove('is-pending');
                    });
            };
            send();
        });
    }

    // -- assign a team to an unassigned kid (writes to MinistryPlatform) --------
    function initAssignDialog() {
        var dialog = document.getElementById('assign-dialog');
        if (!dialog || typeof dialog.showModal !== 'function') return;
        var current = null;
        var picked = null;
        var confirmBtn = dialog.querySelector('[data-assign-confirm]');
        document.addEventListener('click', function (event) {
            var open = event.target.closest('[data-assign]');
            if (open) {
                event.preventDefault();
                current = open.closest('[data-assign-url]');
                picked = null;
                dialog.querySelector('[data-assign-name]').textContent = current.getAttribute('data-name');
                dialog.querySelectorAll('[data-team]').forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
                confirmBtn.disabled = true;
                confirmBtn.textContent = 'Assign';
                dialog.showModal();
                return;
            }
            var teamBtn = event.target.closest('#assign-dialog [data-team]');
            if (teamBtn) {
                picked = teamBtn.getAttribute('data-team');
                dialog.querySelectorAll('[data-team]').forEach(function (b) {
                    b.setAttribute('aria-pressed', b === teamBtn ? 'true' : 'false');
                });
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Assign to ' + picked;
                return;
            }
            if (event.target.closest('[data-assign-cancel]')) {
                dialog.close();
                return;
            }
            if (event.target === dialog) {
                // Only a tap on the backdrop (outside the sheet) closes it, not a tap on the sheet's padding.
                var r = dialog.getBoundingClientRect();
                var inside = event.clientX >= r.left && event.clientX <= r.right &&
                             event.clientY >= r.top && event.clientY <= r.bottom;
                if (!inside) dialog.close();
                return;
            }
            if (event.target.closest('[data-assign-confirm]') && picked && current) {
                var row = current;
                var name = row.getAttribute('data-name');
                dialog.close();
                row.classList.add('is-moving');
                post(row.getAttribute('data-assign-url'), { team: picked }, { silent: true, what: 'the team' })
                    .then(function (data) {
                        toast(name + ' is on ' + data.kid.team + ' now');
                        row.remove();
                        setTimeout(function () { location.reload(); }, 1200);   // counts and pills
                    })
                    .catch(function (e) {
                        row.classList.remove('is-moving');
                        var res = e.result || {};
                        if (res.data && res.data.error === 'already_assigned') {
                            toast(name + ' is already on ' + (res.data.team || 'a team'));
                            row.remove();
                            setTimeout(function () { location.reload(); }, 1200);
                        } else {
                            handleFailure(res, 'the team');
                        }
                    });
            }
        });
    }

    // -- "Show all" without a page reload ---------------------------------------
    function initShowAll() {
        document.addEventListener('click', function (event) {
            var toggle = event.target.closest('[data-show-all-toggle]');
            if (!toggle) return;
            var list = document.querySelector('[data-kid-list]');
            if (!list) return;
            event.preventDefault();
            var on = !list.classList.contains('show-all');
            list.classList.toggle('show-all', on);
            document.querySelectorAll('[data-show-all-toggle]').forEach(function (t) {
                t.classList.toggle('is-on', on);
                if (t.getAttribute('role') === 'button') t.setAttribute('aria-pressed', on ? 'true' : 'false');
                var h = new URL(t.href, location.href);
                if (on) h.searchParams.delete('show'); else h.searchParams.set('show', 'all');
                t.href = h.pathname + h.search;
                t.textContent = on ? (t.getAttribute('data-on-label') || 'Here only')
                                   : (t.getAttribute('data-off-label') || 'Show all');
            });
            var url = new URL(location.href);
            if (on) url.searchParams.set('show', 'all'); else url.searchParams.delete('show');
            url.searchParams.delete('refresh');
            history.replaceState(null, '', url);
            document.querySelectorAll('.refresh-btn').forEach(function (a) {
                var r = new URL(a.href, location.href);
                if (on) r.searchParams.set('show', 'all'); else r.searchParams.delete('show');
                a.href = r.pathname + r.search;
            });
        });
    }

    // Live pages restored from the back/forward cache after a while: reload so check-ins are current.
    window.addEventListener('pageshow', function (event) {
        if (event.persisted && document.body.hasAttribute('data-live')) {
            var loaded = Number(document.body.getAttribute('data-loaded') || 0);
            if (Date.now() - loaded > 60000) location.reload();
        }
    });

    document.addEventListener('DOMContentLoaded', function () {
        document.body.setAttribute('data-loaded', String(Date.now()));
        initJsonForms();
        var current = document.querySelector('.team-switcher .is-current');
        if (current && current.scrollIntoView) current.scrollIntoView({ inline: 'center', block: 'nearest' });
    });
    initAttendanceChips();
    initMarkPresent();
    initCreditChips();
    initShowAll();
    document.addEventListener('DOMContentLoaded', initAssignDialog);

    window.tnt = { post: post, toast: toast, csrfToken: csrfToken, handleFailure: handleFailure };
})();
