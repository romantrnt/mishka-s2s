"""Actual canonical pause/resume through guarded native SoundDevice playback."""
import asyncio
import io
import sys
import wave
from types import SimpleNamespace
import pytest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import SoundDevicePlaybackController
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput

@pytest.mark.asyncio
async def test_fixed_story_queued_pause_uses_request_offset_not_previous_owner(monkeypatch):
    stand = HardwareIntegrationStand(Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    app, sid = stand.application, stand.active_session_id
    seq = 0
    async def send(kind, payload=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind, sid, seq, float(seq), "independent-test", payload or {}, f"native-pause:{seq}", float(seq)))
    await send("HandsetLifted")
    state = app.canonical_runtime.sessions[sid]
    story = "fixed_aibolit_v1"
    assert app.canonical_runtime.stories.get_verified(story)
    state.mode, state.substate, state.handset = "fixed_story_playing", "story_playing", "up"
    state.active_story_id = state.active_audio_id = story
    state.playback_lease = "independent-story-lease"
    state.expected_inputs = ["button_6", "button_7", "handset_down"]
    stand.hardware_mode = "physical"
    stand.playback = playback = SoundDevicePlaybackController()
    playback.position_samples = 999999
    await playback._play_lock.acquire()
    writes, streams = [], []
    class Stream:
        def __init__(self, **kwargs):
            self.index = len(streams)
            streams.append(self)
        def start(self): pass
        def write(self, values): writes.append((self.index, len(values)))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=Stream, __spec__=__import__("importlib.machinery", fromlist=["ModuleSpec"]).ModuleSpec("sounddevice", None), query_devices=lambda *args: {"max_output_channels": 1}))
    blocked, release = asyncio.Event(), asyncio.Event()
    old_run_io = playback._run_io
    native_writes = 0
    async def barrier(operation):
        nonlocal native_writes
        if operation.__name__ == "guarded_write":
            native_writes += 1
            if native_writes == 2:
                blocked.set()
                await release.wait()
        return await old_run_io(operation)
    playback._run_io = barrier
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b"\x01\x00" * 16000)
    async def read(*_): return wav.getvalue()
    stand._offload_audio = read
    output = HeadlessAudioOutput()
    dispatched = []
    def dispatch(response, asset, token, offset, handle):
        dispatched.append((response, offset, handle))
        if response == story:
            stand._headless_asset_play(response, asset, token, offset, handle)
    output.on_asset_play = dispatch
    output.on_pause = stand._pause_story_playback
    output.on_cancel = lambda token: stand._cancel_response_audio()
    app.audio = output
    async def complete(phrase):
        owner = dict(app._active_audio[sid])
        return await send("AudioCompleted", {**owner, "response_id": phrase})
    try:
        app._play_owned_audio(sid, story, "synthetic.wav", 1,
            {"playback_lease": state.playback_lease}, offset_ms=200)
        await asyncio.sleep(0)
        assert writes == []
        pause = asyncio.create_task(send("ButtonPressed", {"button_id": 6}))
        for _ in range(100):
            if playback._generation: break
            await asyncio.sleep(.001)
        assert playback._generation
        # Canonical has already replaced the old story lease, but this must
        # revoke only output, not the one retained pause checkpoint.
        assert state.active_audio_id == "PHRASE_062"
        playback._play_lock.release()
        release.set()
        result = await asyncio.wait_for(pause, 2)
        assert result["reason"] == "story_pause_requested"
        assert result["reason"] == "story_pause_requested"
        assert writes == []
        assert state.dialogue_memory.story_checkpoint == {"story_id": story, "offset_ms": 200}
    finally:
        release.set()
        if playback._play_lock.locked(): playback._play_lock.release()
        stand.hardware_mode = "simulator"
        await stand.aclose()
