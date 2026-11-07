"""The installer's first Music Assistant user (fake MA, no network)."""
import io
import json
import sys
import urllib.error
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ma_setup  # noqa: E402


class Response(io.BytesIO):
    def __init__(self, data=b'', status=200):
        super().__init__(data); self.status = status
    def __enter__(self): return self
    def __exit__(self, *_): pass


def http_error(code, body=b''):
    return urllib.error.HTTPError('http://x/setup', code, 'x', {}, io.BytesIO(body))


def test_needs_setup_reads_mas_setup_page_and_waits_for_startup():
    assert ma_setup.needs_setup(opener=lambda url, timeout: Response()) is True
    def has_user(url, timeout): raise http_error(403)
    assert ma_setup.needs_setup(opener=has_user) is False
    answers = [ConnectionRefusedError(), http_error(503), Response()]
    def starting(url, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception): raise answer
        return answer
    assert ma_setup.needs_setup(opener=starting, sleep=lambda s: None) is True and not answers
    def down(url, timeout): raise ConnectionRefusedError()
    clock = iter(range(0, 1000, 10))
    with pytest.raises(RuntimeError, match='没有响应'):
        ma_setup.needs_setup(opener=down, clock=lambda: next(clock), sleep=lambda s: None)


def test_create_posts_mas_setup_and_drops_the_session_token():
    sent = []
    def opener(request, timeout):
        sent.append((request.full_url, json.loads(request.data)))
        return Response(json.dumps({'success': True, 'token': 'eyJ.session'}).encode())
    assert ma_setup.create('alice', 'correct horse', opener=opener) is None     # nothing returned, nothing kept
    assert sent == [('http://127.0.0.1:8095/setup', {'username': 'alice', 'password': 'correct horse', 'device_name': 'HiFiMate installer'})]
    def refused(request, timeout): raise http_error(400, b'{"success": false, "error": "Setup already completed"}')
    with pytest.raises(RuntimeError, match='already completed'):
        ma_setup.create('alice', 'correct horse', opener=refused)


def test_ask_validates_and_cannot_be_skipped():
    said = []
    names = iter(['', 'hifimate-ctrl', 'a', 'alice'])
    secrets = iter(['short', 'long enough', 'different', 'long enough', 'long enough'])
    answer = ma_setup.ask(read=lambda prompt: next(names), secret=lambda prompt: next(secrets), say=said.append)
    assert answer == ('alice', 'long enough')
    assert any('HiFiMate' in s for s in said) and sum('2 个字符' in s for s in said) == 2
    assert any('至少 8 位' in s for s in said) and any('不一致' in s for s in said)


@pytest.fixture
def no_admin(monkeypatch):
    for name in ('HIFIMATE_MA_ADMIN_USER', 'HIFIMATE_MA_ADMIN_PASSWORD', 'HIFIMATE_MA_ADMIN_PASSWORD_FILE'):
        monkeypatch.delenv(name, raising=False)


def test_main_skips_quietly_when_ma_already_has_a_user(monkeypatch, capsys, no_admin):
    monkeypatch.setattr(ma_setup, 'needs_setup', lambda url: False)
    monkeypatch.setattr(ma_setup, 'ask', lambda: pytest.fail('must not ask'))
    assert ma_setup.main([], interactive=True) == 0 and capsys.readouterr().err == ''
    monkeypatch.setattr(ma_setup, 'needs_setup', lambda url: True)
    monkeypatch.setattr(ma_setup, 'ask', lambda: ('alice', 'long enough'))
    def fail(*a, **k): raise RuntimeError('boom')
    monkeypatch.setattr(ma_setup, 'create', fail)
    assert ma_setup.main([], interactive=True) == 1 and 'boom' in capsys.readouterr().err


def test_main_fails_without_an_owner_when_it_cannot_ask(monkeypatch, capsys, no_admin):
    monkeypatch.setattr(ma_setup, 'needs_setup', lambda url: True)
    monkeypatch.setattr(ma_setup, 'ask', lambda: pytest.fail('must not ask'))
    monkeypatch.setattr(ma_setup, 'create', lambda *a, **k: pytest.fail('nothing to create'))
    assert ma_setup.main(['--non-interactive']) == 1
    assert 'HIFIMATE_MA_ADMIN_PASSWORD' in capsys.readouterr().err
    def eof(): raise EOFError
    monkeypatch.setattr(ma_setup, 'ask', eof)
    assert ma_setup.main([], interactive=True) == 1 and '中止' in capsys.readouterr().err


def test_main_creates_the_owner_from_the_environment(monkeypatch, capsys, no_admin, tmp_path):
    made = []
    monkeypatch.setattr(ma_setup, 'needs_setup', lambda url: True)
    monkeypatch.setattr(ma_setup, 'ask', lambda: pytest.fail('must not ask'))
    monkeypatch.setattr(ma_setup, 'create', lambda user, password, url: made.append((user, password)))
    secret = tmp_path / 'ma.pw'
    secret.write_text('correct horse\n')
    monkeypatch.setenv('HIFIMATE_MA_ADMIN_USER', 'alice')
    monkeypatch.setenv('HIFIMATE_MA_ADMIN_PASSWORD_FILE', str(secret))
    assert ma_setup.main(['--non-interactive']) == 0 and made == [('alice', 'correct horse')]
    assert 'correct horse' not in capsys.readouterr().err
    # already set up: the given owner is ignored, nothing changes
    monkeypatch.setattr(ma_setup, 'needs_setup', lambda url: False)
    assert ma_setup.main(['--non-interactive']) == 0 and len(made) == 1
    assert '忽略' in capsys.readouterr().err
