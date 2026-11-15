"""Menu PCM goes through the actual stand and native playback owner fences.

Only the physical SoundDevice stream is synthetic. No real I/O is performed.
"""
import asyncio
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.foundation.s2s import SoundDevicePlaybackController
from test_owned_private_pcm import AUDIO, HANDLE, install, setup, start

MENUS = [("MENU_FIXED_STORY", "fixed_story_menu"), ("MENU_GAME", "game_menu"), ("MENU_STORY_THEMES", "personal_story_theme"), ("MENU_DAY", "schedule_weather")]


@pytest.mark.asyncio
@pytest.mark.parametrize("response,mode", MENUS)
@pytest.mark.parametrize("boundary", ["play_lock", "stream_start", "write_dispatch"])
@pytest.mark.parametrize("revocation", ["none", "mode", "handset", "lease", "epoch", "hid_fault"])
async def test_menu_native_owner_at_physical_boundary(monkeypatch, response, mode, boundary, revocation):
    stand = await setup()
    handle = replace(HANDLE, response_id=response)
    state = stand.application.canonical_runtime.sessions[handle.session_id]
    state.mode = mode
    state.scene_menu_phase = "narrating"
    # Public menu narration must not acquire a private-profile dependency.
    state.profile_ref = state.identity_request_id = None
    state.identity_capabilities = []
    install(stand, handle)
    stand.playback = SoundDevicePlaybackController()
    entered = threading.Event()
    release = threading.Event()
    opened, writes = [], []

    def revoke():
        if revocation == "mode": state.mode = "root_menu"
        elif revocation == "handset": state.handset = "down"
        elif revocation == "lease": stand.application._active_audio[handle.session_id]["playback_lease"] = "replacement"
        elif revocation == "epoch": stand.application._session_epochs[handle.session_id] = 1
        elif revocation == "hid_fault": stand._capture_hid_fault = {"kind": "input_desynchronized"}

    class Stream:
        def __init__(self, **kwargs): opened.append(True)
        def start(self):
            if boundary == "stream_start":
                entered.set()
                assert release.wait(3)
        def write(self, chunk): writes.append(len(chunk))
        def abort(self): pass
        def close(self): pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=Stream))
    lock = stand.playback._play_lock
    if boundary == "play_lock": await lock.acquire()
    original_io = stand.playback._run_io
    io_calls = 0

    async def delayed_io(operation):
        nonlocal io_calls
        io_calls += 1
        if boundary == "write_dispatch" and io_calls == 2: revoke()
        return await original_io(operation)

    stand.playback._run_io = delayed_io
    try:
        tasks = start(stand, handle, AUDIO)
        if boundary == "play_lock":
            await asyncio.sleep(.02)
            assert not all(task.done() for task in tasks)
            revoke()
            lock.release()
        elif boundary == "stream_start":
            assert await asyncio.to_thread(entered.wait, 2)
            revoke()
            release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        if revocation == "none":
            assert writes == [len(AUDIO.samples)]
            assert [event.event_type for event in stand.events] == ["AudioCompleted"]
            assert stand.events[0].payload.items() >= {
                "response_id": response, "token": handle.token,
                "generation": handle.generation, "playback_id": handle.playback_id,
                "playback_lease": handle.playback_lease,
            }.items()
        else:
            assert not writes and not stand.events
            assert not stand.playback.last_playback_completed
            if boundary == "play_lock": assert not opened
    finally:
        release.set()
        if lock.locked(): lock.release()
        await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("response,mode", MENUS)
@pytest.mark.parametrize("old_phase", ["narrating", "repeat_prompt", "repeat_listen"])
async def test_menu_capture_phase_and_delayed_frame_ownership(response, mode, old_phase):
    stand = await setup()
    original_get = stand.application.sessions.get
    state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
    record = SimpleNamespace(handset_up=True, current_node=mode)
    stand.application.sessions.get = lambda sid: record
    stand._headless_audio_queue = asyncio.Queue()
    state.mode = mode
    state.scene_menu_phase = old_phase
    stand.application._active_audio.clear()
    try:
        assert stand._headless_audio_accepting() is (old_phase == "repeat_listen")
        old_owner = stand._headless_frame_owner()
        # Even a frame sampled during a previous listening turn must not enter
        # after the same-session prompt/repeat cycle advances its generation.
        state.scene_menu_phase = "repeat_prompt"
        state.state_version += 1
        assert not stand._headless_audio_accepting()
        state.scene_menu_phase = "repeat_listen"
        state.state_version += 1
        assert stand._headless_audio_accepting()
        stand._enqueue_headless_audio_frame((.1, .2), old_owner)
        assert stand._headless_audio_queue.empty()
        current_owner = stand._headless_frame_owner()
        stand._enqueue_headless_audio_frame((.1, .2), current_owner)
        assert stand._headless_audio_queue.qsize() == 1
        stand._headless_audio_queue.get_nowait()
        stand.application._session_epochs[HANDLE.session_id] = 1
        stand._enqueue_headless_audio_frame((.1, .2), current_owner)
        assert stand._headless_audio_queue.empty()
    finally:
        stand.application.sessions.get = original_get
        await stand.aclose()
