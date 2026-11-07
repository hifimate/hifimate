"""Filter the PW JSON change stream without reacting to our probe clients."""
import json
import codecs


def watch(stream, controller, emit, stopped, requested=lambda: (None,None)):
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder('utf-8')()
    pending = ''
    seen = {}
    while not stopped.is_set():
        data = stream.read1(65536)
        if not data:
            return
        pending += utf8.decode(data)
        if len(pending) > 16 * 1024 * 1024:
            raise RuntimeError('PipeWire event frame exceeds limit')
        while pending.strip():
            pending = pending.lstrip()
            try:
                objects, end = decoder.raw_decode(pending)
            except json.JSONDecodeError:
                break
            pending = pending[end:]
            if not isinstance(objects, list):
                raise ValueError('PipeWire event is not a list')
            for obj in objects:
                node_id = obj.get('id')
                info = obj.get('info')
                if info is None:
                    old=seen.pop(node_id, {})
                    if controller.binding and (controller.binding[0] == node_id or old.get('node.name')==controller.binding[3]):
                        emit(None)
                    continue
                if obj.get('type') != 'PipeWire:Interface:Node':
                    continue
                previous = seen.get(node_id, {})
                key={**previous, **info.get('props', {})}
                if 'state' in info: key['state']=info['state']
                if 'Format' in info.get('params', {}): key['Format']=info['params']['Format']
                seen[node_id] = key
                if previous == key or not (controller.owner or requested()[0]):
                    continue
                binding = controller.binding
                source,device=requested()
                name=key.get('node.name','')
                address=(device or '').rsplit('/dev_',1)[-1].lower()
                native=(source=='bluetooth' and address and name.startswith('bluez_input.') and address in name.lower())
                # Pending Pulse negotiation needs output-stream Format changes;
                # source-output/capture nodes and unrelated sources cannot wake
                # the controller after each DSP restart.
                if (binding and (node_id == binding[0] or name == binding[3])
                        or (not binding or getattr(controller,'candidate',None)) and key.get('media.class')=='Stream/Output/Audio'
                        or native):
                    emit(None)
