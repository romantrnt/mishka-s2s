"""Application scenarios through the real serial encoder, using a synthetic wire.

This checks host CSV targets, not MCU polarity, firmware or optical brightness.
"""
import asyncio

import pytest

from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lighting import PhysicalLightingOutput, SerialLightingAdapter
from app.foundation.s2s import AudioChunk
from app.foundation.stand import UARTLEDController

# Independent physical contract, deliberately not imported from the renderer.
BUTTON_CHANNELS = {1: {14, 15}, 2: {12, 13}, 3: {10, 11}, 4: {8, 9},
                   5: {6, 7}, 6: {2, 3, 4}, 7: {0, 1, 17}}
FILLS = {1, 3, 6, 9, 10, 13, 14}
DIALOGUE_ANCHORS = {0, 4}
ROOT_MENU = FILLS | {5, 16}


class Wire:
    def __init__(self):
        self.lines = []

    def open(self):
        pass

    def close(self):
        pass

    def write(self, payload):
        self.lines.append(bytes(payload))
        return len(payload)

    def snapshot(self):
        states = {element: 0 for element in range(18)}
        for line in self.lines:
            assert line.endswith(b"\n") and b"\r" not in line
            element, state, duration = map(int, line[:-1].split(b","))
            assert element in states and state in (0, 1) and duration >= 0
            states[element] = state
        return {element for element, value in states.items() if value}


class Render:
    async def synthesize(self, text, token, cancelled):
        return AudioChunk((.1, .2), 16000, token)

    async def convert_private(self, audio, cancelled):
        return audio


class Harness:
    def __init__(self, band="6-7"):
        self.clock = 10.
        self.sequence = 0
        self.sid = "audit-wire"
        self.wire = Wire()
        self.adapter = SerialLightingAdapter("/synthetic-only", transport=self.wire)
        self.adapter.open()
        self.output = PhysicalLightingOutput(self.adapter)
        self.led = UARTLEDController(None, output=self.output, monotonic_now=lambda: self.clock,
                                     background_scheduler=False)
        self.app, *_ = build_application(
            settings=Settings(profile="mock", runtime_mode="exhibition"), led_controller=self.led,
            monotonic_now=lambda: self.clock, background_scheduler=False)
        self.app.canonical_runtime.sessions[self.sid] = CanonicalState(
            self.sid, mode="menu_ready", handset="up", age_band=band)
        self.app.bounded_s2s.tts = self.app.bounded_s2s.voice = Render()

    @property
    def state(self):
        return self.app.canonical_runtime.sessions[self.sid]

    async def send(self, kind, payload=None):
        self.sequence += 1
        result = await self.app.receive_async(Event(kind, self.sid, self.sequence, self.clock, "audit",
            payload or {}, str(self.sequence), self.clock))
        self.output.flush()
        return result

    async def press(self, button):
        return await self.send("ButtonPressed", {"button_id": button})

    async def release(self, button):
        return await self.send("ButtonReleased", {"button_id": button})

    async def complete(self):
        lease = self.app._active_audio[self.sid]
        return await self.send("AudioCompleted", {"response_id": lease["content_id"],
            **{key: lease[key] for key in ("token", "generation", "playback_id", "playback_lease")}})

    def assert_lit(self, expected):
        self.output.flush()
        assert self.wire.snapshot() == set(expected)
        assert self.output.snapshot() == {str(i): int(i in expected) for i in range(18)}

    async def close(self):
        await self.app._cleanup_session_resources(self.sid)
        self.led.cancel_effects(clear=True)
        self.output.close()
        self.adapter.close()


@pytest.mark.parametrize("band", ["1-3", "4-5", "6-7"])
@pytest.mark.parametrize("button", range(1, 8))
def test_all_root_selections_send_only_the_current_scene_channels(button, band):
    async def scenario():
        h = Harness(band)
        try:
            await h.press(button)
            if button in {1, 2, 4, 7}:
                descriptor = h.state.scene_menu_descriptor
                assert descriptor and 5 in descriptor["buttons"]
                h.assert_lit(set().union(*(BUTTON_CHANNELS[b] for b in descriptor["buttons"])))
            elif button == 3:
                h.assert_lit({2, 14})
            elif button == 5:
                h.assert_lit({1, 14, 16})
            else:
                await h.complete()
                h.assert_lit(FILLS | DIALOGUE_ANCHORS)
            await h.send("HandsetPlaced")
            h.led.advance_effects(h.clock + 30.)
            h.assert_lit(set())
        finally:
            await h.close()

    asyncio.run(scenario())


def test_game_target_retry_exit_resume_and_hangup_replace_entire_serial_frame():
    async def scenario():
        h = Harness()
        try:
            await h.press(1); await h.release(1)
            assert (await h.press(1))["reason"] == "scene_menu_skipped"
            await h.release(1)
            assert (await h.press(1))["reason"] == "game_started"
            h.assert_lit({14, 15})
            await h.release(1); await h.press(1); await h.release(1)
            h.assert_lit({12, 13})
            assert (await h.press(3))["reason"] == "game_retry"
            await h.release(3)
            h.assert_lit({12, 13})
            await h.press(7); await h.release(7)
            h.assert_lit({0, 1, 2, 3, 5, 16})
            assert (await h.press(6))["reason"] == "game_exit_cancelled"
            await h.release(6)
            h.assert_lit({12, 13})
            await h.press(7); await h.release(7); await h.press(7); await h.release(7)
            h.assert_lit(ROOT_MENU)
            await h.send("HandsetPlaced")
            h.led.advance_effects(h.clock + 30.)
            h.assert_lit(set())
        finally:
            await h.close()

    asyncio.run(scenario())


def test_dialogue_breathing_processing_and_late_callback_obey_scene_owner():
    async def scenario():
        h = Harness()
        try:
            await h.press(6); await h.release(6); await h.complete()
            h.assert_lit(FILLS | DIALOGUE_ANCHORS)
            h.clock += 1.
            h.led.advance_effects(h.clock)
            h.assert_lit(DIALOGUE_ANCHORS)
            await h.app.dialogue_lifecycle(h.sid, "processing")
            h.assert_lit({14} | DIALOGUE_ANCHORS)
            h.clock += .6
            h.led.advance_effects(h.clock)
            h.assert_lit({13} | DIALOGUE_ANCHORS)
            await h.app.dialogue_lifecycle(h.sid, "output")
            h.assert_lit(FILLS | DIALOGUE_ANCHORS)
            await h.press(7); await h.release(7)
            h.assert_lit(ROOT_MENU)
            await h.app.dialogue_lifecycle(h.sid, "listening")
            h.led.advance_effects(h.clock + 30.)
            h.assert_lit(ROOT_MENU)
            await h.send("HandsetPlaced")
            await h.app.dialogue_lifecycle(h.sid, "output")
            h.led.advance_effects(h.clock + 40.)
            h.assert_lit(set())
        finally:
            await h.close()

    asyncio.run(scenario())
