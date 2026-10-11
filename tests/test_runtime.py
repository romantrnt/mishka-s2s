import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.foundation.runtime import S2SRuntimeManager


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
