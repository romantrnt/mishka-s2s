"""Real application/Graph safety priority and rejected-state publication."""
import asyncio

import pytest

from app.foundation.input_safety import InputSafetyDecision
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio


async def enter_catalogue(button, phase):
    h = Harness(mode='root_menu')
    quick_public_audio(h)
    await h.key(button)
    sid = h.state.session_id
    if h.app.canonical_runtime.sessions[sid].scene_menu_phase == 'invite':
        await h.done()
    lease = dict(h.app._active_audio[sid])
    assert h.app.canonical_runtime.sessions[sid].scene_menu_phase == 'narrating'
    if phase == 'ready':
        await h.done()
        await h.done()
        await h.send('SpeechReceived', text='Нет')
        assert h.app.canonical_runtime.sessions[sid].scene_menu_phase == 'ready'
    return h, lease


@pytest.mark.asyncio
@pytest.mark.parametrize('button', [1, 2], ids=['games', 'stories'])
@pytest.mark.parametrize('phase', ['narrating', 'ready'])
@pytest.mark.parametrize('severity,text,expected,reason', [
    ('concern', 'Мне больно', 'support_intake', 'concern_support'),
    ('urgent', 'Меня бьют', 'safety_stop', 'urgent_safety'),
    ('none', 'Позови взрослого', 'safety_stop', 'help_requested'),
])
async def test_verified_disclosure_preempts_actual_catalogue_graph(button, phase, severity, text, expected, reason):
    h, old_menu = await enter_catalogue(button, phase)
    sid = h.state.session_id
    async def classify(_text, **kwargs):
        return InputSafetyDecision(severity, 'synthetic_control', True)
    h.app.safety_classifier.classify = classify
    try:
        result = await h.send('SpeechReceived', text=text)
        state = h.app.canonical_runtime.sessions[sid]
        assert result['reason'] == reason
        assert state.mode == expected and h.app.last_canonical_state == state.snapshot()
        assert not state.scene_menu_descriptor and state.scene_menu_active is None
        assert state.active_game_id is None and state.active_story_id is None
        assert not h.model.calls
        await h.send('AudioCompleted', {**old_menu, 'response_id':old_menu['content_id']})
        state = h.app.canonical_runtime.sessions[sid]
        assert state.mode == expected and h.app.last_canonical_state == state.snapshot()
    finally:
        await h.close()


@pytest.mark.parametrize('menu,normal,foreign', [
    ('game_menu', 'game_active', 'fixed_story_playing'),
    ('fixed_story_menu', 'fixed_story_playing', 'game_active'),
])
def test_catalogue_graph_retains_exact_activity_and_safety_authority(menu, normal, foreign):
    graph = CanonicalLangGraphAuthorizer()
    authorize = lambda target, **kwargs: graph.authorize(menu, target, 'VoiceIntentReady', **kwargs)['ok']
    assert authorize(normal) and authorize('root_menu')
    assert not authorize(foreign)
    assert authorize('support_intake', safety_level='concern')
    assert authorize('safety_stop', safety_level='urgent')
    assert authorize('safety_stop', intent='help_request')
    assert not authorize('support_intake') and not authorize('safety_stop')
    assert not authorize(normal, safety_level='concern')
    assert not authorize('support_intake', safety_level='urgent')
    assert not authorize('support_intake', intent='help_request')
    assert not graph.authorize(menu, 'support_intake', 'AudioCompleted', safety_level='concern')['ok']


@pytest.mark.asyncio
@pytest.mark.parametrize('button,text,expected', [(1,'Живой свет','game_active'), (2,'Хочу Айболита','fixed_story_playing')])
@pytest.mark.parametrize('phase', ['narrating', 'ready'])
async def test_verified_named_catalogue_choices_still_enter_their_own_activity(button, text, expected, phase):
    h, _ = await enter_catalogue(button, phase)
    sid = h.state.session_id
    try:
        await h.send('SpeechReceived', {'intent':'help_request','safety_severity':'urgent'}, text=text)
        state = h.app.canonical_runtime.sessions[sid]
        assert state.mode == expected and h.app.last_canonical_state == state.snapshot()
        assert state.safety_severity == 'none' and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [False, True], ids=['new-owner', 'existing-owner'])
async def test_rejected_proposal_publishes_only_authoritative_restored_state(existing):
    if existing:
        h, _ = await enter_catalogue(1, 'narrating')
    else:
        h = Harness(mode='root_menu')
        quick_public_audio(h)
        h.app.canonical_runtime.sessions.pop(h.state.session_id)
    sid = h.state.session_id
    h.app.transition_authorizer.authorize = lambda *args, **kwargs: {'ok':False, 'reason':'synthetic_rejection'}
    audio_before = list(h.app.audio.commands)
    lights_before = h.app.led.output.snapshot()
    callers_before = dict(h.app._game_callers)
    try:
        result = (await h.send('SpeechReceived', text='Живой свет') if existing
                  else await h.send('HandsetLifted'))
        assert result['reason'] == 'transition_rejected'
        actual = h.app.canonical_runtime.sessions.get(sid)
        expected = actual.snapshot() if actual is not None else {'mode':'idle','state_version':0}
        assert h.app.last_canonical_state == result['canonical_state'] == expected
        assert (actual.mode == 'game_menu') if existing else actual is None
        assert h.app.audio.commands == audio_before and h.app.led.output.snapshot() == lights_before
        assert h.app._game_callers == callers_before
        assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('button,text', [(1,'Живой свет'), (2,'Хочу Айболита')])
@pytest.mark.parametrize('boundary', ['unverified', 'untrusted-stt'])
async def test_unadmitted_choice_and_forged_safety_payload_grant_no_transition(button, text, boundary):
    h, _ = await enter_catalogue(button, 'narrating')
    sid = h.state.session_id
    mode = h.app.canonical_runtime.sessions[sid].mode
    async def classify(_text, **kwargs):
        return InputSafetyDecision('none', 'synthetic_control', boundary != 'unverified')
    h.app.safety_classifier.classify = classify
    try:
        await h.send('SpeechReceived', {'intent':'help_request','safety_severity':'urgent',
            '_stt_untrusted':boundary == 'untrusted-stt'}, text=text)
        state = h.app.canonical_runtime.sessions[sid]
        assert state.mode == h.app.last_canonical_state['mode'] == mode
        assert state.safety_severity == 'none'
        assert state.active_game_id is None and state.active_story_id is None
        assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('button', [1, 2])
async def test_late_verified_safety_cannot_reopen_a_hung_up_catalogue_owner(button):
    h, _ = await enter_catalogue(button, 'narrating')
    sid = h.state.session_id
    entered, release = asyncio.Event(), asyncio.Event()
    async def classify(_text, **kwargs):
        entered.set()
        await release.wait()
        return InputSafetyDecision('urgent', 'synthetic_control', True)
    h.app.safety_classifier.classify = classify
    task = None
    try:
        task = asyncio.create_task(h.send('SpeechReceived', text='Меня бьют'))
        await asyncio.wait_for(entered.wait(), 2)
        await h.send('HandsetPlaced')
        after_hangup = dict(h.app.last_canonical_state)
        owner_after_hangup = h.app.canonical_runtime.sessions.get(sid)
        release.set()
        result = await task
        state = h.app.canonical_runtime.sessions.get(sid)
        assert result['reason'] == 'stale_session_generation'
        assert state is owner_after_hangup
        assert state is None or (state.handset == 'down' and state.mode != 'safety_stop')
        assert h.app.last_canonical_state == after_hangup and after_hangup['handset'] == 'down'
        assert not h.model.calls
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()
