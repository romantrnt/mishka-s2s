"""Authored hint rules only; integration needs the app and S2S owner hooks."""
import re
from dataclasses import replace
import pytest
from app.foundation.word_game import ANIMALS, FOOD, WORDS, WordState, noun, play, spelling
from app.foundation.word_game_hints import example_for, hint_for, local_hint_request

@pytest.mark.parametrize('text',['Помоги.','Помоги мне, пожалуйста!','Мишка, подскажи','Дай подсказку','Не знаю слово'])
def test_exact_local_request(text):
    assert local_hint_request(text)

@pytest.mark.parametrize('text',['Помоги позвать взрослого','Помоги, мне больно','Меня бьют, помоги','Мама сказала: помоги','Не знаю, мне страшно'])
def test_adult_or_safety_request_is_not_local_hint(text):
    assert not local_hint_request(text)

@pytest.mark.parametrize('band',['1-3','3-5','4-5','5-7','6-7'])
def test_help_escalates_without_playing_any_word_or_consuming_final_turn(band):
    state=WordState(('кот',),'л')
    first=play('Помоги',state,band,final=True)
    second=play('Помоги',first.state,band,final=True,hint_level=first.hint_level)
    assert not first.accepted and not second.accepted
    assert first.state is state and second.state is state
    assert first.hint_level==1 and second.hint_level==2
    assert 'Например,' in second.text
    word=second.text.split('Например, ',1)[1].split('.',1)[0]
    lemma,error=noun(word,band)
    assert not error and spelling(lemma) not in state.used
    assert (not band in {'5-7','6-7'}) or spelling(lemma).startswith('л')
    if band in {'1-3','3-5','4-5'}:
        assert 'букв' not in first.text and not re.search(r'на [А-ЯЁ]\b',first.text)

@pytest.mark.parametrize('band',['3-5','4-5'])
def test_middle_animals_example_has_no_hidden_letter_rule(band):
    state=WordState(('кот',),'ъ')
    example=example_for(state,band)
    assert example and spelling(example) in {spelling(w) for w in ANIMALS}
    assert example[0]!='ъ'
    assert 'букв' not in hint_for(state,band)[0]

def test_repeated_wrong_word_gives_valid_required_letter_example_and_valid_move_resets():
    state=WordState(('кот','тигр'),'р')
    first=play('слон',state,'6-7')
    second=play('слон',state,'6-7',hint_level=first.hint_level)
    assert first.state==second.state==state and not first.accepted and not second.accepted
    assert 'букву Р' in first.text and second.hint_level==2
    word=example_for(state,'6-7')
    accepted=play(word,state,'6-7',hint_level=second.hint_level)
    assert accepted.accepted and accepted.hint_level==0 and spelling(word) in accepted.state.used

def test_offered_example_can_become_a_real_child_move():
    state=WordState();hint=play('Помоги',state,'4-5',hint_level=1)
    word=example_for(state,'4-5')
    assert not hint.accepted and not hint.state.used
    assert play(word,hint.state,'4-5',hint_level=hint.hint_level).accepted

def test_pending_proposal_hint_is_valid_without_committing_the_proposal():
    old=WordState(('кот','тигр'),'р')
    proposal=play('рыба',old,'6-7')
    state=replace(old,pending=proposal.state)
    hinted=play('Подскажи',state,'6-7',hint_level=1)
    word=example_for(state,'6-7')
    assert hinted.state is state and hinted.state.pending is proposal.state and not hinted.accepted
    assert spelling(word).startswith(proposal.state.required)
    assert play(word,state,'6-7').accepted

def test_no_unverified_or_exhausted_example_is_invented(monkeypatch):
    import app.foundation.word_game_hints as hints
    exhausted=WordState(tuple(spelling(w) for w in WORDS),'')
    assert example_for(exhausted,'6-7') is None
    text,level=hint_for(exhausted,'6-7',999)
    assert 'Например' not in text and level==2
    monkeypatch.setattr(hints,'noun',lambda *_:(None,'unavailable'))
    assert example_for(WordState(),'4-5') is None
    assert 'Например' not in hint_for(WordState(),'4-5',1)[0]

def test_young_hints_fit_the_existing_eight_word_budget():
    state=WordState(('кот',),'')
    for level in (0,1,2):
        for reason in (None,'repeat','letter'):
            assert len(hint_for(state,'1-3',level,reason=reason)[0].split())<=8
