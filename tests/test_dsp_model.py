"""The DSP response measured by an offline CamillaDSP run (a fake one here; the real one is checked live)."""
import json
import sys
from pathlib import Path

import pytest

from hifimate import dsp_model

np = pytest.importorskip('numpy')

# A stand-in for camilladsp: reads the model config, applies a linear "DSP"
# (left x 0.5, right->left crossfeed x 0.25, right x 2, one-sample delay on right).
FAKE = '''#!/usr/bin/env python3
import json, sys
import numpy as np
model = json.load(open(sys.argv[1]))
d = model['devices']
x = np.fromfile(d['capture']['filename'], dtype='<f8').reshape(-1, 2)
x = np.vstack([x, np.zeros((d['capture'].get('extra_samples', 0), 2))])
y = np.zeros_like(x)
y[:, 0] = 0.5 * x[:, 0] + 0.25 * x[:, 1]
y[1:, 1] = 2 * x[:-1, 1]
y.astype('<f8').tofile(d['playback']['filename'])
open(sys.argv[1] + '.seen', 'w').write(json.dumps(sorted(model)))
'''


def fake(tmp_path):
    path = tmp_path / 'camilladsp'
    # The offline run gets its own HOME, which hides a `pip --user` numpy: name it.
    site = str(Path(np.__file__).parents[1])
    path.write_text(FAKE.replace('#!/usr/bin/env python3', '#!' + sys.executable + f'\nimport sys; sys.path.insert(0, {site!r})'))
    path.chmod(0o755)
    return path


CONFIG = {'devices': {'samplerate': 48000}, 'filters': {'g': {'type': 'Gain', 'parameters': {'gain': -6}}},
          'pipeline': [{'type': 'Filter', 'channels': [0, 1], 'names': ['g']}]}


def test_transfer_matrix_from_the_offline_run(tmp_path):
    runs = []
    def run(argv, **kw):
        import subprocess
        runs.append(argv)
        return subprocess.run(argv, **kw)
    model = dsp_model.ResponseModel(fake(tmp_path), tmp_path / 'work', run=run)
    h = model.response(CONFIG, 48000, 1024)
    assert h.shape == (2, 2, 513)
    assert np.allclose(abs(h[0, 0]), 0.5) and np.allclose(abs(h[0, 1]), 0.25)
    assert np.allclose(abs(h[1, 1]), 2.0) and np.allclose(abs(h[1, 0]), 0, atol=1e-12)
    assert np.allclose(np.angle(h[1, 1][1]), -2 * np.pi / 1024)        # the one-sample delay is kept
    assert model.response(CONFIG, 48000, 1024) is h and len(runs) == 1  # cached
    model.response({**CONFIG, 'filters': {}}, 48000, 1024)              # a new pipeline is measured again
    model.response(CONFIG, 96000, 1024)                                 # and a new rate
    assert len(runs) == 3
    assert not list((tmp_path / 'work').iterdir())                      # temporary files are gone


def test_a_pipeline_camilladsp_rejects_is_an_error(tmp_path):
    bad = tmp_path / 'camilladsp'
    bad.write_text('#!/bin/sh\necho "Invalid config file!" >&2\nexit 101\n'); bad.chmod(0o755)
    with pytest.raises(RuntimeError, match='Invalid config'):
        dsp_model.ResponseModel(bad, tmp_path / 'work').response(CONFIG, 48000, 1024)


def test_live_faders_main_volume_mute_and_volume_filters():
    faders = [{'volume': -20.0, 'mute': False}, {'volume': 0, 'mute': False}, {'volume': 0, 'mute': False},
              {'volume': 0, 'mute': False}, {'volume': -6.0, 'mute': False}]
    config = {'filters': {'gate': {'type': 'Volume', 'parameters': {'fader': 'Aux4'}}},
              'pipeline': [{'type': 'Filter', 'channels': [0, 1], 'names': ['gate']}]}
    left, right = dsp_model.gains(config, faders)
    assert left == pytest.approx(10 ** (-26 / 20)) and right == left
    faders[4]['mute'] = True                                            # the handoff gate closed
    assert dsp_model.gains(config, faders) == [0.0, 0.0]
    faders[0]['mute'] = True
    assert dsp_model.gains({}, faders) == [0.0, 0.0]
    assert dsp_model.gains({}, None) == [1.0, 1.0]
