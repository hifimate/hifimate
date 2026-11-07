"""Settings the control page changes: playback (config/settings.json) and the
Music Assistant provider subscriptions (provider-subscriber/options.json).

Playback settings live in their own user file, laid over config/hifimate.conf
by the arbiter. hifimate.conf is an installed file (hashed in the install
manifest), so editing it would make the next install refuse to run;
settings.json is user state, kept by reinstalls and upgrades.
"""
import json
import os
from pathlib import Path
import re
import tempfile

RATES = (44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000, 705600, 768000)
PLAYBACK_KEYS = ('fade_ms', 'idle_timeout_s', 'rates')
MAX_SOURCES = 20
SOURCE = re.compile(r'^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}(?:@[A-Za-z0-9_.\-/]{1,100})?'
                    r'|https://(?:github\.com|gitee\.com|gitcode\.com)/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}?(?:\.git)?/?)$')


def atomic_json(path, data, mode=None):
    path = Path(path)
    mode = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
    fd, stage = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(stage, mode)
        os.replace(stage, path)
    finally:
        Path(stage).unlink(missing_ok=True)


# --- playback --------------------------------------------------------------------------

def validate_playback(data):
    """The user's playback settings, checked like the arbiter checks them."""
    if not isinstance(data, dict) or set(data) - set(PLAYBACK_KEYS):
        raise ValueError('只能设置淡入时长、空闲释放时间和允许的采样率')
    out = {}
    if 'fade_ms' in data:
        value = data['fade_ms']
        if type(value) not in (int, float) or not 20 <= value <= 2000:
            raise ValueError('淡入时长须为 20–2000 毫秒')
        out['fade_ms'] = int(round(value))
    if 'idle_timeout_s' in data:
        value = data['idle_timeout_s']
        if type(value) is not int or not (value == 0 or 30 <= value <= 86400):
            raise ValueError('空闲释放时间须为 0（不释放）或 30–86400 秒')
        out['idle_timeout_s'] = value
    if 'rates' in data:
        rates = data['rates']
        if not isinstance(rates, list) or any(type(r) is not int or r not in RATES for r in rates) or len(set(rates)) != len(rates):
            raise ValueError('采样率无效')
        out['rates'] = sorted(rates)
    return out


def user_path(config_dir):
    return Path(config_dir) / 'settings.json'


def load_user(config_dir):
    """The user's playback settings; a broken file is ignored (the defaults apply)."""
    try:
        return validate_playback(json.loads(user_path(config_dir).read_text()))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def effective(conf_path):
    """config/hifimate.conf with the user's playback settings laid over it."""
    conf_path = Path(conf_path)
    settings = json.loads(conf_path.read_text())
    settings.update(load_user(conf_path.parent))
    return settings


def save_playback(config_dir, data):
    """Replace the user's playback settings (keys left out fall back to the defaults)."""
    data = validate_playback(data)
    atomic_json(user_path(config_dir), data, 0o644)
    return data


# --- Music Assistant provider subscriptions -------------------------------------------

def subscriber_dir(prefix):
    """The subscriber's config directory, as the installer wrote it into .env."""
    env = Path(prefix) / 'music-assistant/.env'
    try:
        for line in env.read_text().splitlines():
            key, _, value = line.partition('=')
            if key == 'MUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_CONFIG_DIR' and value:
                return Path(value)
    except OSError:
        pass
    return Path(prefix) / 'music-assistant/provider-subscriber'


def subscriptions(prefix):
    """What is subscribed, what each source provided and when it last synced."""
    d = subscriber_dir(prefix)
    try:
        options = json.loads((d / 'options.json').read_text())
    except (OSError, ValueError):
        options = None
    try:
        state = json.loads((d / 'state.json').read_text()).get('sources', {})
    except (OSError, ValueError, AttributeError):
        state = {}
    sources = []
    for source in (options or {}).get('sources', []):
        key = source_key(source)
        info = state.get(key) or {}
        sources.append({'source': source, 'ref': info.get('ref'), 'providers': info.get('providers') or [],
                        'synced_at': info.get('synced_at')})
    return {'available': options is not None, 'sources': sources,
            'prune_removed': bool((options or {}).get('prune_removed')),
            'interval_minutes': (options or {}).get('interval_minutes')}


def source_key(source):
    """The subscriber's state key for a source (github:owner/repo, or the URL's host)."""
    m = re.match(r'^https://(github\.com|gitee\.com|gitcode\.com)/([^/]+)/([^/]+?)(?:\.git)?/?$', source)
    if m:
        host = {'github.com': 'github', 'gitee.com': 'gitee', 'gitcode.com': 'gitcode'}[m.group(1)]
        return f'{host}:{m.group(2)}/{m.group(3)}'
    return 'github:' + source.split('@', 1)[0]


def validate_sources(sources):
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ValueError(f'最多 {MAX_SOURCES} 个仓库')
    out = []
    for source in sources:
        if not isinstance(source, str) or not SOURCE.match(source.strip()):
            raise ValueError(f'仓库地址无效：{source!r}（写 owner/repo，或 GitHub/Gitee/GitCode 的完整地址）')
        if source.strip() not in out:
            out.append(source.strip())
    return out


def save_subscriptions(prefix, sources, prune_removed=None):
    """Write the sources (and prune_removed) into options.json, keeping the rest."""
    path = subscriber_dir(prefix) / 'options.json'
    options = json.loads(path.read_text())
    options['sources'] = validate_sources(sources)
    if isinstance(prune_removed, bool):
        options['prune_removed'] = prune_removed
    atomic_json(path, options, path.stat().st_mode & 0o777)
    return options['sources']
