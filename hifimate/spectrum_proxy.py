"""HiFi output spectrum server: the SSE stream and the overlay assets.

Serves only /_hifimate/: the spectrum stream (/_hifimate/spectrum), the overlay
script/style and the audio tap used by Music Assistant's web UI (through its
nginx) and the /ctrl page. Every viewer shares one bounded analysis worker in
this process. Anything else is 404.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import socket
import threading
from urllib.parse import parse_qs, urlsplit

from .spectrum import FREQUENCY_SCALES, Spectrum


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, address, assets, spectrum):
        self.assets = Path(assets)
        self.spectrum = spectrum
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    # Unbuffered reads: the SSE loop peeks the socket to notice a closed viewer.
    rbufsize = 0

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.sent = False
        try:
            self.dispatch()
        except (OSError, ValueError):
            self.close_connection = True
            if not getattr(self, 'sent', False):
                self.send_error(500)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    def end_headers(self):
        self.sent = True
        super().end_headers()

    def dispatch(self):
        if (not self.path.startswith('/') or self.path.startswith('//')
                or '\\' in self.path or any(ord(c) < 32 for c in self.path)):
            self.send_error(400)
            return
        path = self.path.split('?', 1)[0]
        if path.startswith('/_hifimate/'):
            self.local(path)
        else:
            self.send_error(404)

    def local(self, path):
        if self.command not in ('GET', 'HEAD'):
            self.send_error(405)
            return
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            self.send_error(403)
            return
        if path == '/_hifimate/spectrum' and self.command == 'GET':
            self.events()
            return
        if path == '/_hifimate/health':
            body = json.dumps({'clients': self.server.spectrum.clients,
                               'analysis_count': self.server.spectrum.analyses}).encode()
            kind = 'application/json'
        elif path in ('/_hifimate/spectrum-overlay.js', '/_hifimate/spectrum-overlay.css', '/_hifimate/spectrum-tap.js',
                      '/_hifimate/nowplaying.js', '/_hifimate/nowplaying.css'):
            body = (self.server.assets / path.rsplit('/', 1)[1]).read_bytes()
            kind = 'text/javascript' if path.endswith('.js') else 'text/css'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def events(self):
        service = self.server.spectrum
        # Each viewer may select its own axis; bands are aggregated server-side.
        query = parse_qs(urlsplit(self.path).query)
        scale = query.get('frequencyScale', [service.frequency_scale])[-1]
        if scale not in FREQUENCY_SCALES:
            self.send_error(400, 'unsupported frequencyScale')
            return
        try:
            service.subscribe(scale)
        except (OSError, RuntimeError):
            self.send_error(503, 'Spectrum unavailable; audio is independent')
            return
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            self.close_connection = True
            version = -1
            while True:
                current, frame = service.latest(version, frequency_scale=scale)
                # Heartbeats detect a closed hidden/idle page within 0.5s.
                payload = (b'data: ' + json.dumps(frame, separators=(',', ':'), allow_nan=False).encode() + b'\n\n'
                           if current != version else b': keepalive\n\n')
                self.wfile.write(payload)
                self.wfile.flush()
                version = current
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and not self.connection.recv(1, socket.MSG_PEEK):
                    return
        except (OSError, ValueError):
            pass
        finally:
            service.unsubscribe(scale)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text())
    from .dsp_model import ResponseModel
    from .spectrum import CamillaState
    service = Spectrum(CamillaState(settings.get('camilla_url', 'ws://127.0.0.1:1234')),
                       ResponseModel(settings['camilladsp'], settings['workdir']), settings.get('fps', 60),
                       frequency_scale=settings.get('frequencyScale', 'log'),
                       channel_layout=settings.get('channelLayout', 'dual-combined'))
    with Server((settings.get('bind', '127.0.0.1'), settings.get('port', 5008)), settings['assets'], service) as server:
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            with service.lifecycle:
                service._stop()


if __name__ == '__main__':
    main()
