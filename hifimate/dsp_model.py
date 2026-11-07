"""The DSP's frequency response, measured by CamillaDSP itself (no patch, no model).

The spectrum is analysed from what the DSP captures (its input) and shown as
what it plays: input spectrum x the DSP's response. The response is measured,
not re-derived: a separate, offline CamillaDSP (the same binary as the live
one) runs the live pipeline (filters, mixers, processors) on a file with one
impulse per input channel and writes the result to a file. Its FFT is the
exact transfer matrix H[out][in] of every filter type CamillaDSP supports,
FIR included. It is re-measured only when the pipeline or rate changes and
never touches the live DSP or the DAC.

What the offline run cannot know is applied from the live state: the main
volume and mute (applied before the pipeline) and Volume filters' faders
(e.g. the handoff gate), which the offline run leaves at 0 dB.
"""
import hashlib
import json
import os
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path

AMPLITUDE = 1e-3       # small, so limiters/compressors stay linear; scaled back after
CACHE = 8
TIMEOUT = 15


def _key(config, rate, size):
    part = {k: config.get(k) for k in ('filters', 'mixers', 'processors', 'pipeline')}
    return hashlib.sha256(json.dumps([part, rate, size], sort_keys=True, default=str).encode()).hexdigest()


class ResponseModel:
    def __init__(self, binary, workdir, run=subprocess.run):
        self.binary, self.workdir, self.run = str(binary), Path(workdir), run
        self.cache = OrderedDict()

    def response(self, config, rate, size):
        """Complex H[out][in][bin] for an rfft of size (bins = size // 2 + 1), 2 x 2."""
        import numpy as np
        key = _key(config, rate, size)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        self.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
            tmp = Path(tmp)
            impulse = np.zeros((2 * size, 2))
            impulse[0, 0] = impulse[size, 1] = AMPLITUDE        # left alone, then right alone
            impulse.astype('<f8').tofile(tmp / 'in.raw')
            model = {'devices': {
                'samplerate': rate, 'chunksize': 1024,
                'capture': {'type': 'RawFile', 'filename': str(tmp / 'in.raw'), 'format': 'F64_LE',
                            'channels': 2, 'extra_samples': size},
                'playback': {'type': 'File', 'filename': str(tmp / 'out.raw'), 'format': 'F64_LE', 'channels': 2}},
                **{k: config.get(k) for k in ('filters', 'mixers', 'processors', 'pipeline') if config.get(k)}}
            (tmp / 'model.json').write_text(json.dumps(model))
            # CamillaDSP reads YAML; JSON is valid YAML.
            result = self.run([self.binary, str(tmp / 'model.json')], capture_output=True, text=True,
                              timeout=TIMEOUT, env={**os.environ, 'HOME': str(tmp)})
            if result.returncode:
                raise RuntimeError('CamillaDSP could not run the pipeline offline: '
                                   + (result.stderr.strip().splitlines() or ['?'])[-1][:200])
            out = np.fromfile(tmp / 'out.raw', dtype='<f8')
        if out.size < 4 * size or out.size % 2:
            raise RuntimeError('offline CamillaDSP produced no response')
        out = out.reshape(-1, 2)[:2 * size] / AMPLITUDE
        # out[:size] answers the left impulse, out[size:] the right one.
        h = np.empty((2, 2, size // 2 + 1), dtype=complex)
        for source in (0, 1):
            block = out[source * size:(source + 1) * size]
            for channel in (0, 1):
                h[channel, source] = np.fft.rfft(block[:, channel])
        self.cache[key] = h
        while len(self.cache) > CACHE:
            self.cache.popitem(last=False)
        return h


def fader_index(name):
    names = {'Main': 0, 'Aux1': 1, 'Aux2': 2, 'Aux3': 3, 'Aux4': 4}
    return names.get(name, 0)


def gains(config, faders):
    """Per output channel linear gain from the live faders: main volume/mute and
    the faders of Volume filters (left at 0 dB by the offline run)."""
    def level(index):
        if not faders or index >= len(faders):
            return 1.0
        fader = faders[index]
        return 0.0 if fader.get('mute') else 10 ** (float(fader.get('volume', 0.0)) / 20)
    channel = [level(0), level(0)]
    filters = config.get('filters') or {}
    for step in config.get('pipeline') or []:
        if step.get('type') != 'Filter' or step.get('bypassed'):
            continue
        for name in step.get('names') or []:
            item = filters.get(name) or {}
            if item.get('type') == 'Volume':
                gain = level(fader_index((item.get('parameters') or {}).get('fader', 'Main')))
                for c in step.get('channels') if step.get('channels') is not None else (0, 1):
                    if c in (0, 1):
                        channel[c] *= gain
    return channel
