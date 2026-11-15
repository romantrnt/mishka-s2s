import asyncio
import pytest
from app.foundation.s2s import STTResult
from test_product_clinical_memory import Harness
from test_engagement_waiting import terminal

@pytest.mark.asyncio
@pytest.mark.parametrize('text',['','непонятный обычный текст'])
async def test_actual_pending_unknown_stt_never_extends_wait(text):
    h=Harness();entered,release=asyncio.Event(),asyncio.Event()
    class Recognizer:
        async def transcribe(self,audio,cancelled):
            entered.set();await release.wait();return STTResult(text,.1,0.,True)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.app.s2s_audio.recognizer=Recognizer()
        deadline=h.app._engagement_owners['memory']['deadline']
        h.clock[0]=150
        await h.app.receive_audio_async('memory',(.2,)*8192,100,wait_for_turn=False)
        await h.app.expire_engagement_sessions()
        await h.app.receive_audio_async('memory',(0.,)*4096,101,wait_for_turn=False)
        await entered.wait();await h.app.expire_engagement_sessions()
        h.clock[0]=180;release.set()
        await asyncio.gather(*tuple(h.app._audio_completions.get('memory',())))
        await h.app.expire_engagement_sessions()
        assert h.app._engagement_owners['memory']['deadline']==deadline
    finally:
        release.set();await h.close()

@pytest.mark.asyncio
async def test_quiet_pcm_never_extends_wait():
    h=Harness()
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        deadline=h.app._engagement_owners['memory']['deadline']
        for n in range(4):
            h.clock[0]=110+n*20
            await h.app.receive_audio_async('memory',(0.,)*4096,100+n,wait_for_turn=False)
            await h.app.expire_engagement_sessions()
        assert h.app._engagement_owners['memory']['deadline']==deadline
    finally:await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('wait',[False,True])
async def test_actual_verified_stt_after_busy_is_fresh_user_activity(wait):
    h=Harness();entered,release=asyncio.Event(),asyncio.Event();pending=None
    class Recognizer:
        async def transcribe(self,audio,cancelled):
            entered.set();await release.wait();return STTResult('Привет',.99,0.,False)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.app.s2s_audio.recognizer=Recognizer();h.clock[0]=150
        await h.app.receive_audio_async('memory',(.2,)*8192,100,wait_for_turn=wait)
        pending=asyncio.create_task(h.app.receive_audio_async('memory',(0.,)*4096,101,wait_for_turn=wait))
        await entered.wait();await h.app.expire_engagement_sessions()
        h.clock[0]=180;release.set();await pending
        await asyncio.gather(*tuple(h.app._audio_completions.get('memory',())))
        await h.app.expire_engagement_sessions()
        assert h.app._engagement_owners['memory']['deadline']==300
    finally:
        release.set()
        if pending:await pending
        await h.close()
