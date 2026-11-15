import asyncio
import threading
from types import SimpleNamespace
import pytest
from test_lighting_fault_lifecycle import setup,finish,fault

@pytest.mark.asyncio
@pytest.mark.parametrize('failed',['playback','capture'])
async def test_each_faulted_device_stop_is_attempted_independently(monkeypatch,failed):
    parts=await setup();stand=parts[0];old_playback,old_capture=stand.playback,stand.audio_input;calls=[]
    async def playback_stop():
        calls.append('playback')
        if failed=='playback':raise OSError('PRIVATE_PLAYBACK_DEVICE')
    def capture_stop():
        calls.append('capture')
        if failed=='capture':raise OSError('PRIVATE_CAPTURE_DEVICE')
    stand.playback=SimpleNamespace(stop=playback_stop)
    stand.audio_input=SimpleNamespace(stop=capture_stop)
    try:
        await fault(parts,monkeypatch)
        if stand._lighting_fault_future is not None:
            await asyncio.gather(asyncio.wrap_future(stand._lighting_fault_future),return_exceptions=True)
        assert sorted(calls)==['capture','playback']
        assert not stand._headless_audio_accepting()
        assert 'PRIVATE_' not in str(stand.errors)
    finally:
        stand.playback,stand.audio_input=old_playback,old_capture
        stand._lighting_fault_future=None
        await finish(parts)

@pytest.mark.asyncio
@pytest.mark.parametrize('blocked',['playback','capture'])
async def test_timeout_does_not_block_other_stop_or_touch_replacement_devices(monkeypatch,blocked):
    parts=await setup();stand=parts[0];old_playback,old_capture=stand.playback,stand.audio_input
    entered=threading.Event();release=threading.Event();calls=[];new_calls=[]
    async def playback_stop():
        calls.append('playback')
        if blocked=='playback':
            entered.set()
            await asyncio.to_thread(release.wait,3)
    def capture_stop():
        calls.append('capture')
        if blocked=='capture':
            entered.set();release.wait(3)
    async def new_playback_stop():new_calls.append('playback')
    stand.playback=SimpleNamespace(stop=playback_stop);stand.audio_input=SimpleNamespace(stop=capture_stop)
    try:
        await fault(parts,monkeypatch)
        await asyncio.wrap_future(stand._lighting_fault_future)
        assert entered.is_set()
        assert sorted(calls)==['capture','playback']
        assert any(x.get('error_code')=='lighting_cleanup_timeout' for x in stand.errors)
        stand.playback=SimpleNamespace(stop=new_playback_stop)
        stand.audio_input=SimpleNamespace(stop=lambda:new_calls.append('capture'))
        release.set();await asyncio.sleep(.08)
        assert not new_calls
        assert not stand._headless_audio_accepting()
    finally:
        release.set();await asyncio.sleep(.02)
        stand.playback,stand.audio_input=old_playback,old_capture
        stand._lighting_fault_future=None
        await finish(parts)

@pytest.mark.asyncio
async def test_new_fault_owner_during_canonical_await_is_not_stopped_by_old_cleanup(monkeypatch):
    parts=await setup();stand=parts[0];old_playback,old_capture=stand.playback,stand.audio_input
    old_receive=stand.application._receive_canonical_event
    entered,release=asyncio.Event(),asyncio.Event();calls=[]
    async def delayed(*args,**kwargs):
        entered.set();await release.wait();return {}
    async def playback_stop():calls.append('playback')
    stand.application._receive_canonical_event=delayed
    stand.playback=SimpleNamespace(stop=playback_stop);stand.audio_input=SimpleNamespace(stop=lambda:calls.append('capture'))
    try:
        await fault(parts,monkeypatch);await entered.wait()
        stand._lighting_fault={'owner_generation':999,'stage':'synthetic_replacement_owner'}
        release.set();await asyncio.wrap_future(stand._lighting_fault_future)
        assert not calls
    finally:
        release.set();stand.application._receive_canonical_event=old_receive
        stand.playback,stand.audio_input=old_playback,old_capture
        stand._lighting_fault_future=None
        await finish(parts)
