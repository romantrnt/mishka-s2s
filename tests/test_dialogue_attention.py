"""Attention continuity contracts; model/semantic labels/24k PCM are fixtures."""
import pytest
from app.foundation.s2s import AudioChunk
from app.foundation.validation import ValidationDecision
from test_contextual_game_flow import Harness

BAD='Привет, я Мишка. Давай знакомиться!'
ACK='Я здесь, слушаю тебя.'

class Voice:
    def __init__(self):self.texts=[]
    async def synthesize(self,text,token,cancelled):
        self.texts.append(text);return AudioChunk((.1,)*2400,24000,token)
    async def convert_private(self,audio,cancelled):return audio
    async def convert(self,audio,cancelled):return audio

class SemanticFixture:
    def __init__(self):self.requirements=[]
    async def validate(self,text,context=(),*,spoken_requirement=None):
        instruction=spoken_requirement.instruction() if spoken_requirement else ''
        self.requirements.append(instruction)
        blocked=BAD in text and 'ATTENTION_CONTINUATION' in instruction
        return ValidationDecision('block' if blocked else 'allow',not blocked,'fixture_attention_restart',1.,'fixture')

async def entered(*replies):
    h=Harness(*replies,mode='root_menu');h.render=Voice()
    h.app.bounded_s2s.tts=h.app.bounded_s2s.voice=h.render
    await h.key(6)
    assert h.state.mode=='bear_dialogue' and h.state.active_audio_id=='DIALOGUE_INTRO'
    await h.done();assert not h.model.calls
    return h

@pytest.mark.asyncio
async def test_first_address_after_actual_bear_intro_repairs_regreeting_without_onboarding():
    h=await entered(f'<say>{BAD}</say>',f'<say>{ACK}</say>')
    semantic=SemanticFixture();h.app.bounded_s2s.response_validator.mini=semantic
    try:
        result=await h.send('SpeechReceived',text='Мишка?')
        assert BAD not in h.render.texts and h.render.texts[-1]==ACK
        assert len(h.model.calls)==2 and all(call[-1]['content']=='Мишка?' for call in h.model.calls)
        assert 'ATTENTION_CONTINUATION' in h.model.calls[0][0]['content']
        assert h.state.mode=='bear_dialogue' and not h.state.onboarding_name_collected
        assert not any(output.get('phrase_id')=='PHRASE_001' for output in result['outputs'])
    finally:await h.close()

@pytest.mark.asyncio
async def test_attention_repair_is_bounded_and_uses_existing_failure_fallback():
    h=await entered(f'<say>{BAD}</say>',f'<say>{BAD}</say>')
    h.app.bounded_s2s.response_validator.mini=SemanticFixture()
    try:
        result=await h.send('SpeechReceived',text='Мишка, ты тут?')
        assert BAD not in h.render.texts and len(h.model.calls)==2
        assert any(output.get('fallback_reason')=='provider_or_validation_failure' for output in result['outputs'])
        assert h.state.mode=='bear_dialogue' and not h.app.bounded_s2s.active_queues
    finally:await h.close()


@pytest.mark.parametrize('text', ['Мишка?', 'Здоровяк!', 'Эй, Мишка?', 'Ты тут?', 'Мишка, ты здесь?', 'Ты меня слышишь?'])
def test_only_complete_attention_calls_get_continuation_scope(text):
    from app.foundation.system_prompt import attention_request
    assert attention_request(text)


@pytest.mark.parametrize('text', ['Привет, Мишка!', 'Мишка, расскажи про динозавров',
    'Как тебя зовут?', 'Где живёт Мишка?', 'Я сказал «Мишка?»', 'Не слышишь музыку?'])
def test_explicit_greeting_content_question_and_report_are_not_attention_only(text):
    from app.foundation.system_prompt import attention_request
    assert not attention_request(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('text,reply', [('Мишка?',ACK), ('Привет, Мишка!',BAD),
    ('Мишка, расскажи про динозавров','Динозавры были разными.')])
async def test_healthy_acknowledgement_and_explicit_greeting_use_one_provider_turn(text,reply):
    h=await entered(f'<say>{reply}</say>');semantic=SemanticFixture()
    h.app.bounded_s2s.response_validator.mini=semantic
    try:
        await h.send('SpeechReceived',text=text)
        assert h.render.texts[-1]==reply and len(h.model.calls)==1 and len(semantic.requirements)==1
        assert ('ATTENTION_CONTINUATION' in semantic.requirements[0])==(text=='Мишка?')
        assert h.state.mode=='bear_dialogue'
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['window','audio_interruption','game_return'])
async def test_current_attention_preserves_history_and_owner_after_continuation(boundary):
    import time
    h=await entered('<say>Динозавры были разными.</say>',f'<say>{BAD}</say>',f'<say>{ACK}</say>')
    semantic=SemanticFixture();h.app.bounded_s2s.response_validator.mini=semantic
    try:
        await h.send('SpeechReceived',text='Мне нравятся динозавры')
        sid=h.state.session_id;before=h.app.conversation_contexts[sid]
        prefix=list(before.history);owner=before._owner;epoch=h.app._session_epochs.get(sid,0)
        if boundary=='window':before.started_at=time.monotonic()-121
        elif boundary=='audio_interruption':await h.app.s2s_audio.cancel_session(sid)
        else:
            h.app._load_game_asset=lambda phrase,token:AudioChunk((.1,)*2400,24000,token)
            await h.launch('free_lights');await h.key(7,duration=6.)
            assert h.state.mode=='bear_dialogue';await h.done()
        await h.send('SpeechReceived',text='Мишка?')
        current=h.app.conversation_contexts[sid]
        assert current.history[:len(prefix)]==prefix and current.history[-1]==('Мишка?',ACK)
        assert current._owner==owner and h.app._session_epochs.get(sid,0)==epoch
        assert h.state.mode=='bear_dialogue' and h.render.texts[-1]==ACK and BAD not in h.render.texts
        assert 'Динозавры' in h.model.calls[-1][0]['content']
    finally:await h.close()


@pytest.mark.asyncio
async def test_actual_stand_reconnect_keeps_attention_history_and_session_owner():
    import asyncio
    import httpx
    from app.foundation.config import Settings
    from app.foundation.lifecycle import RuntimeState
    from app.foundation.stand import create_stand_app
    from test_browser_private_pcm import Socket
    from test_contextual_game_flow import Model
    api=create_stand_app(Settings(profile='mock',runtime_mode='clinical',stand_access_token='synthetic-attention'))
    api.state.lifecycle._state=RuntimeState.READY  # Existing test-only admission, not real readiness.
    stand=api.state.stand;app=stand.application;clock=[100.]
    app.monotonic_now=app.canonical_runtime.monotonic_now=lambda:clock[0]
    voice=Voice();model=Model('<say>Динозавры были разными.</say>',f'<say>{BAD}</say>',f'<say>{ACK}</say>')
    app.bounded_s2s.tts=app.bounded_s2s.voice=voice;app.bounded_s2s.llm=model
    writer=Socket();assert await stand.hub.connect(writer)
    sid=stand.active_session_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://fixture.local',headers={'x-stand-token':'synthetic-attention'}) as client:
        async def post(path,body):
            response=await client.post(path,json={'session_id':sid,**body})
            assert response.status_code==200 and not response.json().get('error'),response.text
            return response.json()
        async def terminal():
            owner=dict(app._active_audio[sid]);payload={k:owner[k] for k in ('token','generation','playback_id','playback_lease')}
            payload.update(session_id=sid,response_id=owner['content_id'])
            async with asyncio.timeout(4):
                while not stand._browser_pcm_callback_current(payload,require_delivered=True):await asyncio.sleep(.001)
            await post('/stand/audio/completed',payload)
        try:
            await post('/stand/handset',{'state':'up'})
            if app.canonical_runtime.sessions[sid].mode=='voice_welcome':
                clock[0]+=6;await app.flush_welcome_timers()
                if sid in app._active_audio:await terminal()
            await post('/stand/button',{'button_id':6,'gesture_id':'attention-entry'})
            await post('/stand/button-released',{'button_id':6,'gesture_id':'attention-entry'})
            assert app.canonical_runtime.sessions[sid].mode=='bear_dialogue';await terminal()
            await post('/stand/text',{'text':'Мне нравятся динозавры'})
            context=app.conversation_contexts[sid];history=list(context.history);owner=context._owner;epoch=app._session_epochs.get(sid,0)
            stand.hub.disconnect(writer);replacement=Socket();assert await stand.hub.connect(replacement)
            semantic=SemanticFixture();app.bounded_s2s.response_validator.mini=semantic
            result=await post('/stand/text',{'text':'Мишка?'})
            current=app.conversation_contexts[sid]
            assert current.history[:len(history)]==history and current.history[-1]==('Мишка?',ACK)
            assert current._owner==owner and app._session_epochs.get(sid,0)==epoch
            assert app.canonical_runtime.sessions[sid].mode=='bear_dialogue' and BAD not in voice.texts
            assert len(model.calls)==3 and 'Динозавры' in model.calls[-1][0]['content']
            assert not any(output.get('phrase_id')=='PHRASE_001' for output in result['outputs'])
            # A brief clinical hook interruption retains the logical conversation.
            saved_history=list(current.history)
            await post('/stand/handset',{'state':'down'});clock[0]+=.1
            await post('/stand/handset',{'state':'up'})
            if sid in app._active_audio:await terminal()
            assert app.canonical_runtime.sessions[sid].mode=='awaiting_handset'
            await post('/stand/button',{'button_id':6,'gesture_id':'attention-resume'})
            await post('/stand/button-released',{'button_id':6,'gesture_id':'attention-resume'})
            assert app.canonical_runtime.sessions[sid].mode=='bear_dialogue'
            if sid in app._active_audio:await terminal()
            model.answers.extend([f'<say>{BAD}</say>',f'<say>{ACK}</say>'])
            await post('/stand/text',{'text':'Мишка, ты тут?'})
            resumed=app.conversation_contexts[sid]
            assert resumed.history[:len(saved_history)]==saved_history and resumed.history[-1]==('Мишка, ты тут?',ACK)
            assert resumed._owner[1]==owner[1] and BAD not in voice.texts
            # End/Start is a genuine fresh visit, not the short interruption above.
            async def action(name):
                return await post('/stand/simulator/action',{'action':name,'instance_id':stand._panel_instance_id,
                    'epoch':stand.remote_devices.epoch,'context_generation':app._profile_context_generations.get(sid,0)})
            await action('end');assert sid not in app.conversation_contexts
            await action('start')
            if app.canonical_runtime.sessions[sid].mode=='voice_welcome':
                clock[0]+=6;await app.flush_welcome_timers()
                if sid in app._active_audio:await terminal()
            await post('/stand/button',{'button_id':6,'gesture_id':'fresh-attention-entry'})
            await post('/stand/button-released',{'button_id':6,'gesture_id':'fresh-attention-entry'})
            assert app.canonical_runtime.sessions[sid].mode=='bear_dialogue';await terminal()
            model.answers.append(f'<say>{BAD}</say>')
            await post('/stand/text',{'text':'Привет, Мишка!'})
            assert voice.texts[-1]==BAD and 'ATTENTION_CONTINUATION' not in model.calls[-1][0]['content']
            assert app.conversation_contexts[sid].history==[('Привет, Мишка!',BAD)]
        finally:await stand.aclose()


@pytest.mark.asyncio
async def test_attention_repair_retains_current_active_riddle_but_never_revives_offered_consent():
    import json
    from test_activity_request_gate import engine
    from test_dialogue_initiatives import wire,played
    from dataclasses import replace
    e,c,m,tts=engine(wire('Зимой и летом одним цветом. Что это?',phase='active',answer='Ёлка'),
        json.dumps({'type':'say','text':BAD}),json.dumps({'type':'say','text':ACK}),
        wire('Это дерево с иголками.',phase='active',answer='Ёлка'),structured=True)
    e.response_validator.mini=SemanticFixture()
    try:
        await e.run_text_turn('Загадай загадку',c);active=c.initiative
        result=await e.run_text_turn('Мишка?',c,initiative_input_scope=active.nonce)
        assert result.chunks==(ACK,) and c.initiative is not None
        assert (c.initiative.nonce,c.initiative.text,c.initiative.answer)==(active.nonce,active.text,active.answer)
        assert '"execute_kind": null' in m.calls[-1][0]['content']
        await e.run_text_turn('Подскажи',c,initiative_input_scope=active.nonce)
        assert c.initiative.answer=='Ёлка' and c.initiative.text==active.text and BAD not in tts.texts
    finally:await e.cancel_session(c.session_id)
    e,c,m,tts=engine(wire('Хочешь загадку?'),json.dumps({'type':'say','text':ACK}),
        wire('Зимой и летом одним цветом. Что это?',phase='active',answer='Ёлка'),structured=True)
    try:
        await e.run_text_turn('Мне нравится лес',c);nonce=played(c)
        result=await e.run_text_turn('Мишка?',c,initiative_input_scope=nonce)
        assert result.chunks==(ACK,) and c.initiative is None
        result=await e.run_text_turn('Да',c,initiative_input_scope=nonce)
        assert not result.chunks and not result.actions and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['generation','identity_owner'])
@pytest.mark.parametrize('attempt',[1,2])
async def test_old_attention_validation_abort_cannot_restore_over_newer_active_state(monkeypatch,boundary,attempt):
    import asyncio,json
    from dataclasses import replace
    from app.foundation.s2s import AudioSynthesisQueue
    from test_activity_request_gate import engine
    from test_dialogue_initiatives import wire
    e,c,m,tts=engine(wire('Зимой и летом одним цветом. Что это?',phase='active',answer='Ёлка'),
        json.dumps({'type':'say','text':BAD}),json.dumps({'type':'say','text':BAD}),structured=True)
    e.response_validator.mini=SemanticFixture();c._owner=(0,None)
    await e.run_text_turn('Загадай загадку',c);old=c.initiative
    entered=asyncio.Event();release=asyncio.Event();original=AudioSynthesisQueue.abort
    aborts=0
    async def held_abort(queue,*args,**kwargs):
        nonlocal aborts
        await original(queue,*args,**kwargs);aborts+=1
        if aborts==attempt:entered.set();await release.wait()
    monkeypatch.setattr(AudioSynthesisQueue,'abort',held_abort)
    task=asyncio.create_task(e.run_text_turn('Мишка?',c,initiative_input_scope=old.nonce))
    try:
        await asyncio.wait_for(entered.wait(),1)
        fresh=replace(old,nonce='synthetic-new-active',answer='Сосна')
        c.initiative=fresh
        if boundary=='generation':e.cancel.start(c.session_id)
        else:c._owner=(1,None)
        release.set();await asyncio.gather(task,return_exceptions=True)
        assert c.initiative is fresh and len(m.calls)==attempt+1
    finally:
        release.set();await e.cancel_session(c.session_id)
