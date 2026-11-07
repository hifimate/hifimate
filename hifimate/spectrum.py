"""Read-only spectrum of the DSP output: its input, shaped by its own response.

Stock CamillaDSP exposes levels, not samples. While a browser subscribes, a
passive PipeWire capture (pw-record) reads exactly what the DSP captures, at
its native rate; each window's spectrum is multiplied by the DSP's transfer
matrix, measured by CamillaDSP itself offline (dsp_model.py), and by the live
main volume, mute and faders, and is analysed as late as the DSP's playback
buffer so it lines up with what the DAC plays. Never touches the DSP or DAC.
One watcher, one capture reader and one bounded FFT loop serve every viewer.
"""
from collections import deque
import cmath
import json
import math
import struct
import subprocess
import threading
import time
# audioMotion's octave mode uses a fixed 1/24-octave ratio.  The number of
# bars therefore depends on the upper edge (the current Nyquist frequency),
# rather than stretching a fixed number of bars over each sample rate.
OCTAVE_STEPS = 24
OCTAVE_RATIO = 2 ** (1 / OCTAVE_STEPS)
OCTAVE_HALF_RATIO = math.sqrt(OCTAVE_RATIO)
REFERENCE_FREQUENCY = 8.17579892  # audioMotion's C-1 equal-tempered anchor
MAX_BANDS = 512
SCALED_BANDS = 240  # audioMotion mode=1 on a non-logarithmic frequency scale
FREQUENCY_SCALES = ('log', 'bark', 'linear', 'mel')
CHANNEL_LAYOUTS = ('single', 'dual-combined')
DEFAULT_CHANNEL_LAYOUT = 'dual-combined'
FLOOR = -90.0
STALE = 0.35
CHUNK = 8192                # bytes per capture read (1024 stereo S32 frames)
WATCH = 0.5                 # s between DSP state polls
MAX_DELAY = 0.5             # s of playback buffer the analysis waits for, at most
FREQUENCY_MIN = 10.0
DEFAULT_FREQUENCY_MAX = 24000.0


def scale_frequency(frequency, frequency_scale='log'):
    """audioMotion 4.5.4 frequency coordinates (not an amplitude weighting)."""
    if frequency_scale == 'log':
        return math.log2(frequency)
    if frequency_scale == 'bark':
        return 26.81 * frequency / (1960 + frequency) - 0.53
    if frequency_scale == 'mel':
        return math.log2(1 + frequency / 700)
    if frequency_scale == 'linear':
        return frequency
    raise ValueError('unsupported frequencyScale')


def unscale_frequency(value, frequency_scale='log'):
    if frequency_scale == 'log':
        return 2 ** value
    if frequency_scale == 'bark':
        return 1960 / (26.81 / (value + 0.53) - 1)
    if frequency_scale == 'mel':
        return 700 * (2 ** value - 1)
    if frequency_scale == 'linear':
        return value
    raise ValueError('unsupported frequencyScale')


def frequency_edges(rate, frequency_scale='log'):
    """10 Hz..Nyquist: 1/24 octave on log, 240 uniform scaled bands otherwise."""
    maximum = max(FREQUENCY_MIN * 2, float(rate) / 2) if rate else DEFAULT_FREQUENCY_MAX
    lower = scale_frequency(FREQUENCY_MIN, frequency_scale)
    upper = scale_frequency(maximum, frequency_scale)
    if frequency_scale != 'log':
        return (FREQUENCY_MIN, *(unscale_frequency(lower + (upper - lower) * i / SCALED_BANDS,
                                                 frequency_scale)
                                 for i in range(1, SCALED_BANDS)), maximum)
    centers = []
    center = REFERENCE_FREQUENCY
    while center <= maximum and len(centers) < MAX_BANDS:
        if center >= FREQUENCY_MIN:
            centers.append(center)
        center *= OCTAVE_RATIO
    if not centers:
        return (FREQUENCY_MIN, maximum)
    edges = [max(FREQUENCY_MIN, centers[0] / OCTAVE_HALF_RATIO)]
    edges.extend(center * OCTAVE_HALF_RATIO for center in centers)
    if edges[-1] > maximum:
        edges[-1] = maximum
    elif len(edges) - 1 < MAX_BANDS:
        edges.append(maximum)
    return tuple(edges)


def band_centers(edges, frequency_scale='log'):
    return tuple(unscale_frequency((scale_frequency(a, frequency_scale) +
                                    scale_frequency(b, frequency_scale)) / 2, frequency_scale)
                 for a, b in zip(edges, edges[1:]))


def band_count(rate=None, frequency_scale='log'):
    return len(frequency_edges(rate, frequency_scale)) - 1


# Compatibility alias for callers that used the old 48 kHz default count.
# Actual analyzers and frames always use the rate-specific count.
BANDS = band_count(48000)


EDGES = frequency_edges(48000)
CENTERS = tuple(math.sqrt(a * b) for a, b in zip(EDGES, EDGES[1:]))


def fft_size(rate):
    """One window for every band, like audioMotion (fftSize 8192 at 48 kHz).

    ~171 ms at 48/96/192/384/768 kHz (186 ms at 44.1 kHz family), native
    rate, no decimation. Bass narrower than a bin is interpolated, as in
    audioMotion; every band shares one time response.
    """
    return 1 << max(13, min(17, round(math.log2(rate * 8192 / 48000))))


class FFTWindow:
    def __init__(self, size, channel_layout=DEFAULT_CHANNEL_LAYOUT):
        if channel_layout not in CHANNEL_LAYOUTS:
            raise ValueError('unsupported channelLayout')
        self.size = size
        self.channel_layout = channel_layout
        try:
            import numpy
            self.np = numpy if hasattr(numpy, 'fft') else None
        except ImportError:
            self.np = None
        if self.np is not None:
            angle = 2 * self.np.pi * self.np.arange(size) / size
            self.window = 0.42 - 0.5 * self.np.cos(angle) + 0.08 * self.np.cos(2 * angle)
            self.gain = 2 / float(self.window.sum())
        else:
            self.window = [0.42 - 0.5 * math.cos(2 * math.pi * i / size)
                           + 0.08 * math.cos(4 * math.pi * i / size) for i in range(size)]
            self.gain = 2 / sum(self.window)
            bits = size.bit_length() - 1
            self.order = [int(f'{i:0{bits}b}'[::-1], 2) for i in range(size)]
            self.twiddles = {length: [cmath.exp(-2j * math.pi * k / length) for k in range(length // 2)]
                             for length in (1 << b for b in range(1, bits + 1))}

    def transform(self, samples):
        mean = sum(samples) / self.size
        values = [(samples[i] - mean) * self.window[i] for i in range(self.size)]
        values = [complex(values[i]) for i in self.order]
        for length, twiddles in self.twiddles.items():
            half = length // 2
            for start in range(0, self.size, length):
                for offset, factor in enumerate(twiddles):
                    i = start + offset
                    product = factor * values[i + half]
                    values[i + half] = values[i] - product
                    values[i] += product
        return values[:self.size // 2 + 1]

    def power(self, pcm, response=None, gains=None):
        """Power spectrum; with response (H[out][in][bin]) and gains, of the DSP output."""
        if self.np is not None and (response is not None or gains is not None):
            np = self.np
            samples = np.frombuffer(pcm, dtype='<i4').reshape((-1, 2)).astype(float) / 2147483648
            spectra = []
            for channel in (0, 1):
                values = samples[:, channel] - samples[:, channel].mean()
                spectra.append(np.fft.rfft(values * self.window))
            gains = gains or (1.0, 1.0)
            out = [(response[c, 0] * spectra[0] + response[c, 1] * spectra[1]) if response is not None else spectra[c]
                   for c in (0, 1)]
            out = [out[c] * gains[c] for c in (0, 1)]
            if self.channel_layout == 'single':
                power = np.abs((out[0] + out[1]) / 2) ** 2
            else:
                power = (np.abs(out[0]) ** 2 + np.abs(out[1]) ** 2) / 2
            return power * self.gain ** 2
        if self.np is not None:
            np = self.np
            samples = np.frombuffer(pcm, dtype='<i4').reshape((-1, 2))
            power = None
            channels = [samples.mean(axis=1)] if self.channel_layout == 'single' else [samples[:, 0], samples[:, 1]]
            for channel in channels:
                # Contiguous per-channel transforms avoid the cost of a large
                # strided 2-D FFT at 768 kHz. DC removal only affects analysis.
                values = channel.astype(float) / 2147483648
                values -= values.mean()
                transformed = np.abs(np.fft.rfft(values * self.window)) ** 2
                power = transformed if power is None else power + transformed
            return power * (self.gain ** 2 / len(channels))
        stereo = list(struct.iter_unpack('<ii', pcm))
        if self.channel_layout == 'single':
            mono = self.transform([(a + b) / 4294967296 for a, b in stereo])
            return [abs(value) ** 2 * self.gain ** 2 for value in mono]
        left = self.transform([p[0] / 2147483648 for p in stereo])
        right = self.transform([p[1] / 2147483648 for p in stereo])
        # single matches WebAudio's mono (L+R)/2 downmix, including cancellation.
        # audioMotion dual-combined instead analyzes L/R separately and overlays
        # them. Our one-array protocol uses their mean power, (|L|²+|R|²)/2:
        # phase-safe and unity for equal channels, without the max-selection
        # noise bias. This is an energy composite, not audioMotion's two traces.
        return [(abs(a) ** 2 + abs(b) ** 2) * self.gain ** 2 / 2 for a, b in zip(left, right)]

    def level(self, power, lo, hi, rate):
        """Match audioMotion: interpolate both band edges and keep inner-bin peaks."""
        last_bin = self.size // 2

        def to_db(value):
            return max(FLOOR, min(0.0, 10 * math.log10(float(value) + 1e-20)))

        def interpolate(frequency):
            position = max(1.0, min(float(last_bin), frequency * self.size / rate))
            lower = min(last_bin - 1, int(position))
            fraction = max(0.0, min(1.0, position - lower))
            lower_db = to_db(power[lower])
            upper_db = to_db(power[min(last_bin, lower + 1)])
            return lower_db + (upper_db - lower_db) * fraction

        low_position = max(1.0, min(float(last_bin), lo * self.size / rate))
        high_position = max(1.0, min(float(last_bin), hi * self.size / rate))
        low_bin = max(1, min(last_bin, math.ceil(low_position)))
        high_bin = max(1, min(last_bin, math.floor(high_position)))
        peak = max(interpolate(lo), interpolate(hi))
        if high_bin >= low_bin:
            values = power[low_bin:high_bin + 1]
            inner = float(self.np.max(values) if self.np is not None else max(values))
            peak = max(peak, to_db(inner))
        return max(FLOOR, min(0.0, peak))



class Analyzer:
    """A single ~171 ms window (see fft_size) for all bands.

    One FFT per analysis is shared by every requested frequency scale; only
    the band aggregation differs between log/bark/linear/mel.
    """
    def __init__(self, rate, frequency_scale='log', channel_layout=DEFAULT_CHANNEL_LAYOUT):
        if frequency_scale not in FREQUENCY_SCALES:
            raise ValueError('unsupported frequencyScale')
        self.rate = rate
        self.frequency_scale = frequency_scale
        self.channel_layout = channel_layout
        self.size = fft_size(rate)
        self.layouts = {}
        self.edges, self.centers = self.layout(frequency_scale)
        self.window = FFTWindow(self.size, channel_layout)
        self.np = self.window.np

    def layout(self, frequency_scale):
        if frequency_scale not in self.layouts:
            edges = frequency_edges(self.rate, frequency_scale)
            self.layouts[frequency_scale] = (edges, band_centers(edges, frequency_scale))
        return self.layouts[frequency_scale]

    def analyze(self, pcm):
        return self.analyze_scales(pcm, (self.frequency_scale,))[self.frequency_scale]

    def analyze_scales(self, pcm, frequency_scales, response=None, gains=None):
        if len(pcm) != self.size * 8:
            raise ValueError('wrong FFT window size')
        frequency_scales = tuple(dict.fromkeys(frequency_scales))
        if any(scale not in FREQUENCY_SCALES for scale in frequency_scales):
            raise ValueError('unsupported frequencyScale')
        if pcm.count(0) == len(pcm):
            return {scale: [FLOOR] * len(self.layout(scale)[1]) for scale in frequency_scales}
        power = self.window.power(pcm, response, gains)
        return {scale: self._bands(scale, power) for scale in frequency_scales}

    def _bands(self, frequency_scale, power):
        edges, _ = self.layout(frequency_scale)
        db = []
        for lo, hi in zip(edges, edges[1:]):
            value = self.window.level(power, lo, hi, self.rate)
            db.append(FLOOR if value <= FLOOR + 1e-8 else value)
        return db


def target(config):
    """(pw-record target, capture a sink's monitor, rate) of what the DSP captures, or None."""
    devices = (config or {}).get('devices') or {}
    capture = devices.get('capture') or {}
    rate = devices.get('capture_samplerate') or devices.get('samplerate')
    device = capture.get('device')
    if capture.get('type') != 'Pulse' or capture.get('channels') != 2 or not device or not rate:
        return None
    if device.endswith('.monitor'):
        return device[:-len('.monitor')], True, int(rate)
    return device, False, int(rate)


def record(node, monitor, rate):
    """A passive PipeWire capture of node at rate, raw S32_LE stereo on stdout."""
    properties = ('{ node.passive = true node.dont-reconnect = true node.force-rate = 0 '
                  'node.name = hifimate-spectrum-capture media.category = Monitor'
                  + (' stream.capture.sink = true' if monitor else '') + ' }')
    return subprocess.Popen(['pw-record', '--target', node, '--rate', str(rate), '--channels', '2',
                             '--format', 's32', '--raw', '--latency', '20ms', '-P', properties, '-'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class CamillaState:
    """The live DSP, read over its websocket (one connection, reopened on error)."""
    def __init__(self, url, connect=None):
        self.url, self.connect, self.ws = url, connect, None

    def _rpc(self, command):
        if self.ws is None:
            if self.connect is None:
                import websocket
                self.connect = lambda url: websocket.create_connection(url, timeout=2)
            self.ws = self.connect(self.url)
        self.ws.send(json.dumps(command))
        reply = json.loads(self.ws.recv())[command]
        if reply.get('result') != 'Ok':
            raise RuntimeError('CamillaDSP refused ' + command)
        return reply.get('value')

    def __call__(self):
        try:
            state = self._rpc('GetState')
            if state not in ('Running', 'Paused'):
                return {'state': state}
            raw = self._rpc('GetConfigJson')
            return {'state': state, 'config': json.loads(raw) if isinstance(raw, str) else raw,
                    'faders': self._rpc('GetFaders'), 'buffer': self._rpc('GetBufferLevel')}
        except Exception:
            self.close()
            return None

    def close(self):
        if self.ws is not None:
            try: self.ws.close()
            except Exception: pass
        self.ws = None


class Spectrum:
    def __init__(self, dsp=None, model=None, fps=60, clock=time.monotonic, *,
                 frequency_scale='log', channel_layout=DEFAULT_CHANNEL_LAYOUT, capture=record):
        if frequency_scale not in FREQUENCY_SCALES:
            raise ValueError('unsupported frequencyScale')
        if channel_layout not in CHANNEL_LAYOUTS:
            raise ValueError('unsupported channelLayout')
        self.frequency_scale = frequency_scale
        self.channel_layout = channel_layout
        self.dsp, self.model, self.capture = dsp, model, capture
        self.process = None
        self.source = None          # (node, monitor, rate) being captured
        self.response = None        # (rate, size, H) for the current pipeline
        self.gains = None
        self.delay = 0              # frames the DAC plays behind the capture
        self.fps = min(60, max(1, fps))
        self.clock = clock
        self.condition = threading.Condition()
        self.lifecycle = threading.Lock()
        self.clients = 0
        # Viewers choose their own axis; the configured scale is always kept.
        self.views = {scale: 0 for scale in FREQUENCY_SCALES}
        self.version = 0
        self.frames = self._empty('idle')
        self.stop = None
        self.threads = []
        self.ring = deque()
        self.total = 0
        self.identity = None
        self.retired = deque(maxlen=32)
        self.next_position = None
        self.last_pcm = 0
        self.generation = 0
        self.received = 0
        self.analyses = 0

    @staticmethod
    def empty(state, rate=None, frequency_scale='log', channel_layout=DEFAULT_CHANNEL_LAYOUT):
        edges = frequency_edges(rate, frequency_scale)
        count = len(edges) - 1
        return {'state': state, 'rate': rate, 'bins': [0.0] * count,
                'db': [FLOOR] * count,
                'frequency_min': FREQUENCY_MIN,
                'frequency_max': edges[-1],
                'centers': [round(c, 1) for c in band_centers(edges, frequency_scale)],
                'frequencyScale': frequency_scale, 'channelLayout': channel_layout,
                'origin': 'dsp-model'}

    @property
    def frame(self):
        return self.frames.get(self.frequency_scale) or next(iter(self.frames.values()))

    def scales(self):
        # Only analyze axes someone is watching; the configured one otherwise.
        return tuple(scale for scale in FREQUENCY_SCALES if self.views[scale]) or (self.frequency_scale,)

    def _empty(self, state, rate=None):
        return {scale: self.empty(state, rate, scale, self.channel_layout) for scale in self.scales()}

    def publish(self, frames, force=False):
        with self.condition:
            # State snapshots can be deduplicated, but each completed analysis
            # is still a liveness event.  A steady tone often produces exactly
            # the same bins for many windows; suppressing those updates makes
            # the browser mistake healthy audio for a dead stream.
            if not force and frames == self.frames:
                return
            self.frames = frames
            self.version += 1
            self.condition.notify_all()

    def clear(self, state='idle'):
        with self.condition:
            self.generation += 1
            self.ring.clear()
            self.total = 0
            self.identity = None
            self.retired.clear()
            self.next_position = None
            self.received += 1
        self.publish(self._empty(state))

    def subscribe(self, frequency_scale=None):
        frequency_scale = frequency_scale or self.frequency_scale
        if frequency_scale not in FREQUENCY_SCALES:
            raise ValueError('unsupported frequencyScale')
        with self.lifecycle:
            if self.clients >= 8:
                raise RuntimeError('too many spectrum viewers')
            with self.condition:
                self.views[frequency_scale] += 1
                if frequency_scale not in self.frames:
                    # Until the next analysis, show the same state on the new axis.
                    current = self.frame
                    self.frames = {**self.frames, frequency_scale: self.empty(
                        current['state'] if current['state'] != 'playing' else 'waiting',
                        current['rate'], frequency_scale, self.channel_layout)}
            if self.clients == 0:
                try:
                    self._start()
                except BaseException:
                    with self.condition:
                        self.views[frequency_scale] -= 1
                    raise
            self.clients += 1

    def unsubscribe(self, frequency_scale=None):
        frequency_scale = frequency_scale or self.frequency_scale
        with self.lifecycle:
            with self.condition:
                self.views[frequency_scale] = max(0, self.views.get(frequency_scale, 0) - 1)
            self.clients = max(0, self.clients - 1)
            if not self.clients:
                self._stop()

    def _start(self):
        self.stop = threading.Event()
        self.clear('waiting')
        self.threads = [threading.Thread(target=self._watch, args=(self.stop,), daemon=True, name='spectrum-watch'),
                        threading.Thread(target=self._analyze, args=(self.stop,), daemon=True, name='spectrum-fft')]
        for thread in self.threads:
            thread.start()

    def _stop(self):
        if self.stop is None:
            return
        self.stop.set()
        self._end_capture()
        for thread in self.threads:
            thread.join(timeout=2)
        if hasattr(self.dsp, 'close'):
            self.dsp.close()
        self.stop = None
        self.threads = []
        self.clear()

    def _end_capture(self):
        process, self.process, self.source = self.process, None, None
        if process is not None:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait()

    def feed_pcm(self, rate, stream, position, pcm):
        """Raw S32_LE stereo frames of capture stream (a new number for each capture)."""
        if not pcm or len(pcm) % 8:
            return
        with self.condition:
            identity = (rate, stream)
            if stream in self.retired:
                return  # late data of a previous capture
            if self.identity and stream != self.identity[1]:
                self.retired.append(self.identity[1])
            if (identity != self.identity or position != self.next_position
                    or self.clock() - self.last_pcm > STALE):
                self.generation += 1
                self.ring.clear()
                self.total = 0
                self.publish(self._empty('waiting', rate))
            self.identity = identity
            self.next_position = position + len(pcm) // 8
            self.ring.append(pcm)
            self.total += len(pcm)
            limit = (fft_size(rate) + int(rate * MAX_DELAY)) * 8
            while self.ring and self.total - len(self.ring[0]) >= limit:
                self.total -= len(self.ring.popleft())
            self.last_pcm = self.clock()
            self.received += 1

    def _read(self, process, rate, stream):
        position = 0
        while True:
            data = process.stdout.read(CHUNK)
            if not data:
                return
            data = data[:len(data) - len(data) % 8]
            self.feed_pcm(rate, stream, position, data)
            position += len(data) // 8

    def _watch(self, stop):
        """Follow the live DSP: what it captures, its pipeline, faders and buffer."""
        streams = 0
        while not stop.is_set():
            live = self.dsp() if self.dsp else None
            config = (live or {}).get('config')
            source = target(config) if config else None
            if source != self.source or (self.process is not None and self.process.poll() is not None):
                self._end_capture()
                if source is not None and not stop.is_set():
                    try:
                        self.process = self.capture(*source)
                    except OSError:
                        self.process = None
                    if self.process is not None:
                        streams += 1
                        self.source = source
                        threading.Thread(target=self._read, args=(self.process, source[2], streams),
                                         daemon=True, name='spectrum-capture').start()
            if source is not None:
                rate = source[2]
                size = fft_size(rate)
                try:
                    h = self.model.response(config, rate, size) if self.model else None
                except Exception:
                    h = None    # shown as the DSP input until the pipeline can be measured
                with self.condition:
                    self.response = (rate, size, h) if h is not None else None
                    from .dsp_model import gains
                    self.gains = gains(config, live.get('faders'))
                    self.delay = min(int(live.get('buffer') or 0), int(rate * MAX_DELAY))
            stop.wait(WATCH)

    def _analyze(self, stop):
        analyzer = None
        analyzed_generation = None
        seen = -1
        next_tick = time.monotonic() + 1 / self.fps
        while not stop.wait(max(0, next_tick - time.monotonic())):
            # Account for FFT time instead of adding it to every frame's wait.
            # If overloaded, discard catch-up work rather than queuing frames.
            next_tick = max(next_tick + 1 / self.fps, time.monotonic())
            with self.condition:
                identity, revision, generation = self.identity, self.received, self.generation
                if not identity or self.clock() - self.last_pcm > STALE:
                    self.publish(self._empty('idle', identity[0] if identity else None))
                    continue
                if revision == seen or self.total < fft_size(identity[0]) * 8:
                    continue
                seen = revision
                size = fft_size(identity[0])
                if self.total < (size + self.delay) * 8:
                    continue
                joined = b''.join(self.ring)
                end = len(joined) - self.delay * 8
                pcm = joined[end - size * 8:end]
                response = self.response[2] if self.response and self.response[:2] == (identity[0], size) else None
                gains = self.gains
            try:
                if analyzer is None or analyzer.rate != identity[0] or analyzed_generation != generation:
                    analyzer = Analyzer(identity[0], self.frequency_scale, self.channel_layout)
                    analyzed_generation = generation
                with self.condition:
                    scales = self.scales()
                levels = analyzer.analyze_scales(pcm, scales, response, gains)
                self.analyses += 1
                with self.condition:
                    # A format change/gap during FFT invalidates the snapshot.
                    if stop.is_set() or generation != self.generation or identity != self.identity or self.clock() - self.last_pcm > STALE:
                        continue
                    frames = {}
                    for scale, db in levels.items():
                        frame = self.empty('playing' if max(db) > FLOOR else 'silent', identity[0],
                                           scale, self.channel_layout)
                        # Full float precision (~18 chars/value) tripled every
                        # frame; 0.1 dB is far below what the LED display resolves.
                        db = [round(v, 1) for v in db]
                        frame.update(db=db, bins=[round((v - FLOOR) / -FLOOR, 4) for v in db])
                        frames[scale] = frame
                # The PCM revision changed and this is a fresh analysis
                # window, even when all numerical bins are equal.
                self.publish(frames, force=True)
            except Exception:
                self.publish(self._empty('unavailable', identity[0]))

    def latest(self, version=-1, timeout=0.5, frequency_scale=None):
        frequency_scale = frequency_scale or self.frequency_scale
        with self.condition:
            if self.version == version:
                self.condition.wait(timeout)
            frame = self.frames.get(frequency_scale)
            if frame is None:  # subscribed after the last state snapshot
                frame = self.empty('waiting', self.frame['rate'], frequency_scale, self.channel_layout)
            return self.version, dict(frame)
