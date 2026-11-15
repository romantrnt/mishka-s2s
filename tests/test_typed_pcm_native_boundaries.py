"""Extend the real native playback expiry fence to private typed schedule PCM.

Only the sounddevice stream is replaced; these exercise the actual stand,
owner guard, SoundDevicePlaybackController lock and native I/O closures.
"""
from dataclasses import replace

import pytest

import test_owned_private_pcm as native


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", ["TYPED_WHAT_NEXT", "TYPED_WHERE_NOW"])
@pytest.mark.parametrize("boundary", ["play_lock", "stream_start", "write_dispatch", "success"])
async def test_typed_schedule_pcm_reuses_native_identity_fence(monkeypatch, response_id, boundary):
    original_setup = native.setup
    original_start = native.start
    handle = replace(native.HANDLE, response_id=response_id)

    async def setup(*args, **kwargs):
        stand = await original_setup(*args, **kwargs)
        state = stand.application.canonical_runtime.sessions[handle.session_id]
        state.mode = "schedule_weather"
        state.identity_capabilities = ["schedule"]
        return stand

    def start(stand, owned_handle=None, audio=native.AUDIO):
        return original_start(stand, owned_handle or handle, audio)

    monkeypatch.setattr(native, "HANDLE", handle)
    monkeypatch.setattr(native, "setup", setup)
    monkeypatch.setattr(native, "start", start)
    if boundary == "success":
        await native.test_physical_pcm_success_uses_guarded_native_path(monkeypatch)
    else:
        await native.test_physical_pcm_rechecks_identity_at_native_boundaries(monkeypatch, boundary)
