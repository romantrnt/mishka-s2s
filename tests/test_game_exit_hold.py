"""A quiz exit is a latched gesture completed only by an uncancelled release."""
from dataclasses import replace

import pytest

from app.foundation.game_engine import advance, current_challenge, finish_prompt, game_answer_buttons, next_deadline, start, timer_step

FILL_ORDER = (14, 13, 10, 9, 6, 3, 1)


def quiz(*, seven=True):
    state = start('quiz', sequence=0, now=100., selection_button=0, run_id='exit-test', age_band='5-7')
    if seven:
        index = next(i for i, question in enumerate(state.deck) if 7 in dict(question.options))
        state = replace(state, challenge_index=index)
    else:
        # Current authored quizzes all have seven choices. This valid limited
        # synthetic challenge isolates hold policy when seven cannot score.
        question = next(question for question in state.deck if question.answer != 7)
        question = replace(question, id='synthetic-six-option-quiz',
            options=tuple(option for option in question.options if option[0] != 7))
        assert question.answer in dict(question.options) and len(question.options) == 6
        state = replace(state, deck=(question,), challenge_index=0)
    return finish_prompt(state, 100.).state


def edge(state, kind, at, button=7, **kwargs):
    return advance(state, edge=kind, button=button, sequence=state.last_sequence + 1, now=at, **kwargs)


def test_short_seven_remains_a_real_answer_only_on_its_current_question():
    state = quiz()
    result = edge(edge(state, 'ButtonPressed', 100.).state, 'ButtonReleased', 100.3)
    assert result.status in {'retry', 'progress'}
    assert result.state.challenge_attempts or result.state.round == 1


@pytest.mark.parametrize('duration',[.6,2.])
def test_recognized_early_release_aborts_without_answer(duration):
    state = quiz()
    result = edge(edge(state, 'ButtonPressed', 100.).state, 'ButtonReleased', 100. + duration)
    assert result.status == 'exit_aborted'
    assert result.state.round == 0 and result.state.challenge_attempts == 0


def test_progress_uses_real_clock_and_ready_stays_latched_until_release():
    state = edge(quiz(), 'ButtonPressed', 100.).state
    assert next_deadline(state) == 100.6
    state = timer_step(state, 100.6).state
    assert state.seven_exit_phase == 'progress' and state.seven_exit_step == 1
    state = timer_step(state, 105.).state
    assert state.seven_exit_phase == 'ready' and state.seven_exit_step == 7
    assert next_deadline(state) is None
    assert timer_step(state, 150.).state == state
    held = edge(state, 'ButtonLongHeld', 150.)
    assert held.status != 'exit_requested' and 7 in held.state.held
    result = edge(held.state, 'ButtonReleased', 151.)
    assert result.status == 'exit_requested' and 7 not in result.state.held


@pytest.mark.parametrize('started',[.1,10.01,100.3,1000000.25])
def test_progress_at_fractional_clock_boundaries_never_reschedules_the_same_tick(started):
    state = edge(quiz(), 'ButtonPressed', 100. + started).state
    began = state.seven_exit_down_at
    deadlines = []
    for step in range(1,8):
        due = next_deadline(state)
        assert not deadlines or due > deadlines[-1]
        deadlines.append(due)
        state = timer_step(state,due).state
        assert state.seven_exit_step == step
    assert state.seven_exit_phase == 'ready' and deadlines[-1] == began + 5.


@pytest.mark.parametrize('duration', [.3, 2., 6.])
def test_cancelled_release_never_answers_or_exits(duration):
    state = edge(quiz(), 'ButtonPressed', 100.).state
    result = edge(state, 'ButtonReleased', 100. + duration, cancelled=True)
    assert result.status == 'exit_aborted'
    assert result.state.round == 0 and result.state.challenge_attempts == 0
    assert result.state.seven_exit_phase == 'idle' and 7 not in result.state.held


def test_whole_quiz_has_the_same_hold_policy_even_when_seven_is_not_an_answer():
    state = quiz(seven=False)
    assert 7 not in game_answer_buttons(state)
    short = edge(edge(state, 'ButtonPressed', 100.).state, 'ButtonReleased', 100.3)
    assert short.status == 'exit_aborted'
    assert short.state.round == 0 and short.state.challenge_attempts == 0
    result = edge(short.state, 'ButtonPressed', 101.)
    assert result.status == 'exit_pending'
    assert edge(result.state, 'ButtonReleased', 106.).status == 'exit_requested'


def test_repeated_press_and_other_key_noise_cannot_score_or_exit():
    state = edge(quiz(), 'ButtonPressed', 100.).state
    repeated = edge(state, 'ButtonPressed', 101.)
    assert repeated.state.seven_exit_down_at == 100.
    noisy = edge(repeated.state, 'ButtonPressed', 102., button=1)
    assert noisy.status == 'exit_aborted'
    assert noisy.state.round == 0 and noisy.state.challenge_attempts == 0
    assert edge(noisy.state, 'ButtonReleased', 106.).status != 'exit_requested'


@pytest.mark.parametrize('band', ['1-3', '3-5', '5-7'])
def test_current_repeat_pool_keeps_age_choices_and_short_seven_separate_from_exit(band):
    state = start('repeat_pattern', sequence=0, now=100., selection_button=0, age_band=band, immersive=True)
    assert game_answer_buttons(state) == ((1,2,3,4) if band == '1-3' else tuple(range(1,8)))
    # A deterministic playable pattern makes the answer meaning explicit;
    # demonstration timing has independent engine/application coverage.
    state = replace(state, phase='playing', pattern=(1,2) if band == '1-3' else (7,1))
    short = edge(edge(state, 'ButtonPressed', 100.).state, 'ButtonReleased', 100.3)
    assert short.status == ('exit_aborted' if band == '1-3' else 'sequence_partial')
    expected_index = 0 if band == '1-3' else 1
    assert short.state.answer_index == expected_index and short.state.round == 0
    duplicate = edge(short.state, 'ButtonReleased', 100.4)
    assert duplicate.status == 'released' and duplicate.state.answer_index == expected_index
    held = edge(state, 'ButtonPressed', 101.).state
    ready = timer_step(held, 106.).state
    assert ready.seven_exit_phase == 'ready' and ready.answer_index == 0
    assert edge(ready, 'ButtonReleased', 107.).status == 'exit_requested'


def test_release_without_down_or_after_cancellation_cannot_exit():
    state = quiz()
    assert edge(state, 'ButtonReleased', 200.).status == 'released'
    inherited = replace(state,held=(7,))
    assert edge(inherited, 'ButtonPressed', 101.).status == 'held'
    assert edge(inherited, 'ButtonReleased', 200.).status == 'released'
    state = edge(state, 'ButtonPressed', 100.).state
    state = edge(state, 'ButtonReleased', 101., cancelled=True).state
    assert edge(state, 'ButtonReleased', 200.).status == 'released'


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['AudioCompleted','AudioFailed'])
async def test_actual_app_timer_fills_real_channels_and_latches_owned_ready_once(terminal):
    from test_contextual_game_flow import Harness
    h = Harness(band='6-7')
    try:
        await h.offer('quiz'); await h.done(); await h.key(1); await h.done()
        sid = h.state.session_id
        original_prompt = dict(h.app._active_audio[sid])
        await h.done()
        before = h.state.game_engine_state
        started = h.clock
        assert (await h.send('ButtonPressed', {'button_id':7}))['reason'] == 'game_exit_pending'
        idle_deadline = h.app._engagement_owners[sid]['deadline']
        scopes = []
        for step in range(1,8):
            scope = h.app._game_timer_scopes[sid]
            scopes.append(scope)
            h.clock = scope[-1]
            result = (await h.app.flush_game_timers())[0]
            assert result['reason'] == ('game_exit_ready' if step == 7 else 'game_exit_progress')
            if step == 1:
                assert h.state.game_audio_stage == 'idle' and h.app._game_voice_scope(sid) is None
            lit = {int(element) for element,value in h.app.led.output.snapshot().items() if value}
            assert lit == set(FILL_ORDER[:step])
            assert h.state.game_engine_state.seven_exit_step == step
        assert h.clock == started + 5.
        assert sid not in h.app._game_timer_scopes
        assert h.state.active_audio_id == 'PHRASE_036'
        lease = dict(h.app._active_audio[sid])
        frame = dict(h.app.led.output.snapshot())
        for late in ['AudioCompleted','AudioFailed']:
            await h.send(late,{**original_prompt,'response_id':original_prompt['content_id']})
            h.app._game_audio_started(sid,original_prompt['playback_id'],original_prompt['playback_lease'],original_prompt['generation'])
            assert h.app._active_audio[sid] == lease and h.app.led.output.snapshot() == frame
        h.clock += 40
        assert await h.app.flush_game_timers() == []
        await h.send('ButtonLongHeld', {'button_id':7,'duration_ms':45000})
        assert h.state.mode == 'game_active' and h.app._active_audio[sid] == lease
        await h.send(terminal,{**lease,'response_id':lease['content_id'],'error_code':'browser_phrase_playback_failed'})
        assert h.app._engagement_owners[sid]['deadline'] == idle_deadline, 'progress and owned ready speech are not user activity'
        assert h.state.game_engine_state.seven_exit_phase == 'ready'
        assert h.state.game_audio_stage == 'idle' and h.app._game_voice_scope(sid) is None
        assert sid not in h.app._active_audio and not h.app._game_prompt_cues
        assert {int(k) for k,v in h.app.led.output.snapshot().items() if v} == set(FILL_ORDER)
        assert (await h.send('ButtonReleased', {'button_id':7}))['reason'] == 'game_exited_by_long_button'
        assert h.state.mode == 'bear_dialogue' and h.state.game_engine_state is None
        assert h.state.game_progress.get('quiz',0) == before.level
        for scope in scopes:
            assert await h.app._fire_game_timer(sid,scope) is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('hangup', [False,True])
async def test_clinical_onhook_hold_and_hangup_disarm_use_current_question_projection(hangup):
    from app.foundation.api import build_application
    from app.foundation.config import Settings
    from app.foundation.domain import Event
    clock = [100.]
    app,*_ = build_application(settings=Settings(profile='mock',runtime_mode='clinical'),monotonic_now=lambda:clock[0],background_scheduler=False)
    sequence = 0
    async def send(kind,button=None,payload=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind,'clinical-hold',sequence,1.,'test',
            payload if payload is not None else {} if button is None else {'button_id':button},str(sequence),clock[0]))
    async def complete():
        owner=dict(app._active_audio['clinical-hold'])
        return await send('AudioCompleted',payload={**owner,'response_id':owner['content_id']})
    try:
        for kind,button in [('ButtonPressed',1),('ButtonReleased',1),('ButtonPressed',2),('ButtonReleased',2)]:
            await send(kind,button)
        state = app.canonical_runtime.sessions['clinical-hold']
        assert state.mode == 'awaiting_handset' and state.pending_game_id == 'quiz'
        assert state.game_engine_state is None
        assert (await send('HandsetLifted'))['reason'] == 'game_started'
        assert app._active_audio['clinical-hold']['content_id'] == 'PHRASE_020'
        await complete()
        assert app._active_audio['clinical-hold']['content_id'] == 'GAME_PROMPT'
        await complete()
        assert state.active_game_id == 'quiz' and state.game_audio_stage == 'playing'
        assert current_challenge(state.game_engine_state) is not None
        assert state.game_engine_state.phase == 'playing' and state.game_return_mode is None
        if not hangup:
            await send('HandsetPlaced')
            assert state.mode == 'game_active' and state.handset == 'down'
        current = dict(app.led.output.snapshot())
        await send('ButtonPressed',7)
        scope = app._game_timer_scopes['clinical-hold']
        clock[0] += 6.
        if hangup:
            await app.flush_game_timers()
            assert state.active_audio_id == 'PHRASE_036'
            await send('HandsetPlaced')
            assert state.game_engine_state.seven_exit_phase == 'idle'
            assert app.led.output.snapshot() == current
            assert (await send('ButtonReleased',7))['reason'] != 'game_exited_by_long_button'
            assert state.mode == 'game_active'
        else:
            assert (await send('ButtonReleased',7))['reason'] == 'game_exited_by_long_button'
            assert state.mode == 'root_menu'
        assert await app._fire_game_timer('clinical-hold',scope) is None
    finally:
        await app._cleanup_session_resources('clinical-hold')


@pytest.mark.asyncio
async def test_direct_resource_cleanup_disarms_a_live_gesture_before_any_await():
    from test_contextual_game_flow import Harness
    h = Harness(band='6-7')
    try:
        await h.launch('quiz')
        sid = h.state.session_id
        await h.send('ButtonPressed', {'button_id':7})
        scope = h.app._game_timer_scopes[sid]
        await h.app._cleanup_session_resources(sid)
        assert h.state.game_engine_state.seven_exit_phase == 'idle'
        h.clock += 6.
        assert await h.app._fire_game_timer(sid,scope) is None
        assert (await h.send('ButtonReleased', {'button_id':7}))['reason'] != 'game_exited_by_long_button'
        assert h.state.mode == 'game_active'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_actual_release_after_ready_threshold_does_not_need_a_timer_tick(cancelled):
    from test_contextual_game_flow import Harness
    h = Harness(band='6-7')
    try:
        await h.launch('quiz')
        await h.send('ButtonPressed', {'button_id':7})
        scope = h.app._game_timer_scopes[h.state.session_id]
        h.clock += 6.
        result = await h.send('ButtonReleased', {'button_id':7,'cancelled':cancelled})
        assert result['reason'] == ('game_exit_aborted' if cancelled else 'game_exited_by_long_button')
        assert h.state.mode == ('game_active' if cancelled else 'bear_dialogue')
        assert await h.app._fire_game_timer(h.state.session_id,scope) is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ready',[False,True])
async def test_abort_restores_current_question_and_rejects_old_audio_and_timer_callbacks(ready):
    from test_contextual_game_flow import Harness
    h = Harness(band='6-7')
    try:
        await h.offer('quiz'); await h.done(); await h.key(1); await h.done()
        sid = h.state.session_id
        old = dict(h.app._active_audio[sid])
        question = current_challenge(h.state.game_engine_state)
        await h.send('ButtonPressed', {'button_id':7})
        scope = h.app._game_timer_scopes[sid]
        h.clock += 6. if ready else 2.
        if ready:
            await h.app.flush_game_timers()
            old_ready = dict(h.app._active_audio[sid])
        result = await h.send('ButtonReleased', {'button_id':7,'cancelled':ready})
        assert result['reason'] == 'game_exit_aborted'
        prompt = next(effect for effect in result['outputs'] if effect['type']=='game_prompt')
        assert prompt['prompt'] == 'Продолжаем игру. ' + question.prompt
        assert h.state.game_engine_state.challenge_attempts == 0
        new = dict(h.app._active_audio[sid])
        frame = dict(h.app.led.output.snapshot())
        await h.send('AudioCompleted', {**old,'response_id':old['content_id']})
        if ready:
            await h.send('AudioCompleted', {**old_ready,'response_id':old_ready['content_id']})
        assert h.app._active_audio[sid] == new
        assert h.app.led.output.snapshot() == frame
        assert await h.app._fire_game_timer(sid,scope) is None
        await h.done()
        assert h.state.game_engine_state.phase == 'playing'
        assert h.state.game_engine_state.seven_exit_phase == 'idle'
        assert h.app._game_timer_scopes[sid][-1] > h.clock
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_pending_hold_fences_late_actual_question_pcm_and_playback_cues():
    import asyncio
    from app.foundation.s2s import CachedTTSProvider
    from test_immersive_game_runtime import GameRuntime
    h = GameRuntime('6-7')
    await h.key(1)
    for _ in range(8):
        if h.state.scene_menu_phase == 'ready':
            break
        if h.app._active_audio.get(h.state.session_id):
            await h.complete()
        elif h.state.scene_menu_phase == 'repeat_listen':
            await h.send('SpeechReceived',text='нет')
    await h.key(2)
    assert h.state.active_audio_id == 'PHRASE_020'
    entered,release = asyncio.Event(),asyncio.Event()
    tts = h.app.bounded_s2s.tts
    while isinstance(tts,CachedTTSProvider):
        tts = tts.delegate
    original = tts.synthesize
    async def synthesize(text,token,cancelled):
        if h.state.mode == 'game_active':
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        return await original(text,token,cancelled)
    tts.synthesize = synthesize
    task = asyncio.create_task(h.complete())
    try:
        await asyncio.wait_for(entered.wait(),3)
        sid = h.state.session_id
        old = dict(h.app._active_audio[sid])
        await h.send('ButtonPressed',{'button_id':7})
        h.clock[0] += 1.
        await h.app.flush_game_timers()
        frame = dict(h.app.led.output.snapshot())
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        h.app._game_audio_started(sid,old['playback_id'],old['playback_lease'],old['generation'])
        assert not any(handle.response_id=='GAME_PROMPT' for handle,_ in h.pcm)
        assert h.app.led.output.snapshot() == frame
        assert not h.app._game_prompt_cues and not h.app._game_cue_tasks
        assert h.state.game_engine_state.seven_exit_phase == 'progress'
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['HandsetPlaced','SessionReset','HardwareDisconnected','help','voice_exit'])
async def test_actual_boundary_disarms_gesture_and_late_callbacks(boundary):
    from test_contextual_game_flow import Harness
    h = Harness(band='6-7')
    try:
        await h.launch('quiz')
        sid = h.state.session_id
        await h.send('ButtonPressed', {'button_id':7})
        scope = h.app._game_timer_scopes[sid]
        h.clock += 5.
        await h.app.flush_game_timers()
        old = dict(h.app._active_audio[sid])
        if boundary in {'help','voice_exit'}:
            await h.send('SpeechReceived', text='Позови взрослого' if boundary=='help' else 'Не хочу играть')
        else:
            await h.send(boundary)
        frame = dict(h.app.led.output.snapshot())
        state = h.app.canonical_runtime.sessions.get(sid)
        assert state is None or state.game_engine_state is None or state.game_engine_state.seven_exit_phase == 'idle'
        assert sid not in h.app._game_timer_scopes
        assert await h.app._fire_game_timer(sid,scope) is None
        await h.send('AudioCompleted', {**old,'response_id':old['content_id']})
        assert h.app.led.output.snapshot() == frame
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_http_and_simulator_release_preserve_only_real_boolean_cancellation():
    import httpx
    from app.foundation.config import Settings
    from app.foundation.lifecycle import RuntimeState
    from app.foundation.stand import create_stand_app
    api = create_stand_app(Settings(profile='mock',hardware_mode='simulator',stand_access_token='hold-test'))
    stand = api.state.stand
    api.state.lifecycle._state = RuntimeState.READY  # Local admission seam, no service-readiness claim.
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url='http://stand',headers={'x-stand-token':'hold-test'}) as client:
            for path,base in [('/stand/button-released',{'button_id':7}),
                              ('/stand/simulator/action',{'action':'button','button_id':7,'press':'release'})]:
                for cancelled in [False,True]:
                    response = await client.post(path,json={**base,'cancelled':cancelled})
                    assert response.status_code == 200, response.text
                    event = stand.publisher.events[-1]
                    assert event['type'] == 'ButtonReleased' and event['payload']['cancelled'] is cancelled
                for malformed in [0,1,'false','true',None,[],{}]:
                    response = await client.post(path,json={**base,'cancelled':malformed})
                    assert response.status_code == 422, response.text
            for press in ['press','hold','long_hold']:
                response = await client.post('/stand/simulator/action',json={
                    'action':'button','button_id':7,'press':press,'cancelled':True,'duration_ms':600})
                assert response.status_code == 422
    finally:
        await stand.aclose()


def test_public_hold_projection_is_bounded_and_contains_no_gesture_owner():
    from app.foundation.canonical_contract import CanonicalState
    from app.foundation.simulator import SimulatorAcceptanceHarness
    h = SimulatorAcceptanceHarness()
    try:
        engine = edge(quiz(), 'ButtonPressed', 100.).state
        state = CanonicalState('stand',mode='game_active',handset='up',active_game_id='quiz',game_audio_stage='playing',game_engine_state=engine)
        h.stand.application.canonical_runtime.sessions['stand'] = state
        for phase,step in [('pending',0),('progress',4),('ready',7)]:
            state.game_engine_state = replace(engine,seven_exit_phase=phase,seven_exit_step=step)
            h.stand.application.last_canonical_state = state.snapshot()
            public = h.stand.panel_snapshot()['canonical_state']
            assert public['game_exit_hold'] == {'phase':phase,'step':step}
            assert 'game_engine_state' not in public
        for phase,step in [('idle',0),('ready',8),('ready',True),('invented',2)]:
            state.game_engine_state = replace(engine,seven_exit_phase=phase,seven_exit_step=step)
            h.stand.application.last_canonical_state = state.snapshot()
            assert 'game_exit_hold' not in h.stand.panel_snapshot()['canonical_state']
    finally:
        h.stand.close()


def test_ws_simulator_action_enforces_owner_and_preserves_fifo():
    from starlette.testclient import TestClient
    from app.foundation.config import Settings
    from app.foundation.lifecycle import RuntimeState
    from app.foundation.stand import create_stand_app
    api = create_stand_app(Settings(profile='mock', hardware_mode='simulator', stand_access_token='ws-hold-test'))
    stand = api.state.stand
    seen = []
    owners = []
    async def fake_action(payload, *, input_owner, input_guard):
        seen.append(dict(payload))
        owners.append((input_owner, input_guard()))
        return {"reason": "ok"}
    stand.remote_devices.action = fake_action
    with TestClient(api) as client:
        api.state.lifecycle._state = RuntimeState.READY
        with client.websocket_connect('/stand/ws', headers={'x-stand-token': 'ws-hold-test'}) as owner_ws:
            initial = owner_ws.receive_json()
            assert initial['write_owner'] is True
            with client.websocket_connect('/stand/ws', headers={'x-stand-token': 'ws-hold-test'}) as guest_ws:
                guest_init = guest_ws.receive_json()
                assert guest_init['write_owner'] is False
                guest_ws.send_json({'type': 'simulator_action', 'action': 'button', 'button_id': 7, 'press': 'press', 'epoch': 1})
                err = guest_ws.receive_json()
                assert err['type'] == 'error' and err['error']['code'] == 'conflict'
            owner_ws.send_json({'type': 'simulator_action', 'action': 'button', 'button_id': 7, 'press': 'press', 'epoch': 1, 'request_id': 'press-7'})
            owner_ws.send_json({'type': 'simulator_action', 'action': 'button', 'button_id': 7, 'press': 'release', 'epoch': 1, 'request_id': 'release-7'})
            completed = []
            while len(completed) < 2:
                ack = owner_ws.receive_json()
                assert ack['type'] == 'button_ack' and ack['error'] is None
                if ack['stage'] == 'completed':
                    completed.append(ack['request_id'])
            assert completed == ['press-7', 'release-7']
            owner_ws.send_json({'type': 'ping'})
            assert owner_ws.receive_json()['type'] == 'pong'
    assert [(item['press'], item['button_id']) for item in seen] == [('press', 7), ('release', 7)]
    assert owners[0][0] == owners[1][0] and owners[0][0].startswith('browser_ws:')
    assert all(current for _, current in owners)
