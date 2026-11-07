#!/usr/bin/env python3
"""Exercise real format negotiation in a private PipeWire graph, without a DAC.

Requires pipewire, wireplumber, dbus-daemon, pacat, parec, pactl, pw-dump,
pw-metadata and pw-top. No host services or hardware monitors are used.
With --mpd, additionally tests MPD native PipeWire output with 24-bit WAVs.
"""
import json
import argparse
import socket
import wave
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from hifimate.formats import LinuxFormatController


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpd",action="store_true",help="exercise native MPD output using 24-bit WAV files")
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='hifimate-private-pw-') as directory:
        root = Path(directory)
        root.chmod(0o700)
        conf = root / 'pw.conf'
        conf.write_text('''context.properties = {
 core.daemon = true core.name = hifimate-test
 default.clock.rate = 48000
 default.clock.allowed-rates = [ 44100 48000 768000 ]
}
context.spa-libs = {
 audio.convert.* = audioconvert/libspa-audioconvert
 support.* = support/libspa-support
}
context.modules = [
 { name = libpipewire-module-protocol-native }
 { name = libpipewire-module-client-node }
 { name = libpipewire-module-metadata }
 { name = libpipewire-module-profiler }
 { name = libpipewire-module-spa-node-factory }
 { name = libpipewire-module-link-factory }
 { name = libpipewire-module-adapter }
 { name = libpipewire-module-access args = { access.force = unrestricted } }
 { name = libpipewire-module-protocol-pulse args = {
   server.address = [ "unix:''' + directory + '''/pulse-test" ]
 } }
]
context.objects = [
 { factory = metadata args = { metadata.name = default } }
 { factory = spa-node-factory args = {
   factory.name = support.node.driver node.name = Dummy-Driver priority.driver = 20000
 } }
]
''' + (ROOT / 'config/host-stack/pipewire/pipewire.conf.d/50-hifimate-inputs.conf').read_text().replace(
                'context.properties = {', 'context.properties = { core.daemon = true core.name = hifimate-test'))
        processes = []
        writers = []
        with (root / 'test.log').open('w+') as log:
            env = {**os.environ, 'XDG_RUNTIME_DIR': directory,
                   'PIPEWIRE_RUNTIME_DIR': directory, 'PIPEWIRE_REMOTE': 'hifimate-test',
                   'PULSE_SERVER': 'unix:' + directory + '/pulse-test',
                   'XDG_STATE_HOME': directory + '/state',
                   'XDG_CACHE_HOME': directory + '/cache'}

            def launch(args, dsp=False, **kwargs):
                # dsp=True: the same per-group rate forcing as hifimate-camilladsp.service.
                process = subprocess.Popen(args, env={**env, 'PULSE_PROP': 'node.force-rate=0'} if dsp else env,
                                           stderr=log, **kwargs)
                processes.append(process)
                return process

            def stop(process):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                processes.remove(process)

            def run(args):
                return subprocess.run(args, env=env, capture_output=True, text=True,
                                      timeout=5, check=True).stdout

            controller = LinuxFormatController({}, run=run, rpc=lambda *args: None)
            try:
                bus = launch(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                             stdout=subprocess.PIPE, text=True)
                env['DBUS_SESSION_BUS_ADDRESS'] = bus.stdout.readline().strip()
                launch(['pipewire', '-c', str(conf)], stdout=log)
                deadline = time.monotonic() + 5
                while not (root / 'pulse-test').exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('private PipeWire did not start')
                    time.sleep(.05)
                launch(['wireplumber', '--profile=policy'], stdout=log)
                time.sleep(.4)
                if args.mpd:
                    music=root/'music'; music.mkdir()
                    for rate in (44100,768000):
                        path=music/f'{rate}.wav'
                        with wave.open(str(path),'wb') as output:
                            output.setnchannels(2); output.setsampwidth(3); output.setframerate(rate)
                            output.writeframes(b'\0'*(rate*6*3))
                    with socket.socket() as reserve:
                        reserve.bind(('127.0.0.1',0)); port=reserve.getsockname()[1]
                    mpdconf=root/'mpd.conf'
                    mpdconf.write_text(f'music_directory "{music}"\ndb_file "{root}/db"\nbind_to_address "127.0.0.1"\nport "{port}"\naudio_output {{\n type "pipewire"\n name "isolated"\n target "dlna_in"\n}}\n')
                    launch(['mpd','--no-daemon',str(mpdconf)],stdout=log)
                    deadline=time.monotonic()+3
                    while True:
                        try: connection=socket.create_connection(('127.0.0.1',port),timeout=2);break
                        except OSError:
                            if time.monotonic()>deadline:raise
                            time.sleep(.05)
                    protocol=connection.makefile('rwb',buffering=0)
                    from contextlib import ExitStack
                    connections=ExitStack()
                    connections.callback(connection.close)
                    connections.callback(protocol.close)
                    assert protocol.readline().startswith(b'OK MPD')
                    def command(text):
                        protocol.write((text+'\n').encode()); lines=[]
                        while True:
                            line=protocol.readline().decode().strip();lines.append(line)
                            if line=='OK':return lines
                            if not line or line.startswith('ACK'):raise RuntimeError(lines)
                    command('update')
                    deadline=time.monotonic()+3
                    expected_files={f'file: {rate}.wav' for rate in (44100,768000)}
                    while True:
                        # listall may expose the first file while MPD is still
                        # scanning the second. Wait for the entire update.
                        status=command('status')
                        files=set(command('listall'))
                        if expected_files <= files and not any(x.startswith('updating_db:') for x in status):
                            break
                        if time.monotonic()>deadline:raise RuntimeError('MPD database update timed out')
                        time.sleep(.05)
                    for rate in (44100,768000,44100):
                        command('stop');command('clear');command(f'add "{rate}.wav"')
                        # Like hifimate-camilladsp: the DSP capture stream forces only its own group.
                        capture=launch(['parec','--raw','--format=float32le',f'--rate={rate}','--channels=2','--device=dlna_in.monitor'],stdout=subprocess.DEVNULL,dsp=True)
                        command('play')
                        deadline=time.monotonic()+6
                        while True:
                            inputs=json.loads(run(['pactl','--format=json','list','sink-inputs']))
                            active=[x for x in inputs if not x.get('corked',False)]
                            if len(active)==1:
                                spec=active[0]['sample_specification']
                                item=active[0];serial=item['properties']['object.serial']
                                nodes=json.loads(run(['pw-dump']))
                                matches=[n for n in nodes if str(n.get('info',{}).get('props',{}).get('object.serial'))==str(serial)]
                                if len(matches)==1:
                                    try:
                                        fmt=controller.observe(matches[0]['id'],serial,item['index'],'dlna_in')
                                        graph=controller.graph_rate('dlna_in')
                                        if fmt.rate==rate and graph==rate:
                                            print(json.dumps({'mpd_file_rate':rate,'received':spec,'graph_rate':graph}),flush=True);break
                                    except RuntimeError:pass
                            if time.monotonic()>deadline:raise RuntimeError(f'MPD format mismatch {rate}: {inputs}')
                            time.sleep(.05)
                        command('stop');stop(capture)
                    protocol.close();connection.close()
                else:
                    last_serial = None
                    for rate in (44100, 768000, 44100):
                        client = launch(['pacat', '--playback', '--raw', '--format=s24le',
                                         f'--rate={rate}', '--channels=2', '--device=dlna_in'],
                                        stdin=subprocess.PIPE)
                        done = threading.Event()

                        def feed(process=client, stopped=done):
                            try:
                                while not stopped.is_set():
                                    process.stdin.write(b'\0' * 6000)
                                    process.stdin.flush()
                            except (BrokenPipeError, ValueError):
                                pass

                        writer = threading.Thread(target=feed, daemon=True)
                        writers.append((done, writer))
                        writer.start()
                        capture = launch(['parec', '--raw', '--format=float32le',
                                          f'--rate={rate}', '--channels=2',
                                          '--device=dlna_in.monitor'], stdout=subprocess.DEVNULL, dsp=True)
                        deadline = time.monotonic() + 6
                        error = None
                        while True:
                            try:
                                sinks = {x['index']: x['name'] for x in json.loads(run(['pactl', '--format=json', 'list', 'sinks']))}
                                inputs = [x for x in json.loads(run(['pactl', '--format=json', 'list', 'sink-inputs']))
                                          if sinks.get(x.get('sink')) == 'dlna_in']
                                if len(inputs) != 1:
                                    raise RuntimeError('expected one private playback stream')
                                item = inputs[0]
                                serial = item['properties']['object.serial']
                                nodes = json.loads(run(['pw-dump']))
                                source = next(n for n in nodes if str(n.get('info', {}).get('props', {}).get('object.serial')) == str(serial))
                                fmt = controller.observe(source['id'], serial, item['index'], 'dlna_in')
                                graph = controller.graph_rate('dlna_in')
                                if fmt.rate != rate or fmt.bits != 24 or graph != rate:
                                    raise RuntimeError(f'format {fmt}, graph {graph}, requested {rate}')
                                if serial == last_serial:
                                    raise RuntimeError('stream serial reused')
                                break
                            except (RuntimeError, KeyError, StopIteration) as exc:
                                error = exc
                                if time.monotonic() >= deadline:
                                    raise RuntimeError(f'negotiation failed at {rate}: {error}') from exc
                                time.sleep(.05)
                        last_serial = serial
                        print(json.dumps({'requested': rate, 'source_rate': fmt.rate,
                                          'source_bits': fmt.bits, 'graph_rate': graph,
                                          'serial': serial, 'zero_pcm': True}))
                        done.set()
                        stop(client)
                        writer.join(timeout=3)
                        try:
                            client.stdin.close()
                        except BrokenPipeError:
                            pass
                        stop(capture)
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read()[-3000:], file=sys.stderr)
                raise
            finally:
                if 'connections' in locals(): connections.close()
                for done, _ in writers:
                    done.set()
                for process in reversed(processes[:]):
                    stop(process)
                    if process.stdout and process.stdout is not log:
                        process.stdout.close()
                    if process.stdin:
                        try:
                            process.stdin.close()
                        except BrokenPipeError:
                            pass
                for _, writer in writers:
                    writer.join(timeout=3)
        print('PASS: private graph 44100 -> 768000 -> 44100; no DAC opened')


if __name__ == '__main__':
    main()
