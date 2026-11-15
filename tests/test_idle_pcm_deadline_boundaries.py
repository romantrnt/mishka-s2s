"""Retained independent actual-PCM deadline boundary regressions."""
import pytest
from test_product_clinical_memory import Harness
from test_engagement_waiting import terminal
from app.foundation.s2s import STTResult

@pytest.mark.asyncio
async def test_fresh_pcm_at_exact_deadline_cannot_start_new_utterance():
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.clock[0]=220
        result=await h.app.receive_audio_async('memory',(.2,)*8192,101,wait_for_turn=False)
        assert h.state.interaction_idle
        assert result['reason']=='interaction_idle_button_required'
    finally:await h.close()


@pytest.mark.asyncio
async def test_vad_utterance_started_before_deadline_can_finish_after_it():
    h=Harness()
    class Recognizer:
        async def transcribe(self,audio,cancelled):return STTResult('Привет',.99,0.,False)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.app.s2s_audio.recognizer=Recognizer();h.clock[0]=210
        await h.app.receive_audio_async('memory',(.2,)*8192,101,wait_for_turn=False)
        assert h.app.s2s_audio.ingress['memory'].active
        h.clock[0]=230
        await h.app.receive_audio_async('memory',(0.,)*4096,102,wait_for_turn=True)
        await h.app.expire_engagement_sessions()
        assert not h.state.interaction_idle
        assert h.app._engagement_owners['memory']['deadline']==350
    finally:await h.close()
