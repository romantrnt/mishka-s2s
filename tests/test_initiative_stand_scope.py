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
        assert not old[1](), 'queued old speech must not consent to the new proposal'
        delivered = []
        async def inject(*args, **kwargs):
            delivered.append(args)
            return {}
        stand.inject_pcm = inject
        await stand._headless_audio_queue.put(None)
        await stand._headless_audio_worker()
        assert delivered == []
        fresh = stand._headless_frame_owner()
        assert fresh[1]()
        assert fresh[1].initiative_scope == tuple(scope)
        scope[2:] = ['replacement', 'offer', True]
        assert not fresh[1]()
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
