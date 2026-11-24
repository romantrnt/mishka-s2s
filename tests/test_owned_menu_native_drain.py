"""Actual menu render waits for native drain; all PCM/provider I/O is synthetic."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.experimental_voice import ExperimentalTTSProvider
from app.foundation.public_menu_audio import enable_public_menu_audio_cache
from app.foundation.s2s import AudioChunk
from story_catalogue_fixtures import tiny_stories
from test_scene_menu_runtime import Harness


class DrainingNative(ExperimentalTTSProvider):
    def __init__(self):
        super().__init__({'route': 'vox555', 'limits': {
            'inference_timeout_s': 2., 'max_text_chars': 1000}})
        self.wait_entered = asyncio.Event()
        self.release = asyncio.Event()
        self._recovery_task = asyncio.create_task(self.release.wait())
        self.calls = []

    def configuration_readiness(self):
        return {'provider': 'synthetic-native-drain'}

    async def wait_for_idle(self, cancelled):
        self.calls.append('wait')
        self.wait_entered.set()
        await super().wait_for_idle(cancelled)
        self.calls.append('idle')

    async def synthesize(self, text, token, cancelled):
        self.calls.append('direct')
        # Retain the actual native busy admission; never start a real worker.
        return await super().synthesize(text, token, cancelled)

    async def _start_worker(self, cancelled):
        raise AssertionError('No worker process is allowed in this test')

    async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
        assert first_chunk and not cancelled()
        assert self._recovery_task.done() and not self._job_lock.locked()
        self.calls.append('stream')
        yield AudioChunk((.1, .2), 16000, token)
        yield AudioChunk((.3,), 16000, token)


async def native_menu(tmp_path):
    h = Harness()
    # Keep real WAV/hash admission without rereading full recordings in a drain test.
    h.app.canonical_runtime.stories = tiny_stories(tmp_path)
    native = DrainingNative()
    h.app.bounded_s2s.tts = native
    cache = enable_public_menu_audio_cache(h.app)
    validator = h.app.bounded_s2s.response_validator.validate_trusted
    async def validate(*args, **kwargs):
        native.calls.append('validate')
        return await validator(*args, **kwargs)
    h.app.bounded_s2s.response_validator.validate_trusted = validate
    return h, native, cache


async def at_idle_wait(pending, native):
    waiting = asyncio.create_task(native.wait_entered.wait())
    try:
        await asyncio.wait({pending, waiting}, timeout=1., return_when=asyncio.FIRST_COMPLETED)
        assert native.wait_entered.is_set(), native.calls
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def close(h, native, pending):
    native.release.set()
    await asyncio.wait_for(native._recovery_task, 2.)
    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2.)
    await h.close()


@pytest.mark.asyncio
async def test_actual_menu_entry_waits_for_cancelled_native_drain_before_request(tmp_path):
    h, native, cache = await native_menu(tmp_path)
    pending = asyncio.create_task(h.button(2))
    try:
        await at_idle_wait(pending, native)
        assert not pending.done() and not h.played and cache.snapshot()['entries'] == 0
        assert native.calls[-1] == 'wait' and 'validate' in native.calls
        native.release.set()
        result = await asyncio.wait_for(pending, 2.)
        assert result['reason'] == 'button_mode'
        assert h.state.scene_menu_phase == 'narrating'
        assert h.played[-1].response_id == 'MENU_FIXED_STORY'
        assert native.calls[-3:] == ['wait', 'idle', 'stream']
        assert 'direct' not in native.calls and native.worker_starts == 0
        assert cache.snapshot()['entries'] == 1
    finally:
        await close(h, native, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['hangup', 'cancel', 'timeout'])
async def test_waiting_menu_owner_cannot_request_audio_or_fill_cache_after_revocation(boundary, tmp_path):
    h, native, cache = await native_menu(tmp_path)
    if boundary == 'timeout':
        h.app.audio.owned_render_timeout_s = .05
    pending = asyncio.create_task(h.button(2))
    try:
        await at_idle_wait(pending, native)
        if boundary == 'hangup':
            await h.send('HandsetPlaced')
        elif boundary == 'cancel':
            pending.cancel()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2.)
        assert not native._recovery_task.cancelled()
        native.release.set()
        await asyncio.wait_for(native._recovery_task, 2.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 2.)
        assert 'direct' not in native.calls and 'stream' not in native.calls
        assert not h.played and cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, pending)


@pytest.mark.asyncio
async def test_non_native_menu_keeps_validated_private_voice_and_public_cache(tmp_path):
    h = Harness()
    h.app.canonical_runtime.stories = tiny_stories(tmp_path)
    cache = enable_public_menu_audio_cache(h.app)
    calls = []
    original = h.render.convert_private
    async def convert(audio, cancelled):
        calls.append('private')
        return replace(await original(audio, cancelled), samples=(.4, .5))
    h.render.convert_private = convert
    try:
        await h.button(2)
        assert h.played[-1].response_id == 'MENU_FIXED_STORY'
        assert calls == ['private'] and len(h.render.texts) == 1
        assert cache.snapshot()['entries'] == 1
    finally:
        await h.close()
