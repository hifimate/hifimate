/* HiFiMate Control: renders observed state only; every action goes through /ctrl/api. */
import { mountNowPlaying, NP_ICON } from './nowplaying.js';
export { playTime, livePosition, IDLE_NOW, lyricAt, lyricKey, wordFill } from './nowplaying.js';
export const SOURCE_INFO = {
  dlna: ['DLNA', 'DLNA', 'dlna', 'upmpdcli · MPD 6600'],
  music_assistant_dlna: ['Music Assistant', 'MA', 'ma', 'MPD 6601'],
  airplay: ['AirPlay', 'AIR', 'air', 'shairport-sync'],
  bluetooth: ['Bluetooth', 'BT', 'bt', 'BlueZ · A2DP']
};
const LANE_BG = { dlna: '#18213a', ma: '#241c38', air: '#132a33', bt: '#173628' };
const UNIT_LABEL = { hifimate: '仲裁', 'hifimate-camilladsp': 'DSP', 'hifimate-mpd': 'DLNA MPD', 'hifimate-dlna': 'upmpdcli',
  'hifimate-music-mpd': 'MA MPD', 'hifimate-airplay': 'shairport', 'hifimate-airplay-net': '网卡跟随', 'hifimate-bluetooth': '别名 / 配对',
  'hifimate-ctrl': '本页', pipewire: '音频服务器', wireplumber: '会话管理', 'hifimate-spectrum': '频谱分析', 'music-assistant': '容器组' };
// Restarting these interrupts playback; the confirmation says so.
const AUDIO_UNITS = ['hifimate', 'hifimate-camilladsp', 'pipewire', 'wireplumber'];

export function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
export function duration(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const n = Math.max(0, Math.floor(seconds));
  return n >= 86400 ? `${Math.floor(n / 86400)} d ${Math.floor(n % 86400 / 3600)} h`
    : n >= 3600 ? `${Math.floor(n / 3600)} h ${Math.floor(n % 3600 / 60)} m`
      : n >= 60 ? `${Math.floor(n / 60)} m ${n % 60} s` : `${n} s`;
}
export function rate(value) { return Number(value) > 0 ? `${Number(value) / 1000} kHz` : '—'; }
export function sourceOf(identity) { return typeof identity === 'string' ? identity.split('/').at(-1) : ''; }
export function viewOf(hash) { const name = String(hash).replace(/^#/, ''); return ['overview', 'services', 'logs', 'spectrum'].includes(name) ? name : 'overview'; }
export function pairingSeconds(pair, now = Date.now() / 1000) { return pair?.active ? Math.max(0, Math.ceil(pair.until - now)) : 0; }
export function canControl(state) { return Boolean(state?.auth?.configured && state?.auth?.logged_in); }
export function canStop(unit) { return !['hifimate-ctrl', 'pipewire', 'wireplumber'].includes(unit); }
export function outputName(row) {
  const name = String(row?.name || row?.device || '');
  // "USB Example DAC at usb-…, high speed : USB Audio" → "Example DAC"
  return row?.kind === 'usb' ? name.replace(/^USB\s+/, '').replace(/\s+at\s+usb-.*$/, '') : name.split(' / ')[0];
}
/** Rounded orthogonal link: horizontal, vertical along the trunk, horizontal. */
export function orthogonalPath(x1, y1, x2, y2, trunk = (x1 + x2) / 2) {
  if (Math.abs(y2 - y1) < 1) return `M${x1} ${y1} H${x2}`;
  const d = Math.sign(y2 - y1), r = Math.min(12, Math.abs(y2 - y1) / 2, Math.abs(trunk - x1), Math.abs(x2 - trunk));
  return `M${x1} ${y1} H${trunk - r} Q${trunk} ${y1} ${trunk} ${y1 + d * r} V${y2 - d * r} Q${trunk} ${y2} ${trunk + r} ${y2} H${x2}`;
}

const esc = escapeHTML;
const kHz = v => Number(v) > 0 ? `${+(v / 1000).toFixed(1)}` : '';
const clock = t => t ? new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—';
const ICON = {
  restart: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M14.5 9A5.5 5.5 0 1 1 12.9 5.1M13 2v3.2H9.8"/></svg>',
  stop: '<svg viewBox="0 0 18 18"><rect x="4.5" y="4.5" width="9" height="9" rx="1.5" fill="currentColor"/></svg>',
  log: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 5h10M4 9h10M4 13h6"/></svg>',
  bt: '<svg width="11" height="13" viewBox="0 0 11 14" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M1.5 4l7 6-3.5 3V1l3.5 3-7 6"/></svg>',
  lock: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#8b93a5" stroke-width="1.4"><rect x="2.5" y="6.5" width="9" height="6" rx="1.5"/><path d="M4.5 6.5V4.5a2.5 2.5 0 0 1 5 0"/></svg>',
  unlock: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#46d6a4" stroke-width="1.4"><rect x="2.5" y="6.5" width="9" height="6" rx="1.5"/><path d="M4.5 6.5V4.5a2.5 2.5 0 0 1 4.9-.7"/></svg>'
};

export async function mountControl(doc = document, win = window) {
  const $ = id => doc.getElementById(id);
  const mobile = () => win.matchMedia('(max-width: 1099px)').matches;
  let state = {}, online = false, stream = null, spectrum = null, busy = false;
  let tab = 'events', unitFilter = '', errorsOnly = false, journal = [], journalAt = 0, sheetUnit = '';
  const painted = {};
  // Replace a region only when its markup changed: keeps focus/hover stable at 1 Hz.
  const paint = (id, html) => { if (painted[id] !== html) { painted[id] = html; $(id).innerHTML = html; } };

  const control = () => canControl(state) && online && !busy;
  const dis = allowed => control() && allowed ? '' : ' disabled';
  const owner = () => state.controller?.ready ? sourceOf(state.controller.owner) : '';
  const lane = key => (state.lanes || []).find(row => row.source === key) || {};
  const output = () => (state.outputs || []).find(row => row.selected);

  function toast(message, bad = false) {
    const t = $('toast'); t.textContent = message; t.hidden = false; t.classList.toggle('bad', bad);
    win.clearTimeout(toast.timer); toast.timer = win.setTimeout(() => { t.hidden = true; }, 5000);
  }
  async function api(path, body) {
    const response = await win.fetch(`/ctrl/api/${path}`, body === undefined ? { cache: 'no-store' } : {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    let result = {};
    try { result = await response.json(); } catch (_) { /* proxy error page */ }
    if (!response.ok) throw Error(result.error || `请求失败（${response.status}）`);
    return result;
  }
  function confirm(title, text, danger = true) {
    return new Promise(resolve => {
      const d = $('confirm-dialog'), ok = $('confirm-ok');
      $('confirm-title').textContent = title; $('confirm-text').textContent = text;
      ok.className = danger ? 'danger' : 'primary';
      const done = value => { d.close(); ok.onclick = null; d.onclose = null; resolve(value); };
      ok.onclick = () => done(true); d.onclose = () => resolve(false);
      d.showModal();
    });
  }
  async function act(path, body, label) {
    if (!control()) return;
    busy = true; render();
    try { const r = await api(path, body); toast(r.message || `${label} · 已接受`); }
    catch (error) { toast(error.message, true); }
    finally { busy = false; render(); }
  }

  /* ---------- header & now playing ---------- */
  function renderHeader() {
    const c = state.controller || {}, src = owner(), info = SOURCE_INFO[src], l = lane(src), out = output();
    const fmt = c.format ? `${kHz(c.format.rate)} kHz / ${c.format.bits}-bit` : '';
    const title = info ? `正在播放 · ${info[0]}` : c.candidate ? '正在切换…' : '空闲';
    const parts = [fmt + (l.codec ? ` · ${l.codec}` : ''), l.name, out ? `→ ${outputName(out)}` : ''].filter(Boolean);
    paint('now', `<span class="pulse${info ? ' on' : ''}"></span><b>${esc(title)}</b>` +
      parts.map(p => `<span class="sep"></span><span>${esc(p)}</span>`).join(''));
    paint('now-card', `<small>${info ? '正在播放' : '状态'}</small><div class="t"><span class="pulse${info ? ' on' : ''}"></span>${esc(info ? info[0] : title)}</div>` +
      `<div class="sub">${esc([l.name, fmt].filter(Boolean).join(' · ') || '没有输入在播放')}</div>` +
      `<div class="chain"><b>${esc(info ? info[1] : '—')}</b><i class="${info ? 'on' : ''}"></i><b>DSP</b><span>${esc(dspVolume())}</span><i class="${info ? 'on' : ''}"></i><b>${esc(out ? outputName(out) : '—')}</b></div>`);
    $('host').textContent = state.system?.host || '';
    const live = $('live');
    live.textContent = online ? '实时' : '重连中'; live.className = `chip ${online ? 'live' : 'down'}`;
    const auth = state.auth || {};
    const login = $('login');
    login.innerHTML = canControl(state) ? ICON.unlock : ICON.lock; login.classList.toggle('on', canControl(state));
    login.setAttribute('aria-label', canControl(state) ? '已登录，点击退出' : '登录以控制');
    $('hint').textContent = !state.auth ? '' : !auth.configured ? '这台主机不能验证控制密码（没有 PAM），目前只能查看。'
      : !online ? '与 HiFiMate 的连接中断，正在重连；控制暂时不可用。' : '';
  }
  function dspVolume() {
    const v = state.dsp?.volume;
    return state.dsp?.mute ? '静音' : Number.isFinite(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)} dB` : '';
  }

  /* ---------- signal path ---------- */
  function laneHTML(key) {
    const [name, icon, color, description] = SOURCE_INFO[key], l = lane(key), on = owner() === key;
    const wait = !on && l.has_data, unknown = l.available === false;
    const sub = on || wait ? [l.name, l.rate ? `${kHz(l.rate)} k${l.container ? ' / ' + l.container : ''}` : '', l.codec].filter(Boolean).join(' · ') || description : description;
    const label = unknown ? '状态未知' : on ? '当前' : wait ? '有数据' : '无数据';
    let button = '';
    if (wait) button = `<button class="btn-warn" data-switch="${key}"${dis(l.can_switch)} title="${l.can_switch ? '' : '该输入尚无可切换的会话'}">切换到此</button>`;
    else if (key === 'bluetooth' && !state.pairing?.active) button = `<button class="btn-bt" data-pair${dis(true)}>${ICON.bt}配对 120 s</button>`;
    const meter = `<span class="meter${on || wait ? ' on' : ''}" style="--lane:var(${on ? '--' + color : '--warn'})">${'<i></i>'.repeat(5)}</span>`;
    return `<div class="lane${on ? ' on' : wait ? ' wait' : ''}" data-lane="${key}">
      <span class="ic" style="background:${LANE_BG[color]};color:var(--${color})">${icon}</span>
      <span class="txt"><b>${esc(name)}</b><span class="sub">${esc(sub)}</span></span>
      <span class="side">${button || `<span class="state">${label}</span>`}${meter}</span></div>` +
      (key === 'bluetooth' && state.pairing?.active ? pairHTML() : '');
  }
  function pairHTML() {
    const left = pairingSeconds(state.pairing), c = 2 * Math.PI * 19;
    return `<div class="pair" data-lane-extra><svg class="ring" viewBox="0 0 44 44"><circle cx="22" cy="22" r="19" fill="none" stroke="#1d3a2f" stroke-width="3"/>
      <circle cx="22" cy="22" r="19" fill="none" stroke="#46d6a4" stroke-width="3" stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${(c * (1 - left / 120)).toFixed(1)}" transform="rotate(-90 22 22)"/>
      <text x="22" y="26.5" text-anchor="middle" fill="#e7eaf0" font-size="13" font-weight="700">${left}</text></svg>
      <span class="spacer"><b>可被发现 · 等待配对</b><span class="sub">在手机蓝牙中选择 “${esc(state.system?.name || 'HiFi')}” 并确认</span></span>
      <button data-pair-stop${dis(true)}>停止</button></div>`;
  }
  function renderPath() {
    paint('inputs', Object.keys(SOURCE_INFO).map(laneHTML).join(''));
    const src = owner(), c = state.controller || {}, info = SOURCE_INFO[src];
    paint('arbiter', `<small>仲裁</small><b>${esc(info ? info[0] : c.candidate ? '切换中' : '空闲')}</b>` +
      `<span class="sub">${esc(c.error ? '错误' : info ? (c.fade === 'released' ? '淡入完成' : c.fade === 'ramping' ? '淡入中' : '已静音') : c.candidate ? '验证格式…' : '等待输入')}</span>`);
    $('arbiter').classList.toggle('on', Boolean(info));
    const d = state.dsp || {}, running = d.state === 'Running', fmt = c.format;
    const badge = running ? 'ok' : d.state === 'Paused' ? 'warn' : '';
    paint('dsp', `<div class="top"><div><small>DSP</small><div class="name">CamillaDSP</div></div><span class="badge ${badge}">${esc(d.state || '未知')}</span></div>
      <dl><dt>采样率</dt><dd>${fmt ? `${fmt.rate.toLocaleString('en').replace(',', ' ')} Hz` : '—'}</dd>
      <dt>主音量</dt><dd>${esc(dspVolume() || '—')}</dd>
      <dt>负载</dt><dd>${Number.isFinite(d.load) ? d.load.toFixed(1) + ' %' : '—'}</dd>
      <dt>削波</dt><dd>${d.clipped ?? '—'}</dd></dl>${eqHTML()}${roomHTML()}`);
    // Stable order (USB DACs first): selecting an output never moves the cards.
    const outputs = (state.outputs || []).slice().sort((a, b) => (a.kind !== 'usb') - (b.kind !== 'usb') || a.device.localeCompare(b.device));
    paint('outputs', outputs.map(row => {
      const on = row.selected, cur = on && fmt ? fmt.rate : 0, name = outputName(row);
      const kind = row.kind === 'usb' ? '<span class="badge ok">USB</span>' : '<span class="badge">模拟</span>';
      const status = on ? (info ? `${(fmt.rate).toLocaleString('en').replace(',', ' ')} Hz · S32_LE · 输出中` : '已选择 · 空闲时关闭') : '未使用';
      return `<div class="out${on ? ' on' : ''}" data-output="${esc(row.device)}"><div class="row1"><span class="radio"></span><b>${esc(name)}</b>${kind}
        ${on ? `<span class="state">${info ? '● 输出中' : '● 已选择'}</span>` : `<span class="spacer"></span><button class="switch" data-output-switch="${esc(row.device)}"${dis(true)}>切换输出</button>`}</div>
        <span class="sub">${esc(row.device)} · ${esc(status)}</span>
        <div class="rates">${row.rates.map(r => `<span class="${r === cur ? 'cur' : ''}">${kHz(r)}</span>`).join('')}</div></div>`;
    }).join('') || '<p class="empty">没有可用的输出设备</p>');
    win.requestAnimationFrame(drawLinks);
  }
  // Personal EQ: count and A/B bypass from the arbiter; the row opens the EQ page.
  function eqHTML() {
    const filters = state.controller?.filters, bypassed = state.controller?.filters_bypassed;
    if (!filters) return '';
    const label = state.controller?.filters_error ? '文件有错误' : bypassed ? '旁路中' : filters.filters ? `${filters.filters} 个滤波器` : '未设置';
    return `<div class="room"><a href="/ctrl/eq/"><span>我的 EQ</span><b class="${filters.filters && !bypassed ? 'on' : ''}">${esc(label)}</b><i>›</i></a></div>`;
  }
  // Room correction (roomfit): status from the arbiter, switch needs login.
  function roomHTML() {
    const room = state.controller?.room;
    if (!room?.configured) return '';
    const count = (room.filters?.L || 0) + (room.filters?.R || 0);
    // The row opens version management on the roomfit page; only 关闭 lives here.
    return `<div class="room"><a href="/ctrl/roomfit/#manage"><span>房间校正</span><b class="${room.enabled ? 'on' : ''}">${room.enabled ? `开 · ${count} 个滤波器` : '关'}</b><i>›</i></a>` +
      (room.enabled ? `<button data-room="off"${dis(true)}>关闭</button>` : '') + '</div>';
  }
  function drawLinks() {
    const svg = $('links'), flow = $('flow');
    if (mobile() || !svg) return;
    const box = flow.getBoundingClientRect(), rel = el => { const r = el.getBoundingClientRect(); return { l: r.left - box.left, r: r.right - box.left, y: r.top - box.top + r.height / 2 }; };
    const arb = rel($('arbiter')), dsp = rel($('dsp')), src = owner(), paths = [];
    const inputs = [...flow.querySelectorAll('.lane')], outs = [...flow.querySelectorAll('.out')];
    const trunkIn = (rel(inputs[0] || $('arbiter')).r + arb.l) / 2;
    inputs.forEach(el => {
      const key = el.dataset.lane, a = rel(el), cls = key === src ? 'on' : lane(key).has_data ? 'wait' : 'off';
      paths.push([cls, orthogonalPath(a.r, a.y, arb.l, arb.y, trunkIn)]);
    });
    paths.push([src ? 'on' : 'off', `M${arb.r} ${arb.y} H${dsp.l}`]);
    const trunkOut = outs.length ? (dsp.r + rel(outs[0]).l) / 2 : 0;
    outs.forEach(el => { const o = rel(el); paths.push([el.classList.contains('on') && src ? 'on' : 'off', orthogonalPath(dsp.r, dsp.y, o.l, o.y, trunkOut)]); });
    // Draw active links last so they sit on top of the shared trunk.
    const order = { off: 0, wait: 1, on: 2 };
    paths.sort((a, b) => order[a[0]] - order[b[0]]);
    const html = paths.map(([cls, d]) => `<path class="${cls}" d="${d}"/>`).join('');
    if (painted.links !== html) { painted.links = html; svg.innerHTML = html; }
  }

  /* ---------- services ---------- */
  function svcState(row) { return row.state === 'active' ? 'ok' : ['activating', 'reloading', 'degraded'].includes(row.state) ? 'warn' : 'err'; }
  function renderServices() {
    const rows = state.services || [], groups = [...new Set(rows.map(r => r.group))];
    const running = rows.filter(r => r.state === 'active').length;
    $('svc-count').textContent = rows.length ? `${running} / ${rows.length} 运行` : '';
    $('svc-sum-count').textContent = rows.length ? `${running} / ${rows.length} 运行` : '';
    const buttons = row => `<div class="acts"><button class="act" title="重启" data-svc="${esc(row.id)}" data-op="restart"${dis(true)}>${ICON.restart}</button>` +
      (row.can_stop ? `<button class="act" title="停止" data-svc="${esc(row.id)}" data-op="stop"${dis(row.state === 'active')}>${ICON.stop}</button>` : '') + '</div>';
    const table = `<table class="svc-table"><tr><th>服务</th><th>运行时长</th><th>重启</th><th>CPU</th><th style="text-align:right">操作</th></tr>` +
      groups.map(g => `<tr class="group"><td colspan="5">${esc(g)}</td></tr>` + rows.filter(r => r.group === g).map(r =>
        `<tr class="svc" data-sheet="${esc(r.id)}"><td><span class="dot ${svcState(r)}"></span><span class="svc-name">${esc(r.id.replace(/^hifimate-/, ''))}</span> <span class="sub">· ${esc(UNIT_LABEL[r.id] || r.detail)}</span>${r.rt ? `<span class="rt">${esc(r.rt)}</span>` : ''}</td>
        <td>${r.state === 'active' ? duration(r.uptime) : esc(r.detail || r.state)}</td><td>${r.restarts ?? '—'}</td><td>${Number.isFinite(r.cpu) ? r.cpu.toFixed(1) + ' %' : '—'}</td><td>${buttons(r)}</td></tr>`).join('')).join('') + '</table>';
    const list = `<div class="svc-groups">` + groups.map(g => `<h3>${esc(g)}</h3><div class="card list">` + rows.filter(r => r.group === g).map(r =>
      `<div class="li" data-sheet="${esc(r.id)}"><span class="dot ${svcState(r)}"></span><div><b>${esc(r.id.replace(/^hifimate-/, ''))}${r.rt ? `<span class="rt">${esc(r.rt)}</span>` : ''}</b><span class="sub">${esc(UNIT_LABEL[r.id] || '')} · ${r.state === 'active' ? duration(r.uptime) : esc(r.state)}${Number.isFinite(r.cpu) ? ' · ' + r.cpu.toFixed(1) + ' %' : ''}</span></div><span class="chev" style="margin-left:auto">›</span></div>`).join('') + '</div>').join('') + '</div>';
    paint('services', `<div class="desktop">${table}</div><div class="mobile">${list}</div>`);
    paint('svc-summary', groups.map(g => {
      const items = rows.filter(r => r.group === g), up = items.filter(r => r.state === 'active').length, worst = items.some(r => svcState(r) === 'err') ? 'err' : items.some(r => r.restarts) || up < items.length ? 'warn' : 'ok';
      return `<a class="li" href="#services"><span class="dot ${worst}"></span><div><b>${esc(g)}</b><span class="sub">${esc(items.map(r => r.id.replace(/^hifimate-/, '')).join(' · '))}</span></div><span class="r">${up} / ${items.length}</span><span class="chev">›</span></a>`;
    }).join(''));
    if (sheetUnit && $('sheet').open) renderSheet();
  }
  function renderSheet() {
    const row = (state.services || []).find(r => r.id === sheetUnit); if (!row) return;
    $('sheet-dot').className = `dot ${svcState(row)}`;
    $('sheet-title').textContent = row.id === 'music-assistant' ? 'Music Assistant' : `${row.id}`;
    $('sheet-state').textContent = [`${row.state} (${row.detail})`, row.state === 'active' ? duration(row.uptime) : '', row.pid ? `PID ${row.pid}` : '', row.rt || '', `重启 ${row.restarts ?? 0} 次`].filter(Boolean).join(' · ');
    paint('sheet-actions', `<button data-svc="${esc(row.id)}" data-op="restart"${dis(true)}>${ICON.restart.replace('<svg', '<svg width="18" height="18"')}重启<span class="sub">${AUDIO_UNITS.includes(row.id) ? '会短暂中断播放' : row.id === 'hifimate-ctrl' ? '页面将自动重连' : ''}</span></button>` +
      (row.can_stop ? `<button class="red" data-svc="${esc(row.id)}" data-op="stop"${dis(row.state === 'active')}>${ICON.stop.replace('<svg', '<svg width="18" height="18"')}停止<span class="sub">${row.id === 'music-assistant' ? '网页入口也会关闭' : '需二次确认'}</span></button>` : '') +
      `<button data-full-log="${esc(row.id)}">${ICON.log.replace('<svg', '<svg width="18" height="18"')}完整日志<span class="sub">›</span></button>`);
  }
  async function openSheet(unit) {
    sheetUnit = unit; painted['sheet-actions'] = null; renderSheet();
    $('sheet-logs').textContent = '正在读取日志…'; $('sheet').showModal();
    try {
      const { logs } = await api(`logs?unit=${encodeURIComponent(unit)}&lines=12`);
      $('sheet-logs').textContent = logs.map(r => `${r.time ? clock(r.time) + ' ' : ''}${r.message}`).join('\n') || '没有日志';
    } catch (error) { $('sheet-logs').textContent = error.message; }
  }
  async function service(unit, op) {
    const name = unit === 'music-assistant' ? 'Music Assistant（4 个容器）' : unit;
    const why = op === 'stop' ? (unit === 'music-assistant' ? '停止后 MA 和本页的网页入口都会关闭，只能在主机上恢复。' : AUDIO_UNITS.includes(unit) ? '所有播放都会中断，直到重新启动。' : '对应的功能会停止，直到重新启动。')
      : AUDIO_UNITS.includes(unit) ? '播放会中断几秒。' : unit === 'music-assistant' ? '网页入口会断开约半分钟，之后自动重连。' : unit === 'hifimate-ctrl' ? '本页会自动重连。' : '对应的接收端会短暂断开。';
    if (await confirm(`${op === 'stop' ? '停止' : '重启'} ${name}？`, why, op === 'stop' || AUDIO_UNITS.includes(unit))) act('service', { unit, operation: op }, `${op === 'stop' ? '停止' : '重启'} ${unit}`);
  }

  /* ---------- events & logs ---------- */
  function eventView(row) {
    const names = s => String(s).replace(/\b(music_assistant_dlna|dlna|airplay|bluetooth)\b/g, k => SOURCE_INFO[k][0]);
    let title = names(row.message), sub = '', icon = ['·', '#1a1e27', 'var(--muted)'];
    if (row.kind === 'arbiter') {
      try {
        const e = JSON.parse(row.message), n = SOURCE_INFO[e.source]?.[0] || e.source;
        if (e.event === 'remote_pause') { title = `${n} ${e.status === 'confirmed' ? '已远程暂停' : '远程暂停 · ' + e.status}`; sub = e.detail || ''; icon = ['Ⅱ', '#1a1e27', 'var(--muted)']; }
        else { title = e.event; sub = Object.entries(e).filter(([k]) => k !== 'event').map(([k, v]) => `${k} ${v}`).join(' · '); }
      } catch (_) { /* plain line */ }
    } else if (row.kind === 'owner') {
      const m = /当前输入：(\S+)(.*)$/.exec(row.message) || [];
      const key = Object.keys(SOURCE_INFO).find(k => k === m[1]);
      title = key ? `切换到 ${SOURCE_INFO[key][0]}` : '输入空闲 · DSP 释放'; sub = (m[2] || '').replace(/^ · /, '');
      icon = key ? ['→', LANE_BG[SOURCE_INFO[key][2]], `var(--${SOURCE_INFO[key][2]})`] : ['◌', '#1a1e27', 'var(--muted)'];
    } else if (row.kind === 'service') {
      icon = ['⟳', '#16212b', '#8ec5ff'];
    } else if (row.kind === 'action') {
      const [what, result] = title.split(' · ');
      title = what; sub = `${result || ''} · 来自 /ctrl`; icon = ['✓', '#132a22', 'var(--bt)'];
    }
    if (row.priority <= 3) icon = ['!', '#2a1618', 'var(--err)'];
    else if (row.priority === 4) icon = ['!', '#2a2416', 'var(--warn)'];
    return { title, sub: [sub, row.kind === 'journal' ? row.unit.replace(/\.service$/, '') : ''].filter(Boolean).join(' · '), icon };
  }
  function renderEvents() {
    const rows = (state.events || []).slice().reverse();
    paint('events', rows.slice(0, 60).map(row => {
      const { title, sub, icon } = eventView(row);
      return `<div class="ev"><span class="tm">${clock(row.time)}</span><span class="ic" style="background:${icon[1]};color:${icon[2]}">${icon[0]}</span><div><b>${esc(title)}</b>${sub ? `<span class="sub">${esc(sub)}</span>` : ''}</div></div>`;
    }).join('') || '<p class="empty">暂无事件</p>');
    paint('recent', rows.slice(0, 4).map(row => {
      const { title, sub } = eventView(row);
      return `<div class="li"><span class="tm">${clock(row.time).slice(0, 8)}</span><div><b>${esc(title)}</b>${sub ? `<span class="sub">${esc(sub)}</span>` : ''}</div></div>`;
    }).join('') || '<p class="empty">暂无事件</p>');
  }
  function renderFilters() {
    const units = ['', ...(state.services || []).map(r => r.id)];
    paint('units', units.map(u => `<button data-unit="${esc(u)}" class="${u === unitFilter ? 'on' : ''}">${esc(u ? u.replace(/^hifimate-/, '') : '全部')}</button>`).join('') +
      `<button data-errors class="${errorsOnly ? 'on err' : ''}">只看错误</button>`);
  }
  function renderJournal() {
    const q = $('search').value.trim().toLowerCase();
    const rows = journal.filter(r => !q || r.message.toLowerCase().includes(q) || String(r.unit).toLowerCase().includes(q)).slice().reverse();
    paint('journal', rows.map(r => `<div class="ln p${r.priority}"><span class="tm">${clock(r.time)}</span><span class="u" style="color:${unitColor(r.unit)}">${esc(String(r.unit || '').replace(/\.service$/, '').replace(/^hifimate-/, ''))}</span><span class="m">${esc(r.message)}</span></div>`).join('') || '<p class="empty">没有匹配的日志</p>');
  }
  function unitColor(unit) {
    return /bluetooth/.test(unit) ? 'var(--bt)' : /airplay/.test(unit) ? 'var(--air)' : /music/.test(unit) ? 'var(--ma)' : /mpd|dlna/.test(unit) ? 'var(--dlna)' : /camilla/.test(unit) ? '#8ec5ff' : /^hifi(\.service)?$/.test(unit) ? 'var(--bt)' : 'var(--muted)';
  }
  async function loadJournal() {
    journalAt = Date.now();
    if (unitFilter === 'music-assistant' && errorsOnly) { journal = []; renderJournal(); return; }
    try {
      const q = new URLSearchParams({ lines: '200', ...(unitFilter ? { unit: unitFilter } : {}), ...(errorsOnly ? { errors: '1' } : {}) });
      journal = (await api(`logs?${q}`)).logs || [];
    } catch (error) { journal = [{ time: 0, unit: '', priority: 3, message: error.message }]; }
    renderJournal();
  }
  function setTab(next) {
    tab = next; if (tab === 'errors') { errorsOnly = true; } else if (tab === 'logs' && painted.tabSet !== 'logs') errorsOnly = false;
    painted.tabSet = tab;
    doc.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    const showJournal = tab !== 'events';
    $('filters').hidden = !showJournal; $('journal').hidden = !showJournal; $('events').hidden = showJournal;
    if (showJournal) { renderFilters(); loadJournal(); }
  }

  /* ---------- footer ---------- */
  function renderKpis() {
    const s = state.system || {}, rows = state.services || [], rt = rows.filter(r => r.rt).length;
    const mem = s.memory ? `${Math.round(s.memory.used / 1048576)} MB` : '—';
    paint('kpis', `<span>CPU<b>${Number.isFinite(s.cpu) ? s.cpu.toFixed(1) + ' %' : '—'}</b></span><span>内存<b>${mem}</b></span>` +
      `<span>内核<b>${esc(/-rt-/.test(s.kernel || '') ? 'PREEMPT_RT' : s.kernel || '—')}</b></span><span>实时调度<b>${rt} 个 RR</b></span>` +
      `<span>运行<b>${duration(s.uptime)}</b></span>${Object.keys(state.errors || {}).length ? `<span style="color:var(--warn)">采集失败<b style="color:var(--warn)">${esc(Object.keys(state.errors).join(', '))}</b></span>` : ''}` +
      `<span class="spacer"></span>${s.commit ? `<span>提交<b>${esc(s.commit.slice(0, 7))}${/dirty/.test(s.commit) ? '+' : ''}</b></span>` : ''}`);
    const fmt = state.controller?.format;
    $('spec-rate').textContent = fmt && owner() ? `${kHz(fmt.rate)} kHz` : '';
  }

  /* ---------- now playing (beside the spectrum) ---------- */
  // Now playing, transport and lyrics (shared with the spectrum panel in Music Assistant).
  const player = mountNowPlaying({ box: $('now-playing'), lyricBox: $('lyric'), win, toast,
    view: () => {
      const now = state.now, info = now ? SOURCE_INFO[now.source] || [now.source, '', 'dlna'] : null, c = now?.controls || {};
      return { now, time: state.time, label: info ? [info[0], `var(--${info[2]})`] : null,
        allowed: key => Boolean(c[key] && canControl(state) && online),
        hint: !canControl(state) ? '登录后可控制' : now?.reason || '' };
    },
    command: async (command, position) => {
      const r = await win.fetch('/ctrl/api/transport', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(position === undefined ? { command } : { command, position }) });
      if (!r.ok) throw Error((await r.json().catch(() => ({}))).error || `请求失败（${r.status}）`);
    },
    lyrics: () => api('lyrics') });
  const renderNow = () => player.render();

  function render() {
    renderHeader(); renderPath(); renderServices(); renderEvents(); renderKpis(); renderNow();
    if (tab !== 'events') renderFilters();
  }

  /* ---------- routing ---------- */
  function route() {
    const view = viewOf(win.location.hash);
    doc.body.dataset.view = view;
    doc.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('on', a.dataset.nav === view));
    if (mobile() && view === 'logs' && tab === 'events') setTab('logs');
    spectrum?.setVisible(!mobile() || view === 'spectrum');
    if (!mobile() && win.location.hash && view !== 'overview') doc.querySelector(`.view[data-view="${view}"]`)?.scrollIntoView({ block: 'start' });
    drawLinks();
  }

  /* ---------- events ---------- */
  doc.addEventListener('click', async event => {
    const t = event.target.closest('button,[data-sheet],[data-output]');
    if (!t) return;
    if (t.matches('[data-close]')) { t.closest('dialog').close(); return; }
    if (t.dataset.switch) return act('switch-input', { source: t.dataset.switch }, `切换到 ${SOURCE_INFO[t.dataset.switch][0]}`);
    if (t.matches('[data-pair]')) return act('pair', {}, '开启蓝牙配对');
    if (t.matches('[data-pair-stop]')) return act('pair-stop', {}, '停止蓝牙配对');
    if (t.dataset.room) return act('room', { enabled: t.dataset.room === 'on' }, `房间校正${t.dataset.room === 'on' ? '打开' : '关闭'}`);
    if (t.dataset.svc) { event.stopPropagation(); return service(t.dataset.svc, t.dataset.op); }
    if (t.dataset.fullLog !== undefined) {
      $('sheet').close(); unitFilter = t.dataset.fullLog; win.location.hash = '#logs'; setTab('logs'); return;
    }
    if (t.dataset.tab) return setTab(t.dataset.tab);
    if (t.dataset.unit !== undefined) { unitFilter = t.dataset.unit; renderFilters(); return loadJournal(); }
    if (t.matches('[data-errors]')) { errorsOnly = !errorsOnly; if (tab === 'errors' && !errorsOnly) setTab('logs'); renderFilters(); return loadJournal(); }
    const device = t.dataset.outputSwitch || (mobile() && t.dataset.output && !t.classList.contains('on') ? t.dataset.output : '');
    if (device) {
      const row = (state.outputs || []).find(r => r.device === device);
      if (!control()) { if (!canControl(state)) toast('请先登录'); return; }
      if (await confirm(`切换输出到 ${outputName(row)}？`, 'DSP 会用新设备重新打开输出，当前播放会中断几秒。', false)) act('switch-output', { device }, `切换输出到 ${outputName(row)}`);
      return;
    }
    if (t.dataset.sheet && !event.target.closest('button')) return openSheet(t.dataset.sheet);
  });
  $('login').onclick = async () => {
    if (canControl(state)) {
      if (await confirm('退出登录？', '退出后只能查看状态。', false)) { try { await api('logout', {}); } catch (_) { /* expired */ } refresh(); }
      return;
    }
    if (state.auth && !state.auth.configured) { toast('这台主机不能验证控制密码（没有 PAM）', true); return; }
    $('login-error').textContent = ''; $('login-dialog').showModal(); $('password').focus();
  };
  $('login-form').onsubmit = async event => {
    event.preventDefault();
    const submit = $('login-form').querySelector('[type=submit]'); submit.disabled = true;
    try { await api('login', { password: $('password').value }); $('password').value = ''; $('login-dialog').close(); toast('已登录'); await refresh(); }
    catch (error) { $('login-error').textContent = error.message; }
    finally { submit.disabled = false; }
  };
  $('search').oninput = renderJournal;

  async function refresh() { try { state = await api('state'); online = true; render(); } catch (_) { online = false; renderHeader(); } }
  function connect() {
    if (doc.hidden || stream) return;
    const current = stream = new win.EventSource('/ctrl/api/events');
    current.onmessage = event => {
      if (stream !== current) return;
      try { const next = JSON.parse(event.data); if (next.error) throw Error(next.error); state = next; online = true; }
      catch (_) { online = false; }
      render();
    };
    // EventSource reconnects by itself (e.g. after a ctrl or nginx restart).
    current.onerror = () => { online = false; renderHeader(); renderPath(); };
  }
  function disconnect() { stream?.close(); stream = null; }
  doc.addEventListener('visibilitychange', () => { if (doc.hidden) disconnect(); else connect(); });
  win.addEventListener('hashchange', route);
  win.addEventListener('resize', () => { painted.links = null; route(); });
  win.setInterval(() => {
    if (state.pairing?.active) { painted.inputs = null; renderPath(); }
    const view = viewOf(win.location.hash);
    if (!doc.hidden && tab !== 'events' && Date.now() - journalAt > 5000 && (!mobile() || view === 'logs')) loadJournal();
  }, 1000);

  await refresh();
  route(); connect();
  try {
    const { mountHiFiMateSpectrum } = await import('/_hifimate/spectrum-overlay.js?mount=manual');
    spectrum = mountHiFiMateSpectrum({ container: $('spectrum'), visible: !mobile() || viewOf(win.location.hash) === 'spectrum' });
  } catch (_) { $('spectrum').innerHTML = '<p class="empty">频谱暂不可用，请检查 hifimate-spectrum 服务。</p>'; }
}

if (typeof document !== 'undefined') {
  mountControl().catch(error => {
    const live = document.getElementById('live');
    if (live) live.textContent = '页面初始化失败，请刷新';
    console.error(error);
  });
}
