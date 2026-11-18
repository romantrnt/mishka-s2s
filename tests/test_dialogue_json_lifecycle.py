"""JSON waiting is owned by the same real app/turn lifecycle as streamed speech."""
import asyncio
import hashlib
import json
import time
import wave
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.foundation.canonical_contract import FixedStoryManifest
from app.foundation.menu_content import STORY_TITLES
from app.foundation.s2s import MockTTSProvider, OllamaLLMProvider, RetryingLLMProvider, S2SConfig
from app.foundation.s2s import EnergyVADAdapter, LocalS2SPipeline, MockSpeechRecognizer
from app.foundation.e2e_probe import ProductionE2EWarmupProbe
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio
from test_dialogue_json_contract import context, engine, semantic, wire, _HTTP_CLIENT
from test_runtime_prompt_contract import young_guest


def install(h):
    h.app.bounded_s2s.llm = RetryingLLMProvider(
        OllamaLLMProvider("http://synthetic.invalid", "synthetic", 1, max_tokens=96), delay_s=0)
    quick_public_audio(h)


def catalogue(tmp_path):
    stories = {}
    for story_id, names in STORY_TITLES.items():
        path = tmp_path/(story_id+".wav")
        with wave.open(str(path), "wb") as output:
            output.setparams((1,2,16000,1600,"NONE","not compressed"))
            output.writeframes(b"\0\0"*1600)
        stories[story_id] = {"display_title":names[0], "audio_file":path.name,
            "audio_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "rights_status":"operator_approved", "availability":"available",
            "eligible_age_bands":["1-3","3-5","5-7"]}
    manifest = tmp_path/"manifest.json"
    manifest.write_text(json.dumps({"stories":stories}), encoding="utf-8")
    return FixedStoryManifest(manifest,tmp_path)


@pytest.mark.asyncio
async def test_actual_guest_assembled_prompt_selects_one_json_contract_and_keeps_age(monkeypatch, tmp_path):
    requests = wire(monkeypatch, '{"type":"say","text":"Я живу в добром лесу."}')
    h = Harness()
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        sid, send = await young_guest(h.app)
        await send("SpeechReceived", text="Мишка, где ты живёшь?")
        ctx = h.app.conversation_contexts[sid]
        assert (ctx.profile.age,ctx.profile.age_band) == (2,"1-3")
        prompt = requests[0]["messages"][0]["content"]
        assert "Верни ровно один завершённый JSON-объект" in prompt
        assert "Never emit JSON" not in prompt and "<say>" not in prompt
        assert "одна законченная мысль" in prompt and "at most 8 words total" in prompt
        assert ctx.verified_story_ids == tuple(STORY_TITLES)
        assert "play_story" in json.dumps(requests[0]["format"])
        assert ctx.history[-1][1] == "Я живу в добром лесу."
    finally:
        await h.app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["free_lights","quiz","repeat_pattern","odd_one_out","word_game"])
async def test_json_game_offer_still_needs_owned_prompt_then_fresh_consent(monkeypatch, tmp_path, game):
    requests = wire(monkeypatch, json.dumps({"type":"offer_game","game_id":game}))
    h = Harness()
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        await h.send("SpeechReceived",text="Хочу поиграть")
        assert h.state.pending_game_offer == game and h.state.mode == "bear_dialogue"
        await h.key(1)  # The schema is not consent or launch authority.
        assert h.state.mode == "bear_dialogue"
        await h.done()
        await h.send("SpeechReceived", text="Да, давай")
        assert h.state.active_game_id == game and h.state.game_return_mode == "bear_dialogue"
        assert len(requests) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("story", list(STORY_TITLES))
async def test_json_recording_uses_verified_asset_and_returns_caller_once(monkeypatch, tmp_path, story):
    requests = wire(monkeypatch, json.dumps({"type":"play_story","story_id":story}))
    h = Harness()
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        await h.send("SpeechReceived", text="Хочу послушать "+STORY_TITLES[story][0])
        assert h.state.mode == "fixed_story_playing" and h.state.active_story_id == story
        assert len(requests) == 1 and h.app._game_callers
        lease = dict(h.app._active_audio[h.state.session_id])
        await h.done()
        assert h.state.mode == "bear_dialogue" and not h.app._game_callers
        returned = dict(h.app._active_audio[h.state.session_id])
        await h.send("AudioCompleted", {**lease,"response_id":story})
        assert h.app._active_audio[h.state.session_id] == returned
        assert h.state.active_audio_id == "DIALOGUE_RETURN"
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_name,mode", [("games","game_menu"),("stories","fixed_story_menu")])
async def test_json_chooser_actions_use_actual_catalogue_and_keep_caller(monkeypatch,tmp_path,catalog_name,mode):
    wire(monkeypatch, json.dumps({"type":"open_catalog","catalog":catalog_name}))
    h = Harness()
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        await h.send("SpeechReceived",text="Хочу выбрать занятие")
        assert h.state.mode == mode and h.app._game_callers
        await h.key(7)
        assert h.state.mode == "bear_dialogue" and not h.app._game_callers
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["date","weather","what_next","where_now","card_doctors"])
async def test_json_information_action_cannot_grant_facts_without_actual_child_request(monkeypatch,tmp_path,kind):
    requests = wire(monkeypatch,json.dumps({"type":"request_information","kind":kind}))
    h = Harness()
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        result = await h.send("SpeechReceived",text="Что ты умеешь?")
        assert len(requests) == 1
        assert not any(e.get("type") == "typed_info_request" for e in result["outputs"])
        assert h.state.mode == "bear_dialogue"
        assert h.state.active_audio_id == "PHRASE_104"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_verified_ids_refresh_once_from_same_catalogue_snapshot_and_clear_outside_bear(monkeypatch,tmp_path):
    import app.foundation.menu_content as menus
    h = Harness()
    h.app.canonical_runtime.stories = catalogue(tmp_path)
    calls = []
    get = menus.catalogue_items
    def counted(*args,**kwargs):
        calls.append(args)
        return get(*args,**kwargs)
    monkeypatch.setattr(menus,"catalogue_items",counted)
    try:
        original = h.app._bounded_dialogue_context(h.state.session_id)
        assert original.verified_story_ids == tuple(STORY_TITLES) and len(calls) == 1
        revoked = next(iter(STORY_TITLES))
        h.app.canonical_runtime.stories.stories[revoked]["rights_status"] = "revoked"
        retained = h.app._bounded_dialogue_context(h.state.session_id)
        assert retained is original and revoked not in retained.verified_story_ids and len(calls) == 2
        h.state.mode = "support_intake"
        assert h.app._bounded_dialogue_context(h.state.session_id).verified_story_ids == ()
        assert len(calls) == 2
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_json_word_game_continues_and_final_turn_restores_caller_once(monkeypatch,tmp_path):
    answers = [{"type":"offer_game","game_id":"word_game"}] + [
        {"type":"say","text":"Лиса, теперь ты."}]*3 + [
        {"type":"say","text":"Вернулись к разговору, я слушаю."}]
    requests = wire(monkeypatch, lambda _request:json.dumps(answers.pop(0),ensure_ascii=False))
    h = Harness(band="1-3")
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        semantic_requests = semantic(h.app.bounded_s2s)
        await h.send("SpeechReceived",text="Хочу играть в слова")
        await h.done()
        await h.key(1)
        await h.done()
        await h.done()
        for turn in range(4):
            await h.send("SpeechReceived",text="Кот")
            if turn < 3:
                assert h.state.mode == "game_active" and h.state.game_engine_state.round == turn+1
        assert h.state.mode == "bear_dialogue" and not h.app._game_callers
        quality_requests = [request for request in semantic_requests
            if "spoken-response quality requirement" in request["messages"][0]["content"]]
        assert len(requests) == 5 and len(quality_requests) == 4
        assert "ПОСЛЕДНИЙ ХОД" in requests[-1]["messages"][0]["content"]
        policy = quality_requests[-1]["messages"][0]["content"]
        assert "final word-game turn" in policy and "no new word request or question" in policy
        assert "final word-game turn" not in quality_requests[0]["messages"][0]["content"]
    finally:
        await h.close()


class PausedBody(httpx.AsyncByteStream):
    def __init__(self, raw, entered, release, *, fail=False):
        self.raw,self.entered,self.release,self.fail = raw,entered,release,fail
        self.closed = False
    async def __aiter__(self):
        yield (json.dumps({"message":{"content":self.raw}})+"\n").encode()
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise httpx.ReadError("synthetic disconnect")
        yield b'{"done":true,"done_reason":"stop"}\n'
    async def aclose(self):
        self.closed = True


def paused_wire(monkeypatch, raw, *, fail=False):
    entered, release = asyncio.Event(), asyncio.Event()
    bodies, requests = [], []
    def respond(request):
        requests.append(json.loads(request.content))
        body = PausedBody(raw,entered,release,fail=fail)
        bodies.append(body)
        return httpx.Response(200,stream=body)
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:
                        _HTTP_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    return entered,release,bodies,requests


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["raw_json","terminal","semantic","first_pcm"])
async def test_cancellation_at_every_json_boundary_has_no_old_audio_action_or_history(monkeypatch, stage):
    e,ctx = engine(),context()
    entered, release = asyncio.Event(), asyncio.Event()
    bodies = []
    if stage in {"raw_json","terminal"}:
        entered,release,bodies,_ = paused_wire(monkeypatch, '{"type":"offer_game"' if stage == "raw_json"
            else '{"type":"offer_game","game_id":"quiz"}')
    else:
        wire(monkeypatch,'{"type":"say_return","text":"Выбери другое занятие.","target":"root_menu"}')
        if stage == "semantic":
            async def waiting():
                entered.set()
                await release.wait()
            semantic(e,before_reply=waiting)
        else:
            class PausedTTS(MockTTSProvider):
                async def synthesize(self,text,token,cancelled):
                    entered.set()
                    await release.wait()
                    return await super().synthesize(text,token,cancelled)
            e.tts = PausedTTS()
    task = asyncio.create_task(e.run_text_turn("Расскажи",ctx))
    await asyncio.wait_for(entered.wait(),2)
    await e.cancel_session(ctx.session_id)
    release.set()
    result = await asyncio.gather(task,return_exceptions=True)
    assert isinstance(result[0],asyncio.CancelledError)
    assert not e.playback.played and not ctx.history and ctx.turns == 0
    assert not e.active_tasks and not e.active_queues
    assert all(body.closed for body in bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["HandsetPlaced","SessionReset"])
async def test_actual_app_hangup_or_reset_revokes_json_wait_before_action(monkeypatch,tmp_path,boundary):
    entered,release,bodies,_ = paused_wire(monkeypatch,'{"type":"offer_game","game_id":"quiz"}')
    h = Harness()
    task = None
    try:
        install(h)
        h.app.canonical_runtime.stories = catalogue(tmp_path)
        task = asyncio.create_task(h.send("SpeechReceived",text="Хочу играть"))
        await asyncio.wait_for(entered.wait(),2)
        await h.send(boundary)
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        assert h.state.pending_game_offer is None and h.state.active_game_id is None
        assert not h.app._game_callers and not h.app.conversation_contexts
        assert not h.app.bounded_s2s.playback.played and all(body.closed for body in bodies)
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_raw_partial_disconnect_is_not_retried_and_terminal_wait_uses_cumulative_budget(monkeypatch):
    entered,release,_,requests = paused_wire(monkeypatch,'{',fail=True)
    e = engine()
    task = asyncio.create_task(e.run_text_turn("Расскажи",context()))
    await asyncio.wait_for(entered.wait(),2)
    release.set()
    result = await task
    assert result.fallback_reason and len(requests) == 1 and not e.playback.played

    # A complete JSON object still owes a clean terminal within the original LLM budget.
    entered,release,_,requests = paused_wire(monkeypatch,'{"type":"say","text":"Привет."}')
    e = engine()
    e.config = replace(e.config,llm_timeout_s=.03)
    result = await e.run_text_turn("Привет",context())
    assert entered.is_set() and result.fallback_reason == "voice_generation_timeout"
    assert len(requests) == 1 and not e.playback.played and not e.active_queues


@pytest.mark.asyncio
async def test_transient_failure_retries_only_before_first_raw_token(monkeypatch):
    requests=[]
    def respond(request):
        requests.append(request)
        if len(requests) == 1:return httpx.Response(503)
        return httpx.Response(200,content='{"message":{"content":"{\\"type\\":\\"say\\",\\"text\\":\\"Привет.\\"}"}}\n{"done":true,"done_reason":"stop"}\n')
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:_HTTP_CLIENT(transport=httpx.MockTransport(respond),**kwargs))
    e=engine()
    result=await e.run_text_turn("Привет",context())
    assert len(requests)==2 and result.chunks==("Привет.",)


@pytest.mark.asyncio
async def test_controlled_xml_vs_json_first_pcm_tradeoff_is_measured(monkeypatch):
    measurements={}
    for structured in (False,True):
        entered,release,_,_ = paused_wire(monkeypatch,
            '{"type":"say","text":"Я живу в лесу."}' if structured else '<say>Я живу в лесу.</say>')
        e=engine()
        e.llm.delegate.supports_dialogue_json=structured
        requests=semantic(e)
        started=time.perf_counter()
        task=asyncio.create_task(e.run_text_turn("Расскажи",context()))
        await asyncio.wait_for(entered.wait(),2)
        before_terminal=bool(e.playback.played)
        await asyncio.sleep(.03)  # Identical controlled provider terminal gap.
        release.set()
        result=await task
        assert result.chunks==("Я живу в лесу.",) and len(requests)==1
        events=list(e.metrics.events)
        stamp=lambda name:next(item["at"] for item in events if item["event"]==name)
        first=stamp("token_received")
        value={"wall_ms":round((time.perf_counter()-started)*1000,2),
               "pcm_before_terminal":before_terminal,
               "raw_to_first_pcm_ms":round((stamp("playback_started")-first)*1000,2)}
        if structured:
            for name in ("json_complete","llm_clean_terminal","spoken_semantic_started","spoken_semantic_finished"):
                value[name+"_after_raw_ms"]=round((stamp(name)-first)*1000,2)
        measurements["json" if structured else "xml"]=value
    assert measurements["xml"]["pcm_before_terminal"]
    assert not measurements["json"]["pcm_before_terminal"]
    print("CONTROLLED_JSON_XML_TIMINGS="+json.dumps(measurements,sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["raw_json","terminal","semantic","first_pcm"])
async def test_new_generation_cannot_inherit_old_json_action_or_return(monkeypatch,stage):
    e,ctx = engine(),context()
    entered,release = asyncio.Event(),asyncio.Event()
    requests=[]
    old = '{"type":"say_return","text":"Выбери другое занятие.","target":"root_menu"}'
    def respond(request):
        requests.append(json.loads(request.content))
        raw = old if len(requests)==1 else '{"type":"say","text":"Я слушаю тебя."}'
        if len(requests)==1 and stage in {"raw_json","terminal"}:
            return httpx.Response(200,stream=PausedBody('{' if stage=="raw_json" else old,entered,release))
        return httpx.Response(200,content=json.dumps({"message":{"content":raw}})+'\n{"done":true,"done_reason":"stop"}\n')
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:_HTTP_CLIENT(transport=httpx.MockTransport(respond),**kwargs))
    count=0
    if stage=="semantic":
        async def waiting():
            nonlocal count
            count+=1
            if count==1:
                entered.set()
                await release.wait()
        semantic(e,before_reply=waiting)
    if stage=="first_pcm":
        class PausedFirstTTS(MockTTSProvider):
            async def synthesize(self,text,token,cancelled):
                nonlocal count
                count+=1
                if count==1:
                    entered.set()
                    await release.wait()
                return await super().synthesize(text,token,cancelled)
        e.tts=PausedFirstTTS()
    old_turn=asyncio.create_task(e.run_text_turn("Первая реплика",ctx))
    await asyncio.wait_for(entered.wait(),2)
    fresh=await e.run_text_turn("Новая реплика",ctx)
    release.set()
    stopped=await asyncio.gather(old_turn,return_exceptions=True)
    assert isinstance(stopped[0],asyncio.CancelledError)
    assert fresh.chunks==("Я слушаю тебя.",) and not fresh.actions and not fresh.return_to_scenario
    assert ctx.history==[("Новая реплика","Я слушаю тебя.")] and ctx.turns==1
    assert len(e.playback.played)==1 and not e.active_queues


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,ok", [("stop",True),("length",False),(None,False)])
async def test_actual_production_warmup_class_uses_speech_required_canonical_json(monkeypatch,tmp_path,terminal,ok):
    # Synthetic adapters and PCM, but the actual probe, VAD, STT dispatch,
    # bounded engine, Ollama HTTP adapter and audio queue all execute.
    requests=wire(monkeypatch,'{"type":"say","text":"Я слушаю тебя."}',terminal=terminal)
    e=engine()
    semantic_requests=semantic(e)
    pcm=tmp_path/'synthetic-warmup.wav'
    with wave.open(str(pcm),'wb') as output:
        output.setparams((1,2,16000,6400,'NONE','not compressed'))
        output.writeframes(b'\xcc\x0c'*6400)
    manager=SimpleNamespace(engine=e,settings=SimpleNamespace(profile="production_candidate",
        warmup_pcm_path=str(pcm),e2e_stage_timeout_s=2,e2e_warmup_timeout_s=5))
    pipeline=LocalS2SPipeline(EnergyVADAdapter(),MockSpeechRecognizer("Привет",.99),e)
    report=await ProductionE2EWarmupProbe(manager,pipeline).run()
    assert report['ok'] is ok
    assert len(requests)==1 and requests[0]['format']['oneOf']
    assert {branch['properties']['type']['enum'][0] for branch in requests[0]['format']['oneOf']}=={'say','say_led','say_return'}
    assert 'exactly one closed json object' in requests[0]['messages'][0]['content'].lower()
    assert 'Ты — Мишка' in requests[0]['messages'][0]['content']
    assert 'canonical/warmup_speech' in requests[0]['messages'][0]['content']
    assert '{0,17}' in requests[0]['format']['oneOf'][0]['properties']['text']['pattern']
    assert len(semantic_requests)==int(ok)
    if ok:
        assert json.loads(semantic_requests[0]['messages'][1]['content'])['candidate']=='Я слушаю тебя.'
        assert {item['stage'] for item in report['stages'] if item['success']} >= {
            'pcm_input','vad','stt','llm','validation','silero_tts','bounded_tts_queue','playback'}
    else:
        assert not any(item.get('event')=='playback_started' for item in report['metrics_snapshot']['events'])
    assert not pipeline.ingress and not pipeline.pending_tasks and not e.active_queues


@pytest.mark.asyncio
async def test_direct_bare_context_still_uses_request_local_json_contract(monkeypatch):
    from app.foundation.s2s import ConversationContext
    requests=wire(monkeypatch,'{"type":"say","text":"Я слушаю тебя."}')
    e=engine()
    ctx=ConversationContext('direct-pipeline','canonical','bear_dialogue',('__all__',),(),1,120,4)
    assert ctx.system_prompt==''
    result=await e.run_text_turn('Привет',ctx)
    assert result.chunks==('Я слушаю тебя.',) and e.playback.played
    assert requests[0]['format']['oneOf']
