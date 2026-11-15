"""Incident delivery remains owned across unrelated UI events and caller cancellation.

All transports and playback are synthetic; real canonical routing and approved
recording dispatch are exercised without contacting an adult or opening a device.
"""
import asyncio

import pytest
import pytest_asyncio

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event


class DelayedDelivery:
    def __init__(self, outcome):
        self.outcome = outcome
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def request(self, payload):
        self.requests.append(dict(payload))
        self.entered.set()
        await self.release.wait()
        if self.outcome == 'failure':
            raise RuntimeError('synthetic transport failure')
        if self.outcome == 'invalid':
            return {'acknowledged': 'true'}
        if self.outcome == 'simulated':
            return {'acknowledged': True, 'simulated': True}
        return {'acknowledged': True}


@pytest_asyncio.fixture
async def harness():
    app, *_ = build_application(settings=Settings(profile='mock'), background_scheduler=False)
    played = []
    original_play = app.audio.play_owned
    def play(handle, asset_id, offset_ms=0):
        played.append(handle)
        return original_play(handle, asset_id, offset_ms=offset_ms)
    app.audio.play_owned = play
    sequence = 0
    async def send(kind, payload=None, text=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, 'handoff-audit', sequence, 1., 'audit',
            payload or {}, f'audit:{sequence}'), text)
    await send('HandsetLifted')
    try:
        yield app, send, played
    finally:
        await app.shutdown_identity()
        await app._cleanup_session_resources('handoff-audit')


def handoff_phrases(played):
    return [handle.response_id for handle in played if handle.response_id in {'PHRASE_089', 'PHRASE_091'}]


async def finish_delivery(app, delivery, request):
    tasks = tuple(app._handoff_delivery_tasks)
    delivery.release.set()
    result = await asyncio.wait_for(request, 2)
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    return result


@pytest.mark.asyncio
async def test_acknowledgement_dispatches_recording_exactly_once(harness):
    app, send, played = harness
    delivery = DelayedDelivery('ack')
    delivery.release.set()
    app.adult_handoff = delivery
    result = await send('SpeechReceived', text='не хочу жить')
    assert result['reason'] == 'urgent_safety'
    assert handoff_phrases(played) == ['PHRASE_089']
    assert len(delivery.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['ack', 'failure', 'invalid', 'simulated'])
async def test_button_during_delivery_cannot_erase_terminal_outcome(harness, outcome):
    app, send, played = harness
    delivery = DelayedDelivery(outcome)
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    await asyncio.wait_for(delivery.entered.wait(), 2)
    incident = app.canonical_runtime.sessions['handoff-audit'].incident_id
    await send('ButtonPressed', {'button_id': 7})
    await send('ButtonReleased', {'button_id': 7})
    await send('ButtonPressed', {'button_id': 7})
    await finish_delivery(app, delivery, request)
    assert handoff_phrases(played) == ['PHRASE_089' if outcome == 'ack' else 'PHRASE_091']
    state = app.canonical_runtime.sessions['handoff-audit']
    assert state.incident_id == incident and state.safety_severity == 'urgent'
    assert state.mode == 'safety_handoff'


@pytest.mark.asyncio
async def test_cancelled_ingress_does_not_cancel_incident_failure_notification(harness):
    app, send, played = harness
    delivery = DelayedDelivery('failure')
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    await asyncio.wait_for(delivery.entered.wait(), 2)
    tasks = tuple(app._handoff_delivery_tasks)
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    delivery.release.set()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    assert handoff_phrases(played) == ['PHRASE_091']
    assert app.canonical_runtime.sessions['handoff-audit'].safety_severity == 'urgent'


@pytest.mark.asyncio
async def test_superseded_audio_cleanup_still_delivers_incident_failure(harness):
    app, send, played = harness
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    original_cancel = app._cancel_audio_owner
    async def cancel(owner, session_id, **kwargs):
        if kwargs.get('operation') == 'audio_stop' and owner is app.bounded_s2s:
            cleanup_entered.set()
            await cleanup_release.wait()
        return await original_cancel(owner, session_id, **kwargs)
    app._cancel_audio_owner = cancel
    delivery = DelayedDelivery('failure')
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        await asyncio.wait_for(delivery.entered.wait(), 2)
        await send('ButtonPressed', {'button_id': 7})
        tasks = tuple(app._handoff_delivery_tasks)
        delivery.release.set()
        cleanup_release.set()
        await asyncio.wait_for(request, 2)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert handoff_phrases(played) == ['PHRASE_091']
    finally:
        cleanup_release.set()
        delivery.release.set()
        app._cancel_audio_owner = original_cancel
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['ack', 'failure'])
@pytest.mark.parametrize('boundary', ['hangup', 'resolved', 'replacement', 'new_session'])
async def test_late_delivery_cannot_speak_across_incident_or_session_boundary(harness, outcome, boundary):
    app, send, played = harness
    delivery = DelayedDelivery(outcome)
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text=(
        'позови взрослого' if boundary == 'new_session' else 'не хочу жить')))
    await asyncio.wait_for(delivery.entered.wait(), 2)
    state = app.canonical_runtime.sessions['handoff-audit']
    incident = state.incident_id
    if boundary in {'hangup', 'new_session'}:
        await send('HandsetPlaced')
        await send('HandsetLifted')
    else:
        await app._resolve_adult_incident_internal('handoff-audit', incident)
        if boundary == 'replacement':
            class Pending:
                async def request(self, payload):
                    return {'acknowledged': False}
            app.adult_handoff = Pending()
            await send('SpeechReceived', text='не хочу жить')
            assert app.canonical_runtime.sessions['handoff-audit'].incident_id != incident
    await finish_delivery(app, delivery, request)
    assert handoff_phrases(played) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['ack', 'failure'])
async def test_reset_while_terminal_dispatch_waits_revokes_late_voice(harness, outcome):
    app, send, played = harness
    delivery = DelayedDelivery(outcome)
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    await asyncio.wait_for(delivery.entered.wait(), 2)
    entered, release = asyncio.Event(), asyncio.Event()
    class ResetBarrier:
        def __await__(self):
            entered.set()
            return release.wait().__await__()
        def done(self):
            return release.is_set()
        def set_result(self, _value):
            release.set()
    app._session_reset_waiters['handoff-audit'] = (asyncio.current_task(), ResetBarrier())
    tasks = tuple(app._handoff_delivery_tasks)
    try:
        delivery.release.set()
        await asyncio.wait_for(entered.wait(), 2)
        result = await app.reset_session('handoff-audit')
        assert result['reason'] == 'safety_handoff_active'
        release.set()
        await asyncio.wait_for(request, 2)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert handoff_phrases(played) == []
        assert app.canonical_runtime.sessions['handoff-audit'].safety_severity == 'urgent'
    finally:
        release.set()
        app._session_reset_waiters.pop('handoff-audit', None)
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['ack', 'failure'])
async def test_hangup_before_transport_starts_keeps_delivery_but_revokes_voice(harness, outcome):
    app, send, played = harness
    delivery = DelayedDelivery(outcome)
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    hangup = asyncio.create_task(send('HandsetPlaced'))
    try:
        await asyncio.wait_for(delivery.entered.wait(), 2)
        await asyncio.wait_for(hangup, 2)
        await finish_delivery(app, delivery, request)
        assert len(delivery.requests) == 1
        assert handoff_phrases(played) == []
        state = app.canonical_runtime.sessions['handoff-audit']
        assert state.mode == 'safety_handoff'
        assert state.safety_severity == 'urgent'
        assert state.handset == 'down'
    finally:
        delivery.release.set()
        await asyncio.gather(request, hangup, return_exceptions=True)


@pytest.mark.asyncio
async def test_sync_transport_is_rejected_before_it_can_block_event_loop(harness):
    app, send, played = harness
    called = []
    class SyncDelivery:
        def request(self, payload):
            called.append(payload)
            return {'acknowledged': True}
    app.adult_handoff = SyncDelivery()
    await send('SpeechReceived', text='не хочу жить')
    assert called == []
    assert handoff_phrases(played) == ['PHRASE_091']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_ingress', [False, True])
async def test_handoff_deadline_cancels_transport_and_notifies_once(harness, cancel_ingress):
    app, send, played = harness
    app.HANDOFF_TIMEOUT_SECONDS = .03
    cleaned = asyncio.Event()
    class HangingDelivery(DelayedDelivery):
        async def request(self, payload):
            try:
                return await super().request(payload)
            finally:
                cleaned.set()
    delivery = HangingDelivery('ack')
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    try:
        await asyncio.wait_for(delivery.entered.wait(), 2)
        if cancel_ingress:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await asyncio.wait_for(cleaned.wait(), 2)
        tasks = tuple(app._handoff_delivery_tasks)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert handoff_phrases(played) == ['PHRASE_091']
        assert len(delivery.requests) == 1
        await asyncio.sleep(0)  # Allow completion callbacks to release ownership.
        assert not app._handoff_delivery_tasks
    finally:
        delivery.release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('hangup', [False, True])
async def test_timed_out_transport_late_ack_never_becomes_voice(harness, hangup):
    app, send, played = harness
    app.HANDOFF_TIMEOUT_SECONDS = .03
    cancelled = asyncio.Event()
    class LateDelivery(DelayedDelivery):
        async def request(self, payload):
            self.requests.append(dict(payload))
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await self.release.wait()
            return {'acknowledged': True}
    delivery = LateDelivery('ack')
    app.adult_handoff = delivery
    request = asyncio.create_task(send('SpeechReceived', text='не хочу жить'))
    try:
        await asyncio.wait_for(delivery.entered.wait(), 2)
        if hangup:
            await send('HandsetPlaced')
            await send('HandsetLifted')
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.wait_for(request, 2)
        assert cancelled.is_set()
        assert handoff_phrases(played) == ([] if hangup else ['PHRASE_091'])
        tasks = tuple(app._handoff_delivery_tasks)
        assert tasks  # Cleanup ownership survives the response deadline.
        delivery.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert handoff_phrases(played) == ([] if hangup else ['PHRASE_091'])
        assert not app._handoff_delivery_tasks
    finally:
        delivery.release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_noncooperative_transport_cannot_accumulate_across_incidents(harness):
    app, send, played = harness
    app.HANDOFF_TIMEOUT_SECONDS = .02
    release = asyncio.Event()
    calls = []
    class StuckDelivery:
        async def request(self, payload):
            calls.append(dict(payload))
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return {'acknowledged': True}
    app.adult_handoff = StuckDelivery()
    try:
        for _ in range(4):
            await asyncio.wait_for(send('SpeechReceived', text='не хочу жить'), 2)
            state = app.canonical_runtime.sessions['handoff-audit']
            await app._resolve_adult_incident_internal('handoff-audit', state.incident_id)
        assert len(calls) == 2
        assert len(app._handoff_transport_tasks) == 2
        assert handoff_phrases(played) == ['PHRASE_091'] * 4
    finally:
        release.set()
        await asyncio.gather(*tuple(app._handoff_delivery_tasks), return_exceptions=True)
    assert not app._handoff_transport_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("drain", [True, False])
async def test_cancelled_ingress_waits_for_audio_cleanup_before_terminal_voice(harness, drain):
    app, send, played = harness
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_cancel = app.bounded_s2s.cancel_session
    original_stop = app.bounded_s2s.playback.stop
    stopped_after_terminal = []
    app.audio_cleanup_timeout_seconds = .05 if not drain else .5

    async def stop():
        stopped_after_terminal.extend(handoff_phrases(played))
        await original_stop()
    app.bounded_s2s.playback.stop = stop

    async def cancel(session_id):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Native teardown may need to finish after its caller is cancelled.
            await release.wait()
        await original_cancel(session_id)
        cleaned.set()
    app.bounded_s2s.cancel_session = cancel
    delivery = DelayedDelivery("failure")
    delivery.release.set()
    app.adult_handoff = delivery
    request = asyncio.create_task(send("SpeechReceived", text="не хочу жить"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        tasks = tuple(app._handoff_delivery_tasks)
        await asyncio.sleep(.01)
        assert handoff_phrases(played) == []
        if drain:
            release.set()
            await asyncio.wait_for(cleaned.wait(), 2)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert handoff_phrases(played) == (["PHRASE_091"] if drain else [])
        release.set()
        await asyncio.wait_for(cleaned.wait(), 2)
        assert stopped_after_terminal == []
    finally:
        release.set()
        await asyncio.wait_for(cleaned.wait(), 2)
        app.bounded_s2s.cancel_session = original_cancel
        app.bounded_s2s.playback.stop = original_stop
        await asyncio.gather(request, return_exceptions=True)
