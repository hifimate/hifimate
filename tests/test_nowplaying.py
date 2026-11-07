"""Now playing and transport for every receiver (fake MPD, busctl and arbiter)."""
import base64
import json
import pytest
from hifimate import nowplaying as np_
from hifimate.airplay import AirPlayEvents
from hifimate.ctrl import App
from test_ctrl import FakeCollector, login, request, serving


class FakeMPD:
    """A tiny MPD: currentsong/status answers and the commands it was sent."""
    def __init__(self, song=None, status=None):
        self.song = song or {}; self.status = {'state': 'play', **(status or {})}; self.sent = []
    def __call__(self, port):
        self.port = port; return self
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def command(self, line):
        if line == 'currentsong': return dict(self.song)
        if line == 'status': return dict(self.status)
        if line.startswith('seekcur') and self.status.get('unseekable'): raise RuntimeError('ACK [5@0] {seekcur} Not seekable')
        self.sent.append(line); return {}


def test_mpd_now_and_commands():
    mpd = FakeMPD({'Title': 'Hotel California', 'Artist': 'Eagles', 'Album': 'Hell Freezes Over'},
                  {'elapsed': '61.4', 'duration': '434.0', 'song': '0', 'nextsong': '1'})
    now = np_.mpd_now('dlna', mpd)
    assert mpd.port == 6600
    assert now == {'title': 'Hotel California', 'artist': 'Eagles', 'album': 'Hell Freezes Over', 'state': 'play', 'songid': None, 'file': None,
                   'position': 61.4, 'duration': 434.0, 'controls': {'toggle': True, 'next': True, 'previous': True, 'seek': True},
                   'reason': None}
    np_.mpd_command('music_assistant_dlna', 'toggle', connect=mpd); assert mpd.port == 6601 and mpd.sent[-1] == 'pause 1'
    np_.mpd_command('dlna', 'next', connect=mpd); assert mpd.sent[-1] == 'next'
    np_.mpd_command('dlna', 'previous', connect=mpd); assert mpd.sent[-1] == 'seekcur 0'     # 61 s in: restart
    np_.mpd_command('dlna', 'seek', 200, connect=mpd); assert mpd.sent[-1] == 'seekcur 200.0'
    for bad in (-1, 500, 'x'):
        with pytest.raises(ValueError): np_.mpd_command('dlna', 'seek', bad, connect=mpd)
    early = FakeMPD({}, {'elapsed': '1.0', 'song': '2', 'state': 'pause'})
    np_.mpd_command('dlna', 'previous', connect=early); assert early.sent == ['previous']
    np_.mpd_command('dlna', 'toggle', connect=early); assert early.sent[-1] == 'play'
    last = FakeMPD({'Name': 'Radio Paradise'}, {'elapsed': '3.0'})
    now = np_.mpd_now('dlna', last)
    assert now['title'] == 'Radio Paradise' and not now['controls']['next'] and not now['controls']['seek']
    with pytest.raises(ValueError, match='下一首'): np_.mpd_command('dlna', 'next', connect=last)
    assert np_.mpd_now('dlna', FakeMPD(status={'state': 'stop'})) is None


def busctl(players):
    """busctl --json=short GetManagedObjects output with MediaPlayer1 objects."""
    def variant(v):
        if isinstance(v, dict): return {'type': 'a{sv}', 'data': {k: variant(x) for k, x in v.items()}}
        return {'type': 'u' if isinstance(v, int) else 's', 'data': v}
    objects = {path: {'org.bluez.MediaPlayer1': {k: variant(v) for k, v in props.items()}} for path, props in players.items()}
    objects['/org/bluez/hci0'] = {'org.bluez.Adapter1': {}}
    return json.dumps({'type': 'a{oa{sa{sv}}}', 'data': [objects]})


def test_bluetooth_now_and_commands():
    path = '/org/bluez/hci0/dev_22_33_44_55_66_77/player0'
    calls = []
    def run(argv, timeout):
        calls.append(argv)
        return busctl({path: {'Status': 'playing', 'Position': 75000,
                              'Track': {'Title': '晴天', 'Artist': '周杰伦', 'Album': '叶惠美', 'Duration': 269000}}}) if 'GetManagedObjects' in argv else ''
    now = np_.bluez_now(run)
    assert now['title'] == '晴天' and now['artist'] == '周杰伦' and now['album'] == '叶惠美'
    assert (now['position'], now['duration'], now['state']) == (75.0, 269.0, 'play')
    assert now['controls'] == {'toggle': True, 'next': True, 'previous': True, 'seek': False}
    np_.bluez_command('toggle', run); assert calls[-1][-2:] == ['org.bluez.MediaPlayer1', 'Pause'] and calls[-1][-3] == path
    np_.bluez_command('previous', run); assert calls[-1][-1] == 'Previous'
    with pytest.raises(ValueError, match='拖动'): np_.bluez_command('seek', run)
    # A phone that sends no track: shown as playing, the reason says why.
    bare = lambda argv, timeout: busctl({path: {'Status': 'paused'}})
    assert np_.bluez_now(bare)['reason'] and np_.bluez_now(bare)['state'] == 'pause'
    assert np_.bluez_now(lambda argv, timeout: busctl({})) is None


def item(kind, code, text=None):
    data = '' if text is None else f'<data encoding="base64">{base64.b64encode(text.encode()).decode()}</data>'
    length = 0 if text is None else len(text.encode())
    return (f'<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>'
            f'<length>{length}</length>{data}</item>').encode()


def test_airplay_metadata_progress_and_pause():
    now = [1000.0]
    events = []
    decoder = AirPlayEvents(events.append); decoder.clock = lambda: now[0]
    decoder.feed(item('ssnc', 'clip', '192.0.2.41') + item('ssnc', 'daid', 'ABCDEF01') + item('ssnc', 'acre', '12345') + item('ssnc', 'pbeg'))
    decoder.feed(item('ssnc', 'mdst') + item('core', 'minm', 'Bohemian Rhapsody') + item('core', 'asar', 'Queen') +
                 item('core', 'asal', 'A Night at the Opera'))
    start = 4294000000                     # wraps past 2^32 during the track
    decoder.feed(item('ssnc', 'prgr', f'{start}/{(start + 44100 * 30) % (1 << 32)}/{(start + 44100 * 355) % (1 << 32)}'))
    out = decoder.now_playing()
    assert out == {'title': 'Bohemian Rhapsody', 'artist': 'Queen', 'album': 'A Night at the Opera', 'state': 'play',
                   'can_control': True, 'duration': 355.0, 'position': 30.0}
    assert [e['event'] for e in events] == ['play']          # metadata never claims the output
    now[0] += 10; assert decoder.now_playing()['position'] == 40.0
    decoder.feed(item('ssnc', 'pfls')); now[0] += 60
    assert decoder.now_playing()['state'] == 'pause' and decoder.now_playing()['position'] == 40.0
    decoder.feed(item('ssnc', 'mdst') + item('core', 'minm', 'Next'))
    assert decoder.now_playing()['title'] == 'Next' and 'artist' not in decoder.now_playing()
    assert 'acre' not in json.dumps(decoder.now_playing())   # no credentials in what /ctrl shows


def test_airplay_transport_uses_dacp_from_the_caller():
    reply = {'airplay': {'title': 'x', 'state': 'play', 'can_control': True},
             'dacp': {'daid': 'ABCDEF01', 'acre': '12345', 'clip': '192.0.2.41'}}
    sent = []
    np_.airplay_command('next', reply, resolve=lambda remote: ('192.0.2.41', 3689),
                        http=lambda endpoint, token, path: sent.append((endpoint, token, path)))
    assert sent == [(('192.0.2.41', 3689), '12345', 'nextitem')]
    with pytest.raises(ValueError, match='DACP'):
        np_.airplay_command('toggle', {'airplay': {}, 'dacp': {}})
    with pytest.raises(ValueError, match='拖动'):
        np_.airplay_command('seek', reply)
    assert np_.airplay_now(reply)['controls'] == {'toggle': True, 'next': True, 'previous': True, 'seek': False}
    assert np_.airplay_now({'airplay': {'can_control': False}})['reason']


def test_paused_source_stays_until_another_plays():
    mpd = FakeMPD({'Title': 'A'}, {'elapsed': '5', 'duration': '100'})
    player = np_.NowPlaying(lambda *a, **k: '', lambda *a, **k: {}, lambda: '/x', connect=mpd)
    assert player.read(None) is None
    assert player.read('dlna')['title'] == 'A'
    mpd.status['state'] = 'pause'                            # pausing ends the arbitration session
    assert player.read(None)['state'] == 'pause'              # still shown, so 播放 can resume it
    player.command('toggle'); assert mpd.sent[-1] == 'play'
    mpd.status['state'] = 'stop'                             # e.g. MA stops a long-paused flow stream
    stopped = player.read(None)
    assert stopped['state'] == 'stop' and stopped['title'] == 'A' and stopped['controls'] == {
        'toggle': True, 'next': False, 'previous': False, 'seek': False}
    player.command('toggle'); assert mpd.sent[-1] == 'play'  # 播放 starts it again
    mpd.song = {}                                            # queue cleared: nothing left to show
    assert player.read(None) is None and player.source is None
    with pytest.raises(ValueError): player.command('toggle')


def test_transport_endpoint_needs_login_and_reports_refusals(tmp_path):
    collector = FakeCollector()
    mpd = FakeMPD({'Title': 'A'}, {'elapsed': '5', 'duration': '100'})
    collector.nowplaying = np_.NowPlaying(lambda *a, **k: '', lambda *a, **k: {}, lambda: '/x', connect=mpd)
    collector.nowplaying.read('dlna')
    app = App(tmp_path, collector, lambda *a, **k: '', lambda *a, **k: {'ok': True}, lambda prefix: None)
    with serving(app) as address:
        assert request(address, 'transport', {'command': 'toggle'})[0] == 403
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'transport', {'command': 'toggle'}, cookie)[0] == 200 and mpd.sent[-1] == 'pause 1'
        assert request(address, 'transport', {'command': 'seek', 'position': 42}, cookie)[0] == 200 and mpd.sent[-1] == 'seekcur 42.0'
        status, _, body = request(address, 'transport', {'command': 'next'}, cookie)
        assert status == 409 and '下一首' in body['error']
        assert request(address, 'transport', {'command': 'eject'}, cookie)[0] == 409


def test_a_stream_mpd_cannot_seek_disables_the_slider():
    mpd = FakeMPD({'Title': 'A'}, {'elapsed': '5', 'duration': '100', 'songid': '7', 'unseekable': '1'})
    player = np_.NowPlaying(lambda *a, **k: '', lambda *a, **k: {}, lambda: '/x', connect=mpd)
    assert player.read('dlna')['controls']['seek']
    with pytest.raises(ValueError, match='不支持拖动'): player.command('seek', 30)
    now = player.read('dlna')
    assert not now['controls']['seek'] and now['reason'] == '这个音源不支持拖动进度'
    mpd.status['songid'] = '8'                                 # the next track may be seekable again
    assert player.read('dlna')['controls']['seek']


MA_FILE = 'http://192.0.2.28:8097/flow/AbCdEf12/mpd_127.0.0.1_6601/0123456789abcdef0123456789abcdef/mpd_127.0.0.1_6601.wav'


def ma_queue(state='playing', elapsed=40.0, updated=1000.0, next_item=True):
    return {'queue_id': 'mpd_127.0.0.1_6601', 'state': state, 'elapsed_time': elapsed, 'elapsed_time_last_updated': updated,
            'current_item': {'name': 'fallback', 'duration': 240, 'media_item': {
                'name': '小幸运', 'artists': [{'name': '田馥甄'}], 'album': {'name': '我的少女时代 电影原声带'}}},
            'next_item': {'name': 'x'} if next_item else None}


def test_ma_flow_stream_reads_the_ma_queue():
    from hifimate import ma_api
    assert ma_api.queue_id(MA_FILE) == 'mpd_127.0.0.1_6601' and ma_api.queue_id('http://x/a.flac') is None
    mpd = FakeMPD({'file': MA_FILE, 'Name': 'Music Assistant'}, {'elapsed': '98.6', 'songid': '1'})
    calls = []
    def caller(command, args, token):
        calls.append((command, args, token)); return ma_queue() if command == 'player_queues/get' else None
    player = np_.NowPlaying(None, None, None, connect=mpd, ma_token=lambda: 'tok', ma_call=caller)
    now = player.read('music_assistant_dlna')
    assert (now['title'], now['artist'], now['album']) == ('小幸运', '田馥甄', '我的少女时代 电影原声带')
    assert now['duration'] == 240.0 and now['position'] >= 40 and now['controls'] == {'toggle': True, 'next': True, 'previous': True, 'seek': True}
    assert 'file' not in now and calls[0] == ('player_queues/get', {'queue_id': 'mpd_127.0.0.1_6601'}, 'tok')
    player.command('next'); player.command('seek', 95.7); player.command('toggle')
    assert calls[-3:] == [('player_queues/next', {'queue_id': 'mpd_127.0.0.1_6601'}, 'tok'),
                          ('player_queues/seek', {'queue_id': 'mpd_127.0.0.1_6601', 'position': 95}, 'tok'),
                          ('player_queues/play_pause', {'queue_id': 'mpd_127.0.0.1_6601'}, 'tok')]
    assert mpd.sent == []                                     # MPD's own next/seek never used on a flow stream


def test_ma_without_a_token_says_how_to_get_one():
    mpd = FakeMPD({'file': MA_FILE, 'Name': 'Music Assistant'}, {'elapsed': '98.6'})
    player = np_.NowPlaying(None, None, None, connect=mpd)
    now = player.read('music_assistant_dlna')
    assert now['title'] == 'Music Assistant' and 'set-ma-token' in now['reason'] and not now['controls']['seek']
    with pytest.raises(ValueError, match='set-ma-token'): player.command('next')
    player.command('toggle'); assert mpd.sent == ['pause 1']   # pause/resume still works through MPD


def test_ma_queue_shape_and_token_file(tmp_path):
    from hifimate import ma_api
    now = ma_api.now(ma_queue(state='paused', elapsed=12.0, next_item=False), clock=lambda: 5000.0)
    assert now['state'] == 'pause' and now['position'] == 12.0 and not now['controls']['next']
    assert ma_api.now(ma_queue(elapsed=10.0, updated=1000.0), clock=lambda: 1005.0)['position'] == 15.0
    radio = ma_queue(); radio['current_item']['streamdetails'] = {'stream_metadata': {'title': 'Live', 'artist': 'DJ'}}
    assert (ma_api.now(radio)['title'], ma_api.now(radio)['artist']) == ('Live', 'DJ')
    assert ma_api.now({'state': 'idle'}) is None
    (tmp_path / 'config').mkdir()
    with pytest.raises(ValueError): ma_api.save_token(tmp_path, 'short')
    ma_api.save_token(tmp_path, ' ' + 'a' * 40 + '\n')
    assert ma_api.load_token(tmp_path) == 'a' * 40 and (tmp_path / 'config/ma-token').stat().st_mode & 0o777 == 0o600


def test_ma_stopped_queue_keeps_its_track_and_resumes_through_ma():
    from hifimate import ma_api
    mpd = FakeMPD({'file': MA_FILE, 'Name': 'Music Assistant'}, {'state': 'stop', 'songid': '5'})
    calls = []
    def caller(command, args, token):
        calls.append((command, args)); return ma_queue(state='idle', elapsed=12.0) if command == 'player_queues/get' else None
    player = np_.NowPlaying(None, None, None, connect=mpd, ma_token=lambda: 'tok', ma_call=caller)
    player.source = 'music_assistant_dlna'                       # it played before; MA has since stopped it
    now = player.read(None)
    assert (now['title'], now['state'], now['duration']) == ('小幸运', 'stop', 240.0)
    assert now['controls'] == {'toggle': True, 'next': False, 'previous': False, 'seek': False}
    player.command('toggle')
    assert calls[-1] == ('player_queues/play_pause', {'queue_id': 'mpd_127.0.0.1_6601'}) and mpd.sent == []
    assert ma_api.now({'state': 'idle', 'current_item': None}) is None

