import pytest
from test_scene_menu_runtime import Harness

@pytest.mark.asyncio
async def test_third_game_actual_direct_choice_and_help_cleanup():
    h=Harness()
    try:
        await h.button(1);await h.release(1)
        page=h.state.scene_menu_descriptor
        assert page['page_count']==1
        assert [item['id'] for item in page['items']]==[
            'free_lights','quiz','repeat_pattern','odd_one_out','word_game']
        assert page['items'][2]['slot']==3
        assert 5 in page['buttons'] and 7 in page['buttons']
        result=await h.button(3);await h.release(3)
        assert result['reason']=='game_started'
        assert h.state.active_game_id=='repeat_pattern'
        assert h.state.game_engine_state.phase=='instruction'
        assert h.app._active_audio[h.sid]['content_id']=='PHRASE_020'
        await h.complete()
        assert h.app._active_audio[h.sid]['content_id']=='GAME_PROMPT'
        await h.complete()
        assert h.state.game_engine_state.phase=='demonstrate'
        old=h.app._game_timer_scopes[h.sid]
        await h.send('SpeechReceived',text='Позови взрослого')
        frame=dict(h.app.led.output.snapshot())
        assert h.state.mode=='safety_stop'
        assert h.state.game_engine_state is None
        assert frame=={str(i):int(i==14) for i in range(18)}
        assert await h.app._fire_game_timer(h.sid,old) is None
        assert h.app.led.output.snapshot()==frame
    finally:await h.close()
