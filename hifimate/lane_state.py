"""Read-only lane evidence shared by the controller and control page."""
import re
from .receiver_events import LANES


def lane_states(inputs, sinks, nodes):
    result={}
    for source,lane in LANES.items():
        sink=next((s for s in sinks if s.get('name')==lane),None)
        streams=[i for i in inputs if sink and i.get('sink')==sink.get('index')
                 and i.get('corked',False) in (False,'false')]
        if source=='bluetooth':
            streams += [n for n in nodes if n.get('info',{}).get('state')=='running'
                        and n['info'].get('props',{}).get('media.class')=='Audio/Source'
                        and n['info']['props'].get('node.name','').startswith('bluez_input.')]
        item=streams[0] if len(streams)==1 else {}
        props=item.get('properties',item.get('info',{}).get('props',{}))
        fmt=re.search(r'(\w+)\s+(\d+)ch\s+(\d+)Hz',str(item.get('sample_specification','')))
        formats=item.get('info',{}).get('params',{}).get('Format',[])
        native=formats[0] if formats and isinstance(formats[0],dict) else {}
        result[source]={'source':source,'lane':lane,'has_data':bool(streams),'count':len(streams),
                        'rate':int(fmt[3]) if fmt else native.get('rate'),
                        'container':fmt[1] if fmt else native.get('format'),
                        # Bluetooth streams report media.name "(null)".
                        'name':next((str(props[k]) for k in ('device.description','media.name','node.description')
                                     if props.get(k) and props[k]!='(null)'),'')[:160],
                        'codec':str(props.get('api.bluez5.codec',''))[:40]}
    return result
