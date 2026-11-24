"""Real app entry/terminal owners; providers are mock, no physical I/O."""
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.canonical_services import LED_PRESETS
from app.foundation.lighting import FakeLightingOutput
from app.foundation.stand import UARTLEDController


class Entry:
    def __init__(self, mode='clinical'):
        self.clock, self.sequence = 10., 0
        self.app, *_ = build_application(settings=Settings(profile='mock', runtime_mode=mode),
            monotonic_now=lambda:self.clock, background_scheduler=False)
        self.sid = 'synthetic-clinical-entry'
        self.lighting = FakeLightingOutput()
        self.app.led = UARTLEDController(None, output=self.lighting,
            monotonic_now=lambda:self.clock, background_scheduler=False)

    @property
    def state(self): return self.app.canonical_runtime.sessions[self.sid]

    async def send(self, kind, payload=None):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, self.sid, self.sequence, 100., 'entry-test',
            payload or {}, f'entry:{self.sequence}', self.clock))

    async def control(self, action):
        return await self.app.session_action(self.sid, action, ownership_guard=lambda:True)

    async def welcome(self, entry):
        await self.send('HandsetLifted')
        if entry == 'panel_up':
            if self.state.mode != 'session_closed':
                await self.control('end')
            await self.control('start')
        assert self.state.mode == 'voice_welcome'
        self.clock += 6
        await self.app.flush_welcome_timers()
        assert self.app._active_audio[self.sid]['content_id'] == 'PHRASE_001'
        return dict(self.app._active_audio[self.sid])

    async def close(self):
        self.app.led.cancel_effects(clear=True)
        await self.app._cleanup_session_resources(self.sid)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['physical_up', 'panel_up'])
@pytest.mark.parametrize('terminal', ['AudioCompleted', 'AudioFailed'])
async def test_clinical_current_welcome_terminal_commits_usable_menu(entry, terminal):
    h = Entry()
    try:
        previous_owner = None
        for cycle in range(2):
            owner = await h.welcome(entry)
            version = h.state.state_version
            if previous_owner is not None:
                stale = await h.send(terminal, {**previous_owner, 'response_id':previous_owner['content_id']})
                assert stale['reason'] == 'stale_audio_callback'
                assert h.state.state_version == version and h.app._active_audio[h.sid] == owner
            for wrong in ({'response_id':'PHRASE_002'}, {'playback_lease':'old-lease'}):
                stale = await h.send(terminal, {**owner, 'response_id':owner['content_id'], **wrong})
                assert stale['reason'] == 'stale_audio_callback'
                assert h.state.mode == 'voice_welcome' and h.state.state_version == version
                assert h.app._active_audio[h.sid] == owner
            result = await h.send(terminal, {**owner, 'response_id':owner['content_id'],
                                            'error_code':'synthetic_audio_failure'})
            assert h.state.mode == 'root_menu', result
            assert h.state.active_audio_id is None and h.sid not in h.app._active_audio
            assert h.state.welcome_status == 'completed'
            assert h.state.audio_status == ('failed' if terminal == 'AudioFailed' else 'idle')
            assert {f'button_{n}' for n in range(1,8)} <= set(h.state.expected_inputs)
            assert any(effect.get('preset') == 'ROOT_MENU' for effect in result['outputs'])
            assert h.lighting.snapshot() == {str(n):dict(LED_PRESETS['ROOT_MENU']).get(n,0) for n in range(18)}
            assert (await h.send(terminal, {**owner, 'response_id':owner['content_id']}))['reason'] == 'stale_audio_callback'
            previous_owner = owner
            # The resulting menu routes a real button; no manual state seeding.
            await h.send('ButtonPressed', {'button_id':6})
            await h.send('ButtonReleased', {'button_id':6})
            assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_INTRO'
            await h.control('end')
            assert h.state.mode == 'session_closed' and not h.state.interaction_open
            if entry == 'physical_up':
                await h.send('HandsetPlaced')
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_exhibition_welcome_still_enters_guest_interview():
    h = Entry('exhibition')
    try:
        owner = await h.welcome('physical_up')
        result = await h.send('AudioCompleted', {**owner, 'response_id':owner['content_id']})
        assert result['reason'] == 'exhibition_introduction_started'
        assert h.state.mode == 'exhibition_introduction' and h.state.active_audio_id == 'PHRASE_078'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_clinical_panel_down_start_already_exposes_silent_root_menu():
    h = Entry()
    try:
        result = await h.control('start')
        assert result['reason'] == 'panel_silent_session_started'
        assert h.state.mode == 'root_menu' and h.state.handset == 'down' and not h.state.audio_open
    finally:
        await h.close()
