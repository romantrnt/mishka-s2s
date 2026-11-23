import asyncio
import pytest
from test_scene_menu_runtime import Harness
from app.foundation.personal_story import STORY_ID


def setup():
    h=Harness()
    h.app._owned_menu_voice_capable=lambda:False
    return h


@pytest.mark.asyncio
async def test_button_three_reentry_keeps_five_choices_and_button_four_preserves_support():
    h=setup()
    try:
        await h.button(3);await h.release(3)
        assert h.state.mode=='personal_story_theme'
        first=h.state.scene_menu_descriptor.copy()
        assert len(first['items'])==5 and first['buttons']==[1,2,3,4,5,7]
        assert (await h.button(7))['state']=='root_menu'
        await h.release(7);await h.button(3);await h.release(3)
        assert h.state.scene_menu_descriptor==first
        await h.button(7);await h.release(7);await h.button(4)
        assert h.state.mode=='support_intake'
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('return_mode',[None,'bear_dialogue'])
async def test_local_story_selection_completion_and_cancel_restore_caller(return_mode):
    h=setup()
    try:
        await h.button(3);await h.release(3)
        h.state.activity_return_mode=return_mode
        result=await h.button(1)
        assert result['reason']=='personal_story_ready',result
        assert h.state.mode=='personal_story_playing' and h.played[-1].response_id==STORY_ID
        await h.release(1)
        await h.complete()
        assert h.state.mode==(return_mode or 'root_menu')
        assert not h.app._personal_stories
    finally:await h.close()


@pytest.mark.asyncio
async def test_hangup_clears_selection_name_and_cancels_late_story_audio():
    h=setup();pending=None
    try:
        await h.button(3);await h.release(3)
        h.render.block=asyncio.Event();h.render.entered.clear()
        pending=asyncio.create_task(h.button(1))
        await asyncio.wait_for(h.render.entered.wait(),2)
        await h.send('HandsetPlaced')
        h.render.block.set();await asyncio.wait_for(pending,2)
        assert not any(handle.response_id==STORY_ID for handle in h.played)
        assert h.sid not in h.app.canonical_runtime.sessions
        assert h.sid not in h.app._canonical_profiles
        assert not h.app._personal_stories
    finally:
        if h.render.block:h.render.block.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_button_seven_returns_from_local_story_without_voice():
    h=setup()
    try:
        await h.button(3);await h.release(3);await h.button(1);await h.release(1)
        assert h.state.mode=='personal_story_playing'
        await h.button(7)
        assert h.state.mode=='root_menu' and not h.app._personal_stories
    finally:await h.close()


@pytest.mark.asyncio
async def test_local_story_pause_resume_keeps_pcm_and_replaces_playback_lease():
    from app.foundation.s2s import AudioChunk
    h=setup()
    async def synth(text,token,cancelled):return AudioChunk((.1,)*2400,24000,token)
    h.render.synthesize=synth
    try:
        await h.button(3);await h.release(3);await h.button(1);await h.release(1)
        owner=h.app._personal_stories[h.sid];original_pcm=owner.audio
        old=h.app._active_audio[h.sid].copy()
        async def pause():return {'story_id':STORY_ID,'offset_ms':500}
        h.app.audio.pause=pause
        await h.button(6);await h.release(6);await h.complete()
        assert h.state.mode=='story_paused'
        await h.button(6);await h.release(6);await h.complete()
        assert h.state.mode=='personal_story_playing'
        assert owner.audio is original_pcm and owner.offset_ms==500
        assert h.app._active_audio[h.sid]['playback_id']!=old['playback_id']
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized,hook_up',[(False,False),(False,True),(True,False),(True,True)])
async def test_new_story_menu_four_entry_combinations_use_simulator_edges(tmp_path,authorized,hook_up):
    from test_panel_session_actions import PanelStand
    h=PanelStand(tmp_path,identity=authorized)
    try:
        if authorized:await h.ready()
        await h.control('start');await h.menu()
        if hook_up:await h.simulator.action({'action':'handset','state':'up'})
        await h.key(3)
        assert h.state.mode=='personal_story_theme'
        ids=[e['id'] for e in h.state.scene_menu_descriptor['items']]
        assert len(ids)==len(set(ids))==5
        await h.key(7);await h.key(3)
        assert h.state.mode=='personal_story_theme'
        assert [e['id'] for e in h.state.scene_menu_descriptor['items']]==ids
        if hook_up:
            await h.simulator.action({'action':'handset','state':'down'})
        else:await h.control('end')
        assert not h.app._personal_stories
    finally:await h.close()
