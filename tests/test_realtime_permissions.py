import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest

spec=importlib.util.spec_from_file_location('permissions',Path(__file__).parents[1]/'scripts/realtime_permissions.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_owned_install_remove_and_preserve_higher(tmp_path):
    root=tmp_path/'root';root.mkdir(mode=0o755)
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='LimitRTPRIO=40\nLimitRTPRIOSoft=40\n')
    prefix=tmp_path/'prefix'
    def apply(action,p=prefix):module.apply(action,1001,p,root,run,os.getuid())
    apply('install')
    target=root/'etc/systemd/system/user@1001.service.d/90-hifimate-rtprio.conf'
    assert 'LimitRTPRIO=40:40' in target.read_text()
    before=target.read_bytes();apply('install');assert target.read_bytes()==before
    with pytest.raises(RuntimeError,match='another prefix'):apply('install',tmp_path/'other')
    target.write_bytes(before+b'# changed\n')
    with pytest.raises(RuntimeError,match='modified'):apply('remove')
    target.write_bytes(before);apply('remove');apply('remove')
    assert not target.exists()
    assert not any('restart' in c for c in calls)


def test_failure_and_external_file(tmp_path):
    root=tmp_path/'root';root.mkdir(mode=0o755)
    def run(args,**kwargs):
        if 'daemon-reload' in args:raise RuntimeError('failed reload')
        return SimpleNamespace(stdout='LimitRTPRIO=0\nLimitRTPRIOSoft=0\n')
    with pytest.raises(RuntimeError):module.apply('install',1001,tmp_path/'prefix',root,run,os.getuid())
    target=root/'etc/systemd/system/user@1001.service.d/90-hifimate-rtprio.conf'
    assert not target.exists() and not (root/'var/lib/hifimate-realtime/1001.json').exists()
    target.write_text('external')
    with pytest.raises(RuntimeError,match='unmanaged'):module.apply('install',1001,tmp_path/'prefix',root,run,os.getuid())


def test_installer_auth_and_dryrun(tmp_path):
    root=Path(__file__).parents[1];prefix=tmp_path/'prefix';fake=tmp_path/'bin';fake.mkdir();log=tmp_path/'sudo.log'
    (fake/'systemd-analyze').write_text('#!/bin/sh\nexit 0\n');(fake/'systemd-analyze').chmod(0o755)
    (fake/'sudo').write_text('#!/bin/sh\necho "$*" >> "$SUDO_LOG"\nexit 1\n');(fake/'sudo').chmod(0o755)
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],SUDO_LOG=str(log))
    def install(*args):return subprocess.run([str(root/'scripts/install.sh'),'--prefix',str(prefix),*args],env=env,capture_output=True,text=True)
    if os.getuid()==0:pytest.skip('installer deliberately refuses root')
    assert install('--setup-realtime').returncode==0
    assert not log.exists() and not prefix.exists()
    assert install('--setup-realtime','--realtime','off').returncode!=0
    result=install('--setup-realtime','--apply')
    assert result.returncode!=0
    # No passwordless sudo: fall back to sudo -v, which fails before any change.
    assert log.read_text().splitlines()==['-n true','-v']
    # Normal copy-only install never authenticates.
    log.unlink();assert install('--apply').returncode==0
    assert not log.exists()


def test_setup_and_uninstall_sudo_calls(tmp_path):
    if os.getuid()==0:pytest.skip('installer deliberately refuses root')
    root=Path(__file__).parents[1];prefix=tmp_path/'prefix';fake=tmp_path/'bin';fake.mkdir();log=tmp_path/'sudo.log'
    (fake/'systemd-analyze').write_text('#!/bin/sh\nexit 0\n');(fake/'systemd-analyze').chmod(0o755)
    (fake/'sudo').write_text('#!/bin/sh\necho "$*" >> "$SUDO_LOG"\nexit 0\n');(fake/'sudo').chmod(0o755)
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],SUDO_LOG=str(log))
    def invoke(script,*args):return subprocess.run([str(root/'scripts'/script),'--prefix',str(prefix),*args],env=env,capture_output=True,text=True)
    result=invoke('install.sh','--apply','--setup-realtime');assert result.returncode==0,result.stderr
    lines=log.read_text().splitlines();assert lines[0]=='-n true' and ' install ' in lines[1]
    assert (prefix/'config/realtime-permissions.request').exists()
    assert 'config/realtime-permissions.request' in (prefix/'.hifimate-install-manifest').read_text()
    log.unlink();result=invoke('uninstall.sh');assert result.returncode==0 and not log.exists()
    result=invoke('uninstall.sh','--apply');assert result.returncode==0,result.stderr
    assert log.read_text().splitlines()[0]=='-n true' and ' remove ' in log.read_text()


def test_nopasswd_rule_is_used_even_when_sudo_v_would_prompt(tmp_path):
    # sudoers with both "ALL" and "NOPASSWD: ALL": commands need no password,
    # but sudo -v still prompts (and fails without a terminal).
    if os.getuid()==0:pytest.skip('installer deliberately refuses root')
    root=Path(__file__).parents[1];prefix=tmp_path/'prefix';fake=tmp_path/'bin';fake.mkdir();log=tmp_path/'sudo.log'
    (fake/'systemd-analyze').write_text('#!/bin/sh\nexit 0\n');(fake/'systemd-analyze').chmod(0o755)
    (fake/'sudo').write_text('#!/bin/sh\necho "$*" >> "$SUDO_LOG"\n[ "$1" = -v ] && exit 1\nexit 0\n');(fake/'sudo').chmod(0o755)
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],SUDO_LOG=str(log))
    result=subprocess.run([str(root/'scripts/install.sh'),'--prefix',str(prefix),'--apply','--setup-realtime'],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    lines=log.read_text().splitlines()
    assert lines[0]=='-n true' and '-v' not in lines and ' install ' in lines[1]


def test_low_limit_and_symlink_protection(tmp_path):
    root=tmp_path/'root';root.mkdir(mode=0o755)
    run=lambda *a,**k:SimpleNamespace(stdout='LimitRTPRIO=0\nLimitRTPRIOSoft=0\n')
    module.apply('install',1001,tmp_path/'prefix',root,run,os.getuid())
    target=root/'etc/systemd/system/user@1001.service.d/90-hifimate-rtprio.conf'
    assert 'LimitRTPRIO=5:5' in target.read_text()
    module.apply('remove',1001,tmp_path/'prefix',root,run,os.getuid())
    assert not target.parent.exists()                 # the empty drop-in directory goes too
    target.parent.mkdir()
    outside=tmp_path/'outside';outside.write_text('untouched');target.symlink_to(outside)
    with pytest.raises(RuntimeError):module.apply('install',1001,tmp_path/'prefix',root,run,os.getuid())
    assert outside.read_text()=='untouched'


def test_infinite_limits_are_preserved(tmp_path):
    root=tmp_path/'root';root.mkdir(mode=0o755)
    run=lambda *a,**k:SimpleNamespace(stdout='LimitRTPRIO=infinity\nLimitRTPRIOSoft=infinity\n')
    module.apply('install',1001,tmp_path/'prefix',root,run,os.getuid())
    target=root/'etc/systemd/system/user@1001.service.d/90-hifimate-rtprio.conf'
    assert 'LimitRTPRIO=infinity:infinity' in target.read_text()


def test_interrupted_removal_can_be_retried(tmp_path, monkeypatch):
    root=tmp_path/'root';root.mkdir(mode=0o755)
    calls=[]
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='LimitRTPRIO=0\nLimitRTPRIOSoft=0\n')
    prefix=tmp_path/'prefix'
    module.apply('install',1001,prefix,root,run,os.getuid())
    state=root/'var/lib/hifimate-realtime/1001.json'
    target=root/'etc/systemd/system/user@1001.service.d/90-hifimate-rtprio.conf'
    original=Path.unlink
    def fail_record_unlink(path, *args, **kwargs):
        if path == state:
            raise OSError('interrupted state cleanup')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_record_unlink)
        with pytest.raises(OSError):
            module.apply('remove',1001,prefix,root,run,os.getuid())
    assert state.exists() and not target.exists()
    module.apply('remove',1001,prefix,root,run,os.getuid())
    assert not state.exists()
    assert calls[-1] == ['systemctl','daemon-reload']


def test_conflicting_user_manager_defaults(tmp_path):
    def check(text):
        module.check_user_defaults(lambda *a,**k: SimpleNamespace(stdout=text), tmp_path)
    check('[Manager]\n#DefaultLimitRTPRIO=0\n')
    check('[Manager]\nDefaultLimitRTPRIO=infinity\n')
    with pytest.raises(RuntimeError, match='conflicts'):
        check('[Manager]\nDefaultLimitRTPRIO = 0\n')
    (tmp_path/'systemd').mkdir()
    personal=tmp_path/'systemd/user.conf'
    personal.write_text('[Manager]\nDefaultLimitRTPRIO=40\n')
    check('# /etc/systemd/user.conf\n[Manager]\nDefaultLimitRTPRIO=0\n')
    with pytest.raises(RuntimeError, match='conflicts'):
        check('# /etc/systemd/user.conf.d/policy.conf\n[Manager]\nDefaultLimitRTPRIO=0\n')
