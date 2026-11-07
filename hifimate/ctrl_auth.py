"""The control password is the Linux password of the user HiFiMate runs as.

Nothing is stored: a login is checked by PAM (service ``hifimate``,
/etc/pam.d/hifimate, written by the system install: pam_unix only). The
control service runs as that user, and pam_unix lets an unprivileged process
check its own user's password (through the setgid unix_chkpwd helper), so no
root and no shadow access is needed. In a system install the user is the
dedicated `hifimate` (set with `sudo hifimate password`, which also sets the
Samba share's password); in a user install it is the login password of the
user who installed it.

Sessions are random tokens kept in memory (only their hashes), bounded, and
dropped when the service restarts (`hifimate password` restarts it).
Never log secrets.
"""
from collections import OrderedDict, deque
import ctypes
import ctypes.util
import hashlib
import os
import pwd
import secrets
import threading
import time

SESSION_SECONDS=30*86400
SERVICE='hifimate'

# --- PAM through ctypes (libpam is on every Linux; no Python package needed) ----------

PAM_SUCCESS=0
PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON = 1, 2


class _Message(ctypes.Structure):
    _fields_=[('msg_style',ctypes.c_int),('msg',ctypes.c_char_p)]


class _Response(ctypes.Structure):
    _fields_=[('resp',ctypes.c_void_p),('resp_retcode',ctypes.c_int)]


_CONV=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.POINTER(_Message)),
                       ctypes.POINTER(ctypes.POINTER(_Response)),ctypes.c_void_p)


class _Conv(ctypes.Structure):
    _fields_=[('conv',_CONV),('appdata_ptr',ctypes.c_void_p)]


def _library(name,fallback):
    return ctypes.CDLL(ctypes.util.find_library(name) or fallback,use_errno=True)


_pam=None


def _load():
    global _pam
    if _pam is None:
        pam=_library('pam','libpam.so.0'); libc=_library('c','libc.so.6')
        pam.pam_start.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.POINTER(_Conv),ctypes.POINTER(ctypes.c_void_p)]
        for name in ('pam_authenticate','pam_end'):
            getattr(pam,name).argtypes=[ctypes.c_void_p,ctypes.c_int]
        libc.calloc.restype=ctypes.c_void_p; libc.calloc.argtypes=[ctypes.c_size_t,ctypes.c_size_t]
        libc.strdup.restype=ctypes.c_void_p; libc.strdup.argtypes=[ctypes.c_char_p]
        _pam=(pam,libc)
    return _pam


def pam_available():
    try:
        _load(); return True
    except OSError:
        return False


def pam_check(password,user=None,service=SERVICE):
    """True when password is user's Linux password (default: the user running this)."""
    pam,libc=_load()
    user=user or pwd.getpwuid(os.getuid()).pw_name
    secret=password.encode()

    def conversation(count,messages,responses,_):
        # Answers live in malloc'd memory: PAM frees them.
        replies=libc.calloc(count,ctypes.sizeof(_Response))
        if not replies: return 5   # PAM_BUF_ERR
        responses[0]=ctypes.cast(replies,ctypes.POINTER(_Response))
        for i in range(count):
            if messages[i].contents.msg_style in (PAM_PROMPT_ECHO_OFF,PAM_PROMPT_ECHO_ON):
                responses[0][i].resp=libc.strdup(secret)
        return PAM_SUCCESS

    callback=_CONV(conversation)       # kept referenced until pam_end
    handle=ctypes.c_void_p()
    status=pam.pam_start(service.encode(),user.encode(),ctypes.byref(_Conv(callback,None)),ctypes.byref(handle))
    if status!=PAM_SUCCESS: return False
    try:
        # Authentication only: pam_unix's account check needs root (it logs
        # "setuid failed" on every login) and has nothing to say about a
        # nologin service user; a locked password already fails here.
        status=pam.pam_authenticate(handle,0)
        return status==PAM_SUCCESS
    finally:
        pam.pam_end(handle,status)


# --- sessions ---------------------------------------------------------------------------

class Auth:
    def __init__(self,verify=None,available=None,clock=time.time):
        # Looked up at call time (not bound here), so tests can stand in for PAM.
        self.verify=verify or (lambda password: pam_check(password))
        self.check_available=available or (lambda: pam_available())
        self.clock=clock; self.lock=threading.RLock()
        self.sessions=OrderedDict(); self.attempts=OrderedDict()

    def available(self):
        return self.check_available()

    def valid(self,token):
        with self.lock:
            key=hashlib.sha256(token.encode()).hexdigest()
            return self.sessions.get(key,0)>self.clock()

    def login(self,password,peer):
        if not self.available(): return 403,None,'这台主机不能用 PAM 验证密码'
        with self.lock:
            now=self.clock()
            # One global budget also bounds attempts from forged/proxied addresses.
            for key in ('*',peer):
                attempts=self.attempts.setdefault(key,deque())
                while attempts and attempts[0]<=now-60: attempts.popleft()
                if len(attempts)>=(20 if key=='*' else 5): return 429,None,'登录尝试过多，请稍后重试'
            self.attempts['*'].append(now); self.attempts[peer].append(now)
            while len(self.attempts)>256:
                key=next(k for k in self.attempts if k!='*'); del self.attempts[key]
        if not isinstance(password,str) or not password or len(password)>1024 or '\0' in password:
            return 401,None,'密码错误'
        # Outside the lock: a failed PAM check deliberately takes about 2 s.
        if not self.verify(password): return 401,None,'密码错误'
        with self.lock:
            token=secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()]=now+SESSION_SECONDS
            while len(self.sessions)>256: self.sessions.popitem(last=False)
            return 200,token,None

    def logout(self,token):
        with self.lock: self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(),None)
