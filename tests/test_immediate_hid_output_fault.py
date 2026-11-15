import asyncio
import threading
import pytest
from test_owned_private_pcm import setup, HANDLE, AUDIO, start

@pytest.mark.asyncio
async def test_native_owner_callback_revoked_before_owner_loop_cleanup():
    stand = await setup()
    stand._hid_generation = 7
    stand._physical_loop = asyncio.get_running_loop()
    binding = ('synthetic-profile', 'identity-1')
    observed = []
    entered = asyncio.Event()
    release = asyncio.Event()
    async def cleanup(*args):
        entered.set()
        await release.wait()
    stand._handle_hid_loss = cleanup
    async def native(audio, current):
        assert current()
        # Real HID callback executes on reader thread while owner loop is
        # occupied here; queued HardwareDisconnected cannot yet run.
        reader = threading.Thread(target=lambda: stand._on_hid_error(
            {'kind': 'reader_exception', 'error_code': 'hid_read_failed'}, 7))
        reader.start()
        reader.join(timeout=2)
        assert not reader.is_alive()
        assert not entered.is_set()
        observed.append(current())
        if current():
            stand.playback.played.append(audio)
    stand.playback.play_owned_pcm = native
    try:
        await asyncio.gather(*start(stand))
        assert observed == [False]
        assert not stand.playback.played
        assert not stand.events
    finally:
        release.set()
        if getattr(stand, '_hid_loss_future', None) is not None:
            await asyncio.wrap_future(stand._hid_loss_future)
        stand._hid_loss_future = None
        await stand.aclose()

@pytest.mark.asyncio
async def test_actual_native_stream_does_not_write_after_hid_fault(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app.foundation.s2s import SoundDevicePlaybackController
    stand = await setup()
    stand._hid_generation = 7
    stand._physical_loop = asyncio.get_running_loop()
    release = asyncio.Event()
    async def cleanup(*args):
        await release.wait()
    stand._handle_hid_loss = cleanup
    writes = []
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self):
            stand._on_hid_error({'kind': 'reader_exception'}, 7)
        def write(self, chunk): writes.append(len(chunk))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream))
    stand.playback = SoundDevicePlaybackController()
    try:
        await asyncio.gather(*start(stand))
        assert writes == []
        assert not stand.events
    finally:
        release.set()
        if getattr(stand, '_hid_loss_future', None) is not None:
            await asyncio.wrap_future(stand._hid_loss_future)
        stand._hid_loss_future = None
        await stand.aclose()
