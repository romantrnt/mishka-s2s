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
async def test_actual_native_pause_retains_nonzero_checkpoint_and_resumes_from_it(monkeypatch):
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
    writes, streams = [], []
    class Stream:
        def __init__(self, **kwargs):
            self.index = len(streams)
            streams.append(self)
        def start(self): pass
        def write(self, values): writes.append((self.index, len(values)))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=Stream, query_devices=lambda *_: {'max_output_channels': 1}))
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
        await asyncio.wait_for(blocked.wait(), 2)
        first_written = writes[0][1]
        assert first_written > 0 and writes == [(0, first_written)]
        checkpoint_ms = 200 + first_written * 1000 // 16000
        pause = asyncio.create_task(send("ButtonPressed", {"button_id": 6}))
        for _ in range(100):
            if playback._generation: break
            await asyncio.sleep(.001)
        assert playback._generation
        # Canonical has already replaced the old story lease, but this must
        # revoke only output, not the one retained pause checkpoint.
        assert state.active_audio_id == "PHRASE_062"
        release.set()
        result = await asyncio.wait_for(pause, 2)
        assert result["reason"] == "story_pause_requested"
        assert writes == [(0, first_written)]
        assert state.dialogue_memory.story_checkpoint == {"story_id": story, "offset_ms": checkpoint_ms}
        assert sid not in app._paused_story_playback
        await complete("PHRASE_062")
        assert state.mode == "story_paused"
        await send("ButtonReleased", {"button_id": 6})
        await send("ButtonPressed", {"button_id": 6})
        assert state.mode == "story_resume_announcement"
        await complete("PHRASE_064")
        resumed = [item for item in dispatched if item[0] == story]
        assert len(resumed) == 2 and resumed[-1][1] == checkpoint_ms
        assert resumed[0][2].playback_id != resumed[-1][2].playback_id
        await asyncio.wait_for(asyncio.gather(*tuple(stand._response_audio_tasks)), 2)
        assert sum(n for stream, n in writes if stream == 1) == 16000 - checkpoint_ms * 16000 // 1000
    finally:
        release.set()
        stand.hardware_mode = "simulator"
        await stand.aclose()
