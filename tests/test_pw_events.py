import json
import threading
from hifimate.pw_events import watch


class Stream:
    def __init__(self, frames):
        self.data = iter(frames)
    def read1(self, size):
        return next(self.data, b'')


class Controller:
    owner = 'session'
    binding = (42, 7, 99, 'dlna_in')


def test_queries_do_not_generate_observations_and_utf8_may_split():
    client = {'id': 101, 'type': 'PipeWire:Interface:Client',
              'info': {'props': {'application.name': 'pw-top'}}}
    node = {'id': 42, 'type': 'PipeWire:Interface:Node', 'info': {
        'props': {'node.name': '音乐', 'media.class': 'Stream/Output/Audio',
                  'object.serial': 99}, 'state': 'running',
        'params': {'Format': [{'rate': 44100}]}}}
    first = json.dumps([client, node], ensure_ascii=False).encode()
    split = first.index('音'.encode()) + 1
    frames = [first[:split], first[split:], json.dumps([client, node]).encode(),
              json.dumps([{'id': 101, 'info': None}]).encode(),
              json.dumps([{'id': 42, 'info': None}]).encode()]
    events = []
    watch(Stream(frames), Controller(), events.append, threading.Event())
    assert events == [None, None]


def test_idle_observations_do_not_claim_or_refresh():
    controller = Controller()
    controller.owner = None
    frame = json.dumps([{'id': 1, 'type': 'PipeWire:Interface:Node', 'info': {
        'props': {'media.class': 'Audio/Source'}, 'state': 'running'}}]).encode()
    events = []
    watch(Stream([frame]), controller, events.append, threading.Event())
    assert not events


def test_unrelated_sources_ignored_and_partial_binding_updates_kept():
    ignored={'id': 90, 'type':'PipeWire:Interface:Node', 'info': {
        'props': {'node.name':'another-source','media.class':'Audio/Source'},'state':'running'}}
    selected={'id':42,'type':'PipeWire:Interface:Node','info':{
        'props':{'node.name':'receiver','media.class':'Stream/Output/Audio'},
        'state':'running','params':{'Format':[{'rate':44100}]}}}
    partial={'id':42,'type':'PipeWire:Interface:Node','info':{'params':{'Format':[{'rate':768000}]}}}
    events=[]
    frames=[json.dumps([ignored,selected]).encode(),json.dumps([partial]).encode(),
            json.dumps([partial]).encode()]
    watch(Stream(frames),Controller(),events.append,threading.Event())
    assert len(events)==2


def test_pending_candidate_without_owner_receives_negotiation_updates():
    c=Controller(); c.owner=None; c.binding=None; c.candidate='pending'
    frame=json.dumps([{'id':42,'type':'PipeWire:Interface:Node','info':{
        'props':{'media.class':'Stream/Output/Audio','object.serial':99},
        'params':{'Format':[{'rate':44100}]}}}]).encode()
    events=[]
    watch(Stream([frame]),c,events.append,threading.Event(),lambda:('dlna',None))
    assert events==[None]
