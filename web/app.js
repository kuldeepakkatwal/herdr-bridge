/* Herdr web chat: plain ES2020, no framework, no build step. Talks to the bridge it is served from.
 *
 * Vendored: marked 15.0.12 (web/marked.min.js, from cdnjs), https://github.com/markedjs/marked
 *   MIT License. Copyright (c) 2018+, MarkedJS (https://github.com/markedjs/)
 *   Copyright (c) 2011-2018, Christopher Jeffrey (https://github.com/chjj/)
 *   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
 *   associated documentation files (the "Software"), to deal in the Software without restriction,
 *   including without limitation the rights to use, copy, modify, merge, publish, distribute,
 *   sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
 *   furnished to do so, subject to the following conditions: The above copyright notice and this
 *   permission notice shall be included in all copies or substantial portions of the Software.
 *   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
 *   NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 *   NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
 *   OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 *   CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 *
 * The bridge does all the merging (transcript + screen); this page only renders what it sends:
 *   state         {herdr, workspaces:[{id,label,path,agents:[{pane_id,title,status,menu,...}]}]}
 *   history_full  {pane_id, items, title}            replaces the list
 *   history       {pane_id, item, index, replace_last} append when index == length, else replace at index
 *   live          {pane_id, lines, working, menu, input}  what Claude is drawing right now
 */
'use strict';

const $ = (id) => document.getElementById(id);
const E = {
  app: $('app'), agents: $('agents'), scrim: $('scrim'), menuBtn: $('menuBtn'), title: $('title'), sub: $('sub'),
  pill: $('pill'), conn: $('conn'), list: $('list'), msgs: $('msgs'), pending: $('pending'), live: $('live'),
  liveLines: $('liveLines'), working: $('working'), workingText: $('workingText'), newPill: $('newPill'),
  menu: $('menu'), keys: $('keys'), composer: $('composer'), box: $('box'), send: $('send'), toast: $('toast'),
};

function load(k) { try { return localStorage.getItem(k); } catch { return null; } }
function save(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: not remembered */ } }

const S = {
  pane: load('herdr.pane'),
  state: null,
  items: [], nodes: [],   // nodes[i] renders items[i]
  live: null,             // last `live` event; null until the bridge sends one
  es: null, retry: 1000, retryTimer: 0,
  fresh: true,            // next history_full jumps to the bottom
  stuck: true,
  menuSig: '', keySig: '', liveSig: '',
};

function h(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
const escHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/* ---------- markdown ---------- */

const md = new marked.Marked({
  gfm: true,
  breaks: true,   // Claude writes for a terminal, where a single newline is a new line
  renderer: {
    html(t) { return t.block ? '<p>' + escHtml(t.text) + '</p>' : escHtml(t.text); },   // raw HTML is shown, never run
    link(t) {
      if (/^\s*(javascript|vbscript|data):/i.test(t.href || '')) return this.parser.parseInline(t.tokens);
      return marked.Renderer.prototype.link.call(this, t).replace(/^<a /, '<a target="_blank" rel="noopener noreferrer" ');
    },
    table(t) { return '<div class="scrollx">' + marked.Renderer.prototype.table.call(this, t) + '</div>'; },
  },
});

function mdHtml(text) {
  try { return md.parse(text); } catch { return '<p>' + escHtml(text) + '</p>'; }
}

/* ---------- ANSI SGR -> spans ---------- */

const BASE16 = [[0, 0, 0], [205, 49, 49], [13, 188, 121], [229, 229, 16], [36, 114, 200], [188, 63, 188], [17, 168, 205], [229, 229, 229],
  [102, 102, 102], [241, 76, 76], [35, 209, 139], [245, 245, 67], [59, 142, 234], [214, 112, 214], [41, 184, 219], [255, 255, 255]];
const CUBE = [0, 95, 135, 175, 215, 255];
function c256(n) {
  if (n < 16) return BASE16[n];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return [v, v, v]; }
  n -= 16;
  return [CUBE[Math.floor(n / 36)], CUBE[Math.floor(n / 6) % 6], CUBE[n % 6]];
}
const darkMQ = matchMedia('(prefers-color-scheme: dark)');
const lum = (c) => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;

function sgr(params, st) {
  st = { ...st };
  const p = params === '' ? [0] : params.split(/[;:]/).map((x) => (x === '' ? 0 : Number(x)));
  for (let i = 0; i < p.length; i++) {
    const c = p[i];
    if (c === 0) st = {};
    else if (c === 1) st.bold = true;
    else if (c === 2) st.dim = true;
    else if (c === 3) st.italic = true;
    else if (c === 4) st.under = true;
    else if (c === 7) st.inv = true;
    else if (c === 22) { st.bold = false; st.dim = false; }
    else if (c === 23) st.italic = false;
    else if (c === 24) st.under = false;
    else if (c === 27) st.inv = false;
    else if (c >= 30 && c <= 37) st.fg = BASE16[c - 30];
    else if (c >= 90 && c <= 97) st.fg = BASE16[c - 82];
    else if (c >= 40 && c <= 47) st.bg = BASE16[c - 40];
    else if (c >= 100 && c <= 107) st.bg = BASE16[c - 92];
    else if (c === 39) st.fg = null;
    else if (c === 49) st.bg = null;
    else if (c === 38 || c === 48) {
      let col = null;
      if (p[i + 1] === 5) { col = c256(p[i + 2] | 0); i += 2; }
      else if (p[i + 1] === 2) { col = [p[i + 2] | 0, p[i + 3] | 0, p[i + 4] | 0]; i += 4; }
      if (col) st[c === 38 ? 'fg' : 'bg'] = col;
    }
  }
  return st;
}

function span(text, st) {
  if (!text) return '';
  let fg = st.fg, bg = st.bg;
  if (st.inv) [fg, bg] = [bg || null, fg || null];
  // Claude's theme was picked for the Mac terminal: a text colour too close to this page's background is dropped
  if (fg && !bg && (darkMQ.matches ? lum(fg) < 0.22 : lum(fg) > 0.85)) fg = null;
  const css = [];
  if (fg) css.push(`color:rgb(${fg})`);
  else if (st.inv) css.push('color:var(--bg)');
  if (bg) css.push(`background:rgb(${bg})`);
  else if (st.inv) css.push('background:var(--fg)');
  if (st.bold) css.push('font-weight:700');
  if (st.dim) css.push('opacity:.6');
  if (st.italic) css.push('font-style:italic');
  if (st.under) css.push('text-decoration:underline');
  const t = escHtml(text);
  return css.length ? `<span style="${css.join(';')}">${t}</span>` : t;
}

const BOX_ONLY = /^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║╔╗╚╝]*[─━═][\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║╔╗╚╝]*$/;
const ESC_RE = /\x1b\[([0-9;:?<=>]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[^[\]]?/g;
function ansiHtml(line) {
  line = line.replace(/\r/g, '');
  let st = {}, out = '', last = 0, m;
  ESC_RE.lastIndex = 0;
  while ((m = ESC_RE.exec(line))) {
    out += span(line.slice(last, m.index), st);
    last = ESC_RE.lastIndex;
    if (m[2] === 'm') st = sgr(m[1], st);
  }
  return out + span(line.slice(last), st);
}

/* ---------- helpers: agent, scroll, toast ---------- */

function agent() {
  for (const w of (S.state && S.state.workspaces) || []) {
    for (const a of w.agents) if (a.pane_id === S.pane) return { ...a, workspace: w.label };
  }
  return null;
}

const atBottom = () => E.list.scrollHeight - E.list.scrollTop - E.list.clientHeight < 48;
function toBottom() { E.list.scrollTop = E.list.scrollHeight; E.newPill.hidden = true; }
// `stuck`: the reader is at the bottom. Kept from scroll events, so when the list area shrinks (menu shown,
// keyboard up) or content grows (image loaded) the view stays pinned to the newest message.
E.list.addEventListener('scroll', () => { S.stuck = atBottom(); if (S.stuck) E.newPill.hidden = true; }, { passive: true });
new ResizeObserver(() => { if (S.stuck) toBottom(); }).observe(E.list);
new ResizeObserver(() => { if (S.stuck) toBottom(); }).observe(E.list.firstElementChild);
E.newPill.addEventListener('click', () => E.list.scrollTo({ top: E.list.scrollHeight, behavior: 'smooth' }));

let toastTimer = 0;
function toast(msg) {
  E.toast.textContent = msg;
  E.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { E.toast.hidden = true; }, 3500);
}

async function post(action, body) {
  const r = await fetch(`api/agents/${encodeURIComponent(S.pane)}/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = 'error ' + r.status;
    try { msg = (await r.json()).error.message || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
}

function sendKeys(keys) {
  if (!S.pane) return;
  post('keys', { keys }).catch((e) => toast('Key not sent: ' + e.message));
}

/* ---------- messages ---------- */

function hhmm(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

// state: undefined/'sent' = plain, 'queued' = Claude has not taken it yet, 'dropped' = removed before Claude took it
const USER_LABEL = { queued: 'Queued', dropped: 'Not sent' };
function userNode(text, ts, state, sending) {
  const n = h('div', 'msg user' + (USER_LABEL[state] ? ' ' + state : '') + (sending ? ' sending' : ''));
  n.append(h('div', 'bubble', text));
  const meta = h('div', 'meta', sending ? 'Sending…' : hhmm(ts));
  if (USER_LABEL[state]) meta.append(h('b', null, USER_LABEL[state]));
  if (meta.textContent) n.append(meta);
  return n;
}

function itemNode(it, old) {
  const text = it.text || '';
  if (it.role === 'user') return userNode(text, it.ts, it.state, false);
  if (it.role === 'assistant') {
    const n = h('div', 'msg md');
    n.innerHTML = mdHtml(text);
    return n;
  }
  if (it.role === 'tool') {
    const lines = text.split('\n').filter(Boolean);
    if (lines.length <= 1) return h('div', 'msg tool one', text);
    const n = h('details', 'msg tool');
    n.open = !!(old && old.open);   // a tool row that grows keeps its open/closed state
    n.append(h('summary', null, `${lines.length} steps`), h('div', 'lines', lines.join('\n')));
    return n;
  }
  return h('div', 'msg note', text);
}

function renderAll() {
  const stick = S.fresh || atBottom();
  const prevTop = E.list.scrollTop;
  S.fresh = false;
  S.nodes = S.items.map((it) => itemNode(it));
  E.msgs.replaceChildren(...S.nodes);
  if (!S.pane) E.msgs.replaceChildren(h('p', 'empty', 'Pick an agent from the list.'));
  else if (!S.items.length) E.msgs.replaceChildren(h('p', 'empty', 'No messages yet.'));
  if (stick) toBottom(); else E.list.scrollTop = prevTop;
}

function applyItem(d) {
  const it = d.item;
  if (!it) return;
  let i = typeof d.index === 'number' ? d.index : (d.replace_last ? S.items.length - 1 : S.items.length);
  if (i < 0) i = 0;
  if (i > S.items.length) { connect(); return; }   // missed an event: reopening the stream sends history_full
  const stick = atBottom();
  if (i === S.items.length) {
    if (!S.items.length) E.msgs.replaceChildren();  // drop the "No messages yet" line
    const n = itemNode(it);
    S.items.push(it); S.nodes.push(n);
    E.msgs.append(n);
    if (it.role === 'user') dropPending();
    if (!stick) E.newPill.hidden = false;
  } else {
    const n = itemNode(it, S.nodes[i]);
    S.nodes[i].replaceWith(n);
    S.items[i] = it; S.nodes[i] = n;
  }
  if (stick) toBottom();
}

/* ---------- optimistic "Sending…" bubbles ---------- */

function dropPending() {
  const n = E.pending.firstElementChild;
  if (n) { clearTimeout(n._t); n.remove(); }
}

function send() {
  const text = E.box.value.trim();
  if (!text || !S.pane) return;
  E.box.value = '';
  grow();
  const n = userNode(text, null, null, true);
  n._t = setTimeout(() => n.remove(), 3000);   // the real item is on its way (or the send failed: toast below)
  E.pending.append(n);
  toBottom();
  post('send', { text }).catch((e) => {
    clearTimeout(n._t); n.remove();
    toast('Not sent: ' + e.message);
    if (!E.box.value) { E.box.value = text; grow(); }   // give the text back so nothing is lost
  });
}

/* ---------- live block, menu, keys ---------- */

function renderLive() {
  const L = S.live, a = agent();
  const lines = (L && L.lines) || [];
  let working = L ? L.working : null;
  if (!L && a && a.status === 'working') working = '';   // a bridge without `live`: still say Claude works
  const sig = JSON.stringify([lines, working]);
  if (sig === S.liveSig) return;
  S.liveSig = sig;
  const stick = atBottom();
  E.live.hidden = !lines.length && working == null;
  E.liveLines.hidden = !lines.length;
  // one div per line; a line of only box drawing (Claude's ╭───╮ frames) is clipped at the edge, not wrapped
  E.liveLines.innerHTML = lines.map((l) => {
    const rule = BOX_ONLY.test(l.replace(ESC_RE, '')) ? ' class="clip"' : '';
    return `<div${rule}>${ansiHtml(l) || ' '}</div>`;
  }).join('');
  E.working.hidden = working == null;
  E.workingText.textContent = working ? '\u00a0· ' + working : '';   // nbsp: a leading space collapses in a flex item
  if (stick) toBottom();
}

const KEYS = [['esc', 'esc'], ['^C', 'ctrl+c'], ['tab', 'tab'], ['⇧tab', 'shift+tab'], ['↑', 'up'], ['↓', 'down'], ['⏎', 'enter']];
const MENU_KEYS = [['↑', 'up'], ['↓', 'down'], ['select', 'enter', 'primary'], ['esc', 'esc']];

function renderBottom() {
  const a = agent();
  const blocked = !!a && a.status === 'blocked';
  const menu = blocked ? ((S.live && S.live.menu) || a.menu || []) : [];
  const mSig = JSON.stringify(menu);
  if (mSig !== S.menuSig) {
    S.menuSig = mSig;
    E.menu.hidden = !menu.length;
    E.menu.replaceChildren(...menu.map((o) => {
      const b = h('button', 'opt');
      b.type = 'button';
      const text = h('span', 'opt-text');
      text.append(h('span', null, o.label));
      if (o.detail) text.append(h('span', 'detail', o.detail));   // Claude's description under the option
      b.append(h('b', null, String(o.n)), text);
      b.onclick = () => sendKeys([String(o.n)]);
      return b;
    }));
  }
  const kSig = String(blocked) + !!S.pane;
  if (kSig !== S.keySig) {
    S.keySig = kSig;
    E.keys.replaceChildren(...(blocked ? MENU_KEYS : KEYS).map(([label, key, cls]) => {
      const b = h('button', 'key' + (cls ? ' ' + cls : ''), label);
      b.type = 'button';
      b.disabled = !S.pane;
      b.setAttribute('aria-label', key);
      b.onclick = () => sendKeys([key]);
      return b;
    }));
  }
  E.send.disabled = !S.pane;
}

// Tapping a key must not take focus (and the phone keyboard) away from the message box.
for (const el of [E.keys, E.menu]) el.addEventListener('mousedown', (e) => e.preventDefault());

/* ---------- top bar and drawer ---------- */

const PILL = { idle: 'idle', working: 'working', blocked: 'needs you', done: 'done' };

function renderTop() {
  const a = agent();
  E.title.textContent = a ? a.title : (S.pane ? S.pane : 'Herdr');
  E.sub.textContent = a ? a.workspace + (a.agent ? '' : ' · shell') : '';
  const word = a && PILL[a.status];
  E.pill.hidden = !word;
  E.pill.textContent = word || '';
  E.pill.className = 'pill ' + (a ? a.status : '');
  document.title = a ? (a.status === 'blocked' ? '● ' : '') + a.title + ' · Herdr' : 'Herdr';
}

function setConn(text) {
  E.conn.hidden = !text;
  E.conn.textContent = text || '';
}

function renderDrawer() {
  const ws = (S.state && S.state.workspaces) || [];
  E.agents.replaceChildren(...ws.map((w) => {
    const sec = h('section', 'ws');
    sec.append(h('div', 'ws-head', w.label));
    if (w.path) sec.append(h('div', 'ws-path', w.path));
    // "needs you" first inside the workspace; otherwise herdr's order (sort is stable)
    const ags = [...w.agents].sort((x, y) => (y.status === 'blocked') - (x.status === 'blocked'));
    for (const a of ags) {
      const b = h('button', 'agent' + (a.pane_id === S.pane ? ' on' : ''));
      b.type = 'button';
      b.append(h('span', 'dot ' + a.status), h('span', 'name', a.title));
      if (a.status === 'blocked') b.append(h('span', 'why blocked', 'needs you'));
      else if (!a.agent) b.append(h('span', 'why', 'shell'));
      b.onclick = () => pick(a.pane_id);
      sec.append(b);
    }
    return sec;
  }));
}

const drawer = (open) => E.app.classList.toggle('drawer-open', open);
E.menuBtn.onclick = () => drawer(true);
E.scrim.onclick = () => drawer(false);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') drawer(false); });

function pick(pane) {
  drawer(false);
  if (pane === S.pane) return;
  S.pane = pane;
  save('herdr.pane', pane);
  S.items = []; S.nodes = []; S.live = null; S.fresh = true;
  S.liveSig = S.menuSig = S.keySig = '';
  while (E.pending.firstChild) dropPending();
  renderAll(); renderTop(); renderDrawer(); renderLive(); renderBottom();
  connect();
}

/* ---------- the event stream ---------- */

function connect() {
  clearTimeout(S.retryTimer);
  if (S.es) S.es.close();
  const es = new EventSource('api/events?v=2' + (S.pane ? '&watch=' + encodeURIComponent(S.pane) : ''));
  S.es = es;
  const mine = (d) => !d.pane_id || d.pane_id === S.pane;
  const on = (name, fn) => es.addEventListener(name, (e) => {
    if (es !== S.es) return;
    let d;
    try { d = JSON.parse(e.data); } catch { return; }
    fn(d);
  });
  es.onopen = () => { if (es === S.es) { S.retry = 1000; setConn(''); } };
  es.onerror = () => {
    if (es !== S.es) return;
    es.close();
    setConn('Offline');
    S.retryTimer = setTimeout(connect, S.retry);   // 1, 2, 4, 8, 15, 15 … seconds
    S.retry = Math.min(S.retry * 2, 15000);
  };
  on('state', (d) => {
    S.state = d;
    setConn(d.herdr === 'offline' ? 'herdr offline' : '');
    renderTop(); renderDrawer(); renderLive(); renderBottom();
  });
  on('history_full', (d) => { if (mine(d)) { S.items = d.items || []; renderAll(); } });
  on('history', (d) => { if (mine(d)) applyItem(d); });
  on('live', (d) => { if (mine(d)) { S.live = d; renderLive(); renderBottom(); } });
}

// iOS drops the stream while the page is in the background: reconnect at once when it comes back.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && (!S.es || S.es.readyState === EventSource.CLOSED)) {
    S.retry = 1000;
    connect();
  }
});

/* ---------- message box ---------- */

function grow() {
  E.box.style.height = 'auto';
  E.box.style.height = Math.min(E.box.scrollHeight, 154) + 'px';   // 6 lines of 22 px + padding, then it scrolls
}
E.box.addEventListener('input', grow);
// Phones (Messages, WhatsApp): Enter is a new line and only Send sends. Desktop: Enter sends, Shift+Enter is a new line.
const TOUCH = matchMedia('(pointer: coarse)').matches || 'ontouchstart' in window;
E.box.enterKeyHint = TOUCH ? 'enter' : 'send';
E.box.addEventListener('keydown', (e) => {
  if (!TOUCH && e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
E.composer.addEventListener('submit', (e) => { e.preventDefault(); send(); });
E.send.addEventListener('mousedown', (e) => e.preventDefault());   // keep the keyboard up after Send

// The phone keyboard shrinks the visible area, not 100dvh: size the app to what is visible.
const vv = window.visualViewport;
if (vv) {
  const fit = () => {
    if (vv.scale > 1.01) return;   // pinch zoom: leave the layout alone
    document.documentElement.style.setProperty('--app-h', vv.height + 'px');
    if (window.scrollY) window.scrollTo(0, 0);
  };
  vv.addEventListener('resize', fit);
  fit();
}

/* ---------- start ---------- */

renderAll(); renderTop(); renderBottom();
if (!S.pane && innerWidth < 900) drawer(true);
connect();
