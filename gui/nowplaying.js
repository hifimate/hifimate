/* Now playing: title/artist/album, transport, progress and a synced lyric row.

Shared by /ctrl (served as /ctrl/nowplaying.js) and the spectrum panel in
Music Assistant's web UI (/_hifimate/nowplaying.js). The caller supplies the data
(view()) and the actions (command(), lyrics()); this module only renders and
keeps the position, the lyric line and the word fill moving between updates.
*/

/** m:ss (or h:mm:ss) for a position in seconds; '—' when unknown. */
export function playTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`;
}
/** Shown when nothing plays: the controls stay in place, disabled. */
export const IDLE_NOW = Object.freeze({ source: null, state: 'stop', title: null, position: null, duration: null, controls: {} });
/** The position now: the snapshot's, advanced by the time since it was taken while playing. */
export function livePosition(now, takenAt, nowSeconds = Date.now() / 1000) {
  if (!now || !Number.isFinite(now.position)) return null;
  const advanced = now.state === 'play' ? now.position + Math.max(0, nowSeconds - takenAt) : now.position;
  return Number.isFinite(now.duration) ? Math.min(now.duration, advanced) : advanced;
}
/** Which lyric line is sung at position (s): the last one started, a little early; -1 before the first. */
export function lyricAt(lines, position, lead = 0.2) {
  if (!lines?.length || !Number.isFinite(position)) return -1;
  let lo = 0, hi = lines.length - 1, at = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (lines[mid][0] <= position + lead) { at = mid; lo = mid + 1; } else hi = mid - 1; }
  return at;
}
/** How much of a word is sung at position: 0..1 (word = [start s, duration s, text]). */
export function wordFill(word, position) {
  if (!Number.isFinite(position) || position <= word[0]) return 0;
  return word[1] > 0 ? Math.min(1, (position - word[0]) / word[1]) : 1;
}
/** The track a lyrics answer belongs to (the server builds the same key). */
export const lyricKey = now => (now && now.title ? ['title', 'artist', 'album'].map(k => now[k] || '').join('|') : null);

export const NP_ICON = {
  prev: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M5 4h2v12H5zM16 4v12L8 10z"/></svg>',
  next: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M13 4h2v12h-2zM4 4v12l8-6z"/></svg>',
  play: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M6 3.5v13l11-6.5z"/></svg>',
  pause: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M5 3.5h3.5v13H5zM11.5 3.5H15v13h-3.5z"/></svg>'
};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

/**
 * Render into box (controls + progress) and lyricBox (one lyric row).
 * view() → { now, time, label: [text, color], allowed(key) → bool, hint, idleHint (why nothing is shown) }
 * command(name, position) → Promise (errors go to toast), lyrics(now) → Promise<{key, lines, reason}>.
 * Call render() whenever the data changes; destroy() stops the timers.
 */
export function mountNowPlaying({ box, lyricBox, view, command, lyrics, toast = () => {}, win = window }) {
  let seeking = false, lyric = { key: null, lines: [], reason: null }, lyricWant = null, lyricRetry = 0;
  let lyricLine = null, karaokeFrame = 0, painted = {}, destroyed = false;
  const paint = (el, key, html) => { if (painted[key] !== html) { painted[key] = html; el.innerHTML = html; } };
  const q = selector => box.querySelector(selector);
  const position = () => { const v = view(); return livePosition(v.now, v.time || Date.now() / 1000); };

  function render() {
    const v = view(), idle = !v.now, now = v.now || IDLE_NOW;
    const [label, color] = idle ? ['未播放', 'var(--np-faint)'] : v.label;
    const allowed = key => !idle && v.allowed(key);
    const title = now.title || (idle ? '没有在播放' : now.state === 'pause' ? '已暂停' : '正在播放');
    const meta = [now.artist, now.album].filter(Boolean).join(' · ');
    const hint = idle ? v.idleHint || '' : v.hint || '';
    paint(box, 'np', `<span class="np-src" style="color:${color}">${esc(label)}</span>
      <div class="np-text"><b title="${esc(title)}">${esc(title)}</b><span class="np-sub" title="${esc(meta)}">${esc(meta || hint)}</span></div>
      <div class="np-ctl">
        <button type="button" class="np-btn" data-transport="previous" aria-label="上一首"${allowed('previous') ? '' : ' disabled'}>${NP_ICON.prev}</button>
        <button type="button" class="np-btn main" data-transport="toggle" aria-label="${now.state === 'play' ? '暂停' : '播放'}"${allowed('toggle') ? '' : ' disabled'}>${now.state === 'play' ? NP_ICON.pause : NP_ICON.play}</button>
        <button type="button" class="np-btn" data-transport="next" aria-label="下一首"${allowed('next') ? '' : ' disabled'}>${NP_ICON.next}</button>
      </div>
      <div class="np-time"><span class="np-pos">—</span>
        <input class="np-seek" type="range" min="0" step="1" aria-label="播放进度"${allowed('seek') && Number.isFinite(now.duration) ? '' : ' disabled'}>
        <span>${playTime(now.duration)}</span></div>
      ${meta && hint ? `<p class="np-hint">${esc(hint)}</p>` : ''}`);
    tick(); tickLyric();
  }
  function tick() {
    const v = view(), now = v.now || IDLE_NOW, seek = q('.np-seek');
    if (!seek || seeking) return;
    const at = livePosition(now, v.time || Date.now() / 1000);
    q('.np-pos').textContent = playTime(at);
    seek.max = String(Math.max(1, Math.round(now.duration || 0)));
    seek.value = String(Math.round(at || 0));
    seek.style.setProperty('--fill', `${now.duration ? Math.min(100, (at || 0) / now.duration * 100) : 0}%`);
  }
  async function run(name, at) {
    try { await command(name, at); } catch (error) { toast(error.message, true); }
  }
  box.addEventListener('input', event => {
    if (!event.target.classList.contains('np-seek')) return;
    seeking = true;
    q('.np-pos').textContent = playTime(Number(event.target.value));
    event.target.style.setProperty('--fill', `${Number(event.target.value) / Number(event.target.max) * 100}%`);
  });
  box.addEventListener('change', async event => {
    if (!event.target.classList.contains('np-seek')) return;
    await run('seek', Number(event.target.value));
    seeking = false;
  });
  box.addEventListener('click', event => {
    const button = event.target.closest('[data-transport]');
    if (button && !button.disabled) run(button.dataset.transport);
  });

  // Lyrics: fetched once per track, synced to the live position.
  async function loadLyric(key, now) {
    lyricWant = key;
    try {
      const result = await lyrics(now);
      if (result && result.key === key) lyric = result;
      else lyricWant = null;                       // the track changed meanwhile: ask again
    } catch (_) { lyricWant = null; lyricRetry = Date.now() + 15000; }
    tickLyric();
  }
  function tickLyric() {
    if (!lyricBox) return;
    const v = view(), now = v.now, key = lyricKey(now);
    if (key && key !== lyric.key && key !== lyricWant && Date.now() >= lyricRetry && v.lyrics !== false) loadLyric(key, now);
    let html = '', line = null;
    if (key && v.lyrics === false) html = `<span class="none">${esc(v.lyricsReason || '暂无歌词')}</span>`;
    else if (key && lyric.key === key) {
      line = lyric.lines[lyricAt(lyric.lines, position())] || null;
      const tr = line && line[2] ? `<span class="tr">${esc(line[2])}</span>` : '';
      if (line && line[3]) html = `<span class="sung">${line[3].map(w => `<span class="w">${esc(w[2])}</span>`).join('')}</span>${tr}`;
      else if (line) html = line[1] ? `<span>${esc(line[1])}</span>${tr}` : '<span class="none">♪</span>';
      else html = `<span class="none">${esc(lyric.lines.length ? '♪' : lyric.reason || '暂无歌词')}</span>`;
    }
    paint(lyricBox, 'lyric', html);
    lyricLine = line && line[3] && now.state === 'play' ? line : null;
    if (line && line[3]) karaoke();                     // paused or new line: draw once now
    if (lyricLine && !karaokeFrame && !destroyed) karaokeFrame = win.requestAnimationFrame(karaoke);
  }
  // Word-timed lines fill word by word at display rate (the 250 ms tick only picks the line).
  function karaoke() {
    karaokeFrame = 0;
    const line = lyricLine || (lyric.lines[lyricAt(lyric.lines, position())] || null);
    const words = lyricBox.querySelectorAll('.w');
    if (!line || !line[3] || words.length !== line[3].length) return;
    const at = position();
    line[3].forEach((word, i) => {
      const fill = `${Math.round(wordFill(word, at) * 100)}%`;
      if (words[i].style.getPropertyValue('--p') !== fill) words[i].style.setProperty('--p', fill);
    });
    if (lyricLine && !destroyed) karaokeFrame = win.requestAnimationFrame(karaoke);
  }
  const timers = [win.setInterval(tick, 500), win.setInterval(tickLyric, 250)];
  return {
    render,
    /** Forget the shown lyrics (another source with the same track may answer differently). */
    resetLyrics() { lyric = { key: null, lines: [], reason: null }; lyricWant = null; lyricRetry = 0; painted.lyric = null; },
    destroy() { destroyed = true; timers.forEach(t => win.clearInterval(t)); if (karaokeFrame) win.cancelAnimationFrame(karaokeFrame); }
  };
}
