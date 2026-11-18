"""Private Max expiry transfers only verified public playback, never its owner."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.max_json import MaxJsonBoundary
from app.foundation.stand import create_stand_app


class Client:
    def __init__(self):
        self.events = []
        self.card = {
            'schema_version': 1, 'record': {'state': 'active'},
            'rfid': {'state': 'active', 'valid_until': '2099-01-01T00:00:00Z'},
            'visit': {'status': 'open'}, 'identity': {'first_name': 'PRIVATE_NAME'},
            'birth': {'age_years': 6, 'birth_data_state': 'complete'},
            'acquaintance': {'kind': 'first'},
            'adaptation': {'wheelchair_access': False, 'participation_mode': 'seated'},
        }
    def start(self): return True
    def poll(self): return self.events.pop(0) if self.events else None
    def patient(self): return self.card
    def clear(self): self.card = None
    def close(self): self.clear(); return True


class Harness:
    async def start(self, phase='fixed_story_playing', confirmed=True):
        self.clock = [100.0]
        self.client = Client()
        self.api = create_stand_app(Settings(profile='mock', hardware_mode='simulator',
            identity_mode='max_json', runtime_mode='exhibition'), phone_client=self.client)
        self.stand = self.api.state.stand
        self.app = self.stand.application
        self.app.background_scheduler = False
        self.app.monotonic_now = self.app.canonical_runtime.monotonic_now = lambda: self.clock[0]
        self.app.now = lambda: self.clock[0]
        self.boundary = self.app._max_client_boundary
        self.boundary.admits_users = None
        self.boundary.now = lambda: datetime(2026, 11, 18, tzinfo=timezone.utc)
        self.boundary.start(background=False)
        self.client.events.append({'kind': 'patient_ready'})
        assert (await self.boundary.step())["reason"] == "profile_ready"
        self.sid = self.boundary.session_id
        self.sequence = 0
        await self.send('HandsetLifted')
        self.state = self.app.canonical_runtime.sessions[self.sid]
        self.story = 'fixed_aibolit_v1'
        self.state.mode, self.state.handset = phase, 'up'
        self.state.active_story_id = self.story
        self.state.active_audio_id, self.state.playback_lease = self.story, 'public-old-lease'
        self.state.expected_inputs = ['button_6', 'button_7', 'handset_down']
        self.app._play_owned_audio(self.sid, self.story, 'synthetic.wav', 7,
            {'playback_lease': self.state.playback_lease}, duration_ms=100000)
        self.old = dict(self.app._active_audio[self.sid])
        if phase != 'fixed_story_playing':
            self.app._paused_story_playback[self.sid] = dict(self.old)
            if confirmed:
                self.state.mode = 'story_pause_announcement'
                await self.send('StoryPositionUpdated', {'story_id': self.story, 'offset_ms': 23000}, source='audio_pause')
                self.app._paused_story_playback.pop(self.sid, None)
                self.state.mode = phase
            if phase == 'story_paused': self.app._invalidate_audio_lease(self.sid)
            else:
                phrase = 'PHRASE_062' if phase == 'story_pause_announcement' else 'PHRASE_064'
                self.state.active_audio_id, self.state.playback_lease = phrase, 'private-old-phrase'
                self.app._register_audio_lease(self.sid, phrase, 7, 'old-phrase-id', 'private-old-phrase')
        self.state.history_tail.append('PRIVATE_HISTORY')
        self.state.dialogue_memory.bounded_turns.append({'text': 'PRIVATE_TURN'})
        self.app._engagement_owners[self.sid] = {
            'scope': self.app._engagement_scope(self.sid), 'nonce': 'original-deadline',
            'deadline': 220., 'busy': phase != 'story_paused',
            'busy_resets_wait': True, 'completed_work': phase != 'story_paused',
        }
        return self
    async def send(self, kind, payload=None, *, source='public-expiry-test'):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, self.sid, self.sequence, self.clock[0], source,
            payload or {}, f'expiry-test:{self.sequence}', self.clock[0]))
    async def expire(self, reason='session_expired'):
        self.client.clear()
        self.client.events.append({'kind': 'clear', 'reason_code': reason})
        return await self.boundary.step()
    async def endpoint(self, path, payload):
        endpoint = next(r.endpoint for r in self.api.routes if getattr(r, 'path', None) == path)
        return await endpoint(payload)
    async def close(self):
        await self.stand.aclose()
    def checkpoint(self, **extra):
        return {'session_id': self.sid, 'story_id': self.story,
            **{k: self.old[k] for k in ('token', 'generation', 'playback_id', 'playback_lease')},
            'offset_ms': 34250, **extra}
    def assert_private_clear(self):
        assert self.state.profile_ref is None and self.state.identity_request_id is None
        assert self.state.display_name is None and not self.state.history_tail
        assert not self.state.dialogue_memory.bounded_turns
        assert self.sid not in self.app._canonical_profiles
        assert self.sid not in self.app._identity_requests
        record = self.app.sessions.get(self.sid)
        assert record is None or (record.child_id is None and record.anonymous)
        assert self.boundary.facts is None and self.client.patient() is None


@pytest.mark.asyncio
async def test_confirmed_paused_public_story_keeps_checkpoint_and_absolute_idle_deadline():
    h = await Harness().start('story_paused')
    try:
        epoch = h.app._session_epochs.get(h.sid, 0)
        await h.expire()
        h.assert_private_clear()
        assert h.app._session_epochs[h.sid] == epoch + 1
        assert h.state.mode == 'story_paused' and h.state.active_story_id == h.story
        assert h.state.dialogue_memory.story_checkpoint == {'story_id': h.story, 'offset_ms': 23000}
        assert h.app._engagement_owners[h.sid]['deadline'] == 220.
        h.clock[0] = 219.999
        assert await h.app.expire_engagement_sessions() == []
        h.clock[0] = 220.
        assert (await h.app.expire_engagement_sessions())[0]['reason'] == 'interaction_idle'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_playing_transfer_accepts_only_exact_old_checkpoint_and_resumes_new_owner():
    h = await Harness().start()
    try:
        epoch = h.app._session_epochs.get(h.sid, 0)
        assert (await h.expire())['reason'] == 'public_story_identity_expired'
        h.assert_private_clear()
        assert h.state.public_story_checkpoint_pending
        assert h.state.expected_inputs == ['button_7', 'handset_down']
        assert h.sid not in h.app._active_audio
        transfer = h.app._public_story_transfer(h.sid)
        assert transfer['epoch'] == epoch + 1
        await h.stand._flush_device_commands()
        commands = h.stand.panel_snapshot()['commands']
        assert h.stand.panel_snapshot()['canonical_state']['public_story_checkpoint_pending']
        request = next(c['payload']['checkpoint_request'] for c in commands
            if c['type'] == 'audio_stop' and 'checkpoint_request' in c['payload'])
        assert request['playback_id'] == h.old['playback_id']
        assert request['target_generation'] == epoch + 1
        for extra in ({'token': -1}, {'playback_id': 'foreign'}, {'generation': epoch+1},
                      {'target_generation': epoch}, {'offset_ms': -1},
                      {'offset_ms': transfer['duration_ms']+1}):
            result = await h.endpoint('/stand/audio/checkpoint', h.checkpoint(**{'target_generation': epoch+1, **extra}))
            assert result['reason'] == 'stale_story_checkpoint'
            assert h.app._public_story_transfer(h.sid) is transfer
        old = {**h.old, 'response_id': h.story, 'session_id': h.sid}
        assert (await h.endpoint('/stand/audio/completed', old))['reason'] == 'stale_exhibition_audio_completion'
        assert (await h.endpoint('/stand/audio/lifecycle', {**old, 'status': 'playing'}))['reason'] == 'stale_audio_lifecycle'
        good = h.checkpoint(target_generation=epoch+1)
        result = await h.endpoint('/stand/audio/checkpoint', good)
        assert result['reason'] == 'public_story_checkpoint_resumed'
        new = h.app._active_audio[h.sid]
        assert new['generation'] == epoch+1 and new['playback_lease'] != h.old['playback_lease']
        assert new['playback_id'] != h.old['playback_id']
        assert h.state.mode == 'fixed_story_playing'
        await h.stand._flush_device_commands()
        plays = [c for c in h.stand.commands if c['type'] == 'audio_play']
        assert plays[-1]['payload']['offset_ms'] == 34250
        assert (await h.endpoint('/stand/audio/checkpoint', good))['reason'] == 'stale_story_checkpoint'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['story_pause_announcement', 'story_resume_announcement'])
async def test_expiry_during_pause_resume_recording_uses_confirmed_checkpoint(phase):
    h = await Harness().start(phase)
    try:
        phrase = dict(h.app._active_audio[h.sid])
        await h.expire()
        h.assert_private_clear()
        assert not h.state.public_story_checkpoint_pending
        assert h.state.dialogue_memory.story_checkpoint['offset_ms'] == 23000
        assert h.state.mode == ('fixed_story_playing' if phase == 'story_resume_announcement' else 'story_paused')
        before = deepcopy(h.state.snapshot())
        await h.send('AudioCompleted', {**phrase, 'response_id': phrase['content_id']})
        assert h.state.snapshot() == before
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['scan_started', 'api_error', 'no_personalization'])
async def test_other_max_clear_reasons_stop_public_story(reason):
    h = await Harness().start('story_paused')
    try:
        await h.expire(reason)
        h.assert_private_clear()
        assert h.state.mode == 'session_closed' and not h.state.active_story_id
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['fault', 'card', 'reset', 'hangup'])
async def test_transferred_anonymous_owner_still_revoked_by_terminal_boundary(terminal):
    h = await Harness().start()
    try:
        await h.expire()
        epoch = h.app._session_epochs[h.sid]
        if terminal in {'fault', 'card'}:
            await h.expire('api_error' if terminal == 'fault' else 'scan_started')
        elif terminal == 'reset': await h.app.reset_session(h.sid)
        else: await h.send('HandsetPlaced')
        assert h.sid not in h.app._public_story_checkpoint_transfers
        assert h.sid not in h.app._active_audio
        assert (await h.endpoint('/stand/audio/checkpoint', h.checkpoint(target_generation=epoch)))['reason'] == 'stale_story_checkpoint'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_no_ack_expires_transfer_and_retains_exit_without_zero_resume():
    h = await Harness().start()
    try:
        await h.expire()
        epoch = h.app._session_epochs[h.sid]
        h.clock[0] += 5.
        assert (await h.endpoint('/stand/audio/checkpoint', h.checkpoint(target_generation=epoch)))['reason'] == 'stale_story_checkpoint'
        assert h.sid not in h.app._public_story_checkpoint_transfers
        assert (await h.send('ButtonPressed', {'button_id': 6}))['reason'] == 'public_story_checkpoint_pending'
        assert h.sid not in h.app._active_audio
        assert (await h.send('ButtonPressed', {'button_id': 7}))['reason'] == 'story_stopped_by_button'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['client', 'application'])
async def test_client_and_application_expiry_orders_are_idempotent(first):
    h = await Harness().start('story_paused')
    try:
        old_request = h.state.identity_request_id
        if first == 'application':
            h.state.identity_expires_monotonic = h.clock[0]
            assert (await h.app.expire_identity_leases())[0]['reason'] == 'public_story_identity_expired'
        else:
            await h.expire()
        epoch = h.app._session_epochs[h.sid]
        snapshot = deepcopy(h.state.snapshot())
        await h.expire()
        result = await h.app._receive_canonical_event(h.app._identity_event(
            h.sid, 'IdentityAuthorizationExpired', {'request_id': old_request}), identity_event_authorized=True)
        assert result['reason'] == 'stale_profile_context'
        assert h.state.snapshot() == snapshot and h.app._session_epochs[h.sid] == epoch
        h.assert_private_clear()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_already_expired_paused_deadline_fires_in_same_expiry_turn():
    h = await Harness().start('story_paused')
    try:
        h.clock[0] = 220.
        assert (await h.expire())['reason'] == 'interaction_idle'
        assert h.state.interaction_idle and h.sid not in h.app._engagement_owners
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['PERSONAL_STORY', 'placeholder:fixed_story:1', 'PERSONAL_WELCOME'])
async def test_unverified_private_or_placeholder_story_never_transfers(content):
    h = await Harness().start('story_paused')
    try:
        h.state.active_story_id = content
        await h.expire()
        h.assert_private_clear()
        assert h.state.mode == 'session_closed'
        assert h.sid not in h.app._public_story_checkpoint_transfers
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_late_classifier_cannot_commit_into_new_anonymous_story():
    from app.foundation.input_safety import InputSafetyDecision
    h = await Harness().start('story_paused')
    entered, release = asyncio.Event(), asyncio.Event()
    class Classifier:
        async def classify(self, text):
            entered.set()
            await release.wait()
            return InputSafetyDecision('urgent', 'test')
    task = None
    try:
        h.app.safety_classifier = Classifier()
        task = asyncio.create_task(h.app.receive_async(Event('SpeechReceived', h.sid, 100,
            h.clock[0], 'old-private-speech', {}, 'old-private-speech', h.clock[0]), 'PRIVATE_TEXT'))
        await asyncio.wait_for(entered.wait(), 1)
        await h.expire()
        snapshot = deepcopy(h.state.snapshot())
        release.set()
        assert (await task)['reason'] == 'stale_session_generation'
        assert h.state.snapshot() == snapshot and h.state.safety_severity != 'urgent'
        h.assert_private_clear()
    finally:
        release.set()
        if task: await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_late_stt_cannot_commit_into_new_anonymous_story():
    from app.foundation.s2s import STTResult
    h = await Harness().start('story_paused')
    entered, release = asyncio.Event(), asyncio.Event()
    class STT:
        async def transcribe(self, audio, cancelled):
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: pass
            return STTResult('PRIVATE_OLD_STT', .99, 0., False)
    try:
        h.app.s2s_audio.recognizer = STT()
        samples = (.1,) * (512 * 12) + (0.,) * (512 * 8)
        assert (await h.app.receive_audio_async(h.sid, samples, 100, wait_for_turn=False))['pending']
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(h.expire(), 4)
        snapshot = deepcopy(h.state.snapshot())
        release.set()
        for _ in range(10): await asyncio.sleep(0)
        assert h.state.snapshot() == snapshot
        assert not h.state.dialogue_memory.bounded_turns
    finally:
        release.set()
        await h.close()


@pytest.mark.asyncio
async def test_resistant_private_render_cannot_survive_public_owner_transfer():
    from test_private_welcome import setup, start
    h, render = setup()
    render.stage, render.block, render.resist = 'tts', asyncio.Event(), True
    try:
        await start(h)
        await asyncio.wait_for(render.entered.wait(), 1)
        private = h.app._private_welcomes[h.boundary.session_id]
        h.state.mode, h.state.active_story_id = 'story_paused', 'fixed_aibolit_v1'
        h.state.dialogue_memory.story_checkpoint = {'story_id': 'fixed_aibolit_v1', 'offset_ms': 23000}
        h.state.story_checkpoint_confirmed = True
        h.app._invalidate_audio_lease(h.boundary.session_id)
        h.client.value = None
        h.client.events.append({'kind': 'clear', 'reason_code': 'session_expired'})
        assert (await h.boundary.step())['reason'] == 'public_story_identity_expired'
        assert private.closed
        render.block.set()
        await asyncio.gather(private.task, return_exceptions=True)
        assert not h.pcm and not h.app._private_welcomes
        assert h.state.display_name is None and not h.app._canonical_profiles
        assert h.state.dialogue_memory.story_checkpoint['offset_ms'] == 23000
    finally:
        render.block.set()
        await h.close()


@pytest.mark.asyncio
async def test_stale_expiry_request_cannot_clear_current_profile_or_story():
    h = await Harness().start('story_paused')
    try:
        before = deepcopy(h.state.snapshot())
        result = await h.app._receive_canonical_event(h.app._identity_event(h.sid,
            'ClientProfileCleared', {'reason_code': 'session_expired', 'request_id': 'old-request'}),
            identity_event_authorized=True)
        assert result['reason'] == 'stale_profile_context'
        assert h.state.snapshot() == before and h.state.profile_ref
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_first_pcm_cleanup_stop_and_uart_stop_both_request_exact_checkpoint():
    h = await Harness().start()
    class Observer:
        def __init__(self): self.messages = []
        async def send_json(self, message): self.messages.append(message)
        async def close(self, **_): pass
    observer = Observer()
    try:
        await h.stand.hub.connect(observer)
        await h.expire()
        await h.stand._flush_device_commands()
        pending = tuple(h.stand._notification_tasks)
        if pending: await asyncio.gather(*pending)
        stops = [m for m in observer.messages if m['type'] == 'audio_stop']
        assert stops and 'command_sequence' not in stops[0]
        assert any('command_sequence' in m for m in stops)
        for message in stops:
            request = message['checkpoint_request']
            assert request['playback_id'] == h.old['playback_id']
            assert request['generation'] == h.old['generation']
            assert request['target_generation'] == h.app._session_epochs[h.sid]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_checkpoint_ack_waits_for_private_cleanup_and_is_one_shot_under_race():
    h = await Harness().start()
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()
    original_cleanup = h.app._cleanup_session_resources
    async def held_cleanup(*args, **kwargs):
        cleanup_entered.set()
        await release_cleanup.wait()
        return await original_cleanup(*args, **kwargs)
    expiry, ack1, ack2 = None, None, None
    try:
        h.app._cleanup_session_resources = held_cleanup
        expiry = asyncio.create_task(h.expire())
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        h.assert_private_clear()
        epoch = h.app._session_epochs[h.sid]
        good = h.checkpoint(target_generation=epoch)
        ack1 = asyncio.create_task(h.endpoint('/stand/audio/checkpoint', good))
        ack2 = asyncio.create_task(h.endpoint('/stand/audio/checkpoint', good))
        await asyncio.sleep(0)
        assert not ack1.done() and not ack2.done() and h.sid not in h.app._active_audio
        release_cleanup.set()
        await expiry
        results = await asyncio.gather(ack1, ack2)
        assert sorted(r['reason'] for r in results) == ['public_story_checkpoint_resumed', 'stale_story_checkpoint']
        assert h.app._active_audio[h.sid]['generation'] == epoch
    finally:
        release_cleanup.set()
        for task in (expiry, ack1, ack2):
            if task: await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_transferred_playing_story_completion_starts_normal_120_second_wait():
    h = await Harness().start()
    try:
        h.clock[0] = 300.  # The old deadline elapsed while real playback was busy.
        assert (await h.expire())['reason'] == 'public_story_identity_expired'
        assert not h.state.interaction_idle
        assert h.app._engagement_owners[h.sid]['deadline'] == 220.
        epoch = h.app._session_epochs[h.sid]
        assert (await h.endpoint('/stand/audio/checkpoint', h.checkpoint(target_generation=epoch)))['reason'] == 'public_story_checkpoint_resumed'
        h.clock[0] = 1000.  # Story can be longer than the old waiting window.
        assert await h.app.expire_engagement_sessions() == []
        current = dict(h.app._active_audio[h.sid])
        await h.send('AudioCompleted', {**current, 'response_id': h.story})
        assert h.state.mode == 'fixed_story_choice_wave'
        # PHRASE_066 still owns real output after the story ends. Waiting
        # starts at its actual completion, as on the ordinary playback path.
        assert h.app._active_audio[h.sid]['content_id'] == 'PHRASE_066'
        h.clock[0] = 1005.
        prompt = dict(h.app._active_audio[h.sid])
        await h.send('AudioCompleted', {**prompt, 'response_id': prompt['content_id']})
        assert h.app._engagement_owners[h.sid]['deadline'] == 1125.
        await h.send('StoryChoiceWaveComplete')
        assert h.state.mode == 'fixed_story_menu'
        h.clock[0] = 1124.999
        assert await h.app.expire_engagement_sessions() == []
        h.clock[0] = 1125.
        assert (await h.app.expire_engagement_sessions())[0]['reason'] == 'interaction_idle'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_current_story_lifecycle_survives_real_release_ack_without_accepting_old_lease():
    h = await Harness().start()
    try:
        # Follow actual root → story menu → skip narration → select story.
        from test_scene_menu_runtime import Render
        class Writer:
            async def send_json(self, _): pass
            async def close(self, **_): pass
        await h.stand.hub.connect(Writer())
        render = Render()
        h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = render
        h.state.mode, h.state.active_story_id = 'root_menu', None
        h.state.scene_menu_active, h.state.scene_menu_phase = None, 'idle'
        h.state.expected_inputs = ['button_' + str(i) for i in range(1, 8)] + ['handset_down']
        for button in (2, 1):
            await h.send('ButtonPressed', {'button_id': button})
            await h.send('ButtonReleased', {'button_id': button})
        assert (await h.send('ButtonPressed', {'button_id': 1}))['reason'] == 'fixed_story_started'
        owner = dict(h.app._active_audio[h.sid])
        callback = {**owner, 'session_id': h.sid, 'response_id': h.story, 'status': 'playing'}
        assert (await h.endpoint('/stand/audio/lifecycle', callback)).get('audio_status') == 'playing'
        await h.send('ButtonReleased', {'button_id': 1})
        assert h.app._active_audio[h.sid]['playback_id'] == owner['playback_id']
        assert [e['type'] for e in h.app.last_canonical_effects] == ['button_edge_ack']
        assert (await h.endpoint('/stand/audio/lifecycle', callback)).get('audio_status') == 'playing'
        assert h.app._active_audio[h.sid]['status'] == 'playing'
        assert h.state.audio_status == 'playing'
        for change in ({'generation': owner['generation'] + 1}, {'playback_id': 'old'},
                       {'playback_lease': 'old'}, {'response_id': 'fixed_other'}, {'session_id': 'other'}):
            assert (await h.endpoint('/stand/audio/lifecycle', {**callback, **change}))['reason'] == 'stale_audio_lifecycle'
        await h.expire()
        assert (await h.endpoint('/stand/audio/lifecycle', callback))['reason'] == 'stale_audio_lifecycle'
    finally:
        await h.close()
