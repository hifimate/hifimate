"""Room correction (roomfit) in the DSP config: cut-only peaking filters per channel.

The filters live in PREFIX/config/room-correction.json (user state, kept on
reinstall). Every DSP config the arbiter builds, at any rate, is first
cleared of ROOM-named filters and steps and then gets the current ones, so
the file is the single source; other filters (the preset's, personal ones) under
other names are untouched.
"""
import json
from pathlib import Path

ROOM = '_HiFiMateRoom'
CHANNELS = {'L': 0, 'R': 1}
LIMITS = {'freq': (20.0, 20000.0), 'gain': (-15.0, 0.0), 'q': (0.3, 10.0)}
MAX_FILTERS = 16


def validate(room):
    """The file's content, checked: cut-only, bounded, at most MAX_FILTERS per channel."""
    if not isinstance(room, dict) or not isinstance(room.get('enabled'), bool):
        raise ValueError('room correction: enabled must be true or false')
    channels = room.get('channels')
    if not isinstance(channels, dict) or set(channels) - set(CHANNELS):
        raise ValueError('room correction: channels must be L and/or R')
    for channel, filters in channels.items():
        if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
            raise ValueError(f'room correction: {channel} needs at most {MAX_FILTERS} filters')
        for item in filters:
            for key, (low, high) in LIMITS.items():
                value = item.get(key) if isinstance(item, dict) else None
                if type(value) not in (int, float) or not low <= value <= high:
                    raise ValueError(f'room correction: {channel} {key} must be {low}..{high}')
    return room


def load(path):
    """The validated file, or None when there is none."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    return validate(json.loads(text))


def save(path, room):
    path = Path(path)
    data = json.dumps(validate(room), ensure_ascii=False, indent=1) + '\n'
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(data)
    temporary.chmod(0o600)
    temporary.replace(path)


def strip(config):
    """Remove every ROOM filter and its pipeline references."""
    filters = {k: v for k, v in (config.get('filters') or {}).items() if not k.startswith(ROOM)}
    pipeline = []
    for step in config.get('pipeline') or []:
        if step.get('type') == 'Filter':
            names = [name for name in step.get('names', []) if not name.startswith(ROOM)]
            if not names:
                continue
            step = {**step, 'names': names}
        pipeline.append(step)
    return {**config, 'filters': filters, 'pipeline': pipeline}


def apply(config, room, before=None):
    """config with the room filters replaced; inserted before the step named before."""
    config = strip(config)
    if not room or not room['enabled']:
        return config
    filters, steps = dict(config['filters']), []
    for channel, index in CHANNELS.items():
        names = []
        for i, item in enumerate(room['channels'].get(channel, []), 1):
            name = f'{ROOM}_{channel}{i}'
            filters[name] = {'type': 'Biquad', 'parameters': {
                'type': 'Peaking', 'freq': float(item['freq']), 'gain': float(item['gain']), 'q': float(item['q'])}}
            names.append(name)
        if names:
            steps.append({'type': 'Filter', 'channels': [index], 'names': names})
    pipeline = list(config['pipeline'])
    at = next((i for i, step in enumerate(pipeline) if before and before in step.get('names', [])), len(pipeline))
    return {**config, 'filters': filters, 'pipeline': pipeline[:at] + steps + pipeline[at:]}


def summary(room):
    if not room:
        return {'configured': False, 'enabled': False, 'filters': {}}
    return {'configured': True, 'enabled': room['enabled'], 'source': room.get('source'), 'created': room.get('created'),
            'filters': {ch: len(room['channels'].get(ch, [])) for ch in CHANNELS}}
