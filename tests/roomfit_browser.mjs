import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync(new URL('../gui/roomfit.js', import.meta.url), 'utf8');
const { encodeWav, processingIssues, nextStep, combinedVerdict, profileLabel, plotX, plotY, plotPath, plotSize } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

// Lossless 32-bit PCM mono WAV with the recorded rate.
const wav = new DataView(encodeWav(new Float32Array([0, 0.5, -1, 1.5]), 48000));
const tag = offset => String.fromCharCode(...[0, 1, 2, 3].map(i => wav.getUint8(offset + i)));
assert.equal(tag(0), 'RIFF'); assert.equal(tag(8), 'WAVE'); assert.equal(tag(36), 'data');
assert.equal(wav.getUint16(20, true), 1); assert.equal(wav.getUint16(22, true), 1);
assert.equal(wav.getUint32(24, true), 48000); assert.equal(wav.getUint16(34, true), 32);
assert.equal(wav.getUint32(40, true), 16);
assert.deepEqual([0, 1, 2, 3].map(i => wav.getInt32(44 + i * 4, true)), [0, 1073741824, -2147483647, 2147483647]);

// Any processing iOS keeps on changes the measured response.
assert.deepEqual(processingIssues({ echoCancellation: false, noiseSuppression: true, autoGainControl: false }), ['noiseSuppression']);
assert.deepEqual(processingIssues({}), []);

// The wizard resumes where the server session stands.
const positions = keys => ['center', 'left', 'right', 'front', 'back'].map(key => ({ key, status: keys.includes(key) ? 'accepted' : 'todo' }));
assert.deepEqual(nextStep(null), { view: 'intro' });
assert.deepEqual(nextStep({ state: 'level', level_db: null, positions: positions([]) }), { view: 'level' });
assert.deepEqual(nextStep({ state: 'measure', level_db: -18, positions: positions([]) }), { view: 'position', key: 'center', canFinish: false });
assert.deepEqual(nextStep({ state: 'measure', level_db: -18, positions: positions(['center', 'left', 'right']) }), { view: 'position', key: 'front', canFinish: true });
assert.deepEqual(nextStep({ state: 'measure', level_db: -18, positions: positions(['center', 'left', 'right', 'front', 'back']) }), { view: 'compute' });
assert.deepEqual(nextStep({ state: 'computed', level_db: -18, positions: positions(['center']) }), { view: 'compute' });
// A position is as good as its worse channel.
assert.equal(combinedVerdict({}), null);
assert.deepEqual(combinedVerdict({ L: { grade: 'good', reasons: [] }, R: { grade: 'warn', reasons: ['a'] } }), { grade: 'warn', reasons: ['a'] });
assert.equal(combinedVerdict({ L: { grade: 'bad', reasons: ['x'] }, R: { grade: 'warn', reasons: ['x'] } }).reasons.length, 1);

// Drawn at its real width: a wide page gets a wider (not a magnified) plot, a bounded height.
assert.deepEqual(plotSize(358), { W: 358, H: 170 }); assert.deepEqual(plotSize(1046), { W: 1046, H: 280 });
assert.deepEqual(plotSize(0), { W: 360, H: 170 }); assert.equal(plotSize(100).W, 280);
assert.equal(plotX(20, 360), 0); assert.ok(Math.abs(plotX(20000, 360) - 360) < 1e-9);
assert.equal(plotY(20, 200), 0); assert.equal(plotY(-20, 200), 200); assert.equal(plotY(-99, 200), 200);
assert.ok(plotPath([20, 1000], [0, 0], 360, 200).startsWith('M0.0 100.0 L'));
assert.equal(plotPath([10, 20, 5000], [0, 0, 0], 360, 200, 20, 1000).split('L').length, 1);   // outside the range dropped
assert.equal(profileLabel({ name: '沙发中间', created: 0 }), '沙发中间');
assert.match(profileLabel({ created: 1792908228 }), / 的方案$/);
console.log('roomfit page helpers passed');
