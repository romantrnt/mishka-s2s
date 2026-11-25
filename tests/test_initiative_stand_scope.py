"""Stand capture ownership, with speaker and initiative state deliberately synthetic."""
import pytest
from test_headless_turn_taking import active_stand, close


@pytest.mark.asyncio
async def test_queued_physical_pcm_cannot_adopt_a_later_dialogue_proposal(monkeypatch):
    stand, _ = await active_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    scope = [id(app.canonical_runtime.sessions[sid]), 0, None, None, False]
    app._dialogue_initiative_scope = lambda current_sid: tuple(scope) if current_sid == sid else None
    try:
        old = stand._headless_frame_owner()
        assert old[1]()
        stand._enqueue_headless_audio_frame((.2,) * 512, old)
        scope[2:] = ['new-proposal', 'offer', True]
        assert old[1].lifecycle_current(), 'the visit itself remains current'
        assert old[1](), 'admitted same-visit speech must remain decodable for urgent content'
        assert old[1].initiative_scope[2] is None, 'old speech must not acquire the new proposal'
        delivered = []
        async def inject(*args, **kwargs):
            delivered.append(kwargs['_ingress_guard'].initiative_scope)
            return {}
        stand.inject_pcm = inject
        await stand._headless_audio_queue.put(None)
        await stand._headless_audio_worker()
        assert len(delivered) == 1 and delivered[0][2] is None
        fresh = stand._headless_frame_owner()
        assert fresh[1]()
        assert fresh[1].initiative_scope == tuple(scope)
        scope[2:] = ['replacement', 'offer', True]
        assert fresh[1]() and fresh[1].initiative_scope[2] == 'new-proposal'
    finally:
        await close(stand)


@pytest.mark.asyncio
async def test_browser_upload_guard_preserves_the_current_proposal_scope(monkeypatch):
    stand, _ = await active_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    scope = [id(app.canonical_runtime.sessions[sid]), 0, 'current', 'offer', True]
    app._dialogue_initiative_scope = lambda current_sid: tuple(scope) if current_sid == sid else None
    try:
        guard = stand._audio_owner_guard(sid)
        assert guard() and guard.initiative_scope == tuple(scope)
        scope[-1] = False
        assert guard() and guard.lifecycle_current(), 'urgent speech retains its visit owner'
        assert guard.initiative_scope[-1] is True, 'the old admission snapshot remains immutable'
        scope[2:] = [None, None, False]
        ordinary = stand._audio_owner_guard(sid)
        assert ordinary()
        scope[2:] = ['new-proposal', 'offer', True]
        assert ordinary(), 'visit ownership must survive a proposal-only change'
        assert ordinary.initiative_scope[2] is None, 'an upload without an offer cannot acquire one later'
    finally:
        await close(stand)


@pytest.mark.asyncio
async def test_owned_browser_completion_cannot_overtake_final_pcm_delivery():
    """A real HTTP ACK held ahead of the socket send is not a completed offer."""
    import asyncio
    import httpx
    from test_browser_private_pcm import setup_browser, cleanup, welcome

    api, stand, boundary, _, clock, writer, _ = await setup_browser()
    held, release = asyncio.Queue(), asyncio.Event()
    original = writer.send_json
    async def delayed_send(payload):
        if payload.get('type') == 'audio_pcm_output' and payload.get('final'):
            await held.put(payload['playback'])
            await release.wait()
        await original(payload)
    writer.send_json = delayed_send
    pending = asyncio.create_task(welcome(stand, clock))
    try:
        identity = await asyncio.wait_for(held.get(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://stand',
                headers={'x-stand-token': 'private-pcm-unit-test'}) as client:
            reply = await client.post('/stand/audio/completed', json=identity)
            assert reply.json()['reason'] == 'stale_private_pcm_callback', 'the final PCM was not delivered'
            release.set()
            await asyncio.wait_for(pending, 3)
            reply = await client.post('/stand/audio/completed', json=identity)
            assert reply.json()['reason'] == 'personal_welcome_menu_started'
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await cleanup(stand, boundary)
