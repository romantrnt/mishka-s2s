import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from app.foundation import api as foundation_api
from app.foundation.runtime import ResourceProbe, S2SRuntimeManager


def run(coro):
    return asyncio.run(coro)


def manager(vram, ram=32.0, profile="production_candidate", minimum_vram=8.0):
    obj = object.__new__(S2SRuntimeManager)
    obj.settings = SimpleNamespace(
        profile=profile,
        min_available_ram_gb=12.0,
        min_available_vram_gb=minimum_vram,
    )
    async def fake_vram():
        return vram
    with patch("app.foundation.runtime.ResourceProbe.available_ram_gb", return_value=ram),          patch("app.foundation.runtime.ResourceProbe.available_vram_gb", side_effect=fake_vram):
        return run(obj.resource_readiness())


def test_resource_readiness_accepts_sufficient_reserve():
    result = manager(9.49)
    assert result["ready"] is True
    assert result["reason"] is None
    assert result["headroom_vram_gb"] == 1.49


def test_resource_readiness_rejects_insufficient_reserve():
    result = manager(5.17)
    assert result["ready"] is False
    assert result["reason"] == "vram_reserve_insufficient"


def test_resource_readiness_requires_gpu_for_production_only():
    result = manager(0.1, profile="mock")
    assert result["ready"] is True
    assert result["minimum_vram_gb"] is None


def test_read_vram_falls_back_to_wsl_nvidia_smi():
    class FakeProc:
        returncode = 0
        async def communicate(self):
            return b"4096\n", b""
        async def wait(self):
            return 0
    seen = []
    async def fake_exec(binary, *args, **kwargs):
        seen.append(binary)
        return FakeProc()
    orig_is_file = Path.is_file
    def fake_is_file(self):
        return True if str(self) == "/usr/lib/wsl/lib/nvidia-smi" else orig_is_file(self)
    with patch("app.foundation.runtime.shutil.which", return_value=None), \
         patch.object(Path, "is_file", fake_is_file), \
         patch("app.foundation.runtime.asyncio.create_subprocess_exec", side_effect=fake_exec):
        vram = run(ResourceProbe._read_vram())
    assert seen == ["/usr/lib/wsl/lib/nvidia-smi"]
    assert vram == 4.0


def test_whisper_loader_prefers_local_files_only_and_falls_back():
    calls = []
    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))
            if kwargs.get("local_files_only") and len(calls) == 1:
                raise FileNotFoundError("not cached yet")
    fake_mod = ModuleType("faster_whisper")
    fake_mod.WhisperModel = FakeWhisperModel
    settings = SimpleNamespace(profile="local_gpu", stt_model="large-v3-turbo")
    with patch.dict(sys.modules, {"faster_whisper": fake_mod}):
        model = foundation_api._whisper_loader(settings)
    assert isinstance(model, FakeWhisperModel)
    assert calls == [
        ("large-v3-turbo", {"device": "cuda", "compute_type": "float16", "local_files_only": True}),
        ("large-v3-turbo", {"device": "cuda", "compute_type": "float16"}),
    ]
