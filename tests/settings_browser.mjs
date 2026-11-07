import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync(new URL('../gui/settings.js', import.meta.url), 'utf8');
const s = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
// every DAC rate ticked means no restriction; otherwise the ticked ones, sorted
assert.deepEqual(s.ratesToSave([48000, 44100], [44100, 48000]), []);
assert.deepEqual(s.ratesToSave([96000, 44100, 44100], [44100, 48000, 96000]), [44100, 96000]);
assert.equal(s.idleChoice(300), 300); assert.equal(s.idleChoice(240), 300); assert.equal(s.idleChoice(0), 0); assert.equal(s.idleChoice(7200), 3600);
assert.equal(s.tidySource('  http://www.github.com/owner/repo.git/ '), 'https://github.com/owner/repo');
for (const ok of ['owner/repo', 'owner/repo@v1.2', 'https://gitee.com/a/b']) assert.ok(s.looksLikeSource(ok), ok);
for (const bad of ['repo', 'https://example.com/a/b', 'a/b c']) assert.ok(!s.looksLikeSource(bad), bad);
assert.equal(s.kHz(44100), '44.1 kHz'); assert.equal(s.kHz(768000), '768 kHz');
console.log('settings page helpers ok');
