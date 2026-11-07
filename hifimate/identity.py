"""One validated receiver name; JSON is the persistent installation setting."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import unicodedata


def validate_name(value):
    if (not isinstance(value, str) or value != value.strip() or not value
            or len(value.encode('utf-8')) > 42
            or any(not (unicodedata.category(c)[0] in 'LMN' or c in ' -_.()') for c in value)):
        raise ValueError('设备名称须为 1–42 UTF-8 字节：中文、字母、数字、空格、-_.()；不允许控制字符或配置语法')
    return value


def load(prefix):
    path = Path(prefix) / 'config/identity.json'
    if path.exists():
        data = json.loads(path.read_text())
        validate_name(data['name'])
        if data.get('schema') != 1 or not Path(data['bluetooth_state']).is_absolute():
            raise ValueError('invalid identity configuration')
        return data
    state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    if not state.is_absolute():
        raise ValueError('XDG_STATE_HOME must be absolute')
    key = hashlib.sha256(str(Path(prefix).absolute()).encode()).hexdigest()[:16]
    return {'schema': 1, 'name': 'HiFiMate', 'bluetooth_state': str(state / 'hifimate' / f'bluetooth-{key}.json')}


def render(prefix, name, config_dir=None):
    name = validate_name(name)
    data = load(prefix)
    data['name'] = name
    replacements = {
        '@AIRPLAY_NAME@': json.dumps(name + ' AirPlay', ensure_ascii=False),
        '@DLNA_NAME@': name + ' DLNA',
    }
    for filename in ('shairport-sync.conf', 'upmpdcli.conf'):
        path = Path(config_dir or prefix / 'config/host-stack') / filename
        text = path.read_text()
        for key, value in replacements.items():
            text = text.replace(key, value)
        path.write_text(text)
    (prefix / 'config/identity.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('select', 'render'))
    parser.add_argument('prefix', type=Path)
    parser.add_argument('--name')
    parser.add_argument('--prompt', action='store_true')
    parser.add_argument('--config-dir', type=Path, help='render receiver names into this host-stack copy')
    args = parser.parse_args()
    name = args.name if args.name is not None else load(args.prefix)['name']
    if args.prompt and args.name is None and sys.stdin.isatty():
        print(f'统一设备名称 [{name}]: ', file=sys.stderr, end='', flush=True)
        name = input() or name
    name = validate_name(name)
    if args.action == 'render':
        render(args.prefix, name, args.config_dir)
    else:
        print(name)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(str(exc))
