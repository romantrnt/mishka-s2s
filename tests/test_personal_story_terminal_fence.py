import pytest
from app.foundation.canonical_contract import CanonicalEvent
from app.foundation.personal_story import STORY_ID
from test_personal_story_app import start_app,enter,send

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['AudioCompleted','AudioFailed'])
@pytest.mark.parametrize('lease',[None,'old-lease'])
async def test_canonical_terminal_requires_current_story_lease(kind,lease):
    app=await start_app()
    try:
        await enter(app);await send(app,'ButtonPressed',{'button_id':1})
        state=app.canonical_runtime.sessions['story-test']
        payload={'response_id':STORY_ID}
        if lease:payload['playback_lease']=lease
        result=app.canonical_runtime.handle(CanonicalEvent(kind,'story-test',999,state.state_version,'synthetic',payload,'bad-completion',99.,99.))
        assert state.mode=='personal_story_playing',result
        assert result['reason'] in {'event_ignored','personal_story_completion_stale'}
    finally:await app.shutdown_identity()
