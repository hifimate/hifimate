import math
from pathlib import Path
import struct
import time

import pytest

from hifimate.spectrum import (Analyzer, FFTWindow, FLOOR, MAX_DELAY, OCTAVE_RATIO, Spectrum,
                                  band_centers, fft_size, frequency_edges,
                                  scale_frequency, unscale_frequency)


def pcm(rate, count, frequency=1000, gain=0.5, phase=-1, offset=0):
    values = []
    for i in range(offset, offset + count):
        value = int(2147483647 * gain * math.sin(2 * math.pi * frequency * i / rate))
        values.append(struct.pack('<ii', value, int(value * phase)))
    return b''.join(values)


def dc_pcm(count, value=0.25):
    sample = int(2147483647 * value)
    return b''.join(struct.pack('<ii', sample, sample) for _ in range(count))


def feed(service, data, rate=48000, stream=1, position=0):
    """What the capture reader hands over: 1024-frame chunks of one capture stream."""
    for offset in range(0, len(data), 8192):
        service.feed_pcm(rate, stream, position + offset // 8, data[offset:offset + 8192])


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.mark.parametrize('rate', [44100, 48000, 96000, 192000, 768000])
def test_native_rate_fft_stereo_volume_and_silence(rate):
    analyzer = Analyzer(rate)
    frequency = 45 * rate / analyzer.size
    signal = pcm(rate, analyzer.size, frequency, gain=0.5, phase=-1)
    db = analyzer.analyze(signal)
    loudest = max(range(len(db)), key=db.__getitem__)
    assert abs(math.log(analyzer.centers[loudest] / frequency)) < 0.12
    assert db[loudest] == pytest.approx(-6.0206, abs=0.02)
    quiet = analyzer.analyze(pcm(rate, analyzer.size, frequency, gain=0.125, phase=-1))
    assert db[loudest] - quiet[loudest] == pytest.approx(12.0412, abs=0.02)
    assert analyzer.analyze(bytes(analyzer.size * 8)) == [FLOOR] * len(db)
    # audioMotion's fftSize 8192 at 48 kHz: ~171 ms (186 ms for 44.1 kHz rates).
    assert analyzer.size / rate * 1000 == pytest.approx(186 if rate % 44100 == 0 else 171, abs=1)


@pytest.mark.parametrize('rate', [44100, 48000, 192000, 768000])
@pytest.mark.parametrize('frequency', [10, 15])
def test_true_sub_bass_is_resolved_without_borrowing_kick_or_dc(rate, frequency):
    analyzer = Analyzer(rate)
    # Real sub-bass must be visible; hiding the first bars is not a fix.
    signal = pcm(rate, analyzer.size, frequency=frequency, gain=0.3)
    db = analyzer.analyze(signal)
    loudest = max(range(len(db)), key=db.__getitem__)
    assert abs(math.log(analyzer.centers[loudest] / frequency)) < 0.2
    assert db[loudest] == pytest.approx(20 * math.log10(0.3), abs=2)
    # A 60 Hz kick and channel DC must not masquerade as infrasonic content.
    db = analyzer.analyze(pcm(rate, analyzer.size, frequency=60))
    assert max(value for center, value in zip(analyzer.centers, db) if center < 20) < -50
    db = analyzer.analyze(dc_pcm(analyzer.size))
    assert db == [FLOOR] * len(analyzer.centers)


def test_low_bass_window_and_smoothing_avoid_50hz_display_step():
    analyzer = Analyzer(48000)
    db = analyzer.analyze(pcm(48000, analyzer.size, frequency=50))
    region = [index for index, center in enumerate(analyzer.centers)
              if 30 <= center <= 100]
    adjacent_steps = [abs(db[index + 1] - db[index])
                      for index in region[:-1]]
    # The old 40 Hz window switch made this same tone jump by over 52 dB.
    assert max(adjacent_steps) < 18
    loudest = max(range(len(db)), key=db.__getitem__)
    assert abs(math.log(analyzer.centers[loudest] / 50)) < 0.12


def test_one_window_gives_every_band_the_same_time_response():
    # Like audioMotion: no faster mid/high or slower bass window. A level
    # change moves 60 Hz and 1 kHz by the same amount within one window.
    analyzer = Analyzer(48000)
    with pytest.raises(ValueError):
        analyzer.analyze(pcm(48000, analyzer.size // 2))
    changes = []
    for frequency in (60, 1000):
        tone = pcm(48000, analyzer.size, frequency=frequency)
        band = max(range(len(analyzer.centers)), key=analyzer.analyze(tone).__getitem__)
        steady = analyzer.analyze(tone)[band]
        quiet = pcm(48000, analyzer.size, frequency=frequency, gain=0.05)
        assert steady - analyzer.analyze(quiet)[band] == pytest.approx(20, abs=0.05)
        half = tone[:len(tone) // 2] + quiet[len(quiet) // 2:]
        changes.append(steady - analyzer.analyze(half)[band])
    assert changes[0] == pytest.approx(changes[1], abs=1.5)
    assert analyzer.analyze(bytes(analyzer.size * 8)) == [FLOOR] * len(analyzer.centers)


def test_dc_bias_does_not_change_real_sub_bass():
    analyzer = Analyzer(48000)
    signal = pcm(48000, analyzer.size, frequency=15, gain=0.3)
    bias = int(0.125 * 2147483647)
    biased = b''.join(struct.pack('<ii', left + bias, right - bias)
                      for left, right in struct.iter_unpack('<ii', signal))
    assert analyzer.analyze(biased) == pytest.approx(analyzer.analyze(signal), abs=1e-6)


def test_bass_history_is_bounded_and_resets_on_a_gap(tmp_path):
    service = Spectrum()
    limit = (fft_size(48000) + int(48000 * MAX_DELAY)) * 8      # the window and the playback delay
    feed(service, bytes(limit * 2))
    assert limit <= service.total < limit + 8192
    service.feed_pcm(48000, 1, 100000, bytes(8192))
    assert service.total == 8192
    assert not any(service.frame['bins'])


def test_resume_after_stale_pcm_does_not_reuse_old_bass_history(tmp_path):
    now = [0.0]
    service = Spectrum(clock=lambda: now[0])
    feed(service, bytes(fft_size(48000) * 8))
    previous = service.generation
    now[0] = 0.5
    service.feed_pcm(48000, 1, fft_size(48000), bytes(8192))
    assert service.generation == previous + 1
    assert service.total == 8192


def test_shared_subscription_idle_format_switch_and_zero(tmp_path):
    service = Spectrum(fps=20)
    assert service.stop is None and service.analyses == 0
    service.subscribe()
    service.subscribe()
    assert service.clients == 2 and len(service.threads) == 2
    feed(service, pcm(48000, fft_size(48000)))
    until(lambda: service.frame['state'] == 'playing')
    assert service.frame['origin'] == 'dsp-model'
    old = service.frame['bins']
    service.feed_pcm(96000, 2, 0, bytes(1024 * 8))
    assert not any(service.frame['bins'])
    feed(service, pcm(96000, fft_size(96000), frequency=5000), 96000, 2, 1024)
    until(lambda: service.frame['state'] == 'playing' and service.frame['rate'] == 96000)
    assert service.frame['bins'] != old
    feed(service, bytes(fft_size(96000) * 8), 96000, 2, 1024 + fft_size(96000))
    until(lambda: service.frame['state'] == 'silent')
    assert not any(service.frame['bins'])
    until(lambda: service.frame['state'] == 'idle')
    assert service.frame['rate'] == 96000
    assert service.frame['frequency_max'] == 48000
    service.unsubscribe()
    assert service.stop is not None
    service.unsubscribe()
    count = service.analyses
    assert service.stop is None and service.threads == []
    time.sleep(0.1)
    assert service.analyses == count
    service.subscribe()
    assert not any(service.frame['bins'])
    service.unsubscribe()


@pytest.mark.parametrize('rate, expected_bands', [(48000, 270), (192000, 318), (768000, 366)])
def test_audio_motion_octave_bands_follow_dynamic_nyquist(rate, expected_bands):
    frame = Spectrum.empty('playing', rate)
    edges = frequency_edges(rate)
    assert len(edges) - 1 == expected_bands
    assert len(frame['bins']) == len(frame['db']) == len(frame['centers']) == len(edges) - 1
    assert frame['frequency_min'] == 10
    assert frame['frequency_max'] == rate / 2
    for lower, upper in zip(edges[1:-2], edges[2:-1]):
        assert upper / lower == pytest.approx(OCTAVE_RATIO)
    assert all(lower < center < upper for lower, center, upper
               in zip(edges, frame['centers'], edges[1:]))


@pytest.mark.parametrize('rate', [44100, 48000, 88200, 96000, 176400, 192000,
                                  352800, 384000, 705600, 768000])
@pytest.mark.parametrize('scale', ['log', 'bark', 'linear', 'mel'])
def test_frequency_scales_have_ordered_bands_and_correct_scaled_centers(rate, scale):
    edges = frequency_edges(rate, scale)
    centers = band_centers(edges, scale)
    assert edges[0] == 10 and edges[-1] == rate / 2
    assert all(a < c < b for a, c, b in zip(edges, centers, edges[1:]))
    if scale != 'log':
        # mode=1 is 240 equal intervals in Bark/linear/Mel, not 1/24 octaves.
        assert len(centers) == 240
        coordinates = [scale_frequency(f, scale) for f in edges]
        step = (coordinates[-1] - coordinates[0]) / 240
        assert [b - a for a, b in zip(coordinates, coordinates[1:])] == pytest.approx([step] * 240)
    for a, c, b in zip(edges, centers, edges[1:]):
        assert scale_frequency(c, scale) == pytest.approx(
            (scale_frequency(a, scale) + scale_frequency(b, scale)) / 2)
        assert unscale_frequency(scale_frequency(c, scale), scale) == pytest.approx(c)


def test_audio_motion_frequency_scale_reference_values():
    # Independent values from audioMotion 4.5.4 _freqScaling and mode=1.
    assert scale_frequency(1000, 'bark') == pytest.approx(8.527432432432433)
    assert scale_frequency(700, 'mel') == 1
    assert frequency_edges(48000, 'linear')[1] == pytest.approx(109.95833333333333)
    assert band_centers(frequency_edges(48000, 'linear'), 'linear')[0] == pytest.approx(59.979166666666664)


@pytest.mark.parametrize('fallback', [False, True])
def test_channel_layout_power_mean_is_phase_safe_without_max_bias(monkeypatch, fallback):
    import sys
    if fallback:
        monkeypatch.setitem(sys.modules, 'numpy', None)
    else:
        pytest.importorskip('numpy')
    window = FFTWindow(2048, 'dual-combined')
    mono_window = FFTWindow(2048, 'single')
    frequency = 45 * 48000 / window.size
    reference = window.power(pcm(48000, window.size, frequency, phase=1))
    opposite = window.power(pcm(48000, window.size, frequency, phase=-1))
    left_only = pcm(48000, window.size, frequency, phase=0)
    right_only = b''.join(struct.pack('<ii', right, left)
                          for left, right in struct.iter_unpack('<ii', left_only))
    left_power = window.power(left_only)
    right_power = window.power(right_only)
    assert reference[45] == pytest.approx(0.25, abs=1e-8)
    assert opposite == pytest.approx(reference, abs=1e-12)
    # One active channel contributes half the stereo power, i.e. -3.0103 dB;
    # max(L,R) would incorrectly keep the full reference power here.
    assert left_power[45] == pytest.approx(reference[45] / 2, abs=1e-12)
    assert right_power == pytest.approx(left_power, abs=1e-12)
    assert mono_window.power(left_only)[45] == pytest.approx(reference[45] / 4, abs=1e-12)
    assert mono_window.power(pcm(48000, window.size, frequency, phase=1)) == pytest.approx(reference, abs=1e-12)
    # audioMotion single downmixes before FFT and can cancel anti-phase;
    # dual-combined retains independent L/R energy before our power average.
    assert max(mono_window.power(pcm(48000, window.size, frequency, phase=-1))) == 0


@pytest.mark.parametrize('scale', ['log', 'bark', 'linear', 'mel'])
def test_configured_scale_and_layout_survive_analysis_and_rate_changes(tmp_path, scale):
    service = Spectrum(fps=20,
                       frequency_scale=scale, channel_layout='dual-combined')
    assert service.frame['frequencyScale'] == scale
    assert service.frame['channelLayout'] == 'dual-combined'
    service.subscribe()
    try:
        for rate, stream in [(48000, 1), (192000, 2)]:
            feed(service, pcm(rate, fft_size(rate), frequency=5000), rate, stream)
            until(lambda: service.frame['state'] == 'playing' and service.frame['rate'] == rate)
            frame = service.frame
            assert frame['frequencyScale'] == scale and frame['channelLayout'] == 'dual-combined'
            assert frame['frequency_max'] == rate / 2
            assert len(frame['bins']) == len(frequency_edges(rate, scale)) - 1
            # The wire format carries 0.1 Hz centers and 0.1 dB levels.
            assert frame['centers'] == pytest.approx(band_centers(frequency_edges(rate, scale), scale), abs=0.05)
            assert all(value == round(value, 1) for value in frame['db'] + frame['centers'])
            assert max(frame['bins']) > 0.8
    finally:
        service.unsubscribe()
    assert service.frame['frequencyScale'] == scale
    assert service.threads == []


def test_each_viewer_scale_gets_its_own_bands_from_one_analysis(tmp_path):
    service = Spectrum(fps=20)
    service.subscribe()
    service.subscribe('bark')
    service.subscribe('linear')
    try:
        assert service.scales() == ('log', 'bark', 'linear')
        feed(service, pcm(48000, fft_size(48000), frequency=5000))
        until(lambda: all(service.latest(timeout=0, frequency_scale=scale)[1]['state'] == 'playing'
                          for scale in ('log', 'bark', 'linear')))
        for scale in ('log', 'bark', 'linear'):
            frame = service.latest(timeout=0, frequency_scale=scale)[1]
            edges = frequency_edges(48000, scale)
            assert frame['frequencyScale'] == scale
            assert len(frame['bins']) == len(edges) - 1
            loudest = max(range(len(frame['db'])), key=frame['db'].__getitem__)
            assert edges[loudest] <= 5000 <= edges[loudest + 1] * 1.05
        # The selected scale changes the actual bands, not only the axis.
        assert len(service.latest(timeout=0, frequency_scale='bark')[1]['bins']) == 240
        assert len(service.latest(timeout=0, frequency_scale='log')[1]['bins']) == 270
        with pytest.raises(ValueError):
            service.subscribe('invalid')
    finally:
        service.unsubscribe('linear')
        service.unsubscribe()
        # Unwatched axes, including the configured default, are not analyzed.
        assert service.scales() == ('bark',)
        service.unsubscribe('bark')
        assert service.scales() == ('log',)
    assert service.threads == []
    assert service.views == {scale: 0 for scale in ('log', 'bark', 'linear', 'mel')}


def test_multi_scale_analysis_matches_single_scale_analyzers():
    signal = pcm(48000, fft_size(48000), frequency=80)
    shared = Analyzer(48000).analyze_scales(signal, ('log', 'bark', 'linear', 'mel'))
    for scale, db in shared.items():
        assert Analyzer(48000, frequency_scale=scale).analyze(signal) == pytest.approx(db, abs=1e-9)


@pytest.mark.parametrize('options', [{'frequency_scale': 'invalid'}, {'channel_layout': 'max'}])
def test_bad_configuration_is_rejected_before_worker_start(tmp_path, options):
    with pytest.raises(ValueError):
        Spectrum(**options)
    assert not (tmp_path / 'runtime').exists()


def test_a_capture_gap_resets_the_fft_window(tmp_path):
    service = Spectrum()
    signal = pcm(48000, fft_size(48000))
    feed(service, signal)
    generation = service.generation
    service.feed_pcm(48000, 1, 100000, signal[:8192])
    assert service.generation == generation + 1
    assert service.total == 8192
    assert not any(service.frame['bins'])


def test_identical_successive_analysis_windows_keep_spectrum_alive(tmp_path):
    service = Spectrum(fps=40)
    service.subscribe()
    try:
        signal = pcm(48000, fft_size(48000), frequency=1000)
        feed(service, signal, position=0)
        until(lambda: service.analyses >= 1)
        first = service.version
        # Feed a new PCM window with identical values.  Its numerical frame is
        # equal, but the SSE stream must still receive a new version.
        feed(service, signal, position=fft_size(48000))
        until(lambda: service.analyses >= 2 and service.version > first)
        assert service.frame['state'] == 'playing'
    finally:
        service.unsubscribe()


class FakeCapture:
    """pw-record: hands out the same PCM until stopped."""
    def __init__(self, data):
        self.data, self.started, self.processes = data, [], []

    def __call__(self, node, monitor, rate):
        self.started.append((node, monitor, rate))
        data = self.data

        class Process:
            def __init__(self):
                self.done = False
                process = self

                class Out:
                    def read(self, n):
                        if process.done:
                            return b''
                        time.sleep(n / 8 / rate)
                        return data[:n]
                self.stdout = Out()

            def poll(self): return 0 if self.done else None
            def terminate(self): self.done = True
            def wait(self, timeout=None): return 0
            def kill(self): self.done = True
        process = Process()
        self.processes.append(process)
        return process


class FakeModel:
    def __init__(self, gain): self.gain = gain

    def response(self, config, rate, size):
        import numpy as np
        h = np.zeros((2, 2, size // 2 + 1), dtype=complex)
        h[0, 0] = h[1, 1] = self.gain
        return h


def dsp_state(device='airplay_in.monitor', rate=48000, state='Running', volume=0.0, mute=False, buffer=0):
    return {'state': state, 'buffer': buffer,
            'faders': [{'volume': volume, 'mute': mute}] + [{'volume': 0.0, 'mute': False}] * 4,
            'config': {'devices': {'samplerate': rate, 'capture_samplerate': rate,
                                   'capture': {'type': 'Pulse', 'device': device, 'channels': 2}},
                       'filters': {}, 'pipeline': []}}


def test_watcher_captures_what_the_dsp_captures_and_shows_its_output(monkeypatch):
    pytest.importorskip('numpy')
    import hifimate.spectrum as module
    monkeypatch.setattr(module, 'WATCH', 0.05)
    live = [dsp_state()]
    tone = pcm(48000, 1024, frequency=1031.25, gain=0.5, phase=1)        # 22 whole cycles: seamless repeats
    capture = FakeCapture(tone)
    service = Spectrum(lambda: live[0], FakeModel(0.5), fps=30, capture=capture)   # the DSP halves: -6 dB
    reference = max(Analyzer(48000).analyze(pcm(48000, fft_size(48000), frequency=1031.25, gain=0.5, phase=1)))
    service.subscribe()
    try:
        until(lambda: service.frame['state'] == 'playing', 5)
        assert capture.started == [('airplay_in', True, 48000)]     # the sink's monitor, native rate
        until(lambda: max(service.frame['db']) == pytest.approx(reference - 6.02, abs=0.3), 5)
        live[0] = dsp_state(volume=-10.0)                           # main volume on top of the response
        until(lambda: max(service.frame['db']) == pytest.approx(reference - 16.02, abs=0.3), 5)
        live[0] = dsp_state(mute=True)                              # muted: the DAC plays silence
        until(lambda: service.frame['state'] == 'silent', 5)
        live[0] = dsp_state(device='bluez_input.14_99', rate=44100) # another source, captured directly
        until(lambda: capture.started[-1] == ('bluez_input.14_99', False, 44100), 5)
        assert capture.processes[0].done
        live[0] = {'state': 'Inactive'}                             # DSP stopped: no capture at all
        until(lambda: capture.processes[-1].done and service.source is None, 5)
    finally:
        service.unsubscribe()
    assert all(p.done for p in capture.processes)


def test_the_window_waits_for_the_dsp_playback_buffer():
    service = Spectrum()
    service.delay = 4096
    feed(service, pcm(48000, fft_size(48000)))
    assert service.total < (fft_size(48000) + service.delay) * 8      # not enough to analyse yet


def test_previous_playback_instance_cannot_reappear(tmp_path):
    service = Spectrum()
    data = pcm(48000, 1024)
    service.feed_pcm(48000, 123, 0, data)
    service.feed_pcm(48000, 124, 0, data)
    service.feed_pcm(48000, 123, 1024, data)
    assert service.identity == (48000, 124)
    assert service.total == 8192
    assert not any(service.frame['bins'])


@pytest.mark.parametrize('rate',[44100,768000])
@pytest.mark.parametrize('scale', ['log', 'bark', 'linear', 'mel'])
def test_numpy_accelerated_fft_matches_fallback(rate,monkeypatch,scale):
    import sys
    numpy=pytest.importorskip('numpy')
    accelerated=Analyzer(rate, frequency_scale=scale)
    assert accelerated.np is numpy
    signal=pcm(rate,accelerated.size,frequency=45*rate/accelerated.size,gain=.3,phase=-1)
    expected=accelerated.analyze(signal)
    monkeypatch.setitem(sys.modules,'numpy',None)
    fallback=Analyzer(rate, frequency_scale=scale)
    assert fallback.np is None
    assert fallback.analyze(signal)==pytest.approx(expected,abs=1e-7)


def test_numpy_and_fallback_match_for_sub_bass_and_dc(monkeypatch):
    import sys
    pytest.importorskip('numpy')
    accelerated = Analyzer(48000)
    signal = pcm(48000, accelerated.size, frequency=15)
    expected = accelerated.analyze(signal)
    monkeypatch.setitem(sys.modules, 'numpy', None)
    fallback = Analyzer(48000)
    assert fallback.analyze(signal) == pytest.approx(expected, abs=1e-7)
    assert fallback.analyze(dc_pcm(fallback.size)) == [FLOOR] * len(fallback.centers)
