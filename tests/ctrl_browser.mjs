import fs from 'node:fs';
import assert from 'node:assert/strict';
// ctrl.js imports ./nowplaying.js; a data: URL module cannot resolve it, so inline it the same way.
const shared = 'data:text/javascript;base64,' + fs.readFileSync(new URL('../gui/nowplaying.js', import.meta.url)).toString('base64');
const source = fs.readFileSync(new URL('../gui/ctrl.js', import.meta.url), 'utf8').replaceAll("from './nowplaying.js'", `from '${shared}'`);
const { escapeHTML, duration, rate, sourceOf, viewOf, pairingSeconds, canControl, canStop, orthogonalPath, playTime, livePosition, IDLE_NOW, lyricAt, lyricKey, wordFill } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
assert.equal(escapeHTML('<img onerror="bad">&'), '&lt;img onerror=&quot;bad&quot;&gt;&amp;');
assert.equal(duration(90061), '1 d 1 h');
assert.equal(duration(null), '—');
assert.equal(rate(192000), '192 kHz');
assert.equal(sourceOf('epoch/3/music_assistant_dlna'), 'music_assistant_dlna');
assert.equal(sourceOf(null), '');
for (const tab of ['overview', 'services', 'logs', 'spectrum']) assert.equal(viewOf('#' + tab), tab);
assert.equal(viewOf('#<script>'), 'overview');
assert.equal(pairingSeconds({ active: true, until: 140 }, 100), 40);
assert.equal(pairingSeconds({ active: true, until: 90 }, 100), 0);
assert.equal(canControl({ auth: { configured: true, logged_in: false } }), false);
assert.equal(canControl({ auth: { configured: true, logged_in: true } }), true);
for (const unit of ['hifimate-ctrl', 'pipewire', 'wireplumber']) assert.equal(canStop(unit), false);
assert.equal(canStop('hifimate'), true);
assert.equal(orthogonalPath(10, 20, 100, 20), 'M10 20 H100');
for (const y of [0, 60]) {
  const path = orthogonalPath(10, 20, 100, y);
  assert.ok(path.includes(' Q') && path.includes(' V') && !path.includes(' L'));
  assert.ok(!path.includes('NaN'));
}
assert.equal(playTime(0), '0:00'); assert.equal(playTime(61.9), '1:01'); assert.equal(playTime(3725), '1:02:05');
assert.equal(playTime(null), '—'); assert.equal(playTime(-1), '—');
// Playing: the position advances from the snapshot; paused or at the end it does not.
assert.equal(livePosition({ state: 'play', position: 10, duration: 100 }, 1000, 1004.5), 14.5);
assert.equal(livePosition({ state: 'pause', position: 10, duration: 100 }, 1000, 1060), 10);
assert.equal(livePosition({ state: 'play', position: 98, duration: 100 }, 1000, 1010), 100);
assert.equal(livePosition({ state: 'play', position: null }, 1000, 1010), null);
// Nothing playing: the block keeps its controls, all off, with an empty progress.
assert.equal(livePosition(IDLE_NOW, 1000, 1010), null);
assert.ok(Object.values(IDLE_NOW.controls).every(on => !on) && !IDLE_NOW.controls.toggle);
// Lyrics: the line started last (slightly early), -1 before the first or without lines.
const lines = [[0, 'a'], [21.96, 'b'], [27.5, 'c']];
assert.equal(lyricAt(lines, 0), 0); assert.equal(lyricAt(lines, 21.8), 1); assert.equal(lyricAt(lines, 21.7), 0);
assert.equal(lyricAt(lines, 999), 2); assert.equal(lyricAt([[5, 'x']], 1), -1);
assert.equal(lyricAt([], 3), -1); assert.equal(lyricAt(lines, null), -1);
assert.equal(lyricKey({ title: '富士山下', artist: '陈奕迅', album: null }), '富士山下|陈奕迅|');
assert.equal(lyricKey({ title: null }), null); assert.equal(lyricKey(null), null);
// Word fill: none before the word, linear through it, full after; zero-length words are instant.
assert.equal(wordFill([20.57, 0.36, '初'], 20.5), 0); assert.ok(Math.abs(wordFill([20.57, 0.36, '初'], 20.75) - 0.5) < 1e-9);
assert.equal(wordFill([20.57, 0.36, '初'], 30), 1); assert.equal(wordFill([91.32, 0, '、'], 91.33), 1);
assert.equal(wordFill([1, 1, 'x'], null), 0);
console.log('ctrl browser helpers passed');
