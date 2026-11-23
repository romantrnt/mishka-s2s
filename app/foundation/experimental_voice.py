"""Opt-in trained TTS in isolated, resident subprocesses.

Only an operator-owned manifest can select paths. HTTP clients select a known
route; they cannot select an interpreter, command, file or worker script.
"""
from __future__ import annotations

import array
import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
import signal
import stat
import sys
import time
from typing import Any, AsyncIterator, Callable
import uuid

from .s2s import AudioChunk

ROUTES = ("qwen765", "voxcpm765", "vox555")
_WORKER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/experimental_tts_worker.py"
_MAX_MANIFEST_BYTES = 256 * 1024
_HEADER_BYTES = 8192
_NATIVE_FIRST_BLOCK_SECONDS = 0.8
_NATIVE_LATER_BLOCK_SECONDS = 2.0
_VOX555_FIRST_BLOCK_SECONDS = 0.4
_VOX555_LATER_BLOCK_SECONDS = 0.32
_NATIVE_QUEUE_FRAMES = 2
_SOURCE_REVISIONS = {
    "qwen765": "022e286b98fbec7e1e916cb940cdf532cd9f488e",
    "voxcpm765": "300e4260b509066151d623dc9c0aa922cf82232b",
    "vox555": "300e4260b509066151d623dc9c0aa922cf82232b",
}
# Immutable identities from MODEL_MANIFEST.json; a different checkpoint is a
# different research candidate and must never be served under these route IDs.
_MODEL_HASHES = {
    "qwen765": "a636cb6cd523ee839d9cd43f4c0290befa5b9b08e018627618bae87cec0dde9a",
    "voxcpm765": "f7f964cfa9da23653baec6e6f7750719977ad944ed9f95fe52fe3a620506891d",
    "vox555": "f7f964cfa9da23653baec6e6f7750719977ad944ed9f95fe52fe3a620506891d",
}
_ADAPTER_HASHES = {
    "voxcpm765": "149bca9ab1cec6f1dc3be4e8938b7bdab0f6fcfcd8fed2b11141ae2a0c2c50c1",
    "vox555": "1142e35de2eb2bcad19bae70a35a5360b90a2909834c83c7a7ffb41e226c5270",
}
_REFERENCE_HASH = "ecc44ef9e2d77f4bc92f456670b4964fb8f519d4ee6cf0731491f36a589a9a48"
_APPROVED_VOX555_REFERENCE_HASHES = frozenset({
    _REFERENCE_HASH,
    "e5af4c97f73398927ad026c5616390ec59f03625ead46f981c5912288845ba8c",  # PHRASE_014 narrator clip
})
_QWEN_FILES = {"config.json", "generation_config.json", "merges.txt", "vocab.json",
               "model.safetensors", "preprocessor_config.json", "tokenizer_config.json",
               "speech_tokenizer/config.json", "speech_tokenizer/configuration.json",
               "speech_tokenizer/model.safetensors"}
_VOX_FILES = {"config.json", "model.safetensors", "audiovae.pth", "tokenizer.json",
              "tokenizer_config.json", "special_tokens_map.json", "tokenization_voxcpm2.py"}


class ExperimentalVoiceUnavailable(RuntimeError):
    """A bounded public failure code; never contains paths or model logs."""

    def __init__(self, reason: str = "experimental_voice_unavailable"):
        self.reason = reason
        super().__init__(reason)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _absolute(value: Any, *, directory: bool = False, preserve_symlink: bool = False) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("absolute private path required")
    # A venv interpreter is often a symlink. Resolving its executable changes
    # sys.prefix and silently launches the system environment instead.
    path = Path(value) if preserve_symlink else Path(value).resolve(strict=True)
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError("required path unavailable")
    return path


def _bounded_number(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid limit")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("limit out of range")
    return float(value)


def _load_route(route: str, manifest_path: str | Path) -> dict[str, Any]:
    """Validate all private inputs before starting any executable."""
    try:
        if route not in ROUTES:
            raise ValueError("unknown route")
        manifest = _absolute(str(manifest_path))
        if manifest.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest oversized")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or not isinstance(data["routes"], dict):
            raise ValueError("manifest schema")
        spec = data["routes"][route]
        allowed = {"python", "model_root", "source_root", "source_revision", "files",
                   "adapter_root", "reference_audio", "limits"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValueError("unknown route settings")
        python = _absolute(spec["python"], preserve_symlink=True)
        if not os.access(python, os.X_OK):
            raise ValueError("interpreter unavailable")
        roots = {"model": _absolute(spec["model_root"], directory=True),
                 "source": _absolute(spec["source_root"], directory=True)}
        if spec["source_revision"] != _SOURCE_REVISIONS[route]:
            raise ValueError("wrong source revision")
        is_qwen = route == "qwen765"
        if not is_qwen:
            roots["adapter"] = _absolute(spec["adapter_root"], directory=True)
            reference = _absolute(spec["reference_audio"])
            roots["reference"] = reference.parent
        elif "adapter_root" in spec or "reference_audio" in spec:
            raise ValueError("Qwen export has no runtime reference or adapter")
        files = spec["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= 2048:
            raise ValueError("file inventory unavailable")
        identities: dict[str, dict[str, str]] = {root: {} for root in roots}
        validated = []
        for item in files:
            if set(item) != {"root", "path", "bytes", "sha256"}:
                raise ValueError("file schema")
            root, relative = item["root"], item["path"]
            if root not in roots or not isinstance(relative, str):
                raise ValueError("file root unavailable")
            rel = PurePosixPath(relative)
            if rel.is_absolute() or not rel.parts or any(p in {".", ".."} for p in rel.parts):
                raise ValueError("file escapes root")
            path = (roots[root] / relative).resolve(strict=True)
            if not path.is_relative_to(roots[root]) or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("file escapes root")
            expected = item["sha256"]
            if (not isinstance(expected, str) or len(expected) != 64
                    or any(c not in "0123456789abcdef" for c in expected)):
                raise ValueError("invalid digest")
            if relative in identities[root] or type(item["bytes"]) is not int or item["bytes"] < 0:
                raise ValueError("invalid file entry")
            if path.stat().st_size != item["bytes"] or _sha(path) != expected:
                raise ValueError("file identity mismatch")
            identities[root][relative] = expected
            validated.append({**item, "absolute_path": str(path)})
        required = _QWEN_FILES if is_qwen else _VOX_FILES
        if not required <= identities["model"].keys():
            raise ValueError("incomplete model")
        # Every model/adapter file is inventoried: configs may execute local
        # tokenizer code, and unlisted alternate weights must not be selected.
        for root in ("model",) if is_qwen else ("model", "adapter"):
            actual = {str(p.relative_to(roots[root])) for p in roots[root].rglob("*") if p.is_file()}
            if actual != identities[root].keys():
                raise ValueError("unlisted model files")
        if identities["model"]["model.safetensors"] != _MODEL_HASHES[route]:
            raise ValueError("wrong model")
        package = "qwen_tts" if is_qwen else "voxcpm"
        source_files = {str(p.relative_to(roots["source"]))
                        for p in (roots["source"] / package).rglob("*.py")}
        if (f"{package}/__init__.py" not in identities["source"]
                or not source_files <= identities["source"].keys()):
            raise ValueError("source package not fully pinned")
        if not is_qwen:
            ref_digest = identities["reference"].get(reference.name)
            ref_ok = (ref_digest == _REFERENCE_HASH
                      or (route == "vox555" and ref_digest in _APPROVED_VOX555_REFERENCE_HASHES))
            if (set(identities["adapter"]) != {"lora_config.json", "lora_weights.safetensors"}
                    or identities["adapter"]["lora_weights.safetensors"] != _ADAPTER_HASHES[route]
                    or not ref_ok):
                raise ValueError("wrong adapter or reference")
        supplied_limits = spec.get("limits", {})
        if not isinstance(supplied_limits, dict) or set(supplied_limits) - {
            "max_text_chars", "max_audio_seconds", "startup_timeout_s", "inference_timeout_s"
        }:
            raise ValueError("invalid limits")
        limits = {
            "max_text_chars": int(_bounded_number(supplied_limits.get("max_text_chars", 600), 1, 1000)),
            "max_audio_seconds": _bounded_number(supplied_limits.get("max_audio_seconds", 30), .01, 60),
            "startup_timeout_s": _bounded_number(supplied_limits.get("startup_timeout_s", 180), .05, 600),
            "inference_timeout_s": _bounded_number(supplied_limits.get("inference_timeout_s", 60), .05, 180),
        }
        digest = lambda root: hashlib.sha256(json.dumps(identities[root], sort_keys=True).encode()).hexdigest()
        return {
            "route": route, "python": str(python), "model_root": str(roots["model"]),
            "source_root": str(roots["source"]), "source_revision": spec["source_revision"],
            "runtime_profile": "qwen-recovery-freeze" if is_qwen else "voxcpm-recovery-freeze",
            "sample_rate": 24000 if is_qwen else 48000,
            "model_sha256": identities["model"]["model.safetensors"],
            "model_inventory_sha256": digest("model"), "source_inventory_sha256": digest("source"),
            "adapter_sha256": None if is_qwen else identities["adapter"]["lora_weights.safetensors"],
            "reference_sha256": None if is_qwen else ref_digest,
            "adapter_root": None if is_qwen else str(roots["adapter"]),
            "reference_audio": None if is_qwen else str(reference),
            "files": validated, "limits": limits,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise ExperimentalVoiceUnavailable("experimental_voice_configuration_unavailable") from exc


def create_experimental_tts_provider(route: str, manifest_path: str | Path) -> "ExperimentalTTSProvider":
    """Operator-only factory. Performs file SHA256 validation; starts no GPU work.

    For async application startup use ``await asyncio.to_thread(factory, ...)``
    before awaiting ``provider.start()``. Never pass a request-provided path.
    """
    return ExperimentalTTSProvider(_load_route(route, manifest_path))


class ExperimentalTTSProvider:
    """One resident model and at most one active command; no internal job queue."""

    _shares_local_gpu = True

    def __init__(self, validated_config: dict[str, Any]):
        self._config = validated_config
        self.provider_name = validated_config["route"]
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._job_lock = asyncio.Lock()
        self._recovery_task: asyncio.Task | None = None
        self._draining_exchange: asyncio.Task | None = None
        self._active_stream_exchange: asyncio.Task | None = None
        self._closed = False
        self._worker_info: dict[str, Any] = {}
        self._last_request: dict[str, Any] = {}
        self._last_reason: str | None = "experimental_voice_worker_not_started"
        self.jobs_submitted = self.jobs_completed = self.jobs_cancelled = self.jobs_failed = 0
        self.worker_starts = 0

    def _identity(self) -> dict[str, Any]:
        names = ("model_sha256", "model_inventory_sha256", "adapter_sha256", "reference_sha256",
                 "source_inventory_sha256", "source_revision", "runtime_profile", "sample_rate")
        return {name: self._config[name] for name in names}

    def configuration_readiness(self) -> dict[str, Any]:
        # Files are verified by the factory and reverified by each new worker.
        return {"configuration_ready": True, "provider": self.provider_name,
                "requested_route": self.provider_name, **self._identity()}

    def readiness(self) -> dict[str, Any]:
        alive = bool(self._process and self._process.returncode is None and self._worker_info)
        recovering = bool(self._recovery_task and not self._recovery_task.done())
        native = alive and self._worker_info.get("native_streaming", False)
        return {**self.configuration_readiness(), "ready": alive and not self._closed and not recovering,
                "actual_route": self.provider_name if alive else None,
                "reason": (self._last_reason or (None if alive else "experimental_voice_worker_not_alive")),
                "worker_alive": alive, "closed": self._closed, "recovering": recovering,
                "native_streaming": native,
                "streaming_mode": "native_audio_chunks" if native else "semantic_text_blocks",
                "inference_mode": "native_incremental" if native else "full_block", "fallback_used": False,
                "worker_info": dict(self._worker_info), **self.native_worker_snapshot()}

    def native_worker_snapshot(self) -> dict[str, Any]:
        return {"active_jobs": int(self._job_lock.locked()), "queued_jobs": 0,
                "jobs_submitted": self.jobs_submitted, "jobs_completed": self.jobs_completed,
                "jobs_cancelled": self.jobs_cancelled, "jobs_failed": self.jobs_failed,
                "worker_starts": self.worker_starts}

    def request_metadata(self) -> dict[str, Any]:
        """Bounded metadata for the last block, without text or private paths."""
        return dict(self._last_request)

    async def _discard_stream(self, stream: asyncio.StreamReader) -> None:
        # Deliberately retain neither stderr nor text/audio in logs or health.
        while await stream.read(4096):
            pass

    async def _stop_worker(self) -> None:
        # Repeated Task.cancel() calls must not interrupt pipe draining/reaping.
        cleanup = asyncio.create_task(self._stop_worker_impl())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _stop_worker_impl(self) -> None:
        process, self._process = self._process, None
        self._worker_info = {}
        stderr, self._stderr_task = self._stderr_task, None
        try:
            if process:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                # Draining after kill avoids wait() deadlocking on a full pipe.
                drain = asyncio.create_task(self._discard_stream(process.stdout))
                try:
                    await asyncio.wait_for(process.wait(), 5)
                    await asyncio.wait_for(drain, 5)
                finally:
                    drain.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await drain
                    if process.stdin:
                        process.stdin.close()
        finally:
            if stderr:
                stderr.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr

    async def _guard(self, awaitable, cancelled: Callable[[], bool], timeout: float, *, preserve_on_cancel=False):
        task = asyncio.ensure_future(awaitable)
        interrupted = False
        end = asyncio.get_running_loop().time() + timeout
        try:
            while True:
                if cancelled() or self._closed:
                    raise asyncio.CancelledError
                remaining = end - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({task}, timeout=min(.025, remaining))
                if done:
                    return task.result()
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if not (interrupted and preserve_on_cancel):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @staticmethod
    async def _cancel_exchange(task) -> None:
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _send_cancel_sync(self, request_id: str) -> None:
        if not self._worker_info.get("cooperative_cancel"):
            return
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            return
        with contextlib.suppress(Exception):
            process.stdin.write(json.dumps({"op": "cancel", "request_id": request_id}).encode("utf-8") + b"\n")

    def _schedule_recovery(self, exchange=None, deadline=None) -> None:
        if self._closed:
            return
        self._last_reason = "experimental_voice_draining" if exchange is not None else "experimental_voice_recovering"
        self._draining_exchange = exchange
        self._recovery_task = asyncio.create_task(self._recover_worker(exchange, deadline),
                                                 name="experimental-voice-recovery")
        self._recovery_task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    async def _recover_worker(self, exchange, deadline) -> None:
        # The canceled caller returns promptly. This one maintenance owner
        # consumes its exact reply, retaining the warm model without replaying
        # text or permitting another pipe reader. Only a failed drain reloads.
        try:
            async with self._job_lock:
                try:
                    if self._closed:
                        return
                    if exchange is not None:
                        try:
                            remaining = max(.001, deadline - asyncio.get_running_loop().time())
                            await asyncio.wait_for(asyncio.shield(exchange), remaining)
                        except Exception:
                            await self._cancel_exchange(exchange)
                        else:
                            self._last_reason = None
                            return
                    await self._stop_worker()
                    if not self._closed:
                        self._last_reason = "experimental_voice_recovering"
                        await self._start_worker(lambda: False)
                except BaseException as exc:
                    await self._cancel_exchange(exchange)
                    await self._stop_worker()
                    self._last_reason = ("experimental_voice_closed" if self._closed
                                         else "experimental_voice_recovery_failed")
                    if isinstance(exc, asyncio.CancelledError):
                        raise
        finally:
            await self._cancel_exchange(exchange)
            if self._draining_exchange is exchange:
                self._draining_exchange = None

    async def _header(self) -> dict[str, Any]:
        line = await self._process.stdout.readline()
        if not line or len(line) > _HEADER_BYTES:
            raise ValueError("invalid worker header")
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("invalid worker message")
        return data

    async def _start_worker(self, cancelled: Callable[[], bool]) -> None:
        if self._closed:
            raise ExperimentalVoiceUnavailable("experimental_voice_closed")
        if self._process and self._process.returncode is None and self._worker_info:
            return
        await self._stop_worker()
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1",
               "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
               "TOKENIZERS_PARALLELISM": "false", "PYTHONDONTWRITEBYTECODE": "1"}
        self._process = await asyncio.create_subprocess_exec(
            self._config["python"], "-u", str(_WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True, limit=_HEADER_BYTES,
        )
        self.worker_starts += 1
        self._stderr_task = asyncio.create_task(self._discard_stream(self._process.stderr))
        async def initialize():
            command = json.dumps({"op": "init", "config": self._config}).encode() + b"\n"
            if len(command) > 512 * 1024:
                raise ValueError("worker configuration oversized")
            self._process.stdin.write(command)
            await self._process.stdin.drain()
            return await self._header()
        info = await self._guard(initialize(), cancelled, self._config["limits"]["startup_timeout_s"])
        expected = {"op": "ready", "route": self.provider_name, **self._identity()}
        if any(info.get(key) != value for key, value in expected.items()):
            raise ValueError("worker identity mismatch")
        # Do not forward untrusted worker fields, paths or exception messages.
        versions = {key: info.get(key) for key in ("torch_version", "transformers_version")}
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", v)
               for v in versions.values()):
            raise ValueError("invalid runtime metadata")
        loaded = info.get("lora_loaded_tensors")
        if loaded is not None and (type(loaded) is not int or not 0 < loaded <= 100000):
            raise ValueError("invalid LoRA metadata")
        native = info.get("native_streaming", False)
        if type(native) is not bool or (native and self.provider_name == "qwen765"):
            raise ValueError("invalid streaming capability")
        coop = info.get("cooperative_cancel", False)
        if type(coop) is not bool:
            raise ValueError("invalid cancel capability")
        self._worker_info = {**versions, "lora_loaded_tensors": loaded, "native_streaming": native,
                             "cooperative_cancel": coop}
        self._last_reason = None

    async def start(self) -> None:
        if self._job_lock.locked() or (self._recovery_task and not self._recovery_task.done()):
            raise ExperimentalVoiceUnavailable("experimental_voice_busy")
        async with self._job_lock:
            try:
                await self._start_worker(lambda: False)
            except BaseException as exc:
                self._last_reason = "experimental_voice_start_failed"
                await self._stop_worker()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ExperimentalVoiceUnavailable(self._last_reason) from exc

    async def wait_for_idle(self, cancelled: Callable[[], bool]) -> None:
        """Let an owned successor wait for cancellation drain without replaying old text."""
        deadline = asyncio.get_running_loop().time() + self._config["limits"]["inference_timeout_s"]
        async def released():
            async with self._job_lock:
                pass
        while True:
            if cancelled():
                raise asyncio.CancelledError
            if self._closed:
                raise ExperimentalVoiceUnavailable("experimental_voice_closed")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ExperimentalVoiceUnavailable("experimental_voice_idle_timeout")
            recovery = self._recovery_task
            if recovery is not None and not recovery.done():
                # A newer session cancelling its wait must not cancel maintenance.
                await self._guard(asyncio.shield(recovery), cancelled, remaining)
            elif self._job_lock.locked():
                await self._guard(released(), cancelled, remaining)
            else:
                return

    async def synthesize(self, text: str, token: int, cancelled: Callable[[], bool]) -> AudioChunk:
        return await self._synthesize(text, token, cancelled, first_chunk=None)

    async def synthesize_stream_chunk(self, text: str, token: int, cancelled: Callable[[], bool], *, first_chunk: bool) -> AudioChunk:
        """Generate one complete semantic text block, without pretending native streaming."""
        return await self._synthesize(text, token, cancelled, first_chunk=bool(first_chunk))

    async def synthesize_audio_stream(self, text: str, token: int, cancelled: Callable[[], bool], *,
                                      first_chunk: bool) -> AsyncIterator[AudioChunk]:
        """Native Vox audio with bounded buffering and warm draining on early close.

        The exchange task is the only stdout reader. Abandoning this iterator
        makes it discard remaining validated frames, then recovery returns the
        same model to readiness. A corrupt or timed-out drain reloads the model.
        """
        if self.provider_name == "qwen765":
            yield await self.synthesize_stream_chunk(text, token, cancelled, first_chunk=first_chunk)
            return
        if self._closed:
            raise ExperimentalVoiceUnavailable("experimental_voice_closed")
        try:
            valid = (isinstance(text, str) and bool(text.strip())
                     and len(text) <= self._config["limits"]["max_text_chars"]
                     and len(text.encode("utf-8")) <= 8192)
        except UnicodeError:
            valid = False
        if not valid:
            raise ExperimentalVoiceUnavailable("experimental_voice_text_limit")
        if cancelled():
            raise asyncio.CancelledError
        if self._job_lock.locked() or (self._recovery_task and not self._recovery_task.done()):
            raise ExperimentalVoiceUnavailable("experimental_voice_busy")
        async with self._job_lock:
            self.jobs_submitted += 1
            request_id = uuid.uuid4().hex
            started = time.perf_counter()
            self._last_request = {"request_id": request_id, "requested_route": self.provider_name,
                "actual_route": None, "status": "processing", "cancelled": False,
                "fallback_used": False, "passthrough_used": False, "native_streaming": False,
                "inference_mode": "native_incremental", "streaming_mode": "native_audio_chunks",
                "first_chunk": bool(first_chunk), "input_mode": "text", "seed": 42,
                "native_first_chunk_ms": None, "audible_ttfa_ms": None,
                "generation_settings": {"max_len": 300, "inference_timesteps": 10, "cfg_value": 2.0,
                    "retry_badcase": False, "denoising": False, "normalization": False,
                    "streaming_prefix_len": 4}, **self._identity()}
            queue = asyncio.Queue(maxsize=_NATIVE_QUEUE_FRAMES)
            abandoned = asyncio.Event()
            exchange_task = None
            deadline = None
            try:
                await self._start_worker(cancelled)
                if not self._worker_info.get("native_streaming"):
                    raise ValueError("native streaming unsupported")

                async def offer(pcm):
                    if abandoned.is_set():
                        return
                    put = asyncio.create_task(queue.put(pcm))
                    drop = asyncio.create_task(abandoned.wait())
                    try:
                        await asyncio.wait({put, drop}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in (put, drop):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(put, drop, return_exceptions=True)

                async def exchange():
                    self._process.stdin.write(json.dumps({"op": "synthesize_stream", "request_id": request_id,
                        "text": text}).encode("utf-8") + b"\n")
                    await self._process.stdin.drain()
                    frames = total = 0
                    nonzero = False
                    previous_ms = 0.0
                    maximum = int(self._config["sample_rate"] * self._config["limits"]["max_audio_seconds"])
                    expected = {"route": self.provider_name, "request_id": request_id, **self._identity()}
                    while True:
                        reply = await self._header()
                        if any(reply.get(key) != value for key, value in expected.items()):
                            raise ValueError("stream identity mismatch")
                        elapsed = reply.get("inference_ms")
                        if (type(elapsed) not in (int, float) or not math.isfinite(elapsed)
                                or elapsed < previous_ms):
                            raise ValueError("invalid stream timing")
                        previous_ms = float(elapsed)
                        size = reply.get("samples")
                        if reply.get("op") == "cancelled":
                            if type(reply.get("frames")) is not int or type(size) is not int:
                                raise ValueError("invalid stream cancelled")
                            return total, float(elapsed), float(elapsed)
                        if reply.get("op") == "done":
                            first_ms = reply.get("native_first_chunk_ms")
                            if (type(reply.get("frames")) is not int or reply["frames"] != frames
                                    or not frames or type(size) is not int or size != total or not nonzero
                                    or type(first_ms) not in (int, float) or not math.isfinite(first_ms)
                                    or not 0 <= first_ms <= elapsed):
                                raise ValueError("invalid stream terminal")
                            return total, float(elapsed), float(first_ms)
                        if (reply.get("op") != "audio_chunk" or type(reply.get("index")) is not int
                                or reply["index"] != frames or type(size) is not int
                                or not 0 < size <= self._config["sample_rate"] or total + size > maximum
                                or reply.get("format") != "f32le"):
                            raise ValueError("invalid stream frame")
                        raw = await self._process.stdout.readexactly(size * 4)
                        pcm = array.array("f")
                        pcm.frombytes(raw)
                        if sys.byteorder != "little":
                            pcm.byteswap()
                        if any(not math.isfinite(value) for value in pcm):
                            raise ValueError("invalid stream PCM")
                        total += size
                        frames += 1
                        nonzero = nonzero or any(pcm)
                        await offer(pcm)

                async def next_frame():
                    if not queue.empty():
                        return queue.get_nowait()
                    if exchange_task.done():
                        exchange_task.result()
                        return None
                    receive = asyncio.create_task(queue.get())
                    try:
                        done, _ = await asyncio.wait({receive, exchange_task}, return_when=asyncio.FIRST_COMPLETED)
                        if receive in done:
                            return receive.result()
                        exchange_task.result()
                        return queue.get_nowait() if not queue.empty() else None
                    finally:
                        if not receive.done():
                            receive.cancel()
                        await asyncio.gather(receive, return_exceptions=True)

                timeout = self._config["limits"]["inference_timeout_s"]
                deadline = asyncio.get_running_loop().time() + timeout
                exchange_task = asyncio.create_task(exchange(), name="experimental-voice-stream")
                self._active_stream_exchange = exchange_task
                pending = array.array("f")
                emitted = 0
                first_block_s = _VOX555_FIRST_BLOCK_SECONDS if self.provider_name == "vox555" else _NATIVE_FIRST_BLOCK_SECONDS
                later_block_s = _VOX555_LATER_BLOCK_SECONDS if self.provider_name == "vox555" else _NATIVE_LATER_BLOCK_SECONDS
                target = max(1, int(self._config["sample_rate"] * (
                    first_block_s if (first_chunk or self.provider_name == "vox555") else later_block_s)))
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    frame = await self._guard(next_frame(), cancelled, remaining)
                    if frame is None:
                        break
                    pending.extend(frame)
                    while len(pending) >= target:
                        samples = tuple(pending[:target])
                        del pending[:target]
                        if cancelled() or self._closed:
                            raise asyncio.CancelledError
                        emitted += 1
                        ready_ms = (time.perf_counter() - started) * 1000
                        self._last_request.update(actual_route=self.provider_name, native_streaming=True,
                            audio_chunks=emitted, block_ready_ms=ready_ms)
                        if emitted == 1:
                            self._last_request["native_first_chunk_ms"] = ready_ms
                        yield AudioChunk(samples, self._config["sample_rate"], token)
                        target = max(1, int(self._config["sample_rate"] * later_block_s))
                total, inference_ms, worker_first_ms = exchange_task.result()
                if cancelled() or self._closed:
                    raise asyncio.CancelledError
                if pending:
                    emitted += 1
                    ready_ms = (time.perf_counter() - started) * 1000
                    self._last_request.update(actual_route=self.provider_name, native_streaming=True,
                        audio_chunks=emitted, block_ready_ms=ready_ms)
                    if emitted == 1:
                        self._last_request["native_first_chunk_ms"] = ready_ms
                    yield AudioChunk(tuple(pending), self._config["sample_rate"], token)
                self.jobs_completed += 1
                self._last_reason = None
                self._last_request.update(status="completed", inference_ms=inference_ms,
                    worker_first_chunk_ms=worker_first_ms, audio_seconds=total / self._config["sample_rate"])
            except BaseException as exc:
                abandoned.set()
                interrupted = isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                if interrupted:
                    self.jobs_cancelled += 1
                    self._last_reason = "experimental_voice_cancelled"
                    self._last_request.update(status="cancelled", cancelled=True)
                else:
                    self.jobs_failed += 1
                    self._last_reason = ("experimental_voice_timeout" if isinstance(exc, TimeoutError)
                                         else "experimental_voice_inference_failed")
                    self._last_request.update(status="failed", reason=self._last_reason)
                if interrupted and not self._closed and exchange_task is not None:
                    self._send_cancel_sync(request_id)
                    self._schedule_recovery(exchange_task, deadline)
                    raise
                await self._cancel_exchange(exchange_task)
                await self._stop_worker()
                if interrupted:
                    if not self._closed:
                        self._schedule_recovery()
                    raise
                reason = self._last_reason
                if exchange_task is not None:
                    self._schedule_recovery()
                raise ExperimentalVoiceUnavailable(reason) from exc
            finally:
                abandoned.set()
                if self._active_stream_exchange is exchange_task:
                    self._active_stream_exchange = None

    async def _synthesize(self, text, token, cancelled, *, first_chunk):
        if self._closed:
            raise ExperimentalVoiceUnavailable("experimental_voice_closed")
        try:
            valid_text = (isinstance(text, str) and bool(text.strip())
                and len(text) <= self._config["limits"]["max_text_chars"] and len(text.encode("utf-8")) <= 8192)
        except UnicodeError:
            valid_text = False
        if not valid_text:
            raise ExperimentalVoiceUnavailable("experimental_voice_text_limit")
        if cancelled():
            raise asyncio.CancelledError
        if self._job_lock.locked() or (self._recovery_task and not self._recovery_task.done()):
            raise ExperimentalVoiceUnavailable("experimental_voice_busy")
        async with self._job_lock:
            self.jobs_submitted += 1
            request_id = uuid.uuid4().hex
            started = time.perf_counter()
            self._last_request = {"request_id": request_id, "requested_route": self.provider_name,
                "actual_route": None, "status": "processing", "cancelled": False,
                "fallback_used": False, "passthrough_used": False, "native_streaming": False,
                "inference_mode": "full_block", "streaming_mode": "semantic_text_blocks",
                "first_chunk": first_chunk, "input_mode": "text", "seed": 42,
                "generation_settings": ({"language": "Russian", "speaker": "mishka",
                    "max_new_tokens": 300, "do_sample": False} if self.provider_name == "qwen765" else
                    {"max_len": 300, "inference_timesteps": 10, "cfg_value": 2.0,
                     "retry_badcase": False, "denoising": False, "normalization": False}),
                **self._identity()}
            exchange_task = None
            exchange_deadline = None
            try:
                await self._start_worker(cancelled)
                async def exchange():
                    self._process.stdin.write(json.dumps({"op": "synthesize", "request_id": request_id,
                        "text": text}).encode("utf-8") + b"\n")
                    await self._process.stdin.drain()
                    reply = await self._header()
                    if reply.get("op") == "cancelled":
                        if reply.get("request_id") != request_id or reply.get("route") != self.provider_name:
                            raise ValueError("invalid worker cancelled")
                        elapsed = reply.get("inference_ms")
                        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                            elapsed = 0.0
                        return (), float(elapsed)
                    size = reply.get("samples")
                    if (reply.get("op") != "audio" or reply.get("request_id") != request_id
                            or reply.get("route") != self.provider_name
                            or reply.get("sample_rate") != self._config["sample_rate"]
                            or type(size) is not int or not 0 < size <= int(self._config["sample_rate"] * self._config["limits"]["max_audio_seconds"])
                            or reply.get("format") != "f32le"):
                        raise ValueError("invalid worker audio")
                    raw = await self._process.stdout.readexactly(size * 4)
                    pcm = array.array("f")
                    pcm.frombytes(raw)
                    if sys.byteorder != "little":
                        pcm.byteswap()
                    if any(not math.isfinite(v) for v in pcm) or not any(pcm):
                        raise ValueError("invalid worker PCM")
                    elapsed = reply.get("inference_ms")
                    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                        raise ValueError("invalid worker timing")
                    return tuple(pcm), float(elapsed)
                timeout = self._config["limits"]["inference_timeout_s"]
                exchange_deadline = asyncio.get_running_loop().time() + timeout
                exchange_task = asyncio.create_task(exchange())
                samples, inference_ms = await self._guard(exchange_task, cancelled, timeout, preserve_on_cancel=True)
                if cancelled() or self._closed:
                    raise asyncio.CancelledError
                if not samples:
                    raise ValueError("invalid worker audio")
                self.jobs_completed += 1
                self._last_reason = None
                self._last_request.update(actual_route=self.provider_name, status="completed",
                    inference_ms=inference_ms, block_ready_ms=(time.perf_counter() - started) * 1000,
                    audio_seconds=len(samples) / self._config["sample_rate"],
                    native_first_chunk_ms=None, audible_ttfa_ms=None)
                return AudioChunk(samples, self._config["sample_rate"], token)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    self.jobs_cancelled += 1
                    self._last_reason = "experimental_voice_cancelled"
                    self._last_request.update(status="cancelled", cancelled=True)
                else:
                    self.jobs_failed += 1
                    self._last_reason = ("experimental_voice_timeout" if isinstance(exc, TimeoutError)
                                         else "experimental_voice_inference_failed")
                    self._last_request.update(status="failed", reason=self._last_reason)
                if isinstance(exc, asyncio.CancelledError) and not self._closed:
                    if exchange_task is not None:
                        self._send_cancel_sync(request_id)
                        self._schedule_recovery(exchange_task, exchange_deadline)
                    else:
                        try:
                            await self._stop_worker()
                        finally:
                            self._schedule_recovery()
                    raise
                await self._cancel_exchange(exchange_task)
                await self._stop_worker()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                reason = self._last_reason
                if exchange_task is not None:
                    # Admission requires readiness; recovery cannot depend on
                    # another synthesis request reaching this provider.
                    self._schedule_recovery()
                raise ExperimentalVoiceUnavailable(reason) from exc

    async def close(self) -> None:
        """Kill/drain current work, reject future work, and release model memory."""
        self._closed = True
        recovery = self._recovery_task
        if recovery is not None and not recovery.done():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        await self._cancel_exchange(self._draining_exchange)
        self._draining_exchange = None
        if self._active_stream_exchange is not None:
            # A native iterator can be suspended at yield while downstream
            # works. Stop its sole reader before draining stdout; waiting for
            # that iterator's lock here would deadlock lifecycle shutdown.
            await self._cancel_exchange(self._active_stream_exchange)
            await self._stop_worker()
            self._last_reason = "experimental_voice_closed"
            return
        # An active request sees closed in its cancellation guard, kills/drains,
        # and releases ownership before lifecycle cleanup acquires the lock.
        async with self._job_lock:
            await self._stop_worker()
            self._last_reason = "experimental_voice_closed"
