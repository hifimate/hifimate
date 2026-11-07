import asyncio

from hifimate.bluetooth import BlueZEvents, BlueZSubscriber, PLAYER, DEVICE
from hifimate.receiver_events import ReceiverEvents

A='/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF'; B='/org/bluez/hci0/dev_11_22_33_44_55_66'

def test_baseline_and_connection_never_play():
    events=[]; b=BlueZEvents(events.append)
    b.baseline({A:{DEVICE:{'Connected':True}},A+'/player0':{PLAYER:{'Status':'playing','Device':A}}})
    b.properties(A,DEVICE,{'Connected':True})
    b.properties(A+'/player0',PLAYER,{'Track':{}})
    b.properties(A+'/player0',PLAYER,{'Status':'playing'})
    assert events==[]
    b.properties(A+'/player0',PLAYER,{'Status':'paused'})
    b.properties(A+'/player0',PLAYER,{'Status':'playing'})
    assert len(events)==1 and events[0]['event']=='play'
    b.properties(A+'/player0',PLAYER,{'Status':'playing'})
    assert len(events)==1
    b.properties(A,DEVICE,{'Connected':False})
    assert events[-1]['event']=='stop' and events[-1]['session']==events[0]['session']

def test_first_signal_and_device_stop_isolation():
    class C:
        def select(self,i): self.owner=i
        def idle(self): self.owner=None
    c=C(); bridge=ReceiverEvents(c); sent=[]
    def emit(e):
        sent.append(e); bridge.request(e['source'],e['event'],e['event_id'],e['device'],e['session'])
    b=BlueZEvents(emit)
    b.properties(A+'/player0',PLAYER,{'Status':'playing'})
    b.properties(B+'/player0',PLAYER,{'Status':'playing'})
    owner=c.owner
    b.properties(A+'/player0',PLAYER,{'Status':'paused'})
    assert c.owner==owner
    b.removed(B,[DEVICE]); assert c.owner is None

def test_old_session_stop_cannot_end_resumed_device():
    class C:
        def select(self,i): self.owner=i
        def idle(self): self.owner=None
    c=C(); bridge=ReceiverEvents(c)
    bridge.request('bluetooth','play','1',A,'old')
    bridge.request('bluetooth','play','2',A,'new'); owner=c.owner
    assert not bridge.request('bluetooth','stop','3',A,'old')
    assert c.owner==owner


def test_old_player_on_same_device_cannot_end_new_player_session():
    events=[]; b=BlueZEvents(events.append)
    b.properties(A+'/player0',PLAYER,{'Status':'playing'})
    b.properties(A+'/player1',PLAYER,{'Status':'playing'})
    assert events[-1]['remote']['player']==A+'/player1'
    b.properties(A+'/player0',PLAYER,{'Status':'paused'})
    b.removed(A+'/player0',[PLAYER])
    assert len(events)==2
    b.properties(A+'/player1',PLAYER,{'Status':'paused'})
    assert events[-1]['event']=='stop' and events[-1]['session']==events[1]['session']


def test_close_after_loop_shutdown_is_safe():
    class Bus:
        def __init__(self): self.disconnects = 0
        def disconnect(self): self.disconnects += 1
    class Thread:
        def join(self, timeout): pass

    subscriber = BlueZSubscriber(lambda _: None, lambda _: None)
    subscriber.loop = asyncio.new_event_loop()
    subscriber.loop.close()
    subscriber.bus = Bus()
    subscriber.thread = Thread()
    subscriber.close()
    assert subscriber.bus.disconnects == 0
