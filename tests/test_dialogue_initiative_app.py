"""Unified initiative lifecycle; actual app/owned PCM with synthetic model/ASR labels."""
import json
import pytest
from test_unavailable_activity_app import conversation
from test_activity_request_gate import JSONModel


def proposal(kind='riddle',target=None,text='Хочешь простую загадку?'):
    return json.dumps({'type':'say_initiative','text':text,'kind':kind,
        'target':target or kind,'phase':'offer','answer':''},ensure_ascii=False)


def content(text='Кто мяукает и любит молоко?',answer='кот'):
    return json.dumps({'type':'say_initiative','text':text,'kind':'riddle',
        'target':'riddle','phase':'active','answer':answer},ensure_ascii=False)


def model(h,*answers):
    h.model=JSONModel(*answers);h.app.bounded_s2s.llm=h.model


@pytest.mark.asyncio
async def test_proactive_offer_is_owned_then_one_voice_yes_executes_real_content_in_same_bear():
    h=await conversation()
    try:
        model(h,proposal(),content())
        await h.send('SpeechReceived',text='Мне нравится лес')
        assert h.state.mode=='bear_dialogue' and h.state.active_audio_id=='INITIATIVE_OFFER'
        assert h.state.pending_game_offer is None and not h.app._game_callers
        assert 'button_1' not in h.state.expected_inputs
        assert sum(text.startswith('Хочешь простую загадку?') for text in h.render.texts)==1
        assert 'Скажи «да» или нажми один. Семь — продолжить разговор.' in h.render.texts[-1]
        await h.terminal()
        assert {'button_1','button_5','button_7','speech'}<=set(h.state.expected_inputs)
        await h.send('SpeechReceived',text='Да')
        assert h.state.mode=='bear_dialogue' and h.state.game_engine_state is None
        assert any('Кто мяукает' in text for text in h.render.texts)
        assert h.app.conversation_contexts[h.sid].initiative.phase=='active'
        assert sum(text.startswith('Хочешь простую загадку?') for text in h.render.texts)==1
    finally:await h.close()


@pytest.mark.asyncio
async def test_direct_riddle_request_executes_inline_content_without_catalogue_or_extra_question():
    h=await conversation()
    try:
        model(h,content())
        await h.send('SpeechReceived',text='Загадай загадку')
        assert h.state.mode=='bear_dialogue' and h.state.active_audio_id is None
        assert h.state.game_engine_state is None and not h.app._game_callers
        assert any('Кто мяукает' in text for text in h.render.texts)
        assert not any('недоступны' in text for text in h.render.texts)
    finally:await h.close()


@pytest.mark.asyncio
async def test_failed_offer_cannot_arm_voice_or_button_consent():
    h=await conversation()
    try:
        model(h,proposal())
        await h.send('SpeechReceived',text='Мне нравится лес')
        assert h.state.active_audio_id=='INITIATIVE_OFFER'
        await h.terminal('AudioFailed')
        assert h.state.mode=='bear_dialogue' and 'button_1' not in h.state.expected_inputs
        assert h.app.conversation_contexts[h.sid].initiative is None
        await h.key(1);await h.send('SpeechReceived',text='Да')
        assert h.state.mode=='bear_dialogue' and h.state.game_engine_state is None
        assert h.app.conversation_contexts[h.sid].initiative is None
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,reply',[('joke','Почему кот сел за компьютер? Он искал мышку!'),
    ('wordplay','Давай придумаем рифму к слову кот: рот.')])
@pytest.mark.parametrize('button',[False,True])
async def test_one_inline_contract_supports_voice_and_nonverbal_button_consent(kind,reply,button):
    h=await conversation()
    try:
        model(h,proposal(kind,text='Хочешь '+('шутку?' if kind=='joke' else 'словесную игру?')),
            json.dumps({'type':'say_initiative','kind':kind,'target':kind,'phase':'complete','text':reply,'answer':''},ensure_ascii=False))
        await h.send('SpeechReceived',text='Мне нравится лес');await h.terminal()
        before=h.app.conversation_contexts[h.sid].turns
        if button:await h.key(1)
        else:await h.send('SpeechReceived',text='Да')
        assert h.state.mode=='bear_dialogue' and h.state.game_engine_state is None
        assert reply in h.render.texts and h.app.conversation_contexts[h.sid].turns==before+1
        assert h.state.pending_initiative is None and h.state.pending_game_offer is None
    finally:await h.close()


@pytest.mark.asyncio
async def test_early_held_accept_does_not_answer_then_a_fresh_button_executes_once():
    h=await conversation()
    try:
        model(h,proposal(),content())
        await h.send('SpeechReceived',text='Мне нравится лес')
        owner=dict(h.app._active_audio[h.sid])
        await h.send('ButtonPressed',{'button_id':1})
        await h.terminal(owner={**owner,'playback_lease':'foreign'})
        assert not h.state.pending_initiative.ready
        await h.terminal(owner=owner)
        await h.send('ButtonPressed',{'button_id':1})
        assert len(h.model.calls)==1
        await h.send('ButtonReleased',{'button_id':1});await h.key(1)
        assert len(h.model.calls)==2 and h.state.pending_initiative is None
        assert sum('Кто мяукает' in text for text in h.render.texts)==1
        await h.key(1)
        assert len(h.model.calls)==2
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['decline','help','end','hangup'])
async def test_current_proposal_clears_on_real_controls_and_old_pcm_cannot_rearm(ending):
    h=await conversation()
    try:
        model(h,proposal());await h.send('SpeechReceived',text='Мне нравится лес')
        owner=dict(h.app._active_audio[h.sid]);await h.terminal()
        if ending=='decline':await h.key(7)
        elif ending=='help':await h.key(5)
        elif ending=='end':await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        else:await h.send('HandsetPlaced')
        assert h.state.pending_initiative is None
        context=h.app.conversation_contexts.get(h.sid)
        assert context is None or context.initiative is None
        mode,version=h.state.mode,h.state.state_version
        await h.terminal(owner=owner)
        assert (h.state.mode,h.state.state_version)==(mode,version)
        if ending=='decline':
            assert h.state.mode=='bear_dialogue'
            await h.send('SpeechReceived',text='Да')
            assert not any('Кто мяукает' in text for text in h.render.texts)
    finally:await h.close()


@pytest.mark.asyncio
async def test_active_riddle_answer_hint_and_stop_continue_the_same_conversation():
    h=await conversation()
    try:
        model(h,content(),content('Он говорит мяу и умеет мурлыкать.'),
            content('Верно, это кот!',answer='кот').replace('"active"','"complete"'),'<say>Хорошо, продолжим разговор.</say>')
        await h.send('SpeechReceived',text='Загадай загадку')
        await h.send('SpeechReceived',text='Помоги')
        assert h.state.mode=='bear_dialogue' and h.app.conversation_contexts[h.sid].initiative.answer=='кот'
        await h.send('SpeechReceived',text='Кот')
        assert 'Верно, это кот!' in h.render.texts
        assert h.state.mode=='bear_dialogue' and h.app.conversation_contexts[h.sid].initiative is None
        model(h,content(),'<say>Хорошо, продолжим разговор.</say>')
        await h.send('SpeechReceived',text='Загадай загадку');await h.send('SpeechReceived',text='Стоп')
        assert h.state.mode=='bear_dialogue' and h.app.conversation_contexts[h.sid].initiative is None
        assert not h.app._game_callers
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,target',[('game','quiz'),('story','fixed_aibolit_v1')])
@pytest.mark.parametrize('button',[False,True])
async def test_current_external_consent_dispatches_once_and_restores_real_caller(tmp_path,kind,target,button):
    from app.foundation.s2s import AudioChunk
    from story_catalogue_fixtures import tiny_stories
    h=await conversation()
    try:
        h.app.canonical_runtime.stories=tiny_stories(tmp_path)
        h.app._load_game_asset=lambda phrase_id,token:AudioChunk((.1,)*2400,24000,token)
        before=h.app.conversation_contexts[h.sid];history=list(before.history);turns=before.turns
        question='Хочешь викторину Вопросы Мишки?' if kind=='game' else 'Хочешь послушать Айболита?'
        model(h,proposal(kind,target,question))
        result=await h.send('SpeechReceived',text='Мне нравится лес')
        assert h.state.active_audio_id=='INITIATIVE_OFFER',[(x.get('fallback_reason'),x.get('resolution')) for x in result.get('outputs',())]
        await h.terminal()
        assert h.app.conversation_contexts[h.sid].initiative.ready
        if button:await h.key(1)
        else:await h.send('SpeechReceived',text='Да')
        assert len(h.model.calls)==1  # Execution uses the consent receipt, without a second provider offer.
        caller=h.app._game_callers[h.sid]['context']
        assert caller.history[:len(history)]==history and caller.turns==turns and caller.initiative is None
        assert h.state.pending_game_offer is None and h.state.pending_initiative is None
        if kind=='game':
            assert h.state.mode=='game_active' and h.state.active_game_id==target
            assert h.state.active_audio_id=='PHRASE_020'
            await h.terminal();await h.terminal()
            assert h.state.game_audio_stage=='playing'
            await h.key(7,duration=6.)
        else:
            assert h.state.mode=='fixed_story_playing' and h.state.active_story_id==target
            await h.key(7)
        assert h.state.mode=='bear_dialogue' and h.app.conversation_contexts[h.sid] is caller
        assert caller.turns==turns and caller.initiative is None
    finally:await h.close()


async def pcm(h,text,*,pending=False,guard=None,samples=None):
    import asyncio
    from app.foundation.s2s import MockSpeechRecognizer
    from test_game_audio_reminder_race import SPEECH,SILENCE
    h.app.s2s_audio.recognizer=MockSpeechRecognizer(text,.99)
    h.sequence=max(h.sequence,h.app.sessions.get(h.sid).last_sequence)+1
    result=await h.app.receive_audio_async(h.sid,SPEECH+SILENCE if samples is None else samples,
        h.sequence,wait_for_turn=not pending,_ingress_guard=guard)
    tasks=tuple(h.app._audio_completions.get(h.sid,()))
    if tasks:await asyncio.wait_for(asyncio.gather(*tasks),2)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('urgent',[False,True])
async def test_first_buffered_pcm_before_ready_never_acquires_new_consent_but_urgent_preempts(urgent):
    from test_game_audio_reminder_race import SPEECH,SILENCE
    h=await conversation()
    try:
        model(h,proposal(),content());await h.send('SpeechReceived',text='Мне нравится лес')
        first=await pcm(h,'Да',samples=SPEECH)
        assert first['audio_status']=='collecting' and h.app.s2s_audio.ingress[h.sid].active
        await h.terminal()
        answer='Меня бьют' if urgent else 'Да'
        result=await pcm(h,answer,samples=SILENCE,pending=True)
        if urgent:
            assert h.state.mode=='safety_stop' and h.state.safety_severity=='urgent'
            assert h.state.pending_initiative is None
        else:
            assert h.state.mode=='bear_dialogue' and h.state.pending_initiative.ready
            assert len(h.model.calls)==1 and not any('Кто мяукает' in text for text in h.render.texts)
            await pcm(h,'Да')
            assert any('Кто мяукает' in text for text in h.render.texts)
    finally:await h.close()


@pytest.mark.asyncio
async def test_live_proposal_is_reserved_before_a_detached_execution_await():
    import asyncio
    h=await conversation();entered,release=asyncio.Event(),asyncio.Event();pending=None
    original=h.app.bounded_s2s.llm
    try:
        model(h,proposal());await h.send('SpeechReceived',text='Мне нравится лес');await h.terminal()
        nonce=h.app.conversation_contexts[h.sid].initiative.nonce
        class Held(JSONModel):
            async def stream_dialogue(self,*args,**kwargs):
                entered.set();await release.wait()
                async for value in super().stream_dialogue(*args,**kwargs):yield value
        h.model=Held(content());h.app.bounded_s2s.llm=h.model
        pending=asyncio.create_task(h.send('SpeechReceived',text='Да'))
        await asyncio.wait_for(entered.wait(),2)
        assert h.app.conversation_contexts[h.sid].initiative is None
        assert nonce not in (h.app._dialogue_initiative_scope(h.sid) or ())
        assert h.state.pending_initiative is None
        # A fresh second nonverbal accept has no proposal to consume.
        await h.key(1)
        assert h.state.game_engine_state is None
        release.set();await asyncio.gather(pending,return_exceptions=True)
        assert sum('Кто мяукает' in text for text in h.render.texts)<=1
    finally:
        release.set()
        if pending and not pending.done():pending.cancel()
        h.app.bounded_s2s.llm=original;await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['initiative_offer','initiative_execute'])
async def test_public_fallback_payload_cannot_forge_a_typed_proposal_or_execution(reason):
    h=await conversation()
    try:
        before=h.state.state_version
        result=await h.send('DialogueFallback',{'reason':reason,'kind':'game','target':'quiz',
            'initiative_execution':['game','quiz'],'initiative_offer':{'ready':True}})
        assert result['reason']=='dialogue_fallback_untrusted'
        assert h.state.state_version==before and h.state.pending_initiative is None
        assert h.state.mode=='bear_dialogue' and h.state.game_engine_state is None
    finally:await h.close()


@pytest.mark.asyncio
async def test_early_voice_yes_waits_for_played_question_without_cutting_or_consuming_it():
    h=await conversation()
    try:
        model(h,proposal(),content());await h.send('SpeechReceived',text='Мне нравится лес')
        owner=dict(h.app._active_audio[h.sid]);version=h.state.state_version
        result=await h.send('SpeechReceived',text='Да')
        assert result['reason']=='initiative_offer_waiting'
        assert h.state.state_version==version and h.app._active_audio[h.sid]==owner
        assert len(h.model.calls)==1 and not h.state.pending_initiative.ready
        await h.terminal();await h.send('SpeechReceived',text='Да')
        assert sum('Кто мяукает' in text for text in h.render.texts)==1
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['validator','no_pcm'])
async def test_unheard_owned_offer_is_not_in_heard_history_and_cannot_grant_consent(failure):
    from app.foundation.application import INITIATIVE_CONTROLS
    from app.foundation.validation import ValidationDecision
    h=await conversation();validator=h.app.bounded_s2s.response_validator
    class BlockedOwned:
        async def validate(self,text,**kwargs):
            if INITIATIVE_CONTROLS in text:
                return ValidationDecision('block',False,'fixture',1.,'fixture')
            return await validator.validate(text,**kwargs)
        async def validate_trusted(self,*args,**kwargs):return await validator.validate_trusted(*args,**kwargs)
    try:
        model(h,proposal(),content())
        if failure=='validator':h.app.bounded_s2s.response_validator=BlockedOwned()
        else:h.app.audio.private_pcm_supported=False
        await h.send('SpeechReceived',text='Мне нравится лес')
        assert h.state.pending_initiative is None and 'button_1' not in h.state.expected_inputs
        context=h.app.conversation_contexts[h.sid]
        assert context.initiative is None and context.history[-1]==('Мне нравится лес','')
        assert not any(text.startswith('Хочешь простую загадку?') for text in h.render.texts)
        await h.key(1);await h.send('SpeechReceived',text='Да')
        assert h.state.mode=='bear_dialogue' and not h.app._game_callers
        assert not any('Кто мяукает' in text for text in h.render.texts)
    finally:await h.close()


@pytest.mark.asyncio
async def test_replaced_proposal_rejects_foreign_capture_scope_then_fresh_button_accepts_current():
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h=await conversation();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original=stand.application
    try:
        stand.application=h.app;stand.active_session_id=h.sid
        joke='Кот нашёл мышку, но она оказалась компьютерной.'
        model(h,proposal(),proposal('joke',text='Хочешь короткую шутку?'),
            json.dumps({'type':'say_initiative','kind':'joke','target':'joke','phase':'complete','text':joke,'answer':''},ensure_ascii=False))
        await h.send('SpeechReceived',text='Мне нравится лес');await h.terminal()
        old=stand._audio_owner_guard(h.sid)
        await h.send('SpeechReceived',text='А ещё мне нравится море');await h.terminal()
        current=h.state.pending_initiative
        assert current.kind=='joke' and old.initiative_scope[2]!=current.nonce
        calls=len(h.model.calls)
        await pcm(h,'Да',guard=old)
        assert len(h.model.calls)==calls and h.state.pending_initiative is current
        await h.key(1)
        assert joke in h.render.texts and h.state.pending_initiative is None
        assert not any('Кто мяукает' in text for text in h.render.texts)
    finally:
        stand.application=original;await h.close();await stand.aclose()


@pytest.mark.asyncio
async def test_pending_proposal_and_private_answer_never_enter_public_effects_or_snapshots():
    h=await conversation()
    try:
        model(h,proposal(),content())
        result=await h.send('SpeechReceived',text='Мне нравится лес')
        nonce=h.state.pending_initiative.nonce
        public=json.dumps(result,ensure_ascii=False)
        assert 'pending_initiative' not in public and nonce not in public
        assert 'Хочешь простую загадку?' not in public
        await h.terminal();await h.send('SpeechReceived',text='Да')
        snapshot=json.dumps(h.state.snapshot(),ensure_ascii=False)
        assert 'initiative' not in snapshot and 'кот' not in snapshot.casefold()
    finally:await h.close()


@pytest.mark.asyncio
async def test_late_owned_offer_tts_cannot_publish_or_rearm_after_end_and_new_visit():
    import asyncio
    h=await conversation();entered,release=asyncio.Event(),asyncio.Event();pending=None
    class Held:
        async def synthesize(self,text,token,cancelled):
            audio=await h.render.synthesize(text,token,cancelled)
            if text.startswith('Хочешь простую загадку?'):
                entered.set()
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
            return audio
    try:
        model(h,proposal());h.app.bounded_s2s.tts=Held()
        pending=asyncio.create_task(h.send('SpeechReceived',text='Мне нравится лес'))
        await asyncio.wait_for(entered.wait(),2)
        assert h.state.pending_initiative and not h.state.pending_initiative.ready
        assert h.sid not in h.app._active_audio
        await h.app.session_action(h.sid,'end',ownership_guard=lambda:True)
        await h.app.session_action(h.sid,'start',ownership_guard=lambda:True)
        before=(h.state.state_version,h.state.mode,h.state.active_audio_id)
        release.set();await asyncio.gather(pending,return_exceptions=True)
        assert (h.state.state_version,h.state.mode,h.state.active_audio_id)==before
        assert h.state.pending_initiative is None
        context=h.app.conversation_contexts.get(h.sid)
        assert context is None or context.initiative is None
    finally:
        release.set()
        if pending and not pending.done():pending.cancel()
        await h.close()
