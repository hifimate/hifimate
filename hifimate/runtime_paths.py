"""Private runtime ownership helpers; never follow lock/FIFO symlinks."""
import fcntl
import os
from pathlib import Path
import socket
import stat

class RuntimePaths:
    def __init__(self,path):
        self.path=Path(path); self.lock=None; self.fifo_fd=None; self.fifo_identity=None; self.socket_identity=None
    def acquire(self):
        info=self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError('runtime parent must be private owned directory')
        fd=os.open(self.path.parent/'controller.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode & 0o077:
                raise RuntimeError('unsafe runtime lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.lock=fd
            if self.path.exists() or self.path.is_symlink():
                info=self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.geteuid(): raise RuntimeError('unsafe endpoint')
                with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as probe:
                    probe.settimeout(.2)
                    try: probe.connect(str(self.path))
                    except ConnectionRefusedError: self.path.unlink()
                    else: raise RuntimeError('endpoint active')
            return self
        except BaseException:
            self.lock=None; os.close(fd); raise
    def fifo(self):
        path=self.path.parent/'airplay.metadata'
        if path.exists() or path.is_symlink():
            info=path.lstat()
            if not stat.S_ISFIFO(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
                raise RuntimeError('unsafe metadata FIFO')
        else: os.mkfifo(path,0o600)
        fd=os.open(path,os.O_RDWR|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC)
        info=os.fstat(fd)
        self.fifo_identity=(info.st_dev,info.st_ino); self.fifo_fd=fd
        # Reuse an owned FIFO inode so an already-open Shairport writer remains
        # connected after restart. Drain stale queued records before admission.
        while True:
            try:
                if not os.read(fd,8192): break
            except BlockingIOError: break
        return fd
    def bound(self):
        info=self.path.lstat(); self.socket_identity=(info.st_dev,info.st_ino)
    def close(self):
        if self.fifo_fd is not None: os.close(self.fifo_fd); self.fifo_fd=None
        # Keep owned FIFO inode for existing writer; next owner drains it.
        if self.socket_identity and self.path.exists():
            info=self.path.lstat()
            if (info.st_dev,info.st_ino)==self.socket_identity: self.path.unlink()
        if self.lock is not None: os.close(self.lock); self.lock=None
