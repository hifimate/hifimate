"""Bounded Shairport XML events; buffer recovery and unrelated metadata never claim."""
import base64
import binascii
import uuid
import ipaddress
import re
import threading
import time
import xml.etree.ElementTree as ET

# Shairport's prgr is start/current/end in RTP frames at 44.1 kHz (32-bit, wrapping).
PROGRESS_RATE = 44100
TRACK_CODES = {'minm': 'title', 'asar': 'artist', 'asal': 'album'}

class AirPlayEvents:
    LIMIT=65536
    def __init__(self,emit):
        self.emit=emit; self.session=None; self.playing=False; self.buffer=b''
        self.token=None; self.retired=set(); self.credentials={}; self.remote={}
        # Set when HiFi itself paused this connection's sender (DACP accepted).
        # A sender keeping its RTSP connection then resumes with prsm only.
        self.resume_claims=False; self.lock=threading.Lock()
        # Now playing for /ctrl: core metadata and Shairport progress.
        self.track={}; self.progress=None; self.paused=False; self.clock=time.time
    def remote_paused(self,session):
        with self.lock:
            if self.playing and session==self.session: self.resume_claims=True
    def feed(self,data):
        with self.lock: self._feed(data)
    def _feed(self,data):
        # Process frames incrementally, bounding retained memory, including large
        # irrelevant artwork. Resume scanning after a discarded oversized frame.
        for offset in range(0,len(data),4096):
            self.buffer+=data[offset:offset+4096]
            while b'</item>' in self.buffer:
                item,self.buffer=self.buffer.split(b'</item>',1)
                start=item.find(b'<item>')
                if start>=0 and len(item)-start<=self.LIMIT:
                    self._item(item[start:]+b'</item>')
            if len(self.buffer)>self.LIMIT: self.buffer=b''
    def _item(self,data):
        try:
            root=ET.fromstring(data)
            kind=bytes.fromhex(root.findtext('type','')).decode('ascii')
            code=bytes.fromhex(root.findtext('code','')).decode('ascii')
            if (kind=='core' and code in TRACK_CODES) or (kind=='ssnc' and code in ('mdst','prgr','pfls')):
                self._now_playing(kind,code,root); return
            if kind!='ssnc' or code not in ('pbeg','pres','paus','pend','prsm','daid','acre','clip','conn'): return
            payload=root.find('data'); decoded=b''
            if payload is not None and payload.text:
                if payload.get('encoding')!='base64': return
                compact=''.join(payload.text.split())
                if len(compact)>self.LIMIT: return
                decoded=base64.b64decode(compact,validate=True)
            length=root.findtext('length')
            if length is not None and (not length.isdecimal() or int(length)!=len(decoded)): return
            if code in ('daid','acre','clip','conn'):
                text=decoded.decode('ascii')
                if code=='conn':
                    # A new AirPlay2 handshake must not inherit another peer's token.
                    self.credentials={}; return
                if code=='daid' and not re.fullmatch(r'[0-9A-Fa-f]{1,16}',text): return
                if code=='acre' and not re.fullmatch(r'[0-9]{1,20}',text): return
                if code=='clip': ipaddress.ip_address(text)
                self.credentials[code]=text
                return
            # Local reference patch sends ASCII connection_number; stock sends
            # no payload. Treat other payloads as unsupported, never as identity.
            token=None
            if decoded:
                if len(decoded)>32 or not decoded.isdigit(): return
                token=decoded.decode('ascii')
        except (ET.ParseError,ValueError,UnicodeError,binascii.Error): return
        if code in ('prsm','pbeg','pres'): self.paused=False
        if code in ('paus','pend'): self._pause()
        if code=='pbeg': self.track={}; self.progress=None
        if code=='prsm':
            # Buffer recovery (flush/resume) never claims, except on the
            # connection HiFi paused: there it is the user resuming playback.
            if self.resume_claims and self.playing and token is None:
                self.resume_claims=False; self.session=uuid.uuid4().hex; self._send('play')
            return
        self.resume_claims=False
        if code in ('pbeg','pres'):
            # Reference Shairport tokenizes connection begin/end only. Its
            # pause/resume markers have no payload and refer to that connection.
            if code=='pres' and token is None: token=self.token
            if token in self.retired: return
            # Shairport sends clip/daid/acre only when a play connection
            # starts. A payloadless pbeg after them is a new connection, even
            # if the previous one never sent pend (sender or Shairport crash;
            # HiFi holds the FIFO read-write, so it sees no writer EOF).
            new_connection=code=='pbeg' and token is None and bool(self.credentials)
            if self.playing and not new_connection and (token is None or token==self.token): return
            if self.token is not None and self.token!=token: self.retired.add(self.token)
            if len(self.retired)>4096: raise RuntimeError('AirPlay session budget exhausted')
            if code=='pbeg':
                self.remote=dict(self.credentials); self.credentials={}
            self.token=token; self.session=uuid.uuid4().hex; self.playing=True; self._send('play')
        elif self.playing:
            # Tokenized sessions cannot be ended by stale or payloadless events.
            # Payloadless stock sessions retain their necessarily weaker fencing.
            if self.token is not None and token!=self.token and not (code=='paus' and token is None): return
            if self.token is None and token is not None: return
            self._send('stop'); self.playing=False
            if code=='pend' and self.token is not None: self.retired.add(self.token)
    def _now_playing(self,kind,code,root):
        payload=root.find('data'); text=''
        if payload is not None and payload.text and payload.get('encoding')=='base64':
            compact=''.join(payload.text.split())
            if len(compact)>4096: return
            text=base64.b64decode(compact,validate=True).decode('utf-8','replace').strip()
        if code=='mdst': self.track={}             # a new track's metadata bundle
        elif code in TRACK_CODES: self.track[TRACK_CODES[code]]=text[:200]
        elif code=='pfls': self._pause()           # flush: the sender paused or seeks
        elif code=='prgr':
            parts=text.split('/')
            if len(parts)!=3 or not all(p.isdecimal() for p in parts): return
            start,current,end=(int(p) for p in parts)
            span=lambda a,b: ((b-a)%(1<<32))/PROGRESS_RATE
            self.progress={'position':span(start,current),'duration':span(start,end),'at':self.clock()}
            self.paused=False
    def _pause(self):
        """Freeze the progress where it stopped (only once per pause)."""
        if self.progress and not self.paused:
            now=self.clock()
            position=min(self.progress['duration'],self.progress['position']+max(0.0,now-self.progress['at']))
            self.progress={**self.progress,'position':position,'at':now}
        self.paused=True
    def now_playing(self):
        """Title/artist/album and progress of the current connection (no credentials)."""
        with self.lock:
            if not (self.playing or self.track or self.progress): return None
            out={**self.track,'state':'pause' if self.paused or not self.playing else 'play',
                 'can_control':bool(self.remote.get('daid') and self.remote.get('acre'))}
            if self.progress:
                elapsed=0 if out['state']!='play' else max(0.0,self.clock()-self.progress['at'])
                out.update(duration=round(self.progress['duration'],1),
                           position=round(min(self.progress['duration'],self.progress['position']+elapsed),1))
            return out
    def credentials_for_control(self):
        with self.lock: return dict(self.remote)
    def _send(self,action):
        self.emit(dict(source='airplay',event=action,event_id=uuid.uuid4().hex,device='shairport-metadata',session=self.session,remote=dict(self.remote)))
