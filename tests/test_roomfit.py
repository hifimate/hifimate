"""Roomfit: sweep, deconvolution, recording upload, local TLS. No audio, no MPD."""
from contextlib import closing
import http.client
import io
import json
import math
import subprocess
import wave
import numpy as np
import pytest
from hifimate import roomfit as rf
from hifimate import tls
from hifimate.ctrl import App
from test_ctrl import FakeCollector, ROOT, login, request, serving


def take_samples(channel='L', level=-20):
    with wave.open(io.BytesIO(rf.take_wav(channel, level))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 3, rf.RATE)
        raw = np.frombuffer(w.readframes(w.getnframes()), np.uint8).reshape(-1, 3)
    v = raw[:, 0].astype(np.int32) | raw[:, 1].astype(np.int32) << 8 | raw[:, 2].astype(np.int32) << 16
    return (np.where(v >= 1 << 23, v - (1 << 24), v) / 8388608.0).reshape(-1, 2)


def pcm_wav(samples, rate, width=4):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(width); w.setframerate(rate)
        scale = (1 << (8 * width - 1)) - 1
        values = np.clip(np.round(np.asarray(samples) * scale), -scale - 1, scale)
        if width == 3:
            ints = values.astype('<i4')
            w.writeframes(ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
        else:
            w.writeframes(values.astype('<i2' if width == 2 else '<i4').tobytes())
    return out.getvalue()


def test_take_layout_level_and_channel():
    stereo = take_samples('R', -20)
    lead, sweep_n = int(rf.LEAD_SECONDS * rf.RATE), int(rf.SWEEP_SECONDS * rf.RATE)
    assert len(stereo) == lead + sweep_n + int(rf.TAIL_SECONDS * rf.RATE)
    assert not stereo[:, 0].any() and not stereo[:lead, 1].any() and not stereo[lead + sweep_n:, 1].any()
    assert 20 * math.log10(np.abs(stereo[:, 1]).max()) == pytest.approx(-20, abs=0.1)
    for bad in (('C', -20), ('L', -3), ('L', -50)):
        with pytest.raises(ValueError): rf.take_wav(*bad)


@pytest.mark.parametrize('rate', [48000, 44100])
def test_deconvolution_recovers_a_room_echo(rate):
    # A 5 ms reflection at half level: a comb with -6 dB at 100 Hz and +3.5 dB
    # at 200 Hz, found wherever the sweep starts in the phone's recording.
    x = take_samples('L', -20)[:, 0]
    d = int(0.005 * rf.RATE)
    room = x + 0.5 * np.concatenate([np.zeros(d), x[:-d]])
    rec = np.concatenate([np.zeros(int(0.61 * rf.RATE)), 0.3 * room, np.zeros(8000)])
    rec = rec + np.random.default_rng(0).normal(0, 3e-5, len(rec))
    rec = rf.resample(rec, rf.RATE, rate) if rate != rf.RATE else rec
    result = rf.analyze(rf.read_recording(pcm_wav(rec, rate))[0], rate)
    f, m = np.array(result['frequency']), np.array(result['magnitude_db'])
    at = lambda hz: m[np.argmin(abs(f - hz))]
    # Comb maxima at 200 and 400 Hz are equal; the 100 Hz notch is far below
    # (ideal -9.5 dB, softened by the 1/6-octave smoothing).
    assert at(200) - at(400) == pytest.approx(0, abs=1)
    assert at(100) - at(200) < -6
    assert result['sweep_start_s'] == pytest.approx(rf.LEAD_SECONDS + 0.61, abs=0.002)
    assert result['snr_db'] > 40 and not result['clipped']


def test_recording_formats_and_rejections():
    tone = 0.25 * np.sin(np.arange(int(10 * 48000)) / 7)
    for width in (2, 3, 4):
        samples, rate = rf.read_recording(pcm_wav(tone, 48000, width))
        assert rate == 48000 and abs(samples).max() == pytest.approx(0.25, abs=1e-3)
    with pytest.raises(ValueError, match='太短'): rf.read_recording(pcm_wav(tone[:48000], 48000))
    with pytest.raises(ValueError): rf.read_recording(b'not a wav')
    with pytest.raises(ValueError, match='没有找到'): rf.analyze(np.zeros(10 * 48000), 48000)


@pytest.fixture
def app(tmp_path):
    played, removed = [], []
    instance = App(tmp_path, FakeCollector(), lambda *a, **k: '', lambda *a, **k: {'ok': True}, lambda prefix: None,
                   roomfit_play=lambda url: played.append(url) or '17', roomfit_remove=removed.append, port=5009)
    instance.played, instance.removed = played, removed
    return instance


def raw(address, method, path, body=None, headers=None):
    with closing(http.client.HTTPConnection(*address, timeout=10)) as conn:
        conn.request(method, path, body, {'Host': 'hifi.test', 'Origin': 'http://hifi.test', **(headers or {})})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()


def test_measurement_needs_login_then_plays_through_mpd_and_analyses(app):
    with serving(app) as address:
        assert request(address, 'roomfit/start', {'channel': 'L', 'level_db': -30})[0] == 403
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'roomfit/start', {'channel': 'X', 'level_db': -30}, cookie)[0] == 400
        assert request(address, 'roomfit/start', {'channel': 'L', 'level_db': 0}, cookie)[0] == 400
        status, _, started = request(address, 'roomfit/start', {'channel': 'L', 'level_db': -30,
                                     'device': {'ua': 'iPhone', 'echoCancellation': 'false'}}, cookie)
        assert status == 200 and started['lead_s'] == rf.LEAD_SECONDS
        take = started['id']
        assert app.played == [f'http://127.0.0.1:5009/ctrl/api/roomfit/sweep/{take}.wav']
        # One take at a time while it plays.
        assert request(address, 'roomfit/start', {'channel': 'R', 'level_db': -30}, cookie)[0] == 409
        # The sweep is for the local MPD only: never through nginx (forwarded headers).
        status, headers, wav = raw(address, 'GET', f'/ctrl/api/roomfit/sweep/{take}.wav')
        assert status == 200 and headers['Content-Type'] == 'audio/wav' and wav[:4] == b'RIFF'
        assert raw(address, 'GET', f'/ctrl/api/roomfit/sweep/{take}.wav', headers={'X-Real-IP': '10.0.0.5'})[0] == 403
        # Upload: login and WAV required, analysed once.
        x = take_samples('L', -30)[:, 0]
        recording = pcm_wav(np.concatenate([np.zeros(12000), 0.2 * x, np.zeros(8000)])
                            + np.random.default_rng(1).normal(0, 2e-5, len(x) + 20000), 48000)
        path = f'/ctrl/api/roomfit/upload/{take}'
        assert raw(address, 'POST', path, recording, {'Content-Type': 'audio/wav'})[0] == 403
        assert raw(address, 'POST', path, recording, {**cookie, 'Content-Type': 'text/plain'})[0] == 415
        status, _, body = raw(address, 'POST', path, recording, {**cookie, 'Content-Type': 'audio/wav'})
        result = json.loads(body)
        assert status == 200 and result['channel'] == 'L' and len(result['frequency']) == 240
        assert raw(address, 'POST', path, recording, {**cookie, 'Content-Type': 'audio/wav'})[0] == 409
        assert raw(address, 'POST', '/ctrl/api/roomfit/upload/../../etc', recording, {**cookie, 'Content-Type': 'audio/wav'})[0] == 400
        status, _, listed = request(address, 'roomfit/takes')
        assert listed['takes'][0]['id'] == take and listed['takes'][0]['analysis']['snr_db'] > 40
        assert listed['takes'][0]['device']['echoCancellation'] == 'false'
    stored = app.prefix / 'state/roomfit' / take
    assert {p.name for p in stored.iterdir()} == {'meta.json', 'recording.wav', 'analysis.json'}
    assert all(p.stat().st_mode & 0o077 == 0 for p in stored.iterdir())


def test_page_files_and_ca_download(app, tmp_path):
    tls_dir = tmp_path / 'config/tls'
    tls.ensure(tls_dir, 'hifi-host', ['192.0.2.28'])
    with serving(app) as address:
        for path in ('/ctrl/roomfit/', '/ctrl/roomfit/roomfit.js', '/ctrl/roomfit/roomfit.css', '/ctrl/roomfit/roomfit-worklet.js'):
            assert raw(address, 'GET', path)[0] == 200
        status, headers, body = raw(address, 'GET', '/ctrl/roomfit/hifimate-ca.crt')
        assert status == 200 and headers['Content-Type'] == 'application/x-x509-ca-cert'
        assert body == (tls_dir / 'ca.crt').read_bytes() and b'PRIVATE' not in body


def x509(path, *args):
    return subprocess.run(['openssl', 'x509', '-in', str(path), '-noout', *args], check=True,
                          capture_output=True, text=True).stdout


def test_local_ca_signs_an_ios_acceptable_server_certificate(tmp_path):
    assert tls.ensure(tmp_path, 'hifi-host', ['192.0.2.28'])
    crt, ca = tmp_path / 'nginx/server.crt', tmp_path / 'ca.crt'
    text = x509(crt, '-text')
    for want in ('DNS:hifi-host', 'DNS:hifi-host.local', 'IP Address:192.0.2.28', 'TLS Web Server Authentication', 'CA:FALSE'):
        assert want in text
    subprocess.run(['openssl', 'verify', '-CAfile', str(ca), str(crt)], check=True, capture_output=True)
    days = subprocess.run(['openssl', 'x509', '-in', str(crt), '-noout', '-checkend', str(399 * 86400)], capture_output=True)
    assert days.returncode == 1   # expires within 398 days, as Apple requires
    assert (tmp_path / 'ca.key').stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'nginx/server.key').stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / 'nginx/ca.key').exists()   # nginx gets only the server pair
    # Unchanged addresses: nothing re-issued. A new address: new server
    # certificate from the same CA, so the phone keeps trusting it.
    ca_before, crt_before = ca.read_bytes(), crt.read_bytes()
    assert not tls.ensure(tmp_path, 'hifi-host', ['192.0.2.28'])
    assert tls.ensure(tmp_path, 'hifi-host', ['192.0.2.40'])
    assert ca.read_bytes() == ca_before and crt.read_bytes() != crt_before
    assert 'IP Address:192.0.2.40' in x509(crt, '-text')


def test_lan_addresses_skip_container_bridges():
    out = ('2: wlp3s0    inet 192.0.2.28/24 brd 192.0.2.255 scope global\n'
           '3: docker0    inet 172.17.0.1/16 scope global\n4: br-2bf9    inet 172.18.0.1/16 scope global\n'
           '5: enp2s0    inet 192.168.1.7/24 scope global\n')
    run = lambda *a, **k: subprocess.CompletedProcess(a, 0, out, '')
    assert tls.lan_addresses(run) == ['192.0.2.28', '192.168.1.7']


def test_nginx_serves_https_and_mounts_only_the_server_pair():
    nginx = (ROOT / 'music-assistant/nginx.conf').read_text()
    assert 'listen __HTTP_BIND__:__HTTPS_PORT__ ssl;' in nginx
    assert 'ssl_certificate /etc/hifimate-tls/server.crt;' in nginx and 'location = /roomfit/ { return 301 /ctrl/roomfit/; }' in nginx
    compose = (ROOT / 'music-assistant/compose.yml').read_text()
    assert 'source: ../config/tls/nginx' in compose and 'MUSIC_ASSISTANT_HTTPS_PORT' in compose
    entry = (ROOT / 'music-assistant/nginx-entrypoint.sh').read_text()
    assert 's/__HTTPS_PORT__/$tls_port/g' in entry


def minimum_phase(response_db, n=1 << 16, taps=8192):
    """Causal FIR with the given magnitude (real-cepstrum folding), like a real mic."""
    cep = np.fft.irfft(np.log(10 ** (response_db / 20)), n)
    fold = np.zeros(n); fold[0] = cep[0]; fold[1:n // 2] = 2 * cep[1:n // 2]; fold[n // 2] = cep[n // 2]
    return np.fft.irfft(np.exp(np.fft.rfft(fold)), n)[:taps]


def test_iphone_15_pro_max_compensation_undoes_the_mic():
    # Faber Acoustical's anechoic measurement (digitised): -14 dB at 20 Hz,
    # -6.8 at 30, -2.6 at 50, -1 at 100, flat at 1 kHz.
    assert {m['id'] for m in rf.mics()} >= {'iphone-15-pro-max'}
    own = rf.mic_response('iphone-15-pro-max', np.array([20.0, 30, 50, 100, 1000]))
    assert np.allclose(own, [-14.2, -6.8, -2.6, -1.0, -0.1], atol=0.3)
    # A flat room heard through that mic reads flat again once compensated.
    x = take_samples('L', -20)[:, 0]
    f = np.fft.rfftfreq(1 << 16, 1 / rf.RATE)
    heard = np.convolve(x, minimum_phase(rf.mic_response('iphone-15-pro-max', np.maximum(f, 1))))[:len(x)]
    rec = np.concatenate([np.zeros(20000), 0.3 * heard, np.zeros(9000)])
    rec = rec + np.random.default_rng(0).normal(0, 2e-5, len(rec))
    result = rf.analyze(rec, rf.RATE, 'iphone-15-pro-max')
    g, m, raw = (np.array(result[k]) for k in ('frequency', 'magnitude_db', 'uncompensated_db'))
    band = (g >= 40) & (g <= 15000)
    assert np.abs(m[band]).max() < 0.5
    assert np.abs(m[(g >= 31) & (g < 40)]).max() < 1.0
    # Below the trust limit (and near the sweep's 20 Hz start) only bounded.
    assert np.abs(m[(g >= 22) & (g < 31)]).max() < 2.5
    assert raw[np.argmin(abs(g - 30))] < -5          # what the bare mic reads
    assert result['trusted_from_hz'] == pytest.approx(31, abs=2)   # mic needs > 6 dB below this
    assert rf.analyze(rec, rf.RATE)['trusted_from_hz'] == rf.UNCOMPENSATED_TRUST_HZ
    for bad in ('../x', 'unknown-mic'):
        with pytest.raises(ValueError): rf.mic_response(bad, np.array([100.0]))


def test_mic_profile_is_listed_validated_and_applied(app):
    with serving(app) as address:
        status, _, data = request(address, 'roomfit/mics')
        assert status == 200 and [m['id'] for m in data['mics']] == [m['id'] for m in rf.mics()]
        assert 'iphone-15-pro-max' in {m['id'] for m in data['mics']}
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'roomfit/start', {'channel': 'L', 'level_db': -30, 'mic': 'nope'}, cookie)[0] == 400
        status, _, started = request(address, 'roomfit/start', {'channel': 'L', 'level_db': -30, 'mic': 'iphone-15-pro-max'}, cookie)
        assert status == 200
        x = take_samples('L', -30)[:, 0]
        recording = pcm_wav(np.concatenate([np.zeros(12000), 0.2 * x, np.zeros(8000)]), 48000)
        status, _, body = raw(address, 'POST', f'/ctrl/api/roomfit/upload/{started["id"]}', recording, {**cookie, 'Content-Type': 'audio/wav'})
        result = json.loads(body)
        assert status == 200 and result['mic'] == 'iphone-15-pro-max' and result['trusted_from_hz'] < 40
        assert len(result['uncompensated_db']) == len(result['magnitude_db'])


def test_nginx_accepts_a_full_recording_upload():
    # Tests talk to ctrl directly; nginx's 1 MB default rejected real uploads (413).
    import re
    nginx = (ROOT / 'music-assistant/nginx.conf').read_text()
    block = nginx[nginx.index('location /ctrl/api/roomfit/upload/ {'):]
    block = block[:block.index('}')]
    limit = int(re.search(r'client_max_body_size (\d+)m;', block)[1]) * 1024 * 1024
    assert limit > rf.MAX_UPLOAD
    assert 'proxy_pass http://127.0.0.1:5009;' in block and 'X-Real-IP' in block
    # A real take (~16 s at 48 kHz, 32-bit) must fit with room to spare.
    assert 44 + 4 * 48000 * 20 < rf.MAX_UPLOAD


def test_snr_is_the_measured_response_not_the_raw_level():
    # Noise 40 dB under the sweep in raw level: deconvolution gains the sweep's
    # processing gain, so the response's SNR is well above the raw ratio.
    x = take_samples('L', -20)[:, 0]
    rec = np.concatenate([np.zeros(14400), 0.3 * x, np.zeros(40000)])
    noisy = rec + np.random.default_rng(3).normal(0, 0.3 * 0.1 * 10 ** (-40 / 20) / np.sqrt(2) * 10, len(rec))
    result = rf.analyze(noisy, rf.RATE)
    assert set(result['snr_bands']) == {'30-80', '80-320', '320-2000', '2000-8000', '8000-16000'}
    assert result['snr_db'] == result['snr_bands']['80-320']
    raw_ratio = 20 * math.log10(np.sqrt(np.mean((0.3 * x[int(3 * 48000):int(11 * 48000)]) ** 2)) / np.std(noisy[:14000]))
    assert result['snr_db'] > raw_ratio + 10
    quieter = rf.analyze(rec + np.random.default_rng(3).normal(0, np.std(noisy[:14000]) * 10, len(rec)), rf.RATE)
    assert quieter['snr_db'] == pytest.approx(result['snr_db'] - 20, abs=3)   # 20 dB more noise


def test_left_then_right_starts_right_after_the_upload(app):
    # 左 + 右: the page uploads L and starts R at once; that used to answer 409.
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        x = take_samples('L', -30)[:, 0]
        recording = pcm_wav(np.concatenate([np.zeros(12000), 0.2 * x, np.zeros(40000)]), 48000)
        status, _, left = request(address, 'roomfit/start', {'channel': 'L', 'level_db': -30}, cookie)
        assert status == 200
        assert raw(address, 'POST', f'/ctrl/api/roomfit/upload/{left["id"]}', recording, {**cookie, 'Content-Type': 'audio/wav'})[0] == 200
        assert request(address, 'roomfit/start', {'channel': 'R', 'level_db': -30}, cookie)[0] == 200


def test_peaking_matches_the_rbj_biquad():
    assert rf.peaking_db(np.array([100.0]), 100, 6, 2)[0] == pytest.approx(6, abs=1e-9)
    assert abs(rf.peaking_db(np.array([10.0, 5000.0]), 100, 6, 2)).max() < 0.05


def test_fit_cuts_peaks_and_leaves_dips():
    f = np.geomspace(20, 20000, 240)
    room = rf.peaking_db(f, 125, 9, 3) + rf.peaking_db(f, 320, 6, 4) + rf.peaking_db(f, 48, -8, 4)
    measured = rf.house_target(f) + room
    peq = rf.fit_peq(f, measured)
    assert 1 <= len(peq) <= rf.FIT_MAX_FILTERS
    assert all(-rf.FIT_MAX_CUT_DB <= x['gain'] < 0 and rf.FIT_Q[0] <= x['q'] <= rf.FIT_Q[1]
               and rf.FIT_LOW <= x['freq'] <= rf.FIT_HIGH for x in peq)
    main = min(peq, key=lambda x: x['gain'])
    assert main['freq'] == pytest.approx(125, rel=0.06) and main['gain'] < -6
    after = rf.smooth_octaves(f, measured + sum(rf.peaking_db(f, x['freq'], x['gain'], x['q']) for x in peq), 1 / 3)
    t = rf.house_target(f); sel = (f >= 35) & (f <= 350)
    assert (after - t)[sel].max() < rf.FIT_MIN_PEAK_DB + 0.5
    # The 48 Hz dip is not filled (cut-only) and not deepened noticeably.
    i = np.argmin(abs(f - 48))
    assert after[i] == pytest.approx(rf.smooth_octaves(f, measured, 1 / 3)[i], abs=1.0)


def test_fit_of_a_flat_room_is_empty():
    f = np.geomspace(20, 20000, 240)
    assert rf.fit_peq(f, rf.house_target(f)) == []


def test_every_iphone_from_the_15_on_has_a_measured_curve():
    rows = {m['id']: m for m in rf.mics()}
    measured = {i for i, m in rows.items() if m['measured']}
    assert measured == {'iphone-15-pro-max', 'iphone-16-pro', 'iphone-17-pro', 'iphone-18-pro'}
    for model in ('iphone-15', 'iphone-15-plus', 'iphone-15-pro', 'iphone-16', 'iphone-16-plus', 'iphone-16-pro-max',
                  'iphone-16e', 'iphone-17', 'iphone-air', 'iphone-17-pro-max', 'iphone-17e', 'iphone-18-pro-max', 'iphone-duo'):
        row = rows[model]
        assert not row['measured'] and row['curve'] in measured and '参考' in row['name']
        # a model without its own measurement uses its generation's curve, unchanged
        f = np.array([30.0, 100, 1000])
        assert np.array_equal(rf.mic_response(model, f), rf.mic_response(row['curve'], f))
    for row in rows.values():
        assert row['source'].startswith('Faber Acoustical') and 'faberacoustical.com' in row['source']
    # Faber's plots (digitised): a bass roll-off, flat by 1 kHz
    for curve, at_30 in (('iphone-16-pro', -7.4), ('iphone-17-pro', -7.2), ('iphone-18-pro', -14.0)):
        own = rf.mic_response(curve, np.array([30.0, 100, 1000]))
        assert abs(own[0] - at_30) < 0.5 and -1.5 < own[1] < -0.5 and abs(own[2]) < 0.3


def test_the_page_shows_the_cas_own_name(tmp_path):
    # new CAs are HiFiMate's; an older one keeps its name (phones already trust it)
    tls.ensure(tmp_path / 'new', 'hifi-host', ['192.0.2.28'])
    assert tls.ca_name(tmp_path / 'new') == 'HiFiMate Local CA (hifi-host)'
    old = tmp_path / 'old'; old.mkdir()
    import subprocess
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-keyout', str(old / 'ca.key'), '-out', str(old / 'ca.crt'), '-days', '30',
                    '-subj', '/CN=HiFi Local CA (hifi-host)/O=HiFi'], check=True, capture_output=True)
    assert tls.ca_name(old) == 'HiFi Local CA (hifi-host)'
    assert tls.ca_name(tmp_path / 'none') is None
