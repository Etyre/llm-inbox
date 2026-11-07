// LLM Inbox page: inbox list on the left, one conversation on the right.
// Reply drafts persist per message in localStorage and are cleared only after the model answers.
'use strict';
const $ = (id) => document.getElementById(id);
let messages = [], current = null, streaming = false;

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};
const draftKey = (id) => `draft:${id}`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

function fmtTime(iso) {
  const d = new Date(iso.replace(/([+-]\d\d)(\d\d)$/, '$1:$2'));
  return isNaN(d) ? iso : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function blocksToHtml(content) {
  if (typeof content === 'string') return `<pre>${esc(content)}</pre>`;
  return (content || []).map((b) => {
    if (b.type === 'text') return `<pre>${esc(b.text)}</pre>`;
    if (b.type === 'tool_use') return `<div class="tool">called ${esc(b.name)}: ${esc(JSON.stringify(b.input))}</div>`;
    if (b.type === 'tool_result') return `<div class="tool">tool_result: ${esc(typeof b.content === 'string' ? b.content : JSON.stringify(b.content))}</div>`;
    if (b.type === 'thinking') return b.thinking ? `<div class="tool">thinking: ${esc(b.thinking)}</div>` : '';
    if (b.type === 'image') return `<div class="tool">[image]</div>`;
    return `<div class="tool">[${esc(b.type)}]</div>`;
  }).join('');
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

async function loadList() {
  messages = await api('/api/messages');
  renderList();
}
function renderList() {
  const showHandled = $('show-handled').checked;
  const ul = $('messages'); ul.innerHTML = '';
  const shown = messages.filter((m) => showHandled || m.status !== 'handled');
  $('empty').hidden = shown.length > 0;
  for (const m of shown) {
    const li = document.createElement('li');
    li.className = (m.status === 'new' && m.turns === 0 ? 'new ' : '') + (current && current.id === m.id ? 'active' : '');
    li.innerHTML = `<div class="top"><span>${m.status === 'new' && m.turns === 0 ? '<span class="dot"></span>' : ''}${esc(m.app)} · ${esc(m.model)}</span><span>${esc(fmtTime(m.at))}</span></div>
                    <div class="line">${esc(m.first_line)}</div>`;
    li.onclick = () => { location.hash = m.id; };
    ul.appendChild(li);
  }
}

async function open(id) {
  if (streaming) return;
  try { current = await api(`/api/messages/${encodeURIComponent(id)}`); }
  catch (e) { $('hint').textContent = e.message; return; }
  $('placeholder').hidden = true; $('detail').hidden = false;
  $('d-app').textContent = current.app; $('d-model').textContent = current.model; $('d-at').textContent = fmtTime(current.at);
  $('d-status').textContent = current.status + (current.note ? ` (${current.note})` : '');
  $('btn-done').hidden = current.status === 'handled'; $('btn-reopen').hidden = current.status !== 'handled';
  $('d-message').textContent = current.message || '(empty)';
  $('d-system').textContent = current.system || '(none)';
  $('d-conv').innerHTML = current.messages.map((m) => `<div class="turn"><div class="role">${esc(m.role)}</div>${blocksToHtml(m.content)}</div>`).join('')
    + `<div class="turn"><div class="role">assistant (the response that carried the message${current.response.stop_reason ? ', stop_reason=' + esc(current.response.stop_reason) : ''})</div>${blocksToHtml(current.response.content)}</div>`;
  renderTurns();
  // Restore the draft before the box is visible.
  const ta = $('reply'); ta.value = store.get(draftKey(id)) || '';
  $('hint').textContent = current.transcript.length ? '' : `Your first reply is prefaced with: "${current.intro}"`;
  renderList();
  ta.focus();
}

function renderTurns() {
  const t = $('turns'); t.innerHTML = '';
  for (const e of current.transcript) addTurn(e.role, e.content, e.stop_reason);
  t.scrollIntoView(false);
}
function addTurn(role, content, stopReason) {
  const div = document.createElement('div');
  div.className = `turn ${role}`;
  div.innerHTML = `<div class="who">${role === 'user' ? 'Eli' : esc(current.model)}</div>${blocksToHtml(content)}`
    + (stopReason === 'refusal' ? '<div class="tool">stop_reason=refusal: the API safety layer ended this turn</div>' : '');
  $('turns').appendChild(div);
  return div;
}

async function send() {
  const ta = $('reply'); const text = ta.value.trim();
  if (!text || streaming || !current) return;
  streaming = true; $('send').disabled = true; ta.disabled = true; $('hint').textContent = `Waiting for ${current.model}…`;
  const mine = addTurn('user', [{ type: 'text', text }]);
  const theirs = addTurn('assistant', []); const pre = document.createElement('pre'); theirs.appendChild(pre);
  try {
    const r = await fetch(`/api/messages/${encodeURIComponent(current.id)}/reply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '', done = null;
    for (;;) {
      const { value, done: end } = await reader.read(); if (end) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = /^event: (\w+)/m.exec(chunk)?.[1]; const data = /^data: (.*)$/m.exec(chunk)?.[1];
        if (!ev || !data) continue;
        const d = JSON.parse(data);
        if (ev === 'delta') { pre.textContent += d.text; window.scrollTo(0, document.body.scrollHeight); }
        else if (ev === 'error') throw new Error(d.message);
        else if (ev === 'done') done = d;
      }
    }
    if (!done) throw new Error('the reply ended without a result');
    current.transcript.push(done.user, done.assistant);
    store.del(draftKey(current.id)); ta.value = '';
    renderTurns(); $('hint').textContent = '';
    const m = messages.find((x) => x.id === current.id); if (m) { m.turns = current.transcript.length; renderList(); }
  } catch (e) {
    mine.remove(); theirs.remove();
    $('hint').textContent = `Not sent: ${e.message}. Your text is kept below.`;
  } finally {
    streaming = false; $('send').disabled = false; ta.disabled = false; ta.focus();
  }
}

async function setStatus(status) {
  await api(`/api/messages/${encodeURIComponent(current.id)}/status`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) });
  await loadList(); await open(current.id);
}

$('reply').addEventListener('input', (e) => { if (current) store.set(draftKey(current.id), e.target.value); });
$('reply').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
$('send').onclick = send;
$('btn-done').onclick = () => setStatus('handled');
$('btn-reopen').onclick = () => setStatus('new');
$('show-handled').onchange = () => { store.set('showHandled', $('show-handled').checked ? '1' : '0'); renderList(); };
window.addEventListener('hashchange', () => { const id = location.hash.slice(1); if (id) open(id); });

(async () => {
  $('show-handled').checked = store.get('showHandled') === '1';
  await loadList();
  const id = location.hash.slice(1);
  if (id) open(id);
  setInterval(() => { if (!streaming) loadList().catch(() => {}); }, 30000);
})();
