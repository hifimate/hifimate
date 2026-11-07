from hifimate.airplay import AirPlayEvents

def test_explicit_resume_not_buffer_recovery():
    events=[]; a=AirPlayEvents(events.append)
    def send(code): a.feed(f'<item><type>73736e63</type><code>{code.encode().hex()}</code></item>'.encode())
    for code in ('prsm','pfls'): send(code)
    assert not events
    send('pbeg'); send('pbeg'); send('pfls'); assert len(events)==1
    send('paus'); old=events[-1]['session']; send('pres')
    assert [e['event'] for e in events]==['play','stop','play']
    assert events[-1]['session']!=old

def test_fragmented_whitespace_payload_and_stale_connection_stop():
    import base64
    events=[]; a=AirPlayEvents(events.append)
    def frame(code,token):
        payload=base64.b64encode(token.encode()).decode()
        return f'<item><type>73736e63</type><code>{code.encode().hex()}</code><length>{len(token)}</length><data encoding="base64">\n {payload}\n</data></item>'.encode()
    for code,token in [('pbeg','1'),('pbeg','2'),('pend','1'),('pbeg','2')]:
        data=frame(code,token)
        for i in range(0,len(data),7): a.feed(data[i:i+7])
    assert [e['event'] for e in events]==['play','play']
    a.feed(frame('pend','2')); assert events[-1]['event']=='stop'
    a.feed(frame('pbeg','1')); assert len(events)==3

def test_irrelevant_bad_metadata_and_bad_length_are_ignored():
    events=[]; a=AirPlayEvents(events.append)
    a.feed(b'<item><type>636f7265</type><code>61736172</code><data encoding="base64">%%%bad</data></item>')
    a.feed(b'<item><type>73736e63</type><code>70626567</code><length>9</length><data encoding="base64">MQ==</data></item>')
    assert not events
    a.feed(b'<item>'+b'x'*70000+b'</item>')
    a.feed(b'<item><type>73736e63</type><code>70626567</code></item>')
    assert len(events)==1


def test_reference_token_begin_payloadless_pause_resume():
    events=[]; decoder=AirPlayEvents(events.append)
    decoder.feed(b'<item><type>73736e63</type><code>70626567</code><length>1</length><data encoding="base64">MQ==</data></item>')
    session=events[-1]['session']
    for code in ('paus','pres'):
        decoder.feed(f'<item><type>73736e63</type><code>{code.encode().hex()}</code><length>0</length></item>'.encode())
    assert [event['event'] for event in events]==['play','stop','play']
    assert events[-1]['session'] != session
    assert decoder.token=='1'
    # A payloadless connection-end does not end a tokenized connection.
    decoder.feed(b'<item><type>73736e63</type><code>70656e64</code><length>0</length></item>')
    assert len(events)==3


def test_dacp_metadata_is_snapshotted_per_play_session_and_not_inherited():
    import base64
    events=[]; decoder=AirPlayEvents(events.append)
    def send(code,value=''):
        payload=base64.b64encode(value.encode()).decode()
        decoder.feed(f'<item><type>73736e63</type><code>{code.encode().hex()}</code><length>{len(value)}</length><data encoding="base64">{payload}</data></item>'.encode())
    for code,value in [('acre','987'),('daid','ABC123'),('clip','192.0.2.3'),('pbeg','1')]: send(code,value)
    assert events[-1]['remote']=={'acre':'987','daid':'ABC123','clip':'192.0.2.3'}
    send('paus'); send('pres'); assert events[-1]['remote']['acre']=='987'
    # New connection lacks DACP support. Old credentials must not be reused.
    send('clip','192.0.2.4'); send('pbeg','2')
    assert events[-1]['remote']=={'clip':'192.0.2.4'}
    assert events[0]['remote']['clip']=='192.0.2.3'


def test_dacp_metadata_rejects_injected_headers_and_unknown_peer():
    import base64
    events=[]; decoder=AirPlayEvents(events.append)
    for code,value in [('acre','123\r\nX:evil'),('daid','not-hex'),('clip','attacker.example')]:
        payload=base64.b64encode(value.encode()).decode()
        decoder.feed(f'<item><type>73736e63</type><code>{code.encode().hex()}</code><data encoding="base64">{payload}</data></item>'.encode())
    decoder.feed(b'<item><type>73736e63</type><code>70626567</code></item>')
    assert events[-1]['remote']=={}


def test_new_connection_after_a_sender_crash_claims_again():
    # Shairport crashed mid-session (no pend), restarted, and a new sender
    # connected. HiFi holds the FIFO read-write, so it sees no EOF; the new
    # connection's identity metadata must still let its pbeg start a session.
    events=[]; a=AirPlayEvents(events.append)
    def send(code,value=None):
        data=''
        if value is not None:
            import base64
            data=f'<length>{len(value)}</length><data encoding="base64">{base64.b64encode(value.encode()).decode()}</data>'
        a.feed(f'<item><type>73736e63</type><code>{code.encode().hex()}</code>{data}</item>'.encode())
    send('clip','192.0.2.3'); send('pbeg')
    first=events[-1]
    send('pbeg')  # a duplicate begin within the same connection never re-claims
    assert len(events)==1
    send('clip','192.0.2.9'); send('daid','ABC123'); send('acre','42'); send('pbeg')
    assert [e['event'] for e in events]==['play','play']
    assert events[-1]['session']!=first['session']
    assert events[-1]['remote']=={'clip':'192.0.2.9','daid':'ABC123','acre':'42'}


def test_resume_after_hifi_paused_the_sender_claims_but_buffer_recovery_does_not():
    events=[]; a=AirPlayEvents(events.append)
    def send(code): a.feed(f'<item><type>73736e63</type><code>{code.encode().hex()}</code></item>'.encode())
    send('pbeg'); first=events[-1]['session']
    # Another source took over; the sender kept its connection. Flush/resume
    # without a HiFi pause is buffer recovery and never steals the output back.
    send('pfls'); send('prsm'); assert len(events)==1
    a.remote_paused('some-other-session'); send('pfls'); send('prsm'); assert len(events)==1
    # HiFi paused this connection via DACP; the user then resumes on the sender.
    a.remote_paused(first); send('pfls'); send('prsm')
    assert [e['event'] for e in events]==['play','play'] and events[-1]['session']!=first
    second=events[-1]['session']
    send('pfls'); send('prsm'); assert len(events)==2  # one resume per HiFi pause
    a.remote_paused(second); send('pend'); send('prsm'); assert events[-1]['event']=='stop' and len(events)==3
