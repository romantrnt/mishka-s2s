"""Distinguish received hardware evidence from normalized/admitted events."""
import asyncio
import json
import pytest
from app.foundation.input_adapters import KeyboardHIDInputAdapter
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.config import Settings


def test_raw_known_edges_include_suppressed_duplicate_and_repeat_not_arbitrary_keys():
    emitted=[]
    adapter=KeyboardHIDInputAdapter('/not-opened',emitted.append)
    adapter.feed_key(4,1,timestamp=10.)
    adapter.feed_key(4,1,timestamp=10.001)
    adapter.feed_key(4,2,timestamp=10.002)
    adapter.feed_key(4,0,timestamp=10.003)
    adapter.feed_key(30,1,timestamp=10.004)
    diag=adapter.diagnostics()
    assert diag['raw_key_events']==4 and diag['events']==2
    assert [event.type for event in emitted]==['ButtonPressed','ButtonReleased']
    assert diag['last_raw_key']==dict(kind='release',control='button',button_id=3,at_monotonic=10.003)
    assert diag['pressed_buttons']==[]


def test_unverified_hid_evidence_does_not_become_authority():
    emitted=[];adapter=KeyboardHIDInputAdapter('/not-opened',emitted.append)
    adapter._unknown_level_blocked=True
    adapter.feed_key(57,1,timestamp=20.)
    diag=adapter.diagnostics()
    assert diag['raw_key_events']==1 and diag['events']==0
    assert diag['unverified_events']==1 and not emitted and not adapter._pressed


@pytest.mark.parametrize('held',[1,2,3,4,5,6,7])
def test_initial_level_projection_uses_button_ids_not_linux_key_codes(monkeypatch,held):
    adapter=KeyboardHIDInputAdapter('/not-opened',lambda _:None)
    adapter._fd=123
    def ioctl(fd,request,bitmap,mutate):
        code=next(code for code,button in adapter.KEY_CODES.items() if button==held)
        bitmap[code//8]|=1<<(code%8)
    monkeypatch.setattr('app.foundation.input_adapters.fcntl.ioctl',ioctl)
    adapter._sync_initial_levels()
    assert adapter.diagnostics()['initial_level_sync']['pressed_buttons']==[held]


@pytest.mark.asyncio
async def test_admission_rejection_still_exposes_received_hid_and_hangup_passes():
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    pending=[]
    adapter=KeyboardHIDInputAdapter('/not-opened',lambda event:pending.append(asyncio.create_task(stand.inject_physical({'type':event.type,**event.payload}))))
    stand.input_adapter=adapter
    stand._admits_physical_users=lambda:True
    try:
        adapter.feed_key(57,0,timestamp=29.)
        await asyncio.gather(*pending)
        assert stand.application.last_canonical_state['handset']=='up'
        pending.clear()
        stand._admits_physical_users=lambda:False
        adapter.feed_key(4,1,timestamp=30.)
        adapter.feed_key(4,0,timestamp=30.1)
        rejected=await asyncio.gather(*pending)
        assert all(result['reason']=='runtime_unavailable' for result in rejected)
        before=adapter.diagnostics().copy()
        for output in (stand.panel_snapshot(),stand.diagnostics_snapshot()):
            info=output['input_hardware']
            assert info['raw_key_events']==3 and info['events']==3
            assert info['pressed_buttons']==[] and info['space_pressed'] is False
            assert info['last_raw_key']['kind']=='release'
            assert info['level_source']=='last_observed_hid'
        assert adapter.diagnostics()==before
        pending.clear()
        adapter.feed_key(57,1,timestamp=31.)
        accepted=await asyncio.gather(*pending)
        assert accepted[0]['reason']!='runtime_unavailable'
        assert stand.application.last_canonical_state['handset']=='down'
    finally:
        await stand.aclose()


@pytest.mark.parametrize('bad',[None,[],{},'PRIVATE_CHILD_CONTEXT',True,42])
def test_malformed_private_fields_cannot_escape_input_projection(bad):
    value={'status':bad,'pressed_buttons':[bad],'last_raw_key':{'kind':bad,'control':bad,'button_id':bad,'at_monotonic':bad},
        'ready':bad,'reader_alive':bad,'events':bad,'device_path':'PRIVATE_PATH','last_error':{'detail':'PRIVATE_TOKEN'}}
    result=HardwareIntegrationStand._safe_input_diagnostics(value)
    serialized=json.dumps(result)
    assert 'PRIVATE_' not in serialized
    assert 'device_path' not in result and 'last_error' not in result


@pytest.mark.parametrize('at',[float('nan'),float('inf'),-1.,True,'PRIVATE_TIME',10**400,-(10**400)])
def test_invalid_timestamps_are_not_promoted(at):
    info=HardwareIntegrationStand._safe_input_diagnostics({'last_raw_key':{'kind':'press','control':'handset','at_monotonic':at}})
    assert info['last_raw_key']=={'kind':'press','control':'handset'}
