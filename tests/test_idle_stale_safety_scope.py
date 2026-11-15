import asyncio
import pytest
from test_product_clinical_memory import Harness
from test_engagement_waiting import terminal
from app.foundation.input_safety import InputSafetyDecision

@pytest.mark.asyncio
async def test_old_pending_safety_classifier_cannot_hold_new_epoch_open():
    h=Harness();entered=asyncio.Event();release=asyncio.Event();pending=None
    class Classifier:
        async def classify(self,text):
            entered.set();await release.wait()
            return InputSafetyDecision('none','synthetic',True)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.app.safety_classifier=Classifier();h.clock[0]=110
        pending=asyncio.create_task(h.send('SpeechReceived',text='Привет'))
        await entered.wait()
        old_epoch=h.app._session_epochs.get('memory',0)
        await h.send('HandsetPlaced')
        await h.send('ButtonPressed',{'button_id':1});await h.send('ButtonReleased',{'button_id':1})
        assert h.state.mode=='game_menu'
        assert h.app._session_epochs['memory']>old_epoch
        assert not h.app._engagement_busy('memory')
        deadline=h.app._engagement_owners['memory']['deadline'];h.clock[0]=deadline
        result=await h.app.expire_engagement_sessions()
        print('stale safety',h.app._engagement_ingress,h.app._engagement_owners,result)
        assert h.state.interaction_idle, 'Old-epoch pending classifier may not retain the new waiting owner'
    finally:
        release.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        await h.close()

@pytest.mark.asyncio
async def test_old_classifier_finally_does_not_release_new_scope_occupancy():
    h=Harness();old_entered=asyncio.Event();new_entered=asyncio.Event()
    old_release=asyncio.Event();new_release=asyncio.Event();old=None;new=None
    class Classifier:
        async def classify(self,text):
            if text=='старый':old_entered.set();await old_release.wait()
            else:new_entered.set();await new_release.wait()
            return InputSafetyDecision('none','synthetic',True)
    try:
        await h.send('HandsetLifted');await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6});await terminal(h)
        h.app.safety_classifier=Classifier()
        old=asyncio.create_task(h.send('SpeechReceived',text='старый'));await old_entered.wait()
        await h.send('HandsetPlaced');await h.send('HandsetLifted')
        await h.send('ButtonPressed',{'button_id':6});await h.send('ButtonReleased',{'button_id':6})
        if 'memory' in h.app._active_audio:await terminal(h)
        new=asyncio.create_task(h.send('SpeechReceived',text='новый'));await new_entered.wait()
        scope=('memory',h.app._engagement_scope('memory'))
        assert h.app._engagement_ingress[scope]==1 and len(h.app._engagement_ingress)==2
        old_release.set();await old
        assert h.app._engagement_ingress=={scope:1}
        h.clock[0]=h.app._engagement_owners['memory']['deadline']
        assert await h.app.expire_engagement_sessions()==[]
        assert not h.state.interaction_idle and not new.done()
    finally:
        old_release.set();new_release.set()
        await asyncio.gather(*(p for p in (old,new) if p),return_exceptions=True)
        await h.close()
