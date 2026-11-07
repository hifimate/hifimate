import gzip
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from hifimate import realtime as rt


def test_kernel_evidence(tmp_path):
    sys=tmp_path/'sys';(sys/'kernel').mkdir(parents=True)
    boot=tmp_path/'boot';boot.mkdir();proc=tmp_path/'proc';proc.mkdir()
    detect=lambda:rt.kernel_realtime(sys,boot,proc,'test-rt')
    assert detect() is None  # suffix alone is not evidence
    (boot/'config-test-rt').write_text('CONFIG_PREEMPT_RT=y\n')
    assert detect() is True
    (sys/'kernel/realtime').write_text('0\n');assert detect() is False
    (sys/'kernel/realtime').write_text('1\n');assert detect() is True
    (sys/'kernel/realtime').unlink();(boot/'config-test-rt').unlink()
    with gzip.open(proc/'config.gz','wt') as f:f.write('# CONFIG_PREEMPT_RT is not set\n')
    assert detect() is False
    (proc/'config.gz').write_bytes(b'\x1f\x8b')
    assert detect() is None  # truncated config must not prevent service startup


def test_scheduler_fallback_and_bounds(monkeypatch):
    calls=[]
    monkeypatch.setattr(rt.resource,'getrlimit',lambda _: (-1,-1))
    monkeypatch.setattr(rt.resource,'setrlimit',lambda *a:calls.append(('limit',a)))
    def denied(*args):
        calls.append(('rr',args));raise PermissionError('denied')
    monkeypatch.setattr(rt.os,'sched_setscheduler',denied)
    assert not rt.configure('auto',lambda:None) and calls==[]
    assert not rt.configure('off',lambda:True) and calls==[]
    assert not rt.configure('on',lambda:False)
    assert calls[0][1][1]==(5000000,5000000)
    assert calls[1][0]=='rr'
    monkeypatch.setattr(rt.os,'sched_setscheduler',lambda *a:calls.append(('success',a)))
    monkeypatch.setattr(rt.resource,'getrlimit',lambda _: (10000,20000))
    assert rt.configure('auto',lambda:True)
    assert calls[-2][1][1]==(10000,10000)


def test_install_modes_and_uninstall(tmp_path):
    root=Path(__file__).parents[1];prefix=tmp_path/'hifimate'
    def install(mode):return subprocess.run([str(root/'scripts/install.sh'),'--prefix',str(prefix),'--apply','--realtime',mode],capture_output=True,text=True)
    assert install('invalid').returncode!=0 and not prefix.exists()
    for mode in ('on','off','auto','auto'):
        result=install(mode);assert result.returncode==0,result.stderr
        assert json.loads((prefix/'config/realtime.json').read_text())=={'mode':mode}
        assert (prefix/'bin/hifimate-rt').exists()
        manifest=(prefix/'.hifimate-install-manifest').read_text()
        assert 'config/realtime.json' in manifest and 'bin/hifimate-rt' in manifest
        for unit in ('hifimate-camilladsp','hifimate-mpd','hifimate-music-mpd','hifimate-airplay'):
            assert '/bin/hifimate-rt ' in (prefix/f'systemd/{unit}.service').read_text()
        for unit in ('hifimate','hifimate-spectrum','hifimate-dlna'):
            assert '/bin/hifimate-rt ' not in (prefix/f'systemd/{unit}.service').read_text()
    result=subprocess.run([str(root/'scripts/uninstall.sh'),'--prefix',str(prefix),'--apply'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert not (prefix/'config/realtime.json').exists()
    assert not (prefix/'bin/hifimate-rt').exists()


def test_failed_limit_still_executes_audio_program(tmp_path,monkeypatch):
    config=tmp_path/'policy.json';config.write_text('{"mode":"on"}')
    monkeypatch.setattr('sys.argv',['hifimate-rt',str(config),'--','audio-program','--argument'])
    monkeypatch.setattr(rt.resource,'getrlimit',lambda _: (-1,-1))
    def denied(*a):raise PermissionError('limit denied')
    monkeypatch.setattr(rt.resource,'setrlimit',denied)
    monkeypatch.setattr(rt.os,'sched_setscheduler',lambda *a:pytest.fail('must set bounded limit first'))
    calls=[]
    monkeypatch.setattr(rt.os,'execvp',lambda *a:calls.append(a))
    rt.main()
    assert calls==[('audio-program',['audio-program','--argument'])]


def test_unavailable_limits_fall_back(monkeypatch):
    def unavailable(*args):
        raise OSError('limits unavailable')
    monkeypatch.setattr(rt.resource, 'getrlimit', unavailable)
    monkeypatch.setattr(rt.os, 'sched_setscheduler', lambda *a: pytest.fail('no bounded limit'))
    assert rt.configure('on') is False


@pytest.mark.parametrize('mode', ['off', 'on'])
def test_real_exec_without_realtime_permission(tmp_path, mode):
    config = tmp_path/'policy.json'
    config.write_text(json.dumps({'mode': mode}))
    def ordinary_only():
        rt.os.sched_setscheduler(0, rt.os.SCHED_OTHER, rt.os.sched_param(0))
        rt.resource.setrlimit(rt.resource.RLIMIT_RTPRIO, (0, 0))
    result = subprocess.run(
        [sys.executable, '-B', '-m', 'hifimate.realtime', str(config), '--',
         sys.executable, '-c', 'import os,sys; print(os.sched_getscheduler(0), sys.argv[1])',
         'audio argument'],
        cwd=Path(__file__).parents[1], capture_output=True, text=True,
        preexec_fn=ordinary_only, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0 audio argument'
    assert ('ordinary scheduling' in result.stderr) == (mode == 'on')


def test_startup_burst_under_real_rr_is_not_killed(tmp_path):
    """Shairport/MPD start with >200 ms of non-blocking work; RTTIME must allow it."""
    import resource
    if resource.getrlimit(resource.RLIMIT_RTPRIO)[0] < rt.PRIORITY or rt.kernel_realtime() is not True:
        pytest.skip('needs a PREEMPT_RT kernel and RLIMIT_RTPRIO >= 5')
    config = tmp_path / 'realtime.json'; config.write_text('{"mode":"on"}')
    burst = ('import os,time\n'
             'assert os.sched_getscheduler(0)==os.SCHED_RR\n'
             'end=time.thread_time()+1.0\n'
             'while time.thread_time()<end: pass\n'  # 1 s of CPU without blocking
             'print("survived")\n')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]))
    result = subprocess.run([sys.executable, '-m', 'hifimate.realtime', str(config), '--',
                             sys.executable, '-I', '-c', burst], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and 'survived' in result.stdout, (result.returncode, result.stderr)


def test_pipewire_data_loops_are_realtime_within_the_hifi_limit():
    # module-rt defaults (83/88) exceed RLIMIT_RTPRIO 5 and RTKit is denied to
    # the headless user, so every PipeWire process needs rt.prio <= PRIORITY.
    # The override is a top-level section: inside context.properties it is ignored.
    import re
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import install_files as files
    root = Path(__file__).resolve().parents[1] / 'config/host-stack'
    names = [n for n in files.FRAGMENTS if n.endswith('52-hifimate-realtime.conf')]
    assert {n.split('/')[1] for n in names} == {'pipewire.conf.d', 'pipewire-pulse.conf.d',
                                                 'client.conf.d', 'wireplumber.conf.d'}
    for name in names:
        text = re.sub(r'#.*', '', (root / name).read_text())
        if name.startswith('wireplumber/'):
            # WirePlumber ignores module.rt.args: an appended module-rt instance.
            assert 'name = libpipewire-module-rt' in text and 'context.modules' in text, name
        else:
            assert re.fullmatch(r'\s*module\.rt\.args\s*=\s*\{[^{}]*\}\s*', text), name
        # A session bus makes module-rt prefer RTKit, which polkit denies here.
        assert re.search(r'rtkit\.enabled\s*=\s*false', text) and re.search(r'rtportal\.enabled\s*=\s*false', text), name
        prio = int(re.search(r'rt\.prio\s*=\s*(\d+)', text)[1])
        assert 1 <= prio <= rt.PRIORITY
        # A failed nice -11 (RLIMIT_NICE 0) disables realtime as well.
        assert re.search(r'nice\.level\s*=\s*20\b', text), name
        # Leave RLIMIT_RTTIME alone: hifimate-rt processes need more than 200 ms.
        assert 'rt.time' not in text
