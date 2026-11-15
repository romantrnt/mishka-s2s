import asyncio
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import TurnResult

class Harness:
    def __init__(self, mode='clinical'):
        self.clock = [100.0]
        self.app, *_ = build_application(settings=Settings(profile='mock', runtime_mode=mode),
                                         monotonic_now=lambda: self.clock[0], background_scheduler=False)
        self.seq = 0
        self.app.now = lambda: self.clock[0]
        self.seen = []
        harness = self
        class Engine:
            async def run_text_turn(self, text, context):
                harness.seen.append(list(context.history))
                context.history.append((text, 'ответ'))
                context.turns += 1
                return TurnResult(('ответ',), (), False)
        self.app.bounded_s2s = Engine()
    @property
    def state(self): return self.app.canonical_runtime.sessions['memory']
    async def send(self, kind, payload=None, text=None):
        self.seq += 1
        return await self.app.receive_async(Event(kind, 'memory', self.seq, self.clock[0], 'test', payload or {}, f'm:{self.seq}', self.clock[0]), transient_text=text)
    async def start(self):
        await self.send('HandsetLifted')
        self.state.mode = 'bear_dialogue'
        self.state.welcome_status = 'complete'
        await self.say('первый вопрос')
    async def say(self, text): return await self.send('VoiceIntentReady', {'intent':'free_speech'}, text)
    async def close(self): await self.app.reset_session('memory')

@pytest.mark.asyncio
async def test_clinical_resume_after_four_minutes_keeps_committed_turns_and_active_budget():
    h = Harness()
    try:
        await h.start()
        first = h.app.conversation_contexts['memory']
        await h.send('HandsetPlaced')
        h.clock[0] += 240
        await h.send('HandsetLifted')
        result = await h.send('ButtonPressed', {'button_id':6})
        assert result['reason'] == 'bear_dialogue_started'
        await h.say('второй вопрос')
        context = h.app.conversation_contexts['memory']
        assert h.seen[-1] == [('первый вопрос','ответ')]
        assert context.turns == 2 and context.started_at == first.started_at + 240
        assert not hasattr(context, '_ram_expires_at')
        assert 'первый вопрос' not in repr(h.state.snapshot())
    finally: await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,offset,retained', [('clinical',299.999,True),('clinical',300,False),('exhibition',0,False)])
async def test_runtime_policy_and_exact_monotonic_expiry(mode, offset, retained):
    h = Harness(mode)
    try:
        await h.start(); await h.send('HandsetPlaced')
        h.clock[0] += offset
        h.app.expire_dialogue_memory()
        assert ('memory' in h.app.conversation_contexts) is retained
    finally: await h.close()

@pytest.mark.asyncio
async def test_late_provider_cannot_mutate_suspended_committed_history():
    h = Harness(); entered = asyncio.Event(); release = asyncio.Event()
    try:
        await h.start()
        class Late:
            async def run_text_turn(self, text, context):
                entered.set(); await release.wait()
                context.history.append((text,'не должен сохраниться'))
                return TurnResult(('поздний',), (), False)
        h.app.bounded_s2s = Late()
        pending = asyncio.create_task(h.say('опоздавший вопрос'))
        await entered.wait(); await h.send('HandsetPlaced')
        release.set()
        assert (await pending)['reason'] == 'stale_session_generation'
        assert h.app.conversation_contexts['memory'].history == [('первый вопрос','ответ')]
    finally: release.set(); await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['SessionReset','AnonymousStart','ClientProfileCleared','IdentityAuthorizationExpired','changed_profile'])
async def test_retained_private_history_cannot_cross_reset_revoke_expiry_or_profile(boundary):
    h = Harness()
    try:
        await h.start()
        h.app.optional_identity = True
        h.app.max_json = True
        h.app.canonical_runtime.optional_identity = True
        h.state.profile_ref = 'synthetic-A'
        h.state.identity_request_id = 'synthetic-request'
        h.state.identity_expires_monotonic = 1000
        h.state.identity_capabilities = ['personalize']
        h.app._identity_requests['memory'] = {'request_id':'synthetic-request'}
        context = h.app._bounded_dialogue_context('memory')
        context.history = [('личный вопрос','ответ')]
        await h.send('HandsetPlaced')
        assert 'memory' in h.app.conversation_contexts
        if boundary == 'changed_profile':
            h.state.profile_ref = 'synthetic-B'
            assert h.app._bounded_dialogue_context('memory').history == []
        elif boundary in {'ClientProfileCleared','IdentityAuthorizationExpired'}:
            if boundary == 'IdentityAuthorizationExpired': h.clock[0] = 1000
            h.seq += 1
            await h.app._receive_canonical_event(Event(boundary,'memory',h.seq,h.clock[0],'identity',{'request_id':'synthetic-request'},f'auth:{h.seq}',h.clock[0]), identity_event_authorized=True)
            assert 'memory' not in h.app.conversation_contexts
        else:
            await h.send(boundary)
            assert 'memory' not in h.app.conversation_contexts
    finally: await h.close()

@pytest.mark.asyncio
async def test_button_activity_extends_inactivity_but_callback_does_not():
    h = Harness()
    try:
        await h.start(); await h.send('HandsetPlaced')
        h.clock[0] += 299
        await h.send('ButtonPressed', {'button_id':1})
        assert h.app.conversation_contexts['memory']._ram_expires_at == 699
        h.clock[0] += 1
        await h.send('ButtonReleased', {'button_id':1})
        assert h.app.conversation_contexts['memory']._ram_expires_at == 699
        assert h.state.dialogue_memory.expires_at == 699
        h.clock[0] = 699
        h.app.expire_dialogue_memory()
        assert 'memory' not in h.app.conversation_contexts
    finally: await h.close()

@pytest.mark.asyncio
async def test_expired_authority_is_unreadable_before_async_cleanup_runs():
    h=Harness()
    try:
        await h.start()
        h.app.optional_identity=True
        h.state.profile_ref='synthetic-A'; h.state.identity_request_id='r'
        h.state.identity_expires_monotonic=101; h.state.identity_capabilities=['personalize']
        h.app._identity_requests['memory']={'request_id':'r'}
        h.app._bounded_dialogue_context('memory').history=[('PRIVATE','answer')]
        h.clock[0]=101
        assert h.app._bounded_dialogue_context('memory').history==[]
    finally: await h.close()
