"""Personal DSP filters for this machine's hardware (speaker EQ, tonal balance).

They live in PREFIX/config/dsp-filters.yml, written in CamillaDSP's own
syntax (a filters map and Filter steps). The file is user state: the
installer creates it once and never overwrites it, and it is not part of
the source tree, so hardware-specific values stay on this machine.

Every DSP config the arbiter builds, at any rate, is first cleared of
USER-named filters and steps and then gets the file's, after the preset's
steps (Headroom) and before room correction and the handoff gate. Inside
the DSP the names carry the USER prefix so a later edit, rename or removal
in the file replaces them exactly; edit the file, not the live DSP config.
"""
import re
from pathlib import Path

USER = '_HiFiMateUser_'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,47}')
# Fader-driven types (Volume, Loudness) would fight the handoff gate.
TYPES = {'Biquad', 'BiquadCombo', 'Gain', 'Delay', 'DiffEq', 'Conv'}
CHANNELS = (0, 1)
MAX_FILTERS = 64

TEMPLATE = """\
# 个人 DSP 滤波器：这台机器的硬件专用（音箱 EQ、音色等）。
# 重新安装不会覆盖这个文件；它也不在源码仓库里。
#
# 写法就是 CamillaDSP 配置里的 filters 和 pipeline 两段（只用 Filter 步骤）。
# 仲裁器把它们放进每一份 DSP 配置：预置的 Headroom 之后、房间校正之前，
# 所有采样率都一样。改完运行一次：
#     ~/hifi/bin/hifimate-ctrl reload-filters
# 检查格式并立即生效（正在播放也不会重开 DAC）。
#
# 例子（去掉行首的 # 即可启用）：
# filters:
#   speaker-1: {type: Biquad, parameters: {type: Peaking, freq: 52, gain: 2.96, q: 1.7}}
#   air_shelf: {type: Biquad, parameters: {type: Highshelf, freq: 8000, gain: 2.0, q: 0.7}}
# pipeline:
#   - {type: Filter, channels: [0, 1], names: [speaker-1, air_shelf]}

filters: {}
pipeline: []
"""


def validate(data):
    """The file's content, checked: known names, Filter steps only, no fader types."""
    if data is None:
        return {'filters': {}, 'pipeline': []}
    if not isinstance(data, dict) or set(data) - {'filters', 'pipeline'}:
        raise ValueError('dsp-filters: only filters and pipeline are allowed')
    filters = data.get('filters') or {}
    pipeline = data.get('pipeline') or []
    if not isinstance(filters, dict) or len(filters) > MAX_FILTERS:
        raise ValueError(f'dsp-filters: filters must be a map of at most {MAX_FILTERS}')
    for name, item in filters.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f'dsp-filters: invalid filter name {name!r}')
        if not isinstance(item, dict) or item.get('type') not in TYPES or not isinstance(item.get('parameters'), dict):
            raise ValueError(f'dsp-filters: {name} needs type ({", ".join(sorted(TYPES))}) and parameters')
    if not isinstance(pipeline, list):
        raise ValueError('dsp-filters: pipeline must be a list')
    used = set()
    for step in pipeline:
        if not isinstance(step, dict) or step.get('type') != 'Filter' or set(step) - {'type', 'channels', 'names', 'bypassed', 'description'}:
            raise ValueError('dsp-filters: pipeline steps must be type Filter')
        channels, names = step.get('channels'), step.get('names')
        if not isinstance(channels, list) or not channels or any(c not in CHANNELS or type(c) is not int for c in channels):
            raise ValueError('dsp-filters: channels must be [0], [1] or [0, 1]')
        if not isinstance(names, list) or not names or any(n not in filters for n in names):
            raise ValueError(f'dsp-filters: every name in a step must be defined in filters: {names}')
        used.update(names)
    unused = sorted(set(filters) - used)
    if unused:
        raise ValueError(f'dsp-filters: defined but not in the pipeline: {", ".join(unused)}')
    return {'filters': filters, 'pipeline': pipeline}


def load(path):
    """The validated file, or None when there is none."""
    import yaml
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f'dsp-filters: not valid YAML: {error}') from None
    return validate(data)


def strip(config):
    """Remove every USER filter and its pipeline references."""
    filters = {k: v for k, v in (config.get('filters') or {}).items() if not k.startswith(USER)}
    pipeline = []
    for step in config.get('pipeline') or []:
        if step.get('type') == 'Filter':
            names = [name for name in step.get('names', []) if not name.startswith(USER)]
            if not names:
                continue
            step = {**step, 'names': names}
        pipeline.append(step)
    return {**config, 'filters': filters, 'pipeline': pipeline}


def apply(config, user, before=None):
    """config with the personal filters replaced; inserted before the step named before."""
    config = strip(config)
    if not user or not user['pipeline']:
        return config
    filters = dict(config['filters'])
    for name, item in user['filters'].items():
        filters[USER + name] = item
    steps = [{**step, 'names': [USER + n for n in step['names']]} for step in user['pipeline']]
    pipeline = list(config['pipeline'])
    at = next((i for i, step in enumerate(pipeline) if before and before in step.get('names', [])), len(pipeline))
    return {**config, 'filters': filters, 'pipeline': pipeline[:at] + steps + pipeline[at:]}


def summary(user):
    return {'configured': user is not None, 'filters': len(user['filters']) if user else 0}


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['template']:
        sys.stdout.write(TEMPLATE)
    else:
        sys.exit('usage: user_filters.py template')
