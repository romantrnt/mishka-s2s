"""Synthetic clock/gates, real application lighting and owned PCM dispatch."""
import asyncio
import json
import time

import pytest

from app.foundation.canonical_services import DIALOGUE_ANCHOR_ELEMENTS
from app.foundation.lighting import FakeLightingOutput
from app.foundation.stand import UARTLEDController
from test_scene_menu_runtime import Harness


def hardware_lighting(h):
    h.app.led = UARTLEDController(None, output=FakeLightingOutput(),
        background_scheduler=False, monotonic_now=time.monotonic)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['root_menu', 'menu_ready', 'exhibition_menu_tutorial_buttons'])
async def test_entry_feedback_precedes_validator_tts_and_listening(mode, record_property):
    h = Harness()
    hardware_lighting(h)
    h.app.canonical_runtime.exhibition_mode = mode != 'root_menu'
    h.state.mode = mode
    entered, allow = asyncio.Event(), asyncio.Event()
    h.render.block = asyncio.Event()
    original = h.app.bounded_s2s.response_validator.validate
    clocks = {'tap': time.perf_counter()}
    original_breathing = h.app.led.start_breathing
    def breathing(**kwargs):
        clocks.setdefault('first_feedback', time.perf_counter())
        return original_breathing(**kwargs)
    h.app.led.start_breathing = breathing
    async def validate(*args, **kwargs):
        clocks['validator'] = time.perf_counter()
        entered.set()
        await allow.wait()
        return await original(*args, **kwargs)
    h.app.bounded_s2s.response_validator.validate = validate
    pending = asyncio.create_task(h.button(6))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clocks['feedback_observed'] = time.perf_counter()
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'preparing'
        assert h.app.led.exhibition_effects.effect == 'breathing'
        frame = h.app.led.output.snapshot()
        assert all(frame[str(element)] == 1 for element in DIALOGUE_ANCHOR_ELEMENTS)
        assert not h.played and not h.render.texts
        allow.set()
        await asyncio.wait_for(h.render.entered.wait(), 2)
        clocks['tts'] = time.perf_counter()
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'preparing'
        assert not h.played
        h.render.block.set()
        await pending
        clocks['first_pcm_port'] = time.perf_counter()
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
        assert len(h.played) == 1 and h.played[0].response_id == 'DIALOGUE_INTRO'
        await h.complete()
        clocks['listening'] = time.perf_counter()
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
        assert clocks['tap'] <= clocks['first_feedback'] <= clocks['validator'] <= clocks['feedback_observed'] < clocks['tts'] < clocks['first_pcm_port'] <= clocks['listening']
        record_property('synthetic_gated_timeline_ms', json.dumps({
            key: round((value-clocks['tap'])*1000, 3) for key, value in clocks.items()}))
    finally:
        allow.set(); h.render.block.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_hangup_during_preparation_prevents_late_intro_and_relighting():
    h = Harness()
    hardware_lighting(h)
    h.app.canonical_runtime.exhibition_mode = True
    h.render.block = asyncio.Event()
    pending = asyncio.create_task(h.button(6))
    try:
        await asyncio.wait_for(h.render.entered.wait(), 2)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'preparing'
        await h.send('HandsetPlaced')
        h.render.block.set()
        await asyncio.gather(pending, return_exceptions=True)
        current = h.app.canonical_runtime.sessions.get(h.sid)
        assert not h.played and (current is None or current.mode != 'bear_dialogue')
        assert not h.app._active_audio
        assert set(h.app.led.output.snapshot().values()) == {0}
    finally:
        h.render.block.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_early_dialogue_feedback_and_tour_cancel_precede_old_audio_cleanup():
    h = Harness()
    hardware_lighting(h)
    h.app.canonical_runtime.exhibition_mode = True
    h.state.mode = 'exhibition_menu_tutorial_buttons'
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app._cancel_audio_owner
    async def cleanup(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    h.app._cancel_audio_owner = cleanup
    pending = asyncio.create_task(h.button(6))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'preparing'
        assert h.app.led.exhibition_effects.effect == 'breathing'
        assert not h.app._menu_tutorial_tasks and not h.app._lighting_wave_tasks
        assert not h.render.texts and not h.played
        release.set()
        await pending
        assert h.played[-1].response_id == 'DIALOGUE_INTRO'
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        h.app._cancel_audio_owner = original
        await h.close()
