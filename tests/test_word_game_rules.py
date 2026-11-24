"""Real rule/validator/PCM path, synthetic ASR and audio ports; no model/device."""
import asyncio
from dataclasses import replace
import json

import pytest

from app.foundation import word_game as rules
from app.foundation.s2s import MockSpeechRecognizer
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio
from test_game_audio_reminder_race import SPEECH, SILENCE


@pytest.mark.parametrize('form,lemma', [('обезьяну','обезьяна'), ('мышью','мышь'),
    ('котом','кот'), ('гуся','гусь'), ('ежом','ёж')])
def test_real_morphology_uses_one_common_noun_lemma(form, lemma):
    assert rules.noun(form, '6-7') == (lemma, None)


@pytest.mark.parametrize('text', ['не кот', 'кот или слон', 'абракадабря', 'Роман',
    'кот лиса', 'кот 7', 'совсем не знаю', 'слово из длинной и непонятной фразы'])
def test_uncertain_or_negated_input_does_not_guess(text):
    assert rules.noun(text, '6-7')[0] is None


@pytest.mark.parametrize('word,letter', [('мышь','ш'), ('конь','н'), ('подъезд','д'),
    ('столы','л'), ('ёж','ж'), ('лисий','й')])
def test_terminal_letters_follow_announced_rules(word, letter):
    assert rules.next_letter(word) == letter


def test_age_counterexample_monkey_to_elephant_is_not_a_universal_error():
    young = rules.play('обезьяну', rules.WordState(), '4-5')
    assert young.accepted and rules.play('слона', young.state, '4-5').accepted
    older = rules.play('обезьяну', rules.WordState(), '6-7')
    assert older.accepted
    bear = older.state.used[-1]
    assert bear.startswith('а') and older.state.required == rules.next_letter(bear)
    assert not rules.play('слона', older.state, '6-7').accepted


def test_categories_and_repeat_inflections_are_actual_rules():
    assert rules.play('яблоко', rules.WordState(), '1-3').accepted
    assert not rules.play('яблоко', rules.WordState(), '4-5').accepted
    assert rules.play('самолётом', rules.WordState(), '6-7').accepted
    first = rules.play('кот', rules.WordState(), '4-5')
    repeated = rules.play('котом', first.state, '4-5')
    assert not repeated.accepted and repeated.state == first.state
    assert 'уже было' in repeated.text


@pytest.mark.parametrize('word,band', [('кошку','4-5'), ('лошадью','4-5'), ('кролика','4-5'),
    ('бегемота','4-5'), ('конфету','1-3'), ('мороженое','1-3')])
def test_familiar_child_animals_and_food_are_not_an_unknown_category(word, band):
    assert rules.play(word, rules.WordState(), band).accepted


@pytest.mark.parametrize('band,bank', [('1-3',rules.ANIMALS+rules.FOOD),
    ('4-5',rules.ANIMALS), ('6-7',rules.WORDS)])
def test_all_authored_answers_are_accepted_common_noun_lemmas(band, bank):
    for word in bank:
        lemma, error = rules.noun(word, band)
        assert not error, word
        assert rules.spelling(lemma) == rules.spelling(word)


def test_pending_pcm_does_not_prove_a_word_was_heard():
    previous = rules.WordState(('кот', 'тигр'), 'р')
    proposal = rules.play('рыба', previous, '6-7')
    pending = replace(previous, pending=proposal.state)
    assert proposal.accepted and proposal.state.required == 'а'
    # The old emitted word was heard: continue on its last letter.
    continuation = rules.play('апельсин', pending, '6-7')
    assert continuation.accepted and 'рыба' in continuation.state.used
    # It was interrupted before the word: another legal R answer is still valid.
    correction = rules.play('робот', pending, '6-7')
    assert correction.accepted and 'рыба' not in correction.state.used
    assert correction.state.pending is None


def test_missing_morphology_fails_closed_without_fake_word(monkeypatch):
    monkeypatch.setattr(rules, '_morphology', lambda: None)
    result = rules.play('кот', rules.WordState(), '1-3')
    assert not result.accepted and result.state == rules.WordState()
    assert 'Не могу проверить' in result.text and 'семь' in result.text
    assert len(result.text.split()) <= 8


class NoWordModel:
    async def stream(self, *args, **kwargs):
        raise AssertionError('word turn must not ask an LLM to choose a legal word')
        yield ''


async def start(band='6-7'):
    h = Harness(band=band)
    render = quick_public_audio(h)
    await h.launch('word_game')
    render.texts.clear()
    h.app.bounded_s2s.llm = NoWordModel()
    return h, render


@pytest.mark.asyncio
async def test_actual_vad_inflected_monkey_never_speaks_illegal_elephant():
    h, render = await start()
    try:
        h.app.s2s_audio.recognizer = MockSpeechRecognizer('Обезьяну', .99)
        h.seq += 1
        result = await h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq)
        assert result['reason'] == 'word_game_turn_finished'
        context = h.app.conversation_contexts[h.state.session_id]
        assert context._word_state.used == ('обезьяна', 'акула')
        assert 'Акула' in ' '.join(render.texts) and 'Слон' not in ' '.join(render.texts)
        assert h.state.game_engine_state.round == context.turns == 1
        assert 'обезьяна' not in json.dumps(h.state.snapshot(), ensure_ascii=False)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_wrong_repeat_and_uncertain_leave_round_and_word_state_unchanged():
    h, render = await start()
    try:
        await h.send('SpeechReceived', text='обезьяна')
        context = h.app.conversation_contexts[h.state.session_id]
        committed = context._word_state
        for wrong in ('слон', 'обезьяну', 'не знаю'):
            render.texts.clear()
            result = await h.send('SpeechReceived', text=wrong)
            context = h.app.conversation_contexts[h.state.session_id]
            assert result['reason'] == 'word_game_retry_finished'
            assert h.state.game_engine_state.round == context.turns == 1
            assert context._word_state == committed
            assert len(render.texts) == 1 and 'PHRASE_104' not in str(result['outputs'])
        result = await h.send('SpeechReceived', text='апельсин')
        assert result['reason'] == 'word_game_turn_finished'
        assert h.state.game_engine_state.round == 2
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('band,elephant_accepted', [('4-5', True), ('6-7', False)])
async def test_announced_mode_governs_actual_asr_monkey_then_elephant(band, elephant_accepted):
    h = Harness(band=band)
    render = quick_public_audio(h)
    try:
        await h.launch('word_game')
        assert any(rules.introduction(band) in text for text in render.texts)
        h.app.bounded_s2s.llm = NoWordModel()
        for word in ('Обезьяну', 'Слона'):
            h.app.s2s_audio.recognizer = MockSpeechRecognizer(word, .99)
            h.seq += 1
            result = await h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq)
        context = h.app.conversation_contexts[h.state.session_id]
        assert result['reason'] == ('word_game_turn_finished' if elephant_accepted else 'word_game_retry_finished')
        assert ('слон' in context._word_state.used) is elephant_accepted
        assert context.turns == h.state.game_engine_state.round == (2 if elephant_accepted else 1)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('band,limit', [('1-3',4), ('4-5',5), ('6-7',6)])
async def test_all_legal_turns_naturally_restore_caller_and_own_budget(band, limit):
    h, render = await start(band)
    sid = h.state.session_id
    caller = h.app._game_callers[sid]['context']
    before = (list(caller.history), caller.turns)
    try:
        for index in range(limit):
            state = h.app._bounded_dialogue_context(sid, game_id='word_game')._word_state
            candidates = (*rules.WORDS, 'автомобиль', 'носок', 'рама', 'азбука')
            candidate = next(word for word in candidates if rules.play(word, state, band).accepted)
            result = await h.send('SpeechReceived', text=candidate)
            assert result['reason'] == ('word_game_finished' if index == limit-1 else 'word_game_turn_finished')
        assert h.state.mode == 'bear_dialogue' and h.state.game_engine_state is None
        assert h.app.conversation_contexts[sid] is caller
        assert (caller.history, caller.turns) == before and not h.app._game_callers
        assert 'разговору' in render.texts[-1]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_blocked_output_never_commits_word_or_round(monkeypatch):
    from app.foundation.validation import ValidationDecision
    h, render = await start()
    async def block(*args, **kwargs):
        return ValidationDecision('block', False, 'blocked', 1., 'test')
    monkeypatch.setattr(h.app.bounded_s2s.response_validator, 'validate', block)
    try:
        result = await h.send('SpeechReceived', text='кот')
        effect = next(item for item in result['outputs'] if item.get('type') == 'bounded_dialogue_turn')
        assert effect['fallback']['reason'] == 'word_game_retry'
        assert h.app.conversation_contexts[h.state.session_id]._word_state == rules.WordState()
        assert h.state.game_engine_state.round == 0
        assert not h.app.bounded_s2s.playback.played
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_structured_provider_mode_still_validates_authored_word_before_pcm():
    h, render = await start('1-3')
    class StructuredNoWordModel(NoWordModel):
        supports_dialogue_json = True
        async def stream_dialogue(self, *args, **kwargs):
            raise AssertionError('no model hop for a rule turn')
            yield ''
    h.app.bounded_s2s.llm = StructuredNoWordModel()
    try:
        result = await h.send('SpeechReceived', text='мышью')
        assert result['reason'] == 'word_game_turn_finished'
        assert len(render.texts) == 1 and len(render.texts[0].split()) <= 8
        assert h.app.conversation_contexts[h.state.session_id]._word_state.used[0] == 'мышь'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('word,old_accepted', [('апельсин',True), ('робот',False)])
async def test_barge_in_can_continue_pending_word_or_correct_unheard_word(word, old_accepted):
    h, render = await start()
    sid = h.state.session_id
    previous = rules.WordState(('кот','тигр'), 'р')
    context = h.app._bounded_dialogue_context(sid, game_id='word_game')
    context._word_state = previous
    entered, block = asyncio.Event(), asyncio.Event()
    class PartialPlayback:
        active_token = 0
        def __init__(self): self.calls = 0
        async def play(self, audio):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await block.wait()
        async def stop(self): pass
    h.app.bounded_s2s.playback = PartialPlayback()
    pending = None
    try:
        pending = asyncio.create_task(h.send('SpeechReceived', text='рыба'))
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(2):
            while context._word_state.pending is None:
                await asyncio.sleep(0)
        assert context._word_state.used == previous.used and context.turns == 0
        result = await h.send('SpeechReceived', text=word)
        await asyncio.gather(pending, return_exceptions=True)
        current = h.app.conversation_contexts[sid]
        assert result['reason'] == 'word_game_turn_finished'
        assert ('рыба' in current._word_state.used) is old_accepted
        assert current._word_state.pending is None
        assert current.turns == h.state.game_engine_state.round == 1
        assert word in current._word_state.used
    finally:
        block.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_exit_during_pending_pcm_discards_word_state_and_late_owner():
    h, render = await start()
    sid = h.state.session_id
    entered, block = asyncio.Event(), asyncio.Event()
    class PartialPlayback:
        active_token = 0
        async def play(self, audio):
            entered.set()
            await block.wait()
        async def stop(self): pass
    h.app.bounded_s2s.playback = PartialPlayback()
    pending = None
    caller = h.app._game_callers[sid]['context']
    before = (list(caller.history), caller.turns)
    try:
        pending = asyncio.create_task(h.send('SpeechReceived', text='кот'))
        await asyncio.wait_for(entered.wait(), 2)
        await h.key(7, duration=6.)
        returned_audio = dict(h.app._active_audio[sid])
        block.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert h.state.mode == 'bear_dialogue' and h.app.conversation_contexts[sid] is caller
        assert (caller.history, caller.turns) == before
        assert not hasattr(caller, '_word_state') and not h.app._game_callers
        assert h.app._active_audio[sid] == returned_audio
        assert not h.app.bounded_s2s.resource_snapshot()['active_tasks']
    finally:
        block.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_missing_morph_hint_does_not_autoretry_or_extend_idle(monkeypatch):
    h, render = await start('1-3')
    monkeypatch.setattr(rules, '_morphology', lambda: None)
    try:
        result = await h.send('SpeechReceived', text='кот')
        assert result['reason'] == 'word_game_retry_finished'
        assert len(render.texts) == 1 and 'семь' in render.texts[0]
        context = h.app.conversation_contexts[h.state.session_id]
        assert context.turns == h.state.game_engine_state.round == 0
        h.clock += 121
        await h.app.expire_engagement_sessions()
        assert h.state.mode == 'root_menu' and not h.app._game_callers
        assert len(render.texts) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_repeated_interruption_cannot_grow_private_word_state_unbounded():
    h, render = await start()
    context = h.app._bounded_dialogue_context(h.state.session_id, game_id='word_game')
    context._word_state = rules.WordState(pending=rules.WordState(tuple(rules.WORDS[:rules.MAX_SEEN_WORDS]), 'а'))
    try:
        result = await h.send('SpeechReceived', text='апельсин')
        effect = next(item for item in result['outputs'] if item.get('type') == 'bounded_dialogue_turn')
        assert effect['fallback_reason'] == 'bounded_limit'
        assert h.state.mode == 'bear_dialogue' and not h.app._game_callers
        assert not hasattr(h.app.conversation_contexts[h.state.session_id], '_word_state')
        assert len(render.texts) == 1 and 'Вернулись к разговору' in render.texts[0]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_reset_and_new_visit_forget_previous_private_words():
    h, render = await start()
    sid = h.state.session_id
    try:
        await h.send('SpeechReceived', text='обезьяну')
        old = h.app.conversation_contexts[sid]
        assert old._word_state.used
        await h.app.reset_session(sid)
        assert sid not in h.app.conversation_contexts and not h.app._game_callers
        await h.send('HandsetLifted')
        h.state = h.app.canonical_runtime.sessions[sid]
        assert h.state.mode == 'voice_welcome' and h.state.game_engine_state is None
        assert sid not in h.app.conversation_contexts and not h.app._game_callers
    finally:
        await h.close()
