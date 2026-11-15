import pytest
from test_scene_menu_runtime import Harness

@pytest.mark.asyncio
async def test_sixth_game_actual_paginated_choice_and_help_cleanup():
    h=Harness()
    try:
        await h.button(1);await h.release(1)
        assert h.state.scene_menu_descriptor['page_count']==3
        await h.button(1);await h.release(1)  # first-page narration interrupt
        for _ in range(2):
            await h.button(6);await h.release(6)
            await h.button(1);await h.release(1)  # page narration interrupt
        page=h.state.scene_menu_descriptor
        assert page['items'][1]['id']=='repeat_pattern'
        assert page['items'][1]['slot']==2
        assert 5 in page['buttons'] and 7 in page['buttons']
        result=await h.button(2);await h.release(2)
        assert result['reason']=='game_started'
        assert h.state.active_game_id=='repeat_pattern'
        assert h.state.game_engine_state.phase=='instruction'
        assert h.app._active_audio[h.sid]['content_id']=='PHRASE_040'
        await h.complete()
        assert h.state.game_engine_state.phase=='demonstrate'
        old=h.app._game_timer_scopes[h.sid]
        await h.button(5)
        frame=dict(h.app.led.output.snapshot())
        assert h.state.mode=='wellbeing_check'
        assert await h.app._fire_game_timer(h.sid,old) is None
        assert h.app.led.output.snapshot()==frame
    finally:await h.close()
