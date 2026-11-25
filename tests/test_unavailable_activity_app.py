"""Actual app/menu/game ownership; provider and semantic classifier labels are fixtures."""
import asyncio
from copy import copy
from dataclasses import replace
import json
import pytest
from test_stage1_nested_game_integrity import NestedGame,CALLER_TEXT
from test_game_conversation_memory import game_voice
from test_activity_request_gate import classified

async def conversation():
    h=NestedGame();game_voice(h)
    await h.send('HandsetLifted');await h.key(6);await h.terminal()
    await h.send('SpeechReceived',text=CALLER_TEXT)
    h.app.bounded_s2s.activity_request_gate,_=classified('allow')
    return h

@pytest.mark.asyncio
async def test_explicit_unavailable_request_has_owned_honest_notice_before_real_chooser():
    h=await conversation()
    try:
        from app.foundation.s2s import AudioChunk
        # This acceptance owns navigation/PCM leases; recorded-asset DSP has its own family.
        h.app._load_game_asset=lambda phrase_id,token: AudioChunk((.1,)*2400,24000,token)
        context=h.app.conversation_contexts[h.sid]
        history,turns,started=list(context.history),context.turns,context.started_at
        await h.send('SpeechReceived',text='Давай загадки')
        assert h.state.active_audio_id=='ACTIVITY_UNAVAILABLE'
        assert h.state.mode=='bear_dialogue' and h.state.game_engine_state is None
        assert any('Загадки пока недоступны' in text for text in h.render.texts)
        await h.key(1)
        assert h.state.mode=='bear_dialogue' and h.state.active_audio_id=='ACTIVITY_UNAVAILABLE'
        await h.terminal()
        assert h.state.mode=='game_menu' and h.state.activity_return_mode=='bear_dialogue'
        caller=h.app._game_callers[h.sid]['context']
        assert caller.history==[*history,('Давай загадки','')]
        assert caller.turns==turns and caller.started_at==started
        assert not hasattr(caller,'_pending_activity_notice')
        assert h.state.active_audio_id=='PHRASE_019'
        await h.terminal()
        assert h.state.active_audio_id=='MENU_GAME'
        slot=next(item['slot'] for item in h.state.scene_menu_descriptor['items'] if item['id']=='free_lights')
        await h.key(slot)
        assert h.state.active_game_id=='free_lights' and h.state.game_return_mode=='bear_dialogue'
        await h.terminal()
        assert h.state.active_audio_id=='GAME_PROMPT'
        await h.terminal()
        assert h.state.game_audio_stage=='playing'
        await h.key(7,duration=6.)
        assert h.state.mode=='bear_dialogue' and h.app.conversation_contexts[h.sid] is caller
        assert caller.history==[*history,('Давай загадки','')] and caller.turns==turns
        await h.terminal()
        h.model.answers.append('<say>Можно нарисовать коту усы.</say>')
        await h.send('SpeechReceived',text='Что ещё нарисовать коту?')
        assert CALLER_TEXT in h.model.calls[-1][0]['content']
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['AudioCompleted','AudioFailed'])
async def test_notice_terminal_requires_current_lease_and_opens_authorized_chooser_once(terminal):
    h=await conversation()
    try:
        result=await h.send('SpeechReceived',text='Давай загадки')
        owner=dict(h.app._active_audio[h.sid]);version=h.state.state_version
        public=json.dumps(result,ensure_ascii=False)
        assert 'activity_notice_target' not in public and '_pending_activity_notice' not in public
        assert 'Давай загадки' not in public
        await h.terminal(terminal,{**owner,'playback_lease':'wrong-lease'})
        assert h.state.state_version==version and h.state.active_audio_id=='ACTIVITY_UNAVAILABLE'
        await h.terminal(terminal,owner)
        assert h.state.mode=='game_menu' and h.state.active_audio_id=='PHRASE_019'
        assert not hasattr(h.app._game_callers[h.sid]['context'],'_pending_activity_notice')
        current=dict(h.app._active_audio[h.sid]);version=h.state.state_version
        assert (await h.terminal(terminal,owner))['reason']=='stale_audio_callback'
        assert h.state.state_version==version and h.app._active_audio[h.sid]==current
    finally:await h.close()

@pytest.mark.asyncio
async def test_cancel_notice_keeps_dialogue_and_next_request_has_a_fresh_lease():
    h=await conversation()
    try:
        await h.send('SpeechReceived',text='Давай загадки')
        old=dict(h.app._active_audio[h.sid]);context=h.app.conversation_contexts[h.sid]
        assert (await h.key(7))[0]['reason']=='activity_notice_cancelled'
        assert h.state.mode=='bear_dialogue' and h.state.active_audio_id is None
        assert h.app.runtime_status['dialogue_lighting_phase']=='listening'
        assert h.state.activity_notice_target is None and not hasattr(context,'_pending_activity_notice')
        assert h.app.conversation_contexts[h.sid] is context
        await h.send('SpeechReceived',text='Хочу поиграть в загадки')
        current=dict(h.app._active_audio[h.sid])
        assert current['playback_lease']!=old['playback_lease']
        assert (await h.terminal(owner=old))['reason']=='stale_audio_callback'
        assert h.app._active_audio[h.sid]==current
        await h.terminal()
        assert h.state.mode=='game_menu'
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['help','end','hangup'])
async def test_notice_priority_cancel_and_new_visit_cannot_reopen_old_chooser(ending):
    h=await conversation()
    try:
        await h.send('SpeechReceived',text='Давай загадки')
        old=dict(h.app._active_audio[h.sid]);context=h.app.conversation_contexts[h.sid]
        if ending=='help':await h.key(5)
        elif ending=='end':await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        else:await h.send('HandsetPlaced')
        assert h.state.activity_notice_target is None and not hasattr(context,'_pending_activity_notice')
        version,mode=h.state.state_version,h.state.mode
        assert (await h.terminal(owner=old))['reason']=='stale_audio_callback'
        assert (h.state.state_version,h.state.mode)==(version,mode)
        assert h.state.mode!='game_menu' and not h.app._game_callers
        if ending=='end':
            await h.app.session_action(h.sid,'start',ownership_guard=lambda:True)
            version,mode=h.state.state_version,h.state.mode
            await h.terminal(owner=old)
            assert (h.state.state_version,h.state.mode)==(version,mode)
            assert h.state.activity_notice_target is None
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('revocation',['context','catalogue'])
async def test_notice_rechecks_authority_until_the_first_actual_chooser(monkeypatch,revocation):
    from app.foundation import activity_requests
    h=await conversation()
    try:
        await h.send('SpeechReceived',text='Давай загадки')
        if revocation=='context':
            h.app.conversation_contexts[h.sid]=copy(h.app.conversation_contexts[h.sid])
        else:
            spec=activity_requests.IMMERSIVE_GAME_SPECS[0]
            monkeypatch.setattr(activity_requests,'IMMERSIVE_GAME_SPECS',
                (*activity_requests.IMMERSIVE_GAME_SPECS,replace(spec,game_id='riddles',display_title='Загадки')))
        await h.terminal()
        assert h.state.mode=='bear_dialogue' and h.state.active_audio_id is None
        assert not h.app._game_callers and h.state.activity_notice_target is None
        assert not hasattr(h.app.conversation_contexts[h.sid],'_pending_activity_notice')
    finally:await h.close()

@pytest.mark.asyncio
async def test_unavailable_request_supersedes_a_ready_supported_offer_without_phantom_consent():
    h=await conversation()
    try:
        h.model.answers.append('<action type="offer_game" game_id="quiz"/>')
        await h.send('SpeechReceived',text='Предложи викторину')
        assert h.state.pending_game_offer=='quiz'
        offer=dict(h.app._active_audio[h.sid]);await h.terminal()
        assert h.state.game_offer_ready
        await h.send('SpeechReceived',text='Давай загадки')
        assert h.state.active_audio_id=='ACTIVITY_UNAVAILABLE'
        assert h.state.pending_game_offer is None and h.state.game_offer_id is None and not h.state.game_offer_ready
        await h.key(7)
        await h.terminal(owner=offer)
        await h.send('SpeechReceived',text='Давай')
        assert h.state.mode=='bear_dialogue' and h.state.game_engine_state is None
        assert h.state.pending_game_offer is None and not h.app._game_callers
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('failed',[False,True])
async def test_native_notice_gate_rejects_old_pcm_then_accepts_fresh_voice_after_terminal(failed):
    from app.foundation.config import Settings
    from app.foundation.s2s import STTResult
    from app.foundation.stand import HardwareIntegrationStand
    from test_game_audio_reminder_race import SPEECH,SILENCE
    h=await conversation();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application;h.app.led=stand.application.led;calls=[]
    class Recognizer:
        async def transcribe(self,samples,cancelled):
            calls.append(True)
            return STTResult('Мне нравится лес',.99,0.,False,{})
    try:
        stand.application=h.app;stand.active_session_id=h.sid
        h.app.s2s_audio.recognizer=Recognizer()
        h.model.answers.append('<say>Давай поиграем в загадки?</say>')
        await h.send('SpeechReceived',text='Расскажу про лес')
        assert h.state.active_audio_id=='ACTIVITY_UNAVAILABLE'
        assert h.app.runtime_status['dialogue_lighting_phase']=='output'
        assert not stand._headless_audio_accepting()
        blocked=stand._headless_frame_owner();assert not blocked[1]()
        old=dict(h.app._active_audio[h.sid])
        await h.terminal('AudioFailed' if failed else 'AudioCompleted')
        assert stand._headless_audio_accepting() and not blocked[1]()
        assert h.app.runtime_status['dialogue_lighting_phase']=='listening'
        # A stale playback dictionary cannot close the fresh canonical listener.
        h.app._active_audio[h.sid]=old
        h.app._active_audio['foreign']=old
        assert stand._headless_audio_accepting()
        h.app._active_audio.pop(h.sid)
        while stand.device_sequence<h.app.sessions.get(h.sid).last_sequence:
            await stand.inject('heartbeat',{},h.sid)
        stand._headless_audio_queue=asyncio.Queue(maxsize=64)
        owner=stand._headless_frame_owner();assert owner[1]()
        stand._enqueue_headless_audio_frame(SPEECH+SILENCE,blocked)
        stand._enqueue_headless_audio_frame(SPEECH+SILENCE,owner)
        await stand._headless_audio_queue.put(None);await stand._headless_audio_worker()
        tasks=tuple(h.app._audio_completions.get(h.sid,()))
        if tasks:await asyncio.wait_for(asyncio.gather(*tasks),2)
        assert len(calls)==1 and h.state.mode=='bear_dialogue'
        assert 'Мне нравится лес' in h.app.conversation_contexts[h.sid].history[-1][0]
    finally:
        h.app._active_audio.pop('foreign',None)
        stand.application=original;await h.close();await stand.aclose()

@pytest.mark.asyncio
async def test_notice_synthesis_window_closes_capture_and_end_revokes_late_pcm():
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h=await conversation();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application;h.app.led=stand.application.led
    entered,release=asyncio.Event(),asyncio.Event();pending=None
    class LateVoice:
        async def synthesize(self,text,token,cancelled):
            audio=await h.render.synthesize(text,token,cancelled)
            if 'Загадки пока недоступны' in text:
                entered.set()
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
            return audio
    try:
        stand.application=h.app;stand.active_session_id=h.sid
        h.app.bounded_s2s.tts=LateVoice()
        pending=asyncio.create_task(h.send('SpeechReceived',text='Давай загадки'))
        await asyncio.wait_for(entered.wait(),2)
        assert h.state.active_audio_id=='ACTIVITY_UNAVAILABLE' and h.sid not in h.app._active_audio
        assert not stand._headless_audio_accepting()
        await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        version,mode=h.state.state_version,h.state.mode
        release.set();await asyncio.wait_for(pending,2)
        assert (h.state.state_version,h.state.mode)==(version,mode)
        assert h.sid not in h.app._active_audio and not h.app._game_callers
        assert h.state.activity_notice_target is None
    finally:
        release.set()
        if pending and not pending.done():pending.cancel()
        stand.application=original;await h.close();await stand.aclose()

@pytest.mark.asyncio
async def test_notice_output_is_validated_and_denial_keeps_a_real_button_chooser():
    from app.foundation.validation import ValidationDecision
    h=await conversation();checked=[]
    validator=h.app.bounded_s2s.response_validator
    class BlockedNotice:
        async def validate(self,text):
            checked.append(text)
            if text.startswith('Загадки пока недоступны'):
                return ValidationDecision('block',False,'fixture',1.,'fixture')
            return await validator.validate(text)
        async def validate_trusted(self,text):return await validator.validate_trusted(text)
    try:
        before=len(h.render.texts)
        h.app.bounded_s2s.response_validator=BlockedNotice()
        await h.send('SpeechReceived',text='Давай загадки')
        assert checked==['Загадки пока недоступны. Можно выбрать другую игру.']
        assert not any('Загадки пока недоступны' in text for text in h.render.texts[before:])
        assert h.state.mode=='game_menu' and h.state.game_engine_state is None
        assert 1 in h.state.scene_menu_descriptor['buttons']
        assert h.state.activity_notice_target is None
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('text',['Я не хочу загадки','Мама сказала: «Давай загадки»','Что такое загадки?'])
async def test_discussion_negation_or_quotation_is_not_a_child_activity_request(text):
    h=await conversation()
    try:
        await h.send('SpeechReceived',text=text)
        assert h.state.mode=='bear_dialogue' and h.state.activity_notice_target is None
        assert h.state.pending_game_offer is None and h.state.game_engine_state is None
        assert not any('Загадки пока недоступны' in text for text in h.render.texts)
    finally:await h.close()

@pytest.mark.asyncio
async def test_client_payload_cannot_authorize_an_unavailable_activity_notice_or_chooser():
    h=await conversation()
    try:
        version=h.state.state_version
        result=await h.send('DialogueFallback',{'reason':'catalogue_request','action':'open_catalog',
            'target':'games','notice_code':'riddles_unavailable','activity_notice_choice_authorized':True})
        assert result['reason']=='dialogue_fallback_untrusted'
        assert h.state.state_version==version and h.state.activity_notice_target is None
        assert h.state.mode=='bear_dialogue' and not h.app._game_callers
    finally:await h.close()

@pytest.mark.asyncio
async def test_unsolicited_unsupported_model_offer_stays_same_conversation_without_authority():
    h=await conversation()
    try:
        before=h.app.conversation_contexts[h.sid]
        history,turns,started=list(before.history),before.turns,before.started_at
        h.model.answers.append('<say>Давай поиграем в загадки?</say>')
        await h.send('SpeechReceived',text='Мне нравится лес')
        assert h.state.active_audio_id=='ACTIVITY_UNAVAILABLE'
        assert h.state.mode=='bear_dialogue' and h.state.pending_game_offer is None
        assert h.state.game_engine_state is None
        context=h.app.conversation_contexts[h.sid]
        assert context.history==history and context.turns==turns and context.started_at==started
        await h.terminal()
        assert h.state.mode=='bear_dialogue' and h.app.conversation_contexts[h.sid] is context
        assert h.state.active_audio_id is None
        await h.send('SpeechReceived',text='Давай')
        assert h.state.mode=='bear_dialogue' and h.state.pending_game_offer is None
    finally:await h.close()
