"""The `hifimate` command: install, upgrade, status, uninstall and everyday tools.

Two ways to install:

- **System** (`sudo hifimate install`, what the one-line installer does): root
  installs the packages and creates a dedicated user `hifimate`, which runs
  everything (see system.py for the layout). Nothing asks for sudo afterwards
  except this command itself.
- **User** (`hifimate install` as a normal user): runs as that user, in
  ~/hifimate, for development or a machine where root is not available.

Either way the package carries the whole installable tree (hifimate/_dist
plus this package); commands put it back together in a staging directory and
run the same scripts/install.sh and scripts/uninstall.sh a git checkout uses.
Only the standard library is used: the receiver itself runs on the
distribution's python3 and packages (scripts/host_stack.py).

Options are remembered (never passwords or password files): a later
`hifimate install --name X` changes only the name, and `hifimate upgrade`
reinstalls the same way after upgrading the package.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from . import system

PACKAGE = Path(__file__).resolve().parent
UNITS = ('hifimate.service', 'hifimate-camilladsp.service', 'hifimate-spectrum.service', 'hifimate-ctrl.service',
         'hifimate-airplay.service', 'hifimate-airplay-net.service', 'hifimate-dlna.service',
         'hifimate-bluetooth.service', 'hifimate-mpd.service', 'hifimate-music-mpd.service')
# install.sh options with a value, and those that are flags. Only these are
# accepted and remembered; one-shot and secret options are never stored.
VALUED = {'--prefix', '--name', '--output-device', '--realtime', '--music-assistant-data-dir', '--music-directory',
          '--without-ma-provider', '--camilladsp-archive', '--camilladsp-sha256',
          '--ctrl-password-file', '--ma-admin-user', '--ma-admin-password-file'}
FLAGS = {'--setup-realtime', '--setup-bluetooth-speaker', '--without-ma-providers', '--choose-output',
         '--non-interactive', '--yes'}
NOT_REMEMBERED = {'--ctrl-password-file', '--ma-admin-user', '--ma-admin-password-file', '--choose-output',
                  '--non-interactive', '--yes', '--camilladsp-archive', '--camilladsp-sha256'}
REPEATABLE = {'--without-ma-provider'}
# What scripts/install.sh reads from a checkout (the same as the wheel's _dist plus the package).
CHECKOUT_TREE = ('hifimate', 'scripts', 'bin', 'config', 'systemd', 'gui', 'music-assistant', 'docs',
                 'README.md', 'INSTALL.md', 'LICENSE', 'NOTICE.md', 'pyproject.toml')
# The system install does these itself (as root), so install.sh never needs sudo.
SYSTEM_DROPS = {'--setup-realtime', '--setup-bluetooth-speaker'}


def is_system():
    """Root manages the system install; anyone else their own user install."""
    if os.geteuid() == 0:
        return True
    if system.RECORD.exists():
        raise SystemExit('hifimate: 这台主机按系统方式安装（专用用户 hifimate），请用 sudo hifimate …')
    return False


def config_path():
    if os.geteuid() == 0:
        return system.RECORD
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'hifimate' / 'install.json'


def load_saved():
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def save(prefix, options, music_assistant, install_deps, share=None):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'version': __version__, 'prefix': str(prefix), 'options': options,
            'music_assistant': music_assistant, 'install_deps': install_deps}
    if share is not None:
        data['share'] = share
    stage = path.with_name('.install.json.new')
    stage.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    os.replace(stage, path)


def default_prefix():
    if os.geteuid() == 0:
        return system.PREFIX
    return Path(os.environ.get('HIFIMATE_PREFIX') or Path.home() / 'hifimate')


class Tree:
    """The installable tree as scripts/install.sh expects it ($ROOT).

    For the system install it is staged in /var/tmp and handed to the service
    user (a git checkout in someone's home is not readable by it)."""

    def __init__(self, owner=None):
        self.owner, self.tmp = owner, None

    def __enter__(self):
        checkout = (PACKAGE.parent / 'scripts/install.sh').is_file() and not (PACKAGE / '_dist').is_dir()
        if checkout and not self.owner:
            return PACKAGE.parent                  # a git checkout: use it as it is
        if not checkout and not (PACKAGE / '_dist').is_dir():
            raise SystemExit('hifimate: 安装文件缺失（软件包不完整），请重新安装 hifimate')
        if self.owner:
            self.tmp = tempfile.TemporaryDirectory(prefix='hifimate-src-', dir='/var/tmp')
        else:
            self.tmp = tempfile.TemporaryDirectory(prefix='hifimate-')
        root = Path(self.tmp.name) / 'hifimate-src'
        if checkout:
            # Only what a release carries (pyproject's wheel force-include), never
            # caches, virtualenvs or test leftovers that happen to be in the checkout.
            skip = shutil.ignore_patterns('__pycache__', '_dist', '*.pyc')
            root.mkdir(parents=True)
            for name in CHECKOUT_TREE:
                source = PACKAGE.parent / name
                if source.is_dir():
                    shutil.copytree(source, root / name, ignore=skip)
                elif source.is_file():
                    shutil.copy2(source, root / name)
            commit = subprocess.run(['git', '-C', str(PACKAGE.parent), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True).stdout.strip()
            if commit:
                (root / 'hifimate/_commit.txt').write_text(commit + '\n')
        else:
            shutil.copytree(PACKAGE / '_dist', root, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(PACKAGE, root / 'hifimate', ignore=shutil.ignore_patterns('__pycache__', '_dist'))
        # Wheels do not always keep the executable bit.
        for pattern in ('bin/*', 'scripts/*.sh', 'scripts/*.py', 'music-assistant/*.sh', 'music-assistant/*.py'):
            for path in root.glob(pattern):
                path.chmod(0o755)
        if self.owner:
            subprocess.run(['chown', '-R', f'{self.owner}:{self.owner}', self.tmp.name], check=True)
        return root

    def __exit__(self, *exc):
        if self.tmp:
            self.tmp.cleanup()


def split_options(argv):
    """install.sh options from argv, checked; (options, prefix)."""
    options, prefix, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        name, eq, inline = arg.partition('=')
        if name in VALUED:
            if eq:
                value = inline
            elif i + 1 < len(argv):
                i += 1
                value = argv[i]
            else:
                raise SystemExit(f'hifimate: {name} 需要一个值')
            if name == '--prefix':
                prefix = Path(value).expanduser()
            else:
                options += [name, value]
        elif arg in FLAGS and not eq:
            options.append(arg)
        else:
            raise SystemExit(f'hifimate: 不认识的选项 {arg}（hifimate install --help）')
        i += 1
    return options, prefix


def pairs(options):
    out, i = [], 0
    while i < len(options):
        step = 2 if options[i] in VALUED else 1
        out.append(tuple(options[i:i + step]))
        i += step
    return out


def remembered(options):
    return [part for item in pairs(options) if item[0] not in NOT_REMEMBERED for part in item]


def merge(base, new):
    """base options updated by new: a new value replaces the old one, flags add up."""
    merged = pairs(base)
    for item in pairs(new):
        if len(item) == 2 and item[0] not in REPEATABLE:
            merged = [old for old in merged if old[0] != item[0]]
        if item not in merged:
            merged.append(item)
    return [part for item in merged for part in item]


def option(options, name, default=None):
    for item in pairs(options):
        if item[0] == name and len(item) == 2:
            return item[1]
    return default


def without(options, names):
    return [part for item in pairs(options) if item[0] not in names for part in item]


def check_docker():
    if not shutil.which('docker'):
        raise SystemExit('hifimate: Music Assistant 需要 Docker Engine 和 Compose v2：'
                         'https://docs.docker.com/engine/install/ （或加 --without-music-assistant）')
    probe = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True)
    if probe.returncode:
        raise SystemExit('hifimate: 不能访问 Docker（安装并启动 Docker，用户安装时把用户加入 docker 组并重新登录，'
                         '或加 --without-music-assistant）：' + (probe.stderr.strip().splitlines() or ['?'])[-1])


def read_secret(env_name, path=None):
    """A password from the environment or a file (never argv), checked like install_secrets.py."""
    value = os.environ.get(env_name)
    path = path or os.environ.get(env_name + '_FILE')
    if path:
        value = Path(path).read_text(encoding='utf-8')
        value = value[:-1] if value.endswith('\n') else value
        value = value[:-1] if value.endswith('\r') else value
    if value is not None and (not 8 <= len(value) <= 1024 or '\n' in value or '\r' in value):
        raise SystemExit(f'hifimate: {env_name}：密码须为 8–1024 个字符，不能有换行')
    return value


def ask_password(prompt='控制页密码（至少 8 位，同时用作音乐共享的密码）：'):
    while True:
        first = getpass.getpass(prompt)
        if len(first) < 8:
            print('密码至少 8 位', file=sys.stderr); continue
        if first != getpass.getpass('再次输入：'):
            print('两次密码不一致', file=sys.stderr); continue
        return first


def interactive():
    return sys.stdin.isatty()


# --- install ------------------------------------------------------------------------

def resolve(args, rest, default=None):
    """Options for this run: the remembered ones updated by the given ones."""
    saved = {} if args.reset_options else load_saved()
    if args.saved and not load_saved():
        raise SystemExit('hifimate: 还没有安装记录，请先运行 hifimate install')
    options, prefix = split_options(rest)
    if saved:
        options = merge(saved.get('options', []), options)
        for name in ('music_assistant', 'install_deps', 'share'):
            if getattr(args, name, None) is None:
                setattr(args, name, saved.get(name, True))
    for name in ('music_assistant', 'install_deps', 'share'):
        if getattr(args, name, None) is None:
            setattr(args, name, True)
    prefix = (prefix or (Path(saved['prefix']) if saved.get('prefix') else default or default_prefix())).absolute()
    if not interactive() and '--non-interactive' not in options and '--yes' not in options:
        options.append('--non-interactive')          # nobody to ask (pipes, CI, cron)
    return options, prefix


def install(args, rest, run=subprocess.call):
    """Install or reinstall. Remembered options carry over (so changing one keeps
    the others) unless --reset-options is given."""
    if is_system():
        return system_install(args, rest, run)
    options, prefix = resolve(args, rest)
    if option(options, '--ctrl-password-file') or os.environ.get('HIFIMATE_CTRL_PASSWORD'):
        raise SystemExit('hifimate: 用户安装时，控制页密码就是你自己的登录密码，不需要另外给出')
    command = ['--prefix', str(prefix), *options]
    if not args.dry_run:
        command += ['--apply', '--enable']
    if args.music_assistant:
        command.append('--with-music-assistant')
        if not args.dry_run:
            check_docker()
    if args.install_deps:
        command.append('--install-deps')
    with Tree() as root:
        status = run(['sh', str(root / 'scripts/install.sh'), *command], env=dry_run_hint('安装'))
    if status == 0 and not args.dry_run:
        save(prefix, remembered(options), args.music_assistant, args.install_deps)
        print(f'HiFiMate {__version__} 已安装到 {prefix}；hifimate status 查看状态')
    return status


def dry_run_hint(action):
    """The scripts' environment: their dry-run hint names this command's way to apply
    (leave out --dry-run), not their own --apply."""
    return {**os.environ, 'HIFIMATE_DRY_RUN_HINT': f'只是演练，什么都没有改动；去掉 --dry-run 即可{action}'}


def system_install(args, rest, run=subprocess.call, sysrun=subprocess.run, sys_=system):
    options, prefix = resolve(args, rest, sys_.PREFIX)
    if prefix != sys_.PREFIX:
        raise SystemExit(f'hifimate: 系统安装的目录固定为 {sys_.PREFIX}')
    music = Path(option(options, '--music-directory', str(sys_.MUSIC)))
    forwarded = without(options, SYSTEM_DROPS | {'--ctrl-password-file', '--ma-admin-password-file',
                                                 '--music-directory', '--music-assistant-data-dir'})
    forwarded += ['--music-directory', str(music),
                  '--music-assistant-data-dir', option(options, '--music-assistant-data-dir', str(sys_.MA_DATA))]
    plan = [f'安装系统软件包{"（含 Samba）" if args.share else ""}' if args.install_deps else None,
            f'创建用户 {sys_.USER}（主目录 {sys_.HOME}），加入 audio、bluetooth{"、docker" if args.music_assistant else ""} 组',
            f'程序和设置装到 {sys_.PREFIX}，音乐库 {music}',
            '设置实时调度上限和蓝牙音箱类别',
            f'以 {sys_.USER} 用户启动服务' + ('，部署 Music Assistant' if args.music_assistant else ''),
            f'把音乐库共享为 \\\\<主机>\\{sys_.SHARE}（用户 {sys_.USER}，密码同控制页）' if args.share else None]
    if args.dry_run:
        print('将要：\n' + '\n'.join(f'  {i}. {step}' for i, step in enumerate(filter(None, plan), 1)))
        return 0
    # Secrets are read here, as root (a password file may not be readable by the
    # service user), and handed to install.sh through its environment.
    env = {k: v for k, v in os.environ.items() if not k.endswith('_PASSWORD_FILE')}
    # The control password is hifimate's Linux password (checked by PAM): asked
    # once, kept across reinstalls; given again, it replaces the old one.
    password = read_secret('HIFIMATE_CTRL_PASSWORD', option(options, '--ctrl-password-file'))
    ma_password = read_secret('HIFIMATE_MA_ADMIN_PASSWORD', option(options, '--ma-admin-password-file'))
    env.pop('HIFIMATE_CTRL_PASSWORD', None)            # install.sh has no use for it
    unattended = not interactive() or '--non-interactive' in options
    if password is None and not sys_.has_password():
        if not unattended:
            password = ask_password()
        elif not load_saved():
            raise SystemExit('hifimate: 首次安装需要控制页密码：HIFIMATE_CTRL_PASSWORD 或 --ctrl-password-file')
        else:   # a reinstall: the services work, only logging in waits for a password
            print('warning: hifimate 用户还没有密码，控制页和音乐共享暂时登录不了；用 sudo hifimate password 设置', file=sys.stderr)
    if ma_password is not None:
        env['HIFIMATE_MA_ADMIN_PASSWORD'] = ma_password
    with Tree() as root:
        if args.install_deps:
            command = ['python3', '-B', str(root / 'scripts/host_stack.py'), '--install-deps', '--apply', '--yes',
                       '--manifest', str(root / 'config/host-stack-manifest.json')]
            if args.share:
                command.append('--with-share')
            if run(command):
                return 1
    if args.music_assistant:
        check_docker()
    user_id, changed = sys_.ensure_user(args.music_assistant, sysrun)
    sys_.ensure_directories(music, sysrun)
    sys_.write_pam(run=sysrun)
    with Tree(owner=sys_.USER) as root:
        rtprio = Path(f'/etc/systemd/system/user@{user_id}.service.d/90-hifimate-rtprio.conf')
        new_limits = not rtprio.exists()
        if option(options, '--realtime') != 'off':
            result = sys_.root_helper(root, 'realtime_permissions.py', 'install', user_id, sysrun)
            if result.returncode:
                print(f'warning: 没有设置实时调度上限：{(result.stderr or result.stdout).strip()}', file=sys.stderr)
                new_limits = False
        else:
            new_limits = False
        if sys_.BLUEZ_CONF.exists():
            result = sys_.root_helper(root, 'bluetooth_speaker.py', 'install', user_id, sysrun)
            if result.returncode:
                print(f'warning: 没有设置蓝牙音箱类别：{(result.stderr or result.stdout).strip()}', file=sys.stderr)
        # A new group or new limits reach the services only through a new manager.
        sys_.start_manager(user_id, restart=changed or new_limits, run=sysrun)
        command = ['sh', str(root / 'scripts/install.sh'), '--prefix', str(sys_.PREFIX), *forwarded,
                   '--apply', '--enable']
        if args.music_assistant:
            command.append('--with-music-assistant')
        status = run(sys_.as_user(command, user_id), env=env)
    if status:
        return status
    if args.share:
        try:
            sys_.setup_share(music, None, sysrun)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
            print(f'warning: 没有设置音乐共享：{error}', file=sys.stderr)
    if password is not None:
        sys_.set_password(password, sysrun)            # Linux (control page) and the share
    save(sys_.PREFIX, remembered(without(options, SYSTEM_DROPS)), args.music_assistant, args.install_deps, args.share)
    print(f'HiFiMate {__version__} 已安装。sudo hifimate status 查看状态；文件位置见 README「安装后的文件」。')
    return 0


def pipx_env():
    """pipx's own locations for this install (the system one lives in /opt/pipx)."""
    venv = Path(sys.prefix)
    env = dict(os.environ)
    if 'venvs' in venv.parts:
        env['PIPX_HOME'] = str(venv.parents[1])
        command = shutil.which('hifimate')
        if command:
            env['PIPX_BIN_DIR'] = str(Path(command).parent)
    return env


def pipx_managed():
    """True when this command runs from `pipx install hifimate` (not `pipx run`)."""
    parts = Path(sys.prefix).parts
    return 'venvs' in parts and parts[-1] == 'hifimate' and shutil.which('pipx') is not None


def upgrade(args, rest, run=subprocess.call, execv=os.execv):
    is_system()
    if not load_saved():
        raise SystemExit('hifimate: 还没有安装记录，请先运行 hifimate install')
    if not args.no_self_upgrade:
        if pipx_managed():
            spec = f'hifimate=={args.version}' if args.version else None
            command = ['pipx', 'install', '--force', spec] if spec else ['pipx', 'upgrade', 'hifimate']
            status = run(command, env=pipx_env()) if os.geteuid() == 0 else run(command)
            if status:
                return status
            # The new package installs; this process is still the old one.
            command = shutil.which('hifimate') or sys.argv[0]
            return execv(command, [command, 'install', '--saved', *rest])   # does not return
        elif args.version:
            raise SystemExit('hifimate: 不是用 pipx install 安装的，无法切换版本；'
                             f'请运行 pipx install --force hifimate=={args.version}')
    ns = argparse.Namespace(saved=True, reset_options=False, dry_run=False, music_assistant=None,
                            install_deps=None, share=None)
    return install(ns, rest, run)


# --- uninstall ----------------------------------------------------------------------

def uninstall(args, rest, run=subprocess.call, sysrun=subprocess.run, sys_=system):
    if is_system():
        user_id = sys_.uid()
        if user_id is None:
            raise SystemExit('hifimate: 没有找到用户 hifimate，系统安装不存在')
        with Tree(owner=sys_.USER) as root:
            command = ['sh', str(root / 'scripts/uninstall.sh'), '--prefix', str(sys_.PREFIX)]
            status = run(sys_.as_user(command + ([] if args.dry_run else ['--apply']), user_id), env=dry_run_hint('卸载'))
            if status or args.dry_run:
                return status
            for script in ('realtime_permissions.py', 'bluetooth_speaker.py'):
                result = sys_.root_helper(root, script, 'remove', user_id, sysrun)
                if result.returncode:
                    print(f'warning: {script} remove：{(result.stderr or result.stdout).strip()}', file=sys.stderr)
        sys_.remove_share(sysrun)
        if args.purge:
            sys_.purge(sysrun)
            print(f'已卸载并删除用户 {sys_.USER}、{sys_.PREFIX} 和 {sys_.HOME}。音乐库保留在 {sys_.MUSIC}。')
        else:
            sysrun(['loginctl', 'disable-linger', sys_.USER], capture_output=True)
            sysrun(['systemctl', 'stop', f'user@{user_id}.service'], capture_output=True)
            sys_.RECORD.unlink(missing_ok=True)
            print(f'已卸载。保留了用户 {sys_.USER}、你的数据（{sys_.PREFIX}、{sys_.HOME}）和音乐库；'
                  '全部删除用 sudo hifimate uninstall --purge。')
        print('命令本身用 sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx uninstall hifimate 删除。')
        return 0
    saved = load_saved()
    prefix = Path(args.prefix).expanduser().absolute() if args.prefix else Path(saved.get('prefix') or default_prefix())
    with Tree() as root:
        command = ['sh', str(root / 'scripts/uninstall.sh'), '--prefix', str(prefix)]
        status = run(command + ([] if args.dry_run else ['--apply']), env=dry_run_hint('卸载'))
    if status == 0 and not args.dry_run:
        config_path().unlink(missing_ok=True)
        print(f'已卸载 {prefix}。命令本身用 pipx uninstall hifimate 删除。')
    return status


# --- status and tools ---------------------------------------------------------------

def target():
    """(prefix, wrap, uid): where HiFiMate is, how to run something as its user,
    and the service user's uid (None for a user install)."""
    if is_system():
        user_id = system.uid()
        if user_id is None:
            raise SystemExit('hifimate: 还没有安装（sudo hifimate install）')
        return system.PREFIX, (lambda argv: system.as_user(argv, user_id)), user_id
    saved = load_saved()
    return Path(saved.get('prefix') or default_prefix()), (lambda argv: argv), None


def status(args, rest, run=subprocess.run):
    prefix, wrap, user_id = target()
    if args.prefix:
        prefix = Path(args.prefix).expanduser().absolute()
    saved = load_saved()
    read = lambda name: (prefix / name).read_text().strip() if (prefix / name).is_file() else None
    installed = read('share/hifimate/version')
    print(f'hifimate 命令   {__version__}')
    if not installed and not (prefix / '.hifimate-install-manifest').is_file():
        print(f'安装目录        {prefix}（没有安装）')
        return 1
    print(f'安装目录        {prefix}' + (f'（用户 {system.USER}）' if user_id is not None else ''))
    print(f'已安装版本      {installed or "?"}（commit {(read("share/hifimate/commit") or "?")[:12]}）'
          + ('' if installed in (None, __version__) else '  ← 与命令版本不同，运行 hifimate upgrade'))
    try:
        conf = json.loads(read('config/hifimate.conf') or '{}')
    except ValueError:
        conf = {}
    print(f'输出设备        {conf.get("alsa_device", "?")}')
    music = option(saved.get('options', []), '--music-directory', str(system.MUSIC) if user_id is not None else None)
    if music:
        print(f'音乐库          {music}' + (f'（共享为 \\\\<主机>\\{system.SHARE}）' if saved.get('share') else ''))
    bad = 0
    states = run(wrap(['systemctl', '--user', 'is-active', *UNITS]), capture_output=True, text=True).stdout.split()
    for unit, state in zip(UNITS, states + ['?'] * len(UNITS)):
        bad += state != 'active'
        print(f'  {"✓" if state == "active" else "✗"} {unit:34} {state}')
    if saved.get('music_assistant', (prefix / 'music-assistant/.env').is_file()):
        ps = run(['docker', 'ps', '-a', '--filter', 'name=^hifimate-', '--format', '{{.Names}}\t{{.Status}}'],
                 capture_output=True, text=True) if shutil.which('docker') else None
        rows = [line.split('\t', 1) for line in (ps.stdout.splitlines() if ps and ps.returncode == 0 else [])]
        if not rows:
            print('  ✗ Music Assistant 容器           无法读取（docker 不可用或未运行）'); bad += 1
        for name, state in sorted(rows):
            ok = state.startswith('Up') and 'unhealthy' not in state
            bad += not ok
            print(f'  {"✓" if ok else "✗"} {name:34} {state}')
    # /ctrl is published on the LAN by Music Assistant's nginx; without it, only locally.
    print('控制页          http://<主机IP>/ctrl/' if saved.get('music_assistant', True)
          else '控制页          http://127.0.0.1:5009/ctrl/（仅本机）')
    return 1 if bad else 0


def tool(args, rest, run=subprocess.call):
    """Everyday tools, run as the user HiFiMate runs as."""
    prefix, wrap, user_id = target()
    bin_ = prefix / 'bin'
    if args.command == 'pair':
        return run(wrap([str(bin_ / 'hifimate-bluetooth'), 'pair', '--seconds', str(args.seconds)]))
    if args.command == 'reload-filters':
        return run(wrap([str(bin_ / 'hifimate-ctrl'), 'reload-filters']))
    if args.command == 'output':
        if args.device:
            return run(wrap([str(bin_ / 'hifimate-output'), '--device', args.device, '--apply']))
        return run(wrap([str(bin_ / 'hifimate-output'), '--choose', '--apply'] if args.choose
                        else [str(bin_ / 'hifimate-output'), '--list']))
    if args.command == 'password':
        if user_id is None:
            raise SystemExit('hifimate: 用户安装时，控制页密码就是你自己的登录密码，用 passwd 修改')
        password = read_secret('HIFIMATE_CTRL_PASSWORD') or ask_password('新的控制页和音乐共享密码（至少 8 位）：')
        try:
            share = system.set_password(password)
        except subprocess.CalledProcessError as error:
            raise SystemExit(f'hifimate: 没有改成：{(error.stderr or "").strip() or error}')
        # Sessions live in the control service's memory: a restart logs everyone out.
        run(wrap(['systemctl', '--user', 'restart', 'hifimate-ctrl.service']))
        print('控制页密码已修改' + ('，音乐共享的密码也已修改' if share else '') + '；已登录的浏览器需要重新登录。')
        return 0
    if args.command == 'logs':
        units = [u if u.endswith('.service') else (f'hifimate-{u}.service' if u != 'hifimate' else 'hifimate.service')
                 for u in args.service] or list(UNITS)
        follow = ['-f'] if args.follow else []
        if user_id is not None:
            matches = [f'_SYSTEMD_USER_UNIT={u}' for u in units]
            return run(['journalctl', f'_UID={user_id}', *matches, '-n', str(args.lines), '--no-pager', *follow])
        return run(['journalctl', '--user', *[f'--unit={u}' for u in units], '-n', str(args.lines), '--no-pager',
                    *follow])
    if args.command == 'restart':
        return run(wrap(['systemctl', '--user', 'restart', *UNITS]))
    raise SystemExit(2)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='hifimate', description='HiFiMate 音频接收机的安装、管理和常用工具。'
                                     '系统安装（推荐）的所有命令都用 sudo 运行。')
    parser.add_argument('--version', action='version', version=f'hifimate {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('install', help='安装或重新安装（应用并启用服务）',
                       description='其余选项交给 scripts/install.sh：--name、--output-device、--choose-output、'
                                   '--non-interactive、--ctrl-password-file、--ma-admin-user、--ma-admin-password-file 等'
                                   '（见 INSTALL.md）。')
    p.add_argument('--dry-run', action='store_true', help='只显示会做什么')
    p.add_argument('--without-music-assistant', dest='music_assistant', action='store_false', default=None)
    p.add_argument('--with-music-assistant', dest='music_assistant', action='store_true',
                   help='重新部署 Music Assistant（之前用 --without-music-assistant 安装时）')
    p.add_argument('--without-share', dest='share', action='store_false', default=None,
                   help='不把音乐库共享到局域网（系统安装）')
    p.add_argument('--with-share', dest='share', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--no-install-deps', dest='install_deps', action='store_false', default=None,
                   help='不调用包管理器安装发行版软件包')
    p.add_argument('--reset-options', action='store_true', help='不沿用上次记住的选项')
    p.add_argument('--saved', action='store_true', help=argparse.SUPPRESS)   # older `hifimate upgrade`
    p.set_defaults(run=install)
    p = sub.add_parser('upgrade', help='升级 hifimate 并按记住的选项重新安装')
    p.add_argument('--version', help='升级到指定版本（pipx install --force hifimate==VERSION）')
    p.add_argument('--no-self-upgrade', action='store_true', help='不升级软件包，只重新安装')
    p.set_defaults(run=upgrade)
    p = sub.add_parser('status', help='显示安装和服务状态')
    p.add_argument('--prefix', help=argparse.SUPPRESS)
    p.set_defaults(run=status)
    p = sub.add_parser('uninstall', help='卸载（保留音乐库和你的数据）')
    p.add_argument('--prefix', help=argparse.SUPPRESS)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--purge', action='store_true', help='系统安装：同时删除用户 hifimate 和全部数据（音乐库保留）')
    p.set_defaults(run=uninstall)
    p = sub.add_parser('pair', help='打开蓝牙配对窗口')
    p.add_argument('--seconds', type=int, default=120)
    p.set_defaults(run=tool)
    sub.add_parser('reload-filters', help='检查并加载修改后的 EQ（dsp-filters.yml）').set_defaults(run=tool)
    p = sub.add_parser('output', help='列出或切换输出设备')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--choose', action='store_true', help='选择并切换')
    group.add_argument('--device', help='切换到指定设备，例如 hw:CARD=DAC,DEV=0')
    p.set_defaults(run=tool)
    sub.add_parser('password', help='修改控制页和音乐共享的密码（hifimate 用户的 Linux 密码）').set_defaults(run=tool)
    p = sub.add_parser('logs', help='查看服务日志')
    p.add_argument('service', nargs='*', help='例如 airplay、dlna、bluetooth、camilladsp；不写则全部')
    p.add_argument('-n', '--lines', type=int, default=100)
    p.add_argument('-f', '--follow', action='store_true')
    p.set_defaults(run=tool)
    sub.add_parser('restart', help='重启 HiFiMate 的所有服务').set_defaults(run=tool)
    args, rest = parser.parse_known_args(argv)
    if rest and args.command not in ('install', 'upgrade'):
        parser.error('不认识的参数：' + ' '.join(rest))
    return args.run(args, rest)


if __name__ == '__main__':
    sys.exit(main())
