from hifimate.receiver_events import ReceiverEvents

class Controller:
    def __init__(self): self.owner=None; self.candidate=None
    def select(self,i): self.candidate=i
    def idle(self): self.owner=None; self.candidate=None

def test_explicit_requests_not_observations():
    c=Controller(); b=ReceiverEvents(c)
    b.update(); assert c.owner is None
    assert not b.request('dlna','play','legacy-hook')
    assert b.request('dlna','play','a',session='one'); candidate=c.candidate
    assert c.owner is None  # A request alone never grants audio.
    assert not b.request('dlna','play','a',session='one'); assert c.candidate==candidate
    assert not b.request('airplay','stop','b'); assert c.candidate==candidate
    assert b.request('dlna','stop','c',session='one'); assert c.candidate is None

def test_new_request_retires_old_lane_and_session():
    c=Controller(); b=ReceiverEvents(c)
    b.request('dlna','play','a',session='old'); old=c.candidate
    b.request('music_assistant_dlna','play','b',session='new')
    assert c.candidate!=old and c.owner is None and b.source=='music_assistant_dlna'
    assert not b.request('dlna','stop','c',session='old')
    assert not b.request('dlna','play','replay',session='old')
