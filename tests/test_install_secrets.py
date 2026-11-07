"""Install passwords come from the environment or a file, never argv."""
import subprocess
import sys
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import install_secrets  # noqa: E402


def test_read_from_variable_or_file(tmp_path):
    assert install_secrets.read('ma', {}) is None
    assert install_secrets.read('ma', {'HIFIMATE_MA_ADMIN_PASSWORD': 'long enough'}) == 'long enough'
    secret = tmp_path / 'pw'
    secret.write_text('with spaces  \r\n')
    assert install_secrets.read('ma', {'HIFIMATE_MA_ADMIN_PASSWORD_FILE': str(secret)}) == 'with spaces  '
    for environ, message in (
            ({'HIFIMATE_MA_ADMIN_PASSWORD': 'short'}, '8–1024'),
            ({'HIFIMATE_MA_ADMIN_PASSWORD': 'two\nlines here'}, '换行'),
            ({'HIFIMATE_MA_ADMIN_PASSWORD': 'long enough', 'HIFIMATE_MA_ADMIN_PASSWORD_FILE': str(secret)}, '只能给一个'),
            ({'HIFIMATE_MA_ADMIN_PASSWORD_FILE': str(tmp_path)}, '普通文件')):
        with pytest.raises(ValueError, match=message):
            install_secrets.read('ma', environ)


def test_ma_admin_needs_both_and_a_valid_name():
    assert install_secrets.ma_admin({}) is None
    both = {'HIFIMATE_MA_ADMIN_USER': 'alice', 'HIFIMATE_MA_ADMIN_PASSWORD': 'long enough'}
    assert install_secrets.ma_admin(both) == ('alice', 'long enough')
    for environ in ({'HIFIMATE_MA_ADMIN_USER': 'alice'}, {'HIFIMATE_MA_ADMIN_PASSWORD': 'long enough'}):
        with pytest.raises(ValueError, match='同时'):
            install_secrets.ma_admin(environ)
    with pytest.raises(ValueError, match='HiFiMate'):
        install_secrets.ma_admin({**both, 'HIFIMATE_MA_ADMIN_USER': 'hifimate-ctrl'})


def test_cli_prints_only_what_it_is_asked_for(tmp_path):
    def cli(*args, **env):
        return subprocess.run([sys.executable, '-B', str(SCRIPTS / 'install_secrets.py'), *args],
                              capture_output=True, text=True, env={'PATH': '/usr/bin:/bin', **env})
    checked = cli('check', '--ma', HIFIMATE_MA_ADMIN_USER='alice', HIFIMATE_MA_ADMIN_PASSWORD='long enough')
    assert checked.returncode == 0 and checked.stdout == ''
    bad = cli('check', '--ma', HIFIMATE_MA_ADMIN_PASSWORD='long enough')       # no user name
    assert bad.returncode == 2 and 'long enough' not in bad.stderr + bad.stdout
    assert cli('check', '--ctrl').returncode == 2                              # no control password any more
