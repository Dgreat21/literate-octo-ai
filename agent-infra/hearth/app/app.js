/* Hearth — the harness at home, held at arm's length.
 *
 * Two rules govern this file.
 *
 * 1. The app never lies about the link (INFRA-7 §3). Connection state is a
 *    field of the model — live / stale / unreachable — not a string on the
 *    first screen. Nothing renders as current unless the model says it is,
 *    and no command is ever assumed to have landed.
 *
 * 2. The herd has two halves and the app shows both. herdr owns panes; dsh
 *    owns sessions. A pane running `dsh web` reads as idle while the session
 *    inside it works for half an hour — sourcing from herdr alone made the
 *    phone disagree with the GUI on screen. Either source may be down on its
 *    own, and the app says which.
 */
'use strict';

const POLL_MS   = 2500;
const LIVE_S    = 6;
const STALE_S   = 25;
const HOLD_MS   = 700;   // 1c-ii: stop is expensive and rare, friction is earned
const SETTLE_MS = 15000;

const $  = (s, r = document) => r.querySelector(s);
const el = (t, cls, txt) => { const n = document.createElement(t); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
const pad = (n) => String(n).padStart(2, '0');
const clock = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const project = (a) => ((a.cwd_short || a.cwd || '').split('/').filter(Boolean).pop() || '—');
const rawTitle = (a) => (a.title || '').trim() || a.agent || '—';
const isSession = (x) => x && x.kind === 'session';
const keyOf = (x) => (isSession(x) ? x.id : x.pane_id);

const S = {
  screen: 'home',
  focus: null,             // { kind: 'session'|'pane', id }
  agents: [], sessions: [], running: [], sources: {}, summary: {},
  host: null,
  link: { state: 'boot', asOf: null, lastOkAt: null, reason: null },
  tail: { pane: null, lines: [], asOf: null },
  view: { id: null, digest: [], raw_last: '', asOf: null },
  autoscroll: true,
  tracker: { nodes: [], edges: [], at: 0 },
  ticket: null,            // selected tracker key
  delegate: null,          // { key, at, outcome, session_id }
  stop: null,              // { key, at, outcome }
  token: localStorage.getItem('hearth.token') || '',
};

// ── focus ────────────────────────────────────────────────────
function defaultFocus() {
  const run = S.running[0];
  if (run) return { kind: 'session', id: run.id };
  const busy = S.agents.find((a) => a.status === 'working') || S.agents.find((a) => a.status === 'blocked');
  if (busy) return { kind: 'pane', id: busy.pane_id };
  if (S.sessions[0]) return { kind: 'session', id: S.sessions[0].id };
  return S.agents[0] ? { kind: 'pane', id: S.agents[0].pane_id } : null;
}
function stillThere(f) {
  return f.kind === 'session' ? S.sessions.some((x) => x.id === f.id)
                              : S.agents.some((a) => a.pane_id === f.id);
}
function focused() {
  if (!S.focus) return null;
  return S.focus.kind === 'session' ? (S.sessions.find((x) => x.id === S.focus.id) || null)
                                    : (S.agents.find((a) => a.pane_id === S.focus.id) || null);
}
// What is actually doing work right now, session first.
function hot() {
  return S.running[0] || S.agents.find((a) => a.status === 'working') || null;
}

// ── link ─────────────────────────────────────────────────────
function linkState() {
  if (S.link.lastOkAt == null) return S.link.state === 'boot' ? 'boot' : 'unreachable';
  if (S.link.state === 'unreachable') return 'unreachable';
  const age = (Date.now() - S.link.lastOkAt) / 1000;
  if (age < LIVE_S) return 'live';
  if (age < STALE_S) return 'stale';
  return 'unreachable';
}
const isLive = () => linkState() === 'live';
const ageSec = () => (S.link.lastOkAt == null ? null : Math.round((Date.now() - S.link.lastOkAt) / 1000));

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  if (S.token) headers['X-Hearth-Token'] = S.token;
  const res = await fetch(path, Object.assign({ cache: 'no-store' }, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) { const e = new Error((body && body.error) || `http_${res.status}`); e.status = res.status; e.body = body; throw e; }
  return body;
}
function markOk(p) { S.link = { state: 'ok', asOf: p.as_of_iso || null, lastOkAt: Date.now(), reason: null }; }
function markDown(err) {
  let reason = 'no answer from the mac';
  if (err.status === 401) reason = 'token rejected — open the link from the mac again';
  else if (err.status === 503) reason = 'the mac answers, neither herdr nor dsh does';
  else if (err.status) reason = `hearthd said ${err.status}`;
  S.link.state = 'unreachable';
  S.link.reason = reason;
}

// ── polling ──────────────────────────────────────────────────
async function pollState() {
  try {
    const d = await api('/api/state');
    S.agents = d.agents || []; S.sessions = d.sessions || []; S.running = d.running || [];
    S.sources = d.sources || {}; S.summary = d.summary || {}; S.host = d.host;
    markOk(d);
    if (!S.focus || !stillThere(S.focus)) S.focus = defaultFocus();
    settleStop();
  } catch (err) { markDown(err); }
  render();
}

function detailTarget() {
  if (S.screen === 'detail') return S.focus;
  if (S.screen === 'home') { const h = hot(); return h ? { kind: isSession(h) ? 'session' : 'pane', id: keyOf(h) } : null; }
  return null;
}

async function pollDetail() {
  const t = detailTarget();
  if (!t) return;
  try {
    if (t.kind === 'session') {
      const steps = S.screen === 'detail' ? 18 : 3;
      const d = await api(`/api/session/${encodeURIComponent(t.id)}?steps=${steps}`);
      S.view = { id: t.id, digest: d.digest || [], raw_last: d.raw_last || '', asOf: d.as_of_iso };
    } else {
      const lines = S.screen === 'detail' ? 90 : 8;
      const d = await api(`/api/agent/${encodeURIComponent(t.id)}?lines=${lines}`);
      S.tail = { pane: t.id, lines: d.tail || [], asOf: d.as_of_iso };
    }
    markOk({ as_of_iso: S.view.asOf || S.tail.asOf });
  } catch (err) { markDown(err); }
  render();
}

// ── stop ─────────────────────────────────────────────────────
// A stop is not done because we asked. It is done when the source says so.
function settleStop() {
  if (!S.stop || S.stop.outcome) return;
  const s = S.sessions.find((x) => x.id === S.stop.key);
  const a = S.agents.find((x) => x.pane_id === S.stop.key);
  if (s && !s.running) S.stop.outcome = 'stopped';
  else if (a && a.status !== 'working') S.stop.outcome = 'stopped';
  else if (Date.now() - S.stop.at > SETTLE_MS) S.stop.outcome = 'no-change';
}

async function sendStop(item) {
  const key = keyOf(item);
  S.stop = { key, at: Date.now(), outcome: null };
  render();
  try {
    const url = isSession(item)
      ? `/api/session/${encodeURIComponent(key)}/cancel`
      : `/api/agent/${encodeURIComponent(key)}/stop`;
    const d = await api(url, { method: 'POST' });
    markOk(d);
    if (d.settled) S.stop.outcome = 'stopped';
  } catch (err) {
    markDown(err);
    S.stop.outcome = 'not-sent';
  }
  render();
  pollState();
}

function wireHold(btn, onDone) {
  const fill = $('.fill', btn), lbl = $('.lbl', btn), base = lbl.textContent;
  let timer = null;
  const reset = (ms) => { fill.style.transition = `width ${ms}ms linear`; fill.style.width = '0%'; lbl.textContent = base; };
  btn.addEventListener('pointerdown', (e) => {
    if (btn.disabled) return;
    e.preventDefault();
    clearTimeout(timer);
    fill.style.transition = 'none'; fill.style.width = '0%';
    requestAnimationFrame(() => { fill.style.transition = `width ${HOLD_MS}ms linear`; fill.style.width = '100%'; });
    lbl.textContent = 'hold…';
    timer = setTimeout(() => { reset(0); onDone(); }, HOLD_MS + 20);
  });
  ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) =>
    btn.addEventListener(ev, () => { clearTimeout(timer); reset(160); }));
}

function stopButton(item) {
  const b = el('button', 'hold');
  b.append(el('span', 'fill'));
  const lbl = el('span', 'lbl', isSession(item) ? 'Hold to cancel' : 'Hold to stop');
  b.append(lbl);
  const s = S.stop && S.stop.key === keyOf(item) ? S.stop : null;
  if (s) {
    b.classList.add('sent');
    lbl.textContent = { 'null': 'sent · waiting for the mac', 'stopped': 'stopped',
      'no-change': 'sent, still running — check the mac', 'not-sent': 'not sent — link failed'
    }[String(s.outcome)];
    if (s.outcome === null || s.outcome === 'stopped') b.disabled = true;
  }
  if (!isLive()) { b.disabled = true; if (!s) lbl.textContent += ' · no link'; }
  if (!b.disabled) wireHold(b, () => sendStop(item));
  return b;
}

// ── render ───────────────────────────────────────────────────
function renderLink() {
  const st = linkState(), bar = $('#linkbar'), age = ageSec(), host = S.host || 'hearth';
  bar.dataset.state = st;
  const t = { boot: ['connecting…', ''],
    live: [host, age === 0 ? 'just now' : `${age}s ago`],
    stale: [`${host} · stale`, `last answer ${age}s ago`],
    unreachable: ['hearth unreachable', S.link.reason || (age != null ? `last answer ${age}s ago` : '')] }[st];
  $('.link-host', bar).textContent = t[0];
  $('.link-age', bar).textContent = t[1];
}

// One source down is not the same as the link being down: the app says which
// half of the herd it cannot see, instead of showing the other half as all.
function sourceWarnings(into) {
  const bad = Object.entries(S.sources).filter(([, v]) => v !== 'ok');
  bad.forEach(([name, why]) => {
    const w = el('div', 'warn');
    w.append(el('b', null, name === 'dsh' ? 'dsh is not answering — sessions are invisible'
                                          : 'herdr is not answering — panes are invisible'));
    w.append(el('div', 'rmeta', why));
    into.append(w);
  });
}

function metaLine(x) {
  if (isSession(x)) {
    const bits = [x.preset || 'session', x.cwd_short || x.cwd];
    if (x.steps != null) bits.push(`${x.steps} steps`);
    if (x.tok_s != null) bits.push(`${x.tok_s} tok/s`);
    if (x.ctx_pct != null) bits.push(`ctx ${x.ctx_pct}%`);
    return bits.join(' · ');
  }
  return `${x.agent} · ${x.pane_id} · ${x.cwd_short || x.cwd}`;
}

function heroCard() {
  const wrap = $('#hero');
  wrap.textContent = '';
  const h = hot();
  if (!h) {
    const c = el('div', 'card quiet');
    const stale = !isLive() && ageSec() != null;
    c.append(el('div', null, 'Nothing running.'));
    c.append(el('div', 'rmeta', stale ? `as of ${ageSec()}s ago — not necessarily true now`
      : `${S.sessions.length} session(s), ${S.agents.length} pane(s) idle`));
    wrap.append(c);
    return;
  }
  const c = el('div', 'card ink');
  const top = el('div', 'hero-top');
  top.append(el('span', 'breath' + (isLive() ? '' : ' still')));
  top.append(el('span', null, isSession(h) ? 'session · running' : 'pane · working'));
  top.append(el('span', 'when', S.link.asOf ? S.link.asOf.slice(11) : '—'));
  c.append(top);
  c.append(el('h3', 'hero-h', isSession(h) ? h.title : project(h)));
  c.append(el('div', 'hero-meta', metaLine(h)));
  if (!isSession(h)) c.append(el('div', 'hero-cmd', rawTitle(h)));

  const t = el('pre', 'tail short');
  if (isSession(h)) {
    const d = S.view.id === h.id ? S.view.digest.slice(-3) : [];
    t.textContent = d.length ? d.map((x) => `${x.step}· ${x.text || (x.tools.join(', ') || '…')}`).join('\n')
                             : 'reading the session…';
  } else {
    const last = S.tail.pane === h.pane_id ? S.tail.lines.filter((l) => l.trim()).slice(-3) : [];
    t.textContent = last.length ? last.join('\n') : 'waiting for the first line…';
  }
  c.append(t);
  c.append(stopButton(h));
  wrap.append(c);
}

function renderHome() {
  const known = S.host
    ? `${S.host} · ${S.running.length} running · ${S.sessions.length} session(s) · ${S.agents.length} pane(s)`
    : 'no answer yet';
  $('#home-sub').textContent = isLive() || ageSec() == null ? known : `${known} · as of ${ageSec()}s ago`;
  heroCard();

  const att = $('#attention');
  att.textContent = '';
  sourceWarnings(att);
  const blocked = S.agents.filter((a) => a.status === 'blocked');
  if (blocked.length) {
    const w = el('div', 'warn');
    w.append(el('b', null, `${blocked.length} pane${blocked.length > 1 ? 's look' : ' looks'} blocked`));
    w.append(el('div', 'rmeta', blocked.map((a) => a.pane_id).join(', ')
      + ' — herdr’s own detection, not a decision queue'));
    att.append(w);
  }
  if (linkState() === 'unreachable') {
    att.append(el('div', 'note', 'Everything above is the last thing the mac said, not what it is doing now.'));
  }
}

function row(x) {
  const r = el('button', 'row');
  const m = el('div', 'rmain');
  m.append(el('div', 'rtitle', isSession(x) ? x.title : `${project(x)} · ${x.agent}`));
  m.append(el('div', 'rmeta', isSession(x) ? metaLine(x) : `${x.pane_id} · ${rawTitle(x)}`));
  r.append(m);
  const state = isSession(x) ? (x.running ? 'working' : 'done') : x.status;
  const label = isSession(x) ? (x.running ? 'running' : 'idle') : (x.label || x.status);
  r.append(el('span', 'tag ' + state, label));
  r.onclick = () => { S.focus = { kind: isSession(x) ? 'session' : 'pane', id: keyOf(x) }; go('detail'); pollDetail(); };
  return r;
}

function renderRuns() {
  const list = $('#runs-list');
  list.textContent = '';
  $('#runs-sub').textContent = S.host ? `${S.sessions.length} session(s) and ${S.agents.length} pane(s) on ${S.host}` : 'no answer yet';
  sourceWarnings(list);

  const live = S.sessions.filter((x) => x.running);
  const recent = S.sessions.filter((x) => !x.running && !x.blank).slice(0, 8);
  if (live.length) { list.append(el('div', 'grouphead', 'running')); live.forEach((x) => list.append(row(x))); }
  if (S.agents.length) { list.append(el('div', 'grouphead', 'panes')); S.agents.forEach((x) => list.append(row(x))); }
  if (recent.length) { list.append(el('div', 'grouphead', 'recent sessions')); recent.forEach((x) => list.append(row(x))); }
  if (!list.children.length) list.append(el('div', 'card quiet', 'Nothing to show.'));
}

function renderDetail() {
  const x = focused();
  const body = $('#detail-body');
  body.textContent = '';
  $('#detail-title').textContent = x ? (isSession(x) ? x.title : project(x)) : 'Nothing selected';
  $('#detail-sub').textContent = x ? metaLine(x) : 'pick one on the herd screen';
  if (!x) return;

  const c = el('div', 'card ink');
  const top = el('div', 'hero-top');
  const busy = isSession(x) ? x.running : x.status === 'working';
  top.append(el('span', 'breath' + (busy && isLive() ? '' : ' still')));
  top.append(el('span', null, isSession(x) ? 'step digest' : `raw tail · ${x.label || x.status}`));
  top.append(el('span', 'when', (isSession(x) ? S.view.asOf : S.tail.asOf || '')?.slice(11) || '—'));
  c.append(top);

  if (isSession(x)) {
    const list = el('div', 'steps');
    const d = S.view.id === x.id ? S.view.digest : [];
    d.forEach((st) => {
      const it = el('div', 'step');
      it.append(el('span', 'sn', `${st.turn}.${st.step}`));
      const b = el('div', 'sb');
      if (st.text) b.append(el('div', 'stext', st.text));
      if (st.tools.length) b.append(el('div', 'stools', st.tools.join(' · ')));
      it.append(b);
      list.append(it);
    });
    if (!d.length) list.append(el('div', 'rmeta', 'no steps yet'));
    c.append(list);
    // 1b-iii: the digest can be wrong; this line is what the tool actually said.
    if (S.view.raw_last) {
      const raw = el('pre', 'tail rawline');
      raw.textContent = S.view.raw_last;
      c.append(raw);
    }
  } else {
    const pre = el('pre', 'tail tall');
    pre.textContent = (S.tail.pane === x.pane_id ? S.tail.lines : []).join('\n');
    if (!isLive()) pre.append(el('span', 'frozen', `— stream broken at ${clock(new Date(S.link.lastOkAt || Date.now()))} —`));
    c.append(pre);
    if (S.autoscroll) requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
  }
  if (busy) c.append(stopButton(x));
  body.append(c);

  body.append(el('div', 'note', isSession(x)
    ? 'Digest built from session events (step/start, assistant/message, tool/call) — not parsed from a log. The single line below it is raw tool output: ground truth if the digest reads wrong.'
    : 'Raw tail of the terminal. Panes have no event stream, so there is nothing to digest here.'));
}

function renderSettings() {
  const b = $('#settings-body');
  b.textContent = '';
  const kv = (k, v) => { const r = el('div', 'kv'); r.append(el('b', null, k)); r.append(el('span', null, v)); b.append(r); };
  kv('mac', S.host || '—');
  kv('link', linkState());
  kv('herdr', S.sources.herdr || '—');
  kv('dsh', S.sources.dsh || '—');
  kv('as of', S.link.asOf || '—');
  kv('age', ageSec() == null ? '—' : ageSec() + 's');
  kv('served from', location.origin);
  kv('token', S.token ? S.token.slice(0, 4) + '…' : 'none');
  kv('poll', POLL_MS / 1000 + 's');
  kv('live / stale', `< ${LIVE_S}s / < ${STALE_S}s`);

  b.append(el('div', 'note',
    'Two sources, listed separately on purpose: herdr owns panes, dsh owns sessions, and either can be down alone. '
    + 'Thresholds are the model, not decoration — past ' + LIVE_S + 's every control goes dead. '
    + 'Outside the home wifi this needs Tailscale (INFRA-5).'));

  const forget = el('button', 'btn ghost', 'Forget token');
  forget.onclick = () => { localStorage.removeItem('hearth.token'); S.token = ''; render(); };
  b.append(forget);
}

// ── tracker ──────────────────────────────────────────────────
// Layered by lineage: goals at the top, then whatever hangs off them. The graph
// is the tracker's own shape (links.csv), not a second opinion about it.
const T_STATUS_TONE = {
  'закрыт': 'done', 'ревью': 'review', 'тестирование': 'review',
  'разработка': 'work', 'в беклоге': 'idle', 'планирование': 'idle', 'анализ': 'idle',
};
const T_ROOT_TYPES = ['цель', 'проект', 'веха'];

async function pollTracker(force) {
  if (!force && Date.now() - S.tracker.at < 30000) return;
  try {
    const d = await api('/api/tracker');
    S.tracker = { nodes: d.nodes || [], edges: d.edges || [], at: Date.now() };
    markOk(d);
  } catch (err) { markDown(err); }
  render();
}

function layout(nodes, edges) {
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const parent = new Map();
  edges.filter((e) => e.kind === 'parent' || e.kind === 'realises')
       .forEach((e) => { if (!parent.has(e.from)) parent.set(e.from, e.to); });

  const depth = (k, seen = new Set()) => {
    if (seen.has(k)) return 0;
    seen.add(k);
    const p = parent.get(k);
    return p && byKey.has(p) ? 1 + depth(p, seen) : 0;
  };
  const rows = new Map();
  nodes.forEach((n) => {
    const d = T_ROOT_TYPES.includes(n.type) && !parent.has(n.key) ? 0 : depth(n.key);
    if (!rows.has(d)) rows.set(d, []);
    rows.get(d).push(n);
  });

  const W = 128, H = 46, GX = 16, GY = 62;
  const pos = new Map();
  let maxW = 0;
  [...rows.keys()].sort((a, b) => a - b).forEach((d) => {
    const row = rows.get(d);
    row.sort((a, b) => (parent.get(a.key) || '').localeCompare(parent.get(b.key) || '') || a.key.localeCompare(b.key));
    row.forEach((n, i) => pos.set(n.key, { x: 12 + i * (W + GX), y: 12 + d * (H + GY), w: W, h: H }));
    maxW = Math.max(maxW, 12 + row.length * (W + GX));
  });
  const maxH = 12 + (Math.max(...rows.keys()) + 1) * (H + GY);
  return { pos, width: maxW, height: maxH };
}

function renderTracker() {
  const nodes = S.tracker.nodes, edges = S.tracker.edges;
  $('#tracker-sub').textContent = nodes.length
    ? `${nodes.length} tickets · ${edges.length} links · tap one to send it to an agent`
    : 'no tracker data';
  const svg = $('#graph');
  svg.textContent = '';
  if (!nodes.length) return;

  const { pos, width, height } = layout(nodes, edges);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (t, attrs) => { const e = document.createElementNS(NS, t);
    Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); return e; };

  edges.forEach((e) => {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) return;
    const x1 = a.x + a.w / 2, y1 = a.y, x2 = b.x + b.w / 2, y2 = b.y + b.h;
    const path = mk('path', {
      d: `M ${x1} ${y1} C ${x1} ${y1 - 26}, ${x2} ${y2 + 26}, ${x2} ${y2}`,
      class: 'edge ' + e.kind, fill: 'none',
    });
    svg.append(path);
  });

  nodes.forEach((n) => {
    const p = pos.get(n.key);
    if (!p) return;
    const g = mk('g', { class: 'node' + (S.ticket === n.key ? ' sel' : ''), tabindex: '0' });
    g.append(mk('rect', { x: p.x, y: p.y, width: p.w, height: p.h, rx: 11,
      class: 'nbox ' + (T_STATUS_TONE[n.status] || 'idle') }));
    const k = mk('text', { x: p.x + 10, y: p.y + 17, class: 'nkey' });
    k.textContent = n.key;
    const t = mk('text', { x: p.x + 10, y: p.y + 33, class: 'ntitle' });
    t.textContent = n.title.length > 17 ? n.title.slice(0, 16) + '…' : n.title;
    g.append(k, t);
    g.addEventListener('click', () => { S.ticket = n.key; render(); });
    svg.append(g);
  });

  renderTicket();
}

function renderTicket() {
  const box = $('#ticket');
  box.textContent = '';
  const n = S.tracker.nodes.find((x) => x.key === S.ticket);
  if (!n) { box.append(el('div', 'note', 'Pick a ticket in the graph.')); return; }

  const c = el('div', 'card');
  const head = el('div', 'thead');
  head.append(el('b', null, n.key));
  head.append(el('span', 'tag ' + (T_STATUS_TONE[n.status] || 'idle'), n.status || '—'));
  c.append(head);
  c.append(el('div', 'ttitle', n.title));
  if (n.note) c.append(el('div', 'rmeta', n.note));
  if (n.description) c.append(el('p', 'tbody', n.description));
  if (n.dod) { c.append(el('div', 'tlabel', 'DoD')); c.append(el('p', 'tbody', n.dod)); }

  const blockers = S.tracker.edges.filter((e) => e.kind === 'blocks' && e.to === n.key)
    .map((e) => e.from)
    .filter((k) => { const b = S.tracker.nodes.find((x) => x.key === k); return b && b.status !== 'закрыт'; });
  if (blockers.length) {
    const w = el('div', 'warn');
    w.append(el('b', null, `blocked by ${blockers.join(', ')}`));
    w.append(el('div', 'rmeta', 'delegating anyway is your call — the agent checks blockers first'));
    c.append(w);
  }

  const d = S.delegate && S.delegate.key === n.key ? S.delegate : null;
  const b = el('button', 'hold');
  b.append(el('span', 'fill'));
  const lbl = el('span', 'lbl', 'Hold to send to an agent');
  b.append(lbl);
  if (d) {
    b.classList.add('sent');
    lbl.textContent = { 'null': 'sending…', 'sent': 'queued to a new session',
      'failed': 'not sent — dsh refused' }[String(d.outcome)];
    if (d.outcome !== 'failed') b.disabled = true;
  }
  if (!isLive()) { b.disabled = true; if (!d) lbl.textContent = 'Hold to send · no link'; }
  if (!b.disabled) wireHold(b, () => sendDelegate(n.key));
  c.append(b);

  if (d && d.outcome === 'sent') {
    const go = el('button', 'btn ghost wide', 'open the session');
    go.onclick = () => { S.focus = { kind: 'session', id: d.session_id }; go2('detail'); };
    c.append(go);
  }
  box.append(c);
}

async function sendDelegate(key) {
  S.delegate = { key, at: Date.now(), outcome: null };
  render();
  try {
    const d = await api(`/api/tracker/${encodeURIComponent(key)}/delegate`, { method: 'POST' });
    markOk(d);
    S.delegate = { key, at: Date.now(), outcome: 'sent', session_id: d.session_id };
    pollState();
  } catch (err) {
    markDown(err);
    S.delegate = { key, at: Date.now(), outcome: 'failed' };
  }
  render();
}

function render() {
  renderLink();
  if (S.screen === 'home') renderHome();
  else if (S.screen === 'runs') renderRuns();
  else if (S.screen === 'detail') renderDetail();
  else if (S.screen === 'tracker') renderTracker();
  else if (S.screen === 'settings') renderSettings();
}

function go(screen) {
  S.screen = screen;
  document.querySelectorAll('.screen').forEach((s) => { s.hidden = s.dataset.screen !== screen; });
  document.querySelectorAll('.tabbar button').forEach((t) => t.classList.toggle('on', t.dataset.go === screen));
  render();
  if (screen === 'detail') pollDetail();
  if (screen === 'tracker') pollTracker();
}
const go2 = go;

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
  setInterval(() => { if (!document.hidden) pollDetail(); }, POLL_MS - 500);
  let lastLink = null;
  setInterval(() => {
    const now = linkState();
    if (now !== lastLink) { lastLink = now; render(); } else { renderLink(); }
  }, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { pollState(); pollDetail(); } });
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
})();
