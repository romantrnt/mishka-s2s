import asyncio
import pytest
from test_product_clinical_memory import Harness
from test_engagement_waiting import terminal
from app.foundation.s2s import STTResult, LocalS2SPipeline

async def dialogue(h):
    await h.send('HandsetLifted')
    await h.send('ButtonPressed', {'button_id':6})
    await h.send('ButtonReleased', {'button_id':6})
    await terminal(h)
    assert isinstance(h.app.s2s_audio, LocalS2SPipeline)
    assert h.app._engagement_owners['memory']['deadline'] == 220

@pytest.mark.asyncio
@pytest.mark.parametrize('wait_for_turn',[False,True])
@pytest.mark.parametrize('text,confidence,unknown',[('',.99,False),('неразборчиво',.2,True)])
async def test_actual_pending_stt_rejected_result_must_not_renew_wait(wait_for_turn,text,confidence,unknown):
    h=Harness();entered=asyncio.Event();release=asyncio.Event();request=None
    class Recognizer:
        async def transcribe(self,audio,cancelled):
            entered.set();await release.wait()
            return STTResult(text,confidence,0.,unknown)
    try:
        await dialogue(h);h.app.s2s_audio.recognizer=Recognizer()
        h.clock[0]=150
        first=await h.app.receive_audio_async('memory',(.2,)*8192,101,wait_for_turn=wait_for_turn)
        assert h.app.s2s_audio.ingress['memory'].active
        await h.app.expire_engagement_sessions()  # real monitor samples VAD activity
        assert h.app._engagement_owners['memory']['busy']
        request=asyncio.create_task(h.app.receive_audio_async('memory',(0.,)*4096,102,wait_for_turn=wait_for_turn))
        await asyncio.wait_for(entered.wait(),2)
        assert not h.state.interaction_idle
        await h.app.expire_engagement_sessions()
        assert h.app._engagement_owners['memory']['deadline']==220
        h.clock[0]=160;release.set();await request
        completions=tuple(h.app._audio_completions.get('memory',()))
        if completions:await asyncio.gather(*completions)
        await asyncio.sleep(0)
        await h.app.expire_engagement_sessions()
        owner=h.app._engagement_owners['memory']
        print({'wait':wait_for_turn,'text_empty':not text,'unknown':unknown,'owner_after':dict(owner)})
        assert owner['deadline']==220, 'Rejected STT result granted a new 120-second lease'
    finally:
        release.set()
        if request:await asyncio.gather(request,return_exceptions=True)
        await h.close()

@pytest.mark.asyncio
async def test_quiet_pcm_keeps_original_wait_and_expires():
    h=Harness()
    try:
        await dialogue(h)
        for seq,now in enumerate([150,180,219.999],101):
            h.clock[0]=now
            await h.app.receive_audio_async('memory',(0.,)*8192,seq,wait_for_turn=False)
            await h.app.expire_engagement_sessions()
            assert not h.app.s2s_audio.ingress['memory'].active
            assert h.app._engagement_owners['memory']['deadline']==220
        h.clock[0]=220
        result=await h.app.expire_engagement_sessions()
        assert result[0]['reason']=='interaction_idle'
        assert h.state.interaction_idle
    finally:await h.close()

@pytest.mark.asyncio
async def test_rejected_short_vad_must_not_renew_wait():
    h=Harness()
    try:
        await dialogue(h);h.clock[0]=150
        await h.app.receive_audio_async('memory',(.2,)*512,101,wait_for_turn=False)
        assert h.app.s2s_audio.ingress['memory'].active
        await h.app.expire_engagement_sessions()
        h.clock[0]=160
        result=await h.app.receive_audio_async('memory',(0.,)*4096,102,wait_for_turn=False)
        assert result['audio_status']=='too_short'
        await h.app.expire_engagement_sessions()
        print({'short_vad_owner':dict(h.app._engagement_owners['memory'])})
        assert h.app._engagement_owners['memory']['deadline']==220
    finally:await h.close()
