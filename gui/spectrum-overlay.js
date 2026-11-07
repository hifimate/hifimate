/* Actual post-DSP output from the local spectrum server (/_hifimate/spectrum). */
import { mountNowPlaying } from './nowplaying.js';
const DEFAULT_BANDS = 270; // 10 Hz .. 24 kHz at one band per 1/24 octave
const MAX_BANDS = 512;
const SCALED_BANDS = 240;
const FREQUENCY_SCALES = ['log', 'bark', 'linear', 'mel'];
const OCTAVE_RATIO = 2 ** (1 / 24);
const OCTAVE_HALF_RATIO = Math.sqrt(OCTAVE_RATIO);
const REFERENCE_FREQUENCY = 8.17579892; // audioMotion's C-1 anchor
const FRAME_MS = 1000 / 60;
// Match audioMotion's default dynamic settings. Its AnalyzerNode applies
// smoothingTimeConstant=0.5 and its bar peaks hold for 500 ms before the
// default gravity drop (fadePeaks=false) begins.
const AUDIO_MOTION_SMOOTHING = 0.5;
const PEAK_HOLD_MS = 500;
// HiFi settings, chosen by listening (2026-10) and fixed: no bar easing,
// linear amplitude with a 2.0 boost, peaks held a little longer than audioMotion.
const DEFAULT_RELEASE = 0, DEFAULT_PEAK_HOLD_MS = 750, DEFAULT_LINEAR_BOOST = 2;
const AUDIO_MOTION_GRAVITY = 3.8;
// audioMotion's level mapping (minDecibels -85, maxDecibels -25, in WebAudio
// AnalyserNode units). Our levels read a full-scale sine as 0 dB; the
// AnalyserNode reads it 20*log10(0.42/2) = -13.56 dB (Blackman window, 1/N).
// 'linear' is audioMotion's linearAmplitude: only strong components rise, so
// bars stay low and separate; 'db' fills most of the height with music.
const AUDIO_MOTION_MIN_DB = -85;
const AUDIO_MOTION_MAX_DB = -25;
const ANALYSER_OFFSET_DB = 20 * Math.log10(2 / 0.42);
// audioMotion ledBars: LED height follows the bar width, so each lit cell is
// roughly square instead of a tall sliver. Unlit cells stay faintly visible.
const LED_UNLIT = 'rgba(127, 140, 160, .12)';
// Browser source: an AnalyserNode beside the page's own audio output, sized
// like audioMotion's default. WebAudio scales by 1/N, so a full-scale sine
// reads 20*log10(0.42/2) dB with its Blackman window; add that back so both
// sources share the backend's calibrated -80..0 dBFS display.
const WEB_FFT_SIZE = 8192;
const WEB_CALIBRATION_DB = 20 * Math.log10(2 / 0.42);
const WEB_FLOOR_DB = -90;
const SOURCES = ['hifimate', 'web'];
export function finiteBins(values) {
  // The backend count changes with Nyquist; also accept old fixed-count frames.
  return Array.isArray(values) && values.length > 0 && values.length <= MAX_BANDS
    ? values.map(v => Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0)
    : Array(DEFAULT_BANDS).fill(0);
}

export function scaleFrequency(frequency, frequencyScale = 'log') {
  switch (frequencyScale) {
    case 'log': return Math.log2(frequency);
    case 'bark': return 26.81 * frequency / (1960 + frequency) - .53;
    case 'mel': return Math.log2(1 + frequency / 700);
    case 'linear': return frequency;
    default: throw Error('unsupported frequencyScale');
  }
}

function unscaleFrequency(value, frequencyScale) {
  switch (frequencyScale) {
    case 'log': return 2 ** value;
    case 'bark': return 1960 / (26.81 / (value + .53) - 1);
    case 'mel': return 700 * (2 ** value - 1);
    case 'linear': return value;
    default: throw Error('unsupported frequencyScale');
  }
}

export function frequencyPosition(frequency, minimum = 10, maximum = 24000, frequencyScale = 'log') {
  const lower = scaleFrequency(minimum, frequencyScale), upper = scaleFrequency(maximum, frequencyScale);
  return Math.max(0, Math.min(1, (scaleFrequency(frequency, frequencyScale) - lower) / (upper - lower)));
}

export function frequencyBandEdges(minimum = 10, maximum = 24000, frequencyScale = 'log') {
  const lower = scaleFrequency(minimum, frequencyScale), upper = scaleFrequency(maximum, frequencyScale);
  if (frequencyScale !== 'log') {
    return [minimum, ...Array.from({ length: SCALED_BANDS - 1 }, (_, i) =>
      unscaleFrequency(lower + (upper - lower) * (i + 1) / SCALED_BANDS, frequencyScale)), maximum];
  }
  const centers = [];
  for (let center = REFERENCE_FREQUENCY;
    center <= maximum && centers.length < MAX_BANDS;
    center *= OCTAVE_RATIO) {
    if (center >= minimum) centers.push(center);
  }
  if (!centers.length) return [minimum, maximum];
  const edges = [Math.max(minimum, centers[0] / OCTAVE_HALF_RATIO),
    ...centers.map(center => center * OCTAVE_HALF_RATIO)];
  if (edges.at(-1) > maximum) {
    edges[edges.length - 1] = maximum;
  } else if (edges.length - 1 < MAX_BANDS) {
    edges.push(maximum);
  }
  return edges;
}

export function analyserBands(spectrum, sampleRate, edges) {
  // Same aggregation as the backend and audioMotion: interpolate both band
  // edges and keep the loudest FFT bin between them.
  const last = spectrum.length - 1, binHz = sampleRate / (2 * spectrum.length);
  const level = i => {
    const value = spectrum[Math.max(0, Math.min(last, i))] + WEB_CALIBRATION_DB;
    return Number.isFinite(value) ? Math.max(WEB_FLOOR_DB, Math.min(0, value)) : WEB_FLOOR_DB;
  };
  const at = frequency => {
    const position = Math.max(1, Math.min(last, frequency / binHz)), lower = Math.floor(position);
    return level(lower) + (level(lower + 1) - level(lower)) * (position - lower);
  };
  return edges.slice(1).map((high, i) => {
    const low = edges[i];
    let peak = Math.max(at(low), at(high));
    for (let bin = Math.ceil(low / binHz); bin <= Math.min(last, Math.floor(high / binHz)); bin++) {
      if (bin >= 1) peak = Math.max(peak, level(bin));
    }
    return peak;
  });
}

export function plotGeometry(width, count = DEFAULT_BANDS) {
  // Just room for the first/last axis label; the bars are the content.
  const left = Math.min(10, width / 16), plotWidth = width - 2 * left;
  return { left, width: plotWidth, step: plotWidth / count };
}

// LEDs as in audioMotion's 1/24-octave view: small square dots on an even
// grid. Each column slot keeps 45 % empty (its spaceH); rows use the same
// pitch as columns, so a cell is a square and the gaps match both ways.
const LED_SPACE_H = .45;

export function barGap(step) {
  return step * LED_SPACE_H;
}

export function ledGeometry(step, range, pixelRatio = 1) {
  // Whole device pixels per row: a fractional pitch makes every few rows sit
  // 1 px closer, which shows as horizontal banding at 1x.
  const ratio = pixelRatio > 0 ? pixelRatio : 1;
  const pitch = Math.max(2, Math.round(step * ratio)) / ratio, height = pitch - barGap(pitch), space = pitch - height;
  const count = Math.max(1, Math.floor((range + space) / pitch));
  return { height, space, pitch, count };
}

// One LED in device pixels: a square with its corners stepped off so it
// reads as a dot (1 px from 4 px cells, a 2-step stair from 8 px). Below
// 4 px a cut corner would leave a "+", so small cells stay square.
function ledCell(ctx, x, y, w, h) {
  const cut = Math.min(w, h) >= 8 ? 2 : Math.min(w, h) >= 4 ? 1 : 0;
  for (let row = 0; row < cut; row++) {
    const inset = cut - row;
    ctx.fillRect(x + inset, y + row, w - 2 * inset, 1);
    ctx.fillRect(x + inset, y + h - 1 - row, w - 2 * inset, 1);
  }
  ctx.fillRect(x, y + cut, w, h - 2 * cut);
}

const ledLayers = new WeakMap();
function offscreen(canvas, width, height) {
  if (typeof OffscreenCanvas !== 'undefined') return new OffscreenCanvas(width, height);
  const made = canvas.ownerDocument?.createElement?.('canvas');
  if (!made?.getContext) return null;
  made.width = width; made.height = height;
  return made;
}

// audioMotion's X-axis labels: the ISO octave band centers (16 Hz .. 16 kHz,
// written 31/63/125/.../2k/4k), extended by octaves for high sample rates.
export const OCTAVE_TICKS = [16, 31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000,
  32000, 64000, 128000, 256000];

function frequencyLabel(value) {
  return value >= 1000 ? `${Number((value / 1000).toFixed(1))}k` : String(Math.round(value));
}

export function frequencyTicks(minimum, maximum, width, frequencyScale = 'log') {
  const plot = plotGeometry(width);
  const selected = [];
  // Place octave labels from 1k outwards so the middle of the axis survives on
  // narrow canvases; skip any label that would overlap one already placed.
  const candidates = OCTAVE_TICKS.filter(f => f >= minimum && f <= maximum)
    .sort((a, b) => Math.abs(Math.log2(a / 1000)) - Math.abs(Math.log2(b / 1000)));
  for (const frequency of candidates) {
    const text = frequencyLabel(frequency);
    const x = plot.left + frequencyPosition(frequency, minimum, maximum, frequencyScale) * plot.width;
    if (selected.every(t => Math.abs(t.x - x) > (t.text.length + text.length) * 3.2 + 8)) {
      selected.push({ frequency, text, x });
    }
  }
  return selected.sort((a, b) => a.x - b.x);
}

export function advanceBars(values, target, elapsedMs, smoothing = AUDIO_MOTION_SMOOTHING) {
  const frameCount = Math.max(0, elapsedMs) / FRAME_MS;
  const factor = 1 - Math.pow(Math.max(0, Math.min(1, smoothing)), frameCount);
  return values.map((v, i) => v + (target[i] - v) * factor);
}

export function displayBins(db, fallback = [], amplitude = 'linear', boost = DEFAULT_LINEAR_BOOST) {
  if (!Array.isArray(db) || db.length !== fallback.length || !db.length) return finiteBins(fallback);
  const linear = amplitude === 'linear', toLinear = v => 10 ** (v / 20);
  const min = linear ? toLinear(AUDIO_MOTION_MIN_DB) : AUDIO_MOTION_MIN_DB;
  const max = linear ? toLinear(AUDIO_MOTION_MAX_DB) : AUDIO_MOTION_MAX_DB;
  return db.map(value => {
    if (!Number.isFinite(value)) return 0;
    const analyser = value - ANALYSER_OFFSET_DB;  // audioMotion's getFloatFrequencyData value
    // audioMotion _normalizedB: with linearBoost b the value AND the range
    // take the b-th root, (v^(1/b) - min) / (max - min)^(1/b): a root
    // compression between linear and dB that keeps quiet detail visible.
    const root = linear ? 1 / Math.max(1, boost) : 1;
    const level = linear ? toLinear(analyser) ** root : analyser;
    return Math.max(0, Math.min(1, (level - min) / (max - min) ** root));
  });
}

function mixColor(a, b, amount) {
  const t = Math.max(0, Math.min(1, amount));
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * t)}, ${Math.round(a[1] + (b[1] - a[1]) * t)}, ${Math.round(a[2] + (b[2] - a[2]) * t)})`;
}

export function spectrumColor(_position, level, _attack = 0, _top = 0) {
  // Fixed audioMotion-style classic gradient: green at the base, yellow in
  // the middle, red at the top. Frequency and transient state never recolor a
  // bar, so the palette remains stable while its height changes.
  const green = [0, 224, 112], yellow = [255, 224, 42], red = [255, 48, 72];
  return level < .55
    ? mixColor(green, yellow, level / .55)
    : mixColor(yellow, red, (level - .55) / .45);
}

export function drawHiFiMateSpectrum(canvas, values, peaks = [], elapsedMs = 1000 / 60,
  { ticks = [], previous = [], peakAges = [], peakVelocities = [], peakHoldMs = PEAK_HOLD_MS,
    gravity = AUDIO_MOTION_GRAVITY, updatePeaks = true,
    minimum = 10, maximum = 24000, frequencyScale = 'log' } = {}) {
  const bins = finiteBins(values), ctx = canvas.getContext('2d');
  const rect = canvas.getBoundingClientRect?.();
  const width = rect?.width || canvas.width, height = rect?.height || canvas.height;
  ctx.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const plot = plotGeometry(width, bins.length), top = 10, bottom = height - 2, range = bottom - top;
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(153,188,227,.09)';
  ctx.beginPath();
  ticks.forEach(tick => { ctx.moveTo(tick.x, top); ctx.lineTo(tick.x, bottom); });
  for (let row = 1; row <= 4; row++) {
    const y = top + range * row / 4;
    ctx.moveTo(plot.left, y); ctx.lineTo(width - plot.left, y);
  }
  ctx.stroke();
  const next = updatePeaks ? bins.map((v, i) => {
    const previousPeak = peaks[i] || 0;
    const previousAge = peakAges[i] || 0;
    const elapsed = Math.max(0, elapsedMs);
    const age = previousAge + elapsed;
    let peak = previousPeak;
    let velocity = peakVelocities[i] || 0;
    if (age > peakHoldMs && previousPeak > 0) {
      // audioMotion's peak drop uses the negative hold-frame counter as an
      // accelerating gravity term. Integrate it over elapsed frames so a
      // skipped animation frame has the same result as several normal frames.
      const frames = Math.max(0, (age - Math.max(previousAge, peakHoldMs)) / FRAME_MS);
      const pixels = Math.max(1, range);
      const acceleration = gravity * 1000 / ((1000 / FRAME_MS) ** 2 * pixels);
      peak += velocity * frames - acceleration * frames * (frames + 1) / 2;
      velocity -= acceleration * frames;
    } else {
      velocity = 0;
    }
    // audioMotion drops the peak first, then lets the current bar replace it.
    if (v >= peak) {
      peak = v;
      peakAges[i] = 0;
      velocity = 0;
    } else {
      peakAges[i] = age;
    }
    peakVelocities[i] = velocity;
    return Math.max(v, peak);
  }) : peaks.slice();
  // The backend aggregates bands in the selected scale (evenly spaced in
  // that scale), so equal-width slots line up with the matching axis ticks.
  const geometry = index => {
    const left = plot.left + index * plot.step;
    const right = left + plot.step;
    const gap = barGap(right - left);
    return { left: left + gap / 2, width: Math.max(0, right - left - gap) };
  };
  // Fixed vertical gradient: green at the base, yellow, red at the top.
  const barGradient = target => {
    const gradient = target.createLinearGradient(0, bottom, 0, top);
    gradient.addColorStop(0, '#00e070'); gradient.addColorStop(.55, '#ffe02a'); gradient.addColorStop(1, '#ff3048');
    return gradient;
  };
  const led = ledGeometry(plot.step, range, canvas.height / height || 1);
  // Like audioMotion, bar and peak heights snap to whole LEDs.
  const ledLevel = value => Math.min(led.count, Math.round(value * led.count));
  const ledTop = count => bottom - count * led.pitch + led.space;
  const fillBars = target => {
    target.fillStyle = barGradient(target);
    bins.forEach((value, i) => {
      const lit = ledLevel(value);
      if (!lit) return;
      const { left: x, width: barWidth } = geometry(i);
      target.fillRect(x, ledTop(lit), barWidth, bottom - ledTop(lit));
    });
    next.forEach((value, i) => {
      const lit = ledLevel(value);
      if (!lit || lit <= ledLevel(bins[i])) return;
      const band = geometry(i);
      // audioMotion peakLine=false: one independent peak LED per bar, never a
      // polyline that visually bridges unrelated frequency bands.
      target.fillStyle = spectrumColor(0, lit / led.count);
      target.fillRect(band.left, ledTop(lit), band.width, led.height);
    });
  };
  // Dots: solid bars are cut by a cached mask of LED shapes (rebuilt only on
  // a size change), one composite per frame instead of thousands of cells.
  const sx = canvas.width / width, sy = canvas.height / height;
  const key = [canvas.width, canvas.height, bins.length, plot.left, plot.step, top, bottom].join();
  let layers = ledLayers.get(canvas);
  if (!layers || layers.key !== key) {
    const mask = offscreen(canvas, canvas.width, canvas.height);
    const unlit = mask && offscreen(canvas, canvas.width, canvas.height);
    const layer = unlit && offscreen(canvas, canvas.width, canvas.height);
    layers = { key, mask, unlit, layer };
    if (layer) {
      for (const [target, color] of [[mask.getContext('2d'), '#fff'], [unlit.getContext('2d'), LED_UNLIT]]) {
        target.fillStyle = color;
        // One integer cell size for every LED (only positions are rounded):
        // sizes alternating between n and n+1 px beat into visible patches.
        const size = Math.max(1, Math.round(led.height * sx));
        bins.forEach((_, i) => {
          const { left: x, width: w } = geometry(i);
          const x0 = Math.round((x + w / 2) * sx - size / 2);
          for (let row = 1; row <= led.count; row++) {
            ledCell(target, x0, Math.round((ledTop(row) + led.height / 2) * sy - size / 2), size, size);
          }
        });
      }
    }
    ledLayers.set(canvas, layers);
  }
  if (layers.layer) {
    const lctx = layers.layer.getContext('2d');
    lctx.setTransform(1, 0, 0, 1, 0, 0);
    lctx.clearRect(0, 0, canvas.width, canvas.height);
    lctx.setTransform(sx, 0, 0, sy, 0, 0);
    fillBars(lctx);
    lctx.setTransform(1, 0, 0, 1, 0, 0);
    lctx.globalCompositeOperation = 'destination-in';
    lctx.drawImage(layers.mask, 0, 0);
    lctx.globalCompositeOperation = 'source-over';
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(layers.unlit, 0, 0);
    ctx.drawImage(layers.layer, 0, 0);
    ctx.setTransform(sx, 0, 0, sy, 0, 0);
    return next;
  }
  // Without offscreen canvases (tests, old browsers): square cells, no corners cut.
  ctx.fillStyle = LED_UNLIT;
  bins.forEach((_, i) => {
    const { left: x, width: barWidth } = geometry(i);
    ctx.fillRect(x, ledTop(led.count), barWidth, bottom - ledTop(led.count));
  });
  fillBars(ctx);
  // One gap per LED row, rather than thousands of individually drawn cells.
  for (let row = 1; row <= led.count; row++) {
    ctx.clearRect(plot.left, bottom - row * led.pitch, plot.width, led.space);
  }
  return next;
}

export function mountHiFiMateSpectrum({ document: doc = document, EventSource: Stream = EventSource,
  requestFrame = requestAnimationFrame, cancelFrame = cancelAnimationFrame,
  now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout,
  tap = doc.defaultView?.__hifimateAudioTap, embedded = false, container = null, visible = true } = {}) {
  if (doc.getElementById('hifimate-spectrum-overlay')) return null;
  const panel = doc.createElement('section');
  panel.id = 'hifimate-spectrum-overlay';
  panel.setAttribute('aria-label', '当前输出频谱');
  const button = doc.createElement('button');
  button.type = 'button'; button.textContent = '收起频谱';
  button.setAttribute('aria-expanded', 'true');
  const fullscreenButton = doc.createElement('button');
  fullscreenButton.type = 'button'; fullscreenButton.textContent = '全屏';
  fullscreenButton.setAttribute('aria-label', '全屏显示频谱');
  const label = doc.createElement('span');
  label.className = 'hifimate-spectrum-meta';
  const status = doc.createElement('span');
  status.textContent = '输出频谱 · 等待音频';
  const scaleSelect = doc.createElement('select');
  scaleSelect.className = 'hifimate-spectrum-scale';
  scaleSelect.setAttribute('aria-label', '频率轴');
  const scaleLabels = { log: 'Log · 1/24 octave', bark: 'Bark', linear: 'Linear', mel: 'Mel' };
  FREQUENCY_SCALES.forEach(scale => {
    const option = doc.createElement('option');
    option.value = scale;
    option.textContent = scaleLabels[scale];
    scaleSelect.append(option);
  });
  label.append(status, scaleSelect);
  // Amplitude, boost, release and peak hold are fixed (DEFAULT_*), never read from storage.
  // Only a page with the early audio tap (Music Assistant) plays audio itself.
  const sourceSelect = tap ? doc.createElement('select') : null;
  if (sourceSelect) {
    sourceSelect.className = 'hifimate-spectrum-scale';
    sourceSelect.setAttribute('aria-label', '频谱音源');
    [['hifimate', 'HiFiMate 输出'], ['web', '本浏览器']].forEach(([value, text]) => {
      const option = doc.createElement('option');
      option.value = value; option.textContent = text;
      sourceSelect.append(option);
    });
    label.append(sourceSelect);
  }
  const opacityInput = embedded ? doc.createElement('input') : null;
  if (opacityInput) {
    opacityInput.type = 'range'; opacityInput.min = '0.3'; opacityInput.max = '1'; opacityInput.step = '0.05';
    opacityInput.className = 'hifimate-spectrum-opacity';
    opacityInput.setAttribute('aria-label', '面板不透明度');
    label.append(opacityInput);
  }
  const canvas = doc.createElement('canvas');
  canvas.width = 640; canvas.height = 300;
  const axis = doc.createElement('div');
  axis.className = 'hifimate-spectrum-axis';
  // In Music Assistant: what the chosen source plays, its transport and lyrics, as in /ctrl.
  const npBox = embedded ? doc.createElement('div') : null, lyricBox = embedded ? doc.createElement('div') : null;
  if (npBox) { npBox.className = 'np hifimate-spectrum-np'; lyricBox.className = 'lyric hifimate-spectrum-lyric'; }
  panel.append(button, fullscreenButton, label, ...(npBox ? [npBox, lyricBox] : []), canvas, axis);
  if (embedded) panel.classList.toggle('hifimate-spectrum-embedded', true);
  (container || doc.body).append(panel);
  if (container) {
    panel.classList.toggle('hifimate-spectrum-inline', true);
    button.hidden = true;
  }
  const view = doc.defaultView;
  // Embedded in another app, start as a small button instead of covering it.
  let stream = null, frame = 0, timer = 0, closed = embedded, inView = true, destroyed = false;
  let source = 'hifimate', webActive = false, unsubscribeTap = null;
  const analysers = new Map();
  let bars = Array(DEFAULT_BANDS).fill(0), peaks = bars.slice(), target = bars.slice(), previous = bars.slice(),
    peakAges = bars.slice(), peakVelocities = bars.slice(), last = 0;
  let previousPaint = null, minimum = 10, maximum = 24000, frequencyScale = 'log',
    channelLayout = 'dual-combined', ticks = [], axisKey = '', fallbackFullscreen = false;
  // null follows the server's configured default; otherwise the stream asks
  // the backend to aggregate its bands in this viewer's chosen scale.
  let requestedScale = null;
  try {
    const storedScale = view?.localStorage?.getItem('hifimate-spectrum-frequency-scale');
    if (FREQUENCY_SCALES.includes(storedScale)) {
      frequencyScale = requestedScale = storedScale;
    }
  } catch (_) { /* storage may be disabled by browser privacy settings */ }
  scaleSelect.value = frequencyScale;
  try {
    const storedSource = view?.localStorage?.getItem('hifimate-spectrum-source');
    if (sourceSelect && SOURCES.includes(storedSource)) source = storedSource;
  } catch (_) { /* optional */ }
  if (sourceSelect) sourceSelect.value = source;
  const buttonText = open => embedded ? (open ? '收起' : '频谱') : (open ? '收起频谱' : '显示频谱');
  if (closed) {
    panel.classList.toggle('hifimate-spectrum-collapsed', true);
    button.textContent = buttonText(false); button.setAttribute('aria-expanded', 'false');
  }

  // Embedded floating window: chip and panel positions, panel size and
  // opacity persist per browser. Geometry is clamped into the viewport.
  const CHIP = { width: 56, height: 34 };
  let layout = { chip: null, panel: null, opacity: .92 };
  try {
    const stored = JSON.parse(view?.localStorage?.getItem('hifimate-spectrum-layout') || 'null');
    if (stored && typeof stored === 'object') layout = { ...layout, ...stored };
  } catch (_) { /* optional */ }
  if (!(layout.opacity >= .3 && layout.opacity <= 1)) layout.opacity = .92;
  if (opacityInput) opacityInput.value = String(layout.opacity);
  function saveLayout() {
    try { view?.localStorage?.setItem('hifimate-spectrum-layout', JSON.stringify(layout)); } catch (_) { /* optional */ }
  }
  const viewport = () => ({ width: view?.innerWidth || 1280, height: view?.innerHeight || 800 });
  function clampBox(box) {
    const { width: vw, height: vh } = viewport();
    const width = Math.min(box.width, vw), height = Math.min(box.height, vh);
    return { width, height, left: Math.max(0, Math.min(vw - width, box.left)), top: Math.max(0, Math.min(vh - height, box.top)) };
  }
  function panelSize() {
    const { width: vw } = viewport();
    return { width: layout.panel?.width || Math.min(vw * .92, 736), height: layout.panel?.height || (embedded ? 480 : 360) };
  }
  function chipBox() {
    const { width: vw, height: vh } = viewport();
    return clampBox({ ...CHIP, ...(layout.chip || { left: vw - CHIP.width - 16, top: vh - CHIP.height - 104 }) });
  }
  // Expand/collapse in place: keep the corner nearest the screen edge fixed.
  function anchored(from, size) {
    const { width: vw, height: vh } = viewport();
    const right = from.left + from.width / 2 > vw / 2, bottom = from.top + from.height / 2 > vh / 2;
    return clampBox({ ...size, left: right ? from.left + from.width - size.width : from.left,
      top: bottom ? from.top + from.height - size.height : from.top });
  }
  const fullscreenActive = () => doc.fullscreenElement === panel || fallbackFullscreen;
  function applyLayout() {
    if (!embedded) return;
    const style = panel.style;
    if (fullscreenActive()) {
      Object.assign(style, { left: '', top: '', width: '', height: '', opacity: '' }); return;
    }
    const box = closed ? chipBox() : clampBox({ ...panelSize(), ...(layout.panel || anchored(chipBox(), panelSize())) });
    Object.assign(style, { left: `${box.left}px`, top: `${box.top}px`,
      width: closed ? '' : `${box.width}px`, height: closed ? '' : `${box.height}px`,
      opacity: closed ? '' : String(layout.opacity) });
  }
  function currentBox() {
    const rect = panel.getBoundingClientRect?.() || {};
    return { left: parseFloat(panel.style.left) || rect.left || 0, top: parseFloat(panel.style.top) || rect.top || 0,
      width: rect.width || (closed ? CHIP.width : panelSize().width), height: rect.height || (closed ? CHIP.height : panelSize().height) };
  }
  let drag = null, suppressClick = false, expandedFrom = null;
  if (embedded) {
    panel.onpointerdown = event => {
      if (fullscreenActive() || event.button > 0 || event.target?.closest?.('.np, .lyric')) return;
      const tag = event.target?.tagName;
      if (tag === 'SELECT' || tag === 'INPUT' || tag === 'OPTION') return;
      const box = currentBox();
      // Leave the bottom-right corner to the native resize handle.
      if (!closed && event.clientX > box.left + box.width - 18 && event.clientY > box.top + box.height - 18) return;
      // Capture only once it is really a drag: a captured pointer retargets
      // the following click to the panel, so the chip button would never open.
      drag = { x: event.clientX, y: event.clientY, box, moved: false, id: event.pointerId };
      // The chip is tiny: the pointer leaves it on the first move, so follow
      // the drag on the document until it is captured or released.
      doc.addEventListener('pointermove', dragMove);
      doc.addEventListener('pointerup', dragEnd);
      doc.addEventListener('pointercancel', dragEnd);
    };
    function dragMove(event) {
      if (!drag) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;  // still a click
      if (!drag.moved) panel.setPointerCapture?.(drag.id);
      drag.moved = true;
      const box = clampBox({ ...drag.box, left: drag.box.left + dx, top: drag.box.top + dy });
      panel.style.left = `${box.left}px`; panel.style.top = `${box.top}px`;
      event.preventDefault?.();
    }
    function dragEnd() {
      doc.removeEventListener('pointermove', dragMove);
      doc.removeEventListener('pointerup', dragEnd);
      doc.removeEventListener('pointercancel', dragEnd);
      if (!drag) return;
      if (drag.moved) {
        panel.releasePointerCapture?.(drag.id);
        // A drag of the chip must not also toggle it. The click (if any) is
        // dispatched right after pointerup; never let the flag eat a later one.
        suppressClick = true; setTimer(() => { suppressClick = false; }, 0);
        const box = currentBox();
        if (closed) layout.chip = { left: box.left, top: box.top };
        else { layout.panel = { ...panelSize(), left: box.left, top: box.top }; expandedFrom = null; }
        saveLayout();
      }
      drag = null;
    }
    if (opacityInput) opacityInput.oninput = () => {
      layout.opacity = Math.max(.3, Math.min(1, Number(opacityInput.value) || .92));
      if (!closed) panel.style.opacity = String(layout.opacity);
      saveLayout();
    };
  }
  function rememberSize() {
    // Native CSS resize changes the panel's own size; keep it for next time.
    if (!embedded || closed || fullscreenActive() || drag) return;
    const rect = panel.getBoundingClientRect?.();
    if (!rect?.width || !rect.height) return;
    const size = { width: Math.round(rect.width), height: Math.round(rect.height) };
    if (size.width === Math.round(panelSize().width) && size.height === Math.round(panelSize().height)) return;
    layout.panel = { ...(layout.panel || currentBox()), ...size }; saveLayout();
  }

  function liveAnalysers() {
    // Attach lazily, in each AudioContext the page plays through.
    for (const output of tap.outputs) {
      const context = output.context;
      if (context.state === 'closed') continue;
      let entry = analysers.get(context);
      if (!entry) {
        const analyser = context.createAnalyser();
        analyser.fftSize = WEB_FFT_SIZE;
        analyser.smoothingTimeConstant = 0; // advanceBars applies DEFAULT_RELEASE
        entry = { analyser, nodes: new Set(), data: new Float32Array(analyser.frequencyBinCount) };
        analysers.set(context, entry);
        context.addEventListener?.('statechange', () => { if (webActive) schedule(); });
      }
      if (!entry.nodes.has(output.node)) { tap.connect(output.node, entry.analyser); entry.nodes.add(output.node); }
    }
    for (const context of [...analysers.keys()]) if (context.state === 'closed') analysers.delete(context);
    return [...analysers.entries()].filter(([context]) => context.state === 'running');
  }
  function sampleWeb() {
    const live = liveAnalysers();
    if (!live.length) {
      target.fill(0); status.textContent = '本浏览器 · 当前页面未在播放'; return false;
    }
    const rate = live[0][0].sampleRate;
    updateAxis({ frequency_min: 10, frequency_max: rate / 2, frequencyScale, channelLayout: 'dual-combined' });
    const edges = frequencyBandEdges(10, rate / 2, frequencyScale);
    let db = null;
    for (const [context, entry] of live) {
      entry.analyser.getFloatFrequencyData(entry.data);
      const bands = analyserBands(entry.data, context.sampleRate, edges);
      db = db ? db.map((value, i) => Math.max(value, bands[i])) : bands;
    }
    const next = displayBins(db, db.map(() => 0));
    if (next.length !== bars.length) {
      bars = next.map(() => 0); peaks = bars.slice(); previous = bars.slice(); peakAges = bars.slice(); peakVelocities = bars.slice(); axisKey = '';
      updateAxis();
    }
    target = next; last = now();
    status.textContent = `本浏览器 · ${rate / 1000} kHz · ${next.length} 频段`;
    return true;
  }

  function resize() {
    const rect = canvas.getBoundingClientRect?.();
    if (!rect?.width || !rect.height) return;
    const ratio = Math.min(view?.devicePixelRatio || 1, 3, Math.sqrt(8000000 / (rect.width * rect.height)));
    const width = Math.round(rect.width * ratio), height = Math.round(rect.height * ratio);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width; canvas.height = height;
    }
    updateAxis();
    drawHiFiMateSpectrum(canvas, bars, peaks, 0,
      { ticks, previous: bars, peakAges, peakVelocities, updatePeaks: false,
        minimum, maximum, frequencyScale });
  }
  function updateAxis(value) {
    const nextMin = value?.frequency_min ?? minimum;
    const nextMax = value?.frequency_max ?? (value?.rate ? value.rate / 2 : maximum);
    // The axis always describes the received bands. Resize calls retain the
    // current mode; a legacy frame (no field, query ignored) means log.
    const nextScale = value ? value.frequencyScale ?? 'log' : frequencyScale;
    const nextLayout = value ? value.channelLayout ?? 'dual-combined' : channelLayout;
    if (!FREQUENCY_SCALES.includes(nextScale) || !['single', 'dual-combined'].includes(nextLayout)) {
      throw Error('unsupported spectrum configuration');
    }
    if (Number.isFinite(nextMin) && Number.isFinite(nextMax) && nextMin > 0 && nextMax > nextMin) {
      if (minimum !== nextMin || maximum !== nextMax || frequencyScale !== nextScale || channelLayout !== nextLayout) {
        bars.fill(0); peaks.fill(0); previous.fill(0); target.fill(0); peakAges.fill(0); peakVelocities.fill(0);
      }
      minimum = nextMin; maximum = nextMax;
      frequencyScale = nextScale; channelLayout = nextLayout;
      scaleSelect.value = frequencyScale;
    }
    const width = canvas.getBoundingClientRect?.().width || canvas.width;
    const key = `${minimum}/${maximum}/${frequencyScale}/${channelLayout}/${bars.length}/${width}`;
    if (key === axisKey) return;
    axisKey = key;
    ticks = frequencyTicks(minimum, maximum, width, frequencyScale);
    axis.replaceChildren(...ticks.map(tick => {
      const node = doc.createElement('span');
      node.textContent = tick.text;
      node.style.left = `${tick.x}px`;
      node.dataset.frequency = String(tick.frequency);
      return node;
    }));
    canvas.setAttribute('aria-label', `${minimum} Hz 至 ${maximum} Hz，${frequencyScale}，线性幅度，${bars.length} 频段`);
  }
  function syncFullscreen() {
    const active = doc.fullscreenElement === panel || fallbackFullscreen;
    panel.classList.toggle('hifimate-spectrum-fullscreen', active);
    fullscreenButton.textContent = active ? '退出全屏' : '全屏';
    fullscreenButton.setAttribute('aria-label', active ? '退出频谱全屏' : '全屏显示频谱');
    applyLayout();
    resize();
  }
  async function toggleFullscreen() {
    try {
      if (fallbackFullscreen) fallbackFullscreen = false;
      else if (doc.fullscreenElement === panel) await doc.exitFullscreen?.();
      else if (panel.requestFullscreen) await panel.requestFullscreen();
      else fallbackFullscreen = true;
    } catch (_) { fallbackFullscreen = true; }
    syncFullscreen();
  }
  function clear() {
    if (frame) cancelFrame(frame);
    if (timer) clearTimer(timer);
    frame = timer = 0;
    previousPaint = null;
    bars.fill(0); peaks.fill(0); target.fill(0); previous.fill(0); peakAges.fill(0); peakVelocities.fill(0);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  function paint() {
    frame = 0;
    if ((!stream && !webActive) || destroyed) return;
    const time = now();
    // Limit rendering to 60 fps even on high-refresh-rate screens.
    if (previousPaint !== null && time - previousPaint < 1000 / 60 - 1) {
      frame = requestFrame(paint); return;
    }
    const elapsed = previousPaint === null ? 1000 / 60 : Math.max(0, time - previousPaint);
    previousPaint = time;
    const sampling = webActive && sampleWeb();
    if (time - last > 280) target.fill(0);
    bars = advanceBars(bars, target, elapsed, DEFAULT_RELEASE);
    peaks = drawHiFiMateSpectrum(canvas, bars, peaks, elapsed,
      { ticks, previous, peakAges, peakVelocities, minimum, maximum, frequencyScale, peakHoldMs: DEFAULT_PEAK_HOLD_MS });
    previous = bars.slice();
    if (sampling || bars.some(v => v > .005) || peaks.some(v => v > .005)) frame = requestFrame(paint);
    else clear();
  }
  function schedule() { if (!frame) frame = requestFrame(paint); }
  function stop() { if (stream) stream.close(); stream = null; webActive = false; clear(); }
  function sync() {
    if (destroyed || doc.hidden || closed || !inView || !visible) { stop(); return; }
    if (source === 'web') {
      // Browser analysis needs no backend stream; only the page's own audio.
      if (stream) { stream.close(); stream = null; }
      if (!webActive) {
        webActive = true; status.textContent = '本浏览器 · 等待音频';
        unsubscribeTap ??= tap.subscribe(() => { if (webActive) schedule(); });
        schedule();
      }
      return;
    }
    webActive = false;
    if (stream) return;
    const current = stream = new Stream(requestedScale
      ? `/_hifimate/spectrum?frequencyScale=${encodeURIComponent(requestedScale)}` : '/_hifimate/spectrum');
    current.onmessage = event => {
      if (current !== stream) return;
      try {
        const value = JSON.parse(event.data);
        if (value.origin !== 'dsp-model' || !['playing', 'silent', 'idle', 'waiting', 'unavailable'].includes(value.state)) throw Error('bad frame');
        const next = displayBins(value.db, value.bins);
        if (next.length !== bars.length) {
          clear(); bars = next.map(() => 0); peaks = bars.slice(); previous = bars.slice(); peakAges = bars.slice(); peakVelocities = bars.slice(); axisKey = '';
        }
        updateAxis(value);
        target = value.state === 'playing' ? next : next.map(() => 0);
        last = now();
        status.textContent = value.state === 'playing' ? `输出频谱 · ${value.rate / 1000} kHz · ${next.length} 频段`
          : value.state === 'unavailable' ? '频谱暂不可用'
          : value.state === 'waiting' ? '输出频谱 · 等待数据'
          : value.state === 'idle' ? '输出频谱 · 无新数据' : '输出频谱 · 静音';
        if (timer) clearTimer(timer);
        timer = setTimer(() => { timer = 0; target.fill(0); status.textContent = '输出频谱 · 无新数据'; schedule(); }, 280);
        schedule();
      } catch (_) { clear(); status.textContent = '频谱暂不可用'; }
    };
    current.onerror = () => { if (current === stream) { clear(); status.textContent = '频谱连接中'; } };
  }
  button.onclick = async () => {
    if (suppressClick) { suppressClick = false; return; }
    // 收起 in full screen leaves full screen too and folds straight to the chip.
    if (fullscreenActive() && !closed) {
      try { if (fallbackFullscreen) fallbackFullscreen = false; else await doc.exitFullscreen?.(); }
      catch (_) { fallbackFullscreen = false; }
      syncFullscreen();
    }
    if (embedded && !fullscreenActive()) {
      // In place: the panel grows from the chip, and the chip returns to the
      // panel's corner nearest the screen edge.
      const from = currentBox();
      if (closed) { expandedFrom = { left: from.left, top: from.top }; layout.panel = { ...anchored(from, panelSize()) }; }
      else {
        // An undragged panel returns the chip exactly where it was.
        const chip = expandedFrom || anchored(from, CHIP);
        layout.chip = { left: chip.left, top: chip.top }; expandedFrom = null;
      }
      saveLayout();
    }
    closed = !closed;
    panel.classList.toggle('hifimate-spectrum-collapsed', closed);
    button.textContent = buttonText(!closed);
    button.setAttribute('aria-expanded', String(!closed));
    applyLayout();
    if (!closed) pollNow();
    sync();
    if (!closed) resize();
  };
  scaleSelect.onchange = () => {
    const nextScale = scaleSelect.value;
    if (!FREQUENCY_SCALES.includes(nextScale) || (nextScale === frequencyScale && nextScale === requestedScale)) return;
    frequencyScale = requestedScale = nextScale;
    axisKey = '';
    try { view?.localStorage?.setItem('hifimate-spectrum-frequency-scale', nextScale); } catch (_) { /* optional */ }
    // Resubscribe so the backend re-aggregates the same FFT in the new scale;
    // bars from the previous scale do not describe the new slots.
    const active = Boolean(stream) || webActive;
    stop();
    updateAxis();
    resize();
    if (active) sync();
  };
  if (sourceSelect) sourceSelect.onchange = () => {
    if (!SOURCES.includes(sourceSelect.value) || sourceSelect.value === source) return;
    source = sourceSelect.value;
    try { view?.localStorage?.setItem('hifimate-spectrum-source', source); } catch (_) { /* optional */ }
    stop(); axisKey = ''; updateAxis(); resize(); sync();
    player?.resetLyrics(); pollNow();
  };
  fullscreenButton.onclick = toggleFullscreen;
  doc.addEventListener('visibilitychange', sync);
  doc.addEventListener('fullscreenchange', syncFullscreen);
  view?.addEventListener('pagehide', stop);
  view?.addEventListener('pageshow', sync);
  const onViewResize = () => { applyLayout(); resize(); };
  view?.addEventListener('resize', onViewResize);
  const observer = view?.IntersectionObserver ? new view.IntersectionObserver(entries => {
    inView = entries[0].isIntersecting; sync();
  }) : null;
  const sizeObserver = view?.ResizeObserver ? new view.ResizeObserver(() => { rememberSize(); resize(); }) : null;
  observer?.observe(panel);
  sizeObserver?.observe(canvas);
  if (embedded) sizeObserver?.observe(panel);
  applyLayout(); updateAxis(); resize(); sync();
  // Now playing for the selected source: HiFi's arbiter (through /ctrl) or this browser's MA web player.
  const SOURCE_LABEL = { dlna: ['DLNA', '#7aa2ff'], music_assistant_dlna: ['Music Assistant', '#b48cff'],
    airplay: ['AirPlay', '#4fd1ff'], bluetooth: ['Bluetooth', '#46d6a4'] };
  let hifimateNow = { now: null, time: 0, auth: null, reason: '' }, webNow = { now: null, time: 0, reason: '' }, npError = '', npTimer = 0, npErrorTimer = 0;
  const fetchJSON = async (url, options) => {
    const r = await view.fetch(url, { cache: 'no-store', credentials: 'same-origin', ...options });
    const text = await r.text(); let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (_) { /* not JSON */ }
    if (!r.ok) throw Error(body?.error || `请求失败（${r.status}）`);
    return body;
  };
  const stored = key => { try { return view?.localStorage?.getItem(key) || null; } catch (_) { return null; } };
  // MA's own API, as this page's MA session (its token and web player id live in this origin's storage).
  const ma = (command, args) => {
    const token = stored('ma_access_token');
    if (!token) throw Error('没有 Music Assistant 登录信息');
    return fetchJSON('/api', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message_id: 'hifimate-spectrum', command, args }) });
  };
  async function pollWeb() {
    const id = stored('sendspin_webplayer_id');
    if (!id || !stored('ma_access_token')) { webNow = { now: null, time: 0, reason: '读不到本浏览器播放器的信息' }; return; }
    const player = await ma('players/get', { player_id: id });
    const media = player?.current_media, state = player?.playback_state || player?.state;
    if (!media || !media.title) { webNow = { now: null, time: 0, reason: '' }; return; }
    const duration = Number(media.duration) > 0 ? Number(media.duration) : null;
    // Stopped (MA stops a long-paused queue) keeps its track: 播放 resumes it, the rest waits for playback.
    const active = ['playing', 'paused'].includes(state);
    // Which MA provider the track comes from (migu has its own lyrics; bilibili titles need fuzzy matching).
    const [origin, originId] = String(media.uri || '').match(/^([a-z_]+)(?:--[^:]*)?:\/\/track\/(.+)$/)?.slice(1) || [];
    webNow = { reason: '', time: Number(media.elapsed_time_last_updated ?? player.elapsed_time_last_updated) || Date.now() / 1000, now: {
      source: 'web', title: media.title || null, artist: media.artist || null, album: media.album || null,
      origin: ['migu', 'bilibili'].includes(origin) ? origin : null, originId: originId || null,
      state: state === 'playing' ? 'play' : active ? 'pause' : 'stop',
      position: active ? Number(media.elapsed_time ?? player.elapsed_time) || 0 : null, duration,
      controls: { toggle: true, next: active, previous: active, seek: active && Boolean(duration) } } };
  }
  async function pollHiFiMate() {
    const state = await fetchJSON('/ctrl/api/state');
    hifimateNow = { now: state?.now || null, time: state?.time || 0, auth: state?.auth || null, reason: '' };
  }
  let polling = false;
  async function pollNow() {
    if (!player || destroyed || polling) return;
    clearTimer(npTimer); npTimer = 0; polling = true;
    try {
      if (!closed && doc.visibilityState !== 'hidden') {
        const from = source;
        try { await (from === 'web' ? pollWeb() : pollHiFiMate()); }
        catch (_) { if (from === 'web') webNow = { now: null, time: 0, reason: '读不到本浏览器播放器的信息' }; else hifimateNow = { now: null, time: 0, auth: null, reason: '读不到 HiFi 的播放信息' }; }
        player.render();
      }
    } finally { polling = false; }
    if (!destroyed) npTimer = setTimer(pollNow, 1000);
  }
  const showError = message => {
    npError = message; player.render(); clearTimer(npErrorTimer);
    npErrorTimer = setTimer(() => { npError = ''; player.render(); }, 5000);
  };
  const player = npBox ? mountNowPlaying({ box: npBox, lyricBox, win: view, toast: showError,
    view: () => {
      if (source === 'web') {
        const now = webNow.now;
        return { now, time: webNow.time, label: ['本浏览器', '#b48cff'],
          allowed: key => Boolean(now?.controls?.[key]), hint: npError, idleHint: npError || webNow.reason };
      }
      const now = hifimateNow.now, c = now?.controls || {}, auth = hifimateNow.auth;
      const login = Boolean(auth?.configured && auth?.logged_in);
      return { now, time: hifimateNow.time, label: now ? SOURCE_LABEL[now.source] || [now.source, '#7aa2ff'] : null,
        allowed: key => Boolean(c[key] && login), hint: npError || (!login ? '在 /ctrl 登录后可控制' : now?.reason || ''),
        idleHint: npError || hifimateNow.reason };
    },
    command: async (command, position) => {
      if (source === 'web') {
        const id = stored('sendspin_webplayer_id');
        const name = { toggle: 'players/cmd/play_pause', next: 'players/cmd/next', previous: 'players/cmd/previous', seek: 'players/cmd/seek' }[command];
        await ma(name, command === 'seek' ? { player_id: id, position: Math.round(position) } : { player_id: id });
      } else {
        await fetchJSON('/ctrl/api/transport', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(position === undefined ? { command } : { command, position }) });
      }
      pollNow();
    },
    lyrics: now => {
      if (source !== 'web') return fetchJSON('/ctrl/api/lyrics');
      const query = new URLSearchParams({ title: now.title || '', artist: now.artist || '', album: now.album || '', duration: String(now.duration || '') });
      if (now.origin) { query.set('origin', now.origin); query.set('origin_id', now.originId || ''); }
      return fetchJSON(`/ctrl/api/lyrics?${query}`);
    } }) : null;
  if (player) { player.render(); pollNow(); }

  return { setVisible(value) { visible = Boolean(value); sync(); if (visible) resize(); }, destroy() {
    destroyed = true; player?.destroy(); clearTimer(npTimer); clearTimer(npErrorTimer); stop(); observer?.disconnect(); sizeObserver?.disconnect(); unsubscribeTap?.();
    doc.removeEventListener('visibilitychange', sync);
    doc.removeEventListener('fullscreenchange', syncFullscreen);
    view?.removeEventListener('pagehide', stop);
    view?.removeEventListener('pageshow', sync);
    view?.removeEventListener('resize', onViewResize);
    panel.remove();
  }};
}

// Music Assistant's nginx injects ?embed=ma; /ctrl imports it with ?mount=manual and mounts it itself.
if (typeof document !== 'undefined' && new URL(import.meta.url).searchParams.get('embed') === 'ma') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => mountHiFiMateSpectrum({ embedded: true }), { once: true });
  else mountHiFiMateSpectrum({ embedded: true });
}
