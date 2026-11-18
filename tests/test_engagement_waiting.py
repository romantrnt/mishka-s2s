import pytest
from test_product_clinical_memory import Harness
from app.foundation.domain import Event

async def terminal(h):
    owner=dict(h.app._active_audio['memory'])
    return await h.send('AudioCompleted',{**owner,'response_id':owner['content_id']})

@pytest.mark.asyncio
async def test_owned_instruction_busy_then_full_wait_and_real_button_restart():
    h=Harness()
    try:
        await h.send('HandsetLifted')
        await h.send('ButtonPressed',{'button_id':6})
        await h.send('ButtonReleased',{'button_id':6})
        assert h.state.mode=='bear_dialogue'
        assert 'memory' in h.app._active_audio
        h.clock[0]=500
        assert await h.app.expire_engagement_sessions()==[]
        assert h.state.handset=='up' and not h.state.interaction_idle
        await terminal(h)
        assert h.app._engagement_owners['memory']['deadline']==620
        h.clock[0]=619.999;assert await h.app.expire_engagement_sessions()==[]
        h.clock[0]=620
        assert (await h.app.expire_engagement_sessions())[0]['reason']=='interaction_idle'
        assert h.state.handset=='up' and h.state.interaction_idle
        assert not any(h.app.led.output.snapshot().values())
        assert (await h.send('ButtonPressed',{'button_id':6}))['reason']=='bear_dialogue_started'
        assert not h.state.interaction_idle and h.state.handset=='up'
    finally: await h.close()

@pytest.mark.asyncio
async def test_clinical_committed_ram_soft_idle_and_exact_expiry():
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        await h.say('первый вопрос')
        assert h.app.conversation_contexts['memory'].history==[('первый вопрос','ответ')]
        # Mock bounded response has no outstanding recorded lease.
        if 'memory' in h.app._active_audio: await terminal(h)
        h.clock[0]=220
        await h.app.expire_engagement_sessions()
        assert h.state.interaction_idle
        context=h.app.conversation_contexts['memory']
        assert context._ram_expires_at==520
        assert h.state.handset=='up'
        h.clock[0]=519.999;h.app.expire_dialogue_memory();assert 'memory' in h.app.conversation_contexts
        h.clock[0]=520;h.app.expire_dialogue_memory();assert 'memory' not in h.app.conversation_contexts
    finally: await h.close()

@pytest.mark.asyncio
async def test_external_idle_event_cannot_reset_active_game():
    h=Harness()
    try:
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        assert h.state.mode=='game_menu'
        before=h.state.snapshot()
        assert (await h.send('InteractionIdleExpired'))['reason']=='interaction_idle_untrusted'
        assert h.state.snapshot()==before
    finally: await h.close()

@pytest.mark.asyncio
async def test_stale_idle_nonce_cannot_end_new_button_activity():
    h=Harness()
    try:
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        old=h.app._engagement_owners['memory']['nonce']
        h.clock[0]=200
        result=await h.send('ButtonPressed',{'button_id':7});await h.send('ButtonReleased',{'button_id':7})
        assert result['reason']=='game_menu_back'
        assert h.app._engagement_owners['memory']['nonce']!=old
        h.clock[0]=220
        event=Event('InteractionIdleExpired','memory',1,220,'internal_interaction_idle',{},'old-idle',220)
        result=await h.app._receive_canonical_event(event,interaction_idle_authorized=True,_interaction_nonce=old)
        assert result['reason']=='interaction_idle_stale'
        assert not h.state.interaction_idle
    finally: await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('failed',['audio','lighting'])
async def test_idle_cleanup_resources_independent_and_diagnostic(failed,monkeypatch):
    h=Harness()
    try:
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        called=[]
        def audio(*args,**kwargs):
            called.append('audio')
            if failed=='audio':raise OSError('private device detail')
        def lighting(*args,**kwargs):
            called.append('lighting')
            if failed=='lighting':raise RuntimeError('private lighting detail')
            h.app.led.clear_all()
        monkeypatch.setattr(h.app.audio,'cancel',audio)
        monkeypatch.setattr(h.app.led,'cancel_effects',lighting,raising=False)
        h.clock[0]=220
        results=await h.app.expire_engagement_sessions()
        assert results[0]['reason']=='interaction_idle'
        assert 'audio' in called and 'lighting' in called
        failure=h.app.runtime_status['session_cleanup_last_failure']
        assert failure['resource']==failed and failure['operation']=='interaction_idle'
        assert 'private' not in repr(failure)
    finally:
        monkeypatch.undo()
        await h.close()

@pytest.mark.asyncio
async def test_new_button_during_idle_cleanup_keeps_new_scene_and_capture():
    import asyncio
    h=Harness();entered=asyncio.Event();release=asyncio.Event()
    try:
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        class SlowCleanup:
            async def cancel_session(self,sid):
                entered.set();await release.wait()
        h.app.bounded_s2s=SlowCleanup()
        h.clock[0]=220
        pending=asyncio.create_task(h.app.expire_engagement_sessions())
        await entered.wait()
        assert h.state.interaction_idle and not any(h.app.led.output.snapshot().values())
        result=await h.send('ButtonPressed',{'button_id':1})
        assert result['reason']=='button_mode'
        frame=h.app.led.output.snapshot()
        release.set();assert (await pending)[0]['reason']=='stale_session_generation'
        assert h.state.mode=='game_menu' and not h.state.interaction_idle
        assert h.app.led.output.snapshot()==frame
    finally:
        release.set();await h.close()

@pytest.mark.asyncio
async def test_urgent_safeguarding_survives_waiting_deadline():
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        await h.send('SpeechReceived',text='не хочу жить')
        assert h.state.safety_severity=='urgent' and h.state.mode=='safety_stop'
        h.clock[0]=1000
        assert await h.app.expire_engagement_sessions()==[]
        assert h.state.mode=='safety_stop' and h.state.safety_severity=='urgent'
    finally: await h.close()

@pytest.mark.asyncio
async def test_held_button_cannot_wake_until_release_then_fresh_press():
    h=Harness()
    try:
        await h.send('ButtonPressed',{'button_id':1})
        h.clock[0]=220;await h.app.expire_engagement_sessions()
        assert h.state.interaction_idle
        assert (await h.send('ButtonPressed',{'button_id':1}))['reason']=='interaction_idle_button_held'
        await h.send('ButtonReleased',{'button_id':1})
        assert (await h.send('ButtonPressed',{'button_id':1}))['reason']=='button_mode'
        assert h.state.mode=='game_menu' and not h.state.interaction_idle
    finally: await h.close()

@pytest.mark.asyncio
async def test_clinical_game_continues_silently_onhook_with_wait_owner():
    h=Harness()
    try:
        await h.send('HandsetLifted')
        for _ in range(3):
            await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        assert h.state.mode=='game_active'
        await h.send('HandsetPlaced')
        assert h.state.mode=='game_active' and h.state.handset=='down'
        assert 'memory' not in h.app._active_audio
        assert h.app._engagement_owners['memory']['deadline']==220
        h.clock[0]=220;await h.app.expire_engagement_sessions()
        assert h.state.interaction_idle and h.state.handset=='down'
        assert not any(h.app.led.output.snapshot().values())
    finally: await h.close()

@pytest.mark.asyncio
async def test_real_reset_after_soft_idle_cannot_be_resurrected_by_lift():
    h=Harness()
    try:
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        h.clock[0]=220;await h.app.expire_engagement_sessions()
        assert h.state.interaction_idle
        await h.send('SessionReset')
        assert not h.state.interaction_idle
        assert (await h.send('HandsetLifted'))['reason']=='session_closed'
        assert h.state.mode=='session_closed' and h.state.handset=='down'
    finally: await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,payload,text',[
    ('SpeechReceived',{},''),
    ('SpeechReceived',{'_stt_untrusted':True},'обычный вопрос'),
    ('VoiceIntentReady',{'intent':'input_retry'},None),
])
async def test_empty_or_untrusted_voice_does_not_refresh_wait(kind,payload,text):
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        deadline=h.app._engagement_owners['memory']['deadline']
        h.clock[0]=150
        await h.send(kind,payload,text)
        if 'memory' in h.app._active_audio:await terminal(h)
        assert h.app._engagement_owners['memory']['deadline']==deadline
    finally: await h.close()

@pytest.mark.asyncio
async def test_system_game_hint_does_not_restart_full_wait():
    h=Harness()
    try:
        await h.send('HandsetLifted')
        for _ in range(3):
            await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        await terminal(h)
        deadline=h.app._engagement_owners['memory']['deadline']
        h.clock[0]=120;await h.app.flush_game_timers()
        assert 'memory' in h.app._active_audio
        h.clock[0]=125;await terminal(h)
        assert h.app._engagement_owners['memory']['deadline']==deadline
        h.clock[0]=deadline;await h.app.expire_engagement_sessions()
        assert h.state.interaction_idle
    finally: await h.close()

@pytest.mark.asyncio
async def test_cancellation_resistant_old_story_job_cannot_keep_new_game_awake(monkeypatch):
    import asyncio
    from test_personal_story_native import setup
    from app.foundation.personal_story import PersonalStoryService
    stand,send,complete,_,_=await setup(monkeypatch)
    app,sid=stand.application,stand.active_session_id
    clock=[app.monotonic_now()];app.monotonic_now=lambda:clock[0];app.now=lambda:clock[0]
    entered,release=asyncio.Event(),asyncio.Event()
    async def resistant(*args,**kwargs):
        entered.set()
        while not release.is_set():
            try:await release.wait()
            except asyncio.CancelledError:pass
        return None
    monkeypatch.setattr(PersonalStoryService,'render',resistant)
    pending=asyncio.create_task(send('ButtonPressed',{'button_id':1}))
    try:
        await entered.wait();await send('HandsetPlaced')
        assert app._personal_story_jobs and sid not in app._personal_stories
        await send('ButtonReleased',{'button_id':1});await send('ButtonPressed',{'button_id':1})
        assert app.canonical_runtime.sessions[sid].mode=='game_menu'
        clock[0]+=120
        assert (await app.expire_engagement_sessions())[0]['reason']=='interaction_idle'
    finally:
        release.set();await pending;await stand.aclose()

@pytest.mark.asyncio
async def test_cancel_resistant_menu_tts_same_sid_old_epoch_is_not_busy():
    import asyncio
    from app.foundation.api import build_application
    from app.foundation.config import Settings
    from test_scene_menu_runtime import Render
    clock=[100.];app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),monotonic_now=lambda:clock[0],background_scheduler=False)
    app.now=lambda:clock[0];sequence=0
    entered,release=asyncio.Event(),asyncio.Event()
    render=Render()
    async def resistant(text,token,cancelled):
        entered.set()
        while not release.is_set():
            try:await release.wait()
            except asyncio.CancelledError:pass
        return None
    render.synthesize=resistant
    app.bounded_s2s.tts=render;app.bounded_s2s.voice=render
    async def send(kind,button=None):
        nonlocal sequence
        sequence+=1
        return await app.receive_async(Event(kind,'old-menu',sequence,clock[0],'test',{} if button is None else {'button_id':button},str(sequence),clock[0]))
    pending=None
    try:
        await send('HandsetLifted')
        pending=asyncio.create_task(send('ButtonPressed',1));await entered.wait()
        await send('HandsetPlaced')
        assert app._owned_text_jobs
        await send('ButtonReleased',1);await send('ButtonPressed',1)
        assert app.canonical_runtime.sessions['old-menu'].mode=='game_menu'
        clock[0]=220
        assert (await app.expire_engagement_sessions())[0]['reason']=='interaction_idle'
    finally:
        release.set()
        if pending:await pending
        await app.shutdown_identity()

@pytest.mark.asyncio
async def test_forged_public_idle_is_inert_even_at_due_deadline():
    import copy
    h=Harness()
    try:
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        snapshot=h.state.snapshot();owner=copy.deepcopy(h.app._engagement_owners)
        frame=h.app.led.output.snapshot();epoch=dict(h.app._session_epochs)
        h.clock[0]=220
        event=Event('InteractionIdleExpired','memory',999,999999,'internal_interaction_idle',{'nonce':owner['memory']['nonce']},'forged',999999)
        assert (await h.app.receive_async(event))['reason']=='interaction_idle_untrusted'
        assert h.state.snapshot()==snapshot and h.app._engagement_owners==owner
        assert h.app.led.output.snapshot()==frame and h.app._session_epochs==epoch
    finally:await h.close()

@pytest.mark.asyncio
async def test_forged_idle_does_not_touch_pending_speech_or_audio_owner():
    import asyncio,copy
    h=Harness();entered,release=asyncio.Event(),asyncio.Event();pending=None
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6})
        original=h.app.safety_classifier.classify
        async def slow(text):
            entered.set();await release.wait();return await original(text)
        h.app.safety_classifier.classify=slow
        pending=asyncio.create_task(h.send('SpeechReceived',text='Привет'))
        await entered.wait()
        lease=copy.deepcopy(h.app._active_audio);owner=copy.deepcopy(h.app._engagement_owners)
        frame=h.app.led.output.snapshot();epoch=dict(h.app._session_epochs);ingress=dict(h.app._engagement_ingress)
        event=Event('InteractionIdleExpired','memory',999,999999,'internal_interaction_idle',{'nonce':'forged'},'forged',999999)
        assert (await h.app.receive_async(event))['reason']=='interaction_idle_untrusted'
        assert not pending.done() and h.app._engagement_ingress==ingress
        assert h.app._active_audio==lease and h.app._engagement_owners==owner
        assert h.app.led.output.snapshot()==frame and h.app._session_epochs==epoch
    finally:
        release.set()
        if pending:await pending
        await h.close()

@pytest.mark.asyncio
async def test_future_input_timestamp_cannot_shorten_wait_deadline():
    h=Harness()
    try:
        event=Event('ButtonPressed','memory',1,999999,'future-test',{'button_id':1},'future',999999)
        await h.app.receive_async(event)
        assert h.app._engagement_owners['memory']['deadline']==220
        h.clock[0]=219.999;assert await h.app.expire_engagement_sessions()==[]
    finally:await h.close()
