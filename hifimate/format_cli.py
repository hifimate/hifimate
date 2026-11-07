"""Send explicit local owner/format/idle events; never infer play from audio."""
import argparse
import json
import os
import socket

def main():
    p=argparse.ArgumentParser(); p.add_argument('--socket',default=f'/run/user/{os.geteuid()}/hifimate/format.sock')
    p.add_argument('event',help='JSON event; see INSTALL.md')
    a=p.parse_args(); data=a.event.encode()
    if len(data)>4096: p.error('event too large')
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as s:
        s.settimeout(15); s.connect(a.socket); s.sendall(data); response=json.loads(s.recv(8192)); print(json.dumps(response))
        return 0 if response.get('ok') else 1
if __name__=='__main__': raise SystemExit(main())
