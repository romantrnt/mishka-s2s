import os
import asyncio
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.foundation.config import Settings
pytestmark = pytest.mark.integration
from app.foundation.runtime import ResourceProbe, S2SRuntimeManager
from app.foundation.validation import OllamaMiniLLMValidator
from app.foundation.s2s import (
    AudioChunk, BoundedConversationEngine, ConversationContext, MockLLMProvider,
    MockPlaybackController, MockTTSProvider, OllamaLLMProvider,
    PassthroughVoiceProvider, RVCProvider, RVCV2Provider, VLLMLLMProvider,
)


def production_settings(**overrides):
    values={"profile":"production_qwen_rvc","llm_provider":"ollama"}
    values.update(overrides)
    return Settings(**values).validate()


def test_qwen3_defaults_and_profile_validation(monkeypatch):
    for name in ("LLM_PROVIDER","LLM_MODEL","LLM_QUANTIZATION","LLM_CONTEXT_LENGTH","LLM_NUM_CTX","LLM_TEMPERATURE","LLM_MAX_TOKENS","LLM_KEEP_ALIVE"):
        monkeypatch.delenv(name,raising=False)
    settings=Settings.from_env()
    assert settings.llm_provider=="ollama" and settings.llm_model=="qwen3:14b"
    assert settings.llm_quantization=="Q4_K_M" and settings.llm_num_ctx<=settings.llm_context_length
    with pytest.raises(ValueError): production_settings(llm_provider="mock")


def test_ollama_and_vllm_streaming_payloads_are_configured():
    ollama=OllamaLLMProvider("http://ollama", "qwen3:14b", 10, quantization="Q4_K_M", num_ctx=4096, temperature=.1, max_tokens=77, keep_alive="30m")
    payload=ollama._payload([{"role":"user","content":"x"}],True)
    assert payload["stream"] is True
    assert payload["options"]=={"temperature":.1,"top_p":.85,"top_k":40,"repeat_penalty":1.05,"num_predict":77,"num_ctx":4096}
    assert payload["keep_alive"]=="30m" and payload["model"]=="qwen3:14b"
    assert OllamaLLMProvider("http://ollama", "qwen3:14b", 10, keep_alive="-1")._payload([],False)["keep_alive"] == -1
    vllm=VLLMLLMProvider("http://vllm", "Qwen/Qwen3-14B", 10, max_tokens=55)
    assert vllm._payload([],True)["stream"] is True and vllm._payload([],True)["max_tokens"]==55


def test_ollama_validator_uses_resident_small_context(monkeypatch):
    monkeypatch.delenv("VALIDATION_KEEP_ALIVE", raising=False)
    validator = OllamaMiniLLMValidator(timeout_s=1.0)
    captured = {}

    def fake_request(payload):
        captured.update(__import__("json").loads(payload))
        return json.dumps({"message": {"content": json.dumps({"decision": "allow", "confidence": 1, "reason": "ok"})}}).encode()

    validator._request = fake_request
    decision = asyncio.run(validator.validate("Привет"))
    assert decision.allowed is True
    assert captured["keep_alive"] == -1
    assert captured["options"]["num_ctx"] == 1024


def test_silero_rvc_playback_order_preserves_pcm_through_queue():
    order=[]
    class Silero:
        async def synthesize(self,text,token,cancelled): order.append("silero_tts"); return AudioChunk((.9,),24000,token)
    class RVC:
        async def convert(self,audio,cancelled): order.append("rvc_v2"); return AudioChunk((.8,),audio.sample_rate,audio.token)
    class Playback(MockPlaybackController):
        async def play(self,audio): order.append("playback"); await super().play(audio)
    # Explicitly select RVC on the engine rather than depending on the test
    # process environment. This exercises the real user-turn queue decision.
    settings=Settings(profile="mock", llm_provider="mock", validation_provider="mock", voice_conversion="rvc_v2")
    playback=Playback(); engine=BoundedConversationEngine(MockLLMProvider(),Silero(),RVC(),playback,settings=settings)
    context=ConversationContext("s","fixture_core","fixture_free",("fixture_neutral",),("diagnosis",),2,120,2)
    result=asyncio.run(engine.run_text_turn("fixture",context))
    assert result.fallback_reason is None and order==["silero_tts","rvc_v2","playback"]
    assert playback.played[0].samples==(.8,)
    assert {"llm","silero_tts","rvc_v2","playback"} <= set(engine.metrics.snapshot())
    assert "limiter" not in engine.metrics.snapshot()


def test_rvc_v2_in_memory_adapter_and_cancellation():
    provider=RVCV2Provider(lambda samples,rate: tuple(value/2 for value in samples))
    result=asyncio.run(provider.convert(AudioChunk((.4,-.4),24000,2),lambda:False))
    assert provider.readiness()["ready"] and result.samples==(.2,-.2)
    async def cancelled():
        try: await provider.convert(AudioChunk((.1,),24000,2),lambda:True)
        except asyncio.CancelledError: return True
        return False
    assert asyncio.run(cancelled()) is True


def test_rvc_v2_persistent_worker_is_reused_and_closed(tmp_path):
    project=tmp_path/"applio"; (project/"rvc/infer").mkdir(parents=True)
    (project/"rvc/infer/infer.py").write_text("# fixture\n",encoding="utf-8")
    model=tmp_path/"voice.pth"; index=tmp_path/"voice.index"
    model.write_bytes(b"model"); index.write_bytes(b"index")
    worker=tmp_path/"worker.py"
    worker.write_text(
        "import json,os,shutil,sys\n"
        "print(json.dumps({'event':'ready','pid':os.getpid(),'sample_rate':24000}),flush=True)\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if r['op']=='shutdown': print(json.dumps({'event':'stopped','id':r['id']}),flush=True); break\n"
        " shutil.copyfile(r['input'],r['output'])\n"
        " print(json.dumps({'event':'converted','id':r['id']}),flush=True)\n",
        encoding="utf-8",
    )
    provider=RVCV2Provider(project_root=str(project),python=sys.executable,model_path=str(model),index_path=str(index),tmp_dir=str(tmp_path/"ram"),worker_script=str(worker))
    assert provider.readiness()["ready"] is False
    assert provider.readiness()["reason"] == "worker_not_alive"
    async def scenario():
        first=await provider.convert(AudioChunk((.1,-.1),24000,1),lambda:False)
        first_pid=provider.readiness()["worker_pid"]
        provider._process.terminate()
        await provider._process.wait()
        failed=provider.readiness()
        second=await provider.convert(AudioChunk((.2,-.2),24000,2),lambda:False)
        second_pid=provider.readiness()["worker_pid"]
        assert second_pid != first_pid
        await provider.close()
        return first,second,first_pid,second_pid,failed
    first,second,first_pid,second_pid,failed=asyncio.run(scenario())
    assert first_pid and second_pid and first.samples==pytest.approx((.1,-.1),abs=1/32768)
    assert second.samples==pytest.approx((.2,-.2),abs=1/32768)
    assert failed["ready"] is False and failed["reason"] == "worker_not_alive"
    assert provider.jobs_submitted==2 and provider.jobs_completed==2 and provider.worker_restarts==2
    assert provider.readiness()["worker_alive"] is False
    assert provider.readiness()["ready"] is False


@pytest.mark.skipif(os.getenv("MISHKA_VOICE_CONVERSION", "rvc_v2") != "rvc_v2", reason="RVC-specific contract; active profile uses rvc_v2")
def test_production_readiness_and_warmup_are_fail_closed(monkeypatch):
    class LLM(MockLLMProvider):
        async def readiness(self): return {"ready":True,"provider":"fixture"}
        async def warmup(self): return {"ok":True}
    class TTS(MockTTSProvider):
        def readiness(self): return {"ready":True}
    class Voice(RVCProvider):
        def readiness(self): return {"ready":True}
    class InputComponent:
        def readiness(self): return {"ready":True}
        async def warmup(self): return {"ok":True}
    class Playback(MockPlaybackController):
        def readiness(self): return {"ready":True}
    llm=LLM(); tts=TTS(); voice=Voice(); engine=BoundedConversationEngine(llm,tts,voice,MockPlaybackController())
    manager=S2SRuntimeManager(production_settings(min_available_ram_gb=1,min_available_vram_gb=1),llm,tts,voice,engine,vad=InputComponent(),stt=InputComponent(),playback=Playback())
    monkeypatch.setattr(ResourceProbe,"available_ram_gb",staticmethod(lambda:2.0))
    async def vram(): return 2.0
    monkeypatch.setattr(ResourceProbe,"available_vram_gb",staticmethod(vram))
    readiness=asyncio.run(manager.readiness()); warmup=asyncio.run(manager.warmup())
    warmed_readiness=asyncio.run(manager.readiness())
    assert readiness["prerequisites_ready"] is True and readiness["ready"] is False
    assert warmup["ok"] is False and warmup["reason"] == "full_e2e_warmup_probe_unavailable"
    assert warmed_readiness["ready"] is False
    assert {"warmup_silero_vad","warmup_faster_whisper","warmup_llm"} <= set(engine.metrics.snapshot())
    assert {"warmup_silero", "warmup_rvc_v2"} <= set(engine.metrics.snapshot())


def test_production_resource_threshold_blocks_readiness(monkeypatch):
    class Component:
        async def readiness(self): return {"ready":True}
    class Local:
        def readiness(self): return {"ready":True}
    class Input(Local):
        async def warmup(self): return {"ok":True}
    llm=Component(); tts=Local(); voice=Local(); engine=BoundedConversationEngine(MockLLMProvider(),MockTTSProvider(),RVCProvider(),MockPlaybackController())
    manager=S2SRuntimeManager(production_settings(),llm,tts,voice,engine,vad=Input(),stt=Input(),playback=Local())
    monkeypatch.setattr(ResourceProbe,"available_ram_gb",staticmethod(lambda:4.0))
    async def no_vram(): return None
    monkeypatch.setattr(ResourceProbe,"available_vram_gb",staticmethod(no_vram))
    result=asyncio.run(manager.readiness())
    assert result["ready"] is False and result["resources"]["ready"] is False


def test_rvc_request_is_fail_closed_when_assets_are_unavailable(monkeypatch):
    from app.foundation import api as api_module
    monkeypatch.setenv("MISHKA_VOICE_CONVERSION", "rvc_v2")
    with pytest.raises(RuntimeError, match="RVC v2 requested"):
        api_module._build_s2s(
            production_settings(
                profile="production_candidate",
                rvc_project_root="/missing/rvc",
                rvc_python="/missing/python",
                rvc_model="/missing/model.pth",
                rvc_index="/missing/model.index",
            ),
            Path("/workspace/mishka/content"),
        )


def test_unknown_voice_mode_is_rejected(monkeypatch):
    from app.foundation import api as api_module
    monkeypatch.setenv("MISHKA_VOICE_CONVERSION", "mystery_voice")
    with pytest.raises(ValueError, match="unsupported MISHKA_VOICE_CONVERSION"):
        api_module._build_s2s(production_settings(profile="production_candidate"), Path("/workspace/mishka/content"))
