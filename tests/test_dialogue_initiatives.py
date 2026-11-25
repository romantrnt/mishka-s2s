"""Unified initiatives; model/semantic/owned-playback completion are explicit fixtures."""
import asyncio
import json
from dataclasses import replace

import pytest

from app.foundation.activity_requests import (activity_request_matches, DialogueInitiative,
    InitiativeConsentReceipt, initiative_offer_matches)
from test_activity_request_gate import engine, classified


def wire(text, *, kind="riddle", phase="offer", target=None, answer=""):
    return json.dumps(dict(type="say_initiative",text=text,kind=kind,target=target or kind,
                           phase=phase,answer=answer), ensure_ascii=False)


def played(context):
    # Engine tests only: actual owned-PCM terminal belongs to APP/transport acceptance.
    context.initiative = replace(context.initiative, ready=True)
    return context.initiative.nonce


@pytest.mark.asyncio
@pytest.mark.parametrize("text,kind,reply,answer,phase", [
    ("Давай загадки", "riddle", "Зимой и летом одним цветом. Что это?", "Ёлка", "active"),
    ("Расскажи шутку", "joke", "Колобок забыл дорогу и покатился домой.", "", "complete"),
    ("Давай словесную игру", "wordplay", "Придумай смешное слово про динозавра.", "", "active"),
])
async def test_direct_inline_request_executes_without_catalogue(text, kind, reply, answer, phase):
    e,c,m,tts=engine(wire(reply,kind=kind,phase=phase,answer=answer),structured=True)
    e.activity_request_gate,requests=classified()
    try:
        result=await e.run_text_turn(text,c)
        assert result.fallback_reason is None and " ".join(result.chunks)==reply
        assert not result.actions and result.activity_request is None and result.initiative_offer is None
        assert m.calls[-1][-1]["content"]==text and tts.texts and not requests
        assert (c.initiative is not None)==(phase=="active")
        if answer:assert c.initiative.answer==answer and all(answer not in spoken for spoken in tts.texts)
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,target,question", [
    ("riddle","riddle","Динозавры были разными. Хочешь загадку о лесе?"),
    ("joke","joke","Динозавры были разными. Хочешь короткую шутку?"),
    ("wordplay","wordplay","Динозавры были разными. Хочешь словесную забаву?"),
    ("game","quiz","Динозавры были разными. Хочешь игру Вопросы Мишки?"),
])
async def test_offer_returns_one_validated_owned_question_without_unowned_pcm(kind,target,question):
    e,c,m,tts=engine(wire(question,kind=kind,target=target),structured=True)
    try:
        result=await e.run_text_turn("Мне нравятся динозавры",c)
        assert result.fallback_reason is None and not result.chunks and not result.actions
        assert not tts.texts and not e.playback.played
        offer=result.initiative_offer
        assert initiative_offer_matches(offer,c) and not offer.ready
        assert (offer.kind,offer.target,offer.text)==(kind,target,question)
        assert all(not assistant for _,assistant in c.history)  # No unheard question enters heard history.
        assert c.turns==0 and not e.active_queues and not e.active_tasks
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("button",[False,True])
async def test_current_played_inline_offer_executes_once_without_second_promise(button):
    e,c,m,tts=engine(wire("Хочешь короткую загадку?"),
        wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка"),structured=True)
    try:
        await e.run_text_turn("Мне нравится лес",c);nonce=played(c)
        result=await e.run_text_turn("" if button else "Давай",c,
            initiative_button_nonce=nonce if button else None,initiative_input_scope=None if button else nonce)
        assert result.fallback_reason is None and result.chunks and not result.actions
        assert "Что это?" in " ".join(result.chunks) and c.initiative.phase=="active"
        assert len(m.calls)==2 and m.calls[-1][-1]["content"]==("" if button else "Давай")
        assert not result.initiative_offer and tts.texts and c.initiative.nonce!=nonce
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,scope,text",[(False,"current","Да"),(True,"old","Да"),(True,None,"Да"),
    (True,"current","Мама сказала да"),(True,"current","Не хочу")])
async def test_unplayed_stale_foreign_or_declined_agreement_never_executes(ready,scope,text):
    e,c,m,tts=engine(wire("Хочешь короткую загадку?"),
        wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка"),structured=True)
    try:
        await e.run_text_turn("Мне нравится лес",c);nonce=c.initiative.nonce
        if ready:played(c)
        result=await e.run_text_turn(text,c,initiative_input_scope=nonce if scope=="current" else scope)
        assert not result.chunks and not result.actions and not tts.texts
        if text=="Да":
            assert result.fallback_reason=="initiative_not_current" and c.initiative is not None and len(m.calls)==1
        else:
            assert result.fallback_reason=="provider_or_validation_failure" and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,target,expected",[("game","quiz","offer_game"),("game","games","open_catalog"),
    ("story","fixed_aibolit_v1","play_story"),("story","stories","open_catalog")])
@pytest.mark.parametrize("button",[False,True])
async def test_supported_owned_consent_uses_new_exact_receipt_without_classifier_or_second_offer(kind,target,expected,button):
    e,c,m,tts=engine(wire("Хочешь выбрать занятие?",kind=kind,target=target),structured=True)
    c.verified_story_ids=("fixed_aibolit_v1",)
    e.activity_request_gate,requests=classified()
    try:
        await e.run_text_turn("Мне нравится лес",c);nonce=played(c)
        text="" if button else "Да"
        result=await e.run_text_turn(text,c,initiative_button_nonce=nonce if button else None,
                                   initiative_input_scope=None if button else nonce)
        assert len(m.calls)==1 and not requests and not tts.texts and not result.chunks
        assert [a.value for a in result.actions]==[expected]
        receipt=result.activity_request
        assert isinstance(receipt,InitiativeConsentReceipt) and receipt.source==("button" if button else "voice")
        assert activity_request_matches(receipt,text,result.actions[0],c) and c.initiative is None
        assert not activity_request_matches(receipt,"Не хочу",result.actions[0],c)
        assert not activity_request_matches(receipt,text,result.actions[0],object())
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_replacement_and_unrelated_turn_expire_previous_proposal():
    e,c,m,tts=engine(wire("Хочешь загадку?"),wire("Хочешь шутку?",kind="joke"),
        json.dumps({"type":"say","text":"Динозавры были разными."}),structured=True)
    try:
        await e.run_text_turn("Мне нравится лес",c);old=played(c)
        await e.run_text_turn("А ещё мне нравится море",c);new=played(c)
        assert old!=new and c.initiative.kind=="joke"
        await e.run_text_turn("Мне нравятся динозавры",c,initiative_input_scope=new)
        assert c.initiative is None and tts.texts==["Динозавры были разными."]
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_current_riddle_hint_and_answer_use_same_private_content():
    e,c,m,tts=engine(wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка"),
        wire("Это дерево с иголками.",phase="active",answer="Ёлка"),
        wire("Это ёлка.",phase="complete"),structured=True)
    try:
        await e.run_text_turn("Давай загадки",c);nonce=c.initiative.nonce
        await e.run_text_turn("Подскажи",c,initiative_input_scope=nonce)
        prompt=m.calls[-1][0]["content"]
        assert "Зимой и летом" in prompt and "Ёлка" in prompt
        nonce=c.initiative.nonce
        result=await e.run_text_turn("Скажи ответ",c,initiative_input_scope=nonce)
        assert result.chunks==("Это ёлка.",) and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending",["cancel","failure","invalid_tail"])
async def test_partial_initiative_never_creates_ready_state_or_pcm(monkeypatch,ending):
    monkeypatch.setenv("MISHKA_STREAM_FIRST_JSON_CHUNK","1")
    e,c,m,tts=engine(structured=True);entered=asyncio.Event();release=asyncio.Event()
    async def split(*_args,**_kwargs):
        yield '{"type":"say_initiative","text":"Хочешь загадку?'
        entered.set();await release.wait()
        if ending=="failure":raise RuntimeError("synthetic interrupted model")
        yield '","kind":"riddle","target":"riddle","phase":"offer","answer":""} []'
    m.stream_dialogue=split
    task=asyncio.create_task(e.run_text_turn("Мне нравится лес",c))
    try:
        await entered.wait();await asyncio.sleep(.02)
        assert c.initiative is None and not tts.texts and not e.playback.played
        if ending=="cancel":task.cancel()
        release.set();await asyncio.gather(task,return_exceptions=True)
        assert c.initiative is None and not tts.texts and not e.playback.played
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_detached_context_revokes_live_offer_before_provider_await():
    import copy
    e,c,m,tts=engine(wire("Хочешь загадку?"),structured=True)
    entered=asyncio.Event();release=asyncio.Event()
    try:
        await e.run_text_turn("Мне нравится лес",c);nonce=played(c);detached=copy.deepcopy(c)
        calls=[]
        def reserve(value):
            calls.append(value)
            if c.initiative and c.initiative.nonce==value:c.initiative=None
        detached._initiative_reserve=reserve
        async def held(*_args,**_kwargs):
            entered.set();await release.wait()
            yield wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка")
        m.stream_dialogue=held
        task=asyncio.create_task(e.run_text_turn("Да",detached,initiative_input_scope=nonce))
        await entered.wait()
        assert calls==[nonce] and c.initiative is None and detached.initiative is None
        release.set();result=await task
        assert result.chunks and detached.initiative.phase=="active"
    finally:
        release.set();await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply",[wire("Хочешь загадку?"),
    wire("Это дерево с иголками.",phase="active",answer="Берёза")])
async def test_no_second_promise_or_changed_private_riddle_answer(reply):
    e,c,m,tts=engine(wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка"),reply,structured=True)
    try:
        await e.run_text_turn("Давай загадки",c);nonce=c.initiative.nonce;before=tuple(tts.texts)
        result=await e.run_text_turn("Подскажи",c,initiative_input_scope=nonce)
        assert result.fallback_reason=="provider_or_validation_failure" and tuple(tts.texts)==before
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_stop_current_inline_activity_continues_normal_conversation():
    e,c,m,tts=engine(wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка"),
        json.dumps({"type":"say","text":"Хорошо, о чём поговорим?"}),structured=True)
    try:
        await e.run_text_turn("Давай загадки",c);nonce=c.initiative.nonce
        from app.foundation.activity_requests import initiative_continuation_requested
        assert initiative_continuation_requested("Стоп",c)
        result=await e.run_text_turn("Стоп",c,initiative_input_scope=nonce)
        assert result.chunks and not result.actions and not result.return_to_scenario and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_trusted_initiative_scope_reaches_same_mandatory_validator_before_offer_pcm():
    from app.foundation.validation import ValidationDecision
    seen=[]
    class SemanticFixture:
        async def validate(self,text,context=(),*,spoken_requirement=None):
            instruction=spoken_requirement.instruction();seen.append((text,instruction))
            return ValidationDecision("block",False,"fixture_mismatched_topic_and_kind",1.,"fixture")
    e,c,m,tts=engine(wire("Хочешь шутку?",kind="riddle"),structured=True)
    e.response_validator.mini=SemanticFixture()
    try:
        result=await e.run_text_turn("Мне нравятся динозавры",c)
        assert result.fallback_reason=="provider_or_validation_failure" and not tts.texts and c.initiative is None
        assert len(seen)==1 and "Мне нравятся динозавры" in seen[0][1] and '"kind": "riddle"' in seen[0][1]
        assert "funny fact" in seen[0][1] and "quality" in seen[0][1]
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply",[wire("Хочешь загадку?",kind="game",target="riddles"),
    '<say>Хочешь загадку?</say><action type="dialogue_initiative" kind="riddle" target="riddle" phase="offer" answer=""/><action type="offer_game" game_id="quiz"/>'])
async def test_unavailable_or_mixed_proposal_never_reaches_tts(reply):
    e,c,m,tts=engine(reply,structured=reply.startswith('{'))
    try:
        result=await e.run_text_turn("Мне нравится лес",c)
        assert result.fallback_reason=="provider_or_validation_failure" and not tts.texts and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_actual_word_game_request_remains_real_game_dispatch():
    e,c,m,tts=engine(structured=True);e.activity_request_gate,requests=classified()
    try:
        result=await e.run_text_turn("Поиграем в слова",c)
        assert [(a.value,a.params) for a in result.actions]==[("offer_game",{"game_id":"word_game"})]
        assert requests and not m.calls and not tts.texts and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.parametrize("text,expected",[("Загадай загадку про динозавров","riddle"),
    ("Не хочу печенье, загадай загадку","riddle"),("Расскажи про загадки",None),
    ("Мама сказала загадай загадку",None),("Мама сказала «загадай загадку»",None),
    ("Мне нравятся динозавры",None),("Хочу играть в слова",None)])
def test_inline_request_is_not_a_mention_quote_or_the_actual_word_game(text,expected):
    from app.foundation.activity_requests import inline_request
    assert inline_request(text)==expected


@pytest.mark.asyncio
@pytest.mark.parametrize("text,reply",[("Мне нравятся динозавры","Динозавры были разными."),
    ("Нет, спасибо","Хорошо, можем поговорить о лесе.")])
async def test_context_reply_and_refusal_can_continue_without_any_proposal(text,reply):
    e,c,m,tts=engine(json.dumps({"type":"say","text":reply}),structured=True)
    try:
        result=await e.run_text_turn(text,c)
        assert result.chunks==(reply,) and not result.actions and result.initiative_offer is None
        assert c.initiative is None and len(m.calls)==1 and tts.texts==[reply]
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,reply",[("Нет, спасибо","Хочешь шутку?"),
    ("Мне нравится лес","Хочешь шутку? Любишь динозавров?")])
async def test_decline_or_competing_question_cannot_arm_new_proposal(text,reply):
    e,c,m,tts=engine(wire(reply,kind="joke"),structured=True)
    try:
        result=await e.run_text_turn(text,c)
        assert result.fallback_reason=="provider_or_validation_failure" and not tts.texts and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_empty_verified_story_catalogue_cannot_prepare_owned_offer():
    e,c,m,tts=engine(wire("Хочешь выбрать сказку?",kind="story",target="stories"),structured=True)
    try:
        result=await e.run_text_turn("Мне нравится лес",c)
        assert result.fallback_reason=="provider_or_validation_failure"
        assert result.initiative_offer is None and c.initiative is None and not tts.texts and not e.playback.played
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_removed_verified_story_catalogue_revokes_ready_consent():
    e,c,m,tts=engine(wire("Хочешь выбрать сказку?",kind="story",target="stories"),
        json.dumps({"type":"say","text":"Можем продолжить разговор."}),structured=True)
    c.verified_story_ids=("fixed_aibolit_v1",)
    try:
        await e.run_text_turn("Мне нравится лес",c);nonce=played(c)
        c.verified_story_ids=()
        result=await e.run_text_turn("Да",c,initiative_input_scope=nonce)
        assert not result.actions and result.activity_request is None and c.initiative is None
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
async def test_polite_current_riddle_request_executes_directly():
    e,c,m,tts=engine(wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка"),structured=True)
    try:
        result=await e.run_text_turn("Можешь загадать загадку?",c)
        assert result.fallback_reason is None and result.chunks and c.initiative.phase=="active"
        assert not result.actions and len(m.calls)==1 and tts.texts
    finally:await e.cancel_session(c.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("current",[True,False])
async def test_named_not_opposed_agreement_requires_current_played_offer(current):
    answers=([wire("Хочешь загадку?")] if current else [])+[wire("Зимой и летом одним цветом. Что это?",phase="active",answer="Ёлка")]
    e,c,m,tts=engine(*answers,structured=True)
    try:
        nonce=None
        if current:
            await e.run_text_turn("Мне нравится лес",c);nonce=played(c)
        result=await e.run_text_turn("Я не против загадки",c,initiative_input_scope=nonce)
        assert bool(result.chunks)==current and bool(tts.texts)==current
        assert (result.fallback_reason is None)==current and not result.actions
    finally:await e.cancel_session(c.session_id)


@pytest.mark.parametrize("text",["Что такое загадка?","Мама сказала можешь загадать загадку", "Можешь не загадывать загадку?"])
def test_polite_head_keeps_discussion_report_and_negation_nonexecuting(text):
    from app.foundation.activity_requests import inline_request
    assert inline_request(text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("active",[False,True])
async def test_legacy_bear_speech_keeps_same_mandatory_initiative_requirement(active):
    from app.foundation.validation import ValidationDecision
    reply=('<say>Зимой и летом одним цветом. Что это?</say><action type="dialogue_initiative" kind="riddle" target="riddle" phase="active" answer="Ёлка"/>' if active else '<say>Динозавры были разными.</say>')
    e,c,m,tts=engine(reply);seen=[]
    class Recorder:
        timeout_s=.2
        async def validate(self,text,context=(),generation_id=None,*,spoken_requirement=None):
            seen.append(spoken_requirement)
            return ValidationDecision("allow",True,"fixture",1.,"fixture")
    e.response_validator=Recorder()
    try:
        result=await e.run_text_turn("Давай загадки" if active else "Мне нравятся динозавры",c)
        assert result.chunks and seen and all(requirement is not None for requirement in seen)
        assert all("Trusted initiative contract" in requirement.instruction() for requirement in seen)
    finally:await e.cancel_session(c.session_id)
