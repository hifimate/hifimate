from contextlib import closing, contextmanager
import http.client
import json
from pathlib import Path
import socket
import threading

from hifimate.spectrum import Spectrum
from hifimate.spectrum_proxy import Server
from test_spectrum import until

ROOT = Path(__file__).parents[1]


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(server, path, method='GET', body=None, headers=None):
    with closing(http.client.HTTPConnection(*server.server_address, timeout=3)) as connection:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()


def test_serves_only_the_spectrum_assets(tmp_path):
    service = Spectrum()
    with running(Server(('127.0.0.1', 0), ROOT / 'gui', service)) as server:
        assert request(server, '/_hifimate/spectrum-overlay.js')[0] == 200
        assert b'new Stream' in request(server, '/_hifimate/spectrum-overlay.js')[2]
        assert request(server, '/_hifimate/spectrum-overlay.css')[0] == 200
        status, headers, body = request(server, '/_hifimate/spectrum-tap.js')
        assert status == 200 and b'__hifimateAudioTap' in body
        assert request(server, '/_hifimate/spectrum.html')[0] == 404       # standalone spectrum moved into /ctrl
        for path in ('/', '/api/config', '/index.html', '/ws'):        # no CamillaGUI behind it any more
            assert request(server, path)[0] == 404
        assert request(server, '/api/config', 'POST', b'x')[0] == 404
        assert request(server, '/_hifimate/spectrum-overlay.js', 'POST', b'x')[0] == 405
        assert request(server, '/_hifimate/health', headers={'Origin': 'http://other.invalid'})[0] == 403
        assert service.clients == 0 and service.analyses == 0


def test_sse_disconnect_releases_worker_and_api_failure_is_independent(tmp_path):
    service = Spectrum()
    with running(Server(('127.0.0.1', 0), ROOT / 'gui', service)) as proxy:
        with socket.create_connection(proxy.server_address, timeout=3) as client:
            client.sendall(b'GET /_hifimate/spectrum HTTP/1.1\r\nHost: localhost\r\n\r\n')
            data = bytearray()
            while b'data: ' not in data:
                data.extend(client.recv(8192))
            assert b'text/event-stream' in data
            assert service.clients == 1
        until(lambda: service.clients == 0 and service.stop is None)
        assert service.analyses == 0
        assert request(proxy, '/_hifimate/health')[0] == 200


def test_sse_frequency_scale_query_selects_backend_bands(tmp_path):
    service = Spectrum()
    with running(Server(('127.0.0.1', 0), ROOT / 'gui', service)) as proxy:
        assert request(proxy, '/_hifimate/spectrum?frequencyScale=bogus')[0] == 400
        assert service.clients == 0
        with socket.create_connection(proxy.server_address, timeout=3) as client:
            client.sendall(b'GET /_hifimate/spectrum?frequencyScale=bark HTTP/1.1\r\nHost: localhost\r\n\r\n')
            data = bytearray()
            while b'\n\n' not in data.split(b'data: ', 1)[-1] or b'data: ' not in data:
                data.extend(client.recv(65536))
            frame = json.loads(data.split(b'data: ', 1)[1].split(b'\n\n', 1)[0])
            assert frame['frequencyScale'] == 'bark'
            assert len(frame['bins']) == 240
            assert service.views['bark'] == 1
        until(lambda: service.clients == 0 and service.stop is None)
        assert service.views['bark'] == 0
