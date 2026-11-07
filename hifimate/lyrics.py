"""Synced lyrics for the track playing now, from the local Netease and Migu APIs.

The Music Assistant stack runs NeteaseCloudMusicApi Enhanced (ncm-api,
MUSIC_ASSISTANT_NCM_API_PORT, default 3003) and migu-api-enhanced (migu-api,
MUSIC_ASSISTANT_MIGU_API_PORT, default 3004) on loopback.

A track that comes from MA's migu provider has its own lyrics: its content
id asks migu-api directly. Otherwise it is found from what now playing knows
(title, artist, album, duration), Netease first: /search/match (it only
answers with a persistId, so a stable md5 of the tags is sent), then
/cloudsearch scored on name, artist and duration; /lyric/new gives
word-timed lyrics (yrc) for many songs, else the line-timed LRC. When
Netease has nothing, migu-api's search and LRC are tried.

Bilibili videos rarely carry the singer: the "artist" is the uploader and
the title is decorated ("周杰伦 - 晴天MV 2160P修复版"). For those the title is
cleaned and split into song and singer when it says both; matching then
does not require the artist, but the song name must match exactly and the
duration closely, since the same name is often sung by others.

A line is [start s, text, translation or None, words or None], words being
[[start s, duration s, text], ...] for word-timed lyrics.

Only the current track is looked up (no client-chosen queries), and every
result, found or not, is cached so a track costs one lookup.
"""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re
import threading
import unicodedata
import urllib.parse
import urllib.request

DEFAULT_PORT = 3003
TIMEOUT = 4
CACHE = 128
DURATION_SLACK = 4.0       # s: a different version (live, edit) is usually further off
YRC_LINE = re.compile(r'^\[(\d+),(\d+)\](.*)$')
YRC_WORD = re.compile(r'\((\d+),(\d+),\d+\)([^(]*)')
TRANSLATION_SLACK = 0.35   # s: a translation belongs to the line starting this close
TAG = re.compile(r'\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]')
EXTRA = re.compile(r'[(（\[【].*?[)）\]】]')
MAX_LINES = 400
MIGU_PORT = 3004
FUZZY_SLACK = 2.5          # s: without a trusted artist, only a near-identical length is the same recording
# Video decorations around a song title: quality, MV, lyrics, live-stream tags.
NOISE = re.compile(r'(?i)(?:(?<![a-z])(?:mv|pv|hd|hq|4k|8k|\d{3,4}p|\d+fps|hi-?res|flac|live|cover)(?![a-z])|官方|完整版'
                   r'|官方mv|高音质|无损|动态歌词|歌词版?|字幕|修复版?|重制版?|纯享版?|现场版?|伴奏版?)')
QUOTED = re.compile(r'《([^》]{1,60})》')          # 「」 usually quotes a lyric line, not the title
DASH = re.compile(r'\s*[-–—|｜/]\s*')
TEXT = 200


def base_url(prefix, key='MUSIC_ASSISTANT_NCM_API_PORT', port=DEFAULT_PORT):
    """An API address from the installed MA .env (loopback only): ncm-api by default."""
    try:
        for line in (Path(prefix) / 'music-assistant/.env').read_text().splitlines():
            name, _, value = line.partition('=')
            if name.strip() == key and value.strip().isdecimal():
                port = int(value.strip())
    except OSError:
        pass
    return f'http://127.0.0.1:{port}'


def migu_url(prefix):
    return base_url(prefix, 'MUSIC_ASSISTANT_MIGU_API_PORT', MIGU_PORT) + '/migu'


def _clean(text):
    """A video title or part of it without decorations."""
    text = unicodedata.normalize('NFKC', text or '')
    text = re.sub(r'[\[【(（][^\]】)）]*[\]】)）]', ' ', text)
    return ' '.join(NOISE.sub(' ', text).split()).strip(' -–—|｜/~·.,，')


def guess(title, artist):
    """(song, singer or None) for a title that may be a decorated video title.

    "周杰伦 - 晴天MV 2160P修复版" -> ("晴天", "周杰伦"), "晴天-周杰伦" -> ("晴天", "周杰伦"),
    "《晴天》" -> ("晴天", None). Which side of a dash is the singer is decided by
    trying both, so the second element is only a candidate.
    """
    quoted = QUOTED.findall(title or '')
    if len(quoted) == 1:
        rest = _clean(QUOTED.sub(' ', title))
        return _clean(quoted[0]), rest or None
    head = re.split(r'[「『“]', title or '')[0]             # a quoted lyric line after the title
    parts = [part for part in (_clean(x) for x in DASH.split(head)) if part]
    if len(parts) == 2:
        return parts[0], parts[1]          # both orders are tried by the caller
    return _clean(head) or title, None


def _norm(text):
    text = unicodedata.normalize('NFKC', text or '').lower()
    return re.sub(r'[\s\W_]+', '', EXTRA.sub('', text))


def parse_lrc(text):
    """[(seconds, text)] sorted; lines with several tags appear at each time."""
    out = []
    for raw in (text or '').splitlines():
        tags = list(TAG.finditer(raw))
        if not tags:
            continue
        words = raw[tags[-1].end():].strip()[:TEXT]
        for tag in tags:
            seconds = int(tag[1]) * 60 + float(tag[2].replace(':', '.'))
            out.append((round(seconds, 2), words))
    out.sort(key=lambda line: line[0])
    return out[:MAX_LINES]


def parse_yrc(text):
    """[(seconds, text, words)] from Netease word-timed lyrics; credit lines (JSON) skipped."""
    out = []
    for raw in (text or '').splitlines():
        line = YRC_LINE.match(raw.strip())
        if not line:
            continue
        words = [[int(m[1]) / 1000, int(m[2]) / 1000, m[3][:TEXT]] for m in YRC_WORD.finditer(line[3]) if m[3]]
        if words:
            out.append((int(line[1]) / 1000, ''.join(w[2] for w in words)[:TEXT], words))
    out.sort(key=lambda line: line[0])
    return out[:MAX_LINES]


def _translations(text):
    return [(t, words) for t, words in parse_lrc(text) if words]


def _nearest(translations, t):
    best = min(translations, key=lambda item: abs(item[0] - t), default=None)
    return best[1] if best and abs(best[0] - t) <= TRANSLATION_SLACK else None


def merge(lrc, translation):
    """Line-timed lines, the translation joined on (nearly) equal times."""
    tr = _translations(translation)
    return [[t, words, _nearest(tr, t), None] for t, words in parse_lrc(lrc)]


def merge_words(yrc, translation):
    """Word-timed lines, likewise."""
    tr = _translations(translation)
    return [[t, text, _nearest(tr, t), words] for t, text, words in parse_yrc(yrc)]


def from_response(data):
    """Lines from a /lyric/new (or /lyric) answer: word-timed when it has them."""
    text = lambda key: (data.get(key) or {}).get('lyric')
    words = merge_words(text('yrc'), text('ytlrc') or text('tlyric'))
    return (words, True) if words else (merge(text('lrc'), text('tlyric')), False)


class Lyrics:
    def __init__(self, base, opener=urllib.request.urlopen, migu=None):
        self.base, self.opener, self.migu = base, opener, migu
        self.cache = OrderedDict(); self.lock = threading.Lock()

    def _get(self, path, **query):
        url = f'{self.base}{path}?{urllib.parse.urlencode(query)}'
        with self.opener(url, timeout=TIMEOUT) as response:
            return json.loads(response.read(4 << 20))

    @staticmethod
    def score(name, singers, length, title, artist, duration, fuzzy=False):
        """Higher is better; None when it is not this track.

        Strict: the artist must be one of the singers. Fuzzy (the artist is only a
        guess): a matching singer counts, a different one does not exclude, but
        then the length must be known and nearly equal.
        """
        if _norm(name) != _norm(title):
            return None
        if fuzzy and EXTRA.search(unicodedata.normalize('NFKC', name or '')) and not EXTRA.search(title or ''):
            return None                     # "晴天 (原唱 周杰伦)", "晴天 (钢琴版)": someone else's version
        singers = {_norm(s) for s in singers if s}
        wanted = {_norm(a) for a in re.split(r'\s*(?:/|&|,|、| feat\.? )\s*', artist or '') if a.strip()}
        hit = bool(wanted and wanted & singers)
        if wanted and not hit and not fuzzy:
            return None
        score = 2 if hit else 0
        slack = DURATION_SLACK if hit or not fuzzy else FUZZY_SLACK
        if duration and length:
            off = abs(length - duration)
            if off > slack:
                return None
            score += 1 - off / slack
        elif fuzzy and not hit:
            return None                     # a name alone is not enough
        return score

    def _score(self, song, title, artist, duration, fuzzy=False):
        singers = [a.get('name') for a in song.get('ar') or song.get('artists') or []]
        return self.score(song.get('name'), singers, (song.get('dt') or song.get('duration') or 0) / 1000,
                          title, artist, duration, fuzzy)

    def find(self, title, artist, album, duration, fuzzy=False):
        """The Netease song id for these tags, or None."""
        if not fuzzy:
            persist = hashlib.md5(f'{title}\n{artist}\n{album}'.encode()).hexdigest()
            matched = self._get('/search/match', title=title, artist=artist or '', album=album or '',
                                duration=duration or 0, md5=persist)
            for song in ((matched.get('result') or {}).get('songs') or []):
                if self._score(song, title, artist, duration) is not None:
                    return song['id']
        found = self._get('/cloudsearch', keywords=f'{title} {artist or ""}'.strip(), limit=20)
        scored = [(s, song) for song in ((found.get('result') or {}).get('songs') or [])
                  if (s := self._score(song, title, artist, duration, fuzzy)) is not None]
        return max(scored, key=lambda pair: pair[0])[1]['id'] if scored else None

    def netease(self, title, artist, album, duration, fuzzy=False):
        """(lines, word-timed) from Netease, or None when it has no such song."""
        song = self.find(title, artist, album, duration, fuzzy)
        if song is None:
            return None
        lines, timed = from_response(self._get('/lyric/new', id=song))
        if not lines:                                    # older API builds: plain /lyric
            lines, timed = from_response(self._get('/lyric', id=song))
        return ('netease', song), lines, timed

    def for_track(self, now):
        """Lyrics for a now-playing entry: {'id', 'source', 'lines', 'words', 'reason'}."""
        title = (now or {}).get('title')
        if not title:
            return {'id': None, 'lines': [], 'reason': '没有曲目信息'}
        artist, album, duration = now.get('artist'), now.get('album'), now.get('duration')
        origin, origin_id = now.get('origin'), now.get('origin_id')
        key = (title, artist, album, round(duration or 0), origin, origin_id)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key); return self.cache[key]
        try:
            found, failed = self.lookup(title, artist, album, duration, origin, origin_id)
        except (OSError, ValueError) as error:
            # Not cached: the API may just be starting.
            return {'id': None, 'lines': [], 'reason': f'歌词服务没有响应：{type(error).__name__}'}
        if found:
            (source, song), lines, timed = found
            result = {'id': song, 'source': source, 'lines': lines, 'words': timed,
                      'reason': None if lines else '这首歌没有滚动歌词'}
        else:
            result = {'id': None, 'source': None, 'lines': [], 'words': False,
                      'reason': '歌词服务没有响应' if failed else '没有找到这首歌的歌词'}
        if not failed or found:
            with self.lock:
                self.cache[key] = result
                while len(self.cache) > CACHE:
                    self.cache.popitem(last=False)
        return result

    def lookup(self, title, artist, album, duration, origin=None, origin_id=None):
        """((source, id), lines, word-timed) of the first source with synced lines, and whether any source failed."""
        attempts = []
        if origin == 'migu' and origin_id and self.migu:
            attempts.append(lambda: self.migu.by_id(origin_id))
        fuzzy = origin == 'bilibili'
        if fuzzy:
            song, singer = guess(title, artist)
            # The uploader may be the singer; either side of a dash may be the song.
            candidates = [(song, singer), (singer, song)] if singer else [(song, None)]
            candidates.append((song, artist))
        else:
            candidates = [(title, artist)]
        for name, who in candidates:
            if not name:
                continue
            attempts.append(lambda name=name, who=who: self.netease(name, who, album if not fuzzy else None, duration, fuzzy))
            if self.migu:
                attempts.append(lambda name=name, who=who: self.migu.search(name, who, duration, fuzzy, self.score))
        best, failed = None, False
        for attempt in attempts:
            try:
                found = attempt()
            except (OSError, ValueError):
                failed = True; continue
            if found and found[1]:
                return found, failed
            best = best or found               # a song without synced lines, if nothing better turns up
        return best, failed


class Migu:
    """migu-api-enhanced: lyrics by content id (MA's migu tracks) or by search."""
    def __init__(self, base, opener=urllib.request.urlopen):
        self.base, self.opener = base, opener

    def _get(self, path, **query):
        url = f'{self.base}{path}?{urllib.parse.urlencode(query)}'
        with self.opener(url, timeout=TIMEOUT) as response:
            return json.loads(response.read(4 << 20))

    def by_id(self, content_id):
        data = self._get('/lyric', contentId=content_id)
        lines = merge(data.get('lrc') if data.get('success') is not False else '', '')
        return ('migu', content_id), lines, False

    def search(self, title, artist, duration, fuzzy, score):
        data = self._get('/search', text=f'{title} {artist or ""}'.strip(), page=1)
        scored = []
        for item in ((data.get('data') or {}).get('items') or [])[:20]:
            song = item.get('song') if isinstance(item, dict) else None
            if not isinstance(song, dict) or not song.get('contentId'):
                continue
            singers = [s.get('name') for s in song.get('singerList') or [] if isinstance(s, dict)]
            value = score(song.get('songName'), singers, float(song.get('duration') or 0), title, artist, duration, fuzzy)
            if value is not None:
                scored.append((value, str(song['contentId'])))
        if not scored:
            return None
        return self.by_id(max(scored)[1])
