"""Check a built wheel carries everything scripts/install.sh installs.

usage: check_wheel.py dist/hifimate-*.whl

Every tracked file under the directories the installer reads (scripts, bin,
config, systemd, gui, music-assistant, the docs) must be in the wheel under
hifimate/_dist, the package under hifimate/, and nothing from tests or a
local Music Assistant deployment (.env, data) may be.
"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ('scripts/', 'bin/', 'config/', 'systemd/', 'gui/', 'music-assistant/', 'docs/')
DOCS = ('README.md', 'INSTALL.md', 'LICENSE', 'NOTICE.md')
FORBIDDEN = ('tests/', 'music-assistant/data/', '__pycache__')


def expected(tracked):
    want = set()
    for name in tracked:
        if name.startswith(DIST) or name in DOCS:
            want.add('hifimate/_dist/' + name)
        elif name.startswith('hifimate/'):
            want.add(name)
    return want


def check(wheel, tracked):
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = sorted(expected(tracked) - names)
    forbidden = sorted(n for n in names if any(f in n for f in FORBIDDEN) or n.endswith('/.env'))
    entry = [n for n in names if n.endswith('entry_points.txt')]
    problems = [f'missing: {n}' for n in missing] + [f'must not ship: {n}' for n in forbidden]
    if not entry or b'hifimate = hifimate.cli:main' not in zipfile.ZipFile(wheel).read(entry[0]):
        problems.append('no hifimate console script')
    return problems


def main():
    tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    problems = check(sys.argv[1], tracked)
    for line in problems:
        print(line, file=sys.stderr)
    print(f'{sys.argv[1]}: ' + ('ok' if not problems else f'{len(problems)} problems'))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
