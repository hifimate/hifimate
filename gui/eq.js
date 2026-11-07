/* HiFiMate EQ page: edit config/dsp-filters.yml as a list and on its curve.

The server is the one place that understands filters: it validates, saves,
and measures the exact response of a draft by running CamillaDSP offline
(hifimate/eq.py). This page keeps the draft as rows (one per filter and
channel set), draws what the server measured, and moves handles directly
while a drag waits for the next measurement.
*/

export const KINDS = {
  Peaking: { label: '峰值', gain: true },
  Lowshelf: { label: '低架', gain: true },
  Highshelf: { label: '高架', gain: true },
  Highpass: { label: '高通', gain: false },
  Lowpass: { label: '低通', gain: false },
  Notch: { label: '陷波', gain: false },
  Bandpass: { label: '带通', gain: false },
  Allpass: { label: '全通', gain: false },
  Gain: { label: '增益', gain: true, flat: true },
  Delay: { label: '延迟', gain: false, flat: true },
};
const BIQUAD_KEYS = new Set(['type', 'freq', 'gain', 'q', 'slope']);
/** A filter name the file accepts (user_filters.NAME). */
export const NAME = /^[A-Za-z0-9][A-Za-z0-9_.+-]{0,47}$/;
const F_MIN = 20, F_MAX = 20000;

/** The kind a filter is edited as, or null when only the text mode can (FIR, DiffEq, ...). */
export function kindOf(def) {
  if (!def || typeof def !== 'object') return null;
  const p = def.parameters || {};
  if (def.type === 'Biquad' && KINDS[p.type] && !KINDS[p.type].flat && Object.keys(p).every(k => BIQUAD_KEYS.has(k))) return p.type;
  if (def.type === 'Gain' && Object.keys(p).every(k => ['gain', 'inverted', 'mute', 'scale'].includes(k)) && !p.inverted && !p.mute && (p.scale ?? 'dB') === 'dB') return 'Gain';
  if (def.type === 'Delay' && Object.keys(p).every(k => ['delay', 'unit', 'subsample'].includes(k)) && (p.unit ?? 'ms') === 'ms') return 'Delay';
  return null;
}

/** The file's filters and pipeline as rows: one per (step, name), in pipeline order. */
export function rowsFromConfig(config) {
  const rows = [];
  for (const step of config?.pipeline || []) {
    for (const name of step.names || []) {
      const def = config.filters[name];
      rows.push({ name, def: structuredClone(def), channels: [...step.channels], bypassed: Boolean(step.bypassed) });
    }
  }
  // Rows of one name share its definition, as the file does.
  const shared = {};
  for (const row of rows) row.def = shared[row.name] ??= row.def;
  return rows;
}

/** Rows back to the file's form: one pipeline step per row. */
export function configFromRows(rows) {
  const filters = {}, pipeline = [];
  for (const row of rows) {
    filters[row.name] = row.def;
    pipeline.push({ type: 'Filter', channels: [...row.channels], names: [row.name], ...(row.bypassed ? { bypassed: true } : {}) });
  }
  return { filters, pipeline };
}

/** The file text for a draft (the server writes the same shape). */
export function renderConfig(config) {
  const lines = ['filters:' + (Object.keys(config.filters).length ? '' : ' {}')];
  for (const [name, def] of Object.entries(config.filters)) lines.push(`  ${name}: ${JSON.stringify(def)}`);
  lines.push('pipeline:' + (config.pipeline.length ? '' : ' []'));
  for (const step of config.pipeline) lines.push(`  - ${JSON.stringify(step)}`);
  return lines.join('\n') + '\n';
}

/** A name not used yet: eq_1, eq_2, ... */
export function freshName(rows, taken = new Set()) {
  const used = new Set([...rows.map(r => r.name), ...taken]);
  let n = 1;
  while (used.has(`eq_${n}`)) n += 1;
  return `eq_${n}`;
}

/** Imported filters, renamed so they never collide with the draft's. */
export function mergeImported(rows, config) {
  const imported = rowsFromConfig(config), renamed = {}, taken = new Set();
  for (const row of imported) {
    if (!(row.name in renamed)) { renamed[row.name] = freshName([...rows], taken); taken.add(renamed[row.name]); }
    row.name = renamed[row.name];
  }
  return [...rows, ...imported];
}

/** A biquad of the given kind with sensible values. */
export function newDef(kind, freq = 1000, gain = 0) {
  if (kind === 'Gain') return { type: 'Gain', parameters: { gain: round(gain, 1) } };
  if (kind === 'Delay') return { type: 'Delay', parameters: { delay: 0, unit: 'ms' } };
  const p = { type: kind, freq: roundFreq(freq) };
  if (KINDS[kind].gain) p.gain = round(gain, 1);
  p.q = kind === 'Peaking' ? 1.0 : kind === 'Notch' ? 10 : 0.707;
  return { type: 'Biquad', parameters: p };
}

/** A filter changed to another kind, keeping what still applies. */
export function changeKind(def, kind) {
  const p = def.parameters || {};
  const next = newDef(kind, p.freq ?? 1000, p.gain ?? 0);
  if (!KINDS[kind].flat && p.q !== undefined && kind !== 'Notch') next.parameters.q = p.q;
  return next;
}

export const round = (v, digits) => Math.round(v * 10 ** digits) / 10 ** digits;
/** Frequencies with 3 significant figures (52.3, 184, 8050), as on a hardware EQ. */
export function roundFreq(f) {
  f = Math.min(24000, Math.max(5, f));
  const step = f < 100 ? 0.1 : f < 1000 ? 1 : 10;
  return round(Math.round(f / step) * step, 1);
}
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Plot geometry: log frequency, linear dB. */
export function geometry(width, height, range, pad = { l: 38, r: 10, t: 10, b: 24 }) {
  const w = width - pad.l - pad.r, h = height - pad.t - pad.b;
  return {
    pad, w, h, range,
    x: f => pad.l + Math.log(f / F_MIN) / Math.log(F_MAX / F_MIN) * w,
    y: db => pad.t + (range - clamp(db, -range, range)) / (2 * range) * h,
    f: x => F_MIN * (F_MAX / F_MIN) ** clamp((x - pad.l) / w, 0, 1),
    db: y => range - (y - pad.t) / h * 2 * range,
  };
}
/** dB range that fits the curves and handles: 12, 18 or 24. */
export function rangeFor(values) {
  const peak = Math.max(0, ...values.filter(Number.isFinite).map(Math.abs));
  return peak <= 10 ? 12 : peak <= 16 ? 18 : 24;
}
export function curvePath(g, freqs, dbs) {
  let d = '';
  freqs.forEach((f, i) => { if (f >= F_MIN && f <= F_MAX) d += `${d ? 'L' : 'M'}${g.x(f).toFixed(1)} ${g.y(dbs[i]).toFixed(1)}`; });
  return d;
}
/** The value of a curve at f (log interpolation). */
export function curveAt(freqs, dbs, f) {
  if (!freqs?.length) return 0;
  let i = freqs.findIndex(x => x >= f);
  if (i <= 0) return dbs[i === 0 ? 0 : dbs.length - 1];
  const t = Math.log(f / freqs[i - 1]) / Math.log(freqs[i] / freqs[i - 1]);
  return dbs[i - 1] + (dbs[i] - dbs[i - 1]) * t;
}
export const sameChannels = (a, b) => a.length === b.length && a.every((c, i) => c === b[i]);
export const channelKey = channels => sameChannels(channels, [0, 1]) ? 'both' : channels[0] === 0 ? 'L' : 'R';

// --- the page --------------------------------------------------------------------------

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
async function api(path, options = {}) {
  const response = await fetch(`/ctrl/api/${path}`, { credentials: 'same-origin', cache: 'no-store', ...options });
  let body = {};
  try { body = await response.json(); } catch (_) { /* non-JSON proxy error */ }
  if (!response.ok) { const error = Error(body.error || `请求失败（${response.status}）`); error.status = response.status; throw error; }
  return body;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

function main() {
  let saved = null, rows = [], curves = null, etag = '', loggedIn = false, bypass = false, room = null;
  let selected = -1, channelView = 'LR', mode = 'visual', previewSeq = 0, previewTimer = 0, showRoom = true, showTotal = true;
  // Drawn at the plot's real size, so text and handles stay readable on a phone.
  let W = 1000, H = 340;
  function measure() {
    const width = Math.round($('plot').parentElement.clientWidth) || 1000;
    W = Math.max(300, width); H = W < 600 ? Math.round(W * 0.7) : 340;
    $('plot').setAttribute('viewBox', `0 0 ${W} ${H}`);
  }

  function toast(message, error = false) {
    const t = $('toast'); t.textContent = message; t.className = 'toast' + (error ? ' err' : ''); t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, error ? 6000 : 2500);
  }
  const draft = () => configFromRows(rows);
  const dirty = () => saved && JSON.stringify(draft()) !== JSON.stringify(configFromRows(rowsFromConfig(saved)));

  async function load() {
    const [state, eq] = await Promise.all([api('state'), api('eq')]);
    loggedIn = Boolean(state.auth?.configured && state.auth?.logged_in);
    saved = eq.config; etag = eq.etag; curves = eq.curves; bypass = eq.bypass; room = eq.room;
    rows = rowsFromConfig(saved); selected = -1;
    $('versions-list').dataset.versions = JSON.stringify(eq.versions || []);
    $('error').textContent = eq.error ? `文件有错误，下面显示的是空的 EQ：${eq.error}` : eq.curves_error || '';
    $('login').hidden = loggedIn;
    renderAll();
  }

  // --- curve ---
  function visibleCurves() {
    if (!curves) return [];
    const out = [], chans = channelView === 'LR' ? ['L', 'R'] : [channelView];
    const differ = channelsDiffer();
    const roomOn = Boolean(curves.room);
    for (const c of chans) {
      const cls = channelView === 'LR' && !differ ? 'eqLR' : `eq${c}`;
      if (roomOn && showRoom) out.push({ cls: `room ${cls}`, db: curves.room[c] });
      if (roomOn && showTotal) out.push({ cls: `${cls} faint`, db: curves.eq[c] }, { cls, db: curves.total[c] });
      else out.push({ cls, db: curves.eq[c] });
      if (channelView === 'LR' && !differ) break;
    }
    return out;
  }
  function handleRows() {
    return rows.map((row, i) => ({ row, i, kind: kindOf(row.def) }))
      .filter(h => h.kind && !KINDS[h.kind].flat && (channelView === 'LR' || h.row.channels.includes(channelView === 'L' ? 0 : 1)));
  }
  function handleY(h) {
    const p = h.row.def.parameters;
    if (KINDS[h.kind].gain) return p.gain;
    const c = h.row.channels.includes(0) ? 'L' : 'R';
    return curves ? curveAt(curves.freq, curves.eq[c], p.freq) : 0;
  }
  function renderPlot() {
    const shown = visibleCurves(), handles = handleRows();
    const range = rangeFor([...shown.flatMap(c => c.db.filter((_, i) => curves.freq[i] >= F_MIN && curves.freq[i] <= F_MAX)), ...handles.map(handleY)]);
    measure();
    const g = geometry(W, H, range, W < 600 ? { l: 30, r: 8, t: 10, b: 22 } : undefined); renderPlot.g = g;
    const radius = W < 600 ? 13 : 11;
    let html = '';
    for (let db = -range; db <= range; db += range > 12 ? 6 : 3) {
      html += `<line class="grid${db ? '' : ' zero'}" x1="${g.pad.l}" x2="${W - g.pad.r}" y1="${g.y(db)}" y2="${g.y(db)}"/>`;
      html += `<text x="${g.pad.l - 6}" y="${g.y(db) + 4}" text-anchor="end">${db > 0 ? '+' : ''}${db}</text>`;
    }
    const ticks = W < 600 ? [[20, '20'], [100, '100'], [1000, '1k'], [10000, '10k'], [20000, '20k']]
      : [[20, '20'], [50, '50'], [100, '100'], [200, '200'], [500, '500'], [1000, '1k'], [2000, '2k'], [5000, '5k'], [10000, '10k'], [20000, '20k']];
    for (const [f, label] of ticks) {
      html += `<line class="grid" x1="${g.x(f)}" x2="${g.x(f)}" y1="${g.pad.t}" y2="${H - g.pad.b}"/><text x="${g.x(f)}" y="${H - 6}" text-anchor="middle">${label}</text>`;
    }
    if (curves) for (const c of shown) html += `<path class="${c.cls}" d="${curvePath(g, curves.freq, c.db)}"/>`;
    // The selected peaking filter's bandwidth, as a band under the curve.
    const sel = handles.find(h => h.i === selected);
    if (sel && sel.kind === 'Peaking' && sel.row.def.parameters.q > 0) {
      const p = sel.row.def.parameters, half = Math.asinh(1 / (2 * p.q)) / Math.log(2);
      const lo = p.freq / 2 ** half, hi = p.freq * 2 ** half;
      html = `<rect class="qband" x="${g.x(Math.max(F_MIN, lo))}" y="${g.pad.t}" width="${Math.max(1, g.x(Math.min(F_MAX, hi)) - g.x(Math.max(F_MIN, lo)))}" height="${g.h}"/>` + html;
    }
    for (const h of handles) {
      const p = h.row.def.parameters, cls = ['handle', channelKey(h.row.channels), h.row.bypassed ? 'off' : '', h.i === selected ? 'sel' : '', loggedIn ? '' : 'ro'].join(' ');
      html += `<g class="${cls}" data-handle="${h.i}" transform="translate(${g.x(clamp(p.freq, F_MIN, F_MAX)).toFixed(1)} ${g.y(handleY(h)).toFixed(1)})"><circle r="${radius}"/><text>${h.i + 1}</text></g>`;
    }
    $('plot').innerHTML = html;
  }
  function channelsDiffer() {
    if (!curves) return false;
    const apart = (a, b) => a.some((v, i) => Math.abs(v - b[i]) > 0.05);
    return apart(curves.eq.L, curves.eq.R) || Boolean(curves.room && apart(curves.total.L, curves.total.R));
  }
  function renderLegend() {
    // Colour is the channel; the line style says which curve.
    const roomOn = Boolean(curves?.room), split = channelView !== 'LR' || channelsDiffer();
    const swatch = (color, style = 'solid', opacity = 1) => `<i style="border-color:${color};border-top-style:${style};opacity:${opacity}"></i>`;
    const chans = channelView === 'LR' ? ['L', 'R'] : [channelView];
    let html = split ? chans.map(c => `<span>${swatch(c === 'L' ? 'var(--l)' : 'var(--r)')}${c === 'L' ? '左声道' : '右声道'}</span>`).join('')
      : `<span>${swatch('var(--accent)')}左右声道相同</span>`;
    if (roomOn) {
      html += `<label><input type="checkbox" data-show="total"${showTotal ? ' checked' : ''}>${swatch('var(--muted)')}EQ + 房间校正</label>`;
      if (showTotal) html += `<span>${swatch('var(--muted)', 'solid', .45)}只有 EQ</span>`;
      html += `<label><input type="checkbox" data-show="room"${showRoom ? ' checked' : ''}>${swatch('var(--room)', 'dashed')}房间校正</label>`;
    } else html += `<span>${room?.configured ? '房间校正已关闭' : '没有房间校正'}</span>`;
    $('legend').innerHTML = html;
  }
  function renderSummary() {
    if (!curves) { $('summary').textContent = ''; return; }
    const head = curves.headroom, boost = curves.max_boost;
    const margin = `最大提升 +${boost.toFixed(1)} dB · 预留余量 ${head.toFixed(1)} dB`;
    $('summary').innerHTML = curves.clips
      ? `${margin} · <span class="warn">可能削波：提升超过预留余量 ${(boost + head).toFixed(1)} dB，建议降低提升或加一个负增益</span>`
      : `${margin} · <span class="ok">不会削波</span>`;
  }

  // --- rows ---
  function typeOptions(kind) {
    return Object.entries(KINDS).map(([k, v]) => `<option value="${k}"${k === kind ? ' selected' : ''}>${v.label}</option>`).join('');
  }
  function numberInput(cls, value, unit, attrs, disabled) {
    return `<label class="unit ${cls}"><input type="number" data-field="${cls}" value="${value ?? ''}" ${attrs}${disabled ? ' disabled' : ''}><span>${unit}</span></label>`;
  }
  function renderRows() {
    $('count').textContent = rows.length ? `${rows.length} 个` : '';
    const off = !loggedIn;
    if (!rows.length) { $('rows').innerHTML = '<p class="empty">还没有滤波器。点「添加滤波器」，或在曲线上双击空白处。</p>'; return; }
    const head = `<div class="rows-head"><div class="row"><span>序</span><span>开</span><span>名字</span><span>类型</span><span>频率</span><span>增益</span><span>Q</span><span>声道</span><span>顺序</span><span></span></div></div>`;
    $('rows').innerHTML = head + '<div class="rows">' + rows.map((row, i) => {
      const kind = kindOf(row.def), p = row.def.parameters || {}, key = channelKey(row.channels);
      const common = `<span class="n ${key}" data-select="${i}" title="处理顺序">${i + 1}</span><input class="en" type="checkbox" data-field="enabled"${row.bypassed ? '' : ' checked'}${off ? ' disabled' : ''} title="启用">` +
        `<input class="nm" data-field="name" value="${esc(row.name)}" maxlength="48" spellcheck="false" autocapitalize="off" title="滤波器的名字"${off ? ' disabled' : ''}>`;
      const move = `<span class="mv"><button type="button" data-move="${i}" data-dir="-1" aria-label="上移"${off || i === 0 ? ' disabled' : ''}>↑</button><button type="button" data-move="${i}" data-dir="1" aria-label="下移"${off || i === rows.length - 1 ? ' disabled' : ''}>↓</button></span>`;
      const channel = `<select class="ch" data-field="channels"${off ? ' disabled' : ''}><option value="both"${key === 'both' ? ' selected' : ''}>左右</option><option value="L"${key === 'L' ? ' selected' : ''}>左</option><option value="R"${key === 'R' ? ' selected' : ''}>右</option></select>`;
      const del = `<button type="button" class="del" data-del="${i}" aria-label="删除"${off ? ' disabled' : ''}>×</button>`;
      let middle;
      if (!kind) middle = `<span class="type raw" title="${esc(JSON.stringify(row.def))}">${esc(row.def.type)}</span><span class="raw f">${esc(row.name)} · 在「文本」里编辑</span>`;
      else if (kind === 'Gain') middle = `<select class="type" data-field="kind"${off ? ' disabled' : ''}>${typeOptions(kind)}</select><span class="f"></span>${numberInput('g', p.gain, 'dB', 'step="0.1" min="-40" max="20"', off)}<span class="q"></span>`;
      else if (kind === 'Delay') middle = `<select class="type" data-field="kind"${off ? ' disabled' : ''}>${typeOptions(kind)}</select>${numberInput('d', p.delay, 'ms', 'step="0.01" min="0" max="1000"', off)}<span class="g"></span><span class="q"></span>`;
      else {
        const slope = p.slope !== undefined && p.q === undefined;
        middle = `<select class="type" data-field="kind"${off ? ' disabled' : ''}>${typeOptions(kind)}</select>` +
          numberInput('f', p.freq, 'Hz', 'step="any" min="5" max="24000"', off) +
          numberInput('g', KINDS[kind].gain ? p.gain : '', 'dB', 'step="0.1" min="-24" max="24"', off || !KINDS[kind].gain) +
          (slope ? numberInput('s', p.slope, 'dB/oct', 'step="1" min="1" max="12"', off) : numberInput('q', p.q, 'Q', 'step="0.01" min="0.05" max="40"', off));
      }
      return `<div class="row${i === selected ? ' sel' : ''}${row.bypassed ? ' off' : ''}" data-row="${i}">${common}${middle}${channel}${move}${del}</div>`;
    }).join('') + '</div>';
  }
  function renderControls() {
    for (const el of document.querySelectorAll('.need-login')) el.disabled = !loggedIn;
    $('dirty').hidden = !dirty();
    $('bypass-banner').hidden = !bypass;
    $('bypass-button').classList.toggle('on-toggle', bypass);
    $('bypass-button').textContent = bypass ? '恢复 EQ' : '旁路对比';
    $('hint').hidden = !loggedIn;
    for (const b of $('mode-seg').children) b.disabled = !loggedIn && b.dataset.mode === 'text';
  }
  function renderAll() { renderPlot(); renderLegend(); renderSummary(); if (mode === 'visual') renderRows(); renderControls(); }

  // --- preview: exact curves for the draft, measured by the server ---
  function changed({ rows: redrawRows = true } = {}) {
    renderPlot(); if (redrawRows && mode === 'visual') renderRows(); renderControls();
    if (!loggedIn) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 150);
  }
  async function preview() {
    const seq = ++previewSeq;
    try {
      const result = await post('eq/preview', { config: draft() });
      if (seq !== previewSeq) return;
      curves = result.curves; $('error').textContent = '';
      renderPlot(); renderLegend(); renderSummary();
    } catch (error) { if (seq === previewSeq) $('error').textContent = error.message; }
  }

  // --- editing ---
  function updateField(i, field, value, input) {
    const row = rows[i], p = row.def.parameters;
    const number = Number(value), ok = value !== '' && Number.isFinite(number);
    if (input) input.classList.toggle('bad', ['f', 'g', 'q', 's', 'd'].includes(field) && !ok);
    if (field === 'name') {
      const name = String(value).trim();
      const taken = rows.some(r => r.name === name && r.def !== row.def);
      const bad = !NAME.test(name) || taken;
      if (input) { input.classList.toggle('bad', bad); input.title = bad ? (taken ? '这个名字已经被别的滤波器用了' : '只能用字母、数字和 _ . + -，以字母或数字开头，最多 48 个') : '滤波器的名字'; }
      if (bad) return false;
      const old = row.name;
      for (const r of rows) if (r.name === old) r.name = name;     // rows of one filter share its name
      return true;
    }
    if (field === 'enabled') row.bypassed = !value;
    else if (field === 'channels') row.channels = value === 'both' ? [0, 1] : value === 'L' ? [0] : [1];
    else if (field === 'kind') { const def = changeKind(row.def, value); for (const r of rows) if (r.name === row.name) r.def = def; }
    else if (!ok) return false;
    else if (field === 'f') p.freq = clamp(number, 5, 24000);
    else if (field === 'g') p.gain = clamp(number, -40, 24);
    else if (field === 'q') p.q = clamp(number, 0.05, 40);
    else if (field === 's') p.slope = clamp(number, 1, 12);
    else if (field === 'd') p.delay = clamp(number, 0, 1000);
    return true;
  }
  $('rows').addEventListener('input', event => {
    const el = event.target, rowEl = el.closest('[data-row]');
    if (!rowEl || !el.dataset.field || el.tagName === 'SELECT' || el.type === 'checkbox' || el.dataset.field === 'name') return;
    if (updateField(Number(rowEl.dataset.row), el.dataset.field, el.value, el)) changed({ rows: false });
  });
  $('rows').addEventListener('change', event => {
    const el = event.target, rowEl = el.closest('[data-row]');
    if (!rowEl || !el.dataset.field) return;
    const value = el.type === 'checkbox' ? el.checked : el.value;
    if (updateField(Number(rowEl.dataset.row), el.dataset.field, value, el)) changed({ rows: el.tagName === 'SELECT' || el.type === 'checkbox' || el.dataset.field === 'name' });
  });
  $('rows').addEventListener('click', event => {
    const mv = event.target.closest('[data-move]');
    if (mv) {
      const i = Number(mv.dataset.move), j = i + Number(mv.dataset.dir);
      if (j < 0 || j >= rows.length) return;
      [rows[i], rows[j]] = [rows[j], rows[i]];
      selected = selected === i ? j : selected === j ? i : selected;
      changed(); return;
    }
    const del = event.target.closest('[data-del]');
    if (del) { rows.splice(Number(del.dataset.del), 1); selected = -1; changed(); return; }
    const pick = event.target.closest('[data-select]');
    if (pick) { selected = Number(pick.dataset.select) === selected ? -1 : Number(pick.dataset.select); renderPlot(); renderRows(); }
  });
  function addFilter(kind = 'Peaking', freq = 1000, gain = 0) {
    rows.push({ name: freshName(rows), def: newDef(kind, freq, gain), channels: [0, 1], bypassed: false });
    selected = rows.length - 1; changed();
  }

  // --- the curve: drag, wheel and pinch ---
  const pointers = new Map();
  let drag = null;
  function plotPoint(event) {
    const box = $('plot').getBoundingClientRect();
    return { x: (event.clientX - box.left) / box.width * W, y: (event.clientY - box.top) / box.height * H };
  }
  $('plot').addEventListener('pointerdown', event => {
    if (!loggedIn) return;
    pointers.set(event.pointerId, plotPoint(event));
    const handle = event.target.closest('[data-handle]');
    if (pointers.size === 2 && selected >= 0) {
      const [a, b] = [...pointers.values()], p = rows[selected].def.parameters;
      if (p.q !== undefined) drag = { pinch: true, distance: Math.abs(a.x - b.x) || 1, q: p.q };
      return;
    }
    if (!handle) return;
    const i = Number(handle.dataset.handle);
    selected = i; drag = { i, kind: kindOf(rows[i].def) };
    $('plot').setPointerCapture(event.pointerId);
    renderPlot(); renderRows();
    event.preventDefault();
  });
  $('plot').addEventListener('pointermove', event => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, plotPoint(event));
    if (!drag) return;
    const g = renderPlot.g;
    if (drag.pinch) {
      if (pointers.size < 2) return;
      const [a, b] = [...pointers.values()], scale = (Math.abs(a.x - b.x) || 1) / drag.distance;
      rows[selected].def.parameters.q = round(clamp(drag.q / scale, 0.1, 20), 2);
    } else {
      const { x, y } = pointers.get(event.pointerId), p = rows[drag.i].def.parameters;
      p.freq = roundFreq(g.f(x));
      if (KINDS[drag.kind].gain) p.gain = round(clamp(g.db(y), -24, 24), 1);
    }
    changed({ rows: true });
  });
  const release = event => { pointers.delete(event.pointerId); if (pointers.size === 0) drag = null; else if (drag?.pinch) drag = null; };
  $('plot').addEventListener('pointerup', release);
  $('plot').addEventListener('pointercancel', release);
  $('plot').addEventListener('wheel', event => {
    if (!loggedIn || selected < 0) return;
    const p = rows[selected].def.parameters;
    if (p?.q === undefined) return;
    event.preventDefault();
    p.q = round(clamp(p.q * Math.exp(-event.deltaY * 0.0015), 0.1, 20), 2);
    changed();
  }, { passive: false });
  $('plot').addEventListener('dblclick', event => {
    if (!loggedIn || event.target.closest('[data-handle]')) return;
    const { x, y } = plotPoint(event), g = renderPlot.g;
    addFilter('Peaking', g.f(x), clamp(g.db(y), -24, 24));
  });

  // --- toolbar ---
  async function apply() {
    if (mode === 'text' && !await leaveText()) return;
    try {
      const result = await post('eq/apply', { config: draft(), etag });
      etag = result.etag; saved = draft();
      $('versions-list').dataset.versions = JSON.stringify(result.versions || []);
      toast(result.live ? '已应用，正在播放的声音已经改变' : '已保存，下次播放时生效');
      renderControls();
    } catch (error) {
      if (error.status === 409) {
        $('error').textContent = error.message;
        if (window.confirm(`${error.message}\n\n重新载入文件？（你现在的修改会丢失）`)) load();
      } else toast(error.message, true);
    }
  }
  async function setBypass() {
    try { bypass = (await post('eq/bypass', { on: !bypass })).bypass; renderControls(); toast(bypass ? '已旁路：现在听到的是没有 EQ 的声音' : '已恢复 EQ'); }
    catch (error) { toast(error.message, true); }
  }
  async function enterText() {
    $('text').value = renderConfig(draft());
    mode = 'text'; $('rows').hidden = true; $('text-mode').hidden = false;
    for (const b of $('mode-seg').children) b.classList.toggle('on', b.dataset.mode === 'text');
  }
  async function leaveText() {
    try {
      const { config } = await post('eq/parse', { text: $('text').value });
      rows = rowsFromConfig(config); selected = -1;
    } catch (error) { $('error').textContent = error.message; return false; }
    $('error').textContent = '';
    mode = 'visual'; $('rows').hidden = false; $('text-mode').hidden = true;
    for (const b of $('mode-seg').children) b.classList.toggle('on', b.dataset.mode === 'visual');
    changed(); return true;
  }
  function showVersions() {
    const list = JSON.parse($('versions-list').dataset.versions || '[]');
    $('versions-list').innerHTML = list.length ? list.map(v => `<div class="version"><div><b>${esc(new Date(v.created * 1000).toLocaleString('zh-CN', { hour12: false }))}</b>
      <div class="sub">${v.filters === null ? '文件有错误' : `${v.filters} 个滤波器`}</div></div><span class="spacer"></span>
      <button type="button" data-version="${esc(v.id)}"${loggedIn ? '' : ' disabled'}>载入</button></div>`).join('') : '<p class="empty">还没有历史版本。</p>';
    $('versions-dialog').showModal();
  }
  document.addEventListener('click', async event => {
    const act = event.target.closest('[data-act]')?.dataset.act;
    if (act === 'add') addFilter('Peaking', 1000, 0);
    else if (act === 'apply') apply();
    else if (act === 'revert') { rows = rowsFromConfig(saved); selected = -1; if (mode === 'text') $('text').value = renderConfig(draft()); changed(); }
    else if (act === 'bypass') setBypass();
    else if (act === 'versions') showVersions();
    else if (act === 'import') { $('import-error').textContent = ''; $('import-dialog').showModal(); }
    const version = event.target.closest('[data-version]')?.dataset.version;
    if (version) {
      try { rows = rowsFromConfig((await post('eq/version', { id: version })).config); selected = -1; $('versions-dialog').close(); changed(); toast('已载入到编辑区，点「应用」生效'); }
      catch (error) { toast(error.message, true); }
    }
    const how = event.target.closest('[data-import]')?.dataset.import;
    if (how) {
      try {
        const { config, skipped } = await post('eq/import', { text: $('import-text').value });
        rows = how === 'replace' ? rowsFromConfig(config) : mergeImported(rows, config);
        selected = -1; $('import-dialog').close(); $('import-text').value = ''; changed();
        toast(`导入了 ${Object.keys(config.filters).length} 个滤波器${skipped ? `，${skipped} 行没有认出` : ''}`);
      } catch (error) { $('import-error').textContent = error.message; }
    }
    if (event.target.closest('[data-close]')) event.target.closest('dialog').close();
    const ch = event.target.closest('[data-ch]')?.dataset.ch;
    if (ch) { channelView = ch; for (const b of $('channel-seg').children) b.classList.toggle('on', b.dataset.ch === ch); renderPlot(); renderLegend(); }
    const m = event.target.closest('[data-mode]')?.dataset.mode;
    if (m && m !== mode) { if (m === 'text') enterText(); else leaveText(); }
  });
  $('legend').addEventListener('change', event => {
    if (event.target.dataset.show === 'room') showRoom = event.target.checked;
    if (event.target.dataset.show === 'total') showTotal = event.target.checked;
    renderPlot(); renderLegend();
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await post('login', { password: $('password').value }); $('password').value = ''; await load(); }
    catch (error) { $('login-error').textContent = error.message; }
  });
  let resized = 0;
  window.addEventListener('resize', () => { cancelAnimationFrame(resized); resized = requestAnimationFrame(renderPlot); });
  window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
  load().catch(error => { $('error').textContent = error.message; });
}

if (typeof document !== 'undefined' && document.getElementById('plot')) main();
