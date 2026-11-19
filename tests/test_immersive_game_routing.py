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
    assert short_release['reason']=='game_exit_aborted' and r.state.mode=='game_active'
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


def test_quiz_with_three_or_fewer_options_does_not_fake_narrow():
    r = Route('6-7')
    r.menu()
    r.start(2)
    r.ready()
    engine = r.state.game_engine_state
    small_idx = next(i for i, c in enumerate(engine.deck) if len(c.options) <= 3)
    r.state.game_engine_state = replace(engine, challenge_index=small_idx, phase='playing')
    ch = current_challenge(r.state.game_engine_state)
    wrong_btn = next(b for b, _ in ch.options if b != ch.answer)
    res = r.press(wrong_btn)
    r.release(wrong_btn)
    assert res['reason'] == 'game_retry'
    assert r.state.game_engine_state.narrowed_options == ()
    assert tuple(game_answer_buttons(r.state.game_engine_state)) == tuple(b for b, _ in ch.options)

