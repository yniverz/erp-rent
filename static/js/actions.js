/*
 * CSP-friendly event wiring (no inline on*="" handlers).
 *
 *   data-click / data-change / data-input = "action-name [action-name …]"
 *   data-confirm = "Text"   on a <form>: confirm before submit
 *                           on a button/link: confirm before the click goes through
 *
 * Only actions registered here or via Actions.register({...}) can run, so
 * injected markup can't call arbitrary functions.
 */
(function () {
    'use strict';

    var handlers = {
        'toggle-nav': function () {
            document.querySelector('.nav-links').classList.toggle('open');
        },
        'remove-row': function (el) { el.closest('tr').remove(); },
        'select-text': function (el) { el.select(); },
        'autosubmit': function (el) { el.form.submit(); },
        'print': function () { window.print(); },
        'close-window': function () { window.close(); },
        'history-back': function (el, e) { e.preventDefault(); history.back(); },
        'tree-toggle': function (el) {
            el.classList.toggle('open');
            var sub = el.closest('li').querySelector(':scope > .category-tree');
            if (sub) sub.classList.toggle('collapsed');
        }
    };

    window.Actions = {
        register: function (map) {
            Object.keys(map).forEach(function (k) { handlers[k] = map[k]; });
        }
    };

    function dispatch(attr) {
        return function (e) {
            var el = e.target.closest ? e.target.closest('[' + attr + ']') : null;
            if (!el) return;
            el.getAttribute(attr).split(/\s+/).forEach(function (name) {
                if (!name) return;
                var fn = handlers[name];
                if (fn) fn(el, e);
                else console.warn('Unknown action: ' + name);
            });
        };
    }

    // Confirmations run first (capture phase) so they can stop the action
    document.addEventListener('submit', function (e) {
        var msg = e.target.getAttribute && e.target.getAttribute('data-confirm');
        if (msg && !window.confirm(msg)) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    }, true);
    document.addEventListener('click', function (e) {
        var el = e.target.closest ? e.target.closest('a[data-confirm], button[data-confirm]') : null;
        if (el && !window.confirm(el.getAttribute('data-confirm'))) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    }, true);

    document.addEventListener('click', dispatch('data-click'));
    document.addEventListener('change', dispatch('data-change'));
    document.addEventListener('input', dispatch('data-input'));
})();
