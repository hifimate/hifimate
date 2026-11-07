import os
import socket
import pytest
from hifimate.runtime_paths import RuntimePaths

def test_private_lock_exclusion_and_stale_socket(tmp_path):
    tmp_path.chmod(0o700); path=tmp_path/'control'
    s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET); s.bind(str(path)); s.close()
    r=RuntimePaths(path).acquire()
    assert not path.exists()
    with pytest.raises(BlockingIOError): RuntimePaths(path).acquire()
    r.close(); RuntimePaths(path).acquire().close()

def test_lock_symlink_and_insecure_parent_rejected(tmp_path):
    tmp_path.chmod(0o700); (tmp_path/'controller.lock').symlink_to(tmp_path/'other')
    with pytest.raises(OSError): RuntimePaths(tmp_path/'control').acquire()
    (tmp_path/'controller.lock').unlink(); tmp_path.chmod(0o755)
    with pytest.raises(RuntimeError): RuntimePaths(tmp_path/'control').acquire()

def test_fifo_reused_drained_and_unrelated_file_preserved(tmp_path):
    tmp_path.chmod(0o700); r=RuntimePaths(tmp_path/'control').acquire()
    fd=r.fifo(); inode=(tmp_path/'airplay.metadata').stat().st_ino
    os.write(fd,b'stale'); r.close()
    r=RuntimePaths(tmp_path/'control').acquire(); fd=r.fifo()
    assert (tmp_path/'airplay.metadata').stat().st_ino==inode
    with pytest.raises(BlockingIOError): os.read(fd,100)
    r.close()
    (tmp_path/'airplay.metadata').unlink(); (tmp_path/'airplay.metadata').write_text('keep')
    r=RuntimePaths(tmp_path/'control').acquire()
    with pytest.raises(RuntimeError): r.fifo()
    r.close(); assert (tmp_path/'airplay.metadata').read_text()=='keep'

def test_partial_startup_releases_lock_and_silences(tmp_path,monkeypatch):
    from hifimate import format_daemon
    tmp_path.chmod(0o700); actions=[]
    class Controller:
        def __init__(self,config): pass
        def mute(self): actions.append('mute')
        def rpc(self,*args): actions.append(args[0])
        def idle(self): actions.append('idle')
    def fail(*args,**kwargs): raise OSError('missing subscriber')
    monkeypatch.setattr(format_daemon,'LinuxFormatController',Controller)
    monkeypatch.setattr(format_daemon.subprocess,'Popen',fail)
    with pytest.raises(OSError): format_daemon.serve({'socket':str(tmp_path/'control')})
    assert actions==['mute','idle']  # mute() now also stops DSP
    assert not (tmp_path/'control').exists()
    RuntimePaths(tmp_path/'control').acquire().close()
