"""SPACE on-hook is a safety revocation, not a debounced action button."""
from unittest.mock import patch
import pytest
from app.foundation.input_adapters import KeyboardHIDInputAdapter as Adapter


def adapter(onhook=True):
    events=[]
    instance=Adapter('/dev/null',events.append)
    instance._known_levels.add(instance.KEY_SPACE)
    if onhook: instance._pressed.add(instance.KEY_SPACE)
    return instance,events


@pytest.mark.parametrize('interval',[0,.0175,.0344,.1])
def test_true_hangup_never_lost_after_lift(interval):
    a,events=adapter()
    a.feed_key(57,0,timestamp=1)
    a.feed_key(57,1,timestamp=1+interval)
    assert [e.type for e in events]==['SpaceReleased','SpacePressed']
    assert 57 in a._pressed


def test_bounce_final_down_remains_onhook():
    a,events=adapter(False)
    for value,now in [(1,1),(0,1.0175),(1,1.0519)]:
        a.feed_key(57,value,timestamp=now)
    assert [e.type for e in events]==['SpacePressed','SpaceReleased','SpacePressed']
    assert 57 in a._pressed


def test_backlogged_real_abi_events_do_not_lose_hangup():
    a,events=adapter(); a._fd=42
    data=a.EVENT_STRUCT.pack(100,0,1,57,0)+a.EVENT_STRUCT.pack(101,0,1,57,1)
    with patch('app.foundation.input_adapters.os.read',side_effect=[data,b'']), patch('app.foundation.input_adapters.time.monotonic',return_value=500):
        a._run()
    assert [e.type for e in events]==['SpaceReleased','SpacePressed']
    assert 57 in a._pressed


def test_duplicate_and_repeat_do_not_restart_session():
    a,events=adapter()
    for _ in range(500): a.feed_key(57,2,timestamp=1)
    a.feed_key(57,1,timestamp=1)
    assert not events


def test_help_button_retains_existing_debounce():
    a,events=adapter()
    for value,now in [(1,1),(0,1.01),(1,1.02),(1,1.1)]:
        a.feed_key(6,value,timestamp=now)
    assert [(e.type,e.payload) for e in events]==[('ButtonPressed',{'button_id':5}),('ButtonReleased',{'button_id':5}),('ButtonPressed',{'button_id':5})]


def test_closed_reader_does_not_drain_buffer_or_schedule_release():
    a,events=adapter(); a.close()
    with patch('app.foundation.input_adapters.os.read',side_effect=AssertionError('closed read')):
        a._run()
    assert not events and a._thread is None


def test_reconnect_sync_still_requires_real_release():
    a,events=adapter(False); a._fd=42
    def ioctl(fd,request,bitmap,mutate): bitmap[57//8]|=1<<(57%8)
    with patch('app.foundation.input_adapters.fcntl.ioctl',side_effect=ioctl): a._sync_initial_levels()
    assert not events
    a.feed_key(57,1,timestamp=1)
    a.feed_key(57,0,timestamp=1.01)
    a.feed_key(57,1,timestamp=1.02)
    assert [e.type for e in events]==['SpaceReleased','SpacePressed']

@pytest.mark.asyncio
async def test_real_adapter_to_stand_rapid_final_hangup_revokes_session():
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.config import Settings
    a,events=adapter()
    stand=HardwareIntegrationStand(Settings(profile='mock',hardware_mode='simulator',content_root='content'),background_scheduler=False)
    stand._admits_physical_users=lambda:True
    try:
        for value,now in [(0,1),(1,1.0175),(0,1.025),(1,1.0344)]:
            a.feed_key(57,value,timestamp=now)
        for event in events:
            await stand.inject_physical({'type':event.type})
        record=stand.application.sessions.get(stand.active_session_id)
        assert record is None or not record.handset_up
        assert stand.snapshot()['canonical_state']['mode']=='idle'
    finally: await stand.aclose()


@pytest.mark.asyncio
async def test_lift_bounce_69ms_preserves_session_while_settled_hangup_ends_it():
    import asyncio
    from app.foundation.input_adapters import NormalizedInputEvent
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.config import Settings
    stand=HardwareIntegrationStand(Settings(profile='mock',hardware_mode='simulator',content_root='content'),background_scheduler=False)
    stand._physical_loop=asyncio.get_running_loop()
    stand._hid_generation=1
    stand._admits_physical_users=lambda:True
    try:
        stand._on_physical_input(NormalizedInputEvent('SpaceReleased',{}),1)
        await asyncio.sleep(0.030)
        stand._on_physical_input(NormalizedInputEvent('SpacePressed',{}),1)
        await asyncio.sleep(0.039)
        stand._on_physical_input(NormalizedInputEvent('SpaceReleased',{}),1)
        await asyncio.sleep(0.160)
        state=stand.snapshot()['canonical_state']
        assert state['handset']=='up' and state['mode']!='idle'
        stand._on_physical_input(NormalizedInputEvent('SpacePressed',{}),1)
        await asyncio.sleep(0.180)
        state=stand.snapshot()['canonical_state']
        assert state['handset']=='down' and state['mode']=='idle'
    finally: await stand.aclose()
