"""Lyrics for the track playing now (fake ncm-api, no network)."""
import io
import json
from urllib.parse import parse_qs, urlsplit
from hifimate import lyrics
from hifimate.ctrl import App
from test_ctrl import FakeCollector, serving
import urllib.request

LRC = '[00:00.000] 作词 : 林夕\n[00:21.96]拦路雨偏似雪花 饮泣的你冻吗\n[00:27.5][01:40.25]这风褛我给你磨到有襟花\nnot a line\n'
SONG = {'id': 65766, 'name': '富士山下', 'ar': [{'name': '陈奕迅'}], 'dt': 258902}


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): pass


class FakeAPI:
    def __init__(self, match=(), search=(), lyric=None):
        self.match, self.search, self.lyric, self.urls = list(match), list(search), lyric, []
    def __call__(self, url, timeout):
        self.urls.append(url); part = urlsplit(url); query = {k: v[-1] for k, v in parse_qs(part.query).items()}
        if part.path == '/search/match':
            assert len(query['md5']) == 32                         # without a persistId it never matches
            body = {'result': {'songs': [{**s, 'artists': s.get('ar'), 'duration': s.get('dt')} for s in self.match]}}
        elif part.path == '/cloudsearch': body = {'result': {'songs': self.search}}
        else: body = self.lyric or {'lrc': {'lyric': LRC}, 'tlyric': {'lyric': ''}}
        return Response(json.dumps(body).encode())


def test_parse_lrc_and_merge_a_translation():
    lines = lyrics.parse_lrc(LRC)
    assert lines == [(0.0, '作词 : 林夕'), (21.96, '拦路雨偏似雪花 饮泣的你冻吗'), (27.5, '这风褛我给你磨到有襟花'), (100.25, '这风褛我给你磨到有襟花')]
    merged = lyrics.merge('[02:08.36]On a dark desert highway\n[02:12.00]\n', '[02:08.20]行驶在漆黑荒芜的公路上\n')
    assert merged == [[128.36, 'On a dark desert highway', '行驶在漆黑荒芜的公路上', None], [132.0, '', None, None]]


YRC = ('{"t":0,"c":[{"tx":"作词: "},{"tx":"宇多田ヒカル"}]}\n'
       '[20570,1900](20570,360,0)初(20930,150,0)め(21080,120,0)て\n'
       '[91040,500](91040,140,0)ま(91180,140,0)あ(91320,0,0)、\n'
       '[46870,800](46870,300,0)Can (47170,500,0)you\n')


def test_word_timed_lyrics_and_their_translation():
    lines = lyrics.merge_words(YRC, '[00:20.570]第一次去卢浮宫时\n[00:47.000]你能\n')
    assert lines[0] == [20.57, '初めて', '第一次去卢浮宫时', [[20.57, 0.36, '初'], [20.93, 0.15, 'め'], [21.08, 0.12, 'て']]]
    assert lines[1][1] == 'Can you' and lines[1][2] == '你能' and lines[1][3][0] == [46.87, 0.3, 'Can ']
    assert lines[2][3][-1] == [91.32, 0.0, '、'] and lines[2][2] is None          # sorted, credits skipped
    words, timed = lyrics.from_response({'yrc': {'lyric': YRC}, 'ytlrc': {'lyric': ''}, 'lrc': {'lyric': LRC}})
    assert timed and len(words) == 3
    plain, timed = lyrics.from_response({'yrc': {'lyric': ''}, 'lrc': {'lyric': LRC}})
    assert not timed and plain[1] == [21.96, '拦路雨偏似雪花 饮泣的你冻吗', None, None]


def test_match_first_then_lyrics_and_a_cache():
    api = FakeAPI(match=[SONG])
    source = lyrics.Lyrics('http://127.0.0.1:3003', api)
    now = {'title': '富士山下', 'artist': '陈奕迅', 'album': '', 'duration': 259.21}
    result = source.for_track(now)
    assert result['id'] == 65766 and result['lines'][1] == [21.96, '拦路雨偏似雪花 饮泣的你冻吗', None, None]
    assert result['reason'] is None and result['words'] is False
    assert [urlsplit(u).path for u in api.urls] == ['/search/match', '/lyric/new']
    assert source.for_track(dict(now)) is result and len(api.urls) == 2     # one lookup per track
    timed = lyrics.Lyrics('http://x', FakeAPI(match=[SONG], lyric={'yrc': {'lyric': YRC}, 'lrc': {'lyric': LRC}}))
    assert timed.for_track(now)['words'] is True


def test_search_fallback_picks_the_version_that_fits():
    live = {'id': 64517, 'name': '富士山下(Live)', 'ar': [{'name': '陈奕迅'}], 'dt': 298973}
    cover = {'id': 1, 'name': '富士山下', 'ar': [{'name': '某翻唱'}], 'dt': 259000}
    other = {'id': 2724692015, 'name': '富士山下', 'ar': [{'name': '陈奕迅'}], 'dt': 259133}
    api = FakeAPI(search=[live, cover, SONG, other])
    found = lyrics.Lyrics('http://x', api).find('富士山下', '陈奕迅', '', 259.21)
    assert found == 2724692015                      # live is too long, cover is someone else, closest length wins
    assert lyrics.Lyrics('http://x', FakeAPI(search=[live, cover])).find('富士山下', '陈奕迅', '', 259.21) is None


def test_nothing_found_or_no_synced_lyrics_or_api_down():
    assert lyrics.Lyrics('http://x', FakeAPI()).for_track({'title': '无名', 'artist': 'x'})['reason'] == '没有找到这首歌的歌词'
    plain = FakeAPI(match=[SONG], lyric={'nolyric': True})
    assert lyrics.Lyrics('http://x', plain).for_track({'title': '富士山下', 'artist': '陈奕迅'})['reason'] == '这首歌没有滚动歌词'
    assert [urlsplit(u).path for u in plain.urls][-2:] == ['/lyric/new', '/lyric']          # older builds: plain /lyric
    calls = []
    def down(url, timeout): calls.append(url); raise ConnectionRefusedError()
    source = lyrics.Lyrics('http://x', down)
    assert '没有响应' in source.for_track({'title': 'a'})['reason']
    source.for_track({'title': 'a'}); assert len(calls) == 2                 # a failure is not cached
    assert source.for_track({'title': None})['reason'] == '没有曲目信息'


def test_base_url_reads_the_installed_ma_env(tmp_path):
    assert lyrics.base_url(tmp_path) == 'http://127.0.0.1:3003'
    (tmp_path / 'music-assistant').mkdir()
    (tmp_path / 'music-assistant/.env').write_text('MUSIC_ASSISTANT_NCM_API_PORT=3010\nMUSIC_ASSISTANT_NCM_API_BIND=0.0.0.0\n')
    assert lyrics.base_url(tmp_path) == 'http://127.0.0.1:3010'


def test_endpoint_answers_for_the_current_track_by_default(tmp_path):
    collector = FakeCollector()
    app = App(tmp_path, collector, lambda *a, **k: '', lambda *a, **k: {}, lambda prefix: None)
    app.lyrics = lyrics.Lyrics('http://x', FakeAPI(match=[SONG]))
    with serving(app) as address:
        def get():
            with urllib.request.urlopen(f'http://{address[0]}:{address[1]}/ctrl/api/lyrics', timeout=5) as r:
                return json.loads(r.read())
        assert get() == {'key': None, 'id': None, 'lines': [], 'reason': None}
        collector.snapshot = lambda: {'now': {'title': '富士山下', 'artist': '陈奕迅', 'album': None, 'duration': 259.2}}
        body = get()
    assert body['key'] == '富士山下|陈奕迅|' and body['id'] == 65766 and len(body['lines']) == 4


def test_endpoint_looks_up_a_named_track_for_the_ma_browser_player(tmp_path):
    collector = FakeCollector()
    app = App(tmp_path, collector, lambda *a, **k: '', lambda *a, **k: {}, lambda prefix: None)
    api = FakeAPI(match=[SONG]); app.lyrics = lyrics.Lyrics('http://x', api)
    with serving(app) as address:
        url = f'http://{address[0]}:{address[1]}/ctrl/api/lyrics?title=%E5%AF%8C%E5%A3%AB%E5%B1%B1%E4%B8%8B&artist=%E9%99%88%E5%A5%95%E8%BF%85&duration=259.2'
        with urllib.request.urlopen(url, timeout=5) as r:
            body = json.loads(r.read())
        with urllib.request.urlopen(url.replace('duration=259.2', 'duration=bogus') + '&title2=' + 'x' * 500, timeout=5) as r:
            assert json.loads(r.read())['key'] == '富士山下|陈奕迅|'
    assert body['key'] == '富士山下|陈奕迅|' and body['id'] == 65766
    assert 'duration=259.2' in api.urls[0]


class FakeMigu:
    """migu-api: /search answers SONGS, /lyric the LRC of any content id."""
    def __init__(self, songs=(), lrc=LRC):
        self.songs, self.lrc, self.urls = list(songs), lrc, []
    def __call__(self, url, timeout):
        self.urls.append(url); part = urlsplit(url)
        if part.path == '/migu/search':
            body = {'success': True, 'data': {'items': [{'song': s} for s in self.songs]}}
        else:
            body = {'success': True, 'lrc': self.lrc}
        return Response(json.dumps(body).encode())


def migu_song(content_id, name, singer, duration):
    return {'contentId': content_id, 'songName': name, 'singerList': [{'name': singer}], 'duration': duration}


def test_a_migu_track_uses_its_own_lyrics_first():
    netease, migu = FakeAPI(match=[SONG]), FakeMigu()
    source = lyrics.Lyrics('http://n', netease, migu=lyrics.Migu('http://m/migu', migu))
    result = source.for_track({'title': '过火', 'artist': '张信哲', 'duration': 302, 'origin': 'migu', 'origin_id': '600929000006961316'})
    assert (result['source'], result['id']) == ('migu', '600929000006961316') and len(result['lines']) == 4
    assert parse_qs(urlsplit(migu.urls[0]).query) == {'contentId': ['600929000006961316']} and not netease.urls


def test_migu_search_when_netease_has_nothing():
    migu = FakeMigu(songs=[migu_song('1', '晴天', '张信哲', 264), migu_song('2', '晴天', '周杰伦', 270)])
    source = lyrics.Lyrics('http://n', FakeAPI(), migu=lyrics.Migu('http://m/migu', migu))
    result = source.for_track({'title': '晴天', 'artist': '周杰伦', 'duration': 269.5})
    assert (result['source'], result['id']) == ('migu', '2')


def test_guess_song_and_singer_from_video_titles():
    assert lyrics.guess('周杰伦 - 晴天MV 2160P修复版', 'up') == ('周杰伦', '晴天')
    assert lyrics.guess('晴天-周杰伦', 'up') == ('晴天', '周杰伦')
    assert lyrics.guess('【Hi-Res无损】｜《晴天》- 周杰伦', 'up') == ('晴天', '周杰伦')
    assert lyrics.guess('刘若英-后来「后来 我总算学会了 如何去爱」动态歌词', 'up') == ('刘若英', '后来')
    assert lyrics.guess('晴天', 'up') == ('晴天', None)


def test_bilibili_matches_without_the_artist_but_only_on_a_near_identical_length():
    jay = {'id': 7, 'name': '晴天', 'ar': [{'name': '周杰伦'}], 'dt': 269600}
    cover = {'id': 8, 'name': '晴天 (原唱 周杰伦)', 'ar': [{'name': 'RyaVocal'}], 'dt': 270700}
    other = {'id': 9, 'name': '晴天', 'ar': [{'name': '别人'}], 'dt': 230000}
    api = FakeAPI(search=[cover, other, jay])
    source = lyrics.Lyrics('http://n', api)
    # The uploader is no singer; "晴天-周杰伦" is split, and both orders are tried.
    found = source.for_track({'title': '晴天-周杰伦', 'artist': '某UP主', 'duration': 270, 'origin': 'bilibili'})
    assert found['id'] == 7
    assert all(urlsplit(u).path != '/search/match' for u in api.urls)       # fuzzy: search only
    # No singer in the title: the length alone decides, and must be close.
    assert lyrics.Lyrics('http://n', FakeAPI(search=[cover, other, jay])).for_track(
        {'title': '晴天', 'artist': '某UP主', 'duration': 271, 'origin': 'bilibili'})['id'] == 7
    for duration in (303, None):                                         # a different recording, or unknown
        assert lyrics.Lyrics('http://n', FakeAPI(search=[cover, other, jay])).for_track(
            {'title': '晴天', 'artist': '某UP主', 'duration': duration, 'origin': 'bilibili'})['id'] is None
    # Strict tracks stay strict: another singer's 晴天 is never taken.
    assert lyrics.Lyrics('http://n', FakeAPI(search=[other])).find('晴天', '周杰伦', '', 230) is None


def test_ma_queue_carries_the_track_origin():
    from hifimate import ma_api
    queue = {'state': 'playing', 'elapsed_time': 1, 'current_item': {'name': 'x', 'duration': 300, 'media_item': {
        'name': '过火', 'artists': [{'name': '张信哲'}],
        'provider_mappings': [{'provider_domain': 'migu', 'item_id': '600929000006961316', 'provider_instance': 'migu--a'}]}}}
    now = ma_api.now(queue, clock=lambda: 0)
    assert (now['origin'], now['origin_id']) == ('migu', '600929000006961316')


def test_endpoint_accepts_only_a_known_origin(tmp_path):
    collector = FakeCollector()
    app = App(tmp_path, collector, lambda *a, **k: '', lambda *a, **k: {}, lambda prefix: None)
    seen = []
    app.lyrics = type('L', (), {'for_track': lambda self, now: seen.append(now) or {'id': None, 'lines': [], 'reason': None}})()
    app.lyrics_now({'title': 'a', 'origin': 'migu', 'origin_id': '600929000006961316'})
    app.lyrics_now({'title': 'a', 'origin': 'migu', 'origin_id': '../etc'})
    app.lyrics_now({'title': 'a', 'origin': 'spotify', 'origin_id': '1'})
    app.lyrics_now({'title': 'a', 'origin': 'bilibili'})
    assert [(n['origin'], n['origin_id']) for n in seen] == [('migu', '600929000006961316'), (None, None), (None, None), ('bilibili', None)]

