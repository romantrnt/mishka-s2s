import asyncio
import pytest
from test_product_clinical_memory import Harness
from test_engagement_waiting import terminal
from app.foundation.s2s import STTResult

async def drain(h):
    pending=tuple(h.app._audio_completions.get('memory',()))
    if pending:await asyncio.gather(*pending)
    await asyncio.sleep(0)

@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected",[("",280),("Привет",320)])
async def test_stt_overlapping_owned_audio_uses_correct_clock(text,expected):
    h=Harness();entered=asyncio.Event();release=asyncio.Event()
    class Recognizer:
        async def transcribe(self,audio,cancelled):
            entered.set();await release.wait();return STTResult(text,.99,0.,False)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6})
        assert h.app._active_audio.get('memory')
        h.app.s2s_audio.recognizer=Recognizer();h.clock[0]=150
        await h.app.receive_audio_async('memory',(.2,)*8192,101,wait_for_turn=False)
        await h.app.expire_engagement_sessions()
        await h.app.receive_audio_async('memory',(0.,)*4096,102,wait_for_turn=False)
        await entered.wait()
        h.clock[0]=160;await terminal(h)
        assert not h.app._active_audio.get('memory')
        assert not h.app._engagement_work_busy('memory')
        assert h.app._engagement_busy('memory')
        h.clock[0]=200;release.set();await drain(h);await h.app.expire_engagement_sessions()
        print('overlap after STT',h.app._engagement_owners['memory'])
        assert h.app._engagement_owners['memory']['deadline']==expected
    finally:
        release.set();await h.close()

@pytest.mark.asyncio
async def test_new_pcm_after_rejected_due_turn_does_not_reclaim_occupancy():
    h=Harness();entered=asyncio.Event();release=asyncio.Event()
    class Recognizer:
        async def transcribe(self,audio,cancelled):
            entered.set();await release.wait();return STTResult('',.99,0.,False)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.app.s2s_audio.recognizer=Recognizer();h.clock[0]=210
        await h.app.receive_audio_async('memory',(.2,)*8192,101,wait_for_turn=False)
        await h.app.expire_engagement_sessions()
        await h.app.receive_audio_async('memory',(0.,)*4096,102,wait_for_turn=False)
        await entered.wait();h.clock[0]=250
        await h.app.expire_engagement_sessions();assert not h.state.interaction_idle
        release.set();await drain(h)
        assert not h.app._engagement_busy('memory')
        h.clock[0]=251
        # The next PCM ingress is a real boundary, before the next 250ms monitor tick.
        result=await h.app.receive_audio_async('memory',(.2,)*8192,103,wait_for_turn=False)
        print('new expired PCM',result['reason'],h.app._engagement_owners.get('memory'))
        assert h.state.interaction_idle, 'Expired waiting must be applied before a new unverified utterance reacquires busy'
    finally:release.set();await h.close()
