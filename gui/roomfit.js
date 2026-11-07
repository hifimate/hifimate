/* HiFiMate guided room correction: level, positions (accept / redo), compute, apply.
   The phone records while HiFiMate plays each sweep through the real chain; the
   host analyses, judges and fits. Everything resumes from the server session. */

/** Mono float samples -> 32-bit PCM WAV (lossless for the 24-bit-ish phone mic). */
export function encodeWav(samples, rate) {
  const data = samples.length * 4, buffer = new ArrayBuffer(44 + data), view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + data, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 4, true); view.setUint16(32, 4, true); view.setUint16(34, 32, true);
  text(36, 'data'); view.setUint32(40, data, true);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt32(44 + i * 4, Math.round(v * 2147483647), true);
  }
  return buffer;
}

/** iOS/Safari processing flags that change the frequency response. */
export function processingIssues(settings) {
  return ['echoCancellation', 'noiseSuppression', 'autoGainControl'].filter(key => settings?.[key] === true);
}

export const MIN_POSITIONS = 3;

/** Where a (resumed) session continues: intro, level, a position, or compute. */
export function nextStep(session) {
  if (!session) return { view: 'intro' };
  if (session.state === 'level' || !session.level_db) return { view: 'level' };
  if (session.state === 'computed') return { view: 'compute' };
  const open = session.positions.find(p => p.status !== 'accepted');
  const accepted = session.positions.filter(p => p.status === 'accepted').length;
  if (!open) return { view: 'compute' };
  return { view: 'position', key: open.key, canFinish: accepted >= MIN_POSITIONS };
}

/** The worse of the two channels' verdicts. */
export function combinedVerdict(verdicts) {
  const order = { good: 0, warn: 1, bad: 2 };
  const list = Object.values(verdicts || {});
  if (!list.length) return null;
  const grade = list.reduce((worst, v) => order[v.grade] > order[worst] ? v.grade : worst, 'good');
  return { grade, reasons: [...new Set(list.flatMap(v => v.reasons))] };
}

const F_MIN = 20, F_MAX = 20000, DB_MIN = -20, DB_MAX = 20;
export function plotX(f, width, lo = F_MIN, hi = F_MAX) { return Math.log(f / lo) / Math.log(hi / lo) * width; }
export function plotY(db, height) { return (DB_MAX - Math.max(DB_MIN, Math.min(DB_MAX, db))) / (DB_MAX - DB_MIN) * height; }
export function plotPath(freqs, dbs, width, height, lo = F_MIN, hi = F_MAX) {
  const points = freqs.map((f, i) => [f, dbs[i]]).filter(([f]) => f >= lo && f <= hi);
  return points.map(([f, db], i) => `${i ? 'L' : 'M'}${plotX(f, width, lo, hi).toFixed(1)} ${plotY(db, height).toFixed(1)}`).join(' ');
}

const $ = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const when = t => new Date(t * 1000).toLocaleString('zh-CN', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
/** A version's name, or its date when it has none. */
export function profileLabel(profile) { return profile.name || `${when(profile.created)} 的方案`; }

async function api(path, options = {}) {
  const response = await fetch(`/ctrl/api/${path}`, { credentials: 'same-origin', cache: 'no-store', ...options });
  let body = {};
  try { body = await response.json(); } catch (_) { /* non-JSON proxy error */ }
  if (!response.ok) throw Error(body.error || `请求失败（${response.status}）`);
  return body;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

/** The plot drawn at the width it is shown at (text and lines keep their size); taller up to a limit. */
export function plotSize(width) {
  const W = Math.max(280, Math.round(width) || 360);
  return { W, H: Math.round(Math.min(280, Math.max(170, W * 0.42))) };
}

function plot(curves, { lo = F_MIN, hi = F_MAX, trusted = 0, width = 360 } = {}) {
  const { W, H } = plotSize(width);
  let html = trusted > lo ? `<rect class="untrusted" x="0" y="0" width="${plotX(trusted, W, lo, hi).toFixed(1)}" height="${H}"/>` : '';
  for (const db of [-15, -10, -5, 0, 5, 10, 15]) html += `<line class="grid${db ? '' : ' zero'}" x1="0" x2="${W}" y1="${plotY(db, H)}" y2="${plotY(db, H)}"/>${db % 10 === 0 ? `<text x="2" y="${plotY(db, H) - 2}">${db > 0 ? '+' : ''}${db}</text>` : ''}`;
  for (const [f, label] of [[20, '20'], [50, '50'], [100, '100'], [200, '200'], [350, '350'], [500, '500'], [1000, '1k'], [2000, '2k'], [5000, '5k'], [10000, '10k']]) {
    if (f >= lo && f <= hi) html += `<line class="grid" x1="${plotX(f, W, lo, hi)}" x2="${plotX(f, W, lo, hi)}" y1="0" y2="${H}"/><text x="${plotX(f, W, lo, hi) + 2}" y="${H + 12}">${label}</text>`;
  }
  for (const c of curves) html += `<path class="${c.cls}" d="${plotPath(c.f, c.db, W, H, lo, hi)}"/>`;
  return `<svg class="plot" viewBox="0 0 ${W} ${H + 16}" role="img" aria-label="频率响应">${html}</svg>`;
}

/** Top view: speakers above, five spots around the listening position. */
function roomMap(session, current) {
  const spots = { center: [170, 118], left: [118, 118], right: [222, 118], front: [170, 82], back: [170, 168] };
  let html = '<rect class="spk" x="60" y="14" width="34" height="24" rx="4"/><rect class="spk" x="246" y="14" width="34" height="24" rx="4"/>' +
    '<text x="62" y="52">左音箱</text><text x="248" y="52">右音箱</text>';
  for (const p of session.positions) {
    const [x, y] = spots[p.key], cls = p.key === current ? 'now' : p.status === 'accepted' ? 'done' : '';
    html += `<circle class="spot ${cls}" cx="${x}" cy="${y}" r="${p.key === 'center' ? 11 : 8}"/>`;
  }
  html += '<text x="160" y="146">中心</text><text x="98" y="192">每个点与中心相距约 30 cm（前方靠近音箱）</text>';
  return `<svg class="room-map" viewBox="0 0 340 200">${html}</svg>`;
}

function verdictHTML(v) {
  if (!v) return '';
  const label = { good: '✓ 好用', warn: '△ 可以用', bad: '✕ 需要重录' }[v.grade];
  return `<div class="verdict ${v.grade}"><b>${label}</b><div>${v.reasons.length ? `<ul>${v.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : '录音干净，电平合适。'}</div></div>`;
}

async function main() {
  const state = await api('state').catch(() => ({}));
  if (!(state.auth?.configured && state.auth?.logged_in)) {
    $('login').hidden = false;
    if (state.auth && !state.auth.configured) $('login-error').textContent = '这台主机不能验证控制密码（没有 PAM）';
    $('login-form').onsubmit = async event => {
      event.preventDefault();
      try { await post('login', { password: $('password').value }); location.reload(); }
      catch (error) { $('login-error').textContent = error.message; }
    };
    return;
  }

  let session = (await api('roomfit/session')).session, mics = [];
  // Page-only state: the last position's curves, the center's curves for the
  // comparison line, the level result under review, the apply result.
  let last = null, centerCurves = null, reviewLevel = false, applied = null, busy = false;
  try { mics = (await api('roomfit/mics')).mics; } catch (_) { /* uncompensated only */ }
  let mic = localStorage.getItem('hifimate-roomfit-mic') ?? (mics.some(m => m.id === 'iphone-15-pro-max') ? 'iphone-15-pro-max' : '');

  // ---- microphone (raw capture) ----
  let microphone = null;
  async function openMicrophone() {
    if (microphone) return microphone;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 } });
    const track = stream.getAudioTracks()[0], settings = track.getSettings?.() || {};
    const context = new AudioContext();
    await context.audioWorklet.addModule('/ctrl/roomfit/roomfit-worklet.js');
    const node = new AudioWorkletNode(context, 'hifimate-recorder');
    const sink = context.createGain(); sink.gain.value = 0;   // keep the graph pulling, silently
    context.createMediaStreamSource(stream).connect(node).connect(sink).connect(context.destination);
    microphone = { track, settings, context, chunks: null };
    node.port.onmessage = event => { if (microphone.chunks) microphone.chunks.push(event.data); };
    return microphone;
  }
  async function sweep(channel, position, level) {
    const m = await openMicrophone();
    await m.context.resume();
    m.chunks = [];
    await sleep(300);   // room noise before the sweep
    const device = { ua: navigator.userAgent.slice(0, 200), mic: m.track.label, rate: String(m.context.sampleRate),
      ...Object.fromEntries(Object.entries(m.settings).map(([k, v]) => [k, String(v)])) };
    const started = await post('roomfit/start', { channel, level_db: level, device, mic: session.mic, session: session.id, position });
    // + 3 s: MPD fetch and the arbitration handoff can delay the whole take.
    const total = started.total_s + 3, t0 = performance.now(), name = channel === 'L' ? '左' : '右';
    while ((performance.now() - t0) / 1000 < total) {
      const elapsed = (performance.now() - t0) / 1000;
      $('progress').querySelector('i').style.width = `${Math.min(100, elapsed / total * 100)}%`;
      $('progress-text').textContent = elapsed < started.lead_s ? `${name}声道：准备中…` : `${name}声道：扫频中，请保持安静`;
      await sleep(200);
    }
    const chunks = m.chunks; m.chunks = null;
    const samples = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
    let offset = 0; for (const c of chunks) { samples.set(c, offset); offset += c.length; }
    $('progress-text').textContent = '上传并分析…';
    return api(`roomfit/upload/${started.id}`, { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: encodeWav(samples, m.context.sampleRate) });
  }
  async function run(task) {
    if (busy) return;
    busy = true; $('error').textContent = ''; $('progress').hidden = false;
    document.querySelectorAll('#view button, #view select').forEach(b => { b.disabled = true; });
    let wakeLock = null;
    try { wakeLock = await navigator.wakeLock?.request('screen'); } catch (_) { /* optional */ }
    try { await task(); }
    catch (error) { $('error').textContent = error.name === 'NotAllowedError' ? '没有麦克风权限：在「设置 → Safari → 麦克风」中允许' : error.message; }
    finally { busy = false; $('progress').hidden = true; wakeLock?.release?.().catch(() => {}); render(); }
  }
  const compute = async () => { session = (await post('roomfit/session/compute', { session: session.id })).session; };

  // ---- views ----
  function stepper(step) {
    const keys = ['intro', 'level', ...(session ? session.positions.map(p => p.key) : ['center', 'left', 'right', 'front', 'back']), 'compute'];
    const at = step.view === 'position' ? keys.indexOf(step.key) : keys.indexOf(step.view);
    $('stepper').innerHTML = keys.map((_, i) => `<i class="${i < at ? 'done' : i === at ? 'now' : ''}"></i>`).join('');
  }
  function render() {
    const step = reviewLevel ? { view: 'level' } : nextStep(session);
    stepper(step);
    $('wizard').hidden = false;
    const view = $('view');
    if (step.view === 'intro') {
      view.innerHTML = `<h2>开始房间校正 <small>约 4 分钟</small></h2>
        <ul class="checklist">
          <li><b>1</b><span>保持房间安静：关掉空调、风扇、电视，测量时不要说话、走动。</span></li>
          <li><b>2</b><span>音量调到平时听音乐的水平。测量会中断正在播放的音乐。</span></li>
          <li><b>3</b><span>取下手机壳。用支架或一摞书让手机在<b>耳朵高度</b>、<b>底部麦克风朝向音箱</b>，测量时不要手持。</span></li>
          <li><b>4</b><span>依次在中心和周围 4 个点测量（至少 3 个），每个点约 35 秒，HiFiMate 自动判断录音是否可用。</span></li>
        </ul>
        <div class="row"><span>麦克风</span><select id="mic-profile"><option value="">不补偿</option>${mics.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join('')}</select></div>
        <div class="actions"><button class="primary" id="go">开始</button></div>`;
      $('mic-profile').value = mic;
      $('mic-profile').onchange = () => { mic = $('mic-profile').value; localStorage.setItem('hifimate-roomfit-mic', mic); };
      $('go').onclick = () => run(async () => { session = (await post('roomfit/session/new', { mic: mic || null })).session; last = centerCurves = applied = null; });
      return;
    }
    if (step.view === 'level') {
      const level = session.level;
      view.innerHTML = `<h2>第一步：自动设定电平</h2>
        <p class="sub">手机放在<b>中心位置</b>。HiFiMate 先播放一次左声道扫频（约 15 秒），按录到的音量自动选择扫频电平。</p>
        ${roomMap(session, 'center')}
        ${level ? `<div class="verdict ${level.enough ? 'good' : 'warn'}"><b>${level.enough ? '✓' : '△'}</b><div>扫频电平设为 <b>${level.level_db} dBFS</b>，预计信噪比 ${level.expected_snr_db} dB。${level.enough ? '' : '<br>音量偏小：请把 HiFi 音量调高一些再「重测」，也可以直接继续。'}</div></div>` : ''}
        <div class="actions">${level ? '<button id="again">重测</button>' : ''}<button class="primary" id="go">${level ? '继续' : '开始'}</button></div>`;
      const measureLevel = () => run(async () => {
        const r = await sweep('L', 'level', session.level_db || -24);
        session = r.session; reviewLevel = true;
      });
      $('go').onclick = level ? () => { reviewLevel = false; render(); } : measureLevel;
      if ($('again')) $('again').onclick = measureLevel;
      return;
    }
    if (step.view === 'position') {
      const p = session.positions.find(x => x.key === step.key), index = session.positions.indexOf(p) + 1;
      const verdict = combinedVerdict(p.verdicts), measured = p.status === 'measured';
      const curves = [];
      if (measured && last?.position === p.key) {
        for (const ch of ['L', 'R']) curves.push({ cls: ch, f: last[ch].frequency, db: last[ch].magnitude_db });
        if (p.key !== 'center' && centerCurves) for (const ch of ['L', 'R']) curves.push({ cls: `${ch} ref`, f: centerCurves[ch].frequency, db: centerCurves[ch].magnitude_db });
      }
      view.innerHTML = `<h2>位置 ${index} / ${session.positions.length}：${esc(p.label)}</h2>
        <p class="sub">${esc(p.hint)}</p>
        ${roomMap(session, p.key)}
        ${measured ? verdictHTML(verdict) : ''}
        ${curves.length ? plot(curves, { lo: 20, hi: 2000, trusted: last.L.trusted_from_hz, width: view.clientWidth }) + `<div class="legend"><span><i style="border-color:var(--l)"></i>左</span><span><i style="border-color:var(--r)"></i>右</span>${curves.length > 2 ? '<span><i style="border-color:var(--muted);border-top-style:dashed"></i>中心位置</span>' : ''}</div>` : ''}
        <div class="actions">${measured
          ? `<button id="redo">重录</button><button class="primary" id="accept"${verdict?.grade === 'bad' ? ' disabled' : ''}>接受</button>`
          : '<button class="primary" id="go">开始测量（约 35 秒）</button>'}</div>
        ${step.canFinish && !measured ? '<div class="actions"><button id="finish">跳过剩余位置，开始计算</button></div>' : ''}`;
      const measure = () => run(async () => {
        const results = {};
        for (const ch of ['L', 'R']) { results[ch] = await sweep(ch, p.key, session.level_db); session = results[ch].session; }
        last = { position: p.key, ...results };
        if (p.key === 'center') centerCurves = results;
      });
      if ($('go')) $('go').onclick = measure;
      if ($('redo')) $('redo').onclick = measure;
      if ($('accept')) $('accept').onclick = () => run(async () => { session = (await post('roomfit/session/accept', { session: session.id, position: p.key })).session; last = null; });
      if ($('finish')) $('finish').onclick = () => run(compute);
      return;
    }
    if (step.view === 'compute') {
      const c = session.computed;
      if (!c) { view.innerHTML = '<h2>计算中…</h2>'; run(compute); return; }
      const f = c.frequency, names = { L: '左', R: '右' };
      view.innerHTML = `<h2>计算完成 <small>${c.positions.length} 个位置平均</small></h2>
        <p class="sub">只削减房间造成的低频峰（35–350 Hz），不提升谷，不需要额外降低音量。</p>
        ${['L', 'R'].map(ch => `<h3 style="margin-top:12px;font-size:14px">${names[ch]}声道 · ${c.channels[ch].filters.length} 个滤波器</h3>
          ${plot([{ cls: 'target', f, db: c.target }, { cls: `${ch} before`, f, db: c.channels[ch].before }, { cls: ch, f, db: c.channels[ch].after }], { lo: 20, hi: 1000, width: view.clientWidth })}
          <div class="filters">${c.channels[ch].filters.map(x => `<span>·</span><b>${x.freq.toFixed(0)} Hz</b><span>${x.gain.toFixed(1)} dB</span><span>Q ${x.q.toFixed(1)}</span>`).join('') || '<span>无需校正</span>'}</div>`).join('')}
        <div class="legend"><span><i style="border-color:var(--muted);border-top-style:dashed"></i>目标</span><span><i style="border-color:var(--l);opacity:.4"></i>测量平均</span><span><i style="border-color:var(--l)"></i>校正后（预测）</span></div>
        <div class="actions"><button id="discard">放弃</button><button class="primary" id="apply">应用</button></div>`;
      $('discard').onclick = () => run(async () => { await post('roomfit/session/abandon', { session: session.id }); session = null; });
      $('apply').onclick = () => run(async () => {
        applied = await post('roomfit/session/apply', { session: session.id });
        session = null; await loadProfiles();
      });
    }
  }

  // ---- current correction: one button (manage versions) and 关闭 ----
  let profiles = { profiles: [], enabled: false, active: null };
  async function loadProfiles() {
    profiles = await api('roomfit/profiles');
    const active = profiles.profiles.find(x => x.active), on = profiles.enabled;
    $('status').hidden = false;
    $('status').innerHTML = `<div class="status-row"><button class="status-button" id="open-manage">
        <span class="sub">房间校正</span><b class="${on ? 'on' : ''}">${on ? (active ? `开 · ${esc(profileLabel(active))}` : '开') : '关'}</b><span class="chev">›</span></button>
      ${on ? '<button id="turn-off">关闭</button>' : ''}</div>
      ${applied ? `<div class="verdict good" style="margin-top:12px"><b>✓</b><div>已应用${applied.live ? '，正在播放的音乐已经生效' : '，下次播放时生效'}。</div></div>` : ''}`;
    $('open-manage').onclick = openManage;
    if ($('turn-off')) $('turn-off').onclick = async () => {
      try { await post('room', { enabled: false }); applied = null; await sleep(800); await loadProfiles(); }
      catch (error) { $('error').textContent = error.message; }
    };
    if ($('manage').open) renderManage();
  }
  function renderManage() {
    const list = profiles.profiles;
    $('manage-list').innerHTML = list.length ? list.map(x => {
      const using = x.active && profiles.enabled;
      return `<div class="profile"><div class="profile-name"><b>${esc(x.name || when(x.created))}</b>${using ? ' <span class="tag">使用中</span>' : ''}
          <div class="sub">${x.name ? `${when(x.created)} · ` : ''}${x.positions.length} 个位置 · 左 ${x.channels.L.length} / 右 ${x.channels.R.length} 个滤波器</div></div><span class="spacer"></span>
        ${using ? '' : `<button data-use="${x.id}" class="primary">使用</button>`}<button data-rename="${x.id}">重命名</button><button data-delete="${x.id}" class="danger-text">删除</button></div>`;
    }).join('') : '<p class="sub">还没有校正方案。完成一次下方的校正后会出现在这里。</p>';
    $('manage-list').querySelectorAll('[data-use]').forEach(b => b.onclick = async () => {
      try { await post('roomfit/profiles/activate', { id: b.dataset.use }); applied = null; await loadProfiles(); }
      catch (error) { $('error').textContent = error.message; }
    });
    $('manage-list').querySelectorAll('[data-rename]').forEach(b => b.onclick = async () => {
      const x = list.find(p => p.id === b.dataset.rename);
      const name = prompt('给这组方案起个名字（例如「沙发中间」「关窗帘」，最多 24 个字；留空恢复为日期）', x.name || '');
      if (name === null) return;
      try { await post('roomfit/profiles/rename', { id: x.id, name }); await loadProfiles(); }
      catch (error) { alert(error.message); }
    });
    $('manage-list').querySelectorAll('[data-delete]').forEach(b => b.onclick = async () => {
      const x = list.find(p => p.id === b.dataset.delete), using = x.active && profiles.enabled;
      if (!confirm(using ? `删除正在使用的「${profileLabel(x)}」？房间校正将关闭。` : `删除「${profileLabel(x)}」？`)) return;
      try { await post('roomfit/profiles/delete', { id: x.id }); applied = null; await loadProfiles(); }
      catch (error) { $('error').textContent = error.message; }
    });
  }
  function openManage() { renderManage(); $('manage').showModal(); }
  $('manage').addEventListener('click', event => {
    if (event.target.closest('[data-close]') || event.target === $('manage')) $('manage').close();   // × or the backdrop
  });
  // A new correction needs the microphone, i.e. HTTPS (the sheet also opens over HTTP from /ctrl).
  $('new-correction').onclick = () => {
    if (!window.isSecureContext) { location.href = `https://${location.hostname}/ctrl/roomfit/`; return; }
    $('manage').close();
    $('wizard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  $('manage').addEventListener('close', () => { if (location.hash === '#manage') history.replaceState(null, '', location.pathname); });

  await loadProfiles();
  if (location.hash === '#manage') openManage();   // from the /ctrl DSP card
  // Managing versions works over HTTP; only the microphone needs HTTPS.
  if (!window.isSecureContext) {
    $('setup').hidden = false;
    // The trust setting shows the CA's own name (older installs: "HiFi Local CA").
    api('roomfit/ca').then(ca => { if (ca.name) $('ca-name').textContent = ca.name; }).catch(() => {});
    $('https-link').href = `https://${location.hostname}/ctrl/roomfit/`;
    return;
  }
  render();
}

if (typeof document !== 'undefined') main().catch(error => { document.body.insertAdjacentHTML('beforeend', `<p class="error" style="padding:16px">${error.message}</p>`); });
