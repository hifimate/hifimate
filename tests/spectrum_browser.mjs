import fs from 'node:fs';
import assert from 'node:assert/strict';
// The overlay imports ./nowplaying.js; inline it as a data: URL as well.
const shared = 'data:text/javascript;base64,' + fs.readFileSync(new URL('../gui/nowplaying.js', import.meta.url)).toString('base64');
const source = fs.readFileSync(process.argv[2] || new URL('../gui/spectrum-overlay.js', import.meta.url), 'utf8').replaceAll("from './nowplaying.js'", `from '${shared}'`);
const { OCTAVE_TICKS, barGap, mountHiFiMateSpectrum, analyserBands, finiteBins, displayBins, advanceBars, drawHiFiMateSpectrum, frequencyBandEdges, frequencyPosition, frequencyTicks, ledGeometry, plotGeometry, scaleFrequency, spectrumColor } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const listeners = new Map(), frames = new Map(), timers = new Map(), streams = [];
let time = 0, id = 0, lastPaint = null;
const octaveBandCount = (maximum, minimum = 10) => {
  const ratio = 2 ** (1 / 24);
  const anchor = 8.17579892;
  const centers = [];
  for (let center = anchor; center <= maximum && centers.length < 512; center *= ratio) {
    if (center >= minimum) centers.push(center);
  }
  return centers.length + (centers.at(-1) * Math.sqrt(ratio) < maximum ? 1 : 0);
};
let rectangles = [], lineToCalls = 0, strokeCalls = 0, lineDashCalls = 0;
const ctx = { setTransform() {}, beginPath() {}, moveTo() {}, lineTo() { lineToCalls++; }, stroke() { strokeCalls++; }, setLineDash() { lineDashCalls++; }, clearRect(x, y) { if (!x && !y) lastPaint = 'clear'; }, createLinearGradient() { return { addColorStop() {} }; }, fillRect(...args) { rectangles.push(args); lastPaint = 'draw'; } };
function element(tag) {
  const classes = new Set();
  return { tag, children: [], attrs: {}, style: {}, dataset: {}, classList: { toggle(name, value) { if (value === undefined ? !classes.has(name) : value) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); } },
    setAttribute(k, v) { this.attrs[k] = v; }, append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }, getBoundingClientRect() { return { width: 1000, height: 300 }; }, remove() {}, getContext() { return ctx; },
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; } };
}
const storage = new Map();
const doc = { hidden: false, body: element('body'), getElementById() { return null; }, createElement: element,
  addEventListener(name, fn) { listeners.set(name, fn); }, removeEventListener(name) { listeners.delete(name); },
  defaultView: { addEventListener() {}, removeEventListener() {}, setInterval() { return 0; }, clearInterval() {}, localStorage: {
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, value); }
  } } };
class Stream {
  constructor(url) { this.url = url; this.closed = false; streams.push(this); }
  close() { this.closed = true; }
}
const app = mountHiFiMateSpectrum({ document: doc, EventSource: Stream,
  requestFrame(fn) { const handle = ++id; frames.set(handle, fn); return handle; },
  cancelFrame(handle) { frames.delete(handle); }, now: () => time,
  setTimer(fn) { const handle = ++id; timers.set(handle, fn); return handle; },
  clearTimer(handle) { timers.delete(handle); } });
assert.equal(streams.length, 1);
assert.equal(streams[0].url, '/_hifimate/spectrum');
const panel = doc.body.children[0];
assert.equal(panel.id, 'hifimate-spectrum-overlay');
const scaleSelect = panel.children[2].children[1];
assert.equal(scaleSelect.value, 'log');
const tick = frequency => panel.children[4].children.find(node => Number(node.dataset.frequency) === frequency);
assert.equal(tick(31).textContent, '31'); assert.equal(tick(1000).textContent, '1k');
assert.equal(panel.children[3].width, 1000);
const current = () => streams.at(-1);
scaleSelect.value = 'bark'; scaleSelect.onchange();
assert.match(panel.children[3].attrs['aria-label'], /bark/);
assert.equal(storage.get('hifimate-spectrum-frequency-scale'), 'bark');
// The selector re-subscribes so the backend re-aggregates bands in Bark.
assert.equal(streams.length, 2);
assert.equal(streams[0].closed, true);
assert.equal(current().url, '/_hifimate/spectrum?frequencyScale=bark');
scaleSelect.value = 'log'; scaleSelect.onchange();
assert.equal(current().url, '/_hifimate/spectrum?frequencyScale=log');
const highRateBands = octaveBandCount(96000);
current().onmessage({ data: JSON.stringify({ origin: 'dsp-model', state: 'playing', rate: 192000, frequency_min: 10, frequency_max: 96000, bins: Array(highRateBands).fill(0.8) }) });
assert.match(panel.children[2].children[0].textContent, new RegExp(`${highRateBands} 频段`));
assert.equal(tick(64000).textContent, '64k'); assert.equal(tick(96000), undefined);
assert.ok(Math.abs(parseFloat(tick(1000).style.left) - (10 + frequencyPosition(1000, 10, 96000) * 980)) < 1e-9);
assert.equal(frames.size, 1);
let entry = [...frames.entries()][0]; frames.delete(entry[0]); entry[1]();
assert.equal(lastPaint, 'draw');
for (const scale of ['bark', 'linear', 'mel']) {
  scaleSelect.value = scale; scaleSelect.onchange();
  assert.equal(current().url, `/_hifimate/spectrum?frequencyScale=${scale}`);
  current().onmessage({ data: JSON.stringify({ origin: 'dsp-model', state: 'playing', rate: 48000,
    frequencyScale: scale, channelLayout: 'dual-combined', bins: Array(240).fill(0.6) }) });
  assert.match(panel.children[3].attrs['aria-label'], new RegExp(`${scale}.*240 频段`));
  for (const node of panel.children[4].children) {
    const x = 10 + frequencyPosition(Number(node.dataset.frequency), 10, 24000, scale) * 980;
    assert.ok(Math.abs(parseFloat(node.style.left) - x) < 1e-9);
  }
  // A resize keeps the received mode, even when no new frame arrives.
  listeners.get('fullscreenchange')();
  assert.match(panel.children[3].attrs['aria-label'], new RegExp(scale));
}
// A rolling downgrade/old server ignores the query: its frames are log, so the
// axis follows the received bands, even when the count is unchanged.
current().onmessage({ data: JSON.stringify({ origin: 'dsp-model', state: 'playing', rate: 48000,
  bins: Array(240).fill(0.6) }) });
assert.match(panel.children[3].attrs['aria-label'], /log.*240 频段/);
assert.ok(Math.abs(parseFloat(tick(1000).style.left) - (10 + frequencyPosition(1000, 10, 24000) * 980)) < 1e-9);
const beforeHide = streams.length;
doc.hidden = true; listeners.get('visibilitychange')();
assert.equal(current().closed, true);
assert.equal(frames.size, 0); assert.equal(timers.size, 0);
assert.equal(lastPaint, 'clear');
doc.hidden = false; listeners.get('visibilitychange')();
assert.equal(streams.length, beforeHide + 1);
assert.equal(current().url, '/_hifimate/spectrum?frequencyScale=mel');
current().onerror(); assert.equal(frames.size, 0);
current().onmessage({ data: '{broken' }); assert.equal(frames.size, 0);
panel.children[0].onclick();
assert.equal(current().closed, true);
panel.children[0].onclick();
assert.equal(streams.length, beforeHide + 2);
panel.children[1].onclick();
assert.equal(panel.classList.contains('hifimate-spectrum-fullscreen'), true);
panel.children[1].onclick();
assert.equal(panel.classList.contains('hifimate-spectrum-fullscreen'), false);
current().onmessage({ data: JSON.stringify({ origin: 'dsp-model', state: 'idle', rate: 48000, bins: Array(64).fill(1) }) });
entry = [...frames.entries()][0]; frames.delete(entry[0]); entry[1]();
assert.equal(frames.size, 0);
assert.equal(timers.size, 0);
assert.equal(finiteBins(Array(64).fill(Infinity))[0], 0);
{
  // audioMotion: -85..-25 dB in AnalyserNode units, which read a full-scale
  // sine 13.56 dB lower than our 0 dB calibration.
  const off = 20 * Math.log10(2 / 0.42), near = (a, b) => a.every((v, i) => Math.abs(v - b[i]) < 1e-9);
  assert.ok(near(displayBins([-85 + off, -55 + off, -25 + off, -120, 10], [0, 0, 0, 0, 0], 'db'), [0, .5, 1, 0, 1]));
  const lin = v => (10 ** (v / 20) - 10 ** (-85 / 20)) / (10 ** (-25 / 20) - 10 ** (-85 / 20));
  assert.ok(near(displayBins([-25 + off, -45 + off, -85 + off, 0], [0, 0, 0, 0], 'linear', 1), [1, lin(-45), 0, 1]));
  // Plain linear (boost 1) keeps music low: -30 dB here is ~12 % of the height.
  assert.ok(Math.abs(displayBins([-30], [0], 'linear', 1)[0] - lin(-30 - off)) < 1e-12 && displayBins([-30], [0], 'linear', 1)[0] < .13);
  // The fixed HiFi display is linear with a 2.0 boost.
  assert.equal(displayBins([-24], [0])[0], displayBins([-24], [0], 'linear', 2)[0]);
  // linearBoost exactly as audioMotion's _normalizedB: value and range both
  // take the b-th root. At b = 2, -24 dB is ~49 % (not clipped to 100 %).
  const am = (d, b) => Math.max(0, Math.min(1, ((10 ** ((d - off) / 20)) ** (1 / b) - 10 ** (-85 / 20))
    / (10 ** (-25 / 20) - 10 ** (-85 / 20)) ** (1 / b)));
  for (const [d, b] of [[-24, 2], [-50, 2], [-30, 3], [-6, 1.5]]) {
    assert.ok(Math.abs(displayBins([d], [0], 'linear', b)[0] - am(d, b)) < 1e-12);
  }
  assert.ok(Math.abs(displayBins([-24], [0], 'linear', 2)[0] - .49) < .01);
  assert.ok(displayBins([-50], [0], 'linear', 2)[0] > displayBins([-50], [0], 'linear', 1)[0]);
  assert.deepEqual(displayBins([-55 + off], [0], 'db', 3), [.5]);  // dB mode ignores the boost
}
// audioMotion-like smoothing uses the same time constant for attacks and
// releases, so a 500ms step response is independent of frame rate.
assert.ok(advanceBars([0], [0.8], 1000 / 60)[0] > 0);
let slow = [0.8], fast = [0.8];
for (let i = 0; i < 6; i++) slow = advanceBars(slow, [0], 1000 / 30);
for (let i = 0; i < 24; i++) fast = advanceBars(fast, [0], 1000 / 120);
assert.ok(Math.abs(slow[0] - fast[0]) < 1e-12);
assert.ok(slow[0] < 0.15);
let ages = [0];
let velocities = [0];
let firstPeak = drawHiFiMateSpectrum(panel.children[3], [1], [0], 1000 / 60,
  { peakAges: ages, peakVelocities: velocities });
assert.equal(firstPeak[0], 1);
for (let i = 0; i < 30; i++) {
  firstPeak = drawHiFiMateSpectrum(panel.children[3], [0], firstPeak, 1000 / 60,
    { peakAges: ages, peakVelocities: velocities });
}
assert.equal(firstPeak[0], 1); // 500ms hold
const gravityPeak = drawHiFiMateSpectrum(panel.children[3], [0], firstPeak, 1000 / 60,
  { peakAges: ages, peakVelocities: velocities });
const gravityStep = 3.8 * 1000 / (60 ** 2 * (300 - 12));
assert.ok(Math.abs(gravityPeak[0] - (1 - gravityStep)) < 1e-12);
for (let i = 0; i < 90; i++) {
  firstPeak = drawHiFiMateSpectrum(panel.children[3], [0], firstPeak, 1000 / 60,
    { peakAges: ages, peakVelocities: velocities });
}
assert.ok(firstPeak[0] < 0.01);
// A resize/axis redraw must not restart the 500ms hold or mutate gravity
// state; it is a render-only operation.
const redrawAges = [500], redrawVelocities = [-.1], redrawPeaks = [.6];
const redrawResult = drawHiFiMateSpectrum(panel.children[3], [.2], redrawPeaks, 0,
  { peakAges: redrawAges, peakVelocities: redrawVelocities, updatePeaks: false });
assert.deepEqual(redrawResult, redrawPeaks);
assert.deepEqual(redrawAges, [500]);
assert.deepEqual(redrawVelocities, [-.1]);
app.destroy(); assert.equal(current().closed, true);
assert.equal(frames.size, 0);
// Ticks use the same logarithmic frequency limits as the actual analysis bands.
for (const max of [22050, 24000, 96000, 384000]) {
  const edges = frequencyBandEdges(10, max);
  assert.equal(edges.length - 1, octaveBandCount(max));
  for (let i = 1; i + 1 < edges.length - 1; i++) assert.ok(Math.abs(edges[i + 1] / edges[i] - 2 ** (1 / 24)) < 1e-12);
  for (const width of [300, 640, 1800]) {
    const ticks = frequencyTicks(10, max, width);
    // audioMotion octave labels only, always including 1k, never past Nyquist.
    assert.ok(ticks.every(t => OCTAVE_TICKS.includes(t.frequency) && t.frequency <= max));
    assert.ok(ticks.some(t => t.frequency === 1000));
    if (width >= 640) assert.deepEqual(ticks.slice(0, 11).map(t => t.text),
      ['16', '31', '63', '125', '250', '500', '1k', '2k', '4k', '8k', '16k']);
    for (let i = 1; i < ticks.length; i++) {
      assert.ok(ticks[i].x - ticks[i - 1].x > (ticks[i].text.length + ticks[i - 1].text.length) * 3.2 + 8);
    }
  }
}
assert.ok(Math.abs(frequencyPosition(1000, 10, 24000) - .5917) < .0001);
assert.ok(Math.abs(frequencyPosition(1000, 10, 96000) - .5022) < .0001);
assert.ok(Math.abs(scaleFrequency(1000, 'bark') - 8.527432432432433) < 1e-12);
assert.equal(scaleFrequency(700, 'mel'), 1);
for (const scale of ['log', 'bark', 'linear', 'mel']) {
  for (const max of [22050, 24000, 96000, 384000]) {
    const edges = frequencyBandEdges(10, max, scale), count = edges.length - 1;
    assert.equal(edges[0], 10); assert.equal(edges.at(-1), max);
    if (scale !== 'log') assert.equal(count, 240);
    assert.ok(edges.slice(1).every((f, i) => f > edges[i]));
    assert.equal(frequencyPosition(10, 10, max, scale), 0);
    assert.equal(frequencyPosition(max, 10, max, scale), 1);
    if (scale !== 'log') {
      edges.forEach((f, i) => assert.ok(Math.abs(frequencyPosition(f, 10, max, scale) - i / count) < 1e-12));
    }
    for (const width of [300, 640, 1800]) {
      const ticks = frequencyTicks(10, max, width, scale);
      assert.ok(ticks.length && ticks.some(t => t.frequency === 1000));
      assert.ok(ticks.every(t => OCTAVE_TICKS.includes(t.frequency) && t.frequency <= max));
      for (let i = 1; i < ticks.length; i++) {
        assert.ok(ticks[i].x - ticks[i - 1].x > (ticks[i].text.length + ticks[i - 1].text.length) * 3.2 + 8);
      }
    }
    // Check the actual canvas bar rectangles: every display slot has the same
    // width, regardless of which axis projection is selected.
    rectangles = [];
    drawHiFiMateSpectrum(panel.children[3], Array(count).fill(.6), [], 0,
      { minimum: 10, maximum: max, frequencyScale: scale });
    const bars = rectangles.slice(0, count), plot = plotGeometry(1000, count);
    const expectedWidth = plot.step * .55;  // audioMotion: 45 % of each slot is gap
    bars.forEach(([x, , barWidth], i) => {
      assert.ok(Math.abs(barWidth - expectedWidth) < 1e-9);
      assert.ok(Math.abs(x - (plot.left + i * plot.step + (plot.step - barWidth) / 2)) < 1e-9);
      if (i) assert.ok(Math.abs(x - (bars[i - 1][0] + expectedWidth + (plot.step - expectedWidth))) < 1e-9);
    });
  }
}
assert.throws(() => frequencyBandEdges(10, 24000, 'invalid'), /frequencyScale/);
assert.equal(finiteBins(Array(270).fill(.7)).length, 270);
assert.equal(finiteBins(Array(64).fill(.7)).length, 64);
assert.equal(finiteBins(Array(192).fill(.7)).length, 192);
assert.equal(finiteBins(Array(100000).fill(1)).length, 270);
assert.equal(drawHiFiMateSpectrum(panel.children[3], Array(240).fill(.6)).length, 240);
// A view-axis change retains equal-width display slots; it only changes the
// tick coordinate system and does not reinterpret the received source bins.
const sourceEdges = frequencyBandEdges(10, 24000, 'log');
rectangles = [];
lineToCalls = strokeCalls = lineDashCalls = 0;
drawHiFiMateSpectrum(panel.children[3], Array(sourceEdges.length - 1).fill(.6), [], 0,
  { minimum: 10, maximum: 24000, sourceFrequencyScale: 'log', frequencyScale: 'bark' });
const sourcePlot = plotGeometry(1000, sourceEdges.length - 1);
const sourceGap = barGap(sourcePlot.step);
assert.ok(Math.abs(rectangles[0][0] - sourcePlot.left - sourceGap / 2) < 1e-9);
assert.ok(rectangles.slice(0, sourceEdges.length - 1).every(([, , width]) =>
  Math.abs(width - (sourcePlot.step - sourceGap)) < 1e-9));
assert.equal(strokeCalls, 1);
assert.equal(lineDashCalls, 0);
assert.ok(lineToCalls > 0); // grid lines remain; peak caps add no line path
assert.equal(spectrumColor(0, .5), spectrumColor(1, .5));
assert.equal(spectrumColor(.5, .5, 0), spectrumColor(.5, .5, .5));
assert.notEqual(spectrumColor(.5, .2), spectrumColor(.5, .8));
assert.equal(spectrumColor(0, 0), 'rgb(0, 224, 112)');
assert.equal(spectrumColor(0, 1), 'rgb(255, 48, 72)');
assert.equal(spectrumColor(.5, .5), spectrumColor(.5, .5));
// Square dots on an even grid: the LED is as tall as the lit column is wide,
// and rows and columns share one pitch, whatever the canvas height.
for (const [step, range, ratio] of [[3.6, 288, 1], [6, 700, 2], [2.4, 1400, 1.5], [4.6, 500, 3]]) {
  const led = ledGeometry(step, range, ratio);
  assert.ok(Math.abs(led.pitch * ratio - Math.round(led.pitch * ratio)) < 1e-9);  // whole device px
  assert.ok(Math.abs(led.pitch - step) <= .5 / ratio + 1e-9);
  assert.ok(Math.abs(led.height - (led.pitch - barGap(led.pitch))) < 1e-9 && Math.abs(led.space - barGap(led.pitch)) < 1e-9);
  assert.ok(led.count * led.pitch - led.space <= range + 1e-9 && (led.count + 1) * led.pitch - led.space > range);
}
rectangles = [];
drawHiFiMateSpectrum(panel.children[3], Array(270).fill(.5), [], 0);
{
  const led = ledGeometry(plotGeometry(1000, 270).step, 288, 1);
  const lit = rectangles.slice(270, 540);
  const litCount = Math.round(.5 * led.count);
  lit.forEach(([, y, , h]) => assert.ok(Math.abs(h - (litCount * led.pitch - led.space)) < 1e-9));
}
// Browser source: WebAudio's 1/N-scaled Blackman FFT reads a full-scale sine
// at 20*log10(0.42/2) dB; calibrated bands put it at 0 dBFS like the backend.
{
  const spectrum = new Float32Array(4096).fill(-200);
  const binHz = 48000 / 8192, bin = Math.round(1000 / binHz);
  spectrum[bin] = 20 * Math.log10(0.42 / 2);
  const edges = frequencyBandEdges(10, 24000, 'log');
  const bands = analyserBands(spectrum, 48000, edges);
  const loud = bands.indexOf(Math.max(...bands));
  assert.ok(Math.abs(bands[loud]) < 1e-6);
  assert.ok(edges[loud] <= bin * binHz && bin * binHz <= edges[loud + 1]);
  assert.ok(bands.every((v, i) => i === loud || v <= 0) && Math.min(...bands) === -90);
}
// The early tap records page outputs without changing any connection.
{
  const vm = await import('node:vm');
  class AudioNode { constructor(context) { this.context = context; this.links = []; }
    connect(target) { this.links.push(target); return target; } }
  class AudioDestinationNode extends AudioNode {}
  class MediaStreamAudioDestinationNode extends AudioNode {}
  const sandbox = { AudioNode, AudioDestinationNode, MediaStreamAudioDestinationNode };
  sandbox.window = sandbox;
  vm.runInNewContext(fs.readFileSync(new URL('../gui/spectrum-tap.js', import.meta.url), 'utf8'), sandbox);
  const tap = sandbox.__hifimateAudioTap, context = {};
  const gain = new AudioNode(context), destination = new AudioDestinationNode(context), mid = new AudioNode(context);
  let notified = 0; tap.subscribe(() => notified++);
  assert.equal(gain.connect(destination), destination);  // chaining preserved
  assert.deepEqual(gain.links, [destination]);
  mid.connect(gain); gain.connect(destination);
  assert.equal(tap.outputs.length, 1); assert.equal(notified, 1);
  const stream = new AudioNode(context); stream.connect(new MediaStreamAudioDestinationNode(context));
  assert.equal(tap.outputs.length, 2);
  tap.connect(gain, mid); assert.equal(tap.outputs.length, 2);  // overlay links are not outputs
}
// Music Assistant embedding: source selector, browser analysis, no backend SSE.
{
  const listeners = new Set(), outputs = [];
  let analyserLevel = -200;
  const context = { state: 'running', sampleRate: 48000, addEventListener() {},
    createAnalyser() { return { context, frequencyBinCount: 4096, set fftSize(v) { this.size = v; },
      getFloatFrequencyData(data) { data.fill(-200); data[171] = analyserLevel; } }; } };
  const tap = { outputs, connect(node, target) { node.links = [target]; },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); } };
  const before = streams.length;
  const embeddedApp = mountHiFiMateSpectrum({ document: doc, EventSource: Stream, tap, embedded: true,
    requestFrame(fn) { const handle = ++id; frames.set(handle, fn); return handle; },
    cancelFrame(handle) { frames.delete(handle); }, now: () => time,
    setTimer(fn) { const handle = ++id; timers.set(handle, fn); return handle; },
    clearTimer(handle) { timers.delete(handle); } });
  const embeddedPanel = doc.body.children.at(-1);
  assert.equal(embeddedPanel.classList.contains('hifimate-spectrum-embedded'), true);
  assert.equal(embeddedPanel.classList.contains('hifimate-spectrum-collapsed'), true);  // starts as a button
  assert.equal(streams.length, before);  // collapsed: no SSE
  const sourceSelect = embeddedPanel.children[2].children.find(c => c.attrs?.['aria-label'] === '频谱音源');
  assert.equal(sourceSelect.attrs['aria-label'], '频谱音源');
  sourceSelect.value = 'web'; sourceSelect.onchange();
  assert.equal(storage.get('hifimate-spectrum-source'), 'web');
  embeddedPanel.children[0].onclick();  // expand
  assert.equal(streams.length, before);  // browser source never opens the backend stream
  const run = () => { const entry = [...frames.entries()].at(-1); frames.delete(entry[0]); time += 20; entry[1](); };
  run();
  assert.match(embeddedPanel.children[2].children[0].textContent, /当前页面未在播放/);
  outputs.push({ node: {}, context }); analyserLevel = 20 * Math.log10(0.42 / 2) - 6;
  listeners.forEach(fn => fn());
  rectangles = []; run();
  assert.match(embeddedPanel.children[2].children[0].textContent, /本浏览器 · 48 kHz · \d+ 频段/);
  assert.ok(outputs[0].node.links.length === 1 && rectangles.length > 0 && frames.size === 1);
  sourceSelect.value = 'hifimate'; sourceSelect.onchange();
  assert.equal(streams.length, before + 1);
  assert.match(streams.at(-1).url, /^\/_hifimate\/spectrum\?frequencyScale=(log|bark|linear|mel)$/);
  embeddedApp.destroy(); assert.equal(listeners.size, 0); assert.equal(streams.at(-1).closed, true);
}
// Embedded floating window: drag, in-place expand/collapse, opacity, fullscreen.
{
  storage.delete('hifimate-spectrum-layout');
  const tap = { outputs: [], connect() {}, subscribe() { return () => {}; } };
  const win = mountHiFiMateSpectrum({ document: doc, EventSource: Stream, tap, embedded: true,
    requestFrame(fn) { const handle = ++id; frames.set(handle, fn); return handle; },
    cancelFrame(handle) { frames.delete(handle); }, now: () => time,
    setTimer(fn) { const handle = ++id; timers.set(handle, fn); return handle; },
    clearTimer(handle) { timers.delete(handle); } });
  const p = doc.body.children.at(-1), toggle = p.children[0];
  p.getBoundingClientRect = () => ({ left: parseFloat(p.style.left), top: parseFloat(p.style.top),
    width: p.style.width ? parseFloat(p.style.width) : 56, height: p.style.height ? parseFloat(p.style.height) : 34 });
  // Moves/releases are followed on the document (the pointer leaves the chip).
  const move = e => listeners.get('pointermove')?.(e), up = e => listeners.get('pointerup')?.(e);
  const at = (x, y, tagName = 'BUTTON') => ({ clientX: x, clientY: y, button: 0, pointerId: 1, target: { tagName } });
  // Default chip: bottom-right above MA's player bar (1280x800 fallback viewport).
  assert.deepEqual([p.style.left, p.style.top, p.style.width], ['1208px', '662px', '']);
  assert.equal(toggle.textContent, '频谱');
  p.onpointerdown(at(1220, 670)); move(at(920, 470)); up(at(920, 470));
  assert.deepEqual([p.style.left, p.style.top], ['908px', '462px']);
  // If the browser sends no click after the drag, the next real click still opens.
  [...timers.values()].at(-1)();
  assert.deepEqual(JSON.parse(storage.get('hifimate-spectrum-layout')).chip, { left: 908, top: 462 });
  p.onpointerdown(at(1000, 500)); move(at(990, 490)); up(at(990, 490));
  toggle.onclick();  // the click ending a drag does not open it
  assert.equal(p.classList.contains('hifimate-spectrum-collapsed'), true);
  p.onpointerdown(at(1000, 500)); move(at(1010, 510)); up(at(1010, 510));
  [...timers.values()].at(-1)();
  assert.deepEqual([p.style.left, p.style.top], ['908px', '462px']);
  let captured = 0; p.setPointerCapture = () => captured++; p.releasePointerCapture = () => {};
  p.onpointerdown(at(930, 470)); up(at(931, 471));  // tiny jitter is still a click
  // A plain click must not capture the pointer: capture retargets the click
  // to the panel in real browsers, and the chip would never open.
  assert.equal(captured, 0);
  toggle.onclick();
  // In place: a chip in the lower-right quadrant keeps its right/bottom edges.
  assert.equal(p.classList.contains('hifimate-spectrum-collapsed'), false);
  assert.deepEqual([p.style.left, p.style.top, p.style.width, p.style.height], ['228px', '16px', '736px', '480px']);   // taller: now playing and lyrics
  assert.equal(p.style.opacity, '0.92'); assert.equal(toggle.textContent, '收起');
  const opacity = p.children[2].children.find(c => c.attrs?.['aria-label'] === '面板不透明度');
  assert.equal(opacity.attrs['aria-label'], '面板不透明度');
  opacity.value = '0.5'; opacity.oninput();
  assert.equal(p.style.opacity, '0.5'); assert.equal(JSON.parse(storage.get('hifimate-spectrum-layout')).opacity, .5);
  p.onpointerdown(at(300, 150, 'SELECT')); move(at(0, 0)); up(at(0, 0));
  assert.equal(p.style.left, '228px');  // controls are not drag handles
  toggle.onclick();  // undragged: the chip returns exactly where it was
  assert.deepEqual([p.style.left, p.style.top, p.style.opacity], ['908px', '462px', '']);
  toggle.onclick(); p.onpointerdown(at(300, 150)); move(at(-5000, -5000)); up(at(-5000, -5000));
  assert.deepEqual([p.style.left, p.style.top], ['0px', '0px']);  // clamped into the viewport
  p.children[1].onclick(); await new Promise(resolve => setTimeout(resolve, 0));  // fallback fullscreen
  assert.equal(p.style.left, '');
  p.children[1].onclick(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual([p.style.left, p.style.top], ['0px', '0px']);
  // 收起 in full screen: leaves full screen and folds straight to the chip.
  p.children[1].onclick(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(p.classList.contains('hifimate-spectrum-fullscreen'), true);
  await toggle.onclick();      // the click that ends the drag above is swallowed
  assert.equal(p.classList.contains('hifimate-spectrum-fullscreen'), true);
  await toggle.onclick();
  assert.equal(p.classList.contains('hifimate-spectrum-fullscreen'), false);
  assert.equal(p.classList.contains('hifimate-spectrum-collapsed'), true);
  assert.equal(toggle.textContent, '频谱'); assert.notEqual(p.style.left, '');
  assert.equal(listeners.has('pointermove'), false);  // released after each drag
  win.destroy();
}
console.log('browser geometry, palette, dense bands, subscription and lifecycle behavior passed');

// Control-page mode: mount into a card, subscribe only while its tab is visible.
{
  const container = element('div'), before = streams.length, bodyCount = doc.body.children.length;
  const initialFrames = frames.size, initialTimers = timers.size;
  const inline = mountHiFiMateSpectrum({ document: doc, EventSource: Stream, container, visible: false,
    requestFrame(fn) { const handle = ++id; frames.set(handle, fn); return handle; },
    cancelFrame(handle) { frames.delete(handle); }, now: () => time,
    setTimer(fn) { const handle = ++id; timers.set(handle, fn); return handle; },
    clearTimer(handle) { timers.delete(handle); } });
  assert.equal(streams.length, before);
  assert.equal(container.children.length, 1);
  assert.equal(doc.body.children.length, bodyCount);
  assert.equal(container.children[0].children[0].hidden, true);
  inline.setVisible(true); assert.equal(streams.length, before + 1);
  inline.setVisible(false); assert.equal(streams.at(-1).closed, true);
  assert.equal(frames.size, initialFrames); assert.equal(timers.size, initialTimers);
  inline.destroy();
}

{
  // The animation settings are fixed: no controls, nothing read from or written to storage.
  const store = new Map([['hifimate-spectrum-release', '0.5'], ['hifimate-spectrum-peak-hold', '0'], ['hifimate-spectrum-scale-keep', 'x']]);
  const page = { hidden: false, body: element('body'), getElementById() { return null; }, createElement: element,
    addEventListener() {}, removeEventListener() {},
    defaultView: { addEventListener() {}, removeEventListener() {},
      localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) } } };
  const mounted = mountHiFiMateSpectrum({ document: page, EventSource: Stream, requestFrame: () => 1, cancelFrame() {},
    now: () => 0, setTimer: () => 1, clearTimer() {} });
  const controls = page.body.children[0].children[2].children;
  assert.ok(!controls.some(c => c.className === 'hifimate-spectrum-release' || ['幅度', '缓降', '峰值保持', '提升'].includes(c.attrs?.['aria-label'])));
  assert.deepEqual([...store.keys()], ['hifimate-spectrum-release', 'hifimate-spectrum-peak-hold', 'hifimate-spectrum-scale-keep']);
  assert.ok(Math.abs(advanceBars([0.9], [0.1], 1000 / 60, 0)[0] - 0.1) < 1e-12);  // no easing at 0
  mounted.destroy();
}
console.log('fixed animation settings passed');
