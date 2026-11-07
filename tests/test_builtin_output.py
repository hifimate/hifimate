from conftest import record_file
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from hifimate.output_devices import enumerate_outputs, device_capabilities, choose, switch
from test_output_devices import card, installed
from test_formats import fixture

CODEC = '''Codec: Cirrus Logic CS4208
Vendor Id: 0x10134208
Default PCM:
    rates [0xff]: 768000
Node 0x02 [Audio Output] wcaps 0x1: Stereo
  Device: name="CS4208 Analog", type="Audio", device=0
  PCM:
    rates [0x7f0]: 32000 44100 48000 88200 96000 176400 192000
    bits [0x1e]: 16 20 24 32
    formats [0x3]: PCM FLOAT
Node 0x03 [Audio Output] wcaps 0x1: Stereo
  Control: name="Speaker Playback Volume", index=0, device=0
  Control: name="Speaker Playback Switch", index=0, device=0
  PCM:
    rates [0x7f0]: 32000 44100 48000 88200 96000 176400 192000
    bits [0x1e]: 16 20 24 32
    formats [0x3]: PCM FLOAT
'''


def builtin(proc,codec=CODEC,info=None):
    p=proc/'card0';(p/'pcm0p/sub0').mkdir(parents=True,exist_ok=True)
    (p/'id').write_text('PCH')
    (p/'pcm0p/info').write_text(info or 'device: 0\nstream: PLAYBACK\nid: CS4208 Analog\n')
    (p/'codec#0').write_text(codec)
    return p


def test_priority_and_manual_choice(tmp_path,monkeypatch):
    builtin(tmp_path)
    row=enumerate_outputs(tmp_path)[0]
    assert row['sourcekind']=='builtin' and not row['error']
    assert row['label']=='本机 Cirrus Logic CS4208 / CS4208 Analog'
    assert max(row['rates'])==192000
    assert choose(tmp_path)=='hw:CARD=PCH,DEV=0'
    card(tmp_path,1,'USB')
    assert device_capabilities('auto',tmp_path)[0]=='hw:CARD=USB,DEV=0'
    assert choose(tmp_path)=='hw:CARD=USB,DEV=0'
    card(tmp_path,2,'USB2')
    with pytest.raises(RuntimeError): choose(tmp_path)
    monkeypatch.setattr('sys.stdin.isatty',lambda:True)
    monkeypatch.setattr('builtins.input',lambda:'1')
    assert choose(tmp_path,interactive=True)=='hw:CARD=PCH,DEV=0'
    assert device_capabilities('hw:CARD=PCH,DEV=0',tmp_path)[0]=='hw:CARD=PCH,DEV=0'
    with pytest.raises(RuntimeError): device_capabilities('hw:CARD=Unplugged,DEV=0',tmp_path)


@pytest.mark.parametrize('codec,info',[
    (CODEC.replace('CS4208','Unknown'),None),
    (CODEC.replace('Stereo','Mono'),None),
    (CODEC.replace('16 20 24 32','16 20 24'),None),
    (CODEC.replace('PCM FLOAT','FLOAT'),None),
    (CODEC.replace('device=0','device=01'),None),
    (CODEC.replace('device=0','device=02'),None),
    (CODEC.replace('32000 44100 48000 88200 96000 176400 192000','32000'),None),
    (CODEC,'device: 0\nstream: CAPTURE\nid: CS4208 Analog\n'),
    (CODEC,'device: 0\nstream: PLAYBACK\nid: HDMI\n'),
])
def test_strict_rejections(tmp_path,codec,info):
    builtin(tmp_path,codec,info)
    assert enumerate_outputs(tmp_path)[0]['error']
    with pytest.raises(RuntimeError): device_capabilities('hw:CARD=PCH,DEV=0',tmp_path)


def test_converter_intersection(tmp_path):
    text=CODEC.replace('32000 44100 48000 88200 96000 176400 192000','44100 48000',1)
    builtin(tmp_path,text)
    assert device_capabilities('auto',tmp_path)[2]=={44100,48000}


def test_real_controller_builtin_rates(tmp_path):
    controller,state,calls=fixture(tmp_path)
    # Replace USB fixture with the supported built-in on same fake ALSA card.
    usb=tmp_path/'card1';(usb/'stream0').unlink();(usb/'id').write_text('PCH')
    analog=builtin(tmp_path)
    # Preserve fixture hw_params writer by using its existing card path.
    for name in ('codec#0','pcm0p/info'):
        (usb/name).write_text((analog/name).read_text())
    import shutil
    shutil.rmtree(analog)
    controller.config['alsa_device']='hw:CARD=PCH,DEV=0'
    controller.select('owner')
    for revision,rate in enumerate((44100,192000),1):
        state['source']=rate
        assert controller.change('owner',revision,42,7,99,'lane')
        assert controller.ready
    state['source']=768000
    with pytest.raises(RuntimeError,match='unsupported DAC rate'):
        controller.change('owner',3,42,7,99,'lane')
    assert state['config']['devices']['playback']['device']=='hw:CARD=PCH,DEV=0'


def test_install_switch_rollback_uninstall(tmp_path,monkeypatch):
    import subprocess
    root=Path(__file__).parents[1];prefix=tmp_path/'install'
    def install(*flags):
        return subprocess.run([str(root/'scripts/install.sh'),'--prefix',str(prefix),*flags],capture_output=True,text=True)
    assert install('--apply').returncode==0
    proc=tmp_path/'proc';builtin(proc);card(proc,1,'USB')
    conf=prefix/'config/hifimate.conf';data=json.loads(conf.read_text());data['proc_asound']=str(proc);conf.write_text(json.dumps(data))
    record_file(prefix, 'config/hifimate.conf')
    result=install('--apply','--output-device','hw:CARD=PCH,DEV=0')
    assert result.returncode==0,result.stderr
    assert json.loads(conf.read_text())['alsa_device']=='hw:CARD=PCH,DEV=0'
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'xdg'))
    offline=lambda *a,**k: SimpleNamespace(returncode=1,stderr='Failed to connect to bus',stdout='')
    switch(prefix,'hw:CARD=USB,DEV=0',True,offline)
    before=conf.read_bytes();calls=[]
    def active(args,**kwargs):
        calls.append(args)
        if 'restart' in args and sum('restart' in x for x in calls)==1:
            raise RuntimeError('fake restart failure')
        return SimpleNamespace(returncode=0,stderr='',stdout=f'FragmentPath={prefix}/systemd/hifimate.service\nExecStart={{ path={prefix}/bin/hifimate-runner ; }}\nActiveState=active\nLoadState=loaded')
    with pytest.raises(RuntimeError): switch(prefix,'hw:CARD=PCH,DEV=0',True,active)
    assert conf.read_bytes()==before
    switch(prefix,'hw:CARD=PCH,DEV=0',True,offline)
    result=subprocess.run([str(root/'scripts/uninstall.sh'),'--prefix',str(prefix),'--apply'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert not conf.exists()


@pytest.mark.parametrize('model,cardid,dev,rate', [('Realtek ALC999','InternalAudio',4,384000),('Another Codec','Studio',12,96000)])
def test_dynamic_codec_pcm_and_controller(tmp_path,model,cardid,dev,rate):
    c,state,calls=fixture(tmp_path)
    (tmp_path/'card1/pcm0p/info').unlink()
    p=tmp_path/'card1';(p/'stream0').unlink();(p/'id').write_text(cardid)
    (p/'codec#2').write_text(CODEC.replace('Cirrus Logic CS4208',model).replace('0x10134208','0xdeadbeef').replace('CS4208 Analog',model+' Analog').replace('device=0',f'device={dev}').replace('192000',str(rate)))
    target=p/f'pcm{dev}p';target.mkdir(exist_ok=True)
    (target/'info').write_text(f'device: {dev}\nstream: PLAYBACK\nid: {model} Analog\n')
    # Fake runner writes original path; symlink only inside test makes readback path equivalent.
    (target/'sub0').symlink_to(p/'pcm0p/sub0',target_is_directory=True)
    device,hw,rates=device_capabilities('auto',tmp_path)
    assert device==f'hw:CARD={cardid},DEV={dev}' and hw==target/'sub0/hw_params'
    assert rate in rates and enumerate_outputs(tmp_path)[0]['label'].startswith('本机 '+model)
    c.config['alsa_device']=device;c.select('owner');state['source']=rate
    assert c.change('owner',1,42,7,99,'lane')
    assert state['config']['devices']['playback']['device']==device


def test_missing_explicit_caps_and_hdmi(tmp_path):
    p=builtin(tmp_path,CODEC.replace('  PCM:\n','  Inherited:\n'))
    assert enumerate_outputs(tmp_path)[0]['error']
    (p/'codec#0').write_text(CODEC.replace('CS4208 Analog','HDMI Analog'))
    (p/'pcm0p/info').write_text('device: 0\nstream: PLAYBACK\nid: HDMI Analog\n')
    assert enumerate_outputs(tmp_path)[0]['error']


def test_multiple_local_pcms_same_card(tmp_path):
    p=builtin(tmp_path)
    second=p/'pcm6p';second.mkdir()
    (second/'info').write_text('device: 6\nstream: PLAYBACK\nid: Other Analog\n')
    extra=CODEC.replace('device=0','device=6').replace('CS4208 Analog','Other Analog').split('Node 0x02',1)[1]
    (p/'codec#0').write_text(CODEC+'Node 0x12'+extra.replace('Node 0x03','Node 0x13'))
    rows=enumerate_outputs(tmp_path)
    assert {r['device'] for r in rows}=={'hw:CARD=PCH,DEV=0','hw:CARD=PCH,DEV=6'}
    assert all(not r['error'] for r in rows)
    with pytest.raises(RuntimeError): device_capabilities('auto',tmp_path)
    assert device_capabilities('hw:CARD=PCH,DEV=6',tmp_path)[1]==second/'sub0/hw_params'


def test_conflicting_speaker_control_cannot_change_other_pcm_rates(tmp_path):
    conflicting='''Node 0x19 [Audio Output] wcaps 0x1: Stereo
  Device: name="Other Analog", type="Audio", device=7
  Control: name="Speaker Playback Volume", index=0, device=0
  PCM:
    rates [0x40]: 48000
    bits [0x1e]: 16 24 32
    formats [0x1]: PCM
'''
    builtin(tmp_path,CODEC+conflicting)
    assert 192000 in device_capabilities('hw:CARD=PCH,DEV=0',tmp_path)[2]
