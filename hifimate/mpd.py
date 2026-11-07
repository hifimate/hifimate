"""Bounded local MPD protocol and explicit playback edges (never initial state)."""
import socket
import threading
import time
import uuid

PORTS = {'dlna':6600, 'music_assistant_dlna':6601}

class MPD:
    def __init__(self, port, timeout=1):
        self.socket=socket.create_connection(('127.0.0.1',port),timeout=timeout)
        self.file=self.socket.makefile('rb')
        try:
            if not self.file.readline(256).startswith(b'OK MPD '): raise RuntimeError('invalid MPD greeting')
        except Exception:
            self.close(); raise
    def close(self):
        try: self.socket.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: self.file.close()
        finally: self.socket.close()
    def __enter__(self): return self
    def __exit__(self,*_): self.close()
    def command(self, command):
        self.socket.sendall((command+'\n').encode('ascii'))
        result={}; size=0
        while True:
            line=self.file.readline(4097); size+=len(line)
            if not line: raise ConnectionError('MPD connection closed')
            if len(line)>4096 or size>65536: raise RuntimeError('invalid MPD response')
            if line==b'OK\n': return result
            if line.startswith(b'ACK '): raise RuntimeError(line.decode('utf8','replace').strip())
            key,separator,value=line.decode('utf8').rstrip('\n').partition(': ')
            if not separator: raise RuntimeError('invalid MPD response field')
            result[key]=value

class MPDEvents:
    def __init__(self, source, emit):
        self.source=source; self.emit=emit; self.state=None; self.session=None
    def update(self, status):
        state=status.get('state')
        if state not in ('play','pause','stop'): raise RuntimeError('unknown MPD state')
        if state==self.state: return
        old=self.state; self.state=state
        if old is None: return  # Starting the service must never resume background music.
        if state=='play':
            self.session=uuid.uuid4().hex
            self.emit(dict(source=self.source,event='play',event_id=uuid.uuid4().hex,
                           session=self.session,remote={'songid':status.get('songid')}))
        elif self.session:
            self.emit(dict(source=self.source,event='stop',event_id=uuid.uuid4().hex,session=self.session))
            self.session=None
    def disconnected(self):
        # A restarted MPD is no longer playing this session. Its first status
        # after reconnecting is a fresh initial snapshot, never a play edge.
        if self.session:
            self.emit(dict(source=self.source,event='stop',event_id=uuid.uuid4().hex,session=self.session))
        self.state=None; self.session=None

class MPDSubscriber:
    def __init__(self,source,emit,fail,stop,connect=MPD):
        self.events=MPDEvents(source,emit); self.source=source; self.fail=fail
        self.stop=stop; self.connect=connect; self.client=None
        self.thread=threading.Thread(target=self._run,daemon=True)
    def start(self): self.thread.start()
    def _connect(self):
        # MPD may be down for a while (restart, crash loop, slow start). Keep
        # retrying with a bounded backoff instead of failing arbitration for
        # every source; this lane simply has no events until MPD is back.
        delay=.25; since=time.monotonic(); reported=False
        while not self.stop.is_set():
            try:
                client=self.connect(PORTS[self.source])
                if reported: print(f'{self.source} MPD reachable again',flush=True)
                return client
            except OSError as error:
                if not reported and time.monotonic()-since>=15:
                    print(f'{self.source} MPD unavailable ({error}); still retrying',flush=True)
                    reported=True
                self.stop.wait(delay); delay=min(delay*2,5)
        return None
    def _run(self):
        try:
            # MPD restarts (closed connection) reconnect instead of failing the
            # whole arbitration service. Protocol errors/timeouts stay fatal.
            while not self.stop.is_set():
                self.client=self._connect()
                if self.client is None: return
                try:
                    with self.client as client:
                        self.events.update(client.command('status'))
                        while not self.stop.is_set():
                            # MPD idle blocks on a server event, no periodic status poll.
                            client.socket.settimeout(None)
                            client.command('idle player')
                            client.socket.settimeout(1)
                            self.events.update(client.command('status'))
                except TimeoutError: raise
                except OSError:
                    if self.stop.is_set(): return
                    self.events.disconnected()
        except Exception as error:
            if not self.stop.is_set(): self.fail(RuntimeError(f'{self.source} MPD subscription: {error}'))
    def close(self):
        if self.client:
            try: self.client.socket.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self.thread.join(timeout=3)
