"""BlueZ explicit Status edges; snapshots and connection alone never claim audio."""
import asyncio
import threading
import uuid

PLAYER='org.bluez.MediaPlayer1'
DEVICE='org.bluez.Device1'
def plain(value):
    return getattr(value, 'value', value)

class BlueZEvents:
    def __init__(self, emit):
        self.emit=emit; self.players={}; self.sessions={}; self.devices={}; self.session_players={}
    def baseline(self, objects):
        for path, interfaces in objects.items():
            if DEVICE in interfaces: self.devices[path]=bool(plain(interfaces[DEVICE].get('Connected',False)))
            if PLAYER in interfaces:
                props=interfaces[PLAYER]
                self.players[path]=(str(plain(props.get('Device',path.rsplit('/player',1)[0]))),str(plain(props.get('Status',''))))
    def _send(self, action, device, session, player=None):
        self.emit({'source':'bluetooth','event':action,'event_id':uuid.uuid4().hex,
                   'device':device,'session':session,'remote':{'player':player}})
    def properties(self, path, interface, changed, invalidated=()):
        if interface==DEVICE:
            if 'Connected' in changed:
                connected=bool(plain(changed['Connected'])); self.devices[path]=connected
                if not connected: self.disconnect(path)
            return
        if interface!=PLAYER: return
        old=self.players.get(path)
        device=str(plain(changed.get('Device',old[0] if old else path.rsplit('/player',1)[0])))
        if 'Status' not in changed: return
        status=str(plain(changed['Status'])); self.players[path]=(device,status)
        if status=='playing' and (old is None or old[1]!='playing'):
            session=uuid.uuid4().hex; self.sessions[device]=session
            self.session_players[device]=path
            self._send('play',device,session,path)
        elif status in ('paused','stopped') and self.session_players.get(device)==path:
            session=self.sessions.pop(device,None)
            self.session_players.pop(device,None)
            if session: self._send('stop',device,session)
    def disconnect(self,device):
        session=self.sessions.pop(device,None)
        self.session_players.pop(device,None)
        if session: self._send('stop',device,session)
        for path,(dev,status) in list(self.players.items()):
            if dev==device: self.players[path]=(dev,'disconnected')
    def removed(self,path,interfaces):
        if DEVICE in interfaces: self.disconnect(path)
        if PLAYER in interfaces:
            player=self.players.pop(path,None)
            if player and self.session_players.get(player[0])==path: self.disconnect(player[0])

class BlueZSubscriber:
    """Own one system bus subscription; any failure is fatal to the audio host."""
    def __init__(self,emit,fail):
        self.events=BlueZEvents(emit); self.fail=fail; self.loop=None; self.bus=None
        # Serialize loop shutdown with close().  The loop can finish between
        # a caller's liveness check and call_soon_threadsafe(), which raises
        # when asyncio has already closed it during service teardown.
        self._loop_lock=threading.Lock()
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.stopping=False
    def start(self): self.thread.start()
    async def _listen(self):
        from dbus_next.aio import MessageBus
        from dbus_next import BusType, Message, MessageType
        self.bus=await MessageBus(bus_type=BusType.SYSTEM).connect()
        async def call(message):
            response=await self.bus.call(message)
            if response.message_type==MessageType.ERROR: raise RuntimeError(str(response.body))
            return response.body
        # Resolve unique name so an unrelated bus sender cannot manufacture edges.
        owner=(await call(Message(destination='org.freedesktop.DBus',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus',member='GetNameOwner',signature='s',body=['org.bluez'])))[0]
        def handler(msg):
            if msg.message_type!=MessageType.SIGNAL: return
            if msg.interface=='org.freedesktop.DBus' and msg.member=='NameOwnerChanged' and msg.body[0]=='org.bluez':
                if msg.body[2]!=owner: self.fail(RuntimeError('BlueZ owner changed'))
                return
            if msg.sender!=owner: return
            try:
                if msg.interface=='org.freedesktop.DBus.Properties' and msg.member=='PropertiesChanged':
                    self.events.properties(msg.path,*msg.body)
                elif msg.interface=='org.freedesktop.DBus.ObjectManager' and msg.member=='InterfacesRemoved':
                    self.events.removed(*msg.body)
                # InterfacesAdded is only baseline information, never Play.
                elif msg.interface=='org.freedesktop.DBus.ObjectManager' and msg.member=='InterfacesAdded':
                    self.events.baseline({msg.body[0]:msg.body[1]})
            except Exception as error: self.fail(error)
        # Queue startup signals until baseline arrives; preserve real edges in order.
        pending=[]
        def buffering(msg): pending.append(msg)
        self.bus.add_message_handler(buffering)
        for rule in ("type='signal',sender='org.bluez'", "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"):
            await call(Message(destination='org.freedesktop.DBus',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus',member='AddMatch',signature='s',body=[rule]))
        objects=(await call(Message(destination='org.bluez',path='/',interface='org.freedesktop.DBus.ObjectManager',member='GetManagedObjects')))[0]
        self.events.baseline(objects)
        self.bus.remove_message_handler(buffering); self.bus.add_message_handler(handler)
        for msg in pending: handler(msg)
        await self.bus.wait_for_disconnect()
        if not self.stopping: raise RuntimeError('BlueZ bus disconnected')
    def _run(self):
        try:
            self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._listen())
        except Exception as error:
            if not self.stopping: self.fail(error)
        finally:
            with self._loop_lock:
                if self.bus: self.bus.disconnect()
                if self.loop and not self.loop.is_closed(): self.loop.close()
    def close(self):
        self.stopping=True
        with self._loop_lock:
            if self.loop and self.bus and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(self.bus.disconnect)
                except RuntimeError:
                    # The loop may close immediately after is_closed(); the
                    # worker's finally block still disconnects the bus.
                    pass
        self.thread.join(timeout=5)
