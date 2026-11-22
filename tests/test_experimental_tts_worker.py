"""CPU checks of the native Vox adapter; these do not establish GPU inference."""
import contextlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest


spec = importlib.util.spec_from_file_location(
    "experimental_tts_worker", Path(__file__).resolve().parents[1] / "scripts/experimental_tts_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Tensor:
    def __init__(self, values):
        self.values = np.asarray(values, dtype=np.float32)

    def detach(self):
        return self

    cpu = float = detach

    def numpy(self):
        return self.values


def setup_stream(increments, **limits):
    calls = []
    advanced = []
    def generate_streaming(**kwargs):
        calls.append(kwargs)
        for index, values in enumerate(increments):
            advanced.append(index)
            yield Tensor(values)
    torch = SimpleNamespace(
        manual_seed=lambda seed: calls.append({"manual_seed": seed}),
        cuda=SimpleNamespace(synchronize=lambda: None),
        inference_mode=contextlib.nullcontext,
        autocast=lambda **kwargs: contextlib.nullcontext(), bfloat16="bfloat16")
    model = SimpleNamespace(tts_model=SimpleNamespace(generate_streaming=generate_streaming))
    config = {"route": "voxcpm765", "sample_rate": 4, "reference_audio": "/private/reference.wav",
              "limits": {"max_audio_seconds": 10, **limits}}
    return config, model, torch, calls, advanced


def test_native_worker_emits_first_increment_before_requesting_next_and_keeps_exact_pcm():
    config, model, torch, calls, advanced = setup_stream([[1, 2, 3, 4, 5], [6, 7, 8]])
    stream = worker.synthesize_stream(config, model, np, torch, "Привет")
    first = next(stream)
    assert advanced == [0], "the first frame must not materialize the remaining generator"
    frames = [first, *stream]
    assert [size for _, size, _ in frames] == [4, 1, 3]
    assert np.frombuffer(b"".join(raw for raw, _, _ in frames), dtype="<f4").tolist() == list(range(1, 9))
    assert all(elapsed >= 0 for _, _, elapsed in frames)
    assert calls == [{"manual_seed": 42}, {"target_text": "Привет",
        "reference_wav_path": "/private/reference.wav", "max_len": 300,
        "inference_timesteps": 10, "cfg_value": 2.0, "retry_badcase": False,
        "seed": 42, "streaming_prefix_len": 4}]


@pytest.mark.parametrize("increments,limits", [([[float("nan")]], {}),
    ([[0, 0], [0]], {}), ([], {}), ([[1, 2, 3], [4, 5]], {"max_audio_seconds": 1})])
def test_native_worker_rejects_nonfinite_silent_empty_or_oversized_total(increments, limits):
    config, model, torch, _, _ = setup_stream(increments, **limits)
    with pytest.raises(ValueError):
        list(worker.synthesize_stream(config, model, np, torch, "text"))


def test_native_worker_preserves_silent_frames_inside_nonzero_audio():
    config, model, torch, _, _ = setup_stream([[], [0, 0], [1, 0]])
    frames = list(worker.synthesize_stream(config, model, np, torch, "text"))
    assert np.frombuffer(b"".join(raw for raw, _, _ in frames), dtype="<f4").tolist() == [0, 0, 1, 0]


def mark_compiled(model):
    def step():
        pass
    step._torchdynamo_orig_callable = step
    model.tts_model.base_lm = SimpleNamespace(forward_step=step)
    model.tts_model.residual_lm = SimpleNamespace(forward_step=step)
    model.tts_model.feat_encoder = SimpleNamespace(_orig_mod=object())
    model.tts_model.feat_decoder = SimpleNamespace(estimator=SimpleNamespace(_orig_mod=object()))


def test_native_warmup_consumes_compiled_reference_route_with_serving_settings():
    config, model, torch, calls, advanced = setup_stream([[0, 0], [1, 0], [0, 1]])
    mark_compiled(model)
    worker.warm_native_model(config, model, np, torch)
    assert advanced == [0, 1, 2]
    assert calls == [{"manual_seed": 42}, {
        "target_text": "Hello, this is the first test sentence.",
        "reference_wav_path": "/private/reference.wav", "max_len": 10,
        "inference_timesteps": 10, "cfg_value": 2.0, "retry_badcase": False,
        "seed": 42, "streaming_prefix_len": 4}]


def test_native_warmup_refuses_upstream_silent_optimization_failure():
    config, model, torch, calls, _ = setup_stream([[1]])
    mark_compiled(model)
    model.tts_model.feat_decoder.estimator = SimpleNamespace()
    with pytest.raises(RuntimeError, match="optimization unavailable"):
        worker.warm_native_model(config, model, np, torch)
    assert calls == []


@pytest.mark.parametrize("increments", [[], [[0, 0]], [[float("nan")]]])
def test_native_warmup_refuses_empty_silent_or_nonfinite_output(increments):
    config, model, torch, _, _ = setup_stream(increments)
    mark_compiled(model)
    with pytest.raises(ValueError):
        worker.warm_native_model(config, model, np, torch)


def test_vox_loader_uses_effective_cuda_optimization_and_warms_after_lora(tmp_path, monkeypatch):
    config, model, torch, calls, advanced = setup_stream([[1, 0]])
    mark_compiled(model)
    model.tts_model.eval = lambda: None
    order = []
    model.load_lora = lambda path: (order.append("verify_lora") or (["lora.one"], []))
    model.set_lora_enabled = lambda enabled: order.append(("enable_lora", enabled))
    def empty_cache():
        assert advanced == [0], "cleanup must follow complete native warmup"
        order.append("empty_cache")
    torch.cuda.empty_cache = empty_cache
    source = tmp_path / "source"
    source.mkdir()
    factory_calls = []
    def from_pretrained(path, **kwargs):
        factory_calls.append((path, kwargs))
        return model
    module = SimpleNamespace(__file__=str(source / "__init__.py"),
        VoxCPM=SimpleNamespace(from_pretrained=from_pretrained))
    torch.cuda.is_available = lambda: True
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(__version__="5.3.0"))
    monkeypatch.setitem(sys.modules, "voxcpm", module)
    @contextlib.contextmanager
    def safe_open(*args, **kwargs):
        yield SimpleNamespace(keys=lambda: ["lora.one"])
    monkeypatch.setitem(sys.modules, "safetensors", SimpleNamespace(safe_open=safe_open))
    config.update(files=[], source_root=str(source), model_root="/private/model", adapter_root="/private/adapter")
    original_path = sys.path[:]
    try:
        result = worker.load_model(config)
    finally:
        sys.path[:] = original_path
    assert result[0] is model and result[-1] == 1
    assert factory_calls == [("/private/model", {"load_denoiser": False,
        "local_files_only": True, "optimize": True, "device": "cuda",
        "lora_weights_path": "/private/adapter"})]
    assert order == ["verify_lora", ("enable_lora", True), "empty_cache"]
    assert advanced == [0] and calls[-1]["inference_timesteps"] == 10


def test_cache_release_waits_for_complete_stream_and_preserves_repeated_pcm():
    config, model, torch, _, advanced = setup_stream([[1, 2, 3, 4, 5], [6, 7]])
    mark_compiled(model)
    resident = model.tts_model.feat_decoder.estimator
    releases = []
    torch.cuda.empty_cache = lambda: releases.append(tuple(advanced))
    first = worker.synthesize_stream(config, model, np, torch, "first")
    assert next(first)[1] == 4 and releases == []
    assert next(first)[1] == 1 and releases == [], "no cleanup between native frames"
    tail = list(first)
    assert releases == [(0, 1)]
    second = list(worker.synthesize_stream(config, model, np, torch, "second"))
    assert len(releases) == 2 and model.tts_model.feat_decoder.estimator is resident
    assert np.frombuffer(tail[0][0], dtype="<f4").tolist() == [6, 7]
    assert np.frombuffer(b"".join(frame[0] for frame in second), dtype="<f4").tolist() == list(range(1, 8))


def test_full_vox_request_releases_cache_after_valid_complete_pcm():
    config, model, torch, _, _ = setup_stream([])
    events = []
    def generate(**kwargs):
        events.append("generate")
        return Tensor([1, 2])
    model.tts_model.generate = generate
    torch.cuda.empty_cache = lambda: events.append("empty_cache")
    raw, samples, _ = worker.synthesize(config, model, np, torch, "text")
    assert events == ["generate", "empty_cache"]
    assert samples == 2 and np.frombuffer(raw, dtype="<f4").tolist() == [1, 2]


def test_optional_cache_api_guard_keeps_real_cleanup_failures_visible():
    worker.release_cuda_cache(SimpleNamespace(cuda=SimpleNamespace()))
    worker.release_cuda_cache(SimpleNamespace(cuda=SimpleNamespace(empty_cache=None)))
    def fail():
        raise RuntimeError("CUDA failure")
    with pytest.raises(RuntimeError, match="CUDA failure"):
        worker.release_cuda_cache(SimpleNamespace(cuda=SimpleNamespace(empty_cache=fail)))


def test_ready_cuda_allocator_metadata_is_bounded_and_optional():
    cuda = SimpleNamespace(memory_allocated=lambda: 2 * 1024 ** 3,
        memory_reserved=lambda: 3 * 1024 ** 3)
    assert worker.cuda_memory_metadata(SimpleNamespace(cuda=cuda)) == {
        "cuda_memory_allocated_mib": 2048, "cuda_memory_reserved_mib": 3072}
    cuda.memory_allocated = lambda: True
    cuda.memory_reserved = lambda: 2 ** 61
    assert worker.cuda_memory_metadata(SimpleNamespace(cuda=cuda)) == {}
    assert worker.cuda_memory_metadata(SimpleNamespace()) == {}


def test_native_worker_aborts_stream_early_when_cancelled():
    config, model, torch, _, advanced = setup_stream([[1, 2], [3, 4], [5, 6], [7, 8]])
    mark_compiled(model)
    releases = []
    torch.cuda.empty_cache = lambda: releases.append(tuple(advanced))
    cancel_flag = False
    stream = worker.synthesize_stream(
        config, model, np, torch, "Привет", is_cancelled=lambda: cancel_flag
    )
    first = next(stream)
    assert np.frombuffer(first[0], dtype="<f4").tolist() == [1, 2]
    assert advanced == [0]
    cancel_flag = True
    assert list(stream) == []
    assert advanced == [0], "cancelled stream must stop without advancing remaining steps"
    assert releases == [(0,)]
