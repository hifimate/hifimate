"""Passwords for an unattended install, never taken from the command line.

Each secret comes from an environment variable (one command line:
`HIFIMATE_MA_ADMIN_PASSWORD=... install.sh ...`) or from a file named by
`*_FILE` (install.sh's --ma-admin-password-file sets that). argv is visible to every local user in the process list; the
environment and a 0600 file are not.

usage: install_secrets.py check [--ma]    validate what is given, print nothing secret

There is no control password here: the control page checks the Linux
password of the user it runs as (hifimate/ctrl_auth.py); the system install
sets it (hifimate/cli.py).
"""
import os
from pathlib import Path
import sys

SECRETS = {
    'ma': 'HIFIMATE_MA_ADMIN_PASSWORD',
}
MA_USER = 'HIFIMATE_MA_ADMIN_USER'
MAX = 1024


def read(kind, environ=os.environ):
    """The secret, or None when neither the variable nor its file is set."""
    name = SECRETS[kind]
    value, file = environ.get(name), environ.get(name + '_FILE')
    if value is not None and file:
        raise ValueError(f'{name} 和 {name}_FILE 只能给一个')
    if file:
        path = Path(file)
        if not path.is_file():
            raise ValueError(f'{name}_FILE 不是普通文件：{file}')
        value = path.read_text(encoding='utf-8')
        # one trailing newline is the file's, not the password's
        value = value[:-1] if value.endswith('\n') else value
        value = value[:-1] if value.endswith('\r') else value
    if value is None:
        return None
    if not 8 <= len(value) <= MAX:
        raise ValueError(f'{name}：密码长度须为 8–{MAX} 个字符')
    if '\n' in value or '\r' in value:
        raise ValueError(f'{name}：密码不能包含换行')
    return value


def ma_admin(environ=os.environ):
    """(username, password) for MA's first user, or None when neither is given."""
    user = (environ.get(MA_USER) or '').strip()
    password = read('ma', environ)
    if not user and password is None:
        return None
    if not user or password is None:
        raise ValueError(f'MA 管理员需要同时给出用户名（--ma-admin-user 或 {MA_USER}）和密码'
                         f'（--ma-admin-password-file 或 {SECRETS["ma"]}）')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ma_setup import invalid_username
    problem = invalid_username(user)
    if problem:
        raise ValueError(f'MA 用户名：{problem}')
    return user, password


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv[:1] == ['check'] and set(argv[1:]) <= {'--ma'}:
            if '--ma' in argv:
                ma_admin()
            return 0
    except (OSError, UnicodeError, ValueError) as error:
        print(f'install: {error}', file=sys.stderr)
        return 2
    print(__doc__.split('\n\n')[-1], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
