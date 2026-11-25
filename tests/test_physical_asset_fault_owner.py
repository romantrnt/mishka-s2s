import asyncio
import io
import sys
import wave
from dataclasses import replace
from importlib.machinery import ModuleSpec
from types import SimpleNamespace
import pytest
from test_owned_private_pcm import setup, HANDLE, install
from app.foundation.s2s import SoundDevicePlaybackController

@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['RINGTONE', 'PHRASE_001', 'placeholder:fixed_story:sample'])
@pytest.mark.parametrize('mutation', ['none', 'read_fault', 'native_fault', 'hangup', 'handset_down', 'newlift', 'replacement', 'hid_generation'])
async def test_physical_asset_retains_owner_through_read_and_native_write(monkeypatch, response, mutation):
    stand = await setup()
    stand.hardware_mode = 'physical'
    stand._hid_generation = 7
    stand._physical_loop = asyncio.get_running_loop()
    handle = replace(HANDLE, response_id=response)
    install(stand, handle)
    state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
    release = asyncio.Event()
    async def cleanup(*args): await release.wait()
    stand._handle_hid_loss = cleanup
    def fault(): stand._on_hid_error({'kind': 'reader_exception'}, 7)
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b'\x01\x00' * 320)
    async def read(*args):
        # Real await inside read, then mutate while old asset task resumes.
        await asyncio.sleep(0)
        if mutation == 'read_fault': fault()
        elif mutation == 'hangup':
            state.handset = 'down'; stand.application._active_audio.pop(HANDLE.session_id)
        elif mutation == 'handset_down': state.handset = 'down'
        elif mutation == 'newlift':
            stand.application._session_epochs[HANDLE.session_id] = 1
        elif mutation == 'replacement':
            stand.application._active_audio[HANDLE.session_id]['playback_id'] = 'replacement'
        elif mutation == 'hid_generation': stand._hid_generation = 8
        return wav.getvalue()
    stand._offload_audio = read
    writes = []; opened = []
    class Stream:
        def __init__(self, **kwargs): opened.append(True)
        def start(self):
            if mutation == 'native_fault': fault()
        def write(self, chunk): writes.append(len(chunk))
        def stop(self): self.drained = True
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    stand.playback = SoundDevicePlaybackController()
    try:
        await stand._play_asset_audio(HANDLE.session_id, response, 'synthetic.wav', 12, 10, handle)
        if mutation == 'none':
            assert writes == [160]  # 10ms offset retained.
            if response != 'RINGTONE':
                assert [e.event_type for e in stand.events] == ['AudioCompleted']
            if response.startswith('placeholder:fixed_story:'):
                assert stand.playback.position_samples == 320
        else:
            assert not writes and not stand.events
            if mutation != 'native_fault': assert not opened
    finally:
        release.set()
        if getattr(stand, '_hid_loss_future', None) is not None:
            await asyncio.wrap_future(stand._hid_loss_future)
        stand._hid_loss_future = None
        stand.hardware_mode = 'simulator'
        await stand.aclose()
