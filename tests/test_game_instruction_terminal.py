"""The new game block stages start/instructions under one owned prompt lease."""
import pytest
from test_game_exit_affordance import GameHarness


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['AudioCompleted','AudioFailed'])
@pytest.mark.parametrize('slot',[1,2,3,4,5])
async def test_start_terminal_advances_to_the_owned_game_prompt_without_scoring(terminal,slot):
    h=GameHarness()
    try:
        await h.start(slot)
        old=dict(h.app._active_audio[h.sid])
        assert old['content_id']=='PHRASE_020'
        result=await h.terminal(terminal,old)
        assert result['reason']=='game_prompt_started'
        assert h.state.mode=='game_active' and h.state.game_audio_stage=='prompt'
        assert h.app._active_audio[h.sid]['content_id']=='GAME_PROMPT'
        assert h.state.game_engine_state.round==0
        current=dict(h.app._active_audio[h.sid]);frame=h.app.led.output.snapshot()
        await h.terminal(terminal,old)
        assert h.app._active_audio[h.sid]==current and h.app.led.output.snapshot()==frame
        await h.terminal()
        assert h.state.game_audio_stage=='playing'
        assert h.state.game_engine_state.round==0
    finally:await h.close()


@pytest.mark.asyncio
async def test_question_playback_failure_exits_instead_of_accepting_an_unheard_answer():
    h=GameHarness()
    try:
        await h.start(2);await h.terminal()
        result=await h.terminal('AudioFailed')
        assert result['reason']=='game_prompt_audio_failed'
        assert h.state.mode=='root_menu' and h.state.game_engine_state is None
        assert h.sid not in h.app._active_audio
    finally:await h.close()


@pytest.mark.asyncio
async def test_start_recording_dispatch_failure_still_allows_the_valid_tts_prompt():
    h=GameHarness()
    try:
        await h.send('HandsetLifted');await h.press(1);await h.press(1)
        def fail(*args,**kwargs):raise OSError('speaker unavailable')
        h.app.audio.play_owned=fail
        result=await h.press(1)
        assert result['reason']=='game_prompt_started'
        assert h.state.mode=='game_active' and h.state.game_audio_stage=='prompt'
        assert h.app._active_audio[h.sid]['content_id']=='GAME_PROMPT'
    finally:await h.close()


@pytest.mark.asyncio
async def test_spam_during_question_preserves_audio_owner_until_owned_completion():
    h=GameHarness()
    try:
        await h.start(4);await h.terminal()
        owner=dict(h.app._active_audio[h.sid]);version=h.state.state_version
        for button in range(1,6):await h.press(button)
        assert h.state.state_version==version and h.app._active_audio[h.sid]==owner
        assert h.state.game_engine_state.round==0
        assert (await h.terminal())['reason']=='game_prompt_finished'
    finally:await h.close()
