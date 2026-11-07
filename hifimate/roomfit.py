"""Room measurement (roomfit): exponential sweep out of the real chain, phone
recording in, impulse and frequency response back.

The sweep is played by the DLNA MPD from the control service, so it passes
arbitration, CamillaDSP (with its current filters) and the DAC like music.
The phone records the whole take; the inverse-filter deconvolution finds the
sweep wherever it starts, so no clock sync with the phone is needed.
"""
import io
import json
import math
from pathlib import Path
import re
import secrets
import time
import wave

RATE = 48000
F_LOW, F_HIGH = 20.0, 20000.0
SWEEP_SECONDS = 8.0
# Arbitration verifies the new chain and then fades in (default 400 ms, up
# to ~3 s with a rate change); the sweep starts after that.
LEAD_SECONDS = 3.0
TAIL_SECONDS = 2.0
LEVELS = (-40, -6)          # allowed sweep level, dBFS
MAX_UPLOAD = 32 * 1024 * 1024
ID = re.compile(r'^[0-9]{8}-[0-9]{6}-[LR]-[0-9a-f]{6}$')
MICS = Path(__file__).with_name('mics')
NOISE_AFTER_S = 0.8
SNR_BANDS = ((30, 80), (80, 320), (320, 2000), (2000, 8000), (8000, 16000))
SNR_MAIN = (80, 320)   # where room correction works
# Compensation is capped: far below a mic's range it would only amplify noise.
MAX_BOOST_DB = 12.0
# Below the frequency where the mic needs more than this, the curve is shown
# but not trusted (iPhone 15 Pro Max: about 30 Hz; 18 Pro: about 40 Hz).
TRUST_BOOST_DB = 6.0
UNCOMPENSATED_TRUST_HZ = 60.0


def _curves():
    """{curve id: document} for hifimate/mics/*.json (one measured unit each)."""
    return {doc['id']: doc for doc in (json.loads(path.read_text()) for path in sorted(MICS.glob('*.json')))}


def mics():
    """Phones the page offers, newest generation last. Each uses a measured
    curve: its own (measured), or its generation's measured model's."""
    rows = []
    for curve, doc in sorted(_curves().items(), key=lambda item: item[1]['models'][0]['id']):
        for model in doc['models']:
            measured = model['id'] == curve
            rows.append({'id': model['id'], 'curve': curve, 'measured': measured, 'source': doc['source'],
                         'name': model['name'] if measured else f"{model['name']}（参考 {doc['name'].split('（')[0]} 实测）"})
    return rows


def mic_response(mic, freqs):
    """The mic's own response (dB) at freqs, log-interpolated, edges held."""
    n = np()
    if not isinstance(mic, str) or not re.fullmatch(r'[a-z0-9-]{1,64}', mic):
        raise ValueError('无效的麦克风')
    curve = next((row['curve'] for row in mics() if row['id'] == mic), None)
    if curve is None:
        raise ValueError('未知的麦克风')
    doc = _curves()[curve]
    return n.interp(n.log(freqs), n.log(n.array(doc['frequency_hz'])), n.array(doc['response_db']))


def np():
    import numpy
    return numpy


def sweep(level_db, rate=RATE):
    """Farina exponential sine sweep with short raised-cosine fades (mono)."""
    n = np()
    count = int(SWEEP_SECONDS * rate)
    t = n.arange(count) / rate
    k = math.log(F_HIGH / F_LOW)
    signal = n.sin(2 * math.pi * F_LOW * SWEEP_SECONDS / k * (n.exp(t * k / SWEEP_SECONDS) - 1))
    fade = int(0.02 * rate)
    ramp = 0.5 - 0.5 * n.cos(n.linspace(0, math.pi, fade))
    signal[:fade] *= ramp
    signal[-fade:] *= ramp[::-1]
    return signal * 10 ** (level_db / 20)


def inverse(rate=RATE):
    """Time-reversed sweep with the -6 dB/octave envelope that flattens it."""
    n = np()
    reference = sweep(0, rate)
    t = n.arange(len(reference)) / rate
    k = math.log(F_HIGH / F_LOW)
    return reference[::-1] * n.exp(-t * k / SWEEP_SECONDS)


def take_wav(channel, level_db, rate=RATE):
    """The full take as 24-bit stereo WAV: lead silence, sweep on one channel, tail."""
    n = np()
    if channel not in ('L', 'R'):
        raise ValueError('channel must be L or R')
    if not LEVELS[0] <= level_db <= LEVELS[1]:
        raise ValueError('level out of range')
    lead, tail = n.zeros(int(LEAD_SECONDS * rate)), n.zeros(int(TAIL_SECONDS * rate))
    mono = n.concatenate([lead, sweep(level_db, rate), tail])
    stereo = n.zeros((len(mono), 2))
    stereo[:, 0 if channel == 'L' else 1] = mono
    pcm = n.clip(n.round(stereo * 8388607), -8388608, 8388607).astype('<i4')
    raw = pcm.reshape(-1).view(n.uint8).reshape(-1, 4)[:, :3].tobytes()   # little-endian 24-bit
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(3); w.setframerate(rate); w.writeframes(raw)
    return out.getvalue()


def read_recording(data):
    """Mono float samples and rate from an uploaded PCM WAV (16/24/32-bit)."""
    n = np()
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError) as error:
        raise ValueError(f'不是有效的 PCM WAV：{error}') from None
    if width not in (2, 3, 4) or not 8000 <= rate <= 192000 or channels not in (1, 2):
        raise ValueError('不支持的录音格式')
    raw = n.frombuffer(frames, n.uint8)
    if width == 3:
        raw = raw.reshape(-1, 3)
        values = (raw[:, 0].astype(n.int32) | raw[:, 1].astype(n.int32) << 8 | raw[:, 2].astype(n.int32) << 16)
        values = n.where(values >= 1 << 23, values - (1 << 24), values) / 8388608.0
    else:
        values = n.frombuffer(frames, '<i2' if width == 2 else '<i4') / float(1 << (8 * width - 1))
    samples = values.reshape(-1, channels).mean(axis=1)
    if len(samples) < rate * (SWEEP_SECONDS + 1):
        raise ValueError('录音太短，没有包含完整扫频')
    return samples, rate


def resample(samples, rate, target=RATE):
    """Band-limited FFT resampling of a whole take (only if the phone's rate differs)."""
    n = np()
    if rate == target:
        return samples
    count = int(round(len(samples) * target / rate))
    spectrum = n.fft.rfft(samples)
    out = n.zeros(count // 2 + 1, complex)
    keep = min(len(spectrum), len(out))
    out[:keep] = spectrum[:keep]
    return n.fft.irfft(out, count) * count / len(samples)


def smooth(freqs, db, fraction=6):
    """1/fraction-octave smoothing on a log grid (power average)."""
    n = np()
    grid = n.geomspace(F_LOW, F_HIGH, 240)
    power = 10 ** (db / 10)
    out = []
    for f in grid:
        lo, hi = f * 2 ** (-0.5 / fraction), f * 2 ** (0.5 / fraction)
        band = (freqs >= lo) & (freqs <= hi)
        out.append(10 * math.log10(max(power[band].mean(), 1e-20)) if band.any() else -200.0)
    return grid, n.array(out)


def analyze(samples, rate, mic=None):
    """Impulse response by deconvolution, then a windowed frequency response.

    With a known mic its own response is divided out (boost capped).
    """
    n = np()
    x = resample(n.asarray(samples, float), rate)
    peak = float(n.abs(x).max()) if len(x) else 0.0
    inv = inverse()
    size = 1 << math.ceil(math.log2(len(x) + len(inv)))
    ir = n.fft.irfft(n.fft.rfft(x, size) * n.fft.rfft(inv, size), size)[:len(x) + len(inv) - 1]
    # The linear impulse sits SWEEP_SECONDS after the recorded sweep start;
    # harmonic distortion lands earlier and is excluded by the window.
    main = int(n.argmax(n.abs(ir)))
    start = main - int(0.005 * RATE)
    if start < 0 or main + int(0.3 * RATE) > len(ir):
        raise ValueError('录音里没有找到完整的扫频，请确认录音覆盖了整个播放过程')
    before, after = int(0.005 * RATE), int(0.5 * RATE)
    freqs = n.fft.rfftfreq(1 << 16, 1 / RATE)
    window = n.ones(before + after)
    fade_out = int(0.1 * RATE)
    window[:before] = 0.5 - 0.5 * n.cos(n.linspace(0, math.pi, before))
    window[-fade_out:] = 0.5 + 0.5 * n.cos(n.linspace(0, math.pi, fade_out))

    def response(first):
        segment = ir[first:first + before + after]
        spectrum = n.fft.rfft(segment * window[:len(segment)], 1 << 16)
        return smooth(freqs, 20 * n.log10(n.maximum(n.abs(spectrum), 1e-12)))
    grid, smoothed = response(main - before)
    measured = smoothed.copy()   # before mic compensation and normalisation
    # Noise of the deconvolved response itself: the same window placed where
    # the room has decayed (0.8 s on) still holds the fully filtered noise.
    noise_at = main + int(NOISE_AFTER_S * RATE)
    noise_curve = response(noise_at)[1] if noise_at + before + after <= len(ir) else None
    raw = smoothed.copy()
    trusted = UNCOMPENSATED_TRUST_HZ
    if mic:
        own = n.maximum(mic_response(mic, grid), -MAX_BOOST_DB)
        smoothed = smoothed - own
        # The bass limit only: mics also fall off near 20 kHz.
        weak = grid[(own < -TRUST_BOOST_DB) & (grid < 1000)]
        trusted = float(weak.max()) if len(weak) else F_LOW
    # Normalise to the 200 Hz-2 kHz average so takes at different levels compare.
    band = (grid >= 200) & (grid <= 2000)
    smoothed = smoothed - smoothed[band].mean()
    raw = raw - raw[band].mean()
    # Level and noise: the sweep sits LEAD_SECONDS into the take; the start
    # of the recording (before the sweep) is the room noise.
    sweep_start = main - len(inv) + 1
    noise = x[:max(0, sweep_start - int(0.2 * RATE))]
    signal = x[max(0, sweep_start):sweep_start + int(SWEEP_SECONDS * RATE)]
    rms = lambda v: float(n.sqrt(n.mean(v ** 2))) if len(v) else 0.0
    to_db = lambda v: round(20 * math.log10(v), 1) if v > 0 else None
    # SNR of the measured response per band (the raw level ratio understates
    # it by the sweep's processing gain); snr_db is the room-correction band.
    snr_bands, snr = {}, None
    if noise_curve is not None:
        per = measured - noise_curve
        for lo, hi in SNR_BANDS:
            snr_bands[f'{lo}-{hi}'] = round(float(n.median(per[(grid >= lo) & (grid <= hi)])), 1)
        snr = snr_bands[f'{SNR_MAIN[0]}-{SNR_MAIN[1]}']
    return {
        'peak_dbfs': to_db(peak), 'clipped': peak >= 0.99,
        'signal_dbfs': to_db(rms(signal)), 'noise_dbfs': to_db(rms(noise)), 'snr_db': snr, 'snr_bands': snr_bands,
        'sweep_start_s': round(sweep_start / RATE, 3),
        'frequency': [round(float(f), 2) for f in grid],
        'magnitude_db': [round(float(v), 2) for v in smoothed],
        'uncompensated_db': [round(float(v), 2) for v in raw],
        'mic': mic, 'trusted_from_hz': round(trusted, 1),
    }


class Store:
    """One directory per take under prefix/state/roomfit (runtime data, never in the manifest)."""
    def __init__(self, root):
        self.root = Path(root)

    def new(self, channel, level_db, meta=None):
        if channel not in ('L', 'R'):
            raise ValueError('channel must be L or R')
        take = time.strftime('%Y%m%d-%H%M%S') + f'-{channel}-{secrets.token_hex(3)}'
        path = self.root / take
        path.mkdir(parents=True, mode=0o700)
        self.write(take, 'meta.json', json.dumps({'id': take, 'channel': channel, 'level_db': level_db,
            'created': time.time(), 'rate': RATE, 'f_low': F_LOW, 'f_high': F_HIGH,
            'sweep_s': SWEEP_SECONDS, 'lead_s': LEAD_SECONDS, **(meta or {})}, ensure_ascii=False).encode())
        return take

    def path(self, take):
        if not isinstance(take, str) or not ID.match(take):
            raise ValueError('无效的测量编号')
        path = self.root / take
        if not path.is_dir():
            raise ValueError('测量不存在')
        return path

    def write(self, take, name, data):
        target = self.path(take) / name
        temporary = target.with_name('.' + name + '.tmp')
        temporary.write_bytes(data); temporary.chmod(0o600); temporary.replace(target)

    def meta(self, take):
        return json.loads((self.path(take) / 'meta.json').read_text())

    def update(self, take, **values):
        meta = {**self.meta(take), **values}
        self.write(take, 'meta.json', json.dumps(meta, ensure_ascii=False).encode())
        return meta

    def list(self, limit=40):
        if not self.root.is_dir():
            return []
        rows = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if ID.match(path.name) and (path / 'meta.json').is_file():
                meta = json.loads((path / 'meta.json').read_text())
                rows.append({k: meta.get(k) for k in ('id', 'channel', 'level_db', 'created', 'analysis', 'device')})
            if len(rows) >= limit:
                break
        return rows


def play(url, connect=None):
    """Queue and start the take on the DLNA MPD; returns the MPD song id."""
    from .mpd import MPD, PORTS
    connect = connect or (lambda: MPD(PORTS['dlna'], timeout=3))
    with connect() as mpd:
        song = mpd.command(f'addid "{url}"')['Id']
        mpd.command(f'playid {song}')
        return song


def remove(song, connect=None):
    """Drop the take from the DLNA MPD queue once it has finished."""
    from .mpd import MPD, PORTS
    connect = connect or (lambda: MPD(PORTS['dlna'], timeout=3))
    with connect() as mpd:
        status = mpd.command('status')
        if status.get('songid') == str(song) and status.get('state') == 'play':
            mpd.command('stop')
        mpd.command(f'deleteid {song}')


# --- Cut-only room correction ------------------------------------------------
FIT_LOW, FIT_HIGH = 35.0, 350.0
FIT_MAX_FILTERS, FIT_MAX_CUT_DB, FIT_MIN_PEAK_DB = 8, 10.0, 1.5
# A single position: narrow detail moves with the phone, so the fit sees a
# 1/3-octave curve, Q stays moderate, and a filter must earn its place.
FIT_Q = (0.7, 5.0)
FIT_SMOOTH_OCTAVES = 1 / 3
FIT_MIN_GAIN = 0.10     # a new filter must cut the remaining cost by 10 %
FIT_MIN_SPACING = 1 / 6  # octaves between filter centres at placement


def smooth_octaves(freqs, db, width):
    """Power average over +-width/2 octaves on an existing log grid."""
    n = np()
    f = n.asarray(freqs, float); power = 10 ** (n.asarray(db, float) / 10)
    return n.array([10 * math.log10(power[(f >= x * 2 ** (-width / 2)) & (f <= x * 2 ** (width / 2))].mean()) for x in f])


def house_target(freqs, bass_db=3.0, knee_low=60.0, knee_high=200.0):
    """Gentle in-room bass rise: +bass_db below knee_low, 0 dB above knee_high."""
    n = np()
    t = (n.log(knee_high) - n.log(n.clip(freqs, knee_low, knee_high))) / (n.log(knee_high) - n.log(knee_low))
    return bass_db * t


def peaking_db(freqs, f0, gain_db, q, rate=RATE):
    """RBJ peaking biquad magnitude in dB (what CamillaDSP's Biquad Peaking computes)."""
    n = np()
    a = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * f0 / rate
    alpha = math.sin(w0) / (2 * q)
    b = n.array([1 + alpha * a, -2 * math.cos(w0), 1 - alpha * a])
    den = n.array([1 + alpha / a, -2 * math.cos(w0), 1 - alpha / a])
    z = n.exp(-1j * 2 * math.pi * n.asarray(freqs) / rate)
    h = (b[0] + b[1] * z + b[2] * z * z) / (den[0] + den[1] * z + den[2] * z * z)
    return 20 * n.log10(n.abs(h))


def fit_peq(freqs, measured_db, target_db=None, low=FIT_LOW, high=FIT_HIGH,
            max_filters=FIT_MAX_FILTERS, max_cut=FIT_MAX_CUT_DB, min_peak=FIT_MIN_PEAK_DB):
    """Peaking cuts that bring peaks above the target down, never deepening dips.

    Cost per point in [low, high]: (above target)^2 + 2 * (pushed below
    min(measured, target))^2, so a dip is left as measured. Greedy placement
    on the largest remaining excess, then coordinate refinement of all
    filters. Returns [{'freq', 'gain', 'q'}] sorted by frequency.
    """
    n = np()
    f = n.asarray(freqs, float); m = smooth_octaves(f, measured_db, FIT_SMOOTH_OCTAVES)
    t = house_target(f) if target_db is None else n.asarray(target_db, float)
    sel = (f >= low) & (f <= high)
    f, m, t = f[sel], m[sel], t[sel]
    floor = n.minimum(m, t)

    def total(filters):
        return sum((peaking_db(f, x['freq'], x['gain'], x['q']) for x in filters), n.zeros_like(f))

    def cost(filters):
        y = m + total(filters)
        return float(n.sum(n.maximum(y - t, 0) ** 2) + 2 * n.sum(n.maximum(floor - y, 0) ** 2))

    def clamp(x):
        return {'freq': float(n.clip(x['freq'], low, high)), 'gain': float(n.clip(x['gain'], -max_cut, 0)),
                'q': float(n.clip(x['q'], *FIT_Q))}

    def refine(filters, rounds=40):
        best = cost(filters)
        steps = {'freq': 1.06, 'gain': 0.5, 'q': 1.25}
        for _ in range(rounds):
            improved = False
            for i in range(len(filters)):
                for key, step in steps.items():
                    for direction in (1, -1):
                        trial = [dict(x) for x in filters]
                        x = trial[i]
                        x[key] = x[key] * step ** direction if key != 'gain' else x[key] + step * direction
                        trial[i] = clamp(x)
                        c = cost(trial)
                        if c < best - 1e-9:
                            filters, best, improved = trial, c, True
            if not improved:
                steps = {k: (v ** 0.5 if k != 'gain' else v / 2) for k, v in steps.items()}
                if steps['gain'] < 0.05:
                    break
        return filters

    filters = []
    for _ in range(max_filters):
        excess = m + total(filters) - t
        # The largest excess not already under a filter's centre.
        free = n.array([all(abs(math.log2(x / y['freq'])) >= FIT_MIN_SPACING for y in filters) for x in f])
        if not free.any():
            break
        i = int(n.argmax(n.where(free, excess, -n.inf)))
        if excess[i] < min_peak:
            break
        # Initial width from where the excess falls to half its height.
        half = excess[i] / 2
        lo = i
        while lo > 0 and excess[lo] > half: lo -= 1
        hi = i
        while hi < len(f) - 1 and excess[hi] > half: hi += 1
        octaves = max(math.log2(f[hi] / f[lo]), 0.1)
        q = math.sqrt(2 ** octaves) / (2 ** octaves - 1)
        before = cost(filters)
        trial = refine(filters + [clamp({'freq': f[i], 'gain': -excess[i], 'q': q})])
        if cost(trial) > before * (1 - FIT_MIN_GAIN):
            break
        filters = trial
    filters = [x for x in filters if x['gain'] <= -0.5]
    return sorted(({'freq': round(x['freq'], 1), 'gain': round(x['gain'], 1), 'q': round(x['q'], 2)} for x in filters),
                  key=lambda x: x['freq'])


# --- Guided measurement: session, verdicts, level, average, profiles -----------
POSITIONS = (
    ('center', '中心（黄金位置）', '你平时听音时头部所在的位置，耳朵高度。'),
    ('left', '左 30 cm', '从中心向左平移约 30 cm（大约一个肩宽），高度不变。'),
    ('right', '右 30 cm', '从中心向右平移约 30 cm，高度不变。'),
    ('front', '前 30 cm', '从中心向音箱方向移动约 30 cm。'),
    ('back', '后 30 cm', '从中心向后（远离音箱）移动约 30 cm。'),
)
MIN_POSITIONS = 3                 # center + two more
CENTER_WEIGHT = 2.0
LEVEL_START_DB = -24.0
TARGET_SNR_DB, GOOD_SNR_DB, BAD_SNR_DB = 38.0, 32.0, 25.0
MAX_RECORDED_PEAK_DBFS = -6.0
SPREAD_WARN_DB = 8.0              # rms difference to the center in 80-320 Hz
PROFILES_KEPT = 3
PROFILE_NAME_MAX = 24
SESSION_ID = re.compile(r'^s-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$')
PROFILE_ID = re.compile(r'^p-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$')
PROCESSING = ('echoCancellation', 'noiseSuppression', 'autoGainControl')


def band_rms(freqs, a, b, low=80.0, high=320.0):
    n = np()
    f = n.asarray(freqs); sel = (f >= low) & (f <= high)
    return float(n.sqrt(n.mean((n.asarray(a)[sel] - n.asarray(b)[sel]) ** 2)))


def judge(result, device=None, reference=None):
    """Is a take usable? grade good / warn / bad, with the reasons in plain words."""
    reasons, grade = [], 'good'
    def mark(level, text):
        nonlocal grade
        reasons.append(text)
        if level == 'bad' or grade == 'good':
            grade = level
    flags = [k for k in PROCESSING if str((device or {}).get(k, '')).lower() == 'true']
    if flags:
        mark('bad', 'iPhone 开启了音频处理（' + '、'.join(flags) + '），录音会失真')
    if result.get('clipped'):
        mark('bad', '录音削波：扫频太响')
    snr = result.get('snr_db')
    if snr is None:
        mark('bad', '无法估计信噪比')
    elif snr < BAD_SNR_DB:
        mark('bad', f'信噪比 {snr} dB 太低：环境太吵，或测量时有人说话、走动')
    elif snr < GOOD_SNR_DB:
        mark('warn', f'信噪比 {snr} dB 偏低，可以用；保持安静重录会更准')
    if reference is not None:
        spread = band_rms(result['frequency'], result['magnitude_db'], reference['magnitude_db'])
        if spread > SPREAD_WARN_DB:
            mark('warn', f'和中心位置差异很大（{spread:.1f} dB）：是否移得太远，或测量时有干扰？')
    return {'grade': grade, 'reasons': reasons}


def recommend_level(result, level_db):
    """The sweep level that reaches TARGET_SNR_DB without nearing clipping (SNR follows the level 1:1)."""
    snr = result.get('snr_db') or 0.0
    peak = result.get('peak_dbfs')
    delta = TARGET_SNR_DB - snr
    if peak is not None:
        delta = min(delta, MAX_RECORDED_PEAK_DBFS - peak)
    level = max(LEVELS[0], min(LEVELS[1], round(level_db + delta)))
    expected = snr + (level - level_db)
    return {'level_db': float(level), 'expected_snr_db': round(expected, 1), 'enough': expected >= GOOD_SNR_DB}


def undo_eq(freqs, magnitude_db, channel, eq):
    """A take measured with room correction active, as the bare room."""
    n = np()
    total = n.zeros(len(freqs))
    for item in (eq or {}).get(channel, []):
        total = total + peaking_db(n.asarray(freqs), item['freq'], item['gain'], item['q'])
    return n.asarray(magnitude_db) - total


def average(curves, weights):
    """Weighted mean in dB (each position's 1/6-octave response)."""
    n = np()
    w = n.asarray(weights, float)
    return (n.asarray(curves) * w[:, None]).sum(axis=0) / w.sum()


class Sessions:
    """Guided measurement sessions under state/roomfit/sessions (one JSON each)."""
    def __init__(self, root):
        self.root = Path(root) / 'sessions'

    def path(self, sid):
        if not isinstance(sid, str) or not SESSION_ID.match(sid):
            raise ValueError('无效的测量会话')
        return self.root / f'{sid}.json'

    def new(self, mic):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        sid = 's-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        session = {'id': sid, 'created': time.time(), 'mic': mic, 'level_db': None, 'level': None, 'state': 'level',
                   'positions': [{'key': k, 'label': label, 'hint': hint, 'status': 'todo', 'takes': {}, 'verdicts': {}}
                                 for k, label, hint in POSITIONS],
                   'computed': None, 'profile': None}
        self.save(session)
        return session

    def load(self, sid):
        path = self.path(sid)
        if not path.is_file():
            raise ValueError('测量会话不存在')
        return json.loads(path.read_text())

    def save(self, session):
        path = self.path(session['id'])
        temporary = path.with_name('.' + path.name + '.tmp')
        temporary.write_text(json.dumps(session, ensure_ascii=False)); temporary.chmod(0o600); temporary.replace(path)

    def latest(self):
        """The newest session that has not been applied or abandoned."""
        if not self.root.is_dir():
            return None
        for path in sorted(self.root.glob('s-*.json'), reverse=True):
            session = json.loads(path.read_text())
            if session['state'] not in ('applied', 'abandoned'):
                return session
        return None

    @staticmethod
    def position(session, key):
        found = next((p for p in session['positions'] if p['key'] == key), None)
        if not found:
            raise ValueError('无效的测量位置')
        return found


class Profiles:
    """Applied corrections under state/roomfit/profiles; the newest PROFILES_KEPT stay."""
    def __init__(self, root):
        self.root = Path(root) / 'profiles'

    def path(self, pid):
        if not isinstance(pid, str) or not PROFILE_ID.match(pid):
            raise ValueError('无效的校正方案')
        return self.root / f'{pid}.json'

    def add(self, profile_fields):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        pid = 'p-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        profile = {'id': pid, 'created': time.time(), **profile_fields}
        path = self.path(pid)
        temporary = path.with_name('.' + path.name + '.tmp')
        temporary.write_text(json.dumps(profile, ensure_ascii=False)); temporary.chmod(0o600); temporary.replace(path)
        for old in sorted(self.root.glob('p-*.json'), reverse=True)[PROFILES_KEPT:]:
            old.unlink()
        return profile

    def load(self, pid):
        path = self.path(pid)
        if not path.is_file():
            raise ValueError('校正方案不存在')
        return json.loads(path.read_text())

    def list(self):
        if not self.root.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.root.glob('p-*.json'), reverse=True)]

    def rename(self, pid, name):
        """A display name (trimmed, at most PROFILE_NAME_MAX characters); empty clears it."""
        if not isinstance(name, str):
            raise ValueError('名称须为文字')
        name = ' '.join(name.split())
        if len(name) > PROFILE_NAME_MAX:
            raise ValueError(f'名称最多 {PROFILE_NAME_MAX} 个字')
        profile = self.load(pid)
        if name:
            profile['name'] = name
        else:
            profile.pop('name', None)
        path = self.path(pid)
        temporary = path.with_name('.' + path.name + '.tmp')
        temporary.write_text(json.dumps(profile, ensure_ascii=False)); temporary.chmod(0o600); temporary.replace(path)
        return profile

    def delete(self, pid):
        path = self.path(pid)
        if not path.is_file():
            raise ValueError('校正方案不存在')
        path.unlink()
