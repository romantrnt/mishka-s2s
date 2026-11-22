#!/usr/bin/env python3
"""Private stdio worker; run only by ExperimentalTTSProvider in its own venv.

No listening socket, downloads, adapter switching, shell commands or fallback.
The stdout protocol is JSON header + bounded little-endian float32 PCM frames.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time

MAX_INIT_BYTES = 512 * 1024
MAX_COMMAND_BYTES = 16 * 1024
ROUTES = {"qwen765", "voxcpm765", "vox555"}
IDENTITY_FIELDS = ("model_sha256", "model_inventory_sha256", "adapter_sha256", "reference_sha256",
                   "source_inventory_sha256", "source_revision", "runtime_profile", "sample_rate")


def read_command(maximum):
    line = sys.stdin.buffer.readline(maximum + 1)
    if not line:
        return None
    if len(line) > maximum or not line.endswith(b"\n"):
        raise ValueError("command size")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("command schema")
    return value


def send(protocol, header, pcm=None):
    line = json.dumps(header, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(line) > 8192:
        raise ValueError("header size")
    protocol.write(line)
    if pcm is not None:
        protocol.write(pcm)
    protocol.flush()


def release_cuda_cache(torch):
    """Return only unused allocator blocks; retain model and live graph pools.

    Call at a completed warmup/request boundary, never between native frames.
    An actual CUDA cleanup failure remains a visible worker failure.
    """
    empty_cache = getattr(getattr(torch, "cuda", None), "empty_cache", None)
    if callable(empty_cache):
        empty_cache()


def cuda_memory_metadata(torch):
    """Optional bounded allocator readings, excluding driver/other processes."""
    values = {}
    for label, method in (("allocated", "memory_allocated"), ("reserved", "memory_reserved")):
        reader = getattr(getattr(torch, "cuda", None), method, None)
        if not callable(reader):
            continue
        try:
            size = reader()
        except Exception:
            continue
        if type(size) is int and 0 <= size <= 2 ** 60:
            values[f"cuda_memory_{label}_mib"] = size // (1024 * 1024)
    return values


def warm_native_model(config, model, np, torch):
    """Compile/capture the serving context before the parent admits a job.

    VoxCPM's constructor already warms full generation. Native serving adds
    reference conditioning and autocast; warm that exact path as well, using
    a fixed phrase and ten patches rather than any private request text.
    """
    tts = model.tts_model
    if (not hasattr(tts.base_lm.forward_step, "_torchdynamo_orig_callable")
            or not hasattr(tts.residual_lm.forward_step, "_torchdynamo_orig_callable")
            or not hasattr(tts.feat_encoder, "_orig_mod")
            or not hasattr(tts.feat_decoder.estimator, "_orig_mod")):
        raise RuntimeError("Vox optimization unavailable")
    torch.manual_seed(42)
    np.random.seed(42)
    nonzero = False
    with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
        for generated in tts.generate_streaming(
            target_text="Hello, this is the first test sentence.",
            reference_wav_path=config["reference_audio"], max_len=10,
            inference_timesteps=10, cfg_value=2.0, retry_badcase=False,
            seed=42, streaming_prefix_len=4,
        ):
            audio = np.asarray(generated.detach().cpu().float().numpy(), dtype=np.float32)
            if not np.isfinite(audio).all():
                raise ValueError("invalid warmup PCM")
            nonzero = nonzero or bool(np.any(audio))
    torch.cuda.synchronize()
    if not nonzero:
        raise ValueError("empty warmup PCM")


def load_model(config):
    # Recheck on every worker start; the parent may have validated hours ago.
    # File descriptors are not accepted from untrusted network requests.
    for entry in config["files"]:
        path = Path(entry["absolute_path"])
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("asset unavailable")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                raise ValueError("asset identity")
    root = Path(config["source_root"]).resolve(strict=True)
    sys.path.insert(0, str(root))
    import numpy as np
    import torch
    import transformers
    expected_transformers = "4.57.3" if config["route"] == "qwen765" else "5.3.0"
    if transformers.__version__ != expected_transformers:
        raise ValueError("runtime profile mismatch")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA required")
    module = importlib.import_module("qwen_tts" if config["route"] == "qwen765" else "voxcpm")
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise ValueError("source package mismatch")
    loaded_count = None
    if config["route"] == "qwen765":
        model = module.Qwen3TTSModel.from_pretrained(
            config["model_root"], device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa", local_files_only=True,
        )
        model.model.eval()
    else:
        model = module.VoxCPM.from_pretrained(
            config["model_root"], load_denoiser=False, local_files_only=True,
            # Upstream optimize() checks the literal device string "cuda";
            # "cuda:0" silently disables compilation despite optimize=True.
            optimize=True, device="cuda", lora_weights_path=config["adapter_root"],
        )
        # from_pretrained logs loaded/skipped but does not expose them. Repeat
        # the same adapter load to enforce the real return-value contract used
        # in the saved generate_comparison.py, before admitting any request.
        loaded, skipped = model.load_lora(config["adapter_root"])
        from safetensors import safe_open
        with safe_open(str(Path(config["adapter_root"]) / "lora_weights.safetensors"),
                       framework="pt", device="cpu") as stream:
            expected_keys = set(stream.keys())
        if not loaded or skipped or set(loaded) != expected_keys:
            raise RuntimeError("incomplete LoRA load")
        model.set_lora_enabled(True)
        model.tts_model.eval()
        loaded_count = len(loaded)
        warm_native_model(config, model, np, torch)
        build_cache = getattr(model.tts_model, "build_prompt_cache", None)
        if callable(build_cache):
            model._mishka_prompt_cache = build_cache(reference_wav_path=config["reference_audio"])
    torch.cuda.synchronize()
    if config["route"] != "qwen765":
        release_cuda_cache(torch)
    return model, np, torch, transformers, loaded_count


def synthesize(config, model, np, torch, text):
    torch.manual_seed(42)
    np.random.seed(42)
    torch.cuda.synchronize()
    started = time.perf_counter()
    with torch.inference_mode():
        if config["route"] == "qwen765":
            wavs, rate = model.generate_custom_voice(
                text=text, language="Russian", speaker="mishka",
                max_new_tokens=300, do_sample=False,
            )
            if len(wavs) != 1 or rate != config["sample_rate"]:
                raise ValueError("Qwen output contract")
            audio = np.asarray(wavs[0], dtype=np.float32)
        else:
            # The exact trained-model API/settings used by the preserved
            # post-training comparison; no continuation prompt/normalizer.
            prompt_cache = getattr(model, "_mishka_prompt_cache", None)
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                if prompt_cache is not None and callable(getattr(model.tts_model, "generate_with_prompt_cache", None)):
                    generated = model.tts_model.generate_with_prompt_cache(
                        target_text=text, prompt_cache=prompt_cache,
                        max_len=300, inference_timesteps=10, cfg_value=2.0,
                        retry_badcase=False, seed=42,
                    )
                else:
                    generated = model.tts_model.generate(
                        target_text=text, reference_wav_path=config["reference_audio"],
                        max_len=300, inference_timesteps=10, cfg_value=2.0,
                        retry_badcase=False, seed=42,
                    )
                if isinstance(generated, tuple):
                    generated = generated[0]
            audio = generated.detach().cpu().float().numpy()
    torch.cuda.synchronize()
    inference_ms = (time.perf_counter() - started) * 1000
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    maximum = int(config["sample_rate"] * config["limits"]["max_audio_seconds"])
    if not 0 < audio.size <= maximum or not np.isfinite(audio).all() or not np.any(audio):
        raise ValueError("invalid or oversized PCM")
    raw = audio.astype("<f4", copy=False).tobytes()
    if config["route"] != "qwen765":
        release_cuda_cache(torch)
    return raw, int(audio.size), inference_ms


def synthesize_stream(config, model, np, torch, text):
    """Yield Vox's incremental VAE output, never cumulative or padded audio."""
    if config["route"] not in {"voxcpm765", "vox555"}:
        raise ValueError("native streaming unsupported")
    torch.manual_seed(42)
    np.random.seed(42)
    torch.cuda.synchronize()
    started = time.perf_counter()
    maximum = int(config["sample_rate"] * config["limits"]["max_audio_seconds"])
    frame_limit = config["sample_rate"]  # At most one second per binary frame.
    total = 0
    nonzero = False
    prompt_cache = getattr(model, "_mishka_prompt_cache", None)
    with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
        if prompt_cache is not None and callable(getattr(model.tts_model, "generate_with_prompt_cache_streaming", None)):
            increments = model.tts_model.generate_with_prompt_cache_streaming(
                target_text=text, prompt_cache=prompt_cache,
                max_len=300, inference_timesteps=10, cfg_value=2.0,
                retry_badcase=False, seed=42, streaming_prefix_len=4,
            )
        else:
            increments = model.tts_model.generate_streaming(
                target_text=text, reference_wav_path=config["reference_audio"],
                max_len=300, inference_timesteps=10, cfg_value=2.0,
                retry_badcase=False, seed=42, streaming_prefix_len=4,
            )
        for generated in increments:
            if isinstance(generated, tuple):
                generated = generated[0]
            audio = np.asarray(generated.detach().cpu().float().numpy(), dtype=np.float32).reshape(-1)
            if not audio.size:
                continue
            total += int(audio.size)
            if total > maximum or not np.isfinite(audio).all():
                raise ValueError("invalid or oversized PCM")
            nonzero = nonzero or bool(np.any(audio))
            torch.cuda.synchronize()
            elapsed = (time.perf_counter() - started) * 1000
            for offset in range(0, int(audio.size), frame_limit):
                frame = audio[offset:offset + frame_limit]
                yield frame.astype("<f4", copy=False).tobytes(), int(frame.size), elapsed
    if not total or not nonzero:
        raise ValueError("invalid PCM")
    release_cuda_cache(torch)


def main():
    # Keep even native-library writes to fd 1 out of the binary protocol.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    config = None
    try:
        first = read_command(MAX_INIT_BYTES)
        if first is None or first.get("op") != "init":
            raise ValueError("init required")
        config = first["config"]
        if config["route"] not in ROUTES:
            raise ValueError("unknown route")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        model, np, torch, transformers, loaded_count = load_model(config)
        send(protocol, {"op": "ready", "route": config["route"],
            **{key: config[key] for key in IDENTITY_FIELDS},
            "torch_version": torch.__version__, "transformers_version": transformers.__version__,
            "lora_loaded_tensors": loaded_count,
            "native_streaming": config["route"] in {"voxcpm765", "vox555"},
            **cuda_memory_metadata(torch)})
        while True:
            command = read_command(MAX_COMMAND_BYTES)
            if command is None:
                return
            text = command.get("text")
            request_id = command.get("request_id")
            if (command.get("op") not in {"synthesize", "synthesize_stream"} or not isinstance(request_id, str)
                    or len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id)
                    or not isinstance(text, str) or not text.strip()
                    or len(text) > config["limits"]["max_text_chars"]
                    or len(text.encode("utf-8")) > 8192):
                raise ValueError("invalid synthesis command")
            if command["op"] == "synthesize_stream":
                identity = {"route": config["route"], "request_id": request_id,
                            **{key: config[key] for key in IDENTITY_FIELDS}}
                frames = total = 0
                first_ms = None
                for raw, size, elapsed in synthesize_stream(config, model, np, torch, text):
                    if first_ms is None:
                        first_ms = elapsed
                    send(protocol, {"op": "audio_chunk", **identity, "index": frames,
                        "samples": size, "format": "f32le", "inference_ms": elapsed}, raw)
                    frames += 1
                    total += size
                send(protocol, {"op": "done", **identity, "frames": frames, "samples": total,
                    "inference_ms": elapsed, "native_first_chunk_ms": first_ms})
                continue
            raw, size, elapsed = synthesize(config, model, np, torch, text)
            send(protocol, {"op": "audio", "route": config["route"], "request_id": request_id,
                "sample_rate": config["sample_rate"], "samples": size, "format": "f32le",
                "inference_ms": elapsed}, raw)
    except Exception:
        # No private paths, text, exception strings or tracebacks in protocol.
        send(protocol, {"op": "error", "code": "experimental_voice_worker_failed"})
        raise SystemExit(1)
    finally:
        protocol.close()


if __name__ == "__main__":
    main()
