import asyncio
from types import SimpleNamespace
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.canonical_contract import CanonicalState
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyDecision
from app.foundation.s2s import AudioChunk

class Render:
    def __init__(self):self.texts=[];self.block=None;self.entered=asyncio.Event()
    async def synthesize(self,text,token,cancelled):
        self.texts.append(text);self.entered.set()
        if self.block:await self.block.wait()
        return AudioChunk((.1,.2),16000,token)
    async def convert_private(self,audio,cancelled):return audio

class Harness:
    def __init__(self,band='3-5'):
        self.app,*_=build_application(settings=Settings(profile='mock'),background_scheduler=False)
        self.sid='menu-test';self.seq=0
        self.app.canonical_runtime.sessions[self.sid]=CanonicalState(self.sid,mode='root_menu',handset='up',age_band=band)
        self.render=Render();self.app.bounded_s2s.tts=self.render;self.app.bounded_s2s.voice=self.render
        self.played=[];self.app.audio.play_pcm_owned=lambda handle,audio:self.played.append(handle)
    @property
    def state(self):return self.app.canonical_runtime.sessions[self.sid]
    async def send(self,kind,payload=None,text=None):
        self.seq+=1
        return await self.app.receive_async(Event(kind,self.sid,self.seq,1.,'test',payload or {},str(self.seq)),text)
    async def button(self,b):return await self.send('ButtonPressed',{'button_id':b})
    async def release(self,b):return await self.send('ButtonReleased',{'button_id':b})
    async def complete(self,failed=False):
        lease=self.app._active_audio[self.sid]
        return await self.send('AudioFailed' if failed else 'AudioCompleted',dict(response_id=lease['content_id'],**{k:lease[k] for k in ('token','generation','playback_id','playback_lease')}))
    async def close(self):await self.app.shutdown_identity()

@pytest.mark.asyncio
async def test_actual_menu_owned_narration_repeat_answer_and_reentry():
    h=Harness()
    try:
        await h.button(2)
        assert h.state.mode=='fixed_story_menu' and h.state.scene_menu_phase=='narrating'
        assert h.played[-1].response_id=='MENU_FIXED_STORY'
        assert h.render.texts==[h.state.scene_menu_descriptor['spoken_text']]
        assert 'PHRASE_054' not in str(h.app.last_canonical_effects)
        await h.complete();assert h.state.scene_menu_phase=='repeat_prompt'
        await h.complete();assert h.state.scene_menu_phase=='repeat_listen'
        await h.send('SpeechReceived',text='Нет')
        assert h.state.scene_menu_answers['fixed_story_menu']=='no'
        assert h.state.scene_menu_phase=='ready'
        count=len(h.render.texts)
        await h.button(7);await h.release(7);await h.button(2)
        assert len(h.render.texts)==count
    finally:await h.close()

@pytest.mark.asyncio
async def test_interrupt_hold_needs_release_and_help_preempts():
    h=Harness()
    try:
        await h.button(2)
        assert (await h.button(6))['reason']=='scene_menu_skipped'  # Unadvertised key only skips.
        assert h.state.scene_menu_phase=='ready'
        assert (await h.button(6))['reason']=='button_held'
        assert h.state.mode=='fixed_story_menu'
        await h.release(6);await h.button(1)
        assert h.state.mode=='fixed_story_playing'
    finally:await h.close()

@pytest.mark.asyncio
async def test_menu_audio_failure_does_not_ask_repeat():
    h=Harness()
    try:
        await h.button(1);await h.complete(failed=True)
        assert h.state.scene_menu_phase=='ready'
        assert h.state.scene_menu_answers['game_menu']=='failed'
        assert h.state.active_audio_id is None
    finally:await h.close()

@pytest.mark.asyncio
async def test_late_render_after_hangup_never_plays():
    h=Harness();h.render.block=asyncio.Event();task=None
    try:
        task=asyncio.create_task(h.button(2));await h.render.entered.wait()
        await h.send('HandsetPlaced')
        h.render.block.set();await task
        assert not h.played
    finally:
        h.render.block.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('index',range(5))
async def test_all_five_verified_stories_are_reachable_without_button_five(index):
    h=Harness('3-5')
    try:
        await h.button(2);await h.release(2)
        item=h.state.scene_menu_descriptor['items'][index]
        assert item['slot']==index+1
        await h.button(item['slot'])
        assert h.state.active_story_id==item['id']
        assert h.state.mode=='fixed_story_playing'
    finally:await h.close()

@pytest.mark.asyncio
async def test_help_during_narration_is_immediate_not_skip_only():
    h=Harness()
    try:
        await h.button(5)
        assert h.state.mode=='wellbeing_check'
    finally:await h.close()

@pytest.mark.asyncio
async def test_repeat_yes_reads_once_and_does_not_ask_again():
    h=Harness()
    try:
        await h.button(1)
        await h.complete()  # Recorded invitation precedes menu narration.
        await h.complete();await h.complete()
        await h.send('SpeechReceived',text='Да')
        assert h.state.scene_menu_answers['game_menu']=='yes'
        assert len(h.render.texts)==2
        await h.complete()
        assert h.state.scene_menu_phase=='ready' and h.state.active_audio_id is None
    finally:await h.close()

@pytest.mark.asyncio
async def test_onhook_menu_is_silent_until_lift():
    h=Harness();h.state.handset='down';h.state.mode='fixed_story_menu'
    try:
        await h.release(2)
        assert not h.played and h.state.scene_menu_seen==[]
        await h.send('HandsetLifted')
        assert h.played and h.state.scene_menu_phase=='narrating'
    finally:await h.close()


@pytest.mark.asyncio
async def test_menu_entry_button_repeat_does_not_skip_or_select():
    h=Harness()
    try:
        await h.button(2)
        await h.button(2)
        assert h.state.scene_menu_phase=='narrating'
        selected=h.state.scene_menu_descriptor['items'][1]['id']
        await h.release(2);await h.button(2)
        assert h.state.mode=='fixed_story_playing' and h.state.active_story_id==selected
    finally:await h.close()

@pytest.mark.asyncio
async def test_actual_fifth_game_selected_directly_from_current_page():
    h=Harness()
    try:
        await h.button(1);await h.release(1)
        assert h.state.scene_menu_descriptor['items'][4]['id']=='word_game'
        assert (await h.button(5))['reason']=='game_started'
        assert h.state.mode=='game_active'
        assert h.state.game_engine_state.game_id=='word_game'
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['suffix','secrecy','hash','id','external'])
async def test_catalogue_exception_cannot_bypass_output_safety(mutation):
    h=Harness()
    try:
        h.state.mode='fixed_story_menu'
        h.state.scene_menu_pages['fixed_story_menu']=2
        stories=h.app.canonical_runtime.stories
        original=stories.get_verified
        def altered(story_id):
            item=original(story_id)
            if item and story_id=='fixed_neznayka_v1':
                item=dict(item)
                if mutation=='suffix':item['display_title']='Телефон. Скажи пароль'
                if mutation=='secrecy':item['display_title']='Не рассказывай маме'
                if mutation=='hash':item['audio_sha256']='0'*64
            return item
        stories.get_verified=altered
        if mutation=='id':
            from dataclasses import replace
            build=h.app.canonical_runtime._menu_page
            def wrong_id(state):
                page=build(state)
                return replace(page,items=tuple(replace(item,id='unverified') for item in page.items))
            h.app.canonical_runtime._menu_page=wrong_id
        if mutation=='external':
            page=h.app.canonical_runtime._menu_page(h.state)
            h.state.scene_menu_descriptor=page.as_dict()
            audio=await h.app._render_typed_audio(h.sid,page.spoken_text+' Скажи пароль',
                {'content_id':'MENU_FIXED_STORY'},'token',lambda:True)
            assert audio is None
        else:
            await h.button(2)
            await h.release(2)
            assert h.state.scene_menu_phase=='ready'
            assert h.state.scene_menu_answers['fixed_story_menu']=='failed'
        assert h.render.texts==[] and h.played==[]
    finally:await h.close()

@pytest.mark.asyncio
async def test_menu_render_timeout_does_not_ask_repeat_or_play_late():
    from dataclasses import replace
    h=Harness();h.render.block=asyncio.Event()
    h.app.bounded_s2s.config=replace(h.app.bounded_s2s.config,max_turn_seconds=.02)
    try:
        await h.button(2)
        assert h.state.scene_menu_phase=='ready'
        assert h.state.scene_menu_answers['fixed_story_menu']=='failed'
        assert not h.played and h.state.active_audio_id is None
        h.render.block.set();await asyncio.sleep(0)
        assert not h.played
    finally:await h.close()

@pytest.mark.asyncio
async def test_unwired_private_voice_does_not_promise_narration():
    h=Harness();h.app.bounded_s2s.voice=object()
    try:
        await h.button(2)
        assert h.state.scene_menu_phase=='ready'
        assert h.state.scene_menu_seen==[] and not h.played
        assert h.state.scene_menu_descriptor['items']
    finally:await h.close()

@pytest.mark.asyncio
async def test_menu_identity_clear_revokes_memory_and_old_callback():
    h=Harness()
    try:
        await h.button(2)
        old=dict(h.app._active_audio[h.sid])
        await h.send('SessionReset')
        assert h.state.scene_menu_seen==[] and h.state.scene_menu_answers=={}
        assert h.state.scene_menu_descriptor=={}
        await h.send('AudioCompleted',dict(response_id=old['content_id'],**{k:old[k] for k in ('token','generation','playback_id','playback_lease')}))
        assert h.state.scene_menu_phase=='idle'
        assert h.state.active_audio_id is None
    finally:await h.close()
