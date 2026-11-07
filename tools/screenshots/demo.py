"""A HiFiMate control page with made-up data, for the README screenshots.

    python3 tools/screenshots/demo.py [--port 5099] [--camilladsp /opt/hifimate/bin/hifimate-camilladsp]

Serves /ctrl/ (the real ctrl App) and /_hifimate/ (the real spectrum service)
on one port. Only the data is invented: a receiver called HiFiMate playing an
invented track at 192 kHz into a "USB DAC", sample EQ and room correction, and
a synthetic piece of music fed into the real spectrum analysis. Nothing of the
host it runs on is shown and nothing plays; the password is demo-pass.
The EQ page draws its curves with an installed CamillaDSP (--camilladsp).
"""
import argparse
import http.client
import json
import shutil
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from hifimate import room_eq, roomfit, spectrum_proxy  # noqa: E402
from hifimate.ctrl import App, Server  # noqa: E402
from hifimate.ctrl_auth import Auth  # noqa: E402
from hifimate.dsp_model import ResponseModel  # noqa: E402
from hifimate.spectrum import Spectrum  # noqa: E402

RATE = 192000
PASSWORD = 'demo-pass'
STARTED = time.time()
TRACK = {'title': '远方的灯塔', 'artist': 'HiFiMate Demo', 'album': '示例专辑', 'duration': 252.0}
# Original lines written for this demo (timed in seconds).
LYRICS = [[0.0, '夜色落在海面上'], [6.0, '风把旧歌轻轻唱'], [12.0, '远方的灯塔亮了'], [18.0, '照着回家的方向'],
          [24.0, '每一个音符都清亮'], [30.0, '像星光落进心上']]

FILTERS = """\
filters:
  speaker-1: {type: Biquad, parameters: {type: Peaking, freq: 48, gain: 3.0, q: 1.4}}
  speaker-2: {type: Biquad, parameters: {type: Peaking, freq: 110, gain: -2.5, q: 2.0}}
  speaker-3: {type: Biquad, parameters: {type: Peaking, freq: 320, gain: -1.5, q: 1.2}}
  presence: {type: Biquad, parameters: {type: Peaking, freq: 2800, gain: 1.2, q: 1.5}}
  sibilance: {type: Biquad, parameters: {type: Peaking, freq: 6800, gain: -2.0, q: 4.0}}
  air_shelf: {type: Biquad, parameters: {type: Highshelf, freq: 9000, gain: 2.5, q: 0.7}}
pipeline:
  - {type: Filter, channels: [0, 1], names: [speaker-1, speaker-2, speaker-3]}
  - {type: Filter, channels: [0, 1], names: [presence, sibilance, air_shelf]}
"""
ROOM = None   # the fitted sample (measure()), as config/room-correction.json has it
RATES = [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000, 705600, 768000]
SERVICES = [  # (id, group, cpu, rt, can_stop)
    ('hifimate', '音频核心', 0.4, None, True), ('hifimate-camilladsp', '音频核心', 6.8, 'RR 5', True),
    ('pipewire', '音频核心', 2.9, 'FIFO 5 · 1 线程', False), ('wireplumber', '音频核心', 0.1, 'FIFO 5 · 1 线程', False),
    ('hifimate-mpd', '接收端', 0.0, 'RR 5', True), ('hifimate-dlna', '接收端', 0.1, None, True),
    ('hifimate-music-mpd', '接收端', 1.8, 'RR 5', True), ('hifimate-airplay', '接收端', 0.0, 'RR 5', True),
    ('hifimate-airplay-net', '接收端', 0.0, None, True), ('hifimate-bluetooth', '接收端', 0.0, None, True),
    ('hifimate-ctrl', '界面与 Music Assistant', 0.1, None, False), ('hifimate-spectrum', '界面与 Music Assistant', 1.2, None, True),
    ('music-assistant', '界面与 Music Assistant', 4.5, None, True)]


def prepare(root):
    """The prefix the ctrl App reads: shipped config plus the sample EQ and room correction."""
    config = root / 'config'
    (config / 'host-stack').mkdir(parents=True)
    shutil.copy(ROOT / 'config/hifimate.conf', config)
    shutil.copy(ROOT / 'config/host-stack/camilladsp.yml', config / 'host-stack')
    (config / 'dsp-filters.yml').write_text(FILTERS)
    measure(root)
    (config / 'identity.json').write_text('{"schema": 1, "name": "HiFiMate"}\n')
    ma = root / 'music-assistant'
    (ma / 'provider-subscriber').mkdir(parents=True)
    (ma / '.hifimate-managed').write_text('hifimate-music-assistant-compose-v1\n')
    (ma / '.env').write_text('MUSIC_LIBRARY_DIR=/srv/hifimate/music\nMUSIC_ASSISTANT_DATA_DIR=/var/lib/hifimate/music-assistant\n'
                             f'MUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_CONFIG_DIR={ma}/provider-subscriber\n')
    (ma / 'provider-subscriber/options.json').write_text(json.dumps(
        {'sources': ['andychao2024/music-assistant-providers'], 'interval_minutes': 360, 'prune_removed': False}))
    (ma / 'provider-subscriber/state.json').write_text(json.dumps({'sources': {
        'github:andychao2024/music-assistant-providers': {
            'ref': 'v3.6.2', 'providers': ['douban_metadata', 'gd_studio_music', 'lxmusic', 'musicbrainz', 'netease_lyrics'],
            'synced_at': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(STARTED - 5400))}}}))


def measure(root):
    """A room correction made by HiFiMate's own fitting from made-up measurements: two saved
    versions (the newer one in use) and an unfinished session showing its computed result."""
    global ROOM
    np_ = roomfit.np()
    f = np_.geomspace(20, 20000, 480)
    rng = np.random.default_rng(3)
    modes = [(42, 7.0, 4.5), (68, 5.0, 6.0), (115, 4.0, 5.0), (180, 3.0, 4.0)]
    dips = [(90, -6.0, 7.0), (150, -4.5, 6.0), (260, -3.0, 4.0)]

    def position(shift, scale):
        db = 1.5 - 0.8 * np_.log2(f / 1000) * (f > 1000)              # gentle treble tilt
        for freq, gain, q in modes:
            db += roomfit.peaking_db(f, freq * shift, gain * scale, q)
        for freq, gain, q in dips:
            db += roomfit.peaking_db(f, freq / shift, gain * (2 - scale), q)
        ripple = np_.convolve(rng.standard_normal(len(f)), np_.ones(9) / 9, 'same')
        return db + ripple * 1.2

    keys = ['center', 'left', 'right', 'front']
    computed = {'positions': keys, 'channels': {}, 'frequency': [round(float(v), 2) for v in f]}
    for channel, offset in (('L', 1.0), ('R', 1.04)):
        curves = [position(offset * s, k) for s, k in ((1.0, 1.0), (0.97, 1.1), (1.03, 0.9), (1.01, 1.05))]
        mean = roomfit.average(curves, [roomfit.CENTER_WEIGHT, 1.0, 1.0, 1.0])
        filters = roomfit.fit_peq(f, mean)
        after = mean + sum((roomfit.peaking_db(f, x['freq'], x['gain'], x['q']) for x in filters), np_.zeros(len(f)))
        computed['channels'][channel] = {'filters': filters, 'before': [round(float(v), 2) for v in mean],
                                         'after': [round(float(v), 2) for v in after],
                                         'spread_db': [round(roomfit.band_rms(f, c, mean), 1) for c in curves]}
    computed['target'] = [round(float(v), 2) for v in roomfit.house_target(f)]
    state = root / 'state/roomfit'
    channels = {ch: computed['channels'][ch]['filters'] for ch in ('L', 'R')}
    profiles = roomfit.Profiles(state)
    older = profiles.add({'session': 's-demo-1', 'mic': 'iphone-16-pro', 'level_db': -18.0, 'positions': keys[:3],
                          'channels': {ch: x[:-1] for ch, x in channels.items()}})
    profiles.rename(older['id'], '书房')
    time.sleep(1.1)                                                    # profile ids carry the second
    current = profiles.add({'session': 's-demo-2', 'mic': 'iphone-16-pro', 'level_db': -18.0, 'positions': keys,
                            'channels': channels})
    profiles.rename(current['id'], '客厅 · 沙发中间')
    ROOM = {'enabled': True, 'profile': current['id'], 'created': current['created'],
            'source': 'roomfit 示例：中心、左、右、前', 'channels': channels}
    room_eq.save(root / 'config/room-correction.json', ROOM)
    sessions = roomfit.Sessions(state)
    session = sessions.new('iphone-16-pro')
    session.update(level_db=-18.0, state='computed', computed=computed)
    for p in session['positions']:
        p['status'] = 'accepted' if p['key'] in keys else 'todo'
    sessions.save(session)


class Collector:
    """What the ctrl page shows: playing the demo track from Music Assistant."""
    def __init__(self):
        self.lock = threading.RLock(); self.at = 0; self.events = []
        self.nowplaying = self

    def command(self, *_):           # transport: the demo keeps playing
        return {'ok': True}

    def snapshot(self, force=False):
        now = time.time()
        lane = lambda source, **live: {'source': source, 'lane': source + '_in', 'has_data': False, 'count': 0,
                                             'rate': None, 'container': None, 'name': '', 'codec': '', 'available': True,
                                             'active': False, 'can_switch': False, **live}
        return {
            'time': now,
            'controller': {'ok': True, 'room': room_eq.summary(ROOM), 'room_error': None,
                           'filters': {'configured': True, 'filters': 6}, 'filters_error': None, 'filters_bypassed': False,
                           'ready': True, 'owner': 'epoch/3/music_assistant_dlna', 'candidate': None, 'revision': 3,
                           'error': None, 'fade': 'released', 'effective': None, 'remote_pause': [],
                           'switchable': ['music_assistant_dlna'],
                           'format': {'rate': RATE, 'bits': 32, 'channels': 2, 'container': 'S32LE'}},
            'lanes': [lane('dlna'), lane('music_assistant_dlna', has_data=True, count=1, rate=RATE, container='s32le',
                                             name='HiFiMate', codec='pcm', active=True),
                      lane('airplay'), lane('bluetooth')],
            'outputs': [{'device': 'hw:CARD=DAC,DEV=0', 'kind': 'usb', 'name': 'USB DAC', 'rates': RATES, 'selected': True},
                        {'device': 'hw:CARD=PCH,DEV=0', 'kind': 'builtin', 'name': '本机声卡 / Analog',
                         'rates': [44100, 48000, 88200, 96000, 192000], 'selected': False}],
            'now': {'source': 'music_assistant_dlna', **TRACK, 'state': 'play',
                    'position': 14.0 + (now - STARTED) % 200, 'controls': {'toggle': True, 'next': True, 'previous': True, 'seek': True},
                    'reason': None, 'origin': None, 'origin_id': None},
            'dsp': {'state': 'Running', 'volume': -8.0, 'mute': False, 'load': 2.1, 'clipped': 0},
            'services': [{'id': i, 'group': g, 'state': 'active', 'detail': '5 / 5 容器' if i == 'music-assistant' else 'running',
                          'uptime': 6 * 86400 + 3600 * 5 + (now - STARTED), 'restarts': 0, 'cpu': cpu, 'rt': rt, 'can_stop': stop}
                         for i, g, cpu, rt, stop in SERVICES],
            'pairing': {'active': False, 'until': 0},
            'events': [{'id': f'demo-{n}', 'time': STARTED - age, 'unit': 'hifimate', 'priority': 6, 'message': text, 'kind': kind}
                       for n, (age, text, kind) in enumerate([(4, '当前输入：music_assistant_dlna · 192 kHz', 'owner'),
                                                              (5, 'DAC 切换到 192 kHz / S32_LE', 'format'),
                                                              (620, '当前输入：airplay · 44.1 kHz', 'owner')])],
            'errors': {},
            'system': {'host': 'hifimate', 'name': 'HiFiMate', 'kernel': '6.12-rt-amd64', 'uptime': 6 * 86400 + 3600 * 5,
                       'cpu': 4.2, 'memory': {'used': 1_350_000_000, 'total': 4_000_000_000}, 'commit': 'demo'},
        }

    def add_event(self, message, **kwargs): self.events.append({'message': message, **kwargs})
    def format_path(self): return '/nonexistent/format.sock'
    def pairing(self): return {'active': False, 'until': 0}
    def logs(self, unit='', errors=False, lines=80):
        return [{'time': STARTED - 60 * i, 'unit': 'hifimate', 'priority': 6, 'message': m}
                for i, m in enumerate(['当前输入：music_assistant_dlna · 192 kHz', 'DSP 运行中 · 192000 Hz', '淡入完成'])]


class Lyrics:
    def for_track(self, now):
        return {'id': 'demo', 'source': 'demo', 'lines': [[t + k * 36.0, text] for k in range(8) for t, text in LYRICS],
                'words': False, 'reason': None}


class Music:
    """16 s of made-up music, looped: bass, chords and a melody with harmonics, drums,
    over a pink noise floor that rolls off above 20 kHz (made once, seamless by FFT)."""
    CHORDS = [(110.0, 130.81, 164.81, 220.0), (87.31, 130.81, 174.61, 220.0),
              (98.0, 146.83, 196.0, 246.94), (82.41, 123.47, 164.81, 207.65)]
    MELODY = [659.25, 587.33, 523.25, 587.33, 659.25, 783.99, 659.25, 587.33]
    loops = {}

    def __init__(self, rate):
        self.rate, self.n, self.done = rate, 0, False
        self.stdout, self.start = self, time.monotonic()
        if rate not in self.loops:
            self.loops[rate] = self.compose(rate)
        self.pcm = self.loops[rate]

    @classmethod
    def compose(cls, rate, seconds=16):
        n = rate * seconds
        t = np.arange(n) / rate
        rng = np.random.default_rng(7)
        x = np.zeros(n)

        def voice(start, length, freq, level, harmonics, decay, bright=1.1):
            a, b = int(start * rate), min(n, int((start + length) * rate))
            tt = t[a:b] - start
            env = np.minimum(1, tt / 0.01) * np.exp(-tt * decay)
            vib = 1 + 0.002 * np.sin(2 * np.pi * 5.5 * tt)
            for h in range(1, harmonics + 1):
                if freq * h > rate / 2.2: break
                x[a:b] += level / h ** bright * env * np.sin(2 * np.pi * freq * h * vib * tt + h)

        for bar, chord in enumerate(cls.CHORDS):
            for k, f in enumerate(chord):                        # chord: slow decay, many harmonics
                voice(bar * 4, 4, f * (1 + 0.0015 * k), 0.10, 24, 0.4, 1.3)
            for beat in range(8):                                # bass on every half note
                voice(bar * 4 + beat * 0.5, 0.5, chord[0] / 2, 0.30, 8, 3.0, 1.6)
        for i in range(seconds * 2):                             # melody: eighth notes
            voice(i * 0.5, 0.5, cls.MELODY[i % len(cls.MELODY)], 0.08, 18, 4.0, 1.0)
        beat = t * 2 % 1                                         # 120 bpm
        kick = np.exp(-beat * 16)
        x += 0.55 * kick * np.sin(2 * np.pi * (45 + 70 * kick) * t)
        # Pink noise, a little brighter in the hats' rhythm, gone above ~22 kHz.
        f = np.fft.rfftfreq(n, 1 / rate)
        shape = np.where(f > 15, 1 / np.sqrt(np.maximum(f, 15)), 0) / (1 + (f / 21000) ** 10)
        noise = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n)
        noise /= np.max(np.abs(noise))
        hats = np.exp(-(t * 4 % 1) * 25)
        x += noise * (0.04 + 0.10 * hats)
        x = np.tanh(x * 0.9) * 0.6
        stereo = np.stack([x, np.roll(x, int(rate * 0.0004))], axis=1)
        return (stereo * (2 ** 31 - 1)).astype('<i4').tobytes()

    def read(self, size):
        if self.done:
            return b''                                                   # terminated: EOF, like pw-record
        frames = size // 8
        time.sleep(max(0.0, self.start + self.n / self.rate - time.monotonic()))   # real time, like pw-record
        at = self.n * 8 % len(self.pcm)
        self.n += frames
        data = self.pcm[at:at + frames * 8]
        return data + self.pcm[:frames * 8 - len(data)]

    def poll(self): return 0 if self.done else None
    def terminate(self): self.done = True
    def kill(self): self.done = True
    def wait(self, timeout=None): return 0


class Flat:
    def response(self, config, rate, size):
        h = np.zeros((2, 2, size // 2 + 1), dtype=complex)
        h[0, 0] = h[1, 1] = 1.0
        return h


def dsp():
    return {'state': 'Running', 'buffer': 0,
            'faders': [{'volume': 0.0, 'mute': False}] + [{'volume': 0.0, 'mute': False}] * 4,
            'config': {'devices': {'samplerate': RATE, 'capture_samplerate': RATE,
                                   'capture': {'type': 'Pulse', 'device': 'music_assistant_dlna_in.monitor', 'channels': 2}},
                       'filters': {}, 'pipeline': []}}


class Front(BaseHTTPRequestHandler):
    """One origin for the browser: /_hifimate/ to the spectrum server, the rest to ctrl (streams SSE through)."""
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_): pass

    def proxy(self):
        port = self.server.spectrum_port if self.path.startswith('/_hifimate/') else self.server.ctrl_port
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0)) or None
        upstream = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('connection', 'keep-alive')}
        upstream.request(self.command, self.path, body, headers)
        response = upstream.getresponse()
        self.send_response(response.status)
        streaming = 'text/event-stream' in (response.getheader('Content-Type') or '')
        for k, v in response.getheaders():
            if k.lower() not in ('connection', 'transfer-encoding', 'content-length'): self.send_header(k, v)
        if streaming:
            self.send_header('Connection', 'close'); self.end_headers(); self.close_connection = True
            try:
                while chunk := response.read1(65536):
                    self.wfile.write(chunk); self.wfile.flush()
            except OSError: pass
        else:
            data = response.read()
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        upstream.close()

    do_GET = do_POST = do_PUT = do_DELETE = proxy


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=5099)
    parser.add_argument('--camilladsp', default='/opt/hifimate/bin/hifimate-camilladsp')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='hifimate-demo-'))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))   # kill cleans up too
    try:
        prepare(root)
        app = App(root, Collector(), lambda *a, **k: '', lambda *a, **k: {'ok': True}, lambda prefix: None,
                  auth=Auth(verify=lambda pw: pw == PASSWORD, available=lambda: True),
                  response_model=ResponseModel(args.camilladsp, root / 'work'))
        app.lyrics = Lyrics()
        Music.loops[RATE] = Music.compose(RATE)          # before the first viewer waits for it
        service = Spectrum(dsp, Flat(), fps=30, capture=lambda node, monitor, rate: Music(rate))
        ctrl = Server(('127.0.0.1', 0), app, ROOT / 'gui')
        spec = spectrum_proxy.Server(('127.0.0.1', 0), ROOT / 'gui', service)
        front = ThreadingHTTPServer(('127.0.0.1', args.port), Front)
        front.daemon_threads = True
        front.ctrl_port, front.spectrum_port = ctrl.server_address[1], spec.server_address[1]
        for server in (ctrl, spec):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'demo on http://127.0.0.1:{args.port}/ctrl/ (password {PASSWORD}); Ctrl-C stops it '
              f'(ctrl :{front.ctrl_port}, spectrum :{front.spectrum_port})', flush=True)
        front.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
