"""Canonical game control, prompt ownership and startup regression scenarios."""
from dataclasses import replace
import pytest
from app.foundation.canonical_contract import CanonicalRuntime, CanonicalState, CanonicalEvent
from app.foundation.game_engine import IMMERSIVE_GAME_SPECS, current_challenge, game_answer_buttons
from app.foundation.menu_content import build_menu_page
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer


class Route:
    def __init__(self, band='3-5'):
        self.clock=10.
        self.runtime=CanonicalRuntime(monotonic_now=lambda:self.clock)
        self.runtime.menu_voice_capable=True
        self.state=CanonicalState('games',mode='root_menu',handset='up',age_band=band)
        self.runtime.sessions['games']=self.state
        self.seq=0
    def send(self,kind,payload=None,text=None):
        self.seq+=1
        return self.runtime.handle(CanonicalEvent(kind,'games',self.seq,self.state.state_version,
            'test',payload or {},str(self.seq),monotonic_timestamp=self.clock),transient_text=text,
            personal_welcome_answer_verified=True,dialogue_fallback_authorized=True,
            game_timer_authorized=True)
    def done(self):
        return self.send('AudioCompleted',{'response_id':self.state.active_audio_id,
            'playback_lease':self.state.playback_lease})
    def press(self,b):return self.send('ButtonPressed',{'button_id':b})
    def release(self,b):return self.send('ButtonReleased',{'button_id':b})
    def menu(self):
        first=self.press(1);self.release(1)
        assert first['state']['scene_menu_phase']=='invite'
        assert first['state']['active_audio_id']=='PHRASE_019'
        self.done()  # invitation then exact spoken descriptor
        assert self.state.active_audio_id=='MENU_GAME'
        self.done()  # repeat question
        self.done()  # repeat listen
        self.send('VoiceIntentReady',{'intent':'no','safety_severity':'none'},'нет')
        assert self.state.scene_menu_phase=='ready'
    def start(self,slot):
        result=self.press(slot)
        assert result['state']['active_audio_id']=='PHRASE_020'
        self.release(slot)
        return result
    def ready(self):
        prompt=self.done()
        assert prompt['state']['active_audio_id']=='GAME_PROMPT'
        self.done()
        assert self.state.game_audio_stage=='playing'


@pytest.mark.parametrize('band',['1-3','3-5','5-7','4-5','6-7'])
def test_five_game_choices_are_stable_and_five_is_a_choice(band):
    page=build_menu_page('game_menu',band,game_specs=IMMERSIVE_GAME_SPECS)
    assert [x.id for x in page.items]==['free_lights','quiz','repeat_pattern','odd_one_out','word_game']
    assert page.buttons==(1,2,3,4,5,7)
    assert page.action_for(5)==('select','word_game')
    assert page.as_dict()==build_menu_page('game_menu',band,game_specs=IMMERSIVE_GAME_SPECS).as_dict()
    assert 'Пять — помощь' not in page.spoken_text


def test_invite_menu_start_and_prompt_are_ordered_and_do_not_score_early():
    r=Route();r.menu();r.start(2)
    old=r.state.game_engine_state
    answer=current_challenge(old).answer
    r.press(answer);r.release(answer)
    assert r.state.game_engine_state.round==old.round
    prompt=r.done()
    effect=next(x for x in prompt['effects'] if x['type']=='game_prompt')
    assert effect['content_id']=='GAME_PROMPT' and effect['playback_lease']==r.state.playback_lease
    assert effect['options'] and effect['answer_buttons']
    old=r.state.game_engine_state
    version=r.state.state_version
    lease=r.state.playback_lease
    r.press(answer);r.release(answer)
    assert r.state.game_engine_state.round==old.round
    assert r.state.state_version==version and r.state.playback_lease==lease
    stale=r.send('AudioCompleted',{'response_id':'GAME_PROMPT','playback_lease':'stale'})
    assert stale['effects']==[] and r.state.game_audio_stage=='prompt'
    r.done()
    r.press(answer);r.release(answer)
    assert r.state.game_engine_state.round==old.round+1
    assert r.state.game_audio_stage=='prompt'


def test_free_light_release_does_not_overwrite_the_ripple_and_seven_requires_hold():
    r=Route();r.menu();r.start(1)
    # Free lights is playable immediately during the intro prompt without waiting.
    pressed_early=r.press(1)
    assert any(e.get('animation')=='ripple' and e['origin_button']==1 for e in pressed_early['effects'])
    r.release(1)
    r.ready()
    pressed=r.press(4)
    assert any(e.get('animation')=='ripple' and e['origin_button']==4 for e in pressed['effects'])
    released=r.release(4)
    assert not any(e['type'] in {'lighting_preset','game_lighting'} for e in released['effects'])
    short_press=r.press(7)
    assert short_press['reason']=='game_exit_pending' and r.state.mode=='game_active'
    short_release=r.release(7)
    assert short_release['reason']=='game_free_play' and r.state.mode=='game_active'
    assert {'type':'game_lighting','animation':'ripple','origin_button':7} in short_release['effects']
    r.press(7)
    r.clock += 5.1
    exited=r.release(7)
    assert exited['reason']=='game_exited_by_long_button' and r.state.mode=='game_menu'
    assert {'audio_stop','capture_close','game_exit'} <= {e['type'] for e in exited['effects']}


def test_five_is_game_answer_when_offered_and_never_help_in_game_menu():
    r=Route('5-7');r.menu()
    r.start(5)
    assert r.state.active_game_id=='word_game' and r.state.mode=='game_active'
    r.ready()
    result=r.send('VoiceIntentReady',{'intent':'free_speech','safety_severity':'none'},'Кот')
    assert result['reason']=='word_game_turn'
    assert next(e for e in result['effects'] if e['type']=='bounded_dialogue_turn')['game_id']=='word_game'


def test_seven_is_an_answer_when_the_current_quiz_offers_it():
    r=Route('5-7');r.menu();r.start(2);r.ready()
    # Choose an actual deterministic challenge whose semantic answers contain 7.
    engine=r.state.game_engine_state
    selected=None
    for index in range(len(engine.deck)):
        candidate=replace(engine,challenge_index=index)
        if 7 in game_answer_buttons(candidate):selected=candidate;break
    assert selected is not None
    r.state.game_engine_state=selected
    result=r.press(7)
    assert r.state.mode=='game_active' and result['reason']!='game_exited_by_button'


@pytest.mark.parametrize('text',['не хочу играть','Я не хочу больше играть','хватит','давай закончим','выйти из игры','вернись в меню'])
@pytest.mark.parametrize('slot',[1,2,3,4,5])
def test_all_games_accept_natural_voice_exit_even_while_prompt_is_pending(text,slot):
    r=Route();r.menu();r.start(slot)
    result=r.send('VoiceIntentReady',{'intent':'free_speech','safety_severity':'none'},text)
    assert result['reason']=='game_exited_by_voice' and r.state.mode=='game_menu'
    assert r.state.game_engine_state is None


@pytest.mark.parametrize('severity,intent',[('urgent','free_speech'),('none','help_request'),('concern','free_speech')])
def test_child_safety_preempts_game_exit_and_word_generation(severity,intent):
    r=Route();r.menu();r.start(5);r.ready()
    result=r.send('VoiceIntentReady',{'intent':intent,'safety_severity':severity},'не хочу играть')
    assert r.state.mode==('support_intake' if severity=='concern' else 'safety_stop')
    assert not any(e['type']=='bounded_dialogue_turn' for e in result['effects'])


def test_progress_survives_reentry_and_is_scrubbed_when_session_identity_clears():
    r=Route();r.menu();r.start(3);r.ready()
    r.state.game_engine_state=replace(r.state.game_engine_state,level=4)
    r.press(7);r.clock += 5.1;r.release(7)
    assert r.state.game_progress['repeat_pattern']==4
    assert r.state.mode=='game_menu'
    r.start(3)
    assert r.state.game_engine_state.level==4
    r.send('SessionReset')
    assert r.state.game_progress=={}


def test_word_provider_exit_and_new_voice_exit_transitions_are_authorized():
    graph=CanonicalLangGraphAuthorizer()
    for event in ['VoiceIntentReady','DialogueFallback','AudioFailed']:
        assert graph.authorize('game_active','root_menu',event)['ok']
    r=Route();r.menu();r.start(5);r.ready()
    assert r.send('DialogueFallback',{'reason':'dialogue_exit'})['reason']=='word_game_finished'


def test_fifth_odd_one_out_option_can_score_without_invoking_help():
    r=Route();r.menu();r.start(4);r.ready()
    engine=r.state.game_engine_state
    index=next(i for i,c in enumerate(engine.deck) if c.answer==5)
    engine=replace(engine,challenge_index=index)
    r.state.game_engine_state=engine
    result=r.press(5)
    assert r.state.mode=='game_active' and r.state.game_engine_state.round==engine.round+1
    assert result['reason']!='help_priority'


def test_game_chooser_voice_exit_works_after_the_repeat_answer():
    r=Route();r.menu()
    assert 'speech' in r.state.expected_inputs
    result=r.send('VoiceIntentReady',{'intent':'free_speech','safety_severity':'none'},'не хочу играть')
    assert result['reason']=='game_exited_by_voice' and r.state.mode=='root_menu'


def test_word_provider_retry_recording_reopens_the_next_word_turn():
    r=Route();r.menu();r.start(5);r.ready()
    retry=r.send('DialogueFallback',{'reason':'provider_or_validation_failure'})
    assert retry['reason']=='word_game_retry' and r.state.active_audio_id=='PHRASE_104'
    r.done()
    assert r.state.active_audio_id is None and r.state.mode=='game_active'
    assert r.send('VoiceIntentReady',{'intent':'free_speech','safety_severity':'none'},'Собака')['reason']=='word_game_turn'


def test_failed_game_menu_audio_keeps_the_advertised_voice_exit_live():
    r=Route()
    r.press(1);r.release(1)
    result=r.send('AudioFailed',{'response_id':r.state.active_audio_id,
        'playback_lease':r.state.playback_lease})
    assert r.state.scene_menu_phase=='ready' and 'speech' in r.state.expected_inputs
    assert {'type':'capture_open'} in result['effects']
    assert r.send('VoiceIntentReady',{'intent':'free_speech','safety_severity':'none'},
                  'не хочу играть')['reason']=='game_exited_by_voice'


def test_word_game_help_then_root_and_menu_does_not_generate_a_word_turn_in_menu():
    r=Route();r.menu();r.start(5);r.ready()
    assert r.press(5)['reason']=='help_priority'
    r.release(5)
    for _ in range(2):
        r.press(1);r.release(1)
    assert r.state.mode=='root_menu'
    assert r.state.active_game_id is None and r.state.game_audio_stage=='idle'
    assert r.state.game_engine_state is None
    r.press(1);r.release(1)
    r.done();r.done();r.done()
    result=r.send('VoiceIntentReady',{'intent':'no','safety_severity':'none'},'нет')
    assert r.state.mode=='game_menu' and r.state.scene_menu_phase=='ready'
    assert result['reason']!='word_game_turn'
    assert not any(e['type']=='bounded_dialogue_turn' for e in result['effects'])


@pytest.mark.asyncio
async def test_irrelevant_keys_during_a_real_word_turn_preserve_its_history_owner():
    import asyncio
    from test_immersive_game_runtime import GameRuntime
    h=GameRuntime('6-7')
    entered,release=asyncio.Event(),asyncio.Event()
    original=h.app.bounded_s2s.run_text_turn
    async def blocked(text,context):
        entered.set()
        await release.wait()
        return await original(text,context)
    task=None
    try:
        await h.start(5);await h.complete()
        h.app.bounded_s2s.run_text_turn=blocked
        task=asyncio.create_task(h.send('SpeechReceived',text='Кот'))
        await asyncio.wait_for(entered.wait(),3)
        version=h.state.state_version
        frame=h.app.led.output.snapshot()
        for button in (1,2,3,4,6):await h.key(button)
        assert h.state.state_version==version and h.app.led.output.snapshot()==frame
        release.set()
        result=await task
        effect=next(e for e in result['outputs'] if e.get('type')=='bounded_dialogue_turn')
        assert effect['resolution']=='bounded_dialogue_completed'
        context=h.app.conversation_contexts[h.state.session_id]
        assert context.turns==1 and context.history[0][0]=='Кот'
        assert h.state.mode=='game_active' and h.state.active_game_id=='word_game'
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()


def test_quiz_narrows_from_seven_to_three_on_first_wrong_and_resets_next_round():
    r = Route('6-7')
    r.menu()
    r.start(2)
    r.ready()
    engine = r.state.game_engine_state
    paint_idx = next(i for i, c in enumerate(engine.deck) if c.id == 'quiz-6-7-23')
    r.state.game_engine_state = replace(engine, challenge_index=paint_idx, phase='playing')
    challenge = current_challenge(r.state.game_engine_state)
    assert challenge.answer == 7
    assert tuple(game_answer_buttons(r.state.game_engine_state)) == (1, 2, 3, 4, 5, 6, 7)

    # Idle hint timer must NOT narrow options.
    r.state.game_engine_state = replace(r.state.game_engine_state, hint_deadline=r.clock + 1.0)
    r.clock += 2.0
    r.send('GameTimer', {'timer_generation': r.state.game_engine_state.timer_generation})
    assert tuple(game_answer_buttons(r.state.game_engine_state)) == (1, 2, 3, 4, 5, 6, 7)
    r.done()

    # First wrong answer (button 1) narrows to 3 options: 7 + 2 wrong (excluding 1), no answer spoiler.
    wrong_res = r.press(1)
    r.release(1)
    assert wrong_res['reason'] == 'game_retry'
    narrowed = tuple(game_answer_buttons(r.state.game_engine_state))
    assert len(narrowed) == 3 and 7 in narrowed and 1 not in narrowed
    lighting = next(e for e in wrong_res['effects'] if e.get('type') == 'lighting_preset' and e.get('preset') == 'GAME_OUTPUT')
    assert tuple(lighting['buttons']) == narrowed
    prompt_eff = next(e for e in wrong_res['effects'] if e.get('type') == 'game_prompt')
    assert 'фиолетов' not in prompt_eff['prompt'].lower()
    assert prompt_eff['answer_buttons'] == list(narrowed)
    assert [b for b, _ in prompt_eff['options']] == list(narrowed)
    assert prompt_eff['prompt'] == challenge.hints[0]

    # Rapid tap during hint animation is ignored and does not corrupt state.
    other_wrong = next(b for b in narrowed if b != 7)
    rapid = r.press(other_wrong)
    r.release(other_wrong)
    assert rapid['reason'] == 'game_prompt_waiting'
    assert r.state.game_engine_state.challenge_attempts == 1
    r.done()

    # Tapping an eliminated button (including 5) is ignored and never triggers help_priority.
    eliminated = next(b for b in (1, 2, 3, 4, 5, 6) if b not in narrowed)
    ignored = r.press(eliminated)
    r.release(eliminated)
    assert ignored['reason'] == 'game_ignored'
    assert r.state.game_engine_state.challenge_attempts == 1
    if 5 not in narrowed:
        ignored_five = r.press(5)
        r.release(5)
        assert ignored_five['reason'] == 'game_ignored' and r.state.mode == 'game_active'

    # Second wrong answer keeps the 3 narrowed options.
    second = r.press(other_wrong)
    r.release(other_wrong)
    assert second['reason'] == 'game_retry'
    assert tuple(game_answer_buttons(r.state.game_engine_state)) == narrowed
    r.done()

    # Short-tap 7 scores the answer and resets narrowing for the next question.
    win = r.press(7)
    assert win['reason'] == 'game_exit_pending'
    r.clock += 0.1
    scored = r.release(7)
    assert scored['reason'] == 'game_progress'
    assert r.state.game_engine_state.narrowed_options == ()
    assert r.state.game_engine_state.challenge_attempts == 0
    next_ch = current_challenge(r.state.game_engine_state)
    assert tuple(game_answer_buttons(r.state.game_engine_state)) == tuple(b for b, _ in next_ch.options)


def test_quiz_every_band_uses_all_seven_buttons_and_does_not_narrow_small_odd():
    for band in ('1-3', '4-5', '6-7'):
        r = Route(band)
        r.menu()
        r.start(2)
        r.ready()
        assert tuple(game_answer_buttons(r.state.game_engine_state)) == (1, 2, 3, 4, 5, 6, 7)


def test_quiz_rules_spoken_once_and_third_miss_highlights_answer_without_autoscore():
    r = Route('6-7')
    r.menu()
    r.start(2)
    first_prompt = r.done()
    eff1 = next(e for e in first_prompt['effects'] if e['type'] == 'game_prompt')
    assert eff1['rules_intro'] == 'Нажимай нужный цвет или цифру. Чтобы выйти, подержи семь.'
    assert eff1['exit_hint'] is None
    r.done()
    ch = current_challenge(r.state.game_engine_state)
    wrong_btns = [b for b, _ in ch.options if b != ch.answer]
    # Two inactivity pauses (wait_hint) must NOT count toward the 3-miss highlight threshold.
    for _ in range(2):
        r.state.game_engine_state = replace(r.state.game_engine_state, hint_deadline=r.clock + 1.0)
        r.clock += 2.0
        r.send('GameTimer', {'timer_generation': r.state.game_engine_state.timer_generation})
        r.done()
    assert r.state.game_engine_state.wrong_attempts == 0
    # 1st miss -> narrows to 3 (not straight to 1)
    r.press(wrong_btns[0]); r.release(wrong_btns[0]); r.done()
    assert len(game_answer_buttons(r.state.game_engine_state)) == 3
    # 2nd miss
    other_wrong = next(b for b in game_answer_buttons(r.state.game_engine_state) if b != ch.answer)
    r.press(other_wrong); r.release(other_wrong); r.done()
    # 3rd miss -> gently highlights only the right button, without auto-scoring
    third = r.press(other_wrong); r.release(other_wrong)
    assert third['reason'] == 'game_retry'
    assert tuple(game_answer_buttons(r.state.game_engine_state)) == (ch.answer,)
    assert r.state.game_engine_state.round == 0
    r.done()
    # Exit and re-enter quiz in the same session -> rules_intro is NOT repeated
    r.press(7); r.clock += 5.1; r.release(7)
    assert r.state.mode == 'game_menu'
    r.start(2)
    second_prompt = r.done()
    eff2 = next(e for e in second_prompt['effects'] if e['type'] == 'game_prompt')
    assert eff2['rules_intro'] is None and eff2['exit_hint'] is None


def test_quiz_never_dictates_options_and_blocks_early_tap_until_prompt_completes():
    from app.foundation.game_presentation import GamePresentationMixin
    mixin = GamePresentationMixin()
    eff = {'game_id': 'quiz', 'prompt': 'Сколько лап у собаки?', 'display': 'digits',
           'rules_intro': 'Нажимай нужный цвет или цифру. Чтобы выйти, подержи семь.',
           'options': ((1, 'один'), (2, 'два'), (3, 'три'), (4, 'четыре'), (5, 'пять'), (6, 'шесть'), (7, 'семь'))}
    segs = mixin._game_prompt_segments(eff)
    assert [t for t, _, _ in segs] == [
        'Нажимай нужный цвет или цифру. Чтобы выйти, подержи семь.',
        'Сколько лап у собаки?',
    ]

    r = Route('6-7')
    r.menu()
    r.start(2)
    r.done()  # PHRASE_020 -> GAME_PROMPT (rules + question)
    assert r.state.game_audio_stage == 'prompt'
    answer = current_challenge(r.state.game_engine_state).answer
    # Tap BEFORE question finishes (even during rules/praise/question) must NOT score!
    r.press(answer)
    early = r.release(answer)
    assert early['reason'] in {'game_prompt_waiting', 'game_exit_aborted'}
    assert r.state.game_audio_stage == 'prompt' and r.state.game_level == 0
    assert r.state.game_engine_state.round == 0
    # After AudioCompleted(GAME_PROMPT), valid tap scores immediately.
    r.done()
    assert r.state.game_audio_stage == 'playing'
    p = r.press(answer)
    rel = r.release(answer)
    scored = rel if answer == 7 else p
    assert scored['reason'] == 'game_progress'
    assert r.state.game_engine_state.round == 1


def test_quiz_short_round_completes_cleanly_and_120s_rfid_expiry_preserves_game():
    r = Route('4-5')
    r.runtime.optional_identity = True
    r.state.profile_ref = 'demo-ref'
    r.state.profile_status = 'identified'
    r.state.identity_request_id = 'req-120'
    r.state.identity_expires_monotonic = r.clock + 120.0
    r.menu()
    r.start(2)
    r.ready()
    # Play 5 questions
    for i in range(5):
        ans = current_challenge(r.state.game_engine_state).answer
        r.press(ans)
        if ans == 7:
            r.clock += 0.1
        r.release(ans)
        r.done(); r.done()  # PHRASE_022 + GAME_PROMPT -> playing
    assert r.state.game_engine_state.round == 5
    # Simulate 120s RFID expiry mid-quiz!
    r.clock += 125.0
    r.seq += 1
    exp = r.runtime.handle(CanonicalEvent('IdentityAuthorizationExpired', 'games', r.seq, r.state.state_version, 'internal_identity',
                                          {'request_id': 'req-120'}), r.clock, identity_event_authorized=True)
    assert exp['reason'] == 'public_game_identity_expired'
    assert r.state.profile_status == 'anonymous' and r.state.profile_ref is None
    assert r.state.mode == 'game_active' and r.state.active_game_id == 'quiz'
    assert r.state.handset == 'up' and r.state.game_engine_state.round == 5
    # Finish remaining 3 questions (round 5 -> 8 = natural short round completion)
    for i in range(3):
        ans = current_challenge(r.state.game_engine_state).answer
        p = r.press(ans)
        if ans == 7:
            r.clock += 0.1
        rel = r.release(ans)
        res = rel if ans == 7 else p
        if i < 2:
            assert res['reason'] == 'game_progress'
            r.done(); r.done()
        else:
            assert res['reason'] == 'game_completed'
    assert r.state.mode == 'game_menu'
    assert r.state.handset == 'up'
    # Re-enter quiz cleanly for 10+ total questions
    r.start(2)
    r.ready()
    assert r.state.mode == 'game_active' and r.state.game_engine_state.round == 0


def test_rapid_double_tap_7_from_game_menu_does_not_enter_schedule_weather():
    r = Route('4-5')
    r.menu()
    assert r.state.mode == 'game_menu'
    back = r.press(7)
    assert back['reason'] == 'game_menu_back'
    assert r.state.mode == 'root_menu'
    r.release(7)
    # Rapid second press 50ms later must not bounce into schedule_weather.
    r.clock += 0.05
    bounce = r.press(7)
    assert bounce['reason'] == 'submenu_back_bounce_ignored'
    assert r.state.mode == 'root_menu'
    r.release(7)
    # Deliberate press 700ms later enters schedule_weather normally.
    r.clock += 0.70
    ok = r.press(7)
    assert ok['reason'] == 'button_mode'
    assert r.state.mode == 'schedule_weather'


def test_game_prompt_audio_failed_retries_once_then_blocks_without_opening_answers():
    r = Route('4-5')
    r.menu()
    r.start(2)
    r.done()  # PHRASE_020 -> GAME_PROMPT
    assert r.state.game_audio_stage == 'prompt'
    # 1st AudioFailed retries GAME_PROMPT instead of opening silent answers
    r.seq += 1
    fail1 = r.runtime.handle(CanonicalEvent('AudioFailed', 'games', r.seq, r.state.state_version, 'browser_private_pcm',
        {'response_id': 'GAME_PROMPT', 'playback_lease': r.state.playback_lease}), r.clock)
    assert fail1['reason'] == 'game_prompt_retried'
    assert r.state.game_audio_stage == 'prompt'
    # 2nd AudioFailed moves to prompt_failed (answers 1..4 blocked; 5 retries)
    r.seq += 1
    fail2 = r.runtime.handle(CanonicalEvent('AudioFailed', 'games', r.seq, r.state.state_version, 'browser_private_pcm',
        {'response_id': 'GAME_PROMPT', 'playback_lease': r.state.playback_lease}), r.clock)
    assert fail2['reason'] == 'game_prompt_audio_failed'
    assert r.state.game_audio_stage == 'prompt_failed'
    blocked = r.press(1)
    assert blocked['reason'] == 'game_prompt_waiting'
    r.release(1)
    retry = r.press(5)
    assert retry['reason'] == 'game_prompt_manual_retry'
    assert r.state.game_audio_stage == 'prompt'
    r.release(5)
    r.done()
    assert r.state.game_audio_stage == 'playing'


def test_hold_6_for_5s_starts_silent_session_and_handset_controls_audio():
    t = 100.0
    rt = CanonicalRuntime(exhibition_mode=True, optional_identity=True, monotonic_now=lambda: t)
    sid = 'silent-1'
    seq = 0
    # 1. Press 6 while handset down -> silent_hold_started; early release cancels
    seq += 1
    p1 = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, 0, 'uart', {'button_id': 6}, f'c{seq}', t))
    assert p1['reason'] == 'silent_hold_started'
    assert rt.sessions[sid].silent_hold_active is True
    from app.foundation.lighting import BUTTON_WAVE_ELEMENTS
    assert p1['effects'][0]['elements'] == list(BUTTON_WAVE_ELEMENTS[6])
    t += 0.72
    seq += 1
    tick2 = rt.handle(CanonicalEvent('SilentHoldTick', sid, seq, p1['state']['state_version'], 'timer', {'generation': rt.sessions[sid].silent_hold_generation}, f'c{seq}', t))
    assert tick2['reason'] == 'silent_hold_progress'
    assert tick2['effects'][0]['elements'] == list(BUTTON_WAVE_ELEMENTS[6]) + list(BUTTON_WAVE_ELEMENTS[7])
    t += 1.28
    seq += 1
    rel1 = rt.handle(CanonicalEvent('ButtonReleased', sid, seq, tick2['state']['state_version'], 'uart', {'button_id': 6}, f'c{seq}', t))
    assert rel1['reason'] == 'silent_hold_cancelled'
    assert rt.sessions[sid].mode == 'idle'

    # 2. Hold 6 for 5s -> silent_hold_6_started -> menu_ready without audio/ringtone
    t += 1.0
    seq += 1
    p2 = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, rel1['state']['state_version'], 'uart', {'button_id': 6}, f'c{seq}', t))
    gen = rt.sessions[sid].silent_hold_generation
    t += 5.1
    seq += 1
    tick = rt.handle(CanonicalEvent('SilentHoldTick', sid, seq, p2['state']['state_version'], 'timer', {'generation': gen}, f'c{seq}', t))
    assert tick['reason'] == 'silent_hold_6_started'
    assert rt.sessions[sid].mode == 'menu_ready' and rt.sessions[sid].silent_mode is True
    assert not any(e.get('type') in {'play_asset', 'speak', 'capture_open'} for e in tick['effects'])

    # 3. Releasing 6 after threshold does NOT open bear_dialogue
    seq += 1
    rel2 = rt.handle(CanonicalEvent('ButtonReleased', sid, seq, tick['state']['state_version'], 'uart', {'button_id': 6}, f'c{seq}', t + 8.3))
    assert rel2['reason'] == 'silent_hold_release_consumed'
    assert rt.sessions[sid].mode == 'menu_ready'

    # 4. Duplicate HandsetPlaced while already down does NOT reset silent session
    seq += 1
    dup_down = rt.handle(CanonicalEvent('HandsetPlaced', sid, seq, rel2['state']['state_version'], 'handset', {}, f'c{seq}', t + 8.5))
    assert dup_down['reason'] == 'handset_already_placed'
    assert rt.sessions[sid].mode == 'menu_ready'

    # 5. Sound-required game (quiz) asks for handset and resumes on HandsetLifted
    seq += 1
    gmenu = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, rt.sessions[sid].state_version, 'uart', {'button_id': 1}, f'c{seq}', t + 9.0))
    assert rt.sessions[sid].mode == 'game_menu'
    seq += 1
    rt.handle(CanonicalEvent('ButtonReleased', sid, seq, rt.sessions[sid].state_version, 'uart', {'button_id': 1}, f'c{seq}', t + 9.1))
    seq += 1
    qreq = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, rt.sessions[sid].state_version, 'uart', {'button_id': 2}, f'c{seq}', t + 9.2))
    assert qreq['reason'] == 'game_handset_required'
    assert rt.sessions[sid].mode == 'awaiting_handset' and rt.sessions[sid].pending_game_id == 'quiz'
    seq += 1
    lift = rt.handle(CanonicalEvent('HandsetLifted', sid, seq, rt.sessions[sid].state_version, 'handset', {}, f'c{seq}', t + 10.0))
    assert lift['reason'] == 'game_started'
    assert rt.sessions[sid].mode == 'game_active' and rt.sessions[sid].active_game_id == 'quiz'
    assert rt.sessions[sid].silent_mode is False

    # 6. Real HandsetPlaced (up -> down) resets cleanly
    seq += 1
    placed = rt.handle(CanonicalEvent('HandsetPlaced', sid, seq, rt.sessions[sid].state_version, 'handset', {}, f'c{seq}', t + 11.0))
    assert placed['reason'] == 'exhibition_reset'


def test_hold_6_interrupted_by_handset_lifted_starts_voice_once():
    rt = CanonicalRuntime(exhibition_mode=True, optional_identity=True)
    sid = 'silent-lift-interrupt'
    t = 50.0
    p = rt.handle(CanonicalEvent('ButtonPressed', sid, 1, 0, 'uart', {'button_id': 6}, 'c1', t))
    assert p['reason'] == 'silent_hold_started'
    lift = rt.handle(CanonicalEvent('HandsetLifted', sid, 2, p['state']['state_version'], 'handset', {}, 'c2', t + 1.5))
    assert lift['reason'] == 'exhibition_handset_lifted'
    assert rt.sessions[sid].silent_hold_active is False and rt.sessions[sid].mode == 'voice_welcome'


def test_free_lights_short_7_ripples_and_hold_7_exits_without_ripple():
    clock = [100.0]
    rt = CanonicalRuntime(exhibition_mode=True, optional_identity=True, monotonic_now=lambda: clock[0])
    sid = 'free-lights-7'
    s0 = rt.handle(CanonicalEvent('SessionStarted', sid, 1, 0, 'panel', {}, 's1', clock[0]), session_action_authorized=True)
    gm = rt.handle(CanonicalEvent('ButtonPressed', sid, 2, s0['state']['state_version'], 'uart', {'button_id': 1}, 'p1', clock[0]))
    rt.handle(CanonicalEvent('ButtonReleased', sid, 3, gm['state']['state_version'], 'uart', {'button_id': 1}, 'r1', clock[0]))
    fl = rt.handle(CanonicalEvent('ButtonPressed', sid, 4, rt.sessions[sid].state_version, 'uart', {'button_id': 1}, 'p2', clock[0]))
    rt.handle(CanonicalEvent('ButtonReleased', sid, 5, fl['state']['state_version'], 'uart', {'button_id': 1}, 'r2', clock[0]))
    assert rt.sessions[sid].active_game_id == 'free_lights'

    # Short tap for every button 1..7 gives ripple from that button, no exit wave
    seq = 5
    for btn in range(1, 8):
        clock[0] += 1.0
        seq += 1
        p = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, rt.sessions[sid].state_version, 'uart', {'button_id': btn}, f'fl-p{btn}', clock[0]))
        assert not [e for e in p['effects'] if e.get('type') == 'lighting_wave_intent']
        if btn == 7:
            assert p['reason'] == 'game_exit_pending'
            clock[0] += 0.15
            seq += 1
            r = rt.handle(CanonicalEvent('ButtonReleased', sid, seq, p['state']['state_version'], 'uart', {'button_id': btn}, f'fl-r{btn}', clock[0]))
            assert r['reason'] == 'game_free_play'
            assert {'type': 'game_lighting', 'animation': 'ripple', 'origin_button': 7} in r['effects']
        else:
            assert p['reason'] == 'game_free_play'
            assert {'type': 'game_lighting', 'animation': 'ripple', 'origin_button': btn} in p['effects']
            clock[0] += 0.15
            seq += 1
            rt.handle(CanonicalEvent('ButtonReleased', sid, seq, p['state']['state_version'], 'uart', {'button_id': btn}, f'fl-r{btn}', clock[0]))
        assert rt.sessions[sid].active_game_id == 'free_lights'

    # Early release of 7 in middle of hold (>= 0.6s, < 5s) -> cancels exit, no ripple
    clock[0] += 2.0
    seq += 1
    p7e = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, rt.sessions[sid].state_version, 'uart', {'button_id': 7}, 'fl-p7e', clock[0]))
    clock[0] += 1.5
    seq += 1
    r7e = rt.handle(CanonicalEvent('ButtonReleased', sid, seq, p7e['state']['state_version'], 'uart', {'button_id': 7}, 'fl-r7e', clock[0]))
    assert r7e['reason'] == 'game_exit_aborted'
    assert rt.sessions[sid].active_game_id == 'free_lights'
    assert not [e for e in r7e['effects'] if e.get('animation') == 'ripple']

    # Long hold 7 (>= 5s) after cancel -> exits to game_menu without ripple on 7
    clock[0] += 2.0
    seq += 1
    p7h = rt.handle(CanonicalEvent('ButtonPressed', sid, seq, rt.sessions[sid].state_version, 'uart', {'button_id': 7}, 'fl-p7h', clock[0]))
    clock[0] += 5.2
    seq += 1
    r7h = rt.handle(CanonicalEvent('ButtonReleased', sid, seq, p7h['state']['state_version'], 'uart', {'button_id': 7}, 'fl-r7h', clock[0]))
    assert r7h['reason'] == 'game_exited_by_long_button'
    assert rt.sessions[sid].mode == 'game_menu'
    assert not [e for e in r7h['effects'] if e.get('animation') == 'ripple']




