"""Music Assistant's first user, made by the installer (required, no skipping).

MA makes its owner (an admin) through its own first-time setup, POST /setup,
the call its setup page makes. Until then MA has no user and every browser
is sent to that page; asking here means nobody has to open MA first.

The MA user hifimate-ctrl (hifimate_ctrl_token.py) is made only after this: MA treats any user
as "setup done", so creating ours first would take the owner role away. That
is why the install cannot skip the owner: without it there is no /ctrl
control of MA (now playing, transport) unless the owner is made later in the
browser and the install run again.

The owner comes from HIFIMATE_MA_ADMIN_USER plus HIFIMATE_MA_ADMIN_PASSWORD
(or its _FILE; see install_secrets.py), or is asked for in a terminal.
--non-interactive never asks: without them, a fresh MA fails the install.

MA has no anonymous mode: a browser logs in once, and the session renews on
use for up to 90 days.
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get('HIFIMATE_MA_URL', 'http://127.0.0.1:8095')   # MA's own port (host network)
RESERVED = {'hifimate-ctrl', 'homeassistant_system_user'}


def invalid_username(username):
    """Why MA's owner cannot have this name, or None."""
    if username.lower() in RESERVED:
        return '这个名字留给 HiFiMate 自己用，请换一个'
    if len(username) < 2:
        return '用户名至少 2 个字符'
    return None


def needs_setup(url=URL, opener=urllib.request.urlopen, wait=60, clock=time.monotonic, sleep=time.sleep):
    """True while MA has no user (it serves /setup), False once it has one (403)."""
    deadline = clock() + wait
    while True:
        try:
            with opener(url + '/setup', timeout=5) as response:
                return response.status == 200
        except urllib.error.HTTPError as error:
            if error.code == 403:
                return False
            last = error
        except OSError as error:                 # still starting
            last = error
        if clock() >= deadline:
            raise RuntimeError(f'Music Assistant 没有响应：{last}')
        sleep(2)


def create(username, password, url=URL, opener=urllib.request.urlopen):
    """Create MA's owner. The session token MA returns is dropped, never kept."""
    body = json.dumps({'username': username, 'password': password, 'device_name': 'HiFiMate installer'}).encode()
    request = urllib.request.Request(url + '/setup', body, {'Content-Type': 'application/json'})
    try:
        with opener(request, timeout=30) as response:
            reply = json.loads(response.read(1 << 16) or b'{}')
    except urllib.error.HTTPError as error:
        try:
            reply = json.loads(error.read(1 << 16) or b'{}')
        except ValueError:
            reply = {}
        reply.setdefault('error', f'HTTP {error.code}')
    if not reply.get('success'):
        raise RuntimeError(reply.get('error') or 'Music Assistant 拒绝了创建')


def ask(read=input, secret=getpass.getpass, say=lambda text: print(text, file=sys.stderr)):
    """(username, password), asked until both are valid."""
    say('Music Assistant 还没有用户。现在创建它的管理员账户（登录 MA 网页用；'
        'HiFiMate 控制页对 MA 的控制也依赖它）。')
    while True:
        username = read('MA 用户名：').strip()
        problem = invalid_username(username)
        if problem:
            say(problem); continue
        break
    while True:
        password = secret('MA 密码（至少 8 位）：')
        if len(password) < 8:
            say('密码至少 8 位'); continue
        if password != secret('再次输入：'):
            say('两次密码不一致'); continue
        return username, password


def main(argv=None, interactive=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--url', default=URL)
    parser.add_argument('--non-interactive', action='store_true', help='never ask; fail without the variables')
    args = parser.parse_args(argv)
    if interactive is None:
        interactive = not args.non_interactive and sys.stdin.isatty() and sys.stderr.isatty()
    from install_secrets import ma_admin
    try:
        given = ma_admin()
        if not needs_setup(args.url):
            if given:
                print('Music Assistant 已有用户，忽略给出的 MA 管理员', file=sys.stderr)
            return 0
        if given:
            answer = given
        elif interactive:
            answer = ask()
        else:
            print('Music Assistant 还没有用户：用 --ma-admin-user 加 --ma-admin-password-file '
                  '（或 HIFIMATE_MA_ADMIN_USER / HIFIMATE_MA_ADMIN_PASSWORD）给出管理员，或在终端里交互安装',
                  file=sys.stderr)
            return 1
        create(*answer, url=args.url)
    except (RuntimeError, OSError, ValueError) as error:
        print(f'未能创建 Music Assistant 用户：{error}', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n未创建 Music Assistant 管理员，安装中止', file=sys.stderr)
        return 1
    print(f'已创建 Music Assistant 管理员 {answer[0]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
