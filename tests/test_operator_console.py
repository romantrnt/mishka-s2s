"""Operator diagnostics stay private, read-only and separate from transcripts."""
import json

import httpx
import pytest

from app.foundation.config import Settings
from app.foundation.stand import create_stand_app

TOKEN = 'synthetic_operator_' + 'a' * 48
PATHS = ('/console/operator', '/console/operator.js', '/console/operator.css', '/stand/operator')


@pytest.mark.asyncio
async def test_operator_is_private_even_without_configured_credentials():
    for token in ('', TOKEN):
        api = create_stand_app(Settings(profile='mock', stand_access_token=token))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
                for path in PATHS:
                    denied = await client.get(path)
                    assert denied.status_code in (401, 503), (path, denied.status_code)
                    assert 'operator-timeline' not in denied.text
                    if token:
                        assert (await client.get(path, headers={'x-stand-token': token})).status_code == 200
        finally:
            await api.state.stand.aclose()


@pytest.mark.asyncio
async def test_operator_poll_is_available_while_degraded_without_taking_audio_lease():
    api = create_stand_app(Settings(profile='mock', stand_access_token=TOKEN))
    stand = api.state.stand
    try:
        before = stand.hub.lease_snapshot()
        state = dict(stand.application.last_canonical_state)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test',
                                     headers={'x-stand-token': TOKEN}) as client:
            response = await client.get('/stand/operator')
            assert response.status_code == 200
            result = response.json()
            assert result['storage'] == 'bounded_process_memory'
            assert result['transcripts_enabled'] is False
            assert response.headers['cache-control'] == 'no-store'
            assert result['alert'] is None
            assert (await client.get('/stand/operator?after=-1')).status_code == 422
            assert (await client.post('/stand/operator')).status_code == 405
        assert stand.hub.lease_snapshot() == before
        assert stand.application.last_canonical_state == state
        assert not stand.application._active_audio
    finally:
        await stand.aclose()


def test_operator_ring_redacts_every_source_and_handles_eviction_restart_and_reset():
    from app.foundation.operator_monitor import OperatorMonitor, OperatorErrorBuffer
    secret = 'SYNTHETIC_PRIVATE_CANARY_NAME_UID_TOKEN_SPEECH'
    monitor = OperatorMonitor(capacity=8)
    state = {'mode':'safety_stop', 'support_alert_kind':'urgent_safety', 'display_name':secret,
             'session_id':secret, 'safety_reason':secret}
    monitor.state(state)
    monitor.state(state)  # repeated state does not duplicate a safety alert
    monitor.event({'type':'ButtonPressed', 'session_id':secret, 'source':secret,
                   'payload':{'button_id':7,'text':secret,'uid':secret,'nested':{'token':secret}}})
    monitor.event({'type':secret,'payload':{'button_id':1}})
    errors = OperatorErrorBuffer(monitor, maxlen=2)
    errors.append({'stage':secret,'detail':secret,'error_code':secret})
    errors.append({'stage':'hid_reader','detail':secret})
    health = {'status':'degraded', 'handset':'down', 'audio':{'status':'unavailable','device':secret},
              'hid':{'status':'active','path':secret},'lighting':{'status':'available'},'config':secret}
    result = monitor.snapshot(health=health,runtime='DEGRADED')
    assert secret not in json.dumps(result)
    assert result['alert']['kind'] == 'urgent_safety'
    assert result['alert']['adult_acknowledged'] is False
    assert result['alert']['delivery_confirmed'] is False
    assert len([i for i in result['items'] if i['kind']=='alert_opened']) == 1
    assert any(i['data']=={'event':'ButtonPressed','button':7} for i in result['items'])
    assert any(i['data']=={'component':'unknown'} for i in result['items'])
    assert len(errors) == 2
    same = monitor.snapshot(after=result['cursor'],stream=result['stream'],health=health,runtime='DEGRADED')
    assert same['items'] == [] and same['gap'] is False
    for _ in range(15):
        monitor.event({'type':'ButtonPressed','payload':{'button_id':1}})
    gap = monitor.snapshot(after=result['cursor'],stream=result['stream'],health=health,runtime='DEGRADED')
    assert gap['gap'] is True and len(gap['items']) <= 8
    assert len({i['cursor'] for i in gap['items']}) == len(gap['items'])
    monitor.reset()
    after = monitor.snapshot(health=health,runtime='DEGRADED',stream='old-process')
    assert after['alert'] is None and after['gap'] is True
    assert any(i['kind']=='alert_ended' and i['data']['adult_acknowledged'] is False for i in after['items'])
    assert monitor.snapshot(after=10**9)['gap'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized', [False, True])
@pytest.mark.parametrize('up', [False, True])
async def test_actual_help_lifecycle_is_collected_without_an_open_operator(tmp_path, authorized, up):
    from test_panel_session_actions import PanelStand
    h = PanelStand(tmp_path, identity=authorized)
    try:
        if authorized:
            await h.ready()
        await h.control('start')
        await h.menu()
        if up:
            await h.simulator.action({'action':'handset','state':'up'})
        await h.key(5)
        await h.key(7)
        assert h.state.mode == 'safety_stop'
        before = h.stand.operator.snapshot()
        assert before['alert']['kind'] == 'help_requested'
        assert any(i['kind']=='event' and i['data'].get('event')=='ButtonPressed' for i in before['items'])
        await h.control('end')
        ended = h.stand.operator.snapshot()
        assert ended['alert'] is None
        assert any(i['kind']=='alert_ended' for i in ended['items'])
        if authorized:
            await h.ready()
        else:
            await h.control('start')
        fresh = h.stand.operator.snapshot()
        assert fresh['alert'] is None
        assert len([i for i in fresh['items'] if i['kind']=='alert_opened']) == 1
        assert all(i['data'].get('adult_acknowledged') is not True for i in fresh['items'])
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_operator_api_does_not_export_canaries_from_existing_diagnostic_buffers():
    api = create_stand_app(Settings(profile='mock', stand_access_token=TOKEN))
    stand = api.state.stand
    try:
        secret = 'SYNTHETIC_PRIVATE_OPERATOR_SENTINEL'
        stand.errors.append({'stage':secret,'detail':secret,'error_code':secret})
        stand.application.last_canonical_state = {'mode':'root_menu','display_name':secret,'uid':secret}
        stand.application.canonical_trace.append({'input':secret,'state_after':secret,'effects':[secret]})
        stand.results.append({'reason':secret,'canonical_state':{'profile':secret}})
        stand.publisher.events.append({'type':secret,'payload':{'text':secret}})
        stand._collect_events()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test',headers={'x-stand-token':TOKEN}) as client:
            response = await client.get('/stand/operator')
            assert response.status_code == 200
            assert secret not in response.text
            assert TOKEN not in response.text
            assert 'transcript' not in response.text.replace('transcripts_enabled','')
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_operator_gateway_rejects_query_credentials_and_protects_assets():
    from app.foundation.remote_access import PrivateStandGateway
    import base64
    api = create_stand_app(Settings(profile='mock', stand_access_token=TOKEN))
    gateway = PrivateStandGateway(api, token=TOKEN, allowed_origins=['http://test'], secure_cookie=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url='http://test') as client:
            for path in PATHS:
                assert (await client.get(path)).status_code == 401
                assert (await client.get(f'{path}?token={TOKEN}')).status_code == 401
            basic = base64.b64encode(f'mishka:{TOKEN}'.encode()).decode()
            response = await client.get('/console/operator',headers={'Authorization':'Basic '+basic})
            assert response.status_code == 200 and 'operator-timeline' in response.text
            assert 'frame-ancestors' in response.headers['content-security-policy']
            assert (await client.get('/stand/operator')).status_code == 200
            assert (await client.get('/console/operator.js')).status_code == 200
    finally:
        await api.state.stand.aclose()


@pytest.mark.asyncio
async def test_urgent_alert_is_visible_before_old_audio_cleanup_finishes(tmp_path):
    import asyncio
    from app.foundation.domain import Event
    from test_panel_session_actions import PanelStand
    from test_local_safety_application import block_cleanup, help_payload
    h = PanelStand(tmp_path)
    request = None
    release = None
    try:
        await h.control('start')
        await h.menu()
        entered, release, _, _ = block_cleanup(h.app)
        request = asyncio.create_task(h.app.receive_async(Event('VoiceIntentReady','stand',900,1.,'operator-synthetic',help_payload('urgent'))))
        await asyncio.wait_for(entered.wait(), 2)
        assert h.state.mode == 'safety_stop'
        assert not request.done()
        alert = h.stand.operator.snapshot()['alert']
        assert alert is not None and alert['kind'] == 'urgent_safety'
        release.set()
        await request
        await h.control('end')
        assert h.stand.operator.snapshot()['alert'] is None
    finally:
        if release:
            release.set()
        if request:
            await asyncio.gather(request,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_operator_observer_failure_cannot_prevent_safety_stop(tmp_path):
    from app.foundation.domain import Event
    from test_panel_session_actions import PanelStand
    from test_local_safety_application import help_payload
    h = PanelStand(tmp_path)
    try:
        await h.control('start')
        await h.menu()
        def fail(*_):
            raise RuntimeError('SYNTHETIC_PRIVATE_EXCEPTION_DETAIL')
        h.app.technical_state_observer = fail
        result = await h.app.receive_async(Event('VoiceIntentReady','stand',900,1.,'operator-synthetic',help_payload('urgent')))
        assert result['reason'] == 'urgent_safety'
        assert h.state.mode == 'safety_stop'
        assert {'kind':'operator_observation_failed'} in h.app.canonical_diagnostics
        assert 'SYNTHETIC_PRIVATE_EXCEPTION_DETAIL' not in json.dumps(list(h.app.canonical_diagnostics))
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_late_old_audio_after_end_and_new_start_cannot_reopen_operator_alert(tmp_path):
    from test_panel_session_actions import PanelStand
    h = PanelStand(tmp_path)
    try:
        await h.control('start')
        await h.menu()
        await h.simulator.action({'action':'handset','state':'up'})
        await h.key(5)
        await h.key(7)
        old = dict(h.app._active_audio['stand'])
        assert h.stand.operator.snapshot()['alert']['kind'] == 'help_requested'
        await h.control('end')
        await h.control('start')
        before = h.stand.operator.snapshot()
        await h.done(old)
        after = h.stand.operator.snapshot()
        assert after['alert'] is None
        assert len([i for i in before['items'] if i['kind']=='alert_opened']) == len([i for i in after['items'] if i['kind']=='alert_opened'])
    finally:
        await h.close()


def test_concurrent_operator_reads_preserve_unique_ordered_bounded_cursors():
    from concurrent.futures import ThreadPoolExecutor
    from app.foundation.operator_monitor import OperatorMonitor
    monitor = OperatorMonitor()
    def work(_):
        for _ in range(100):
            monitor.event({'type':'ButtonPressed','payload':{'button_id':1}})
            value = monitor.snapshot()
            cursors = [item['cursor'] for item in value['items']]
            assert cursors == sorted(set(cursors))
            assert len(cursors) <= 240
            assert value['alert'] is None
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work,range(4)))
    assert len(monitor.snapshot()['items']) == 240
