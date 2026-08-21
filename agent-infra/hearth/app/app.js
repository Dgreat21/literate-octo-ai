/* Hearth — the harness at home, held at arm's length.
 *
 * One rule governs this file (INFRA-7 §3): the app never lies about the link.
 * Connection state is a field of the model — live / stale / unreachable — not a
 * string on the first screen. Nothing renders as current unless the model says
 * it is, and no command is ever assumed to have landed.
 */
'use strict';

// ── freshness thresholds ─────────────────────────────────────
const POLL_MS   = 2500;
const LIVE_S    = 6;    // younger than this: trust it
const STALE_S   = 25;   // older than this: stop calling it a connection
const HOLD_MS   = 700;  // 1c-ii: stop is expensive and rare, friction is earned
const SETTLE_MS = 15000;

const $  = (s, r = document) => r.querySelector(s);
const el = (t, cls, txt) => { const n = document.createElement(t); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
const pad = (n) => String(n).padStart(2, '0');
const clock = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

// The terminal title is whatever the shell last wrote there — often a 200-char
// command line. Useful, but not a headline. The project the pane sits in is the
// honest short answer to "what is this"; the raw title stays visible below it.
const project = (a) => ((a.cwd_short || a.cwd || '').split('/').filter(Boolean).pop() || a.pane_id || '—');
const rawTitle = (a) => (a.title || '').trim() || a.agent;

// ── model ────────────────────────────────────────────────────
const S = {
  screen: 'home',
  selected: null,          // pane_id shown on the log screen
  agents: [],
  summary: {},
  host: null,
  link: { state: 'boot', asOf: null, lastOkAt: null, reason: null },
  tail: { pane: null, lines: [], asOf: null },
  autoscroll: true,
  stop: null,              // { pane, at, status_before, outcome }
  token: localStorage.getItem('hearth.token') || '',
};

function linkState() {
  if (S.link.lastOkAt == null) return S.link.state === 'boot' ? 'boot' : 'unreachable';
  const age = (Date.now() - S.link.lastOkAt) / 1000;
  if (S.link.state === 'unreachable') return 'unreachable';
  if (age < LIVE_S) return 'live';
  if (age < STALE_S) return 'stale';
  return 'unreachable';
}
const isLive = () => linkState() === 'live';
const ageSec = () => (S.link.lastOkAt == null ? null : Math.round((Date.now() - S.link.lastOkAt) / 1000));

// ── transport ────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  if (S.token) headers['X-Hearth-Token'] = S.token;
  const res = await fetch(path, Object.assign({ cache: 'no-store' }, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON is a fault either way */ }
  if (!res.ok) {
    const e = new Error((body && body.error) || `http_${res.status}`);
    e.status = res.status; e.body = body;
    throw e;
  }
  return body;
}

function markOk(payload) {
  S.link = { state: 'ok', asOf: payload.as_of_iso || null, lastOkAt: Date.now(), reason: null };
}
function markDown(err) {
  // Distinguish "the mac is not there" from "the mac is there, herdr is not".
  let reason = 'no answer from the mac';
  if (err.status === 401) reason = 'token rejected — open the link from the mac again';
  else if (err.status === 503) reason = 'the mac answers, herdr does not';
  else if (err.status) reason = `hearthd said ${err.status}`;
  S.link.state = 'unreachable';
  S.link.reason = reason;
}

async function pollState() {
  try {
    const d = await api('/api/state');
    S.agents = d.agents || [];
    S.summary = d.summary || {};
    S.host = d.host;
    markOk(d);
    if (!S.selected || !S.agents.some((a) => a.pane_id === S.selected)) {
      const busy = S.agents.find((a) => a.status === 'working') || S.agents.find((a) => a.status === 'blocked');
      S.selected = (busy || S.agents[0] || {}).pane_id || null;
    }
    settleStop();
  } catch (err) {
    markDown(err);
  }
  render();
}

// Home wants "what is it saying" too — the last few lines of whatever is working.
function tailTarget() {
  if (S.screen === 'detail') return { pane: S.selected, lines: 90 };
  if (S.screen === 'home') {
    const busy = S.agents.find((a) => a.status === 'working');
    return busy ? { pane: busy.pane_id, lines: 8 } : { pane: null, lines: 0 };
  }
  return { pane: null, lines: 0 };
}

async function pollTail() {
  const want = tailTarget();
  if (!want.pane) return;
  try {
    const d = await api(`/api/agent/${encodeURIComponent(want.pane)}?lines=${want.lines}`);
    S.tail = { pane: want.pane, lines: d.tail || [], asOf: d.as_of_iso };
    markOk(d);
  } catch (err) {
    markDown(err);   // the log freezes; it must never keep scrolling on its own
  }
  render();
}

// A stop is not done because we asked. It is done when herdr says the pane moved.
function settleStop() {
  if (!S.stop || S.stop.outcome) return;
  const a = S.agents.find((x) => x.pane_id === S.stop.pane);
  if (a && a.status !== 'working') S.stop.outcome = 'stopped';
  else if (Date.now() - S.stop.at > SETTLE_MS) S.stop.outcome = 'no-change';
}

async function sendStop(pane) {
  S.stop = { pane, at: Date.now(), outcome: null };
  render();
  try {
    const d = await api(`/api/agent/${encodeURIComponent(pane)}/stop`, { method: 'POST' });
    markOk(d);
    if (d.settled) S.stop.outcome = 'stopped';
  } catch (err) {
    markDown(err);
    S.stop.outcome = 'not-sent';   // the honest one: we do not know it landed
  }
  render();
  pollState();
}

// ── hold-to-stop ─────────────────────────────────────────────
function wireHold(btn, onDone) {
  const fill = $('.fill', btn);
  const lbl = $('.lbl', btn);
  const base = lbl.textContent;
  let timer = null;
  const reset = (ms) => { fill.style.transition = `width ${ms}ms linear`; fill.style.width = '0%'; lbl.textContent = base; };
  const start = (e) => {
    if (btn.disabled) return;
    e.preventDefault();
    clearTimeout(timer);
    fill.style.transition = 'none'; fill.style.width = '0%';
    requestAnimationFrame(() => { fill.style.transition = `width ${HOLD_MS}ms linear`; fill.style.width = '100%'; });
    lbl.textContent = 'hold…';
    timer = setTimeout(() => { reset(0); onDone(); }, HOLD_MS + 20);
  };
  const end = () => { clearTimeout(timer); reset(160); };
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', end);
  btn.addEventListener('pointerleave', end);
  btn.addEventListener('pointercancel', end);
}

// ── render ───────────────────────────────────────────────────
function renderLink() {
  const st = linkState();
  const bar = $('#linkbar');
  bar.dataset.state = st;
  const age = ageSec();
  const host = S.host || 'hearth';
  const text = {
    boot: ['connecting…', ''],
    live: [host, age === 0 ? 'just now' : `${age}s ago`],
    stale: [`${host} · stale`, `last answer ${age}s ago`],
    unreachable: ['hearth unreachable', S.link.reason || (age != null ? `last answer ${age}s ago` : '')],
  }[st];
  $('.link-host', bar).textContent = text[0];
  $('.link-age', bar).textContent = text[1];
}

function heroCard() {
  const busy = S.agents.find((a) => a.status === 'working');
  const wrap = $('#hero');
  wrap.textContent = '';

  if (!busy) {
    const c = el('div', 'card quiet');
    const stale = !isLive() && ageSec() != null;
    c.append(el('div', null, S.agents.length ? 'Nothing running.' : 'No panes.'));
    c.append(el('div', 'rmeta', stale
      ? `as of ${ageSec()}s ago — not necessarily true now`
      : (S.agents.length ? `${S.agents.length} pane(s) idle` : 'herdr has no agents')));
    wrap.append(c);
    return;
  }

  const c = el('div', 'card ink');
  const top = el('div', 'hero-top');
  top.append(el('span', 'breath' + (isLive() ? '' : ' still')));
  top.append(el('span', null, isLive() ? 'working' : linkState()));
  top.append(el('span', 'when', S.link.asOf ? S.link.asOf.slice(11) : '—'));
  c.append(top);

  c.append(el('h3', 'hero-h', project(busy)));
  c.append(el('div', 'hero-meta', `${busy.agent} · ${busy.pane_id} · ${busy.cwd_short || busy.cwd}`));
  c.append(el('div', 'hero-cmd', rawTitle(busy)));

  const t = el('pre', 'tail short');
  const last = S.tail.pane === busy.pane_id ? S.tail.lines.filter((l) => l.trim()).slice(-3) : [];
  t.textContent = last.length ? last.join('\n') : 'waiting for the first line…';
  c.append(t);

  c.append(stopButton(busy));
  wrap.append(c);
}

function stopButton(agent) {
  const b = el('button', 'hold');
  b.dataset.needsLink = '1';
  b.append(el('span', 'fill'));
  const lbl = el('span', 'lbl', 'Hold to stop');
  b.append(lbl);

  const s = S.stop && S.stop.pane === agent.pane_id ? S.stop : null;
  if (s) {
    b.classList.add('sent');
    lbl.textContent = {
      null: 'sent · waiting for herdr',
      'stopped': 'stopped',
      'no-change': 'sent, still working — check the mac',
      'not-sent': 'not sent — link failed',
    }[s.outcome];
    if (s.outcome === null || s.outcome === 'stopped') b.disabled = true;
  }
  if (!isLive()) {
    b.disabled = true;
    if (!s) lbl.textContent = 'Hold to stop · no link';
  }
  if (!b.disabled) wireHold(b, () => sendStop(agent.pane_id));
  return b;
}

function renderHome() {
  const n = S.summary || {};
  const known = S.host
    ? `${S.host} · ${S.agents.length} pane(s) · ${n.working || 0} working, ${n.blocked || 0} waiting on you`
    : 'no answer yet';
  // Every number carries its age once the link is not live (INFRA-7 §3.3).
  $('#home-sub').textContent = isLive() || ageSec() == null ? known : `${known} · as of ${ageSec()}s ago`;
  heroCard();

  const att = $('#attention');
  att.textContent = '';
  const blocked = S.agents.filter((a) => a.status === 'blocked');
  if (blocked.length) {
    // §4: no approvals screen in v0 — a line that points at the pane, nothing more.
    const w = el('div', 'warn');
    w.append(el('b', null, `${blocked.length} agent${blocked.length > 1 ? 's need' : ' needs'} a decision`));
    w.append(el('div', 'rmeta', blocked.map((a) => a.pane_id).join(', ') + ' — answer it on the mac'));
    att.append(w);
  }
  if (linkState() === 'unreachable') {
    att.append(el('div', 'note', 'Everything above is the last thing the mac said, not what it is doing now.'));
  }
}

function renderRuns() {
  const list = $('#runs-list');
  list.textContent = '';
  $('#runs-sub').textContent = S.host ? `${S.agents.length} pane(s) on ${S.host}` : 'no answer yet';
  if (!S.agents.length) { list.append(el('div', 'card quiet', 'Nothing to show.')); return; }
  S.agents.forEach((a) => {
    const r = el('button', 'row');
    const m = el('div', 'rmain');
    m.append(el('div', 'rtitle', `${project(a)} · ${a.agent}`));
    m.append(el('div', 'rmeta', `${a.pane_id} · ${rawTitle(a)}`));
    r.append(m);
    r.append(el('span', 'tag ' + a.status, a.label || a.status));
    r.onclick = () => { S.selected = a.pane_id; go('detail'); pollTail(); };
    list.append(r);
  });
}

function renderDetail() {
  const a = S.agents.find((x) => x.pane_id === S.selected);
  const body = $('#detail-body');
  body.textContent = '';
  $('#detail-title').textContent = a ? project(a) : 'No pane';
  $('#detail-sub').textContent = a ? `${a.agent} · ${a.pane_id} · ${a.cwd_short || a.cwd}` : 'pick one on the panes screen';
  if (!a) return;

  const c = el('div', 'card ink');
  const top = el('div', 'hero-top');
  top.append(el('span', 'breath' + (a.status === 'working' && isLive() ? '' : ' still')));
  top.append(el('span', null, `raw tail · ${a.label || a.status}`));
  top.append(el('span', 'when', S.tail.asOf ? S.tail.asOf.slice(11) : '—'));
  c.append(top);

  const pre = el('pre', 'tail tall');
  pre.textContent = (S.tail.pane === a.pane_id ? S.tail.lines : []).join('\n');
  if (!isLive()) {
    // The stream stops with a mark. A log that keeps moving while the link is
    // down is the most dangerous lie this app could tell.
    pre.append(el('span', 'frozen', `— stream broken at ${clock(new Date(S.link.lastOkAt || Date.now()))} —`));
  }
  c.append(pre);
  if (a.status === 'working') c.append(stopButton(a));
  body.append(c);

  body.append(el('div', 'note',
    'Raw tail, not a digest. A step digest (1b-iii) needs an event stream from hearthd — it is not wired yet, so this shows what the terminal shows.'));

  if (S.autoscroll) requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
}

function renderSettings() {
  const b = $('#settings-body');
  b.textContent = '';

  const kv = (k, v) => { const r = el('div', 'kv'); r.append(el('b', null, k)); r.append(el('span', null, v)); b.append(r); };
  kv('mac', S.host || '—');
  kv('link', linkState());
  kv('as of', S.link.asOf || '—');
  kv('age', ageSec() == null ? '—' : ageSec() + 's');
  kv('served from', location.origin);
  kv('token', S.token ? S.token.slice(0, 4) + '…' : 'none');
  kv('poll', POLL_MS / 1000 + 's');
  kv('live / stale', `< ${LIVE_S}s / < ${STALE_S}s`);

  b.append(el('div', 'note',
    'Thresholds are the model, not decoration: past ' + LIVE_S + 's every control here goes dead on purpose. ' +
    'Outside the home wifi this needs Tailscale (INFRA-5) — there is no other way in, by design.'));

  const forget = el('button', 'btn ghost', 'Forget token');
  forget.onclick = () => { localStorage.removeItem('hearth.token'); S.token = ''; render(); };
  b.append(forget);
}

function render() {
  renderLink();
  if (S.screen === 'home') renderHome();
  else if (S.screen === 'runs') renderRuns();
  else if (S.screen === 'detail') renderDetail();
  else if (S.screen === 'settings') renderSettings();
}

function go(screen) {
  S.screen = screen;
  document.querySelectorAll('.screen').forEach((s) => { s.hidden = s.dataset.screen !== screen; });
  document.querySelectorAll('.tabbar button').forEach((t) => t.classList.toggle('on', t.dataset.go === screen));
  render();
  if (screen === 'detail') pollTail();
}

// ── boot ─────────────────────────────────────────────────────
(function boot() {
  const m = /[#&]t=([^&]+)/.exec(location.hash);
  if (m) {
    S.token = decodeURIComponent(m[1]);
    localStorage.setItem('hearth.token', S.token);
    history.replaceState(null, '', location.pathname + location.search);
  }

  document.body.addEventListener('click', (e) => {
    const t = e.target.closest('[data-go]');
    if (t) go(t.dataset.go);
  });

  go('home');
  pollState();
  setInterval(() => { if (!document.hidden) pollState(); }, POLL_MS);
  setInterval(() => { if (!document.hidden) pollTail(); }, POLL_MS - 500);
  // The age must tick even when nothing arrives — and when the link *changes*
  // state, the whole screen has to follow it (controls die, the log freezes).
  let lastLink = null;
  setInterval(() => {
    const now = linkState();
    if (now !== lastLink) { lastLink = now; render(); } else { renderLink(); }
  }, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { pollState(); pollTail(); } });

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
})();
