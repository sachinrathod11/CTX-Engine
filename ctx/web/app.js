'use strict';
const $ = (id) => document.getElementById(id);
const S = { pid: null, projects: [], result: null, include: new Set(), exclude: new Set(), poll: null, health: null };
const fmt = (n) => Number(n || 0).toLocaleString();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem('ctx.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('ctx.' + k, JSON.stringify(v)); } catch { /* storage off */ } },
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error((data && (data.detail?.[0]?.msg || data.detail)) || `Request failed (${res.status})`);
  return data;
}

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2200);
}

function ago(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

// ---------------------------------------------------------------- health
async function loadHealth() {
  try {
    const h = await api('/api/health');
    S.health = h;
    const el = $('llm-status');
    const l = h.llm;
    if (!l.use_llm) { el.className = 'pill pill-muted'; el.textContent = 'Offline mode (LLM turned off)'; }
    else if (l.llm_ready && l.embed_ready) { el.className = 'pill pill-ok'; el.textContent = `${l.llm_model} · ${l.embed_model}`; }
    else if (l.ollama_running) {
      const miss = [!l.llm_ready && l.llm_model, !l.embed_ready && l.embed_model].filter(Boolean);
      el.className = 'pill pill-warn'; el.textContent = `Run: ollama pull ${miss.join(' & ')}`;
    } else { el.className = 'pill pill-muted'; el.textContent = 'Offline mode (Ollama not running)'; }
    el.title = 'Without Ollama, ctx still works: keyword + structure search, template rewrite.';
    if (!$('budget').dataset.touched) { $('budget').value = store.get('budget', h.budget); updateBudget(); }
  } catch { /* server restarting */ }
}

// ---------------------------------------------------------------- projects
async function loadProjects(selectId) {
  S.projects = await api('/api/projects');
  const ul = $('projects');
  ul.innerHTML = '';
  if (!S.projects.length) ul.innerHTML = '<li class="empty-li">No projects yet</li>';
  for (const p of S.projects) {
    const li = document.createElement('li');
    const busy = p.job && p.job.state === 'running';
    li.innerHTML = `<span class="p-name">${esc(p.name)}</span><span class="p-meta">${busy ? 'indexing…' : fmt(p.files) + ' files'}</span>`;
    li.title = p.path;
    li.onclick = () => selectProject(p.id);
    if (p.id === S.pid) li.classList.add('active');
    ul.appendChild(li);
  }
  const want = selectId || S.pid || store.get('pid');
  if (want && S.projects.some((p) => p.id === want)) { if (want !== S.pid) selectProject(want); }
  else if (S.projects.length) selectProject(S.projects[0].id);
  else { S.pid = null; $('proj-info').hidden = true; $('history-wrap').hidden = true; }
}

async function selectProject(pid) {
  S.pid = pid;
  store.set('pid', pid);
  document.querySelectorAll('#projects li').forEach((li, i) => li.classList.toggle('active', S.projects[i]?.id === pid));
  $('proj-info').hidden = false;
  S.result = null; S.include.clear(); S.exclude.clear();
  $('result').hidden = true; $('empty').hidden = false;
  await refreshStatus();
  loadHistory();
  loadFileList('');
}

async function refreshStatus() {
  if (!S.pid) return;
  let s;
  try { s = await api(`/api/projects/${S.pid}`); } catch (e) { return; }
  $('pi-name').textContent = s.name;
  $('pi-path').textContent = s.path;
  const job = s.job || {};
  const running = job.state === 'running';
  $('pi-progress').hidden = !running;
  if (running) {
    const labels = { scan: 'Scanning', parse: 'Parsing', graph: 'Linking', summarize: 'Summarizing (LLM)', embed: 'Embedding', done: 'Done' };
    $('pi-stage').textContent = labels[job.stage] || job.stage || 'Starting';
    $('pi-count').textContent = job.total > 1 ? `${fmt(job.done)} / ${fmt(job.total)}` : '';
    $('pi-bar').style.width = job.total ? `${Math.round((100 * job.done) / job.total)}%` : '5%';
    $('pi-msg').textContent = job.message || '';
  }
  if (job.state === 'error') { $('pi-msg').textContent = job.message; $('pi-progress').hidden = false; }
  const rows = [
    ['Files', fmt(s.files)],
    ['Code chunks', fmt(s.chunks)],
    ['LLM summaries', `${fmt(s.llm_summaries)} / ${fmt(s.files)}`],
    ['Import links', fmt(s.import_edges)],
    ['API links', fmt(s.http_edges)],
    ['Search vectors', s.embed_model || '—'],
    ['Updated', ago(s.indexed_at)],
    ['Watching files', s.watching ? 'yes' : 'no'],
  ];
  $('pi-stats').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
  $('reindex').disabled = running;
  clearTimeout(S.poll);
  if (running) S.poll = setTimeout(async () => { await refreshStatus(); if (!(await isRunning())) loadProjects(); }, 1200);
}

async function isRunning() {
  try { const s = await api(`/api/projects/${S.pid}`); return s.job && s.job.state === 'running'; } catch { return false; }
}

async function loadFileList(q) {
  if (!S.pid) return;
  try {
    const files = await api(`/api/projects/${S.pid}/files?q=${encodeURIComponent(q)}`);
    $('file-list').innerHTML = files.map((f) => `<option value="${esc(f)}">`).join('');
  } catch { /* ignore */ }
}

async function loadHistory() {
  if (!S.pid) return;
  let h = [];
  try { h = await api(`/api/projects/${S.pid}/history`); } catch { /* ignore */ }
  $('history-wrap').hidden = !h.length;
  $('history').innerHTML = '';
  const seen = new Set();
  for (const item of h) {
    if (seen.has(item.question)) continue;
    seen.add(item.question);
    const li = document.createElement('li');
    li.innerHTML = `<span>${esc(item.question)}</span><span class="h-meta">${fmt(item.tokens)} tokens · ${ago(item.time)}</span>`;
    li.onclick = () => { $('q').value = item.question; build(); };
    $('history').appendChild(li);
    if (seen.size >= 8) break;
  }
}

// ---------------------------------------------------------------- build
function updateBudget() { $('budget-out').textContent = fmt($('budget').value); }

async function build(keepSelection = false) {
  const q = $('q').value.trim();
  $('ask-error').hidden = true;
  if (!S.pid) { showAskError('Add a project first.'); return; }
  if (!q) { showAskError('Type a question first.'); $('q').focus(); return; }
  if (!keepSelection) { S.include.clear(); S.exclude.clear(); }
  $('go').disabled = true;
  $('empty').hidden = true;
  $('loading').hidden = false;
  $('loading-msg').textContent = $('use-llm').checked && S.health?.llm?.llm_ready
    ? 'Searching your project and asking the local model…' : 'Searching your project…';
  try {
    const res = await api(`/api/projects/${S.pid}/prompt`, {
      method: 'POST',
      body: JSON.stringify({
        question: q, budget: Number($('budget').value), use_llm: $('use-llm').checked, rewrite: $('rewrite').checked,
        include: [...S.include], exclude: [...S.exclude],
      }),
    });
    S.result = res;
    renderResult(res);
    loadHistory();
  } catch (e) {
    showAskError(e.message);
    if (!S.result) $('empty').hidden = false;
  } finally {
    $('go').disabled = false;
    $('loading').hidden = true;
  }
}

function showAskError(msg) { $('ask-error').textContent = msg; $('ask-error').hidden = false; }

function renderResult(r) {
  $('result').hidden = false;
  $('prompt').value = r.prompt;
  $('rewritten').textContent = r.rewritten;
  updateMeter(r.tokens, r.budget, false);
  const red = r.redactions.length;
  $('redact-badge').hidden = !red;
  if (red) {
    $('redact-badge').textContent = `${red} secret${red > 1 ? 's' : ''} removed`;
    $('redact-badge').title = r.redactions.map((x) => `${x.kind} in ${x.where} (line ${x.line})`).join('\n');
  }
  const t = r.timings;
  const total = Object.values(t).reduce((a, b) => a + b, 0);
  const modes = r.modes;
  $('timings').textContent = `Built in ${total.toFixed(1)}s · search terms: ${r.terms.slice(0, 10).join(', ')} · ` +
    `rerank: ${modes.rerank} · vectors: ${modes.vector}`;

  const ul = $('files');
  ul.innerHTML = '';
  const sel = r.files.filter((f) => f.selected);
  const other = r.files.filter((f) => !f.selected);
  const row = (f) => {
    const li = document.createElement('li');
    const checked = f.selected ? !S.exclude.has(f.path) : S.include.has(f.path);
    if (!checked) li.classList.add('off');
    const why = (f.reasons || []).filter((x) => !['bm25', 'bm25_expanded', 'vector', 'path', 'phrase'].includes(x)).slice(0, 2);
    const hits = (f.reasons || []).filter((x) => ['bm25', 'bm25_expanded', 'vector', 'path', 'phrase'].includes(x));
    const label = { bm25: 'keywords', bm25_expanded: 'related words', vector: 'meaning', path: 'file name', phrase: 'exact text' };
    li.innerHTML = `<input type="checkbox" ${checked ? 'checked' : ''} aria-label="Include ${esc(f.path)}">
      <div class="f-path">${esc(f.path)}${f.lines ? `<span class="f-lines">${esc(f.lines)}</span>` : ''}</div>
      ${f.summary ? `<div class="f-sum">${esc(f.summary.slice(0, 180))}</div>` : ''}
      ${why.length || hits.length ? `<div class="f-why">${esc([...hits.map((h) => label[h]), ...why].join(' · '))}</div>` : ''}`;
    li.querySelector('input').onchange = (e) => {
      if (f.selected) { e.target.checked ? S.exclude.delete(f.path) : S.exclude.add(f.path); }
      else { e.target.checked ? S.include.add(f.path) : S.include.delete(f.path); }
      li.classList.toggle('off', !e.target.checked);
      $('rebuild').disabled = !S.include.size && !S.exclude.size;
    };
    return li;
  };
  sel.forEach((f) => ul.appendChild(row(f)));
  if (other.length) {
    const sep = document.createElement('li');
    sep.className = 'sep';
    sep.textContent = 'Also considered';
    ul.appendChild(sep);
    other.slice(0, 6).forEach((f) => ul.appendChild(row(f)));
  }
  $('rebuild').disabled = true;
  $('conn-wrap').hidden = !r.connections.length;
  $('connections').innerHTML = r.connections.map((c) => `<li>${esc(c).replace(/`([^`]+)`/g, '<code>$1</code>')}</li>`).join('');
}

function updateMeter(tokens, budget, approx) {
  const pct = Math.min(100, Math.round((100 * tokens) / budget));
  $('meter-bar').style.width = pct + '%';
  $('tok').textContent = `${approx ? '≈ ' : ''}${fmt(tokens)} / ${fmt(budget)} tokens`;
}

async function copyPrompt() {
  const text = $('prompt').value;
  try { await navigator.clipboard.writeText(text); }
  catch {
    $('prompt').select();
    document.execCommand('copy');
  }
  toast('Prompt copied. Paste it into any chatbot.');
}

function downloadPrompt() {
  const blob = new Blob([$('prompt').value], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const slug = ($('q').value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40) || 'prompt').replace(/^-|-$/g, '');
  a.download = `ctx-${slug}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------------------------------------------------------------- wiring
$('add-form').onsubmit = async (e) => {
  e.preventDefault();
  const path = $('add-path').value.trim();
  $('add-error').hidden = true;
  if (!path) { $('add-error').textContent = 'Paste a folder path first.'; $('add-error').hidden = false; return; }
  try {
    const p = await api('/api/projects', { method: 'POST', body: JSON.stringify({ path }) });
    $('add-path').value = '';
    await loadProjects(p.id);
    toast(`Added ${p.name}. Indexing…`);
  } catch (err) { $('add-error').textContent = err.message; $('add-error').hidden = false; }
};
$('add-path').oninput = () => { $('add-error').hidden = true; };
$('reindex').onclick = async () => { await api(`/api/projects/${S.pid}/index`, { method: 'POST', body: '{}' }); refreshStatus(); };
$('remove').onclick = async () => {
  if (!S.pid) return;
  await api(`/api/projects/${S.pid}`, { method: 'DELETE' });
  S.pid = null; store.set('pid', null);
  $('result').hidden = true; $('empty').hidden = false;
  loadProjects();
};
$('go').onclick = () => build(false);
$('rebuild').onclick = () => build(true);
$('q').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); build(false); } });
$('q').oninput = () => { $('ask-error').hidden = true; };
$('budget').oninput = () => { $('budget').dataset.touched = '1'; store.set('budget', Number($('budget').value)); updateBudget(); };
$('use-llm').onchange = () => store.set('use_llm', $('use-llm').checked);
$('rewrite').onchange = () => store.set('rewrite', $('rewrite').checked);
$('copy').onclick = copyPrompt;
$('download').onclick = downloadPrompt;
$('prompt').oninput = () => { if (S.result) updateMeter(Math.round($('prompt').value.length / 3.6), S.result.budget, true); };
$('add-file').onclick = () => {
  const f = $('file-search').value.trim();
  if (!f) return;
  S.include.add(f); S.exclude.delete(f);
  $('file-search').value = '';
  build(true);
};
$('file-search').oninput = (e) => loadFileList(e.target.value);

$('use-llm').checked = store.get('use_llm', true);
$('rewrite').checked = store.get('rewrite', true);
updateBudget();
loadHealth();
setInterval(loadHealth, 15000);
loadProjects().catch((e) => showAskError('Could not reach the ctx server: ' + e.message));
