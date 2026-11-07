"""Private local format event endpoint. No observations create playback intent."""
import json
import array
import os
import socket
import struct
import signal
import subprocess
import threading
import queue
import select
from .receiver_events import ReceiverEvents
from pathlib import Path
from .formats import LinuxFormatController, SafetyError

def worker_loop(work, stop, fatal, controller, bridge, observation_pending, sessions):
    """All work, including queue timeout, shares the same fatal boundary."""
    while not stop.is_set():
        try:
            try: event=work.get(timeout=.25)
            except queue.Empty:
                with controller.lock: bridge.tick()
                continue
            if event is None: observation_pending.clear()
            with controller.lock:
                if event:
                    if event['event']=='play' and event.get('session') and not sessions.authorized(event): continue
                    # A manual reissue may revive a still-flowing session after
                    # this stop was observed but before its queued work ran.
                    if event['event']=='stop' and event.get('session') and sessions.current(event): continue
                    bridge.request(event['source'],event['event'],event['event_id'],
                                   event.get('device'),event.get('session'),event.get('remote'))
                bridge.update()
        except Exception as error:
            controller.error=str(error)
            fatal.append('format worker: '+str(error)); stop.set()
            try: controller.mute()
            except Exception as silence_error: fatal.append('silence failed: '+str(silence_error))
            return

def raise_if_fatal(fatal):
    if fatal: raise RuntimeError('; '.join(fatal))


def serve(config):
    from .runtime_paths import RuntimePaths
    path = Path(config['socket'].replace('%(uid)', str(os.geteuid())))
    resources=RuntimePaths(path).acquire()
    try:
        return _serve(config,path,resources)
    finally:
        resources.close()

def _serve(config,path,resources):
    from contextlib import ExitStack
    with ExitStack() as cleanup:
        return _run(config,path,resources,cleanup)

def _run(config,path,resources,cleanup):
    controller = LinuxFormatController(config)
    def silence():
        try: controller.idle()
        except Exception: pass
    cleanup.callback(silence)
    controller.mute()
    from .remote_pause import RemotePause, Sessions
    sessions=Sessions()
    pauser=RemotePause(sessions); airplay_decoders=[]
    def pause(event):
        result=pauser(event)
        # Tell the AirPlay decoder which connection HiFi paused, so that the
        # sender's later resume (prsm on the same connection) is a new request.
        if event['source']=='airplay' and result.get('status') in ('confirmed','accepted'):
            for decoder in airplay_decoders: decoder.remote_paused(event.get('session'))
        return result
    bridge=ReceiverEvents(controller,pause=pause,current=sessions.authorized,
                          idle_timeout=config.get('idle_timeout_s',300))
    work=queue.Queue(maxsize=128)
    stop = threading.Event()
    controller.cancelled=stop
    fatal=[]
    def reap(process):
        if process.poll() is None: process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        if process.stdout: process.stdout.close()
    subscriber = subprocess.Popen(['pactl', 'subscribe'], stdout=subprocess.PIPE, text=True)
    cleanup.callback(reap,subscriber)
    observation_pending = threading.Event()
    def enqueue(item):
        if item is not None: sessions.observe(item)
        if item is None:
            if observation_pending.is_set(): return
            observation_pending.set()
        try: work.put_nowait(item)
        except queue.Full:
            fatal.append('receiver event queue full'); stop.set()
    def events():
        try:
            for event in subscriber.stdout:
                if stop.is_set(): break
                if any(' on '+kind+' #' in event for kind in ('sink-input','sink','source')): enqueue(None)
        finally:
            if not stop.is_set(): fatal.append('Pulse subscription ended')
            stop.set()  # systemd restarts after cleanup; never silently lose observations
    def worker():
        worker_loop(work,stop,fatal,controller,bridge,observation_pending,sessions)
    from .mpd import MPDSubscriber, PORTS
    def receiver_failure(error):
        fatal.append(str(error)); stop.set()
    mpd_subscribers=[]
    if config.get('mpd_events_enabled',True):
        for source in PORTS:
            listener=MPDSubscriber(source,enqueue,receiver_failure,stop)
            mpd_subscribers.append(listener); listener.start()
            cleanup.callback(listener.close)
    cleanup.callback(stop.set)
    bluetooth=None
    if config.get('bluetooth_enabled',True):
        from .bluetooth import BlueZSubscriber
        def bluez_failure(error):
            controller.error='BlueZ subscription failed: '+str(error)
            fatal.append(controller.error)
            stop.set()
        bluetooth=BlueZSubscriber(enqueue,bluez_failure)
        bluetooth.start()
        cleanup.callback(bluetooth.close)
        cleanup.callback(stop.set)
    metadata_fd=None
    if config.get('airplay_metadata_enabled',True):
        from .airplay import AirPlayEvents
        metadata_fd=resources.fifo()
        decoder=AirPlayEvents(enqueue); airplay_decoders.append(decoder)
        def metadata():
            try:
                while not stop.is_set():
                    if select.select([metadata_fd],[],[],.5)[0]: decoder.feed(os.read(metadata_fd,8192))
            except Exception as error:
                fatal.append(str(error)); stop.set()
        threading.Thread(target=metadata,daemon=True).start()
    pw_subscriber=subprocess.Popen(['pw-dump','--monitor'],stdout=subprocess.PIPE)
    cleanup.callback(reap,pw_subscriber)
    cleanup.callback(stop.set)
    def pw_events():
        try:
            from .pw_events import watch
            watch(pw_subscriber.stdout, controller, enqueue, stop, lambda: (bridge.source,bridge.device))
        except Exception as error:
            fatal.append(str(error))
        finally:
            if not stop.is_set(): fatal.append('PW subscription ended'); stop.set()
    threading.Thread(target=pw_events,daemon=True).start()
    thread = threading.Thread(target=events, daemon=True)
    thread.start()
    worker_thread=threading.Thread(target=worker,daemon=True)
    worker_thread.start()
    cleanup.callback(worker_thread.join)
    cleanup.callback(stop.set)
    def terminate(*_):
        stop.set()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    cleanup.callback(endpoint.close)
    endpoint.bind(str(path)); resources.bound(); os.chmod(path, 0o600); endpoint.listen(8); endpoint.settimeout(.5)
    notify = os.environ.get('NOTIFY_SOCKET')
    if notify:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
            ready.connect('\0' + notify[1:] if notify.startswith('@') else notify)
            ready.sendall(b'READY=1')
    try:
        while not stop.is_set():
            try: connection, _ = endpoint.accept()
            except socket.timeout: continue
            with connection:
                connection.settimeout(4)
                try:
                    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    if uid != os.geteuid(): raise PermissionError('wrong uid')
                    data, ancillary, flags, _ = connection.recvmsg(4096, 256)
                    for level, kind, payload in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            fds = array.array('i')
                            fds.frombytes(payload[:len(payload)-len(payload)%fds.itemsize])
                            for fd in fds: os.close(fd)
                    if ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC): raise ValueError('oversized event')
                    event = json.loads(data)
                    if event['action'] == 'status':
                        result=True
                    elif event['action'] == 'nowplaying':
                        # AirPlay metadata only lives here (the FIFO). The DACP
                        # credentials go to the same-uid caller (SO_PEERCRED above),
                        # which sends the commands itself: avahi/HTTP waits must
                        # never block this serial IPC loop.
                        decoder=airplay_decoders[0] if airplay_decoders else None
                        connection.send(json.dumps({'ok':True,'airplay':decoder.now_playing() if decoder else None,
                                                    'dacp':decoder.credentials_for_control() if decoder else None}).encode())
                        continue
                    elif event['action'] in ('room', 'filters'):
                        # room-correction.json or dsp-filters.yml changed (or the
                        # EQ page's A/B bypass): update the running DSP now.
                        if isinstance(event.get('bypass'), bool):
                            controller.user_bypass=event['bypass']
                        result=controller.apply_room()
                    elif event['action'] == 'switch':
                        with controller.lock:
                            result=bridge.switch(event.get('source'),sessions)
                            bridge.update()
                    elif event['action'] == 'receiver':
                        # Hooks without receiver sessions are observations only.
                        # Only trusted in-process subscriptions introduce sessions.
                        enqueue(None)
                        connection.send(b'{"ok":true,"queued":true}')
                        continue
                    elif event['action'] == 'select':
                        with controller.lock:
                            controller.select(event['identity'])
                            bridge.source=None; bridge.pending=None; bridge.intent=None; bridge.active=None
                        result = True
                    elif event['action'] == 'format':
                        result = controller.change(event['identity'],event['revision'],event['stream'],event['pulse_input'],event['serial'],event['lane'])
                    elif event['action'] == 'idle':
                        with controller.lock:
                            if event.get('identity') != controller.owner: raise ValueError('stale idle owner')
                            controller.idle(); bridge.source=None; bridge.pending=None; bridge.intent=None; bridge.active=None
                        result = True
                    else: raise ValueError('unknown action')
                    with controller.lock:
                        response={'ok':result,**controller.status(),'remote_pause':list(bridge.pause_results),
                                  'switchable':list(bridge.requests)}
                    connection.send(json.dumps(response).encode())
                except Exception as error:
                    if isinstance(error,SafetyError):
                        fatal.append('IPC safety failure: '+str(error)); stop.set()
                    try: connection.send(json.dumps({'ok':False,'error':str(error)}).encode())
                    except OSError: pass
    finally:
        stop.set()
        if bluetooth: bluetooth.close()
        for listener in mpd_subscribers: listener.close()
        reap(subscriber); reap(pw_subscriber)
        if metadata_fd is not None:
            pass  # RuntimePaths closes fd and retains the owned FIFO inode
        worker_thread.join()
        try: controller.idle()
        finally:
            endpoint.close()

    raise_if_fatal(fatal)
