"""Fast local enqueue client; never waits for source format or DSP preparation."""
import argparse
import json
import os
import socket
import uuid

def main():
    p=argparse.ArgumentParser(); p.add_argument('source',choices=['dlna','music_assistant_dlna','airplay','bluetooth']); p.add_argument('action',choices=['play','stop'])
    p.add_argument('--socket',default=f'/run/user/{os.geteuid()}/hifimate/format.sock'); a=p.parse_args()
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as s:
        s.settimeout(.2); s.connect(a.socket)
        s.sendall(json.dumps({'action':'receiver','source':a.source,'event':a.action,'event_id':uuid.uuid4().hex}).encode())
        # The server acknowledges enqueue, never waits for audio readiness.
        response=json.loads(s.recv(4096))
        return 0 if response.get('ok') else 1
if __name__=='__main__': raise SystemExit(main())
