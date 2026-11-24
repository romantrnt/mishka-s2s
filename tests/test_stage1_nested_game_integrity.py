"""Synthetic providers; real root6, consent, owners and nested caller returns.

These are application/PCM-port checks, not microphone, browser or board receipts.
"""
import asyncio
import copy

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lighting import FakeLightingOutput
from app.foundation.stand import BrowserPlaybackController, UARTLEDController
from test_contextual_game_flow import Model, ApprovedActivityRequests
from app.foundation.activity_requests import ActivityRequestGate
from test_scene_menu_runtime import Render


GAMES = ("free_lights", "quiz", "repeat_pattern", "odd_one_out", "word_game")
CALLER_TEXT = "Я люблю рисовать кота"


class SyntheticVoice(Render):
    async def convert(self, audio, cancelled):
        if cancelled():
            raise asyncio.CancelledError
        return await self.convert_private(audio, cancelled)


class NestedGame:
    def __init__(self):
        self.clock = 100.
        self.sequence = 0
        self.sid = "stage1-nested"
        self.app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"),
            monotonic_now=lambda: self.clock, background_scheduler=False)
        self.render = SyntheticVoice()
        self.model = Model("<say>У кота мягкая шерсть.</say>")
        self.app.bounded_s2s.llm = self.model
        self.app.bounded_s2s.activity_request_gate = ActivityRequestGate(ApprovedActivityRequests())
        self.app.bounded_s2s.tts = self.app.bounded_s2s.voice = self.render
        self.output = FakeLightingOutput()
        self.app.led = UARTLEDController(None, output=self.output,
            monotonic_now=lambda: self.clock, background_scheduler=False)
        self.messages = []
        self.app.bounded_s2s.playback = BrowserPlaybackController(self)

    async def publish(self, value):
        self.messages.append(value)

    @property
    def state(self):
        return self.app.canonical_runtime.sessions[self.sid]

    async def send(self, kind, payload=None, text=None):
        self.sequence += 1
        return await asyncio.wait_for(self.app.receive_async(Event(kind, self.sid, self.sequence,
            self.clock, "nested-test", payload or {}, f"nested:{self.sequence}", self.clock), text), 5)

    async def key(self, button, duration=.01):
        pressed = await self.send("ButtonPressed", {"button_id": button})
        self.clock += duration
        released = await self.send("ButtonReleased", {"button_id": button})
        return pressed, released

    async def terminal(self, kind="AudioCompleted", owner=None):
        owner = dict(owner or self.app._active_audio[self.sid])
        return await self.send(kind, {**owner, "response_id": owner["content_id"],
                                      "error_code": "synthetic_playback_failure"})

    async def launch(self, game):
        await self.send("HandsetLifted")
        await self.key(6)
        assert self.state.mode == "bear_dialogue" and self.state.active_audio_id == "DIALOGUE_INTRO"
        await self.terminal()
        assert self.app.led.effect_snapshot()["effect"] == "breathing"
        await self.send("SpeechReceived", text=CALLER_TEXT)
        before = self.app.conversation_contexts[self.sid]
        history, turns = copy.deepcopy(before.history), before.turns
        self.model.answers.append(f'<action type="offer_game" game_id="{game}"/>')
        result = await self.send("SpeechReceived", text="Предложи игру")
        assert result["reason"] == "game_offered" and self.state.pending_game_offer == game
        assert self.app.conversation_contexts[self.sid].turns == turns
        # Navigation keeps the real user request, without a fabricated spoken
        # answer or a charge against the caller's conversational turn budget.
        history.append(("Предложи игру", ""))
        assert self.app.conversation_contexts[self.sid].history == history
        offer = dict(self.app._active_audio[self.sid])
        assert offer["content_id"] == "DIALOGUE_OFFER"
        await self.key(1)  # Audio still owns the invitation; this cannot consent.
        assert self.state.mode == "bear_dialogue" and not self.app._game_callers
        await self.terminal(owner=offer)
        assert (await self.key(1))[0]["reason"] == "game_started"
        assert self.state.active_game_id == game and self.state.game_return_mode == "bear_dialogue"
        caller = self.app._game_callers[self.sid]["context"]
        assert caller.history == history and caller.turns == turns
        assert self.state.pending_game_offer is None
        start = dict(self.app._active_audio[self.sid])
        assert start["content_id"] == "PHRASE_020"
        assert (await self.terminal())["reason"] == "game_prompt_started"
        prompt = dict(self.app._active_audio[self.sid])
        assert prompt["content_id"] == "GAME_PROMPT"
        return caller, history, turns, [start, prompt]

    async def close(self):
        self.app.led.cancel_effects(clear=True)
        await asyncio.wait_for(self.app._cleanup_session_resources(self.sid), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("game", GAMES)
@pytest.mark.parametrize("prompt_failed", [False, True])
async def test_real_root_offer_each_game_returns_exact_caller_by_buttons(game, prompt_failed):
    h = NestedGame()
    try:
        caller, history, turns, old = await h.launch(game)
        if prompt_failed:
            result = await h.terminal("AudioFailed")
            if game == "word_game":
                assert result["reason"] == "word_game_prompt_recovered"
                assert h.state.game_audio_stage == "playing"
            else:
                assert result["reason"] == "game_prompt_retried"
                old.append(dict(h.app._active_audio[h.sid]))
                assert (await h.terminal("AudioFailed"))["reason"] == "game_prompt_audio_failed"
                assert h.state.game_audio_stage == "prompt_failed"
                assert h.sid not in h.app._active_audio and h.sid not in h.app._game_timer_scopes
        else:
            await h.terminal()
            assert h.state.game_audio_stage == "playing"
        assert h.state.game_engine_state.round == 0
        assert (await h.key(7, duration=6.))[1]["reason"] == "game_exited_by_long_button"
        assert h.state.mode == "bear_dialogue" and h.state.game_engine_state is None
        assert h.app.conversation_contexts[h.sid] is caller
        assert caller.history == history and caller.turns == turns
        assert not h.app._game_callers and h.sid not in h.app._game_timer_scopes
        returned = dict(h.app._active_audio[h.sid])
        assert returned["content_id"] == "DIALOGUE_RETURN"
        frame = h.output.snapshot()
        generation = h.app.led.effect_snapshot()["generation"]
        for owner in old:
            for terminal in ("AudioCompleted", "AudioFailed"):
                assert (await h.terminal(terminal, owner))["reason"] == "stale_audio_callback"
                assert h.app._active_audio[h.sid] == returned
                assert h.app.conversation_contexts[h.sid] is caller
                assert h.output.snapshot() == frame
                assert h.app.led.effect_snapshot()["generation"] == generation
        await h.terminal()
        assert h.sid not in h.app._active_audio
        assert h.app.led.effect_snapshot()["effect"] == "breathing"
        h.model.answers.append("<say>Можно нарисовать коту усы.</say>")
        await h.send("SpeechReceived", text="Что ещё нарисовать коту?")
        restored = h.app.conversation_contexts[h.sid]
        assert restored.turns == turns + 1 and CALLER_TEXT in h.model.calls[-1][0]["content"]
        assert getattr(restored, "_game_id", None) is None
        await h.key(7)
        assert h.state.mode == "root_menu" and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_real_root_word_completion_preserves_last_pcm_and_restores_caller():
    h = NestedGame()
    try:
        caller, history, turns, _ = await h.launch("word_game")
        await h.terminal()
        limit = h.app._canonical_audio_context(h.sid).max_turns
        h.model.answers.extend(["<say>Лиса, теперь твоя очередь.</say>"] * (limit - 1)
            + ["<say>Спасибо, мы вернулись к разговору.</say>"])
        for index in range(limit):
            h.messages.clear()
            result = await h.send("SpeechReceived", text="Кот")
            assert result["reason"] == ("word_game_finished" if index == limit - 1 else "word_game_turn_finished")
            if index < limit - 1:
                assert h.state.mode == "game_active" and h.state.game_engine_state.round == index + 1
        assert h.state.mode == "bear_dialogue" and h.state.game_engine_state is None
        assert h.app.conversation_contexts[h.sid] is caller
        assert caller.history == history and caller.turns == turns
        assert not h.app._game_callers and not h.app._active_audio
        assert any(value["type"] == "audio_pcm_output" for value in h.messages)
        assert not any(value["type"] == "audio_stop" for value in h.messages)
        h.model.answers.append("<say>Коту можно нарисовать длинный хвост.</say>")
        await h.send("SpeechReceived", text="Продолжим рисунок")
        assert h.app.conversation_contexts[h.sid].turns == turns + 1
        assert CALLER_TEXT in h.model.calls[-1][0]["content"]
        await h.key(7)
        assert h.state.mode == "root_menu"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_late_word_model_cannot_replace_restored_caller_or_its_return_prompt():
    h = NestedGame()
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None

    class LateModel:
        async def stream(self, messages, request_id, cancelled):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            yield "<say>Игра ещё продолжается.</say>"

    try:
        caller, history, turns, _ = await h.launch("word_game")
        await h.terminal()
        h.app.bounded_s2s.llm = LateModel()
        pending = asyncio.create_task(h.send("SpeechReceived", text="Кот"))
        await asyncio.wait_for(entered.wait(), 2)
        await h.key(7, duration=6.)
        returned = dict(h.app._active_audio[h.sid])
        assert returned["content_id"] == "DIALOGUE_RETURN"
        tts_count = len(h.render.texts)
        frame = h.output.snapshot()
        generation = h.app.led.effect_snapshot()["generation"]
        release.set()
        # Exit may actively cancel the obsolete provider task rather than
        # return its stale receipt. Neither outcome grants later side effects.
        result, = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
        assert isinstance(result, asyncio.CancelledError) or (
            isinstance(result, dict) and result["reason"] == "stale_session_generation")
        assert h.state.mode == "bear_dialogue" and h.app.conversation_contexts[h.sid] is caller
        assert caller.history == history and caller.turns == turns
        assert not h.app._game_callers and h.app._active_audio[h.sid] == returned
        assert len(h.render.texts) == tts_count and h.output.snapshot() == frame
        assert h.app.led.effect_snapshot()["generation"] == generation
    finally:
        release.set()
        if pending is not None:
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)
        await h.close()
