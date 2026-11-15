import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.foundation.stand import HardwareIntegrationStand


def test_hid_loss_dispatches_fault_without_blocking_loop_on_capture_driver():
    async def scenario():
        stand = HardwareIntegrationStand.__new__(HardwareIntegrationStand)
        stand._hid_generation = 1
        stand.settings = SimpleNamespace(audio_cleanup_timeout_s=.03)
        entered, release = threading.Event(), threading.Event()
        calls = []
        def stop():
            calls.append("capture")
            entered.set()
            assert release.wait(2)
        async def receive(event):
            calls.append(event.event_type)
            return {"outputs": []}
        stand.audio_input = SimpleNamespace(stop=stop)
        stand.application = SimpleNamespace(receive_async=receive)
        task = asyncio.create_task(stand._handle_hid_loss("test", 1))
        try:
            await asyncio.sleep(.01)
            assert calls[0] == "HardwareDisconnected"
            assert entered.is_set()
            with pytest.raises(asyncio.TimeoutError):
                await task
            # Repeated cleanup joins the same driver call, never adds workers.
            with pytest.raises(asyncio.TimeoutError):
                await stand._stop_headless_audio_capture()
            assert calls.count("capture") == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            pending = getattr(stand, "_capture_stop_task", None)
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["led", "capture", "hid", "serial", "uart"])
def test_partial_close_failure_does_not_abandon_remaining_resources(failure):
    stand = HardwareIntegrationStand.__new__(HardwareIntegrationStand)
    calls = []
    def record(name):
        def call(*args, **kwargs):
            calls.append(name)
            if name == failure:
                raise OSError("synthetic cleanup failure")
        return call
    stand._closed = False
    stand.application = SimpleNamespace(cancel_identity_work=record("identity"),
        led=SimpleNamespace(cancel_effects=record("led")))
    stand.audio_input = SimpleNamespace(stop=record("capture"))
    stand.input_adapter = SimpleNamespace(close=record("hid"))
    stand.lighting_output = SimpleNamespace(close=record("lighting"))
    stand.lighting_serial = SimpleNamespace(close=record("serial"))
    stand.adapter = SimpleNamespace(close=record("uart"))
    stand._uart_executor = SimpleNamespace(shutdown=record("uart_executor"))
    stand._audio_executor = SimpleNamespace(shutdown=record("audio_executor"))
    stand.pty = SimpleNamespace(close=record("pty"))
    stand._cancel_story_audio = record("story")
    stand._cancel_response_audio = record("response")
    with pytest.raises(OSError):
        stand.close()
    assert set(calls) == {"identity", "led", "capture", "hid", "lighting", "serial",
                          "uart", "uart_executor", "audio_executor", "pty", "story", "response"}
    before = list(calls)
    stand.close()
    assert calls == before


def test_pending_capture_stop_cannot_partially_replace_hid_on_restart():
    async def scenario():
        stand = HardwareIntegrationStand.__new__(HardwareIntegrationStand)
        stand.hardware_mode = "physical"
        stand._hid_generation = 7
        stand._capture_hid_fault = 7
        stand._headless_audio_task = None
        stand._headless_audio_status = {}
        stand.settings = SimpleNamespace(audio_cleanup_timeout_s=.01)
        release = threading.Event()
        stand.audio_input = SimpleNamespace(stop=lambda: release.wait(2))
        old_adapter = stand.input_adapter = object()
        try:
            with pytest.raises(asyncio.TimeoutError):
                await stand._stop_capture_device(stand.audio_input)
            for _ in range(2):
                with pytest.raises(RuntimeError, match="cleanup is still pending"):
                    await stand.start_hardware()
                assert stand.input_adapter is old_adapter
                assert stand._hid_generation == stand._capture_hid_fault == 7
        finally:
            release.set()
            await stand._capture_stop_task
    asyncio.run(scenario())
