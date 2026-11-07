import io
import threading
from types import SimpleNamespace
import pytest
from hifimate.mpd import MPD,MPDEvents,MPDSubscriber


@pytest.mark.parametrize('source', ['dlna', 'music_assistant_dlna'])
def test_mpd_initial_play_and_song_changes_never_create_intent(source):
    sent=[]; events=MPDEvents(source,sent.append)
    events.update({'state':'play','songid':'1'}); events.update({'state':'play','songid':'2'})
    assert not sent
    events.update({'state':'pause'}); events.update({'state':'play','songid':'2'})
    first=sent[-1]; assert first['event']=='play' and first['remote']['songid']=='2'
    events.update({'state':'play','songid':'3'}); assert len(sent)==1
    events.update({'state':'stop'}); assert sent[-1]['session']==first['session']
    events.update({'state':'play'}); assert sent[-1]['session']!=first['session']


def test_mpd_transport_reads_real_protocol_and_rejects_ack(monkeypatch):
    sent=[]; closed=[]
    class Socket:
        def makefile(self,mode): return io.BytesIO(b'OK MPD 0.24.0\nstate: play\nsongid: 1\nOK\nACK [5@0] {pause} error\n')
        def sendall(self,data): sent.append(data)
        def shutdown(self,_): closed.append('shutdown')
        def close(self): closed.append('close')
    monkeypatch.setattr('socket.create_connection',lambda address,timeout:Socket())
    with MPD(6600) as client:
        assert client.command('status')=={'state':'play','songid':'1'}
        with pytest.raises(RuntimeError,match='ACK'): client.command('pause 1')
    assert sent==[b'status\n',b'pause 1\n'] and closed==['shutdown','close']


def test_mpd_idle_subscription_fault_is_fatal_without_polling():
    commands=[]; failures=[]; sent=[]; stop=threading.Event()
    class Client:
        socket=SimpleNamespace(settimeout=lambda _:None)
        def __init__(self,_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def command(self,command):
            commands.append(command)
            if len(commands)==4: raise RuntimeError('MPD EOF')
            return {'state':'stop' if len(commands)==1 else 'play'}
    listener=MPDSubscriber('dlna',sent.append,failures.append,stop,Client)
    listener._run()
    assert commands==['status','idle player','status','idle player']
    assert len(sent)==1 and sent[0]['event']=='play' and 'EOF' in str(failures[0])


def test_mpd_restart_reconnects_ends_session_and_never_resumes():
    connects=[]; failures=[]; sent=[]; stop=threading.Event()
    script=[  # one list of status/idle replies per MPD connection
        [{'state':'stop'},{},{'state':'play','songid':'1'},ConnectionError('MPD connection closed')],
        [{'state':'play','songid':'1'},{},{'state':'pause'},{},{'state':'play'},RuntimeError('ACK protocol')],
    ]
    class Client:
        socket=SimpleNamespace(settimeout=lambda _:None)
        def __init__(self,_):
            connects.append(1)
            if len(connects)==2: raise ConnectionRefusedError('MPD restarting')
            self.replies=list(script[len(connects)//2])
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def command(self,_):
            reply=self.replies.pop(0)
            if isinstance(reply,Exception): raise reply
            return reply
    listener=MPDSubscriber('dlna',sent.append,failures.append,stop,Client)
    listener.stop.wait=lambda _: False  # no real backoff sleep in the test
    listener._run()
    assert len(connects)==3  # closed, refused while restarting, reconnected
    play,restart_stop,second_play=sent
    assert play['event']=='play'
    # The restart ends the old session; the reconnect snapshot (play) is not
    # a new intent, only the later pause->play edge is.
    assert restart_stop['event']=='stop' and restart_stop['session']==play['session']
    assert second_play['event']=='play' and second_play['session']!=play['session']
    # Protocol errors on the reconnected subscription remain fatal.
    assert len(failures)==1 and 'ACK protocol' in str(failures[0])


def test_mpd_outage_is_retried_not_fatal_and_reported_once(monkeypatch, capsys):
    failures=[]; sent=[]; stop=threading.Event(); clock=[0.0]; waits=[]
    monkeypatch.setattr('hifimate.mpd.time.monotonic',lambda: clock[0])
    attempts=[]
    class Client:
        socket=SimpleNamespace(settimeout=lambda _:None)
        def __init__(self,_):
            attempts.append(clock[0])
            if len(attempts)<40: raise ConnectionRefusedError('down')  # ~3 minutes down
            self.replies=[{'state':'stop'},RuntimeError('ACK end of test')]
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def command(self,_):
            reply=self.replies.pop(0)
            if isinstance(reply,Exception): raise reply
            return reply
    listener=MPDSubscriber('music_assistant_dlna',sent.append,failures.append,stop,Client)
    def wait(seconds): waits.append(seconds); clock[0]+=seconds; return False
    listener.stop.wait=wait
    listener._run()
    assert clock[0]>60 and len(attempts)==40  # never gave up while MPD was down
    assert max(waits)==5 and waits[0]==.25     # bounded exponential backoff
    out=capsys.readouterr().out
    assert out.count('MPD unavailable')==1 and 'reachable again' in out
    assert len(failures)==1 and 'ACK end of test' in str(failures[0]) and not sent


def test_stop_ends_an_outage_retry_without_failure():
    failures=[]; stop=threading.Event()
    def refuse(_):
        stop.set(); raise ConnectionRefusedError('down')
    listener=MPDSubscriber('dlna',lambda _:None,failures.append,stop,refuse)
    listener._run()
    assert not failures


def test_mpd_eof_is_a_closed_connection():
    class Socket:
        def makefile(self,mode): return io.BytesIO(b'OK MPD 0.24.0\nstate: play\n')
        def sendall(self,data): pass
        def shutdown(self,_): pass
        def close(self): pass
    import socket as socket_module
    original=socket_module.create_connection
    socket_module.create_connection=lambda address,timeout:Socket()
    try:
        with MPD(6600) as client, pytest.raises(ConnectionError):
            client.command('status')
    finally:
        socket_module.create_connection=original
