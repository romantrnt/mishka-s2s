"""Request-local production wire contract; all provider I/O is in memory."""
import asyncio
import json
import re
from dataclasses import FrozenInstanceError

import httpx
import pytest

from app.foundation.s2s import (BoundedConversationEngine, ConversationContext,
    DialogueResponseContract, MockPlaybackController, MockTTSProvider, OllamaLLMProvider, ProfileContext,
    RetryingLLMProvider, RVCProvider, S2SConfig)
from app.foundation.menu_content import STORY_TITLES
from app.foundation.validation import (AsyncResponseValidator, OllamaMiniLLMValidator,
    SpokenResponseRequirement)

_HTTP_CLIENT = httpx.AsyncClient


def context(mode="bear_dialogue", band="1-3", sid="json-dialogue"):
    return ConversationContext(sid, "canonical_product", mode, ("__all__",), (), 8, 120, 2,
                               profile=ProfileContext(age_band=band))


def wire(monkeypatch, content, *, terminal="stop"):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        response_content = content(requests[-1]) if callable(content) else content
        values = [{"message":{"content":part}} for part in
                  (response_content if isinstance(response_content, list) else [response_content])]
        if terminal is not None:
            values.append({"done":True,"done_reason":terminal})
        return httpx.Response(200, content="\n".join(map(json.dumps, values)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        _HTTP_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    return requests


def engine():
    provider = OllamaLLMProvider("http://synthetic.invalid", "synthetic", 1, max_tokens=96)
    return BoundedConversationEngine(RetryingLLMProvider(provider, delay_s=0), MockTTSProvider(),
        RVCProvider(), MockPlaybackController(), config=S2SConfig(), profile="mock")


@pytest.mark.asyncio
async def test_actual_ollama_json_is_closed_before_speech(monkeypatch):
    requests = wire(monkeypatch, '{"type":"say","text":"Я живу в добром лесу."}')
    e = engine()
    result = await e.run_text_turn("Где ты живёшь?", context())
    assert result.chunks == ("Я живу в добром лесу.",)
    assert not result.fallback_reason
    assert requests[0]["format"]["oneOf"]
    assert requests[0]["options"]["num_predict"] == 96


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    '{"type":"say","type":"say","text":"Привет."}',
    '{"type":"say","text":"Привет.","game_id":"quiz"}',
    '{"type":"offer_game","game_id":"quiz","text":"Привет."}',
    '{"type":"offer_game","game_id":"delete_files"}',
    '{"type":"play_story","story_id":"fixed_aibolit_v1"}',  # No trusted snapshot.
    '{"type":"request_information","kind":"raw_sql"}',
    '{"type":"say_return","text":"Привет.","target":"unsafe"}',
    '{"type":"say_led","text":"Привет.","pattern":"blink"}',
    '{"type":"say","text":true}',
    '{"type":"say","text":NaN}',
    '{"type":"say","text":"<say>Привет.</say>"}',
    '{"type":"say","text":"Привет. Пока."}',
    '{"type":"say","text":"Привет  друг."}',
    '{"type":"say","text":"Привет\\nдруг."}',
    '{"type":"say","text":"Привет."} extra',
    '{"type":"say","text":"Привет."} {"type":"open_catalog","catalog":"games"}',
    '{"type":"say","text":"Привет.', '[]', '<say>Привет.</say>',
])
async def test_bad_json_never_reaches_semantics_actions_or_pcm(monkeypatch, content):
    requests = wire(monkeypatch, content)
    e = engine()
    result = await e.run_text_turn("Привет", context())
    assert result.fallback_reason == "provider_or_validation_failure"
    assert not result.actions and not result.chunks and not e.playback.played
    assert e.response_validator.metrics.mini_calls == 0
    assert len(requests) == 1
    assert not e.active_tasks and not e.active_queues


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None,"length","error",""])
async def test_complete_object_without_clean_stop_is_not_a_response(monkeypatch, terminal):
    requests = wire(monkeypatch, '{"type":"offer_game","game_id":"quiz"}', terminal=terminal)
    e = engine()
    result = await e.run_text_turn("Хочу играть", context())
    assert result.fallback_reason and not result.actions and not e.playback.played
    assert len(requests) == 1  # A format/terminal error is never repaired/retried.
    assert not any(item["event"] == "json_validated" for item in e.metrics.events)


@pytest.mark.parametrize("band,words", [("1-3",8),("3-5",18),("4-5",18),("5-7",30),("6-7",30)])
def test_age_aliases_enforce_n_and_n_plus_one_without_repair(band, words):
    contract = DialogueResponseContract.from_context(context(band=band))
    text = " ".join(["Слово"]*words)+"."
    assert contract.to_xml(json.dumps({"type":"say","text":text}, ensure_ascii=False)) == '<say>'+text+'</say>'
    for bad in (text[:-1]+" ещё.", text.replace(" ","\t",1), text.replace(" ","  ",1)):
        with pytest.raises(ValueError):
            contract.to_xml(json.dumps({"type":"say","text":bad}, ensure_ascii=False))


CATALOGUE_ACTIONS = ([{"type":"offer_game","game_id":game} for game in
    ("free_lights","quiz","repeat_pattern","odd_one_out","word_game")]
    + [{"type":"play_story","story_id":story} for story in STORY_TITLES]
    + [{"type":"open_catalog","catalog":catalog} for catalog in ("games","stories")]
    + [{"type":"request_information","kind":kind} for kind in
       ("date","weather","what_next","where_now","card_doctors")])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", CATALOGUE_ACTIONS)
async def test_all_catalogue_actions_roundtrip_production_wire_and_unchanged_parser(monkeypatch, action):
    wire(monkeypatch, json.dumps(action))
    e, ctx = engine(), context()
    ctx.verified_story_ids = tuple(STORY_TITLES)
    result = await e.run_text_turn("Хочу выбрать занятие", ctx)
    assert not result.fallback_reason and len(result.actions) == 1
    assert result.actions[0].value == action["type"]
    assert result.actions[0].params == {k:v for k,v in action.items() if k != "type"}
    assert not result.chunks and not e.playback.played and ctx.turns == 0


@pytest.mark.parametrize("mode", ["support_intake","game_active"])
def test_request_schema_removes_every_catalogue_branch_outside_bear(mode):
    ctx = context(mode)
    ctx.verified_story_ids = tuple(STORY_TITLES)
    contract = DialogueResponseContract.from_context(ctx)
    assert {branch["properties"]["type"]["enum"][0] for branch in contract.schema()["oneOf"]} == {"say","say_led","say_return"}
    for action in CATALOGUE_ACTIONS:
        with pytest.raises(ValueError):
            contract.to_xml(json.dumps(action))


def test_request_scope_is_frozen_and_schema_never_leaks_between_ages_or_catalogues():
    ctx = context()
    ctx.verified_story_ids = ("fixed_aibolit_v1",)
    original = DialogueResponseContract.from_context(ctx)
    ctx.profile = ProfileContext(age_band="6-7")
    ctx.verified_story_ids = ("fixed_neznayka_v1",)
    fresh = DialogueResponseContract.from_context(ctx)
    changed = original.schema()
    changed["oneOf"].clear()
    assert original.max_words == 8 and fresh.max_words == 30 and original.schema()["oneOf"]
    assert original.story_ids == ("fixed_aibolit_v1",) and fresh.story_ids == ("fixed_neznayka_v1",)
    with pytest.raises(FrozenInstanceError):
        original.max_words = 99
    with pytest.raises(ValueError):
        original.to_xml('{"type":"play_story","story_id":"fixed_neznayka_v1"}')


def semantic(e, *, verdict="allow", confidence=.99, before_reply=None, quality="valid"):
    requests = []
    mini = OllamaMiniLLMValidator()
    async def request(raw):
        payload=json.loads(raw)
        requests.append(payload)
        if before_reply:
            await before_reply()
        reply={"decision":verdict,"confidence":confidence}
        if "quality" in payload["format"]["properties"]:
            reply={"quality":quality,**reply}
        return json.dumps({"message":{"content":json.dumps(reply)}}).encode()
    mini._request = request
    e.response_validator = AsyncResponseValidator(mini)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("text,verdict,confidence,allowed", [
    ("Я живу в добром лесу.","allow",.99,True),
    ("Я живу в добром волшебном лесу рядом срекой.","block",.99,False),
    ("Лиса, теперь ты.","allow",.99,True),
    ("Зайчик-побегайчик вернулся домой.","allow",.99,True),
    ("Мишка дружит с Алёной.","allow",.99,True),
    ("Я живу в добром лесу.","allow",.74,False),
    ("Я живу в добром лесу.","review",.99,False),
    ("Я живу в добром лесу.","allow",.75,True),
])
async def test_same_mandatory_semantic_request_receives_quality_and_full_safety(monkeypatch, text, verdict, confidence, allowed):
    wire(monkeypatch, json.dumps({"type":"say","text":text}, ensure_ascii=False))
    e = engine()
    requests = semantic(e, verdict=verdict, confidence=confidence)
    result = await e.run_text_turn("Расскажи", context())
    assert bool(result.chunks) is allowed and bool(e.playback.played) is allowed
    assert len(requests) == 1  # One combined request, never a repair/second model pass.
    policy = requests[0]["messages"][0]["content"]
    assert "normal separate words" in policy and "obvious invented word gluing" in policy
    assert "proper names" in policy and "accepted hyphenation" in policy
    assert "Block violence, sexual content, self-harm" in policy
    assert "real private data" in policy and "called, will call" in policy
    assert "the total word limit is 8" in policy.lower()
    assert "срекой" not in policy  # No literal malformed-word denylist.
    assert json.loads(requests[0]["messages"][1]["content"])["candidate"] == text


@pytest.mark.asyncio
async def test_quality_scope_is_not_added_to_trusted_application_or_unscoped_semantic():
    e = engine()
    requests = semantic(e)
    assert (await e.response_validator.validate_trusted("Выбери кнопку.")).allowed
    assert not requests
    assert (await e.response_validator.validate("Я живу в телефоне.")).allowed
    assert "spoken-response quality requirement" not in requests[-1]["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [{"type":"say_led","text":"Я слушаю тебя.","pattern":"calm"},
    {"type":"say_return","text":"Теперь выбери занятие.","target":"root_menu"}])
async def test_speech_with_existing_led_or_return_is_still_supported(monkeypatch, action):
    wire(monkeypatch, json.dumps(action, ensure_ascii=False))
    e = engine()
    result = await e.run_text_turn("Привет", context())
    assert result.chunks == (action["text"],) and e.playback.played
    assert len(result.actions) == 1
    assert result.actions[0].value == ("led" if action["type"] == "say_led" else "return_to_scenario")


@pytest.mark.asyncio
@pytest.mark.parametrize("band,limit", [("1-3",8),("4-5",18),("6-7",30)])
@pytest.mark.parametrize("extra", [0,1])
async def test_actual_json_wire_applies_age_limit_before_semantics_and_pcm(monkeypatch, band, limit, extra):
    text = " ".join(["Слово"]*(limit+extra))+"."
    requests = wire(monkeypatch,json.dumps({"type":"say","text":text},ensure_ascii=False))
    e = engine()
    result = await e.run_text_turn("Привет",context(band=band))
    assert bool(result.fallback_reason) is bool(extra)
    assert bool(e.playback.played) is not bool(extra)
    assert e.response_validator.metrics.mini_calls == (0 if extra else 1)
    pattern = requests[0]["format"]["oneOf"][0]["properties"]["text"]["pattern"]
    assert "{0,"+str(limit-1)+"}" in pattern


@pytest.mark.asyncio
async def test_parallel_requests_keep_separate_wire_mode_age_and_catalogue(monkeypatch):
    requests = wire(monkeypatch,'{"type":"say","text":"Я слушаю тебя."}')
    shared = engine().llm
    first,second = engine(),engine()
    first.llm = second.llm = shared
    young,older = context(sid="young"),context("support_intake","6-7",sid="older")
    young.verified_story_ids = ("fixed_aibolit_v1",)
    results = await asyncio.gather(first.run_text_turn("Привет",young),second.run_text_turn("Привет",older))
    assert all(result.chunks == ("Я слушаю тебя.",) for result in results)
    schemas = [request["format"] for request in requests]
    assert len(schemas) == 2
    bear = next(schema for schema in schemas if len(schema["oneOf"]) > 3)
    other = next(schema for schema in schemas if len(schema["oneOf"]) == 3)
    assert '{0,7}' in bear["oneOf"][0]["properties"]["text"]["pattern"]
    assert '{0,29}' in other["oneOf"][0]["properties"]["text"]["pattern"]
    assert 'fixed_aibolit_v1' in json.dumps(bear) and 'fixed_aibolit_v1' not in json.dumps(other)


@pytest.mark.asyncio
async def test_quality_semantic_timeout_still_fails_closed_without_second_call(monkeypatch):
    wire(monkeypatch,'{"type":"say","text":"Я живу в лесу."}')
    e = engine()
    async def stalled():
        await asyncio.Event().wait()
    requests = semantic(e,before_reply=stalled)
    e.response_validator.timeout_s = .01
    result = await e.run_text_turn("Привет",context())
    assert result.fallback_reason and not e.playback.played and len(requests) == 1
    assert any(item.get("reason") == "mini_llm_timeout" for item in e.metrics.events)


@pytest.mark.asyncio
async def test_new_quality_policy_appends_to_complete_existing_safety_policy():
    e = engine()
    requests = semantic(e)
    await e.response_validator.validate("Я живу в лесу.")
    await e.response_validator.validate("Я живу в лесу.",
        spoken_requirement=SpokenResponseRequirement("1-3","bear_dialogue"))
    assert len(requests) == 2
    base, scoped = [request["messages"][0]["content"] for request in requests]
    assert scoped.startswith(base) and len(scoped) > len(base)


@pytest.mark.asyncio
async def test_raw_size_bound_counts_original_chunks_before_bridge_parsing(monkeypatch):
    requests = wire(monkeypatch,[" "*700," "*700])
    e = engine()
    result = await e.run_text_turn("Привет",context())
    assert result.fallback_reason and len(requests) == 1 and not e.playback.played
    assert len([item for item in e.metrics.events if item["event"] == "token_received"]) == 2
    assert not e.response_validator.metrics.mini_calls


@pytest.mark.asyncio
async def test_completed_json_budget_does_not_charge_semantics_or_audio(monkeypatch):
    from dataclasses import replace
    wire(monkeypatch,'{"type":"say","text":"Я слушаю тебя."}')
    e = engine()
    e.config = replace(e.config,llm_timeout_s=.02)
    async def slow():
        await asyncio.sleep(.04)
    requests = semantic(e,before_reply=slow)
    result = await e.run_text_turn("Привет",context())
    assert result.chunks == ("Я слушаю тебя.",) and e.playback.played and len(requests) == 1


@pytest.mark.parametrize("band", ["3-5","4-5","5-7","6-7"])
def test_older_schema_rejects_three_sentences_already_rejected_by_bridge(band):
    contract = DialogueResponseContract.from_context(context(band=band))
    text = "Привет. Я Мишка. Я слушаю."
    assert len(text.split()) < contract.max_words
    with pytest.raises(ValueError):
        contract.to_xml(json.dumps({"type":"say","text":text},ensure_ascii=False))
    assert re.fullmatch(contract.text_pattern,text) is None


@pytest.mark.parametrize("band,limit", [("3-5",18),("4-5",18),("5-7",30),("6-7",30)])
@pytest.mark.parametrize("kind,extra", [("say",{}),("say_led",{"pattern":"calm"}),
    ("say_return",{"target":"root_menu"})])
def test_older_one_or_two_sentences_share_global_word_cap_in_every_speech_branch(band,limit,kind,extra):
    contract=DialogueResponseContract.from_context(context(band=band))
    schema=next(branch for branch in contract.schema()['oneOf'] if branch['properties']['type']['enum']==[kind])
    pattern=schema['properties']['text']['pattern']
    one=" ".join(["Слово"]*limit)+"."
    assert re.fullmatch(pattern,one)
    assert contract.to_xml(json.dumps({"type":kind,"text":one,**extra},ensure_ascii=False)).startswith('<say>'+one+'</say>')
    assert re.fullmatch(pattern,one[:-1]+" ещё.") is None
    # Every split of the SAME total budget is legal, including 1 + N-1.
    for first in range(1,limit):
        text=" ".join(["Слово"]*first)+"! "+" ".join(["Слово"]*(limit-first))+"?"
        assert re.fullmatch(pattern,text)
        assert contract.to_xml(json.dumps({"type":kind,"text":text,**extra},ensure_ascii=False)).startswith('<say>'+text+'</say>')
        overflow=text[:-1]+" ещё?"
        assert re.fullmatch(pattern,overflow) is None
        with pytest.raises(ValueError):
            contract.to_xml(json.dumps({"type":kind,"text":overflow,**extra},ensure_ascii=False))
    assert re.fullmatch(pattern,"Привет. Я Мишка. Я слушаю.") is None


@pytest.mark.parametrize("band", ["3-5","4-5","5-7","6-7"])
def test_older_sentence_grammar_preserves_normal_words_and_internal_punctuation(band):
    pattern=DialogueResponseContract.from_context(context(band=band)).text_pattern
    for text in ("Зайчик-побегайчик вернулся домой.","Алёна, Мишка; зайчик: друзья!",
                 "Алёна, Мишка; зайчик: друзья! Лиса-плутовка пришла?", "Привет! Пока?"):
        assert re.fullmatch(pattern,text)
    for text in ("Привет. Пока. Ещё?", "Привет! Пока? Ещё.", "Привет? Пока! Ещё!",
                 "Привет.. Пока.", "Привет.  Пока.", "Привет.\nПока."):
        assert re.fullmatch(pattern,text) is None


def test_young_sentence_pattern_is_byte_identical_and_still_single_sentence():
    pattern=DialogueResponseContract.from_context(context()).text_pattern
    assert pattern==r"^[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*(?:[,;:]? [А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*){0,7}[.!?]$"
    assert re.fullmatch(pattern,"Я живу в добром лесу рядом с рекой.")
    assert re.fullmatch(pattern,"Я живу в добром волшебном лесу рядом с рекой.") is None
    assert re.fullmatch(pattern,"Привет. Пока.") is None


@pytest.mark.asyncio
async def test_actual_older_wire_validates_two_sentences_but_rejects_three_before_semantics(monkeypatch):
    answers=["Я слушаю тебя. Давай поговорим.","Привет. Я Мишка. Я слушаю."]
    requests=wire(monkeypatch,lambda _:json.dumps({'type':'say','text':answers.pop(0)},ensure_ascii=False))
    e=engine()
    semantic_requests=semantic(e)
    ctx=context(band='4-5')
    first=await e.run_text_turn('Привет',ctx)
    assert first.chunks and len(semantic_requests)==1
    second=await e.run_text_turn('Ещё',ctx)
    assert second.fallback_reason and not second.chunks and not second.actions
    assert len(semantic_requests)==1 and len(requests)==2
    assert not e.playback.played


@pytest.mark.asyncio
async def test_production_shaped_quality_request_judges_literal_word_boundaries_without_repair():
    """Request composition only; the controlled verdict does not prove model quality."""
    requests=[]
    mini=OllamaMiniLLMValidator(model='qwen3:14b',num_ctx=16384,keep_alive='-1')
    mini._story_runner_pinned=True  # Same already-pinned launch option as the observed production request.
    async def request(raw):
        requests.append(json.loads(raw))
        return b'{"message":{"content":"{\\"quality\\":\\"invalid\\",\\"decision\\":\\"block\\",\\"confidence\\":1.0}"}}'
    mini._request=request
    validator=AsyncResponseValidator(mini)
    text='Я живу в добром волшебном лесу рядом срекой.'
    decision=await validator.validate(text,spoken_requirement=SpokenResponseRequirement('1-3','bear_dialogue'))
    assert decision.blocked and len(requests)==1
    payload=requests[0]
    assert payload['model']=='qwen3:14b' and payload['options']=={'temperature':0,'num_predict':96,'num_ctx':16384}
    assert payload['stream'] is False and payload['think'] is False and payload['shift'] is False
    assert payload['format']==mini.SPOKEN_DECISION_SCHEMA and payload['keep_alive']==-1
    assert json.loads(payload['messages'][1]['content'])=={'candidate':text,'context':'()'}
    policy=payload['messages'][0]['content']
    assert 'Оцени буквальный текст candidate' in policy
    assert 'Безопасный смысл сам по себе недостаточен для allow' in policy
    assert 'не исправляй его мысленно' in policy
    assert 'предлог' in policy and 'местоимени' in policy
    assert 'срекой' not in policy and 'стобой' not in policy


@pytest.mark.asyncio
async def test_new_quality_rubric_stays_out_of_trusted_unscoped_and_full_story_requests():
    from test_story_backend_context import contract, URL, MODEL, response
    requests=[]
    mini=OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=await contract())
    async def request(raw):
        payload=json.loads(raw)
        requests.append(payload)
        if 'quality' in payload['format']['properties']:
            return b'{"message":{"content":"{\\"quality\\":\\"valid\\",\\"decision\\":\\"allow\\",\\"confidence\\":0.99}"}}'
        return response()
    mini._request=request
    validator=AsyncResponseValidator(mini)
    assert (await validator.validate_trusted('Выбери кнопку.')).allowed and not requests
    assert (await validator.validate('Я живу в лесу.')).allowed
    assert (await validator.validate_story('Лиса вернулась домой. '*30)).allowed
    base_policy=requests[0]['messages'][0]['content']
    assert requests[1]['messages'][0]['content']==base_policy
    assert 'проверка русской речи' not in base_policy
    assert (await validator.validate('Лиса, теперь ты.',spoken_requirement=
        SpokenResponseRequirement('1-3','game_active'))).allowed
    assert requests[2]['messages'][0]['content'].startswith(base_policy)
    assert 'Оцени буквальный текст candidate' in requests[2]['messages'][0]['content']
    assert 'valid short word-game answer' in requests[2]['messages'][0]['content']
    assert len(requests)==3
