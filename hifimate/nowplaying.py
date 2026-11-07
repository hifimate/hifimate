"""Now playing and transport for the active receiver (DLNA, MA, AirPlay, Bluetooth).

One shape for every source: title, artist, album, position, duration, state
and what can be controlled. Each source answers through its own channel:
the two MPDs directly, BlueZ MediaPlayer1 (AVRCP) through busctl, AirPlay
from the arbiter (Shairport metadata) with DACP sent from here. Nothing is
guessed: a missing field stays empty and an unsupported control is off.
"""
import json
import socket

MPD_PORTS = {'dlna': 6600, 'music_assistant_dlna': 6601}
COMMANDS = ('toggle', 'next', 'previous', 'seek')
RESTART_AFTER_S = 3.0    # 上一首 restarts the track when it has played this long
TEXT = 200


def _text(value):
    return str(value)[:TEXT] if value not in (None, '') else None


class MPDClient:
    """Minimal MPD protocol client (stdlib; the control service has no extra modules)."""
    def __init__(self, port, timeout=1.5):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=timeout)
        self.file = self.socket.makefile('rb')
        if not self.file.readline(256).startswith(b'OK MPD '):
            self.close(); raise RuntimeError('invalid MPD greeting')

    def command(self, line):
        self.socket.sendall((line + '\n').encode())
        out, size = {}, 0
        while True:
            raw = self.file.readline(4097); size += len(raw)
            if not raw or size > 65536:
                raise RuntimeError('MPD connection closed')
            if raw == b'OK\n':
                return out
            if raw.startswith(b'ACK '):
                raise RuntimeError(raw.decode('utf8', 'replace').strip())
            key, sep, value = raw.decode('utf8', 'replace').rstrip('\n').partition(': ')
            if sep:
                out.setdefault(key, value)

    def close(self):
        try: self.file.close()
        finally: self.socket.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def mpd_now(source, connect=MPDClient, unseekable=frozenset(), stopped=False):
    """The current track; stopped=True also reports a stopped queue's song, so it can be played again."""
    with connect(MPD_PORTS[source]) as mpd:
        song, status = mpd.command('currentsong'), mpd.command('status')
    state = status.get('state')
    if state == 'stop' and stopped and song:
        return {'title': _text(song.get('Title') or song.get('Name')), 'artist': _text(song.get('Artist') or song.get('AlbumArtist')),
                'album': _text(song.get('Album')), 'state': 'stop', 'songid': status.get('songid'), 'file': song.get('file'),
                'position': None, 'duration': None,
                'controls': {'toggle': True, 'next': 'nextsong' in status, 'previous': False, 'seek': False}, 'reason': None}
    if state not in ('play', 'pause'):
        return None
    duration = float(status['duration']) if status.get('duration') else None
    stream = song.get('Name')                         # internet radio: station name
    seekable = bool(duration) and (source, status.get('songid')) not in unseekable
    return {'title': _text(song.get('Title') or stream), 'artist': _text(song.get('Artist') or song.get('AlbumArtist')),
            'album': _text(song.get('Album')), 'state': state, 'songid': status.get('songid'), 'file': song.get('file'),
            'position': round(float(status.get('elapsed', 0)), 1), 'duration': round(duration, 1) if duration else None,
            'controls': {'toggle': True, 'next': 'nextsong' in status, 'previous': True, 'seek': seekable},
            'reason': None if seekable or not duration else '这个音源不支持拖动进度'}


def mpd_command(source, command, value=None, connect=MPDClient):
    with connect(MPD_PORTS[source]) as mpd:
        status = mpd.command('status')
        if command == 'toggle':
            mpd.command('pause 1' if status.get('state') == 'play' else 'play')
        elif command == 'next':
            if 'nextsong' not in status:
                raise ValueError('队列里没有下一首')
            mpd.command('next')
        elif command == 'previous':
            # Like a player's ⏮: restart the track, or go back when just started.
            if float(status.get('elapsed', 0)) > RESTART_AFTER_S or int(status.get('song', 0)) == 0:
                mpd.command('seekcur 0')
            else:
                mpd.command('previous')
        elif command == 'seek':
            duration = float(status.get('duration') or 0)
            if not duration or not isinstance(value, (int, float)) or not 0 <= value <= duration:
                raise ValueError('无法跳到这个位置')
            try:
                mpd.command(f'seekcur {value:.1f}')
            except RuntimeError as error:
                # e.g. an HTTP stream without Range support; MPD cannot tell in advance.
                if 'Not seekable' in str(error):
                    raise Unseekable(status.get('songid')) from None
                raise


class Unseekable(ValueError):
    def __init__(self, songid):
        super().__init__('这个音源不支持拖动进度'); self.songid = songid


# --- Bluetooth: BlueZ MediaPlayer1 (AVRCP) ------------------------------------

def _unpack(variant):
    return variant.get('data') if isinstance(variant, dict) and 'type' in variant else variant


def bluez_players(run):
    """{path: props} of MediaPlayer1 objects (busctl JSON)."""
    out = run(['busctl', '--system', '--json=short', 'call', 'org.bluez', '/',
               'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'], timeout=2)
    objects = json.loads(out)['data'][0]
    return {path: {k: _unpack(v) for k, v in ifaces['org.bluez.MediaPlayer1'].items()}
            for path, ifaces in objects.items() if 'org.bluez.MediaPlayer1' in ifaces}


def bluez_pick(players):
    """The player that is playing, else the only one."""
    playing = [p for p, props in players.items() if props.get('Status') == 'playing']
    if playing:
        return playing[0]
    return next(iter(players)) if len(players) == 1 else None


def bluez_now(run):
    players = bluez_players(run)
    path = bluez_pick(players)
    if not path:
        return None
    props = players[path]
    track = {k: _unpack(v) for k, v in (props.get('Track') or {}).items()}
    status = props.get('Status')
    duration = (track.get('Duration') or 0) / 1000 or None
    return {'title': _text(track.get('Title')), 'artist': _text(track.get('Artist')), 'album': _text(track.get('Album')),
            'state': 'play' if status == 'playing' else 'pause',
            'position': round((props.get('Position') or 0) / 1000, 1), 'duration': round(duration, 1) if duration else None,
            # AVRCP has no absolute seek (only fast-forward / rewind).
            'controls': {'toggle': True, 'next': True, 'previous': True, 'seek': False},
            'reason': None if track.get('Title') else '手机没有发送曲目信息'}


def bluez_command(command, run):
    players = bluez_players(run)
    path = bluez_pick(players)
    if not path:
        raise ValueError('没有可控制的蓝牙播放器')
    if command == 'toggle':
        method = 'Pause' if players[path].get('Status') == 'playing' else 'Play'
    elif command in ('next', 'previous'):
        method = command.capitalize()
    else:
        raise ValueError('蓝牙不支持拖动进度')
    run(['busctl', '--system', 'call', 'org.bluez', path, 'org.bluez.MediaPlayer1', method], timeout=3)


# --- AirPlay: metadata from the arbiter, DACP from here ----------------------

DACP = {'toggle': 'playpause', 'next': 'nextitem', 'previous': 'previtem'}


def airplay_now(reply):
    now = reply.get('airplay')
    if not now:
        return None
    control = bool(now.get('can_control'))
    return {'title': _text(now.get('title')), 'artist': _text(now.get('artist')), 'album': _text(now.get('album')),
            'state': now.get('state', 'play'), 'position': now.get('position'), 'duration': now.get('duration'),
            # DACP has no seek every sender honours; offer only what works.
            'controls': {'toggle': control, 'next': control, 'previous': control, 'seek': False},
            'reason': None if control else '发送端不支持远程控制'}


def airplay_command(command, reply, resolve=None, http=None):
    from .remote_pause import dacp_endpoint, dacp_request
    if command not in DACP:
        raise ValueError('AirPlay 不支持拖动进度')
    remote = reply.get('dacp') or {}
    endpoint = (resolve or dacp_endpoint)(remote) if remote.get('acre') else None
    if not endpoint:
        raise ValueError('发送端不支持远程控制（没有 DACP）')
    (http or dacp_request)(endpoint, remote['acre'], DACP[command])


MA_NO_TOKEN = '未配置 MA 令牌：重新运行一次安装即可自动配置（或 hifimate-ctrl set-ma-token）'


class NowPlaying:
    """Polled by the state collector; remembers the last source while it is paused."""
    def __init__(self, run, request, format_path, connect=MPDClient, ma_token=lambda: None, ma_call=None):
        self.run, self.request, self.format_path, self.connect = run, request, format_path, connect
        self.ma_token, self.ma_call = ma_token, ma_call
        self.source = None
        self.unseekable = set()   # (source, songid) whose seek MPD refused

    def _airplay(self):
        return self.request(self.format_path(), {'action': 'nowplaying'}, timeout=2)

    def read(self, owner_source):
        # Pausing ends the arbitration session; keep showing (and resuming)
        # the source that played last until another one takes over.
        if owner_source:
            self.source = owner_source
        source = self.source
        if not source:
            return None
        if source in MPD_PORTS:
            # Pausing may end in a stop (MA stops its flow stream): keep the track, so 播放 resumes it.
            now = mpd_now(source, self.connect, self.unseekable, stopped=True)
            if now and source == 'music_assistant_dlna':
                now = self._ma(now)
        elif source == 'bluetooth':
            now = bluez_now(self.run)
        elif source == 'airplay':
            now = airplay_now(self._airplay())
        else:
            now = None
        if now is None and not owner_source:
            self.source = None       # stopped or gone: nothing to show
        if now:
            now.pop('file', None)
        return {'source': source, **now} if now else None

    def _ma_queue(self, mpd):
        """(queue id, token) when MA plays its flow stream into MPD and a token is set."""
        from . import ma_api
        qid = ma_api.queue_id(mpd.get('file'))
        return (qid, self.ma_token()) if qid else (None, None)

    def _ma(self, mpd):
        """MA's queue for what MPD only knows as one flow stream."""
        from . import ma_api
        qid, token = self._ma_queue(mpd)
        if not qid:
            return mpd                                   # MA sent a plain track URL: MPD's tags
        if not token:
            return {**mpd, 'reason': MA_NO_TOKEN, 'controls': {**mpd['controls'], 'seek': False}}
        try:
            queue = (self.ma_call or ma_api.call)('player_queues/get', {'queue_id': qid}, token)
            now = ma_api.now(queue or {})
        except (OSError, ValueError, ma_api.MAError) as error:
            return {**mpd, 'reason': f'无法读取 MA 队列：{error}', 'controls': {**mpd['controls'], 'seek': False}}
        return {**now, 'file': mpd.get('file')} if now else mpd

    def command(self, command, value=None):
        if command not in COMMANDS:
            raise ValueError('无效的操作')
        source = self.source
        if not source:
            raise ValueError('没有正在播放的来源')
        if source == 'music_assistant_dlna':
            from . import ma_api
            try:
                mpd = mpd_now(source, self.connect, stopped=True) or {}
            except (OSError, RuntimeError):
                mpd = {}
            qid, token = self._ma_queue(mpd)
            if qid and token:
                # MA owns the queue: MPD's own next/seek do not apply to a flow stream.
                try:
                    ma_api.command(command, qid, token, value, self.ma_call or ma_api.call)
                except ma_api.MAError as error:
                    raise ValueError(str(error)) from None
                return {'ok': True, 'source': source, 'command': command}
            if qid and command in ('next', 'previous', 'seek'):
                raise ValueError(MA_NO_TOKEN)
        if source in MPD_PORTS:
            try:
                mpd_command(source, command, value, self.connect)
            except Unseekable as error:
                if len(self.unseekable) > 256: self.unseekable.clear()
                self.unseekable.add((source, error.songid)); raise
            except RuntimeError as error:
                if str(error).startswith('ACK '):        # MPD refused: say why, not 503
                    raise ValueError('播放器拒绝了这个操作：' + str(error).split('} ', 1)[-1]) from None
                raise
        elif source == 'bluetooth':
            bluez_command(command, self.run)
        elif source == 'airplay':
            airplay_command(command, self._airplay())
        return {'ok': True, 'source': source, 'command': command}
