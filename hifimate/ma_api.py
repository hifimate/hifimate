"""Music Assistant's own API for what MA plays into its MPD (6601).

MA streams a whole queue to MPD as one "flow" URL, so MPD only knows the
station name "Music Assistant". Title, artist, album, progress, next and
seek come from MA's queue instead, through its JSON-RPC endpoint with a
long-lived token of a non-admin MA user (role "user": queue and player
control only). The installer creates both (music-assistant/hifimate_ctrl_token.py)
into PREFIX/config/ma-token (0600); `hifimate-ctrl set-ma-token` is the manual
fallback. /ctrl never shows it.
"""
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

API = 'http://127.0.0.1:8095/api'
FLOW = re.compile(r'/flow/[^/]+/([A-Za-z0-9_.:-]{1,128})/')


class MAError(RuntimeError):
    pass


def token_path(prefix):
    return Path(prefix) / 'config/ma-token'


def load_token(prefix):
    try:
        text = token_path(prefix).read_text().strip()
    except OSError:
        return None
    return text or None


def save_token(prefix, token):
    token = token.strip()
    if not re.fullmatch(r'[A-Za-z0-9._~+/=-]{20,4096}', token):
        raise ValueError('这不像 MA 的长期令牌')
    path = token_path(prefix)
    temporary = path.with_name('.ma-token.tmp')
    temporary.write_text(token + '\n'); temporary.chmod(0o600); temporary.replace(path)


def call(command, args, token, url=API, opener=urllib.request.urlopen, timeout=2):
    body = json.dumps({'message_id': 'hifimate', 'command': command, 'args': args}).encode()
    request = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'})
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read(1 << 20)
    except urllib.error.HTTPError as error:
        messages = {401: 'MA 拒绝了令牌（重新运行一次安装即可）', 403: 'MA 不允许 HiFiMate 的控制账户执行这个操作'}
        raise MAError(messages.get(error.code, f'MA HTTP {error.code}')) from None
    return json.loads(data) if data else None


def queue_id(mpd_file):
    """The MA queue in a flow URL (http://host:8097/flow/<session>/<queue_id>/<x>.wav)."""
    match = FLOW.search(mpd_file or '')
    return match[1] if match else None


def _name(value):
    return value.get('name') if isinstance(value, dict) else None


def _origin(media):
    """(provider domain, item id) the track comes from, e.g. ('migu', '6009...') or ('bilibili', 'BV1...')."""
    for mapping in media.get('provider_mappings') or []:
        if isinstance(mapping, dict) and mapping.get('provider_domain') and mapping.get('item_id'):
            return str(mapping['provider_domain'])[:40], str(mapping['item_id'])[:128]
    return None, None


def now(queue, clock=time.time):
    """Our now-playing shape from an MA PlayerQueue."""
    item = queue.get('current_item') or {}
    media = item.get('media_item') or {}
    origin = dict(zip(('origin', 'origin_id'), _origin(media)))      # for lyrics; not shown
    stream = ((item.get('streamdetails') or {}).get('stream_metadata')) or {}   # radio: live title/artist
    artists = [a.get('name') for a in media.get('artists') or [] if isinstance(a, dict) and a.get('name')]
    state = {'playing': 'play', 'paused': 'pause', 'idle': 'stop'}.get(queue.get('state'))
    if not state or not item:
        return None
    if state == 'stop':
        # Paused long enough, MA stops the queue but keeps its item: 播放 resumes it (player_queues/play).
        return {'title': stream.get('title') or media.get('name') or item.get('name'),
                'artist': stream.get('artist') or (' / '.join(artists) if artists else None),
                'album': stream.get('album') or _name(media.get('album')), 'state': 'stop',
                'position': None, 'duration': float(item['duration']) if item.get('duration') else None,
                'controls': {'toggle': True, 'next': False, 'previous': False, 'seek': False}, 'reason': None, **origin}
    duration = item.get('duration') or None
    position = float(queue.get('elapsed_time') or 0)
    if state == 'play':
        position += max(0.0, clock() - float(queue.get('elapsed_time_last_updated') or clock()))
    if duration:
        position = min(position, float(duration))
    return {'title': stream.get('title') or media.get('name') or item.get('name'),
            'artist': stream.get('artist') or (' / '.join(artists) if artists else None),
            'album': stream.get('album') or _name(media.get('album')),
            'state': state, 'position': round(position, 1), 'duration': float(duration) if duration else None,
            'controls': {'toggle': True, 'next': bool(queue.get('next_item')), 'previous': True, 'seek': bool(duration)},
            'reason': None, **origin}


COMMANDS = {'toggle': 'player_queues/play_pause', 'next': 'player_queues/next', 'previous': 'player_queues/previous'}


def command(name, qid, token, value=None, caller=call):
    if name == 'seek':
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError('无法跳到这个位置')
        caller('player_queues/seek', {'queue_id': qid, 'position': int(value)}, token)
    elif name in COMMANDS:
        caller(COMMANDS[name], {'queue_id': qid}, token)
    else:
        raise ValueError('无效的操作')
