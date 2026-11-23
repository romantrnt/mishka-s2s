"""Synthetic PCM: real authored-story owners, early delivery and late cancellation."""
import asyncio

import pytest

from app.foundation.demo_story_audio import render
from app.foundation.demo_story_catalog import load_catalog
from app.foundation.stand import HeadlessAudioOutput
from app.foundation.visit_profile import VisitProfile
from test_scene_menu_runtime import Harness


def streaming_harness():
    h = Harness()
    h.app._owned_menu_voice_capable = lambda: False
    port = HeadlessAudioOutput()
    port.on_asset_play = lambda *_: None
    port.on_cancel = lambda *_: None
    port.on_pcm_play = lambda handle, audio: h.played.append(handle)
    h.streams = []
    port.on_pcm_stream_play = lambda handle, stream, on_cue=None: h.streams.append((handle, stream, on_cue))
    h.app.audio = port
    return h


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['back', 'hangup', 'finish', 'failure'])
async def test_first_part_plays_before_rest_finishes_and_owner_controls_late_work(ending):
    h = streaming_harness()
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.render.synthesize
    calls = 0
    async def blocked(text, token, cancelled):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
            if ending == 'failure':raise RuntimeError('synthetic late TTS failure')
        return await original(text, token, cancelled)
    h.render.synthesize = blocked
    try:
        await h.button(3);await h.release(3)
        requested = asyncio.create_task(h.button(1))
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(requested, 2)
        assert result['reason'] == 'personal_story_ready'
        assert h.state.mode == 'personal_story_playing'
        owner = h.app._personal_stories[h.sid]
        assert not owner.complete and len(owner.parts) == 1
        handle, stream, cue = h.streams[-1]
        first, _ = await anext(stream)
        assert first.samples and first.session_id == h.sid
        assert not owner.started
        cue(())
        assert owner.started
        await h.release(1)
        if ending in {'back', 'hangup'}:
            await (h.button(7) if ending == 'back' else h.send('HandsetPlaced'))
            release.set()
            await asyncio.sleep(0)
            assert owner.closed and owner.audio is None and not owner.parts
            assert h.sid not in h.app._personal_stories
            with pytest.raises(StopAsyncIteration):await anext(stream)
        else:
            release.set()
            if ending == 'failure':
                with pytest.raises(RuntimeError, match='stream_failed'):
                    async for _ in stream:pass
                await h.complete(failed=True)
            else:
                async for part, _ in stream:assert part.token == handle.token
                assert owner.complete and not owner.failed
                assert len(owner.parts) > 1
                await h.complete()
            assert h.state.mode == 'root_menu'
            assert not h.app._personal_stories
    finally:
        release.set()
        h.app._discard_personal_story(h.sid)
        await h.close()


@pytest.mark.asyncio
async def test_public_fragment_cache_reuses_voice_but_never_a_private_name():
    h = Harness()
    entry = load_catalog()[0]
    a = VisitProfile(display_name='Адель')
    b = VisitProfile(display_name='Зарина')
    try:
        for profile, token in [(a, 1), (b, 2)]:
            audio = await render(h.app.bounded_s2s, entry, profile, token, lambda:True)
            assert audio.samples and audio.token == token
        assert h.app.bounded_s2s._story_fragment_cache.hits > 0
        assert any('Адель' in text for text in h.render.texts)
        assert any('Зарина' in text for text in h.render.texts)
        private = [t for t in h.render.texts if 'Адель' in t or 'Зарина' in t]
        assert len(private) == 2
    finally:await h.close()


@pytest.mark.asyncio
async def test_waiting_wave_changes_real_binary_elements_and_cannot_relight_after_back():
    from types import SimpleNamespace
    from app.foundation.lighting import FakeLightingOutput, BUTTON_FILL_ELEMENTS, BUTTON_DIGIT_ELEMENTS
    from app.foundation.stand import UARTLEDController
    h = streaming_harness()
    output = FakeLightingOutput()
    now = [h.app.monotonic_now()]
    h.app.led = UARTLEDController(SimpleNamespace(send=lambda *_:None), output=output,
        monotonic_now=lambda:now[0], background_scheduler=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.render.synthesize
    async def blocked(*args):
        entered.set();await release.wait();return await original(*args)
    h.render.synthesize = blocked
    requested = None
    try:
        await h.button(3);await h.release(3)
        requested = asyncio.create_task(h.button(1))
        await asyncio.wait_for(entered.wait(),2)
        assert h.state.mode == 'personal_story_generating'
        assert h.app.led.effect_snapshot()['wave_loop'] is True
        assert h.app.led.effect_snapshot()['wave_stage_ms'] == 900
        before = output.snapshot()
        assert before[str(BUTTON_FILL_ELEMENTS[1])] == 1
        assert before[str(BUTTON_DIGIT_ELEMENTS[7][1])] == 1
        now[0] += 1.1;h.app.led.advance_effects(now[0])
        assert output.snapshot() != before
        assert {c.state for c in output.commands} <= {0,1}
        await h.button(7);await h.release(7)
        final = output.snapshot()
        now[0] += 10;h.app.led.advance_effects(now[0])
        assert h.state.mode == 'root_menu' and output.snapshot() == final
    finally:
        release.set()
        if requested:await asyncio.wait_for(requested,2)
        await h.close()
