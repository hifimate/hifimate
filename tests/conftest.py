"""Lifecycle tests never touch the caller's config, services or privilege tools."""
import hashlib
import json
import os
from pathlib import Path

import pytest


def record_file(prefix, name):
    """Register intentional fixture changes with the managed installation."""
    marker = prefix / '.hifimate-install-manifest'
    lines = marker.read_text().splitlines()
    lines = [line for line in lines if not line.endswith(' ' + name)]
    lines.append(hashlib.sha256((prefix / name).read_bytes()).hexdigest() + ' ' + name)
    data = ('\n'.join(lines) + '\n').encode()
    marker.write_bytes(data)
    (prefix / '.hifimate-install-manifest.sha256').write_text(
        hashlib.sha256(data).hexdigest() + '  .hifimate-install-manifest\n')


def fake_camilladsp_archive(path, version_text='CamillaDSP 4.1.3 (test)', features='pulse-backend, websocket'):
    """An archive shaped like the upstream release: one executable called camilladsp
    (--version names the release, --help lists the features, as upstream does)."""
    import io
    import tarfile
    script = (f'#!/bin/sh\ncase "$1" in --version) echo "{version_text}" ;; '
              f'--help) echo "Built with features: {features}" ;; esac\nexit 0\n').encode()
    with tarfile.open(path, 'w:gz') as tar:
        info = tarfile.TarInfo('camilladsp'); info.mode = 0o755; info.size = len(script)
        tar.addfile(info, io.BytesIO(script))
    return path


class FakeMA:
    """MA's /setup: 403 once it has a user (the default), 200 + POST before."""
    def __init__(self):
        import http.server
        import threading
        fake = self
        self.has_user, self.created = True, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, code, body=b'{}'):
                self.send_response(code); self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self.reply(403 if fake.has_user else 200)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if fake.has_user: return self.reply(403)
                fake.created.append((body['username'], body['password'])); fake.has_user = True
                self.reply(200, b'{"success": true}')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(autouse=True)
def no_real_pam(monkeypatch):
    """Never this host's PAM: no Linux password is ever right unless a test says so."""
    from hifimate import ctrl_auth
    monkeypatch.setattr(ctrl_auth, 'pam_check', lambda password, user=None, service=None: False)
    monkeypatch.setattr(ctrl_auth, 'pam_available', lambda: True)


@pytest.fixture(autouse=True)
def no_system_install(tmp_path, monkeypatch):
    """Tests never see (or write) this host's system install record."""
    from hifimate import system
    monkeypatch.setattr(system, 'RECORD', tmp_path / 'etc-hifimate' / 'install.json')


@pytest.fixture(autouse=True)
def fake_ma(monkeypatch):
    """Never the real Music Assistant: the installer's MA owner step talks to this."""
    ma = FakeMA()
    monkeypatch.setenv('HIFIMATE_MA_URL', ma.url)
    for name in ('HIFIMATE_MA_ADMIN_USER', 'HIFIMATE_MA_ADMIN_PASSWORD', 'HIFIMATE_MA_ADMIN_PASSWORD_FILE',
                 'HIFIMATE_CTRL_PASSWORD', 'HIFIMATE_CTRL_PASSWORD_FILE'):
        monkeypatch.delenv(name, raising=False)
    yield ma
    ma.server.shutdown(); ma.server.server_close()


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    runtime = home / '.runtime'
    runtime.mkdir()
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
    guard = tmp_path / 'host-guard'
    guard.mkdir()
    for name in ('sudo', 'systemctl', 'docker', 'systemd-analyze', 'bluetoothctl', 'busctl', 'dbus-send', 'systemd-run', 'journalctl'):
        path = guard / name
        path.write_text('#!/bin/sh\necho "unexpected host command: $0 $*" >&2\nexit 99\n')
        path.chmod(0o755)
    monkeypatch.setenv('PATH', f'{guard}:{os.environ["PATH"]}')
    # No network: the installer takes a fake "official" CamillaDSP archive.
    archive = fake_camilladsp_archive(tmp_path / 'camilladsp-linux-pulseaudio-test.tar.gz')
    monkeypatch.setenv('HIFIMATE_CAMILLADSP_ARCHIVE', str(archive))
    monkeypatch.setenv('HIFIMATE_CAMILLADSP_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
