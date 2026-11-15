import asyncio
import pytest
import pytest_asyncio
from app.foundation.domain import Event
from app.foundation.config import Settings,SettingsError
from test_profile_software_chain import build_chain,attach_client

@pytest_asyncio.fixture
async def ready():
    app,clock,led,output=build_chain();clock.value=10.
    app.now=lambda:clock()
    client,boundary=attach_client(app)
    client.ready();await boundary.step()
    seq=0
    async def send(kind,button=None,**payload):
        nonlocal seq
        seq+=1
        if button is not None:payload['button_id']=button
        return await app.receive_async(Event(kind,'software',seq,clock(),'test',payload,f'idle:{seq}',clock()))
    try:yield app,clock,led,output,client,boundary,send
    finally:
        await boundary.close();await app.shutdown_identity()


def test_clinical_max_remains_blocked_no_config_bypass():
    with pytest.raises(SettingsError):
        Settings(profile='mock',runtime_mode='clinical',identity_mode='max_json').validate()

@pytest.mark.asyncio
async def test_first_button_claims_once_then_distinct_120_waiting_clock(ready):
    app,clock,led,output,client,boundary,send=ready
    state=app.canonical_runtime.sessions['software'];expiry=state.identity_expires_monotonic
    assert state.identity_ready_deadline==70.
    clock.value=69.
    assert (await send('ButtonPressed',1))['reason']=='profile_ready_engaged'
    assert state.identity_ready_deadline is None and state.identity_expires_monotonic==expiry
    assert state.mode=='profile_ready' and state.handset=='down'
    owner=app._engagement_owners['software'];assert owner['deadline']==189.
    clock.value=80.
    await send('ButtonPressed',1);await send('ButtonHeld',1);await send('ButtonReleased',1)
    assert app._engagement_owners['software']['deadline']==189.
    clock.value=100.
    await send('ButtonPressed',1);await send('ButtonReleased',1)
    assert app._engagement_owners['software']['deadline']==220.
    clock.value=219.999;assert await app.expire_engagement_sessions()==[]
    clock.value=220.;assert (await app.expire_engagement_sessions())[0]['reason']=='interaction_idle'
    assert state.handset=='down' and state.interaction_idle
    assert not any(output.snapshot().values())
    assert (await send('ButtonPressed',1))['reason']=='profile_ready_engaged'
    state=app.canonical_runtime.sessions['software']
    assert state.mode=='profile_ready' and not state.interaction_idle
    assert state.identity_ready_deadline is None
    assert 'software' not in app._active_audio
    result=await send('HandsetLifted')
    state=app.canonical_runtime.sessions['software']
    assert state.handset=='up' and state.profile_status=='identified', result
    assert state.welcome_status=='pending'

@pytest.mark.asyncio
async def test_unengaged_exact60_and_immediate_dark_before_slow_cleanup(ready):
    app,clock,led,output,client,boundary,send=ready
    clock.value=69.999;assert await app.expire_identity_leases()==[]
    entered,release=asyncio.Event(),asyncio.Event()
    old=app._cleanup_session_resources
    async def slow(*args,**kwargs):
        entered.set();await release.wait();return await old(*args,**kwargs)
    app._cleanup_session_resources=slow;clock.value=70.
    task=asyncio.create_task(app.expire_identity_leases())
    try:
        await entered.wait()
        assert not any(output.snapshot().values())
        assert not app._identity_scope_live('software')
        assert 'software' not in app._active_audio
    finally:
        release.set();await task;app._cleanup_session_resources=old

@pytest.mark.asyncio
async def test_reauthorization_old_request_cannot_clear_new_scope(ready):
    app,clock,led,output,client,boundary,send=ready
    old=app.canonical_runtime.sessions['software'].identity_request_id
    clock.value=50.;client.ready();await boundary.step()
    state=app.canonical_runtime.sessions['software']
    assert state.identity_request_id!=old and state.identity_ready_deadline==110.
    clock.value=70.
    result=await app._receive_canonical_event(app._identity_event('software','IdentityReadyExpired',{'request_id':old}),identity_event_authorized=True)
    assert result['reason']=='stale_profile_context'
    assert state.profile_status=='identified'

@pytest.mark.asyncio
async def test_engaged_ready_still_obeys_real_revoke_and_auth_expiry(ready):
    app,clock,led,output,client,boundary,send=ready
    await send('ButtonPressed',1);await send('ButtonReleased',1)
    state=app.canonical_runtime.sessions['software']
    assert state.identity_ready_deadline is None
    clock.value=state.identity_expires_monotonic
    result=await app.expire_engagement_sessions()
    assert state.profile_ref is None
    assert not any(output.snapshot().values())
    assert 'software' not in app._engagement_owners

@pytest.mark.asyncio
async def test_actual_client_clear_wins_over_busy_playback(ready):
    app,clock,led,output,client,boundary,send=ready
    await send('HandsetLifted');clock.value+=5
    await send('WelcomeTimerFired')
    assert app._active_audio.get('software')
    client.profile_cleared();await boundary.step()
    assert app.canonical_runtime.sessions['software'].profile_ref is None
    assert 'software' not in app._active_audio
    assert 'software' not in app._engagement_owners
