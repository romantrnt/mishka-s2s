import asyncio
from dataclasses import replace
import pytest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import build_application
from app.foundation.personal_story import STORY_ID
from test_personal_story_service import engine

async def start_app():
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'))
    app.bounded_s2s=engine()
    # These tests isolate authored-story playback/ownership. The real menu
    # narrator is independently exercised in test_scene_menu_runtime.
    app._owned_menu_voice_capable=lambda:False
    app._story_seq=0
    return app
async def send(app,kind,payload=None,text=None):
    app._story_seq+=1
    event=Event(kind,'story-test',app._story_seq,float(app._story_seq),'test',payload or {},str(app._story_seq),float(app._story_seq))
    return await app.receive_async(event,text) if text is not None else await app.receive_async(event)
async def enter(app):
    await send(app,'HandsetLifted')
    result=await send(app,'ButtonPressed',{'button_id':3})
    assert app.canonical_runtime.sessions['story-test'].mode=='personal_story_theme',result
    await send(app,'ButtonReleased',{'button_id':3})
    assert len(app.canonical_runtime.sessions['story-test'].scene_menu_descriptor['items']) == 5

@pytest.mark.asyncio
async def test_actual_app_select_generate_play_complete_no_private_effects():
    app=await start_app()
    try:
        await enter(app)
        result=await send(app,'ButtonPressed',{'button_id':1})
        assert result['reason']=='personal_story_ready',result
        assert app.canonical_runtime.sessions['story-test'].mode=='personal_story_playing'
        assert any(c.get('response_id')==STORY_ID for c in app.audio.commands)
        assert 'Зайчик' not in repr(result)
        lease=app._active_audio['story-test']
        await send(app,'AudioCompleted',{'response_id':STORY_ID,'token':lease['token'],'generation':lease['generation'],
                   'playback_id':lease['playback_id'],'playback_lease':lease['playback_lease']})
        assert not app._personal_stories
        assert app.canonical_runtime.sessions['story-test'].mode=='root_menu'
    finally: await app.shutdown_identity()

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,payload',[('HandsetPlaced',{}),('ButtonPressed',{'button_id':5}),('ButtonPressed',{'button_id':7}),('SessionReset',{}),('HardwareDisconnected',{})])
async def test_cancel_during_render_never_queues_late_pcm(kind,payload):
    app=await start_app(); entered=asyncio.Event();release=asyncio.Event()
    original=app.bounded_s2s.tts.synthesize
    async def delayed(*args):
        entered.set()
        try: await release.wait()
        except asyncio.CancelledError: await release.wait()
        return await original(*args)
    app.bounded_s2s.tts.synthesize=delayed
    pending=None
    try:
        await enter(app)
        pending=asyncio.create_task(send(app,'ButtonPressed',{'button_id':1}))
        await asyncio.wait_for(entered.wait(),2)
        await send(app,kind,payload)
        release.set();await asyncio.wait_for(pending,2)
        assert not app._personal_stories
        assert not any(c.get('response_id')==STORY_ID for c in app.audio.commands)
    finally:
        release.set()
        if pending: await asyncio.gather(pending,return_exceptions=True)
        await app.shutdown_identity()

@pytest.mark.asyncio
async def test_cancel_during_generating_publication_cannot_start_provider_after_await():
    app=await start_app();published=asyncio.Event();release=asyncio.Event();pending=None
    async def hook(sid,result):
        if result['state']=='personal_story_generating':
            published.set()
            await release.wait()
    app.state_update_hook=hook
    try:
        await enter(app)
        pending=asyncio.create_task(send(app,'ButtonPressed',{'button_id':1}))
        await asyncio.wait_for(published.wait(),2)
        assert not app.bounded_s2s.llm.calls
        assert (await send(app,'ButtonPressed',{'button_id':7}))['reason']=='activity_back'
        release.set()
        assert (await asyncio.wait_for(pending,2))['reason']=='stale_session_generation'
        assert app.canonical_runtime.sessions['story-test'].mode=='root_menu'
        assert not app.bounded_s2s.llm.calls and not app._personal_stories
    finally:
        release.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        await app.shutdown_identity()

@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable',['provider','timeout'])
async def test_unavailable_generates_one_honest_080(unavailable):
    app=await start_app()
    if unavailable=='provider': app.bounded_s2s.llm.stream_story=None
    else:
        app.bounded_s2s.config=replace(app.bounded_s2s.config,max_turn_seconds=.01)
        async def slow(*args):await asyncio.sleep(10)
        app.bounded_s2s.tts.synthesize=slow
    try:
        await enter(app)
        result=await send(app,'ButtonPressed',{'button_id':1})
        if unavailable=='provider':
            assert result['reason'] == 'personal_story_ready'
            assert len(app.canonical_runtime.sessions['story-test'].personal_story_selection) == 5
            assert not app.bounded_s2s.llm.calls
            assert any(c.get('response_id') == STORY_ID for c in app.audio.commands)
        else:
            assert result['reason']=='personal_story_unavailable',result
            assert app.canonical_runtime.sessions['story-test'].mode=='fixed_story_menu'
            assert sum(c.get('response_id')=='PHRASE_080' for c in app.audio.commands)==1
    finally:await app.shutdown_identity()

@pytest.mark.asyncio
async def test_external_ready_result_not_authority():
    app=await start_app()
    try:
        await enter(app)
        result=await send(app,'PersonalStoryReady',{'request_id':'fake','personal_story_authorized':True})
        assert result['reason'] in {'personal_story_result_untrusted','unsupported_event'}
        assert not any(c.get('response_id')==STORY_ID for c in app.audio.commands)
    finally:await app.shutdown_identity()

@pytest.mark.asyncio
@pytest.mark.parametrize('text,untrusted,expected',[('catalogue_title',False,True),('slot',False,True),('Что угодно неизвестное',False,False),('catalogue_title',True,False),('Не хочу жить',False,False)])
async def test_voice_theme_still_requires_safe_trusted_admission(text,untrusted,expected):
    app=await start_app()
    try:
        await enter(app)
        if text == 'catalogue_title':
            text = app.canonical_runtime.sessions['story-test'].scene_menu_descriptor['items'][0]['title']
        elif text == 'slot':
            text = 'Кнопка один'
        result=await send(app,'SpeechReceived',{'_stt_untrusted':untrusted},text)
        assert not app.bounded_s2s.llm.calls,result
        if expected:assert app.canonical_runtime.sessions['story-test'].mode=='personal_story_playing'
        else:assert not any(c.get('response_id')==STORY_ID for c in app.audio.commands)
    finally:await app.shutdown_identity()

@pytest.mark.asyncio
async def test_actual_pause_resume_reuses_ram_pcm_and_new_lease():
    app=await start_app()
    try:
        await enter(app);await send(app,'ButtonPressed',{'button_id':1})
        owner=app._personal_stories['story-test'];audio=owner.audio
        old=app._active_audio['story-test'].copy()
        async def pause():
            assert app._personal_story_scope('story-test',owner,checkpoint=True)
            assert not app._personal_story_scope('story-test',owner,playback=True)
            return {'story_id':STORY_ID,'offset_ms':1000}
        app.audio.pause=pause
        await send(app,'ButtonPressed',{'button_id':6})
        assert app.canonical_runtime.sessions['story-test'].dialogue_memory.story_checkpoint['offset_ms']==1000
        async def complete(phrase):
            lease=app._active_audio['story-test']
            return await send(app,'AudioCompleted',{'response_id':phrase,'token':lease['token'],'generation':lease['generation'],
                            'playback_id':lease['playback_id'],'playback_lease':lease['playback_lease']})
        await complete('PHRASE_062');await send(app,'ButtonReleased',{'button_id':6})
        await send(app,'ButtonPressed',{'button_id':6});await complete('PHRASE_064')
        assert app.canonical_runtime.sessions['story-test'].mode=='personal_story_playing'
        assert owner.audio is audio and owner.offset_ms==1000
        assert not app.bounded_s2s.llm.calls
        assert app._active_audio['story-test']['playback_id']!=old['playback_id']
    finally:await app.shutdown_identity()

@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',['identity_changed','expiry','explicit_revoke'])
async def test_story_ram_and_playback_scope_do_not_outlive_identity(revoke):
    app=await start_app()
    try:
        await enter(app);await send(app,'ButtonPressed',{'button_id':1})
        owner=app._personal_stories['story-test']
        state=app.canonical_runtime.sessions['story-test']
        # Scoped identity boundary fixture. No real profile/Max implementation is changed.
        state.profile_ref='synthetic-profile';state.identity_request_id='request-a'
        owner.identity=('synthetic-profile','request-a')
        app._identity_scope_live=lambda sid,capability='personalize': True
        assert app._personal_story_scope('story-test',owner,playback=True)
        if revoke=='identity_changed':state.identity_request_id='request-b'
        elif revoke=='expiry':app._identity_scope_live=lambda sid,capability='personalize':False
        else:app._drop_identity_owner('story-test')
        assert not app._personal_story_scope('story-test',owner,playback=True)
        if revoke!='explicit_revoke':await send(app,'ButtonPressed',{'button_id':7})
        assert owner.audio is None and owner.closed
    finally:await app.shutdown_identity()

@pytest.mark.asyncio
async def test_failed_pcm_port_has_honest_fallback_and_discards_private_owner():
    app=await start_app()
    def broken(*args):raise RuntimeError('PRIVATE_PCM_ERROR_PAYLOAD')
    app.audio.play_pcm_owned=broken
    try:
        await enter(app);result=await send(app,'ButtonPressed',{'button_id':1})
        assert result['reason']=='personal_story_unavailable'
        assert not app._personal_stories
        assert sum(c.get('response_id')=='PHRASE_080' for c in app.audio.commands)==1
        assert 'PRIVATE_PCM_ERROR_PAYLOAD' not in repr(result)
    finally:await app.shutdown_identity()

@pytest.mark.asyncio
@pytest.mark.parametrize('beyond_end,expected',[(0,'root_menu'),(1,'fixed_story_menu')])
async def test_resume_at_full_native_checkpoint_finishes_not_empty_pcm(beyond_end,expected):
    app=await start_app()
    try:
        await enter(app);await send(app,'ButtonPressed',{'button_id':1})
        owner=app._personal_stories['story-test'];plays=[]
        offset = int(1000 * len(owner.audio.samples) / owner.audio.sample_rate) + beyond_end
        async def pause():return {'story_id':STORY_ID,'offset_ms':offset}
        app.audio.pause=pause
        await send(app,'ButtonPressed',{'button_id':6})
        async def complete(phrase):
            lease=app._active_audio['story-test']
            return await send(app,'AudioCompleted',{'response_id':phrase,'token':lease['token'],'generation':lease['generation'],
                            'playback_id':lease['playback_id'],'playback_lease':lease['playback_lease']})
        await complete('PHRASE_062');await send(app,'ButtonReleased',{'button_id':6})
        await send(app,'ButtonPressed',{'button_id':6})
        app.audio.play_pcm_owned=lambda *args:plays.append(args)
        result=await complete('PHRASE_064')
        assert app.canonical_runtime.sessions['story-test'].mode==expected,result
        assert not plays and not app._personal_stories
        if beyond_end == 0:
            assert result['reason']=='personal_story_finished'
            assert not any(c.get('response_id')=='PHRASE_080' for c in app.audio.commands)
        else:assert result['reason']=='personal_story_unavailable'
        assert owner.closed and owner.audio is None
    finally:await app.shutdown_identity()
