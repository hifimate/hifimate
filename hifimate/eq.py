"""The personal EQ as the control page edits it (config/dsp-filters.yml).

The file stays the one source of truth, in its own format (CamillaDSP
``filters`` and ``pipeline``, see user_filters.py), so editing it by hand
keeps working. The page edits a draft and applies it here:

- ``read``: the file, its fingerprint (etag) and saved versions;
- ``save``: refuses when the file changed since the draft was read (someone
  edited it over SSH), keeps the previous file as a version (last VERSIONS),
  replaces atomically;
- ``curves``: the exact response of a draft, the room correction and both
  together, measured by running the very CamillaDSP binary offline
  (dsp_model.py) - every filter type, FIR included, no re-derived maths;
- ``parse_text`` / ``import_text``: the text mode (the file's YAML) and
  filter lists exported by REW, AutoEQ and Equalizer APO.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from . import room_eq, user_filters

VERSIONS = 10
RATE = 48000
SIZE = 32768                      # 1.5 Hz bins at 48 kHz: enough for 20 Hz detail
POINTS = 240                      # log-spaced points sent to the page
F_LOW, F_HIGH = 10.0, 22000.0
HEADER = ('# Personal DSP filters for this machine (CamillaDSP filters and pipeline).\n'
          '# Edited by the HiFiMate EQ page (/ctrl/eq/); editing by hand works too, then run\n'
          '# `sudo hifimate reload-filters`. Reinstalling never changes this file.\n')


class Conflict(Exception):
    """The file changed since the draft was read."""


def path(prefix):
    return Path(prefix) / 'config/dsp-filters.yml'


def versions_dir(prefix):
    return Path(prefix) / 'state/eq/versions'


def etag(data):
    return hashlib.sha256(data).hexdigest()[:16]


def read(prefix):
    """The file as the page needs it. A broken file is returned with its error,
    so the page can show and fix it."""
    p = path(prefix)
    raw = p.read_bytes() if p.is_file() else b''
    result = {'etag': etag(raw), 'text': raw.decode('utf-8', 'replace'), 'error': None,
              'config': {'filters': {}, 'pipeline': []}}
    try:
        result['config'] = parse_text(result['text'])
    except ValueError as error:
        result['error'] = str(error)
    result['versions'] = versions(prefix)
    return result


def parse_text(text):
    """The text mode: the file's YAML, validated like the arbiter does."""
    import yaml
    if len(text) > 256 * 1024:
        raise ValueError('dsp-filters: 文件太大')
    try:
        data = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError as error:
        raise ValueError(f'dsp-filters: YAML 写法错误：{str(error).splitlines()[0]}') from None
    return user_filters.validate(data)


def render(config):
    """The file for a validated config: one line per filter, readable by hand."""
    lines = [HEADER.rstrip('\n'), 'filters:' + ('' if config['filters'] else ' {}')]
    for name, item in config['filters'].items():
        lines.append(f'  {name}: {json.dumps(item, ensure_ascii=False)}')
    lines.append('pipeline:' + ('' if config['pipeline'] else ' []'))
    for step in config['pipeline']:
        lines.append(f'  - {json.dumps(step, ensure_ascii=False)}')
    return '\n'.join(lines) + '\n'


def save(prefix, config, expected, clock=time.time):
    """Write a validated config; returns the new etag."""
    config = user_filters.validate(config)
    p = path(prefix)
    old = p.read_bytes() if p.is_file() else b''
    if etag(old) != expected:
        raise Conflict('EQ 文件在你编辑期间被改过（可能是在主机上手动修改的），请重新载入后再改')
    data = render(config).encode()
    if old and old != data:
        keep_version(prefix, old, clock)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, stage = tempfile.mkstemp(prefix='.dsp-filters.', dir=p.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(stage, 0o664)
        os.replace(stage, p)
    finally:
        Path(stage).unlink(missing_ok=True)
    return etag(data)


def keep_version(prefix, data, clock=time.time):
    d = versions_dir(prefix)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))
    name = stamp
    n = 1
    while (d / f'{name}.yml').exists():
        n += 1; name = f'{stamp}-{n}'
    (d / f'{name}.yml').write_bytes(data)
    for old in sorted(d.glob('*.yml'))[:-VERSIONS]:
        old.unlink()


VERSION_ID = re.compile(r'^[0-9]{8}-[0-9]{6}(-[0-9]{1,3})?$')


def versions(prefix):
    """Saved versions, newest first: id, time and how many filters."""
    out = []
    d = versions_dir(prefix)
    for p in sorted(d.glob('*.yml'), reverse=True) if d.is_dir() else []:
        if not VERSION_ID.match(p.stem):
            continue
        try:
            config = parse_text(p.read_text())
            count = len(config['filters'])
        except (OSError, ValueError):
            count = None
        out.append({'id': p.stem, 'created': p.stat().st_mtime, 'filters': count})
    return out


def version(prefix, ident):
    if not isinstance(ident, str) or not VERSION_ID.match(ident):
        raise ValueError('版本不存在')
    p = versions_dir(prefix) / f'{ident}.yml'
    if not p.is_file():
        raise ValueError('版本不存在')
    return parse_text(p.read_text())


# --- curves ---------------------------------------------------------------------------

def grid(points=POINTS, low=F_LOW, high=F_HIGH):
    return [low * (high / low) ** (i / (points - 1)) for i in range(points)]


def _measure(model, config, freqs):
    """dB of H[c][c] for a two-channel pipeline at freqs (log-interpolated bins)."""
    import numpy as np
    if not config['pipeline']:
        return {'L': [0.0] * len(freqs), 'R': [0.0] * len(freqs)}
    h = model.response(config, RATE, SIZE)
    bins = np.fft.rfftfreq(SIZE, 1 / RATE)
    out = {}
    for name, c in (('L', 0), ('R', 1)):
        db = 20 * np.log10(np.maximum(np.abs(h[c, c]), 1e-9))
        out[name] = [round(float(v), 3) for v in np.interp(freqs, bins, db)]
    return out


def combined(user, room):
    """One pipeline: the personal filters, then room correction (the DSP's order)."""
    config = user_filters.apply({'filters': {}, 'pipeline': []}, user)
    return room_eq.apply(config, room)


def headroom(prefix):
    """The preset Headroom gain (dB) before the personal filters, or 0."""
    import yaml
    try:
        base = yaml.safe_load((Path(prefix) / 'config/host-stack/camilladsp.yml').read_text())
        return float(base['filters']['Headroom']['parameters']['gain'])
    except (OSError, KeyError, TypeError, ValueError):
        return 0.0


def curves(model, user, room, head=0.0):
    """The draft (eq), room correction (room) and both (total) per channel, and
    whether the total boost can clip after the preset headroom."""
    user = user_filters.validate(user)
    freqs = grid()
    result = {'freq': [round(f, 2) for f in freqs],
              'eq': _measure(model, user_filters.apply({'filters': {}, 'pipeline': []}, user), freqs),
              'room': _measure(model, room_eq.apply({'filters': {}, 'pipeline': []}, room), freqs) if room else None}
    result['total'] = _measure(model, combined(user, room), freqs) if room else result['eq']
    audible = [i for i, f in enumerate(freqs) if 20 <= f <= 20000]
    peak = max(result['total'][c][i] for c in ('L', 'R') for i in audible)
    result['max_boost'] = round(max(peak, 0.0), 2)
    result['headroom'] = head
    result['clips'] = peak + head > 0.05
    return result


# --- import (REW, AutoEQ, Equalizer APO) -------------------------------------------

_TYPES = {'PK': 'Peaking', 'PEQ': 'Peaking', 'MODAL': 'Peaking',
          'LS': 'Lowshelf', 'LSC': 'Lowshelf', 'LSQ': 'Lowshelf', 'LS6DB': 'Lowshelf', 'LS12DB': 'Lowshelf',
          'HS': 'Highshelf', 'HSC': 'Highshelf', 'HSQ': 'Highshelf', 'HS6DB': 'Highshelf', 'HS12DB': 'Highshelf',
          'LP': 'Lowpass', 'LPQ': 'Lowpass', 'HP': 'Highpass', 'HPQ': 'Highpass',
          'NO': 'Notch', 'AP': 'Allpass', 'BP': 'Bandpass'}
_NUMBER = r'([-+]?[0-9]*\.?[0-9]+)'


def import_text(text, prefix_name='eq'):
    """Filters from REW / AutoEQ / Equalizer APO text: a config of new filters.

    Understood: ``Preamp: -6.2 dB``; ``Filter N: ON|OFF <type> Fc <f> Hz
    [Gain <g> dB] [Q <q>]`` (also ``BW Oct``); ``Channel: L|R|ALL`` (Equalizer
    APO) for the lines after it. Other lines are ignored and counted.
    """
    if len(text) > 64 * 1024:
        raise ValueError('导入的内容太长')
    filters, pipeline, skipped = {}, [], 0
    channels = [0, 1]
    n = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', '//')):
            continue
        low = line.lower()
        if low.startswith('channel:'):
            value = line.split(':', 1)[1].strip().upper()
            channels = {'L': [0], 'R': [1], 'ALL': [0, 1], 'L R': [0, 1], '1': [0], '2': [1]}.get(value, channels)
            continue
        if low.startswith('preamp:'):
            m = re.search(_NUMBER + r'\s*db', low)
            if not m:
                skipped += 1; continue
            n += 1
            name = f'{prefix_name}_preamp_{n}'
            filters[name] = {'type': 'Gain', 'parameters': {'gain': float(m.group(1))}}
            pipeline.append({'type': 'Filter', 'channels': list(channels), 'names': [name]})
            continue
        m = re.match(r'filter\s*[0-9]*\s*:\s*(on|off)\s+([a-z0-9]+)(?:\s+(6|12)\s*db)?\s+(.*)$', low)
        if not m:
            skipped += 1; continue
        state, kind, slope, rest = m.groups()
        biquad = _TYPES.get(kind.upper() + (f'{slope}DB' if slope else ''), _TYPES.get(kind.upper()))
        fc = re.search(r'fc\s*' + _NUMBER + r'\s*(k?)hz', rest)
        if not biquad or not fc:
            skipped += 1; continue
        freq = float(fc.group(1)) * (1000 if fc.group(2) else 1)
        params = {'type': biquad, 'freq': round(freq, 2)}
        gain = re.search(r'gain\s*' + _NUMBER + r'\s*db', rest)
        q = re.search(r'\bq\s*' + _NUMBER, rest)
        bw = re.search(r'bw\s*(?:oct\s*)?' + _NUMBER, rest)
        if biquad in ('Peaking', 'Lowshelf', 'Highshelf'):
            if not gain:
                skipped += 1; continue
            params['gain'] = float(gain.group(1))
        if biquad in ('Lowshelf', 'Highshelf') and not q and slope:
            params['slope'] = float(slope)
        elif q:
            params['q'] = float(q.group(1))
        elif bw and biquad != 'Lowshelf' and biquad != 'Highshelf':
            octaves = float(bw.group(1))
            params['q'] = round(math.sqrt(2 ** octaves) / (2 ** octaves - 1), 4)
        else:
            params['q'] = 0.7071
        if not freq > 0:
            skipped += 1; continue
        n += 1
        name = f'{prefix_name}_{n}'
        filters[name] = {'type': 'Biquad', 'parameters': params}
        step = {'type': 'Filter', 'channels': list(channels), 'names': [name]}
        if state == 'off':
            step['bypassed'] = True
        pipeline.append(step)
    if not filters:
        raise ValueError('没有认出任何滤波器（支持 REW、AutoEQ、Equalizer APO 的格式）')
    config = user_filters.validate({'filters': filters, 'pipeline': pipeline})
    return config, skipped
