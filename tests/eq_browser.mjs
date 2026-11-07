import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync(new URL('../gui/eq.js', import.meta.url), 'utf8');
const eq = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

// The file's form <-> rows, one row per (step, name); a name used twice shares its definition.
const config = {
  filters: {
    bass: { type: 'Biquad', parameters: { type: 'Peaking', freq: 52, gain: 2.96, q: 1.7 } },
    air: { type: 'Biquad', parameters: { type: 'Highshelf', freq: 8000, gain: 2, q: 0.7 } },
    fir: { type: 'Conv', parameters: { type: 'Raw', filename: '/x.raw' } },
  },
  pipeline: [
    { type: 'Filter', channels: [0, 1], names: ['bass', 'air'] },
    { type: 'Filter', channels: [1], names: ['fir'], bypassed: true },
    { type: 'Filter', channels: [0], names: ['bass'] },
  ],
};
const rows = eq.rowsFromConfig(config);
assert.deepEqual(rows.map(r => [r.name, r.channels, r.bypassed]), [['bass', [0, 1], false], ['air', [0, 1], false], ['fir', [1], true], ['bass', [0], false]]);
assert.equal(rows[0].def, rows[3].def);                        // shared
rows[0].def.parameters.gain = 3;
const back = eq.configFromRows(rows);
assert.equal(back.filters.bass.parameters.gain, 3);
assert.deepEqual(back.pipeline.map(s => s.names), [['bass'], ['air'], ['fir'], ['bass']]);
assert.equal(back.pipeline[2].bypassed, true);
assert.equal(config.filters.bass.parameters.gain, 2.96);        // rows never alias the input

// What the list can edit, and what only the text mode can.
assert.equal(eq.kindOf(config.filters.bass), 'Peaking');
assert.equal(eq.kindOf(config.filters.fir), null);
assert.equal(eq.kindOf({ type: 'Biquad', parameters: { type: 'Lowshelf', freq: 80, gain: 3, slope: 6 } }), 'Lowshelf');
assert.equal(eq.kindOf({ type: 'Biquad', parameters: { type: 'Peaking', freq: 80, gain: 3, bandwidth: 1 } }), null);
assert.equal(eq.kindOf({ type: 'Biquad', parameters: { type: 'LinkwitzTransform', freq_act: 50 } }), null);
assert.equal(eq.kindOf({ type: 'Gain', parameters: { gain: -3 } }), 'Gain');
assert.equal(eq.kindOf({ type: 'Gain', parameters: { gain: -3, inverted: true } }), null);
assert.equal(eq.kindOf({ type: 'Delay', parameters: { delay: 2, unit: 'ms' } }), 'Delay');
assert.equal(eq.kindOf({ type: 'Delay', parameters: { delay: 2, unit: 'samples' } }), null);

// The text the page shows is the file's shape and parses back to the same.
const text = eq.renderConfig(back);
assert.match(text, /^filters:\n  bass: \{"type":"Biquad"/);
assert.match(text, /\n  - \{"type":"Filter","channels":\[1\],"names":\["fir"\],"bypassed":true\}\n/);
assert.equal(eq.renderConfig({ filters: {}, pipeline: [] }), 'filters: {}\npipeline: []\n');

// New and imported names never collide.
assert.equal(eq.freshName([{ name: 'eq_1' }, { name: 'eq_3' }]), 'eq_2');
const merged = eq.mergeImported([{ name: 'eq_1', def: {}, channels: [0, 1] }], {
  filters: { eq_1: { type: 'Gain', parameters: { gain: -6 } }, eq_2: { type: 'Gain', parameters: { gain: 1 } } },
  pipeline: [{ type: 'Filter', channels: [0, 1], names: ['eq_1'] }, { type: 'Filter', channels: [0], names: ['eq_2'] }],
});
assert.deepEqual(merged.map(r => r.name), ['eq_1', 'eq_2', 'eq_3']);
assert.equal(merged[1].def.parameters.gain, -6);

// Changing a kind keeps what still applies.
const peak = eq.newDef('Peaking', 123.456, 4.04);
assert.deepEqual(peak.parameters, { type: 'Peaking', freq: 123, gain: 4, q: 1 });
assert.deepEqual(eq.changeKind(peak, 'Highpass').parameters, { type: 'Highpass', freq: 123, q: 1 });
assert.deepEqual(eq.changeKind(peak, 'Notch').parameters.q, 10);
assert.deepEqual(eq.changeKind(peak, 'Gain'), { type: 'Gain', parameters: { gain: 4 } });

// Frequencies like a hardware EQ: 3 significant figures, in range.
assert.equal(eq.roundFreq(52.34), 52.3);
assert.equal(eq.roundFreq(184.4), 184);
assert.equal(eq.roundFreq(8047), 8050);
assert.equal(eq.roundFreq(1), 5);
assert.equal(eq.roundFreq(99999), 24000);

// Plot geometry round-trips; the range fits the curves.
const g = eq.geometry(1000, 340, 12);
for (const f of [20, 100, 1000, 20000]) assert.ok(Math.abs(g.f(g.x(f)) - f) / f < 1e-9);
for (const db of [-12, -3, 0, 7.5, 12]) assert.ok(Math.abs(g.db(g.y(db)) - db) < 1e-9);
assert.equal(g.x(20), 38); assert.equal(g.x(20000), 990);
assert.equal(eq.rangeFor([1, -9.5]), 12); assert.equal(eq.rangeFor([15]), 18); assert.equal(eq.rangeFor([30]), 24);
assert.equal(eq.curveAt([100, 1000], [0, 10], Math.sqrt(100 * 1000)), 5);
assert.equal(eq.curveAt([100, 1000], [0, 10], 50), 0);
assert.equal(eq.curveAt([100, 1000], [0, 10], 5000), 10);
assert.match(eq.curvePath(g, [10, 20, 1000, 30000], [0, 1, 2, 3]), /^M38\.0 /);
assert.equal(eq.channelKey([0, 1]), 'both'); assert.equal(eq.channelKey([1]), 'R');
// names: what the file accepts (user_filters.NAME)
for (const ok of ['bass_cut', 'speaker-1', 'a.b+c', 'X']) assert.ok(eq.NAME.test(ok), ok);
for (const bad of ['', '-x', 'with space', 'é', 'x'.repeat(49)]) assert.ok(!eq.NAME.test(bad), bad);
console.log('eq page helpers ok');
