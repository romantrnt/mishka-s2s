from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .s2s import AudioChunk, BoundedConversationEngine


logger = logging.getLogger("mishka.runtime")


class ResourceProbe:
    """Best-effort diagnostics. Thresholds are deployment policy, not guarantees."""

    @staticmethod
    def available_ram_gb() -> float | None:
        try:
            values={}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key,value=line.split(":",1); values[key]=float(value.strip().split()[0])
            return round(values["MemAvailable"]/1024/1024,2)
        except (OSError,KeyError,ValueError):
            return None

    @staticmethod
    async def available_vram_gb() -> float | None:
        binary=shutil.which("nvidia-smi")
        if not binary: return None
        try:
            process=await asyncio.create_subprocess_exec(binary,"--query-gpu=memory.free","--format=csv,noheader,nounits",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            stdout,_=await asyncio.wait_for(process.communicate(),3)
            values=[float(line) for line in stdout.decode().splitlines() if line.strip()]
            return round(max(values)/1024,2) if process.returncode==0 and values else None
        except (OSError,ValueError,TimeoutError): return None


class S2SRuntimeManager:
    def __init__(self, settings: Settings, llm: Any, tts: Any, voice: Any,
                 engine: BoundedConversationEngine, *, vad: Any = None,
                 stt: Any = None, playback: Any = None):
        self.settings=settings; self.llm=llm; self.tts=tts; self.voice=voice; self.engine=engine
        self.vad=vad; self.stt=stt; self.playback=playback; self.audio_pipeline=None
        self.last_warmup: dict[str,Any] | None=None
        self.last_successful_warmup: dict[str,Any] | None=None
        self.last_failed_warmup: dict[str,Any] | None=None
        self.active_probe_correlation_id: str | None=None
        self.e2e_probe: Any = None
        self._probe_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovery_running = False
        self.recovery_generation = 0
        self.last_recovery: dict[str, Any] | None = None

    async def close(self) -> None:
        """Release session-owned audio and resident native workers."""
        await self.engine.cancel_all_sessions()
        closer=getattr(self.voice,"close",None)
        if closer is not None:
            result=closer()
            if inspect.isawaitable(result): await result

    def configuration(self) -> dict[str,Any]:
        provider="mock" if self.settings.profile=="mock" else self.settings.llm_provider
        model="mock" if self.settings.profile=="mock" else self.settings.llm_model
        voice_mode = self.settings.voice_conversion
        voice = "mock_passthrough" if self.settings.profile=="mock" else ("passthrough" if voice_mode in {"off", "disabled"} else "rvc_v2")
        validation_provider = self.settings.validation_provider
        validation_num_ctx = self.settings.validation_num_ctx
        return {"profile":self.settings.profile,"policy_version":self.settings.policy_version,"deployment_policy":self.settings.policy_snapshot(),"llm_provider":provider,"llm_model":model,"llm_quantization":self.settings.llm_quantization or None,"llm_context_length":self.settings.llm_context_length,"llm_num_ctx":self.settings.llm_num_ctx,"llm_temperature":self.settings.llm_temperature,"llm_max_tokens":self.settings.llm_max_tokens,"llm_keep_alive":self.settings.llm_keep_alive,"llm_warmup_timeout_s":self.settings.llm_warmup_timeout_s,"validation_provider":validation_provider,"validation_model":self.settings.validation_model,"validation_timeout_ms":self.settings.validation_timeout_ms,"validation_num_ctx":validation_num_ctx,"validation_fail_closed":self.settings.validation_fail_closed,"stt_provider":"mock" if self.settings.profile=="mock" else "faster_whisper","stt_model":"mock" if self.settings.profile=="mock" else self.settings.stt_model,"tts_provider":"mock" if self.settings.profile=="mock" else "silero","tts":"mock" if self.settings.profile=="mock" else "silero","voice_conversion":voice,"lazy":True,"effective_settings":self.settings.redacted_snapshot()}

    async def resource_readiness(self) -> dict[str, Any]:
        ram = ResourceProbe.available_ram_gb()
        vram = await ResourceProbe.available_vram_gb()
        ram_ok = ram is not None and ram >= self.settings.min_available_ram_gb
        needs_gpu = self.settings.profile in {"local_gpu", "production_candidate", "exhibition_candidate", "production_qwen_rvc"}
        vram_ok = not needs_gpu or (vram is not None and vram >= self.settings.min_available_vram_gb)
        reasons = []
        if needs_gpu and vram is None:
            reasons.append("vram_unavailable")
        elif needs_gpu and not vram_ok:
            reasons.append("vram_reserve_insufficient")
        if not ram_ok:
            reasons.append("ram_reserve_insufficient" if ram is not None else "ram_unavailable")
        return {
            "ready": ram_ok and vram_ok,
            "available_ram_gb": ram,
            "minimum_ram_gb": self.settings.min_available_ram_gb,
            "available_vram_gb": vram,
            "minimum_vram_gb": self.settings.min_available_vram_gb if needs_gpu else None,
            "headroom_vram_gb": round(vram - self.settings.min_available_vram_gb, 2) if vram is not None and needs_gpu else None,
            "policy": "configured MIN_AVAILABLE_VRAM_GB/MIN_AVAILABLE_RAM_GB reserve",
            "reason": None if not reasons else ",".join(reasons),
            "thresholds_require_target_benchmark": True,
        }

    async def recover(self, lifecycle, trigger: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one fail-closed recovery cycle; never runs in parallel."""
        async with self._recovery_lock:
            if self._recovery_running:
                return {"ok": False, "reason": "recovery_already_running", "generation": self.recovery_generation}
            self._recovery_running = True
        correlation_id = str(uuid.uuid4())
        started = time.perf_counter()
        generation = await lifecycle.begin_recovery(trigger, correlation_id)
        self.recovery_generation = generation
        cleanup: dict[str, Any] = {}
        try:
            if self.settings.profile == "mock":
                result = {"ok": False, "reason": "mock_recovery_forbidden", "diagnostic": {"stage": "profile"}}
            else:
                pipeline = self.audio_pipeline
                if pipeline is not None:
                    for session_id in tuple(set(pipeline.pending_tasks) | set(pipeline.ingress) | set(pipeline.generations)):
                        await pipeline.cancel_session(session_id)
                cleanup = await self.engine.cancel_all_sessions()
                resources = await self.resource_readiness()
                if not resources.get("ready"):
                    result = {"ok": False, "reason": "resource_capacity_insufficient", "diagnostic": {"stage": "resource_readiness", "resources": resources}}
                else:
                    checks = {}
                    for name, component in (("vad", self.vad), ("stt", self.stt), ("llm", self.llm), ("silero", self.tts), ("playback", self.playback)):
                        checks[name] = component.readiness() if hasattr(component, "readiness") else {"ready": False, "reason": "readiness_unavailable"}
                    failed = {name: value for name, value in checks.items() if not value.get("ready")}
                    if failed:
                        result = {"ok": False, "reason": "dependency_unavailable", "diagnostic": {"stage": "dependency_readiness", "failed": failed, "resources": resources}}
                    else:
                        result = await self.warmup()
                        result = result if isinstance(result, dict) else {"ok": False, "reason": "invalid_warmup_result"}
                        result.setdefault("diagnostic", {})
                        result["diagnostic"].update({"stage": "e2e_warmup", "resources": result.get("resources", resources)})
            result.setdefault("diagnostic", {})
            result["diagnostic"].update({"trigger": trigger, "trigger_details": details or {}})
            result["correlation_id"] = correlation_id
            result["recovery_generation"] = generation
            result["cleanup"] = cleanup
            result["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            lifecycle.finish_recovery(generation, result)
            self.last_recovery = result
            return result
        except asyncio.CancelledError:
            result = {"ok": False, "reason": "recovery_cancelled", "correlation_id": correlation_id, "recovery_generation": generation, "cleanup": cleanup, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
            lifecycle.finish_recovery(generation, result)
            self.last_recovery = result
            raise
        except Exception as exc:
            result = {"ok": False, "reason": "recovery_exception", "correlation_id": correlation_id, "recovery_generation": generation, "cleanup": cleanup, "error": type(exc).__name__, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
            lifecycle.finish_recovery(generation, result)
            self.last_recovery = result
            return result
        finally:
            async with self._recovery_lock:
                self._recovery_running = False

    async def readiness(self) -> dict[str,Any]:
        if self.settings.profile=="mock":
            return {**self.configuration(),"ready":False,"reason":"mock_profile_not_production_ready","resources":{"required":False},"latency_metrics":self.engine.metrics.snapshot(),"warmup":self.last_warmup}
        vad_status=self.vad.readiness() if hasattr(self.vad,"readiness") else {"ready":False,"reason":"vad_has_no_readiness"}
        stt_status=self.stt.readiness() if hasattr(self.stt,"readiness") else {"ready":False,"reason":"stt_has_no_readiness"}
        llm_status=await self.llm.readiness() if hasattr(self.llm,"readiness") else {"ready":False,"reason":"provider_has_no_readiness"}
        tts_status=self.tts.readiness() if hasattr(self.tts,"readiness") else {"ready":False,"reason":"tts_has_no_readiness"}
        voice_mode = self.settings.voice_conversion
        voice_status = ({"ready": True, "status": "disabled", "provider": "passthrough", "voice_conversion": "disabled", "bypassed": True, "reason": "rvc_disabled"} if voice_mode in {"off", "disabled"} else self.voice.readiness() if hasattr(self.voice, "readiness") else {"ready": False, "reason": "voice_has_no_readiness"})
        playback_status=self.playback.readiness() if hasattr(self.playback,"readiness") else {"ready":False,"reason":"playback_has_no_readiness"}
        resources=await self.resource_readiness()
        components={"vad":vad_status,"stt":stt_status,"llm":llm_status,"silero":tts_status,"rvc_v2":voice_status,"playback":playback_status}
        prerequisites_ready=bool(all(item.get("ready") for item in components.values()) and resources["ready"])
        warmup_required=self.settings.profile in {"production_candidate","exhibition_candidate","production_qwen_rvc"}
        warmup_ready=not warmup_required or bool(self.last_warmup and self.last_warmup.get("ok"))
        failed_components=[(name, item) for name, item in components.items() if not item.get("ready")]
        if failed_components:
            reason=str(failed_components[0][1].get("reason") or f"{failed_components[0][0]}_not_ready")
        elif not resources["ready"]:
            reason=str(resources.get("reason") or "resource_capacity_insufficient")
        elif not warmup_ready:
            reason="warmup_incomplete"
        else:
            reason=None
        return {**self.configuration(),"ready":prerequisites_ready and warmup_ready,
                "reason":reason,"prerequisites_ready":prerequisites_ready,"warmup_required":warmup_required,"warmup_ready":warmup_ready,
                "components":components,"resources":resources,
                "latency_metrics":self.engine.metrics.snapshot(),"warmup":self.last_warmup}

    async def warmup(self) -> dict[str,Any]:
        started=time.perf_counter(); stages: dict[str, Any] = {}
        if self.settings.profile=="mock":
            self.last_warmup={"ok":True,"mock":True,"latency_ms":0.0}; return self.last_warmup
        try:
            for name,component in (("silero_vad",self.vad),("faster_whisper",self.stt)):
                stage_started=time.perf_counter()
                if not hasattr(component,"warmup"): raise RuntimeError(f"{name} has no warmup")
                stages[name]=await asyncio.wait_for(component.warmup(), self.settings.e2e_stage_timeout_s); self.engine.metrics.observe("warmup_"+name,stage_started,bool(stages[name].get("ok")))
                if not stages[name].get("ok"): raise RuntimeError(f"{name} warmup failed")
            llm_started=time.perf_counter(); stages["llm"]=await asyncio.wait_for(self.llm.warmup(), self.settings.e2e_stage_timeout_s); self.engine.metrics.observe("warmup_llm",llm_started,bool(stages["llm"].get("ok")))
            if not stages["llm"].get("ok"): raise RuntimeError("LLM warmup failed")
            cancelled=lambda:False
            silero_started=time.perf_counter()
            audio: AudioChunk | None = None
            warm_tts=getattr(self.engine, "tts", self.tts)
            for warmup_text in ("Привет!", "Расскажи сказку.", "Как дела?"):
                audio=await asyncio.wait_for(warm_tts.synthesize(warmup_text, 0, cancelled), self.settings.e2e_stage_timeout_s)
            self.engine.metrics.observe("warmup_silero", silero_started)
            stages["silero"]={"ok":True,"sample_rate":audio.sample_rate,"samples":len(audio.samples)}
            voice_mode = self.settings.voice_conversion
            if voice_mode in {"off", "disabled"}:
                converted = audio
                stages["rvc_v2"] = {"ok":True,"provider":"passthrough","bypassed":True,"sample_rate":converted.sample_rate,"samples":len(converted.samples)}
            else:
                rvc_started = time.perf_counter()
                try:
                    converted = await asyncio.wait_for(
                        self.voice.convert(audio, cancelled),
                        self.settings.e2e_stage_timeout_s,
                    )
                    self.engine.metrics.observe("warmup_rvc_v2", rvc_started)
                    stages["rvc_v2"] = {
                        "ok": True,
                        "sample_rate": converted.sample_rate,
                        "samples": len(converted.samples),
                    }
                    if getattr(self.voice, "last_fallback_reason", None):
                        stages["rvc_v2"]["fallback"] = "passthrough"
                        stages["rvc_v2"]["primary_error"] = self.voice.last_fallback_reason
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    converted = audio
                    reason = f"{type(exc).__name__}: {str(exc)[:160]}"
                    logger.warning("RVC warmup unavailable; continuing with clean Silero audio: %s", reason)
                    self.engine.metrics.observe("warmup_rvc_v2", rvc_started, success=False)
                    stages["rvc_v2"] = {
                        "ok": True,
                        "fallback": "passthrough",
                        "primary_error": reason,
                        "sample_rate": converted.sample_rate,
                        "samples": len(converted.samples),
                    }
            playback_status=self.playback.readiness() if hasattr(self.playback,"readiness") else {"ready":False}
            stages["playback"]=playback_status
            if not playback_status.get("ready"): raise RuntimeError("playback warmup failed")
            # Model warmup is not production readiness. A real probe must
            # exercise PCM -> VAD -> STT -> LLM -> validation -> TTS ->
            # bounded queue -> playback. Fixture/mock probes are forbidden.
            probe = getattr(self, "e2e_probe", None)
            if probe is None: raise RuntimeError("full_e2e_warmup_probe_unavailable")
            async with self._probe_lock:
                e2e = await asyncio.wait_for(probe(), self.settings.e2e_stage_timeout_s)
            stages["e2e"] = e2e
            self.active_probe_correlation_id = e2e.get("correlation_id") if isinstance(e2e, dict) else None
            if not isinstance(e2e, dict) or not e2e.get("ok"):
                self.last_warmup={"ok":False,"e2e":e2e,"stages":stages,
                                  "latency_ms":round((time.perf_counter()-started)*1000,2),
                                  "reason":str((e2e or {}).get("reason", "full_e2e_warmup_failed"))}
                return self.last_warmup
            resources = await self.resource_readiness()
            stages["resources"] = resources
            if not resources["ready"]:
                self.last_warmup={
                    "ok":False,
                    "e2e":e2e,
                    "stages":stages,
                    "resources":resources,
                    "latency_ms":round((time.perf_counter()-started)*1000,2),
                    "reason":"resource_capacity_insufficient",
                }
                return self.last_warmup
            self.last_warmup={"ok":True,"e2e":e2e,"stages":stages,"resources":resources,"latency_ms":round((time.perf_counter()-started)*1000,2)}
            self.last_successful_warmup = self.last_warmup
        except Exception as exc:
            self.last_warmup={"ok":False,"stages":stages,"latency_ms":round((time.perf_counter()-started)*1000,2),"reason":str(exc) or type(exc).__name__}
            self.last_failed_warmup = self.last_warmup
        return self.last_warmup
