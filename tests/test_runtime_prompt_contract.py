"""Effective model instructions agree with strict mode and age admission."""
import json

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import (BoundedConversationEngine, ConversationContext, MockPlaybackController,
    MockTTSProvider, ProfileContext, RVCProvider, S2SConfig)
from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator
from test_contextual_game_flow import Harness, Model, ApprovedActivityRequests, WORD_GAME_WORDS
from app.foundation.activity_requests import ActivityRequestGate


async def young_guest(app):
    """Use accepted onboarding events, not a manufactured age-bearing state."""
    sid, sequence, clock = 'synthetic-young-prompt', 0, [10.]
    app.monotonic_now = app.canonical_runtime.monotonic_now = lambda:clock[0]
    async def send(kind, payload=None, text=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, sid, sequence, 1., 'local_guest', payload or {},
            f'guest:{sequence}', clock[0]), text)
    async def done():
        lease = app._active_audio[sid]
        return await send('AudioCompleted', {'response_id':lease['content_id'],
            **{key:lease[key] for key in ('token','generation','playback_id','playback_lease')}})
    async def key(button):
        await send('ButtonPressed', {'button_id':button})
        await send('ButtonReleased', {'button_id':button})
    await send('HandsetLifted')
    clock[0] = 15.
    await app.flush_welcome_timers()
    for _ in range(3):
        await done()
    await send('SpeechReceived', text='Меня зовут Аня')
    await done()
    await done()
    await key(2)
    await done()
    await send('ExhibitionOnboardingComplete', {'token':app.sessions.get(sid).cancellation_token})
    await key(6)  # Accepted tutorial skip, then a fresh conversation entry.
    await key(6)
    assert app.canonical_runtime.sessions[sid].mode == 'bear_dialogue'
    if app.canonical_runtime.sessions[sid].active_audio_id:
        await done()
    return sid, send


@pytest.mark.asyncio
@pytest.mark.parametrize('extra,verdict', [(False,'allow'), (True,'allow'), (False,'block')])
async def test_actual_guest_effective_prompt_keeps_age_cap_and_mandatory_semantics(extra, verdict):
    app, *_ = build_application(settings=Settings(profile='mock'), background_scheduler=False)
    requests = []
    mini = OllamaMiniLLMValidator()
    async def request(payload):
        requests.append(payload)
        return json.dumps({'message':{'content':json.dumps({'decision':verdict,'confidence':1.})}}).encode()
    mini._request_bytes = request
    try:
        sid, send = await young_guest(app)
        app.bounded_s2s.response_validator = AsyncResponseValidator(mini)
        answer = ('Я живу в добром волшебном лесу рядом с рекой.' if extra
                  else 'Я живу в добром лесу, рядом с рекой.')
        model = Model('<say>'+answer+'</say>')
        app.bounded_s2s.llm = model
        await send('SpeechReceived', text='Мишка, где ты живёшь?')
        context = app._canonical_audio_context(sid)
        assert (context.profile.age,context.profile.age_band,context.profile.age_source) == (2,'1-3','exhibition_button')
        assert context.max_turns == 4
        prompt = model.calls[0][0]['content']
        final = prompt.split('NON_NEGOTIABLE_RUNTIME_OUTPUT:',1)[1]
        assert 'Choose exactly ONE output form' in final
        assert 'YOUNG_SPEECH: one complete thought in one Russian sentence' in final
        assert 'aim for 3-6 words' in final and 'at most 8 words total' in final
        assert 'plus at most the allowlisted' not in final
        failures = [event.get('reason') for event in app.bounded_s2s.metrics.events
                    if event.get('event') == 'validation_failure']
        assert ('age_word_budget_exceeded' in failures) == extra
        assert bool(requests) is not extra  # Only in-budget speech reaches mandatory semantics.
        if verdict == 'block':
            assert any(event.get('event') == 'validation_block' for event in app.bounded_s2s.metrics.events)
            assert not app.bounded_s2s.playback.played
    finally:
        await app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['bear_dialogue','support_intake','game_active'])
@pytest.mark.parametrize('response', ['<say>Я слушаю тебя.</say>',
    '<say>Я слушаю тебя.</say><action type="led" pattern="calm"/>',
    '<say>Я слушаю тебя.</say><action type="return_to_scenario" target="root_menu"/>',
    '<action type="offer_game" game_id="quiz"/>',
    '<action type="open_catalog" catalog="games"/>',
    '<action type="play_story" story_id="fixed_aibolit_v1"/>',
    '<action type="request_information" kind="date"/>',
    '<say>Давай играть.</say><action type="offer_game" game_id="quiz"/>'])
async def test_effective_mode_format_matches_unchanged_parser_authority(mode, response):
    model = Model(response)
    engine = BoundedConversationEngine(model,MockTTSProvider(),RVCProvider(),MockPlaybackController(),
        config=S2SConfig(),profile='mock')
    engine.activity_request_gate = ActivityRequestGate(ApprovedActivityRequests())
    context = ConversationContext('prompt-mode','canonical_product',mode,('__all__',),('diagnosis',),4,120,2,
        profile=ProfileContext(age=2,age_band='1-3'))
    try:
        result = await engine.run_text_turn('Хочу поиграть, что предложишь?',context)
        prompt = model.calls[0][0]['content']
        final = prompt.split('NON_NEGOTIABLE_RUNTIME_OUTPUT:',1)[1]
        assert ('Choose exactly ONE output form' in final) == (mode == 'bear_dialogue')
        assert ('Catalogue control actions are forbidden in this mode' in final) == (mode != 'bear_dialogue')
        catalogue = any(f'type="{kind}"' in response for kind in
                        ('offer_game','open_catalog','play_story','request_information'))
        allowed = response.startswith('<say>') and not catalogue
        allowed = allowed or (mode == 'bear_dialogue' and response.startswith('<action'))
        assert bool(result.fallback_reason) is not allowed
        if not allowed:
            assert not result.actions
    finally:
        await engine.cancel_session(context.session_id)


@pytest.mark.asyncio
async def test_young_word_game_final_turn_keeps_one_sentence_and_real_completion():
    from test_conversation_catalogue_gateway import quick_public_audio
    h = Harness(band='1-3')
    render = quick_public_audio(h)
    try:
        await h.launch('word_game')
        render.texts.clear()
        for word in WORD_GAME_WORDS['1-3']:
            await h.send('SpeechReceived',text=word)
        assert len(render.texts) == 4
        assert all(len(text.split()) <= 8 and text.count('.') == 1 for text in render.texts)
        assert 'разговору' in render.texts[-1] and '?' not in render.texts[-1]
        assert len(h.model.calls) == 1  # No generator may override the rules.
        assert h.state.mode == 'bear_dialogue' and h.state.active_game_id is None
        assert not h.app._game_callers
    finally:
        await h.close()
