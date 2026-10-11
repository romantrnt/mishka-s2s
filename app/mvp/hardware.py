from __future__ import annotations

from .types import HardwareEvent


class MockHardwareAdapter:
    """Accepts documented payloads and records outbound commands instead of using UART."""
    def __init__(self):
        self.commands: list[dict] = []

    def parse(self, payload: dict) -> HardwareEvent:
        return HardwareEvent.from_dict(payload)

    def send(self, command: dict) -> None:
        self.commands.append(command)


class MockLEDController:
    def __init__(self, hardware: MockHardwareAdapter):
        self.hardware = hardware

    def show(self, pattern: str, color: str = "yellow", duration_ms: int = 800) -> dict:
        command = {"action": "led_pattern", "pattern": pattern, "color": color, "duration_ms": duration_ms}
        self.hardware.send(command)
        return command

    def clear(self) -> dict:
        command = {"action": "led_clear"}
        self.hardware.send(command)
        return command


class MockAudioOutput:
    """Text/mock audio output. Stale tokens are discarded after barge-in/cancel."""
    def __init__(self):
        self.active_token = 0
        self.queue: list[dict] = []

    def speak(self, response_id: str, text: str, audio_asset: str | None, token: int) -> dict | None:
        if token != self.active_token:
            return None
        output = {"action": "audio", "mode": "text/mock", "response_id": response_id, "text": text, "audio_asset": audio_asset}
        self.queue.append(output)
        return output

    def cancel(self, token: int) -> None:
        self.active_token = token
        self.queue.clear()

    def clear(self) -> None:
        self.queue.clear()


class OptionalLocalTTSOutput(MockAudioOutput):
    """Adapter for a future local TTS callable; safe text/mock fallback is default.

    ``synthesizer`` is injected by a production composition root, so importing
    this MVP never imports XTTS or loads a model.
    """
    def __init__(self, synthesizer=None):
        super().__init__()
        self.synthesizer = synthesizer

    def speak(self, response_id: str, text: str, audio_asset: str | None, token: int) -> dict | None:
        output = super().speak(response_id, text, audio_asset, token)
        if output is None:
            return None
        if self.synthesizer is not None:
            output["mode"] = "local_tts"
            output["generated_audio"] = self.synthesizer(text)
        return output
