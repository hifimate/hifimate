from conftest import record_file
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from hifimate.output_devices import enumerate_outputs, device_capabilities, choose, switch, recover, FILES, JOURNAL


def card(proc, number, name, rates='44100, 768000'):
    p=proc/f'card{number}'; p.mkdir(parents=True)
    (p/'id').write_text(name)
    (p/'pcm0p').mkdir()
    (p/'pcm0p/info').write_text('device: 0\nstream: PLAYBACK\nid: USB Audio\n')
    (p/'stream0').write_text(f'USB {name}\nPlayback:\n Interface 1\n Format: S32_LE\n Channels: 2\n Rates: {rates}\n')


def installed(tmp_path, monkeypatch):
    prefix=tmp_path/'installation'; (prefix/'config').mkdir(parents=True)
    proc=tmp_path/'proc'; card(proc,0,'First'); card(proc,1,'Second')
    conf=json.dumps({'alsa_device':'hw:CARD=First,DEV=0','proc_asound':str(proc),'other':42}).encode()
    (prefix/'config/hifimate.conf').write_bytes(conf)
    manifest=('# hifimate-install-v1\n'+hashlib.sha256(conf).hexdigest()+' config/hifimate.conf\n').encode()
    (prefix/'.hifimate-install-manifest').write_bytes(manifest)
    (prefix/'.hifimate-install-manifest.sha256').write_text(hashlib.sha256(manifest).hexdigest()+'  .hifimate-install-manifest\n')
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'xdg'))
    return prefix


def test_discovery_and_invalid(tmp_path):
    card(tmp_path,0,'DAC'); assert device_capabilities('auto',tmp_path)[2]=={44100,768000}
    card(tmp_path,1,'DAC'); assert all(r['error'] for r in enumerate_outputs(tmp_path))
    with pytest.raises(RuntimeError): device_capabilities('hw:CARD=DAC,DEV=0',tmp_path)
    card(tmp_path,2,'Bad','44100 - 768000'); assert enumerate_outputs(tmp_path)[2]['error']


def test_choose(tmp_path,monkeypatch):
    card(tmp_path,0,'One'); card(tmp_path,1,'Two')
    monkeypatch.setattr('sys.stdin.isatty',lambda:False)
    with pytest.raises(RuntimeError): choose(tmp_path)
    monkeypatch.setattr('sys.stdin.isatty',lambda:True)
    monkeypatch.setattr('builtins.input',lambda: '2')
    assert choose(tmp_path,interactive=True)=='hw:CARD=Two,DEV=0'
    monkeypatch.setattr('builtins.input',lambda: '0')
    with pytest.raises(RuntimeError): choose(tmp_path,interactive=True)


def test_offline_repeat_and_integrity(tmp_path,monkeypatch):
    p=installed(tmp_path,monkeypatch)
    def forbidden(*a,**kw): return SimpleNamespace(returncode=1, stderr='Failed to connect to bus', stdout='')
    for name in ('Second','First','Second'):
        switch(p,f'hw:CARD={name},DEV=0',True,forbidden)
        data=(p/'config/hifimate.conf').read_bytes()
        assert json.loads(data)['other']==42
        assert hashlib.sha256(data).hexdigest() in (p/'.hifimate-install-manifest').read_text()
    original=(p/'config/hifimate.conf').read_bytes()
    with pytest.raises(RuntimeError): switch(p,'hw:CARD=Missing,DEV=0',True,forbidden)
    assert (p/'config/hifimate.conf').read_bytes()==original


def test_active_rollback_and_foreign(tmp_path,monkeypatch):
    p=installed(tmp_path,monkeypatch)
    unit=tmp_path/'xdg/systemd/user/hifimate.service';unit.parent.mkdir(parents=True)
    (p/'systemd').mkdir();(p/'systemd/hifimate.service').touch();unit.symlink_to(p/'systemd/hifimate.service')
    calls=[]
    def run(args,**kw):
        calls.append(args)
        if 'restart' in args and sum('restart' in x for x in calls)==1: raise RuntimeError('restart failed')
        return SimpleNamespace(returncode=0, stdout=f'FragmentPath={p}/systemd/hifimate.service\nExecStart={{ path={p}/bin/hifimate-runner ; }}\nActiveState=active\nLoadState=loaded', stderr='')
    paths=[p/'config/hifimate.conf',p/'.hifimate-install-manifest',p/'.hifimate-install-manifest.sha256']
    before=[x.read_bytes() for x in paths]
    with pytest.raises(RuntimeError): switch(p,'hw:CARD=Second,DEV=0',True,run)
    assert before==[x.read_bytes() for x in paths]
    assert sum('restart' in x for x in calls)==2
    switch(p,'hw:CARD=Second,DEV=0',True,run)
    unit.unlink();unit.symlink_to(tmp_path/'other.service')
    with pytest.raises(RuntimeError,match='another installation'): switch(p,'hw:CARD=First,DEV=0',True,run)


def test_installer_selection_and_uninstall(tmp_path, monkeypatch):
    import subprocess
    root=Path(__file__).parents[1]
    prefix=tmp_path/'hifimate'
    install=root/'scripts/install.sh'
    def invoke(*args):
        return subprocess.run([str(install),'--prefix',str(prefix),*args],capture_output=True,text=True)
    assert invoke('--apply').returncode==0
    proc=tmp_path/'proc';card(proc,0,'First');card(proc,1,'Second')
    conf=prefix/'config/hifimate.conf';data=json.loads(conf.read_text());data['proc_asound']=str(proc);conf.write_text(json.dumps(data))
    record_file(prefix, 'config/hifimate.conf')
    before=conf.read_bytes()
    result=invoke('--apply','--enable')
    assert result.returncode!=0 and 'require --output-device' in result.stderr
    assert conf.read_bytes()==before
    assert invoke('--apply','--output-device','hw:CARD=Missing,DEV=0').returncode!=0
    assert conf.read_bytes()==before
    result=invoke('--apply','--output-device','hw:CARD=Second,DEV=0')
    assert result.returncode==0,result.stderr
    assert json.loads(conf.read_text())['alsa_device']=='hw:CARD=Second,DEV=0'
    assert invoke('--apply').returncode==0
    assert json.loads(conf.read_text())['alsa_device']=='hw:CARD=Second,DEV=0'
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'xdg'))
    for name in ('First','Second'):
        switch(prefix,f'hw:CARD={name},DEV=0',True,lambda *a,**k: SimpleNamespace(returncode=1,stderr='Failed to connect to bus',stdout=''))
    result=subprocess.run([str(root/'scripts/uninstall.sh'),'--prefix',str(prefix),'--apply'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert not conf.exists()
    assert not (prefix/'bin/hifimate-output').exists()


def test_inactive_service_not_restarted(tmp_path,monkeypatch):
    p=installed(tmp_path,monkeypatch)
    unit=tmp_path/'xdg/systemd/user/hifimate.service';unit.parent.mkdir(parents=True)
    (p/'systemd').mkdir();(p/'systemd/hifimate.service').touch();unit.symlink_to(p/'systemd/hifimate.service')
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout=f'FragmentPath={p}/systemd/hifimate.service\nActiveState=inactive\nLoadState=loaded',stderr='')
    switch(p,'hw:CARD=Second,DEV=0',True,run)
    assert len(calls)==1 and 'show' in calls[0]


def test_runtime_rate_compatibility(tmp_path,monkeypatch):
    card(tmp_path/'proc',0,'Low','8000, 32000')
    assert enumerate_outputs(tmp_path/'proc')[0]['error']
    card(tmp_path/'proc',1,'High','44100, 1536000')
    assert device_capabilities('auto',tmp_path/'proc')[2]=={44100,1536000}
    p=installed(tmp_path/'install',monkeypatch)
    # Updating managed config in fixture while preserving manifest integrity.
    config=json.loads((p/FILES[0]).read_text());config['rates']=[48000]
    raw=json.dumps(config).encode();(p/FILES[0]).write_bytes(raw)
    manifest=('# hifimate-install-v1\n'+hashlib.sha256(raw).hexdigest()+' config/hifimate.conf\n').encode()
    (p/FILES[1]).write_bytes(manifest)
    (p/FILES[2]).write_text(hashlib.sha256(manifest).hexdigest()+'  .hifimate-install-manifest\n')
    before=[(p/n).read_bytes() for n in FILES]
    with pytest.raises(RuntimeError,match='intersection'): switch(p,'hw:CARD=Second,DEV=0',True)
    assert before==[(p/n).read_bytes() for n in FILES]


def test_noop_and_cli_constraints(tmp_path,monkeypatch):
    from hifimate.output_devices import main
    p=installed(tmp_path,monkeypatch)
    before=[(p/n).stat().st_mtime_ns for n in FILES]
    switch(p,'hw:CARD=First,DEV=0',True,lambda *a,**k: pytest.fail('no service call'))
    assert before==[(p/n).stat().st_mtime_ns for n in FILES]
    for flags in (['--apply'],['--list','--device','x'],['--choose','--device','x']):
        monkeypatch.setattr('sys.argv',['hifimate-output',*flags])
        with pytest.raises(SystemExit) as result: main()
        assert result.value.code==2


def test_actual_foreign_and_unavailable_bus(tmp_path,monkeypatch):
    p=installed(tmp_path,monkeypatch)
    before=(p/FILES[0]).read_bytes()
    foreign=lambda *a,**k: SimpleNamespace(returncode=0,stdout='FragmentPath=/other/hifimate.service\nActiveState=active\nLoadState=loaded',stderr='')
    with pytest.raises(RuntimeError,match='another installation'): switch(p,'hw:CARD=Second,DEV=0',True,foreign)
    assert (p/FILES[0]).read_bytes()==before
    bad=lambda *a,**k: SimpleNamespace(returncode=1,stdout='',stderr='Access denied')
    with pytest.raises(RuntimeError,match='cannot determine'): switch(p,'hw:CARD=Second,DEV=0',True,bad)
    # No bus is offline only when controller lock is not held.
    runtime=tmp_path/'runtime';(runtime/'hifimate').mkdir(parents=True)
    monkeypatch.setenv('XDG_RUNTIME_DIR',str(runtime))
    import fcntl
    with (runtime/'hifimate/controller.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        offline=lambda *a,**k: SimpleNamespace(returncode=1,stdout='',stderr='Failed to connect to bus')
        with pytest.raises(RuntimeError,match='lock held'): switch(p,'hw:CARD=Second,DEV=0',True,offline)


def test_crash_recovery(tmp_path,monkeypatch):
    import hifimate.output_devices as module
    p=installed(tmp_path,monkeypatch)
    before=[(p/n).read_bytes() for n in FILES]
    real=module.atomic
    def crash(path,data,**kwargs):
        real(path,data,**kwargs)
        if path==p/FILES[0]: raise KeyboardInterrupt()
    offline=lambda *a,**k: SimpleNamespace(returncode=1,stderr='Failed to connect to bus',stdout='')
    monkeypatch.setattr(module,'atomic',crash)
    with pytest.raises(KeyboardInterrupt): switch(p,'hw:CARD=Second,DEV=0',True,offline)
    assert (p/JOURNAL).exists() and (p/FILES[0]).read_bytes()!=before[0]
    monkeypatch.setattr(module,'atomic',real)
    recover(p,offline)
    assert before==[(p/n).read_bytes() for n in FILES]
    assert not (p/JOURNAL).exists()


def test_active_crash_recovery_and_pending_uninstall(tmp_path,monkeypatch):
    import subprocess
    import hifimate.output_devices as module
    p=installed(tmp_path,monkeypatch)
    before=[(p/n).read_bytes() for n in FILES]
    real=module.atomic
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout=f'FragmentPath={p}/systemd/hifimate.service\nExecStart={{ path={p}/bin/hifimate-runner ; }}\nActiveState=active\nLoadState=loaded',stderr='')
    def crash(path,data,**kwargs):
        real(path,data,**kwargs)
        if path==p/FILES[0]: raise KeyboardInterrupt()
    monkeypatch.setattr(module,'atomic',crash)
    with pytest.raises(KeyboardInterrupt): switch(p,'hw:CARD=Second,DEV=0',True,run)
    result=subprocess.run([str(Path(__file__).parents[1]/'scripts/uninstall.sh'),'--prefix',str(p),'--apply'],capture_output=True,text=True)
    assert result.returncode!=0 and '--recover --apply' in result.stderr
    monkeypatch.setattr(module,'atomic',real)
    foreign=lambda *a,**k: SimpleNamespace(returncode=0,stdout='FragmentPath=/other/service\nActiveState=inactive\nLoadState=loaded',stderr='')
    with pytest.raises(RuntimeError): recover(p,foreign)
    assert (p/JOURNAL).exists()
    recover(p,run)
    assert before==[(p/n).read_bytes() for n in FILES]
    assert sum('restart' in c for c in calls)==1


def test_installer_conflicting_options(tmp_path):
    import subprocess
    prefix=tmp_path/'no-files'
    result=subprocess.run([str(Path(__file__).parents[1]/'scripts/install.sh'),'--prefix',str(prefix),'--list-outputs','--choose-output'],capture_output=True,text=True)
    assert result.returncode==2 and 'conflicting output options' in result.stderr
    assert not prefix.exists()


def test_usb_pcm_mapping_and_altsets(tmp_path):
    p=tmp_path/'card2';p.mkdir();(p/'id').write_text('External')
    (p/'stream8').write_text('Actual model\nPlayback:\n Interface 1\n Altset 1\n Format: S32_LE\n Channels: 2\n Rates: 44100, 384000\n Altset 2\n Format: SPECIAL DSD_U32_BE\n Channels: 2\n Rates: 768000\n')
    pcm=p/'pcm5p';pcm.mkdir();(pcm/'info').write_text('device: 5\nstream: PLAYBACK\nid: USB Sound\nname: Actual PCM\n')
    device,hw,rates=device_capabilities('auto',tmp_path)
    assert device=='hw:CARD=External,DEV=5' and hw==pcm/'sub0/hw_params'
    assert rates=={44100,384000}
    assert enumerate_outputs(tmp_path)[0]['label'].startswith('USB Actual model')
    # No endpoint key in standard PCM info: two playback PCMs cannot safely be mapped.
    other=p/'pcm9p';other.mkdir();(other/'info').write_text('device: 9\nstream: PLAYBACK\nid: Other\n')
    (p/'stream9').write_text((p/'stream8').read_text())
    rows=enumerate_outputs(tmp_path)
    assert len(rows)==2 and all('ambiguous' in r['error'] for r in rows)
    assert not any('duplicate ALSA' in r['error'] for r in rows)


def test_usb_without_pcm_metadata_never_invents_dev_zero(tmp_path):
    card(tmp_path,0,'External')
    (tmp_path/'card0/pcm0p/info').unlink()
    assert enumerate_outputs(tmp_path)==[]
    with pytest.raises(RuntimeError): device_capabilities('auto',tmp_path)
