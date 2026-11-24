"""Semantic labels are fixtures; actual MiniLLM accuracy needs separate acceptance."""
import asyncio
import json
import time

import pytest

from app.foundation.activity_requests import (ActivityRequestGate, ActivityRequestReceipt,
    OllamaActivityRequestClassifier, activity_request_matches)
from app.foundation.s2s import (BoundedConversationEngine, ConversationContext, MockTTSProvider,
    MockPlaybackController, ProfileContext, ResponseTagParser, TurnResult)
from app.foundation.validation import OllamaMiniLLMValidator
from app.foundation.config import Settings
from test_contextual_game_flow import Harness, Model
from test_conversation_catalogue_gateway import quick_public_audio
from test_game_audio_reminder_race import SPEECH, SILENCE
from app.foundation.s2s import MockSpeechRecognizer


class Voice:
    async def convert(self, audio, cancelled):
        return audio


class TTS(MockTTSProvider):
    def __init__(self):
        super().__init__()
        self.texts = []
    async def synthesize(self, text, token, cancelled):
        self.texts.append(text)
        return await super().synthesize(text, token, cancelled)


class JSONModel(Model):
    supports_dialogue_json = True
    async def stream_dialogue(self, messages, request_id, cancelled, *, response_schema):
        async for value in self.stream(messages, request_id, cancelled):
            yield value


def classified(decision="allow", confidence=1., **terminal):
    backend = OllamaMiniLLMValidator(base_url="http://in-memory.invalid", timeout_s=.2)
    requests = []
    async def request(payload):
        requests.append(json.loads(payload))
        return json.dumps({"done": True, "done_reason": "stop", **terminal,
            "message": {"content": json.dumps({"decision": decision, "confidence": confidence})}}).encode()
    backend._request = request
    return ActivityRequestGate(OllamaActivityRequestClassifier(backend), timeout_s=.2), requests


def engine(*answers, structured=False):
    model = (JSONModel if structured else Model)(*answers)
    tts = TTS()
    value = BoundedConversationEngine(model, tts, Voice(), MockPlaybackController(), profile="mock",
                                      settings=Settings(profile="mock"))
    context = ConversationContext("activity-test", "canonical_product", "bear_dialogue", ("__all__",), (),
        8, 120, 4, profile=ProfileContext(age_band="6-7"))
    return value, context, model, tts


@pytest.mark.asyncio
@pytest.mark.parametrize("text,action", [
    ("Давай поиграем", '<action type="open_catalog" catalog="games"/>'),
    ("Во что можно поиграть?", '<action type="open_catalog" catalog="games"/>'),
    ("Какие игры у тебя есть?", '<action type="open_catalog" catalog="games"/>'),
    ("Не хочу сказки, хочу квиз", '<action type="offer_game" game_id="quiz"/>'),
    ("Поиграем в слова?", '<action type="offer_game" game_id="word_game"/>'),
    ("Хочу игру «Повтори огоньки»", '<action type="offer_game" game_id="repeat_pattern"/>'),
    ("Не хочу играть, лучше послушаю сказку", '<action type="open_catalog" catalog="stories"/>'),
    ("Хочу послушать сказку «Айболит»", '<action type="play_story" story_id="fixed_aibolit_v1"/>'),
])
async def test_positive_request_receipt_uses_current_utterance_and_exact_target(text, action):
    e, context, model, tts = engine(action)
    e.activity_request_gate, requests = classified()
    result = await e.run_text_turn(text, context)
    assert len(result.actions) == 1 and not result.chunks and not result.fallback_reason
    assert activity_request_matches(result.activity_request, text, result.actions[0], context)
    assert text not in repr(result.activity_request)
    assert not activity_request_matches(result.activity_request, text, result.actions[0], object())
    assert not tts.texts and not e.playback.played
    assert len(requests) == 1 and len(model.calls) == (0 if 'type="offer_game"' in action else 1) and context.turns == 0
    data = json.loads(requests[0]["messages"][1]["content"])
    assert data["utterance"] == text and data["target"] == result.activity_request.target
    assert set(data) == {"utterance", "current_request_evidence", "proposed_action", "target", "public_title", "public_aliases"}
    assert all(isinstance(alias, str) for alias in data["public_aliases"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Я не хочу играть. Давай поговорим про облака.", "Давай выберем другое действие",
    "Мама сказала «хочу квиз»", "Что такое квиз?", "Вчера я хотел поиграть",
    "Игра в слова мне знакома", "Я сказал слово игра, это не просьба играть",
])
@pytest.mark.parametrize("structured", [False, True])
async def test_rejected_action_repairs_original_input_once_before_any_pcm(text, structured):
    action = ('{"type":"open_catalog","catalog":"games"}' if structured else
              '<action type="open_catalog" catalog="games"/>')
    reply = "Облака состоят из капель воды."
    speech = json.dumps({"type": "say", "text": reply}, ensure_ascii=False) if structured else f"<say>{reply}</say>"
    e, context, model, tts = engine(action, speech, structured=structured)
    e.activity_request_gate, requests = classified("block")
    context.history = [("HISTORY_WANTED_GAME", "HISTORY_OFFERED_GAME")]
    result = await e.run_text_turn(text, context)
    assert not result.actions and not result.fallback_reason and result.chunks == (reply,)
    assert tts.texts == [reply] and len(e.playback.played) == 1
    assert len(requests) <= 1 and len(model.calls) == 2
    assert all(call[-1] == {"role": "user", "content": text} for call in model.calls)
    assert "SPEECH_FORM only" in model.calls[1][0]["content"]
    assert all("HISTORY_" not in request["messages"][1]["content"] for request in requests)
    assert context.turns == 1 and context.history[-1] == (text, reply)
    assert len(context.history) == 2
    assert len(e.metrics.completed_voice_turns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,confidence,terminal", [
    ("allow", .5, {}), ("review", 1., {}), ("allow", True, {}),
    ("allow", float("nan"), {}), ("allow", 1., {"done_reason": "length"}),
])
async def test_semantic_uncertainty_and_incomplete_decisions_are_not_receipts(decision, confidence, terminal):
    gate, _ = classified(decision, confidence, **terminal)
    action = ResponseTagParser().parse('<action type="offer_game" game_id="quiz"/>')[0]
    assert await gate.authorize("Хочу квиз", action, object()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "not-json", '[]', '{"decision":"allow","confidence":1,"extra":true}',
    '{"decision":"allow","confidence":1,"decision":"block"}',
    '{"decision":"allow","confidence":"1"}',
])
async def test_malformed_classifier_result_never_approves_navigation(content):
    backend = OllamaMiniLLMValidator(base_url="http://in-memory.invalid")
    async def request(payload):
        return json.dumps({"done": True, "done_reason": "stop", "message": {"content": content}}).encode()
    backend._request = request
    gate = ActivityRequestGate(OllamaActivityRequestClassifier(backend))
    action = ResponseTagParser().parse('<action type="offer_game" game_id="quiz"/>')[0]
    assert await gate.authorize("Хочу квиз", action, object()) is None


@pytest.mark.asyncio
async def test_missing_classifier_is_fail_closed_without_network_or_default_approval():
    e, context, model, tts = engine('<say>Я тебя слушаю.</say>')
    assert e.activity_request_gate.classifier is None
    result = await e.run_text_turn("Хочу квиз", context)
    assert not result.actions and result.chunks == ("Я тебя слушаю.",)
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text,action,pre_admission", [
    ("Хочу игру в слова", '<action type="offer_game" game_id="quiz"/>', True),
    ("Не хочу сказки, хочу квиз", '<action type="open_catalog" catalog="stories"/>', True),
    ("Хочу поиграть", '<action type="offer_game" game_id="quiz"/>', False),
    ("Мама читала «Айболит»", '<action type="play_story" story_id="fixed_aibolit_v1"/>', False),
])
async def test_semantically_wrong_target_never_gets_a_receipt(text, action, pre_admission):
    repair = '<say>Уточни, во что поиграем?</say>'
    e, context, model, tts = engine(*((repair,) if pre_admission else (action, repair)))
    e.activity_request_gate, requests = classified("block")
    result = await e.run_text_turn(text, context)
    assert not result.actions and not result.activity_request and not result.fallback_reason
    assert len(requests) <= 1 and len(model.calls) == (1 if pre_admission else 2)
    assert tts.texts == ["Уточни, во что поиграем?"]


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_cancellation_propagates_before_tts():
    entered = asyncio.Event()
    class Slow:
        async def classify(self, *args):
            entered.set()
            await asyncio.Event().wait()
    e, context, model, tts = engine('<action type="open_catalog" catalog="games"/>', '<say>Я тебя слушаю.</say>')
    e.activity_request_gate = ActivityRequestGate(Slow(), timeout_s=.02)
    started = time.monotonic()
    result = await e.run_text_turn("Поиграем?", context)
    assert time.monotonic() - started < 1.
    assert not result.actions and result.chunks == ("Я тебя слушаю.",) and len(model.calls) == 2
    assert len(e.metrics.completed_voice_turns) == 1
    trace = e.metrics.completed_voice_turns[0]
    assert trace["stage_durations_ms"]["activity_request"] >= 15
    assert trace["durations_ms"]["llm_to_tts_request_ms"] >= 15
    e, context, model, tts = engine('<action type="open_catalog" catalog="games"/>')
    entered.clear()
    e.activity_request_gate = ActivityRequestGate(Slow(), timeout_s=1.)
    task = asyncio.create_task(e.run_text_turn("Поиграем?", context))
    await asyncio.wait_for(entered.wait(), 1)
    assert not tts.texts and not e.playback.played
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not tts.texts and not context.history and context.turns == 0


@pytest.mark.asyncio
async def test_ordinary_speech_has_no_extra_classifier_hop_and_bad_repair_is_finite():
    e, context, model, tts = engine('<say>Я тебя слушаю.</say>')
    e.activity_request_gate, requests = classified()
    result = await e.run_text_turn("Привет", context)
    assert result.chunks and not requests and len(model.calls) == 1
    action = '<action type="open_catalog" catalog="games"/>'
    e, context, model, tts = engine(action, action)
    e.activity_request_gate, requests = classified("block")
    result = await e.run_text_turn("Привет", context)
    assert not result.actions and result.chunks == ("О чём поговорим?",)
    assert len(model.calls) == 2 and not requests and context.turns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("old_receipt", [False, True])
async def test_application_rejects_missing_or_old_turn_receipt(old_receipt):
    h = Harness()
    quick_public_audio(h)
    action = ResponseTagParser().parse('<action type="open_catalog" catalog="games"/>')[0]
    async def bypass(text, context):
        receipt = ActivityRequestReceipt(text, "open_catalog", "games", object()) if old_receipt else None
        return TurnResult((), (action,), False, activity_request=receipt)
    h.app.bounded_s2s.run_text_turn = bypass
    try:
        result = await h.send("SpeechReceived", text="Хочу поиграть")
        assert h.state.mode == "bear_dialogue" and h.state.pending_game_offer is None
        assert not h.app._game_callers and h.state.active_audio_id != "PHRASE_019"
        assert any(effect.get("fallback_reason") == "activity_request_unverified" for effect in result["outputs"])
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_authorized_semantics_preserve_real_nested_consent_and_return():
    h = Harness('<action type="offer_game" game_id="quiz"/>')
    quick_public_audio(h)
    h.app.bounded_s2s.activity_request_gate, requests = classified()
    try:
        assert (await h.send("SpeechReceived", text="Давай сыграем в квиз"))["reason"] == "game_offered"
        assert len(requests) == 1 and h.state.active_game_id is None
        await h.done()
        assert (await h.key(1))[0]["reason"] == "game_started"
        assert h.state.game_return_mode == "bear_dialogue"
        await h.key(7, duration=6.)
        assert h.state.mode == "bear_dialogue" and not h.app._game_callers
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_vad_stt_negative_conversation_is_repaired_without_game_navigation():
    reply = "Облака состоят из капель воды."
    text = "Я не хочу играть. Давай поговорим про облака."
    h = Harness('<say>Привет, я тебя слушаю.</say>', '<action type="open_catalog" catalog="games"/>',
                f'<say>{reply}</say>', band="6-7")
    quick_public_audio(h)
    h.app.bounded_s2s.activity_request_gate, requests = classified("block")
    try:
        await h.send("SpeechReceived", text="Привет")
        h.app.s2s_audio.recognizer = MockSpeechRecognizer(text, .99)
        h.seq += 1
        result = await h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq)
        assert result["reason"] == "dialogue_turn" and h.state.mode == "bear_dialogue"
        assert not h.app._game_callers and h.state.pending_game_offer is None
        assert len(requests) == 1 and len(h.model.calls) == 3
        context = h.app.conversation_contexts[h.state.session_id]
        assert context.turns == 2 and context.history[-1] == (text, reply)
        assert not any(command.get("response_id") in {"PHRASE_019", "DIALOGUE_OFFER"} for command in h.app.audio.commands)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_old_owner_cancelled_during_semantic_wait_cannot_publish_a_late_offer():
    entered, release = asyncio.Event(), asyncio.Event()
    class Delayed:
        async def classify(self, *args):
            entered.set()
            await release.wait()
            return True
    h = Harness('<action type="offer_game" game_id="quiz"/>')
    quick_public_audio(h)
    h.app.bounded_s2s.activity_request_gate = ActivityRequestGate(Delayed(), timeout_s=1.)
    task = asyncio.create_task(h.send("SpeechReceived", text="Хочу квиз"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await h.key(7)
        assert h.state.mode == "root_menu"
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert h.state.mode == "root_menu" and h.state.pending_game_offer is None
        assert not h.app._game_callers and h.state.active_audio_id != "DIALOGUE_OFFER"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_cancel_resistant_repair_hook_cannot_restart_over_the_new_turn():
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    e, old_context, _, tts = engine()
    class Replies:
        def __init__(self): self.old_calls = 0
        async def stream(self, messages, request_id, is_cancelled):
            if messages[-1]["content"] == "Старый вопрос":
                self.old_calls += 1
                yield ('<action type="open_catalog" catalog="games"/>' if self.old_calls == 1
                       else '<say>Старый ответ.</say>')
            else:
                yield '<say>Новый ответ.</say>'
    e.llm = Replies()
    e.activity_request_gate, _ = classified("block")
    processing = 0
    async def hook(sid, phase, **details):
        nonlocal processing
        if phase == "processing":
            processing += 1
            if processing == 2:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
    e.lifecycle_hook = hook
    old = asyncio.create_task(e.run_text_turn("Старый вопрос", old_context))
    new = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        new_context = ConversationContext(old_context.session_id, "canonical_product", "bear_dialogue", ("__all__",), (),
            8, 120, 4, profile=ProfileContext(age_band="6-7"))
        new = asyncio.create_task(e.run_text_turn("Новый вопрос", new_context))
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        old_result, new_result = await asyncio.wait_for(asyncio.gather(old, new, return_exceptions=True), 2)
        assert isinstance(old_result, asyncio.CancelledError)
        assert new_result.chunks == ("Новый ответ.",)
        assert tts.texts == ["Новый ответ."] and not old_context.history
        assert e.llm.old_calls == 1
    finally:
        release.set()
        await asyncio.gather(*(task for task in (old, new) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [
    '<action type="request_information" kind="weather"/>',
    '<say>Выбери другое занятие.</say><action type="return_to_scenario" target="root_menu"/>',
    '<action type="play_story" story_id="fixed_aibolit_v1"/>',
])
async def test_speech_only_repair_cannot_reintroduce_other_navigation(repair):
    e, context, model, tts = engine('<action type="open_catalog" catalog="games"/>', repair)
    e.activity_request_gate, requests = classified("block")
    result = await e.run_text_turn("Не хочу играть", context)
    assert not result.actions and result.chunks == ("О чём поговорим?",)
    assert tts.texts == ["О чём поговорим?"] and len(model.calls) == 2 and not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("text,action", [
    ('Я прочитал фразу «давай поиграем».', '<action type="open_catalog" catalog="games"/>'),
    ('В тексте было написано "хочу квиз".', '<action type="offer_game" game_id="quiz"/>'),
    ('Мама сказала «хочу сказку».', '<action type="open_catalog" catalog="stories"/>'),
    ("Я прочитал фразу 'давай поиграем'.", '<action type="open_catalog" catalog="games"/>'),
    ('Я сказал, что хочу «Квиз».', '<action type="offer_game" game_id="quiz"/>'),
    ('Хочу игру в слова.', '<action type="offer_game" game_id="quiz"/>'),
    ('Хочу викторину.', '<action type="offer_game" game_id="word_game"/>'),
    ('Хочу игру «Повтори огоньки».', '<action type="offer_game" game_id="free_lights"/>'),
    ('Не хочу викторину, хочу игру в слова.', '<action type="offer_game" game_id="quiz"/>'),
    ('Не хочу Айболита, хочу Телефон.', '<action type="play_story" story_id="fixed_aibolit_v1"/>'),
    ('Мне нравится сказка Айболит.', '<action type="play_story" story_id="fixed_aibolit_v1"/>'),
    ('Мне нравится играть.', '<action type="open_catalog" catalog="games"/>'),
    ('Мама сказала «хочу квиз», а я хочу поиграть.', '<action type="offer_game" game_id="quiz"/>'),
    ('Да.', '<action type="offer_game" game_id="quiz"/>'),
    ('Квиз.', '<action type="open_catalog" catalog="games"/>'),
    ('Она спрашивала, какие у тебя есть игры.', '<action type="open_catalog" catalog="games"/>'),
    ('В книге было «Какие у тебя есть игры?»', '<action type="open_catalog" catalog="games"/>'),
])
async def test_structural_constraints_reject_even_a_confident_wrong_semantic_allow(text, action):
    gate, requests = classified("allow")
    parsed = ResponseTagParser().parse(action)[0]
    assert await gate.authorize(text, parsed, object()) is None
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("text,action", [
    ('Я прочитал фразу «давай поиграем», но хочу квиз.', '<action type="offer_game" game_id="quiz"/>'),
    ('Не хочу викторину, хочу игру в слова.', '<action type="offer_game" game_id="word_game"/>'),
    ('Не хочу Айболита, хочу Телефон.', '<action type="play_story" story_id="fixed_neznayka_v1"/>'),
    ('Можно мне «Вопросы Мишки»?', '<action type="offer_game" game_id="quiz"/>'),
    ('Мне хочется игру в слова.', '<action type="offer_game" game_id="word_game"/>'),
    ('Я прочитал «Айболит», хочу «Телефон».', '<action type="play_story" story_id="fixed_neznayka_v1"/>'),
    ('Хочу не квиз, а слова.', '<action type="offer_game" game_id="word_game"/>'),
    ('Он сказал «хочу играть», а я хочу послушать сказку.', '<action type="open_catalog" catalog="stories"/>'),
    ('Включи «Телефон».', '<action type="play_story" story_id="fixed_neznayka_v1"/>'),
    ('Квиз.', '<action type="offer_game" game_id="quiz"/>'),
    ('Игра в слова', '<action type="offer_game" game_id="word_game"/>'),
    ('Какие у тебя есть игры?', '<action type="open_catalog" catalog="games"/>'),
    ('Какие у тебя сказки можно послушать?', '<action type="open_catalog" catalog="stories"/>'),
    ('Я не прочь поиграть.', '<action type="open_catalog" catalog="games"/>'),
    ('Я не против послушать сказку.', '<action type="open_catalog" catalog="stories"/>'),
])
async def test_current_positive_clause_and_quoted_titles_still_require_semantics(text, action):
    parsed = ResponseTagParser().parse(action)[0]
    gate, requests = classified("allow")
    assert await gate.authorize(text, parsed, object()) is not None
    assert len(requests) == 1
    blocked, _ = classified("block")
    assert await blocked.authorize(text, parsed, object()) is None


@pytest.mark.asyncio
async def test_repeated_bare_game_name_offers_but_only_current_heard_yes_launches():
    action = '<action type="offer_game" game_id="quiz"/>'
    h = Harness(action, action)
    quick_public_audio(h)
    h.app.bounded_s2s.activity_request_gate, requests = classified()
    try:
        assert (await h.send("SpeechReceived", text="Квиз"))["reason"] == "game_offered"
        first = h.state.game_offer_id
        await h.send("SpeechReceived", text="Да")
        assert h.state.mode == "bear_dialogue" and h.state.active_game_id is None
        await h.done()
        assert (await h.send("SpeechReceived", text="Квиз"))["reason"] == "game_offered"
        assert h.state.game_offer_id != first and not h.state.game_offer_ready
        await h.send("SpeechReceived", text="Да")
        assert h.state.mode == "bear_dialogue" and h.state.active_game_id is None
        await h.done()
        assert (await h.send("SpeechReceived", text="Да"))["reason"] == "game_started"
        assert h.state.mode == "game_active" and h.state.active_game_id == "quiz"
        assert len(requests) == 2 and len(h.model.calls) == 0
    finally:
        await h.close()
