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
async def test_question_playback_failure_retries_then_blocks_unheard_answers_until_manual_retry():
    h=GameHarness()
    try:
        await h.start(2);await h.terminal()
        first=dict(h.app._active_audio[h.sid])
        result=await h.terminal('AudioFailed')
        assert result['reason']=='game_prompt_retried'
        retry=dict(h.app._active_audio[h.sid])
        assert retry['playback_lease']!=first['playback_lease']
        await h.terminal('AudioCompleted',first)
        assert h.app._active_audio[h.sid]==retry and h.state.game_audio_stage=='prompt'
        result=await h.terminal('AudioFailed',retry)
        assert result['reason']=='game_prompt_audio_failed'
        assert h.state.mode=='game_active' and h.state.game_audio_stage=='prompt_failed'
        assert h.state.expected_inputs==['button_5','button_7','handset_down']
        assert h.sid not in h.app._active_audio
        assert h.sid not in h.app._game_timer_scopes
        for button in range(1,5):
            assert (await h.press(button))['reason']=='game_prompt_waiting'
            assert h.state.game_engine_state.round==0
        assert (await h.press(5))['reason']=='game_prompt_manual_retry'
        assert h.state.game_audio_stage=='prompt' and h.state.game_engine_state.round==0
        assert (await h.terminal())['reason']=='game_prompt_finished'
        assert h.state.game_audio_stage=='playing' and h.state.game_engine_state.round==0
    finally:await h.close()


@pytest.mark.asyncio
async def test_start_recording_dispatch_failure_still_allows_the_valid_tts_prompt():
    h=GameHarness()
    try:
        await h.send('HandsetLifted');await h.press(1)
        failed=[]
        def fail(handle,*args,**kwargs):
            failed.append(handle.response_id)
            raise OSError('speaker unavailable')
        h.app.audio.play_owned=fail
        result=await h.press(2)
        assert result['reason']=='game_prompt_started'
        assert failed==['PHRASE_020']
        assert h.state.active_game_id=='quiz'
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
