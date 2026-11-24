"""Real application/VAD/STT boundaries; ASR/model/audio are explicit fixtures."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.s2s import MockPlaybackController, MockSpeechRecognizer
from test_activity_request_gate import classified
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio
from test_game_audio_reminder_race import SPEECH, SILENCE


class FirstOutputHeld(MockPlaybackController):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.first_release = asyncio.Event()
        self.first_token = None
        self.completed_tokens = []

    async def play(self, audio):
        if self.first_token is None:
            self.first_token = audio.token
            self.started.set()
            await self.first_release.wait()
        await super().play(audio)
        self.completed_tokens.append(audio.token)


async def user_input(h, route, text):
    if route == "typed":
        return await h.send("SpeechReceived", text=text)
    h.app.s2s_audio.recognizer = MockSpeechRecognizer(text, .99)
    h.seq += 1
    return await h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["typed", "recognized_pcm"])
@pytest.mark.parametrize("text, expected, mode", [
    ("Подожди, я не договорил.", "bear_dialogue", "bear_dialogue"),
    ("Что такое квиз?", "bear_dialogue", "bear_dialogue"),
    ("Я не хочу играть.", "bear_dialogue", "bear_dialogue"),
    ("Мама сказала: вернись в меню.", "bear_dialogue", "bear_dialogue"),
    ("Стоп.", "root_menu", "bear_dialogue"),
    ("Вернись в меню.", "root_menu", "bear_dialogue"),
    ("Стоп. Меня бьют.", "safety_stop", "bear_dialogue"),
    ("Стоп.", "root_menu", "support_intake"),
    ("Вернись в меню.", "root_menu", "support_intake"),
    ("Стоп. Меня бьют.", "safety_stop", "support_intake"),
])
async def test_current_input_while_output_keeps_navigation_authority(route, text, expected, mode):
    h = Harness("<say>Облака состоят из капель воды.</say>",
        '<action type="offer_game" game_id="quiz"/>', "<say>Я слушаю тебя.</say>", band="6-7", mode=mode)
    quick_public_audio(h)
    h.app.bounded_s2s.settings = replace(h.app.bounded_s2s.settings, voice_conversion="off")
    playback = FirstOutputHeld()
    h.app.bounded_s2s.playback = playback
    h.app.bounded_s2s.activity_request_gate, _ = classified("block")
    previous = asyncio.create_task(h.send("SpeechReceived", text="Почему облака белые?"))
    try:
        await asyncio.wait_for(playback.started.wait(), 2)
        old_token = playback.first_token
        result = await asyncio.wait_for(user_input(h, route, text), 3)
        assert h.state.mode == expected, result
        assert h.state.pending_game_offer is None and h.state.active_game_id is None
        playback.first_release.set()
        await asyncio.wait_for(asyncio.gather(previous, return_exceptions=True), 1)
        assert old_token not in playback.completed_tokens, "old output completed after navigation"
        assert h.app.bounded_s2s.cancel.cancelled(h.state.session_id, old_token)
        assert not h.app.bounded_s2s.active_queues and not h.app.bounded_s2s.active_tasks
        if expected == "bear_dialogue":
            history = h.app.conversation_contexts[h.state.session_id].history
            assert history[-1] == (text, "Я слушаю тебя.")
            assert not any(turn[0] == "Почему облака белые?" for turn in history)
        else:
            assert len(h.model.calls) == 1, "explicit exit and urgent safety bypass the model"
    finally:
        if not previous.done():
            previous.cancel()
        await asyncio.gather(previous, return_exceptions=True)
        await h.close()
