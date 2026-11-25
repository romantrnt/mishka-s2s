"""Real Stand/controller guards, synthetic identity receipts and native I/O only."""
from dataclasses import replace
from importlib.machinery import ModuleSpec
from types import SimpleNamespace
import sys

import pytest

from app.foundation.domain import Event
from app.foundation.s2s import AudioChunk, SoundDevicePlaybackController
from test_owned_private_pcm import setup, install, HANDLE

IDS=['TYPED_DATE','TYPED_WEATHER','TYPED_WHAT_NEXT','TYPED_WHERE_NOW','TYPED_CARD_DOCTORS']

async def support_pcm(response_id):
    stand=await setup()
    stand.application.record_audio_diagnostic=lambda kind,**details:stand.diags.append((kind,details))
    state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
    state.mode='support_intake';state.safety_severity='none'
    state.dialogue_information_kind=response_id.removeprefix('TYPED_').lower()
    state.identity_capabilities=['personalize','schedule']
    handle=replace(HANDLE,response_id=response_id)
    install(stand,handle)
    binding=(state.profile_ref,state.identity_request_id)
    return stand,state,handle,binding


@pytest.mark.asyncio
@pytest.mark.parametrize('response_id',IDS)
async def test_support_pcm_requires_exact_kind_safe_phase_and_current_private_grant(response_id):
    stand,state,handle,binding=await support_pcm(response_id)
    try:
        assert stand._pcm_owner_current(handle,binding)
        state.dialogue_information_kind='foreign'
        assert not stand._pcm_owner_current(handle,binding)
        state.dialogue_information_kind=response_id.removeprefix('TYPED_').lower()
        state.safety_severity='concern'
        assert not stand._pcm_owner_current(handle,binding)
        state.safety_severity='none'
        stand.clock[0]=21.0  # Actual scope check sees the fixture grant expire.
        private=response_id in {'TYPED_WHAT_NEXT','TYPED_WHERE_NOW','TYPED_CARD_DOCTORS'}
        assert stand._pcm_owner_current(handle,binding)==(not private)
        stand.clock[0]=10.0
        if private:
            assert not stand._pcm_owner_current(handle,None)
            state.identity_capabilities=[]
            assert not stand._pcm_owner_current(handle,binding)
    finally:await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('response_id',IDS)
async def test_real_native_second_frame_is_revoked_by_actual_same_mode_concern(monkeypatch,response_id):
    stand,state,handle,binding=await support_pcm(response_id);writes=[]
    class Stream:
        def __init__(self,**kwargs):pass
        def start(self):pass
        def write(self,chunk):writes.append(len(chunk))
        def stop(self): self.drained = True
        def abort(self):pass
        def close(self):pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec('sounddevice',loader=None),query_devices=lambda *_:{'max_output_channels':1}))
    controller=SoundDevicePlaybackController();controller.active_token=handle.token
    original=controller._run_io;calls=0
    async def io(operation):
        nonlocal calls
        result=await original(operation);calls+=1
        if calls==2:  # stream.start then first real guarded write completed.
            await type(stand.application).receive_async(stand.application,
                Event('SpeechReceived',handle.session_id,1,1.,'test',{},'same-mode-concern',10.),'Мне больно')
            assert state.mode=='support_intake' and state.safety_severity=='concern'
        return result
    controller._run_io=io
    try:
        assert stand._pcm_owner_current(handle,binding)
        result=await controller.play_owned_pcm(AudioChunk((.1,)*8192,16000,handle.token,handle.session_id),
            lambda:stand._pcm_owner_current(handle,binding))
        assert writes==[4096] and not result.completed
        assert not stand._pcm_owner_current(handle,binding)
    finally:
        await controller.stop();await stand.aclose()
