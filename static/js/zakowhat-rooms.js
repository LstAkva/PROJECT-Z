/* Jonli xona (live rooms): shared helpers for the join, phone, projector and host pages. */
(function () {
  'use strict';

  function api(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    opts.headers = Object.assign({ 'Accept': 'application/json' }, opts.headers);
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    opts.cache = 'no-store';
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          const detail = data && data.detail;
          const err = new Error(typeof detail === 'string' ? detail : 'Xatolik yuz berdi. Qayta urinib ko‘ring.');
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  // The team token lives only in this browser (per-viewer convenience); losing it means joining again.
  function tokenKey(code) { return 'zakowhat_room_' + code; }
  function getToken(code) { try { return localStorage.getItem(tokenKey(code)); } catch (e) { return null; } }
  function setToken(code, token) { try { localStorage.setItem(tokenKey(code), token); } catch (e) {} }
  function clearToken(code) { try { localStorage.removeItem(tokenKey(code)); } catch (e) {} }

  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(function (kv) {
      if (kv[0] === 'class') node.className = kv[1];
      else if (kv[0].startsWith('on')) node.addEventListener(kv[0].slice(2), kv[1]);
      else node.setAttribute(kv[0], kv[1]);
    });
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clock(seconds) {
    if (seconds === null || seconds === undefined) return '';
    const s = Math.max(0, seconds);
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }

  /* Local countdown between polls: the server's remaining_seconds is the source of truth. */
  function Countdown(node) {
    let left = null, paused = false;
    setInterval(function () {
      if (left === null || paused) return;
      left = Math.max(0, left - 1);
      draw();
    }, 1000);
    function draw() {
      node.textContent = left === null ? '' : (paused ? '⏸ ' : '') + clock(left);
      node.classList.toggle('is-low', left !== null && left <= 10);
    }
    return { set: function (seconds, isPaused) { left = (seconds === null || seconds === undefined) ? null : seconds; paused = !!isPaused; draw(); } };
  }

  /* Repeating poll; slower while the tab is hidden. Stops when fn returns false. */
  function poll(fn, ms) {
    let stopped = false;
    function tick() {
      if (stopped) return;
      Promise.resolve().then(fn).then(function (keep) {
        if (keep === false) { stopped = true; return; }
        setTimeout(tick, document.hidden ? ms * 3 : ms);
      }).catch(function () { setTimeout(tick, ms * 2); });
    }
    tick();
    return function () { stopped = true; };
  }

  function standingsTable(rows, meId) {
    const table = el('table', { class: 'room-table' });
    const head = el('tr');
    head.appendChild(el('th', {}, 'O‘rin'));
    head.appendChild(el('th', {}, 'Jamoa'));
    head.appendChild(el('th', { style: 'text-align:right' }, 'Ball'));
    table.appendChild(el('thead')).appendChild(head);
    const body = table.appendChild(el('tbody'));
    (rows || []).forEach(function (r) {
      const tr = el('tr', r.team_id === meId ? { class: 'is-me' } : {});
      tr.appendChild(el('td', {}, r.rank));
      tr.appendChild(el('td', {}, r.name));
      tr.appendChild(el('td', { class: 'num' }, r.points));
      body.appendChild(tr);
    });
    if (!rows || !rows.length) {
      const tr = el('tr');
      tr.appendChild(el('td', { colspan: '3' }, 'Hozircha jamoalar yo‘q'));
      body.appendChild(tr);
    }
    return table;
  }

  function questionBlock(q) {
    const box = el('div');
    if (q.media_url && /\.(png|jpe?g|gif|webp|svg)(\?|$)/i.test(q.media_url)) {
      box.appendChild(el('img', { class: 'room-media', src: q.media_url, alt: 'Savol rasmi' }));
    }
    box.appendChild(el('p', { class: 'room-question' }, q.text));
    if (q.options && q.options.length) {
      const list = el('ul', { class: 'room-options' });
      q.options.forEach(function (o) { list.appendChild(el('li', {}, (o.key ? o.key + ') ' : '') + (o.text || ''))); });
      box.appendChild(list);
    }
    return box;
  }

  /* Keyboard and presentation-clicker control for the host (works in the console and on the projector window):
     Space / → / PageDown / Enter = next step (start → reveal the answer → next question),
     P (or З on a Russian layout, "." / B from a clicker's blank-screen key) = pause / resume the clock.
     getState() returns the latest {status, paused}; onState(hostState) runs after each action. */
  function hostKeys(code, getState, onState) {
    let busy = false, last = 0;
    document.addEventListener('keydown', function (e) {
      const t = e.target;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
      const st = getState();
      if (!st) return;
      let path = null, body = null;
      if ([' ', 'ArrowRight', 'PageDown', 'Enter'].indexOf(e.key) !== -1) {
        if (t && /^(BUTTON|A)$/.test(t.tagName) && (e.key === ' ' || e.key === 'Enter')) return;   // let the focused button act
        if (st.status === 'lobby') path = '/start';
        else if (st.status === 'question') path = '/reveal';
        else if (st.status === 'reveal') path = '/next';
      } else if (['p', 'P', 'з', 'З', '.', 'b', 'B'].indexOf(e.key) !== -1 && st.status === 'question') {
        path = '/timer'; body = { action: st.paused ? 'resume' : 'pause' };
      }
      if (!path) return;
      e.preventDefault();
      const now = Date.now();
      if (busy || now - last < 700) return;      // a double click on a clicker must not skip a question
      busy = true; last = now;
      api('/api/rooms/' + code + path, { method: 'POST', body: body || {} })
        .then(function (s) { if (onState) onState(s); })
        .catch(function () {})
        .finally(function () { busy = false; });
    });
  }

  window.ZWRoom = { api: api, getToken: getToken, setToken: setToken, clearToken: clearToken, el: el,
                    clock: clock, Countdown: Countdown, poll: poll, standingsTable: standingsTable,
                    questionBlock: questionBlock, hostKeys: hostKeys };
})();
