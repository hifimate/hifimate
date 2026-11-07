/* HiFiMate settings page: playback (config/settings.json), Music Assistant
provider subscriptions, and where things are. The server validates and
saves; each save that needs a restart is confirmed first. */

const IDLE = [0, 60, 300, 900, 1800, 3600];
export const kHz = r => `${(r / 1000).toLocaleString('en', { maximumFractionDigits: 1 })} kHz`;
/** The rates to save: [] (no restriction) when every rate the DAC has is ticked. */
export function ratesToSave(ticked, dacRates) {
  const set = [...new Set(ticked)].sort((a, b) => a - b);
  return dacRates.length && dacRates.every(r => set.includes(r)) ? [] : set;
}
/** The idle choice for a value: the nearest offered one. */
export const idleChoice = value => IDLE.reduce((best, v) => Math.abs(v - value) < Math.abs(best - value) ? v : best, IDLE[0]);
/** A source as the user typed it, tidied: owner/repo or a full repository URL. */
export function tidySource(text) {
  return text.trim().replace(/^https?:\/\/(www\.)?/i, 'https://').replace(/\/+$/, '').replace(/\.git$/i, '');
}
export function looksLikeSource(text) {
  return /^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9_.-]+(@[A-Za-z0-9_.\-/]+)?$/.test(text) ||
    /^https:\/\/(github\.com|gitee\.com|gitcode\.com)\/[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9_.-]+$/.test(text);
}

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
async function api(path, options = {}) {
  const response = await fetch(`/ctrl/api/${path}`, { credentials: 'same-origin', cache: 'no-store', ...options });
  let body = {};
  try { body = await response.json(); } catch (_) { /* non-JSON proxy error */ }
  if (!response.ok) throw Error(body.error || `请求失败（${response.status}）`);
  return body;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

function main() {
  let state = null, loggedIn = false, sources = [], removed = new Set();

  function toast(message, error = false) {
    const t = $('toast'); t.textContent = message; t.className = 'toast' + (error ? ' err' : ''); t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, error ? 6000 : 3000);
  }
  function confirm(title, text) {
    return new Promise(resolve => {
      $('confirm-title').textContent = title; $('confirm-text').textContent = text;
      const dialog = $('confirm-dialog');
      dialog.onclick = event => { const v = event.target.closest('[data-confirm]')?.dataset.confirm; if (v) { dialog.close(); resolve(v === 'yes'); } };
      dialog.showModal();
    });
  }

  async function load() {
    const [status, settings] = await Promise.all([api('state'), api('settings')]);
    loggedIn = Boolean(status.auth?.configured && status.auth?.logged_in);
    state = settings; removed = new Set();
    sources = settings.subscriptions.sources.map(s => ({ ...s, saved: true }));
    $('login').hidden = loggedIn;
    renderPlayback(); renderSources(); renderDevice();
    for (const el of document.querySelectorAll('.need-login')) el.disabled = !loggedIn;
    for (const el of document.querySelectorAll('#playback input,#playback select,#add-source,#prune')) el.disabled = !loggedIn;
  }

  // --- playback ---
  function renderPlayback() {
    const { effective, defaults, dac } = state.playback;
    $('fade').value = effective.fade_ms ?? defaults.fade_ms ?? 400; $('fade-out').textContent = `${$('fade').value} ms`;
    $('fade-default').textContent = defaults.fade_ms ?? 400;
    $('idle').value = String(idleChoice(effective.idle_timeout_s ?? 300));
    const dacRates = dac?.rates || [], allowed = effective.rates?.length ? effective.rates : dacRates;
    $('rates').innerHTML = dacRates.length ? dacRates.map(r => `<label><input type="checkbox" value="${r}"${allowed.includes(r) ? ' checked' : ''}${loggedIn ? '' : ' disabled'}>${kHz(r)}</label>`).join('')
      : '<span class="sub">还不知道当前 DAC 支持哪些采样率。</span>';
    $('rates-note').hidden = !dacRates.length;
  }
  $('fade').addEventListener('input', () => { $('fade-out').textContent = `${$('fade').value} ms`; });
  async function savePlayback(values) {
    if (!await confirm('保存播放设置', '保存后会重启仲裁服务，正在播放的音乐会中断几秒，需要在发送端重新播放。')) return;
    try {
      await post('settings/playback', { playback: values });
      toast('已保存，仲裁服务正在重启'); $('playback-error').textContent = '';
      setTimeout(() => load().catch(() => {}), 3000);
    } catch (error) { $('playback-error').textContent = error.message; }
  }

  // --- subscriptions ---
  function renderSources() {
    const subs = state.subscriptions;
    if (!subs.available) { $('sources').innerHTML = '<p class="sub">没有找到订阅器的设置（没有部署 Music Assistant？）。</p>'; return; }
    $('prune').checked = subs.prune_removed;
    $('sources').innerHTML = sources.length ? sources.map((s, i) => {
      const synced = s.synced_at ? new Date(s.synced_at).toLocaleString('zh-CN', { hour12: false }) : '';
      const meta = s.saved ? [s.ref ? `版本 ${s.ref}` : '还没有同步', synced && `同步于 ${synced}`].filter(Boolean).join(' · ') : '保存后同步';
      return `<div class="source${s.saved ? '' : ' new'}${removed.has(s.source) ? ' gone' : ''}"><b>${esc(s.source)}</b>
        <span class="meta">${esc(meta)}</span>
        <span class="providers">${s.providers.map(p => `<span>${esc(p)}</span>`).join('')}</span>
        <button type="button" data-remove="${i}"${loggedIn ? '' : ' disabled'}>${removed.has(s.source) ? '撤销' : '删除'}</button></div>`;
    }).join('') : '<p class="sub">没有订阅任何仓库。</p>';
  }
  $('sources').addEventListener('click', event => {
    const i = event.target.closest('[data-remove]')?.dataset.remove;
    if (i === undefined) return;
    const s = sources[Number(i)];
    if (!s.saved) sources.splice(Number(i), 1);
    else if (removed.has(s.source)) removed.delete(s.source); else removed.add(s.source);
    renderSources();
  });
  $('add-form').addEventListener('submit', event => {
    event.preventDefault();
    const text = tidySource($('add-source').value);
    if (!text) return;
    if (!looksLikeSource(text)) { $('subs-error').textContent = '写成 owner/repo，或 GitHub / Gitee / GitCode 的仓库地址'; return; }
    if (sources.some(s => s.source === text)) { $('subs-error').textContent = '已经订阅了这个仓库'; return; }
    sources.push({ source: text, providers: [], saved: false }); $('add-source').value = ''; $('subs-error').textContent = '';
    renderSources();
  });
  async function saveSources() {
    const list = sources.filter(s => !removed.has(s.source)).map(s => s.source);
    if (!await confirm('保存并同步音乐源', '会同步这些仓库，然后重启 Music Assistant（大约 1 分钟，期间 Music Assistant 的播放会中断）。第三方音乐源的代码会在 Music Assistant 里运行，请确认来源可信。')) return;
    try {
      await post('settings/subscriptions', { sources: list, prune_removed: $('prune').checked });
      toast('已保存，正在同步并重启 Music Assistant'); $('subs-error').textContent = '';
      setTimeout(() => load().catch(() => {}), 2000);
    } catch (error) { $('subs-error').textContent = error.message; }
  }

  // --- device ---
  function renderDevice() {
    const d = state.device, rows = [['设备名称', d.name], ['安装目录', d.prefix], ['音乐库', d.music],
      ['音乐共享', d.share ? '\\\\<主机IP>\\Music（用户 hifimate）' : d.system ? '没有共享' : null], ['Music Assistant 数据', d.ma_data]];
    $('device-info').innerHTML = rows.filter(([, v]) => v).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  }

  document.addEventListener('click', event => {
    const act = event.target.closest('[data-act]')?.dataset.act;
    if (act === 'playback-save') {
      const ticked = [...document.querySelectorAll('#rates input:checked')].map(i => Number(i.value));
      const dacRates = state.playback.dac?.rates || [];
      if (dacRates.length && !ticked.length) { $('playback-error').textContent = '至少要勾选一个采样率'; return; }
      savePlayback({ fade_ms: Number($('fade').value), idle_timeout_s: Number($('idle').value), ...(dacRates.length ? { rates: ratesToSave(ticked, dacRates) } : {}) });
    } else if (act === 'playback-default') savePlayback({});
    else if (act === 'subs-save') saveSources();
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await post('login', { password: $('password').value }); $('password').value = ''; await load(); }
    catch (error) { $('login-error').textContent = error.message; }
  });
  load().catch(error => toast(error.message, true));
}

if (typeof document !== 'undefined' && document.getElementById('playback')) main();
