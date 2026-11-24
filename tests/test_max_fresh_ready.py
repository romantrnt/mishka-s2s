"""Fresh Max context replacement through the real private application boundary."""
import asyncio
import json
import contextlib
from copy import deepcopy
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.foundation.domain import Event
from app.foundation.personal_story import StoryOwner
from app.foundation.max_json import MaxJsonBoundary
from test_profile_software_chain import PublicPhoneClientDouble, build_chain


class PollOwnedClient(PublicPhoneClientDouble):
    """Like PhoneUbuntuClient: poll(ready) installs the card; enqueue does not."""
    def ready(self, **kwargs):
        previous = self.card
        super().ready(**kwargs)
        self.events[-1]['_fixture_card'] = deepcopy(self.card)
        self.card = previous

    def poll(self):
        assert not getattr(self, 'patient_due', False), 'poll passed an unread ready'
        self.calls.append('poll')
        if not self.events:
            return None
        event = self.events.popleft()
        if event['kind'] == 'patient_ready':
            self.card = event.pop('_fixture_card')
            self.patient_due = True
        elif event['kind'] == 'clear':
            self.card = None
        return event

    def patient(self):
        self.patient_due = False
        return super().patient()

    def clear(self):
        self.patient_due = False
        super().clear()


class Harness:
    def __init__(self):
        self.app, self.clock, self.led, _ = build_chain()
        self.app.audio.private_pcm_supported = False
        self.client = PollOwnedClient()
        self.boundary = MaxJsonBoundary(self.app, self.client, session_id='software',
            now=lambda: datetime(2026,11,10,tzinfo=timezone.utc))
        self.boundary.start(background=False)
        self.sequence = 0

    @property
    def state(self):
        return self.app.canonical_runtime.sessions['software']

    async def send(self, kind, payload=None, text=None):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, 'software', self.sequence,
            1., 'fresh-ready-test', payload or {}, f'fresh:{self.sequence}', self.clock()), text)

    async def ready(self, name='Новоетестовоеимя'):
        self.client.ready(first_name=name)
        return await self.boundary.step()

    async def complete(self):
        lease = self.app._active_audio['software']
        return await self.send('AudioCompleted', {**lease, 'response_id': lease['content_id']})

    async def guest_name(self):
        await self.send('HandsetLifted')
        self.clock.value = self.state.welcome_deadline_monotonic
        await self.send('WelcomeTimerFired')
        for _ in range(3):
            await self.complete()
        assert self.state.mode == 'exhibition_name_capture'
        await self.send('SpeechReceived', text='Тестик')
        assert self.state.display_name == 'Тестик'


@pytest_asyncio.fixture
async def h():
    harness = Harness()
    try:
        yield harness
    finally:
        await harness.boundary.close()
        await harness.app.shutdown_identity()
        await harness.app._cleanup_session_resources('software')
        harness.led.cancel_effects(clear=True)


@pytest_asyncio.fixture
async def replacing(h, request):
    if getattr(request, 'param', True):
        await h.send('HandsetLifted')
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app._cleanup_session_resources
    pending = None
    async def cleanup(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task() is pending:
            entered.set(); await release.wait()
            if getattr(h, 'cleanup_error', False):
                raise RuntimeError('synthetic_cleanup_failure')
        return result
    h.app._cleanup_session_resources = cleanup
    pending = asyncio.create_task(h.ready())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        yield pending, release
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        h.app._cleanup_session_resources = original


@pytest.mark.asyncio
async def test_ready_after_real_lift_enters_authorized_welcome_without_synthetic_lift(h):
    await h.send('HandsetLifted')
    result = await h.ready()
    assert result['reason'] == 'profile_ready'
    assert h.state.handset == 'up' and h.state.mode == 'voice_welcome'
    assert h.app.last_canonical_event['type'] == 'ClientProfileReady'
    assert h.state.profile_status == 'identified'
    assert h.app._identity_scope_live('software', 'personalize')
    assert h.state.identity_ready_deadline is None
    h.clock.value = h.state.welcome_deadline_monotonic
    await h.send('WelcomeTimerFired')
    assert (await h.complete())['reason'] == 'authorized_profile_menu_started'


@pytest.mark.asyncio
async def test_scan_started_revokes_guest_name_and_history_before_new_ready(h):
    await h.guest_name()
    old = h.state
    h.client.events.append({'kind': 'clear', 'reason_code': 'scan_started'})
    result = await h.boundary.step()
    assert result['reason'] == 'profile_cleared'
    assert h.state.display_name is None and not h.state.dialogue_memory.bounded_turns
    assert (await h.ready())['reason'] == 'profile_ready'
    assert h.state.handset == 'up' and h.state.profile_status == 'identified'
    assert h.state.display_name != old.display_name or old.display_name is None
    assert 'Тестик' not in json.dumps(h.app._canonical_audio_context('software').history, ensure_ascii=False)


@pytest.mark.asyncio
async def test_down_ready_is_silent_and_repeated_same_card_is_a_new_context(h):
    assert (await h.ready())['reason'] == 'profile_ready'
    first = h.state.identity_request_id
    expiry = h.state.identity_expires_monotonic
    assert h.state.handset == 'down' and h.state.mode == 'profile_ready'
    assert h.state.identity_ready_deadline == 60.
    assert not h.app._active_audio
    h.state.history_tail.append('PRIVATE_OLD')
    assert (await h.ready())['reason'] == 'profile_ready'
    assert h.state.identity_request_id != first
    assert h.state.identity_expires_monotonic == expiry
    assert not h.state.history_tail and h.state.handset == 'down'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['exhibition_name_capture', 'bear_dialogue', 'game_active',
    'fixed_story_playing', 'story_paused', 'personal_story_generating', 'personal_story_playing'])
async def test_new_ready_retires_all_seeded_old_owners_before_first_cleanup_await(h, phase):
    await h.send('HandsetLifted')
    old = h.state
    old.mode = phase
    old.display_name = 'Староетестовоеимя'
    old.history_tail.append('PRIVATE_HISTORY')
    old.dialogue_memory.bounded_turns.append({'text':'PRIVATE_TURN'})
    old.pending_game_offer = 'quiz'; old.game_offer_id = 'old-offer'; old.game_offer_ready = True
    old.active_game_id = 'quiz'; old.game_return_mode = 'bear_dialogue'
    old.active_story_id = 'PERSONAL_STORY'
    h.app.conversation_contexts['software'] = h.app._canonical_audio_context('software')
    h.app._game_callers['software'] = {'context':h.app.conversation_contexts['software']}
    story = StoryOwner(h.app._session_epochs.get('software',0), 'old-story', (None,None), '3-5', 99., audio=object())
    h.app._personal_stories['software'] = story
    before = h.app._session_epochs.get('software',0)
    original = h.app._cleanup_session_resources
    observed = []
    async def cleanup(*args, **kwargs):
        if not observed:
            observed.append(True)
            assert h.state is not old and h.state.handset == 'up'
            assert h.state.profile_ref is None and h.state.display_name is None
            assert not h.state.dialogue_memory.bounded_turns and not h.state.history_tail
            assert h.app._session_epochs['software'] > before
            assert not h.app._canonical_profiles and not h.app._identity_requests
            assert 'software' not in h.app.conversation_contexts and 'software' not in h.app._game_callers
            assert story.closed and story.audio is None and 'software' not in h.app._personal_stories
        return await original(*args, **kwargs)
    h.app._cleanup_session_resources = cleanup
    try:
        result = await h.ready()
    finally:
        h.app._cleanup_session_resources = original
    assert observed and result['reason'] == 'profile_ready'
    assert h.state.pending_game_offer is None and h.state.game_return_mode is None
    assert h.state.active_story_id is None and h.state.active_game_id is None
    assert not h.state.dialogue_memory.bounded_turns


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['raise', 'timeout', 'sync'])
async def test_cleanup_failure_has_no_grant_and_a_later_scan_can_retry(h, failure):
    await h.send('HandsetLifted')
    original = h.app.s2s_audio.cancel_session
    cancel = h.app.audio.cancel
    h.app.audio_cleanup_timeout_seconds = .01
    async def broken(*args, **kwargs):
        if failure == 'timeout':
            await asyncio.Event().wait()
        raise RuntimeError('synthetic_cleanup_failure')
    if failure == 'sync':
        h.app.audio.cancel = lambda _: (_ for _ in ()).throw(RuntimeError('synthetic_stop_failure'))
    else:
        h.app.s2s_audio.cancel_session = broken
    try:
        assert (await h.ready())['reason'] == 'profile_cleanup_failed'
    finally:
        h.app.s2s_audio.cancel_session = original
        h.app.audio.cancel = cancel
    assert not h.app._canonical_profiles and not h.app._identity_requests
    assert h.state.profile_ref is None and h.client.patient() is not None
    assert await h.boundary.step() is None  # Max-only retention cannot silently reauthorize.
    assert not h.app._identity_requests
    assert h.state.handset == 'up'
    assert (await h.ready())['reason'] == 'profile_ready'
    assert h.state.handset == 'up' and h.app._identity_scope_live('software')


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['SessionCancel', 'SessionReset', 'HardwareDisconnected', 'urgent'])
async def test_new_card_cannot_bypass_explicit_end_or_local_stop(h, terminal):
    await h.send('HandsetLifted')
    if terminal == 'urgent':
        await h.send('VoiceIntentReady', {'intent':'free_speech', 'safety_severity':'urgent'})
    else:
        await h.send(terminal)
    state = h.state
    state.substate = 'profile_cleared'  # A display label is not retry authority.
    assert (await h.ready())['reason'] == 'profile_active_session_ignored'
    assert h.state is state and h.state.profile_ref is None and h.client.patient() is not None
    assert await h.boundary.step() is None
    assert not h.app._canonical_profiles and not h.app._identity_requests
    assert h.state.handset == 'up'


@pytest.mark.asyncio
async def test_guest_without_private_ram_is_not_ended_by_scan_clear(h):
    await h.send('HandsetLifted')
    state = h.state
    h.client.events.append({'kind':'clear','reason_code':'scan_started'})
    assert (await h.boundary.step())['reason'] == 'profile_already_clear'
    assert h.state is state and h.state.mode == 'voice_welcome'


@pytest.mark.asyncio
async def test_held_key_cannot_skip_new_onboarding_or_become_a_new_action(h):
    await h.send('HandsetLifted')
    await h.send('ButtonPressed', {'button_id':7})
    assert (await h.ready())['reason'] == 'profile_ready'
    for kind in ('ButtonPressed','ButtonHeld','ButtonLongHeld'):
        expected = 'button_held' if kind == 'ButtonPressed' else 'readiness_button_held'
        assert (await h.send(kind, {'button_id':7,'duration_ms':2000}))['reason'] == expected
    assert h.state.mode == 'voice_welcome'
    assert (await h.send('ButtonReleased', {'button_id':7}))['reason'] == 'readiness_button_released'
    h.clock.value = h.state.welcome_deadline_monotonic
    await h.send('WelcomeTimerFired'); await h.complete()
    chosen = await h.send('ButtonPressed', {'button_id':7})
    assert h.state.mode == 'schedule_weather'
    assert len([e for e in chosen['outputs'] if e.get('type') == 'exhibition_menu_selected']) == 1
    assert (await h.send('ButtonPressed', {'button_id':7}))['reason'] == 'exhibition_menu_choice_held'


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['HandsetPlaced', 'reset', 'HardwareDisconnected', 'urgent'])
async def test_terminal_during_cleanup_wins_before_grant(h, terminal):
    await h.send('HandsetLifted')
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app._cleanup_session_resources
    pending = None
    async def cleanup(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task() is pending:
            entered.set(); await release.wait()
        return result
    h.app._cleanup_session_resources = cleanup
    pending = asyncio.create_task(h.ready())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert h.state.profile_ref is None and not h.app._identity_requests
        if terminal == 'reset':
            await h.app.reset_session('software')
        elif terminal == 'urgent':
            await h.send('VoiceIntentReady', {'intent':'free_speech','safety_severity':'urgent'})
        else:
            await h.send(terminal)
        owner = h.app.canonical_runtime.sessions.get('software')
        release.set()
        assert (await pending)['reason'] == 'stale_profile_context'
        assert h.app.canonical_runtime.sessions.get('software') is owner
        assert not h.app._canonical_profiles and not h.app._identity_requests
        assert (h.client.patient() is None) == (terminal in {'HandsetPlaced','reset'})
    finally:
        release.set(); await pending
        h.app._cleanup_session_resources = original


@pytest.mark.asyncio
async def test_button_spam_and_release_during_barrier_does_not_abort_ready(h, replacing):
    pending, release = replacing
    for button in (1,7):
        for kind in ('ButtonPressed','ButtonHeld','ButtonLongHeld'):
            assert (await h.send(kind, {'button_id':button,'duration_ms':2000}))['reason'] == 'profile_replacing'
    await h.send('ButtonReleased', {'button_id':1})
    assert h.state.profile_ref is None and h.app._profile_replacing('software')
    assert (await h.app.receive_audio_async('software', (.1,)*512, 100))['reason'] == 'profile_replacing'
    release.set()
    assert (await pending)['reason'] == 'profile_ready'
    assert h.app._identity_scope_live('software') and h.state.handset == 'up'
    assert 7 in h.state.readiness_held_buttons and 1 not in h.state.readiness_held_buttons
    assert (await h.send('ButtonPressed', {'button_id':7}))['reason'] == 'button_held'


@pytest.mark.asyncio
@pytest.mark.parametrize('queued', ['clear_ready','metadata_clear_ready','metadata_ready','same_ready'])
@pytest.mark.parametrize('unwind', ['complete','cancel','error'])
async def test_queued_new_context_survives_old_ready_commit_cancel_and_error(h, replacing, queued, unwind):
    pending, release = replacing
    clears = h.client.calls.count('clear')
    if queued.startswith('metadata'):
        h.client.events.append({'kind':'reader_state','state':'connected'})
    if 'clear' in queued:
        h.client.events.append({'kind':'clear','reason_code':'scan_started'})
    new_name = 'Новоетестовоеимя' if queued == 'same_ready' else 'Другоетестовоеимя'
    h.client.ready(first_name=new_name)
    # A faithful source has not installed the queued card yet.
    assert h.client.card['identity']['first_name'] == 'Новоетестовоеимя'
    if unwind == 'cancel':
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        h.cleanup_error = unwind == 'error'
        release.set()
        result = await pending
        assert result['reason'] == ('client_unavailable' if unwind == 'error' else 'stale_profile_context')
    assert h.state.profile_ref is None and not h.app._identity_requests
    assert h.client.calls.count('clear') == clears
    assert h.boundary._deferred_event is not None or h.client.events
    h.cleanup_error = False
    reasons = []
    while h.boundary._deferred_event is not None or h.client.events:
        reasons.append((await h.boundary.step())['reason'])
    assert reasons[-1] == 'profile_ready'
    if 'clear' in queued:
        assert 'profile_already_clear' in reasons
    assert h.state.display_name == new_name and h.state.handset == 'up'
    assert h.app._identity_scope_live('software')
    # Deferred ready still reads patient before any further poll.
    assert not h.client.patient_due


@pytest.mark.asyncio
async def test_metadata_budget_is_bounded_and_requires_observed_empty_queue(h, replacing):
    pending, release = replacing
    h.client.events.extend({'kind':'reader_state','state':'connected'} for _ in range(10))
    calls = h.client.calls.count('poll')
    release.set()
    assert (await pending)['reason'] == 'stale_profile_context'
    assert h.client.calls.count('poll') - calls == 9
    assert h.boundary._reader_ready and h.boundary._deferred_event['kind'] == 'patient_ready'
    assert h.state.profile_ref is None
    assert (await h.boundary.step())['reason'] == 'profile_ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['close','hangup','reset'])
async def test_intentional_terminal_discards_deferred_source_and_card(h, replacing, terminal):
    pending, release = replacing
    h.client.ready()
    release.set(); assert (await pending)['reason'] == 'stale_profile_context'
    assert h.boundary._deferred_event
    if terminal == 'close':
        await h.boundary.close()
    elif terminal == 'hangup':
        await h.send('HandsetPlaced')
    else:
        await h.app.reset_session('software')
    assert h.boundary._deferred_event is None and h.client.patient() is None
    assert not h.app._canonical_profiles and not h.app._identity_requests


@pytest.mark.asyncio
async def test_forged_stale_and_replayed_profile_events_cannot_replace_live_owner(h):
    await h.ready()
    owner = h.state
    payload = {'request_id':'forged','client_generation':h.boundary.generation - 1,
               'expires_monotonic':999.,'ready_deadline':60.}
    event = h.app._identity_event('software','ClientProfileReady',payload)
    assert (await h.app.receive_async(event))['reason'] == 'max_json_requires_private_client_boundary'
    assert (await h.app._receive_canonical_event(event,identity_event_authorized=True,
        identity_profile=h.app._canonical_profiles['software']))['reason'] == 'stale_client_generation'
    clear = h.app._identity_event('software','ClientProfileCleared',{'request_id':'old','reason_code':'scan_started'})
    assert (await h.app._receive_canonical_event(clear,identity_event_authorized=True))['reason'] == 'stale_profile_context'
    assert h.state is owner and h.app._identity_scope_live('software')


@pytest.mark.asyncio
async def test_reauthorization_while_up_replaces_profile_without_scan_clear(h):
    await h.ready(); await h.send('HandsetLifted')
    old_request = h.state.identity_request_id
    old_context = h.state.profile_ref
    assert (await h.ready(name='Другоетестовоеимя'))['reason'] == 'profile_ready'
    assert h.state.handset == 'up' and h.state.identity_request_id != old_request
    assert h.state.profile_ref != old_context and h.state.display_name == 'Другоетестовоеимя'
    expiry = h.app._identity_event('software','IdentityAuthorizationExpired',{'request_id':old_request})
    assert (await h.app._receive_canonical_event(expiry,identity_event_authorized=True))['reason'] == 'stale_profile_context'
    assert h.app._identity_scope_live('software')


@pytest.mark.asyncio
async def test_actual_late_classifier_from_guest_cannot_enter_new_profile(h):
    from app.foundation.input_safety import InputSafetyDecision
    entered, release = asyncio.Event(), asyncio.Event()
    class Classifier:
        async def classify(self, text):
            entered.set(); await release.wait()
            return InputSafetyDecision('urgent','synthetic')
    await h.send('HandsetLifted')
    h.app.safety_classifier = Classifier()
    task = asyncio.create_task(h.send('SpeechReceived', text='Старыйтестовыйтекст'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert (await h.ready())['reason'] == 'profile_ready'
        snapshot = deepcopy(h.state.snapshot())
        release.set()
        assert (await task)['reason'] == 'stale_session_generation'
        assert h.state.snapshot() == snapshot and h.app._identity_scope_live('software')
    finally:
        release.set(); await task


@pytest.mark.asyncio
async def test_resistant_stt_blocks_grant_until_drain_and_cannot_commit_old_text(h):
    from app.foundation.s2s import STTResult
    entered, release = asyncio.Event(), asyncio.Event()
    class STT:
        async def transcribe(self, audio, cancelled):
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: pass
            return STTResult('Старыйтестовыйтекст', .99, 0., False)
    await h.send('HandsetLifted')
    h.app.s2s_audio.recognizer = STT()
    h.app.audio_cleanup_timeout_seconds = .01
    samples = (.1,)*(512*12) + (0.,)*(512*8)
    assert (await h.app.receive_audio_async('software',samples,100,wait_for_turn=False))['pending']
    await asyncio.wait_for(entered.wait(),1)
    pending = tuple(h.app.s2s_audio.pending_tasks.values())
    try:
        assert (await h.ready())['reason'] == 'profile_cleanup_failed'
        assert h.state.profile_ref is None and h.client.patient() is not None
        assert await h.boundary.step() is None
        snapshot = deepcopy(h.state.snapshot())
        release.set(); await asyncio.gather(*pending,return_exceptions=True)
        assert h.state.snapshot() == snapshot
        assert not h.state.dialogue_memory.bounded_turns
        assert (await h.ready())['reason'] == 'profile_ready'
    finally:
        release.set(); await asyncio.gather(*pending,return_exceptions=True)


@pytest.mark.asyncio
async def test_old_welcome_timer_and_pcm_callback_cannot_own_new_ready(h):
    await h.send('HandsetLifted')
    deadline, epoch = h.state.welcome_deadline_monotonic, h.app._session_epochs.get('software',0)
    lease = dict(h.app._active_audio['software'])
    assert (await h.ready())['reason'] == 'profile_ready'
    snapshot = deepcopy(h.state.snapshot())
    # Even equal fake-clock deadlines do not make the old timer current.
    assert h.state.welcome_deadline_monotonic == deadline
    h.clock.value = deadline
    assert await h.app._fire_exhibition_welcome('software',deadline,epoch=epoch) is None
    old_timer = Event('WelcomeTimerFired','software',1,1.,'canonical_welcome_timer',
                      {'deadline_monotonic':deadline,'generation':epoch},'old-timer',h.clock())
    assert (await h.app.receive_async(old_timer))['reason'] == 'stale_session_generation'
    assert (await h.send('AudioCompleted',{**lease,'response_id':lease['content_id']}))['reason'] == 'stale_exhibition_audio_completion'
    assert h.state.snapshot() == snapshot


@pytest.mark.asyncio
async def test_later_trusted_request_is_not_cleared_by_old_ready_unwind(h, replacing):
    from app.foundation.identity import normalize_profile
    pending, release = replacing
    profile = normalize_profile({'profile_ref':'max-context:later','display_name':'Позднееимя'})
    payload = {'request_id':'later-owner','client_generation':h.boundary.generation,
               'expires_monotonic':3600.,'ready_deadline':60.}
    event = h.app._identity_event('software','ClientProfileReady',payload)
    result = await h.app._receive_canonical_event(event,identity_event_authorized=True,identity_profile=profile)
    assert result['reason'] == 'profile_ready'
    owner = h.state
    clears = h.client.calls.count('clear')
    release.set()
    assert (await pending)['reason'] == 'stale_profile_context'
    assert h.state is owner and h.state.identity_request_id == 'later-owner'
    assert h.app._identity_scope_live('software') and h.client.calls.count('clear') == clears


@pytest.mark.asyncio
async def test_final_runtime_admission_change_during_cleanup_does_not_grant(h, replacing):
    pending, release = replacing
    h.boundary.admits_users = lambda: False
    release.set()
    assert (await pending)['reason'] == 'stale_profile_context'
    assert h.state.profile_ref is None and not h.app._identity_requests


@pytest.mark.asyncio
@pytest.mark.parametrize('unwind', ['cancel','error'])
async def test_new_scan_after_observed_empty_source_is_not_destroyed_by_old_unwind(h, replacing, unwind):
    pending, release = replacing
    poll = h.client.poll
    injected = False
    def poll_then_capture():
        nonlocal injected
        event = poll()
        if event is None and not injected:
            injected = True
            h.client.ready(first_name='Следующеетестовоеимя')
        return event
    h.client.poll = poll_then_capture
    polls = h.client.calls.count('poll')
    clears = h.client.calls.count('clear')
    if unwind == 'cancel':
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await pending
    else:
        h.cleanup_error = True; release.set()
        assert (await pending)['reason'] == 'client_unavailable'
    assert h.client.calls.count('poll') == polls and h.client.calls.count('clear') == clears
    h.client.poll = poll
    if not injected:
        h.client.ready(first_name='Следующеетестовоеимя')
    assert h.client.events
    assert h.state.profile_ref is None
    h.cleanup_error = False
    assert (await h.boundary.step())['reason'] == 'profile_ready'
    assert h.state.display_name == 'Следующеетестовоеимя'


@pytest_asyncio.fixture
async def postgrant(h):
    from test_private_welcome import Render
    from app.foundation.s2s import CachedTTSProvider
    render = Render()
    render.stage, render.block, render.resist = 'tts', asyncio.Event(), True
    h.app.bounded_s2s.tts = CachedTTSProvider(render)
    h.app.bounded_s2s.voice = render
    h.app.audio.private_pcm_supported = True
    h.pcm = []
    h.app.audio.play_pcm_owned = lambda *args: h.pcm.append(args)
    await h.send('HandsetLifted')
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    async def hook(_session_id, update):
        if update.get('reason') == 'profile_ready' and asyncio.current_task() is pending:
            entered.set()
            await release.wait()
    h.app.state_update_hook = hook
    pending = asyncio.create_task(h.ready())
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(render.entered.wait(), 2)
    owner = h.app._private_welcomes['software']
    try:
        yield pending, release, render, owner
    finally:
        render.block.set(); release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        h.app.state_update_hook = None


@pytest.mark.asyncio
@pytest.mark.parametrize('queued', ['clear_ready', 'metadata_ready', 'same_ready'])
async def test_postgrant_source_change_fences_private_uses_before_publication_returns(h, postgrant, queued):
    pending, release, render, owner = postgrant
    request = h.state.identity_request_id
    h.state.history_tail.append('PRIVATE_OLD_HISTORY')
    h.state.dialogue_memory.bounded_turns.append({'role':'user','content':'PRIVATE_OLD_TURN'})
    if queued == 'clear_ready':
        h.client.events.append({'kind':'clear','reason_code':'scan_started'})
    elif queued == 'metadata_ready':
        h.client.events.append({'kind':'reader_state','state':'connected'})
    name = 'Новоетестовоеимя' if queued == 'same_ready' else 'Следующеетестовоеимя'
    h.client.ready(first_name=name)
    clears = h.client.calls.count('clear')
    assert not h.app._identity_scope_live('software')
    polls = h.client.calls.count('poll')
    for _ in range(3):
        assert not h.app._identity_scope_live('software')
    assert h.client.calls.count('poll') == polls  # Never poll past deferred ready.
    assert (await h.send('ButtonPressed', {'button_id':7}))['reason'] == 'stale_profile_context'
    assert (await h.send('VoiceIntentReady', {'intent':'free_speech'}))['reason'] == 'stale_profile_context'
    assert (await h.app.receive_audio_async('software', (.1,)*1024, 200,
        wait_for_turn=False))['reason'] == 'stale_profile_context'
    context = h.app._bounded_dialogue_context('software')
    assert not context.history and context.profile.display_name is None
    texts = len(render.texts)
    assert await h.app._render_typed_audio('software', 'PRIVATE_OLD_FACT',
        {'content_id':'TYPED_CARD_DOCTORS'}, 0, lambda:True) is None
    assert len(render.texts) == texts
    render.block.set()
    await asyncio.wait_for(owner.task, 2)
    assert owner.audio is None and not h.pcm
    public = json.dumps([h.app.resource_snapshot(), h.state.snapshot(),
        list(h.app.canonical_trace), list(h.app.audio_runtime_diagnostics)], ensure_ascii=False)
    assert 'PRIVATE_OLD_' not in public and 'Новоетестовоеимя' not in public
    release.set()
    assert (await pending)['reason'] == 'stale_session_generation'
    assert h.client.calls.count('clear') == clears
    reasons = []
    while h.boundary._deferred_event is not None or h.client.events:
        reasons.append((await h.boundary.step())['reason'])
    assert reasons.count('profile_ready') == 1
    assert h.state.identity_request_id != request and h.state.display_name == name
    assert h.app._identity_scope_live('software') and not h.state.history_tail


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['HandsetPlaced', 'urgent'])
async def test_postgrant_revocation_keeps_cleanup_and_urgent_available(h, postgrant, terminal):
    pending, release, render, owner = postgrant
    h.client.events.append({'kind':'clear','reason_code':'scan_started'})
    assert not h.app._identity_scope_live('software')
    render.block.set()
    if terminal == 'urgent':
        result = await h.send('VoiceIntentReady', {'intent':'free_speech','safety_severity':'urgent'})
        assert h.state.mode == 'safety_stop'
    else:
        result = await h.send('HandsetPlaced')
        assert result['canonical_state']['handset'] == 'down'
        assert not h.app._identity_requests
    assert result['reason'] != 'stale_profile_context'
    release.set(); await pending
    assert not h.app._identity_scope_live('software') and not h.pcm


@pytest.mark.asyncio
async def test_source_poll_error_revokes_receipt_monotonically_until_a_new_ready(h):
    await h.ready()
    request = h.state.identity_request_id
    poll = h.client.poll
    calls = 0
    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('synthetic_poll_failure')
        return poll()
    h.client.poll = fail_once
    assert not h.app._identity_scope_live('software')
    assert not h.app._identity_scope_live('software') and calls == 1
    assert await h.boundary.step() is None
    assert not h.app._identity_scope_live('software')
    assert h.state.identity_request_id == request and h.boundary.facts is None
    assert (await h.ready())['reason'] == 'profile_ready'
    assert h.state.identity_request_id != request and h.app._identity_scope_live('software')


@pytest.mark.asyncio
async def test_committed_metadata_budget_never_replays_an_old_ready(h):
    await h.ready()
    for _ in range(9):
        h.client.events.append({'kind':'reader_state','state':'connected'})
    assert not h.app._identity_scope_live('software')
    assert h.boundary._deferred_event is None and not h.boundary.facts
    assert await h.boundary.step() is None
    assert not h.app._identity_scope_live('software')


@pytest.mark.asyncio
async def test_queued_ready_survives_real_guest_lift_without_a_synthetic_edge(h):
    h.client.ready()
    clears = h.client.calls.count('clear')
    assert (await h.send('HandsetLifted'))['reason'] == 'exhibition_handset_lifted'
    assert h.client.calls.count('clear') == clears and h.client.events
    assert (await h.boundary.step())['reason'] == 'profile_ready'
    assert h.state.handset == 'up' and h.state.mode == 'voice_welcome'
    assert h.app.last_canonical_event['type'] == 'ClientProfileReady'


@pytest.mark.asyncio
async def test_revoked_down_profile_lift_retires_only_old_app_owner(h):
    await h.ready()
    old_request = h.state.identity_request_id
    h.client.ready(first_name='Следующеетестовоеимя')
    clears = h.client.calls.count('clear')
    assert (await h.send('HandsetLifted'))['reason'] == 'exhibition_handset_lifted'
    assert h.state.handset == 'up' and h.state.profile_ref is None
    assert h.client.calls.count('clear') == clears and h.boundary._deferred_event
    assert (await h.boundary.step())['reason'] == 'profile_ready'
    assert h.state.identity_request_id != old_request and h.state.display_name == 'Следующеетестовоеимя'


@pytest.mark.asyncio
@pytest.mark.parametrize('unwind', ['cancel', 'source_error'])
async def test_postgrant_revoked_ready_unwind_scrubs_only_captured_app_ram(h, postgrant, unwind):
    pending, release, render, owner = postgrant
    old = h.state
    h.client.ready(first_name='Следующеетестовоеимя')
    poll = h.client.poll
    if unwind == 'source_error':
        h.client.poll = lambda: (_ for _ in ()).throw(RuntimeError('synthetic_poll_failure'))
    assert not h.app._identity_scope_live('software')
    h.client.poll = poll
    deferred = h.boundary._deferred_event
    generation, clears = h.boundary.generation, h.client.calls.count('clear')
    render.block.set()
    if unwind == 'cancel':
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        release.set()
        assert (await pending)['reason'] == 'stale_session_generation'
    assert old.profile_ref is None and old.display_name is None
    assert not h.app._canonical_profiles and not h.app._identity_requests
    assert h.boundary._deferred_event is deferred and h.boundary.generation == generation
    assert h.client.calls.count('clear') == clears
    assert (await h.boundary.step())['reason'] == 'profile_ready'
    assert h.state.display_name == 'Следующеетестовоеимя' and h.app._identity_scope_live('software')


@pytest.mark.asyncio
@pytest.mark.parametrize('replacing', [False], indirect=True)
async def test_real_lift_during_down_ready_cleanup_preserves_pending_verified_ready(h, replacing):
    pending, release = replacing
    epoch, revision = h.app._session_epochs['software'], h.app._session_revisions['software']
    version = h.state.state_version
    assert h.state.handset == 'down' and h.state.mode == 'idle'
    lifted = await h.send('HandsetLifted')
    assert lifted['reason'] == 'profile_replacing'
    assert lifted['canonical_state']['handset'] == 'up' and h.state.mode == 'idle'
    assert h.state.profile_ref is None and not h.app._active_audio and not h.app._private_welcomes
    assert h.state.last_sequence_by_source['fresh-ready-test'] == h.sequence
    assert h.app.last_canonical_state['handset'] == 'up'
    assert h.app._session_epochs['software'] == epoch and h.app._session_revisions['software'] == revision
    assert h.state.state_version == version
    replay = Event('HandsetLifted','software',h.sequence,1.,'fresh-ready-test',{},
        f'fresh:{h.sequence}',h.clock())
    assert (await h.app.receive_async(replay))['reason'] == 'idempotent_duplicate'
    assert (await h.send('HandsetLifted'))['reason'] == 'profile_replacing'
    assert not h.app._active_audio and h.app._profile_replacing('software')
    release.set()
    assert (await pending)['reason'] == 'profile_ready'
    assert h.state.mode == 'voice_welcome' and h.state.handset == 'up'
    assert h.state.profile_status == 'identified' and h.app._identity_scope_live('software')
    assert h.state.identity_ready_deadline is None
    assert h.app.last_canonical_event['type'] == 'ClientProfileReady'
    assert not h.client.events and h.boundary._deferred_event is None


@pytest.mark.asyncio
@pytest.mark.parametrize('replacing', [False], indirect=True)
@pytest.mark.parametrize('supersede', ['HandsetPlaced','reset','urgent','HardwareDisconnected','new_ready'])
async def test_real_lift_during_ready_cleanup_does_not_weaken_superseding_owners(h, replacing, supersede):
    pending, release = replacing
    await h.send('HandsetLifted')
    assert h.state.handset == 'up' and not h.app._active_audio
    if supersede == 'new_ready':
        h.client.ready(first_name='Следующеетестовоеимя')
    elif supersede == 'reset':
        await h.app.reset_session('software')
    elif supersede == 'urgent':
        await h.send('VoiceIntentReady', {'intent':'free_speech','safety_severity':'urgent'})
    else:
        await h.send(supersede)
    owner = h.app.canonical_runtime.sessions.get('software')
    release.set()
    assert (await pending)['reason'] == 'stale_profile_context'
    assert h.app.canonical_runtime.sessions.get('software') is owner
    assert not h.app._identity_requests and not h.app._canonical_profiles
    if supersede == 'new_ready':
        assert (await h.boundary.step())['reason'] == 'profile_ready'
        assert h.state.handset == 'up' and h.state.profile_status == 'identified'
        assert h.state.display_name == 'Следующеетестовоеимя'
    elif supersede in {'urgent','HardwareDisconnected'}:
        assert h.state.mode == 'safety_stop'
    else:
        assert owner is None or owner.handset == 'down'


@pytest.mark.asyncio
async def test_first_real_lift_during_cleanup_is_up_in_actual_stand_and_remote_projection():
    from app.foundation.config import Settings
    from app.foundation.remote_devices import RemoteDeviceSimulator
    from app.foundation.simulator import SimulatorAcceptanceHarness
    harness = SimulatorAcceptanceHarness(Settings(profile='mock', hardware_mode='simulator',
        runtime_mode='exhibition', identity_mode='max_json'))
    stand, client = harness.stand, PollOwnedClient()
    stand._admits_physical_users = lambda:True
    app, sid = stand.application, stand.active_session_id
    boundary = MaxJsonBoundary(app, client, session_id=sid,
        now=lambda:datetime(2026,11,10,tzinfo=timezone.utc))
    boundary.start(background=False)
    remote = RemoteDeviceSimulator(stand, admits_users=lambda:True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = app._cleanup_session_resources
    pending = None
    async def cleanup(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task() is pending:
            entered.set(); await release.wait()
        return result
    app._cleanup_session_resources = cleanup
    client.ready()
    try:
        pending = asyncio.create_task(boundary.step())
        await asyncio.wait_for(entered.wait(), 2)
        assert app.sessions.get(sid) is None
        epoch, revision = app._session_epochs[sid], app._session_revisions[sid]
        assert (await harness.space_released())['reason'] == 'profile_replacing'
        assert app.canonical_runtime.sessions[sid].handset == 'up'
        snapshot = stand.snapshot()
        projected = remote.projection(snapshot)
        assert snapshot['session']['handset_up'] is True
        assert projected['devices']['handset'] == {'state':'up','space_level':'released'}
        record = app.sessions.get(sid)
        assert record.anonymous and record.child_id is None
        assert record.state.value == 'WAITING_PROFILE' and record.current_node == 'idle'
        assert not app._active_audio and not app._private_welcomes and not app._identity_requests
        assert app._session_epochs[sid] == epoch and app._session_revisions[sid] == revision
        assert (await harness.space_released())['reason'] == 'profile_replacing'
        assert app.sessions.get(sid) is record
        assert stand.snapshot()['session']['handset_up'] is True
        release.set()
        assert (await pending)['reason'] == 'profile_ready'
        assert app.canonical_runtime.sessions[sid].mode == 'voice_welcome'
        assert app._identity_scope_live(sid)
        assert remote.projection(stand.snapshot())['devices']['handset']['state'] == 'up'
    finally:
        release.set()
        if pending is not None:
            await pending
        app._cleanup_session_resources = original
        await stand.aclose()
