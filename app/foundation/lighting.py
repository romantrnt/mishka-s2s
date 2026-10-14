"""Canonical lighting contract: element id + binary state + transition."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence


LIGHTING_ELEMENT_IDS: tuple[int, ...] = (
    10, 11, 20, 21, 30, 31, 40, 41,
    50, 51, 60, 61, 62, 70, 71, 72,
)

BUTTON_DIGIT_ELEMENTS: Mapping[int, tuple[int, int]] = {
    1: (10, 11), 2: (20, 21), 3: (30, 31), 4: (40, 41),
    5: (50, 51), 6: (60, 61), 7: (70, 71),
}

BUTTON_OUTPUT_ELEMENTS: Mapping[int, tuple[int, ...]] = {
    1: (10, 11), 2: (20, 21), 3: (30, 31), 4: (40, 41),
    5: (50, 51), 6: (60, 61, 62), 7: (70, 71, 72),
}

ELEMENT_TO_BUTTON: Mapping[int, int] = {
    element_id: button_id
    for button_id, element_ids in BUTTON_OUTPUT_ELEMENTS.items()
    for element_id in element_ids
}

READINESS_WAVE_GROUPS: tuple[tuple[int, ...], ...] = (
    (10, 11), (20, 21), (30, 31), (40, 41),
    (50, 51), (60, 61, 62), (70, 71, 72),
)
READINESS_WAVE_DURATION_MS = 1200


def _element_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("element_id must be an integer")
    if value not in LIGHTING_ELEMENT_IDS:
        raise ValueError(f"unknown lighting element {value}")
    return value


def _state(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValueError("lighting state must be 0 or 1")
    return value


def _transition(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("transition_ms must be a non-negative integer")
    return value


@dataclass(frozen=True)
class LightingCommand:
    element_id: int
    state: int
    transition_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "element_id", _element_id(self.element_id))
        object.__setattr__(self, "state", _state(self.state))
        object.__setattr__(self, "transition_ms", _transition(self.transition_ms))

    def to_wire(self) -> bytes:
        return f"{self.element_id},{self.state},{self.transition_ms}\n".encode("ascii")

    def as_dict(self) -> dict[str, int]:
        return {"element_id": self.element_id, "state": self.state, "transition_ms": self.transition_ms}


@dataclass
class LightingState:
    elements: dict[int, int] = field(default_factory=lambda: {
        element_id: 0 for element_id in LIGHTING_ELEMENT_IDS
    })

    def __post_init__(self) -> None:
        values = dict(self.elements)
        unknown = set(values) - set(LIGHTING_ELEMENT_IDS)
        missing = set(LIGHTING_ELEMENT_IDS) - set(values)
        if unknown:
            raise ValueError(f"unknown lighting elements: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing lighting elements: {sorted(missing)}")
        self.elements = {element_id: _state(values[element_id]) for element_id in LIGHTING_ELEMENT_IDS}

    @classmethod
    def from_mapping(cls, values: Mapping[int | str, int]) -> "LightingState":
        normalized: dict[int, int] = {}
        for raw_id, state in values.items():
            if isinstance(raw_id, bool):
                raise ValueError("lighting element ids must be integers")
            try:
                element_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("lighting element ids must be integers") from exc
            if not isinstance(raw_id, int) and str(raw_id) != str(element_id):
                raise ValueError("lighting element ids must be integers")
            normalized[element_id] = state
        return cls(normalized)

    def apply(self, command: LightingCommand) -> None:
        if not isinstance(command, LightingCommand):
            raise TypeError("expected LightingCommand")
        self.elements[command.element_id] = command.state

    def snapshot(self) -> dict[str, int]:
        return {str(element_id): self.elements[element_id] for element_id in LIGHTING_ELEMENT_IDS}


class LightingOutput(Protocol):
    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> None: ...
    def set_elements(self, commands: Sequence[LightingCommand]) -> None: ...
    def clear_all(self, transition_ms: int = 0) -> None: ...


class FakeLightingOutput:
    def __init__(self) -> None:
        self.state = LightingState()
        self.commands: list[LightingCommand] = []
        self.batches: list[tuple[LightingCommand, ...]] = []

    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> None:
        self.set_elements((LightingCommand(element_id, state, transition_ms),))

    def set_elements(self, commands: Sequence[LightingCommand]) -> None:
        validated = tuple(command if isinstance(command, LightingCommand) else LightingCommand(**command) for command in commands)
        for command in validated:
            self.state.apply(command)
        self.batches.append(validated)
        self.commands.extend(validated)

    def clear_all(self, transition_ms: int = 0) -> None:
        self.set_elements(tuple(LightingCommand(element_id, 0, transition_ms) for element_id in LIGHTING_ELEMENT_IDS))

    def snapshot(self) -> dict[str, int]:
        return self.state.snapshot()


class LightingController:
    def __init__(self, output: LightingOutput):
        self.output = output

    def show_digit(self, button_id: int, state: int, transition_ms: int = 0) -> tuple[LightingCommand, ...]:
        """Compatibility helper: explicitly expand a button's fill and digit."""
        if button_id not in BUTTON_DIGIT_ELEMENTS:
            raise ValueError(f"unknown button {button_id}")
        commands = tuple(LightingCommand(element_id, state, transition_ms) for element_id in BUTTON_DIGIT_ELEMENTS[button_id])
        self.output.set_elements(commands)
        return commands

    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> LightingCommand:
        """Set exactly one canonical element; never expand to a button."""
        command = LightingCommand(element_id, state, transition_ms)
        self.output.set_element(command.element_id, command.state, command.transition_ms)
        return command

    def set_elements(self, commands: Sequence[LightingCommand]) -> tuple[LightingCommand, ...]:
        """Apply exactly the explicitly supplied canonical elements."""
        validated = tuple(command if isinstance(command, LightingCommand) else LightingCommand(**command) for command in commands)
        self.output.set_elements(validated)
        return validated

    def show_button(self, button_id: int, state: int, transition_ms: int = 0) -> tuple[LightingCommand, ...]:
        """Compatibility helper: explicitly expand one whole physical button."""
        if button_id not in BUTTON_OUTPUT_ELEMENTS:
            raise ValueError(f"unknown button {button_id}")
        commands = tuple(LightingCommand(element_id, state, transition_ms) for element_id in BUTTON_OUTPUT_ELEMENTS[button_id])
        self.output.set_elements(commands)
        return commands

    def _readiness_wave_batches(self, total_duration_ms: int) -> tuple[tuple[LightingCommand, ...], ...]:
        total_duration_ms = _transition(total_duration_ms)
        if total_duration_ms <= 0:
            raise ValueError("readiness wave duration must be positive")
        base, remainder = divmod(total_duration_ms, len(READINESS_WAVE_GROUPS))
        return tuple(
            tuple(
                LightingCommand(element_id, 1, base + (1 if index < remainder else 0))
                for element_id in group
            )
            for index, group in enumerate(READINESS_WAVE_GROUPS)
        )

    def readiness_wave(self, total_duration_ms: int = READINESS_WAVE_DURATION_MS) -> tuple[LightingCommand, ...]:
        emitted: list[LightingCommand] = []
        for commands in self._readiness_wave_batches(total_duration_ms):
            self.output.set_elements(commands)
            emitted.extend(commands)
        return tuple(emitted)

    async def readiness_wave_async(self, total_duration_ms: int = READINESS_WAVE_DURATION_MS, *, stage_hook=None, cancelled=None) -> tuple[LightingCommand, ...]:
        batches = self._readiness_wave_batches(total_duration_ms)
        emitted: list[LightingCommand] = []
        for index, commands in enumerate(batches):
            if callable(cancelled) and cancelled():
                break
            self.output.set_elements(commands)
            emitted.extend(commands)
            if stage_hook is not None:
                result = stage_hook(index, commands)
                if inspect.isawaitable(result):
                    await result
            if index + 1 < len(batches):
                await asyncio.sleep(commands[0].transition_ms / 1000)
        return tuple(emitted)

    def clear_all(self, transition_ms: int = 0) -> None:
        self.output.clear_all(transition_ms)


def show_digit(output: LightingOutput, button_id: int, state: int, transition_ms: int = 0) -> tuple[LightingCommand, ...]:
    return LightingController(output).show_digit(button_id, state, transition_ms)


EXHIBITION_BREATHING_MIN_PERCENT = 30
EXHIBITION_BREATHING_MAX_PERCENT = 80
EXHIBITION_BREATHING_PERIOD_MS = 2000
EXHIBITION_BREATHING_TICK_MS = 100
EXHIBITION_BREATHING_PHASE_MS = 1000
EXHIBITION_PWM_PERIOD_MS = 1000
EXHIBITION_PWM_SLOTS = 10
EXHIBITION_WAVE_STAGE_MS = 120
EXHIBITION_WAVE_GROUPS: tuple[tuple[int, ...], ...] = READINESS_WAVE_GROUPS
EXHIBITION_DIGIT_WAVE_GROUPS: tuple[tuple[int, ...], ...] = tuple(BUTTON_DIGIT_ELEMENTS[button_id] for button_id in range(1, 8))


def quantize_brightness_percent(value: float, *, slots: int = EXHIBITION_PWM_SLOTS) -> tuple[float, int]:
    """Convert logical brightness to binary duty-cycle evidence."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("brightness must be numeric")
    if not 0.0 <= float(value) <= 100.0:
        raise ValueError("brightness must be within 0..100 percent")
    if isinstance(slots, bool) or not isinstance(slots, int) or slots <= 0:
        raise ValueError("brightness slots must be a positive integer")
    on_slots = max(0, min(slots, int(round(float(value) / 100.0 * slots))))
    return (round(on_slots * 100.0 / slots, 6), on_slots)


def _triangular_brightness(elapsed_ms: float) -> float:
    period = float(EXHIBITION_BREATHING_PERIOD_MS)
    phase = (max(0.0, float(elapsed_ms)) % period) / period
    triangle = phase * 2.0 if phase <= 0.5 else (1.0 - phase) * 2.0
    return EXHIBITION_BREATHING_MIN_PERCENT + (EXHIBITION_BREATHING_MAX_PERCENT - EXHIBITION_BREATHING_MIN_PERCENT) * triangle


class ExhibitionLightingEngine:
    """Single owner for exhibition breathing, wave and effect generations."""

    def __init__(
        self,
        output: LightingOutput,
        *,
        monotonic_now=time.monotonic,
        background_scheduler: bool = True,
        frame_hook=None,
    ) -> None:
        self.output = output
        self.monotonic_now = monotonic_now
        self.frame_hook = frame_hook
        # Production uses a cancellable wakeup poll. Canonical effect timing
        # never comes from the sleep duration: advance() reads monotonic_now.
        # Deterministic simulator runs disable this poll and call advance()
        # directly from the fake clock owner.
        self.background_scheduler = bool(background_scheduler)
        self.generation = 0
        self.effect = "idle"
        self.started_at_monotonic: float | None = None
        self.last_emit_key: tuple | None = None
        self.wave_stage = -1
        self.frame_count = 0
        self.last_frame: dict[str, object] | None = None
        self.last_error: dict[str, object] | None = None
        self._task: asyncio.Task[object] | None = None
        self._pwm_phase_origin: float | None = None
        self._wave_groups: tuple[tuple[int, ...], ...] = EXHIBITION_WAVE_GROUPS
        self._wave_hold_on_complete = True

    @property
    def task_active(self) -> bool:
        return self._task is not None and not self._task.done()

    def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _schedule(self, generation: int) -> None:
        if not self.background_scheduler:
            return
        if self.task_active:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = asyncio.create_task(self._run(generation), name=f"mishka-led-effect-{generation}")

    async def _run(self, generation: int) -> None:
        try:
            while generation == self.generation and self.effect in {"breathing", "wave"}:
                self.advance()
                # Non-authoritative production wakeup only. The injected
                # monotonic clock remains the sole effect-time source.
                await asyncio.sleep(EXHIBITION_BREATHING_TICK_MS / 1000.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A physical serial failure must not become an unhandled asyncio
            # task exception. Keep the effect state visible in diagnostics;
            # the next canonical effect/reset can reconnect the adapter.
            self.last_error = {
                "exception_type": type(exc).__name__,
                "detail": str(exc)[:200],
                "generation": generation,
            }
            self.last_frame = {
                "effect": self.effect,
                "error": "lighting_output_failed",
                "error_detail": str(exc)[:200],
            }
            self._notify_frame(dict(self.last_frame))
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    @staticmethod
    def _consume_frame_hook_task(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    def _notify_frame(self, frame: dict[str, object]) -> None:
        hook = self.frame_hook
        if not callable(hook):
            return
        try:
            result = hook(dict(frame))
        except Exception:
            return
        if not inspect.isawaitable(result):
            return
        try:
            task = asyncio.create_task(result)
        except RuntimeError:
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return
        task.add_done_callback(self._consume_frame_hook_task)

    def _emit(self, commands: tuple[LightingCommand, ...], frame: dict[str, object]) -> dict[str, object]:
        self.output.set_elements(commands)
        self.frame_count += 1
        self.last_frame = {**frame, "frame_index": self.frame_count, "commands": len(commands)}
        result = dict(self.last_frame)
        self._notify_frame(result)
        return result

    def _all_binary_commands(self, state: int, transition_ms: int = 0) -> tuple[LightingCommand, ...]:
        return tuple(LightingCommand(element_id, state, transition_ms) for element_id in LIGHTING_ELEMENT_IDS)

    def _start_generation(self, effect: str, now: float) -> int:
        self._cancel_task()
        self.generation += 1
        self.effect = effect
        self.started_at_monotonic = float(now)
        self._pwm_phase_origin = float(now)
        self.last_emit_key = None
        self.wave_stage = -1
        self._wave_groups = EXHIBITION_WAVE_GROUPS
        self._wave_hold_on_complete = True
        return self.generation

    def start_breathing(self, *, now: float | None = None) -> dict[str, object]:
        now = self.monotonic_now() if now is None else float(now)
        if self.effect == "breathing" and self.started_at_monotonic is not None:
            self._schedule(self.generation)
            return self.snapshot()
        generation = self._start_generation("breathing", now)
        self.output.clear_all()
        self.advance(now, force=True)
        self._schedule(generation)
        return self.snapshot()

    def start_wave(
        self,
        *,
        now: float | None = None,
        groups: Sequence[Sequence[int]] | None = None,
        hold_on_complete: bool = True,
    ) -> dict[str, object]:
        now = self.monotonic_now() if now is None else float(now)
        normalized = tuple(tuple(_element_id(int(element_id)) for element_id in group) for group in (groups or EXHIBITION_WAVE_GROUPS))
        if not normalized:
            raise ValueError("wave groups must not be empty")
        generation = self._start_generation("wave", now)
        self._wave_groups = normalized
        self._wave_hold_on_complete = bool(hold_on_complete)
        self.output.clear_all()
        self.advance(now, force=True)
        self._schedule(generation)
        return self.snapshot()

    def cancel_effects(self, *, clear: bool = True) -> dict[str, object]:
        self._cancel_task()
        self.generation += 1
        self.effect = "idle"
        self.started_at_monotonic = None
        self._pwm_phase_origin = None
        self.last_emit_key = None
        self.wave_stage = -1
        self._wave_groups = EXHIBITION_WAVE_GROUPS
        self._wave_hold_on_complete = True
        if clear:
            self.output.clear_all()
        return self.snapshot()

    def reset_to_standby(self, *, now: float | None = None) -> dict[str, object]:
        self.cancel_effects(clear=True)
        return self.start_breathing(now=now)

    def advance(self, now: float | None = None, *, force: bool = False) -> dict[str, object] | None:
        if self.effect == "idle" or self.started_at_monotonic is None:
            return None
        now = self.monotonic_now() if now is None else float(now)
        elapsed_ms = max(0.0, (now - self.started_at_monotonic) * 1000.0)
        if self.effect == "breathing":
            # The physical board has no brightness/PWM field. Its third CSV
            # field is the transition duration, so standby breathing is a
            # pair of binary target states with a one-second fade each:
            #   all 0 -> all 1 over 1000 ms -> all 0 over 1000 ms -> ...
            phase = int(elapsed_ms // EXHIBITION_BREATHING_PHASE_MS)
            state = 1 if phase % 2 == 0 else 0
            transition_ms = EXHIBITION_BREATHING_PHASE_MS
            key = (phase, state)
            if not force and key == self.last_emit_key:
                return None
            self.last_emit_key = key
            return self._emit(
                self._all_binary_commands(state, transition_ms),
                {
                    "effect": "breathing",
                    "protocol": "binary_transition",
                    "at_monotonic": now,
                    "state": state,
                    "transition_ms": transition_ms,
                    # These fields are retained as presentation-compatible
                    # metadata, but now describe the binary target rather
                    # than an invented PWM brightness value.
                    "brightness_percent": 100 if state else 0,
                    "requested_brightness_percent": 100 if state else 0,
                    "on_slots": EXHIBITION_PWM_SLOTS if state else 0,
                    "pwm_slots": None,
                },
            )


        groups = self._wave_groups
        stage = int(elapsed_ms // EXHIBITION_WAVE_STAGE_MS)
        if stage >= len(groups):
            if self.wave_stage != len(groups):
                self.wave_stage = len(groups)
                active = {element_id for group in groups for element_id in group}
                if self._wave_hold_on_complete:
                    self.effect = "wave_hold"
                    commands = tuple(
                        LightingCommand(element_id, 1 if element_id in active else 0, EXHIBITION_WAVE_STAGE_MS)
                        for element_id in LIGHTING_ELEMENT_IDS
                    )
                    return self._emit(commands, {
                        "effect": "wave_complete",
                        "at_monotonic": now,
                        "wave_stage": self.wave_stage,
                        "wave_total_stages": len(groups),
                        "active_elements": sorted(active),
                        "held": True,
                        "brightness_percent": 100,
                    })
                self.effect = "idle"
                return self._emit(self._all_binary_commands(0), {"effect": "wave_complete", "at_monotonic": now, "wave_stage": self.wave_stage})
            return None
        if not force and stage == self.wave_stage:
            return None
        self.wave_stage = stage
        active = set(groups[stage])
        commands = tuple(LightingCommand(element_id, 1 if element_id in active else 0, EXHIBITION_WAVE_STAGE_MS) for element_id in LIGHTING_ELEMENT_IDS)
        return self._emit(commands, {"effect": "wave", "at_monotonic": now, "wave_stage": stage + 1, "wave_total_stages": len(groups), "active_elements": sorted(active), "brightness_percent": 100})

    def snapshot(self) -> dict[str, object]:
        frame = dict(self.last_frame or {})
        return {"active": self.effect != "idle", "effect": self.effect, "generation": self.generation, "clock": "monotonic_injected", "task_active": self.task_active, "scheduler": "background_wakeup" if self.background_scheduler else "manual_fake_clock", "wave_stage": self.wave_stage, "frame_count": self.frame_count, "state": frame.get("state"), "transition_ms": frame.get("transition_ms"), "protocol": frame.get("protocol"), "brightness_percent": frame.get("brightness_percent"), "requested_brightness_percent": frame.get("requested_brightness_percent"), "on_slots": frame.get("on_slots"), "pwm_slots": frame.get("pwm_slots"), "elements": list(LIGHTING_ELEMENT_IDS), "last_frame": frame, "last_error": dict(self.last_error) if self.last_error else None}



class SerialLightingAdapter:
    """Physical lighting wire adapter.

    This is separate from the legacy JSONL/CRC32 simulator transport. The
    only bytes written here are canonical CSV/LF lighting lines:
    <element_id>,<state>,<transition_ms>\n. The word "binary" in older
    notes describes the state field (0/1), not the wire framing.
    """

    def __init__(self, path: str, *, baudrate: int = 115200,
                 timeout_s: float = 1.0, transport=None) -> None:
        if not path:
            raise ValueError("lighting serial path is required")
        self.path = str(path)
        self.baudrate = int(baudrate)
        self.timeout_s = max(0.01, float(timeout_s))
        self.transport = transport
        self._lock = threading.Lock()
        self._ready = False
        self._closed = False
        self._sent = 0
        self._failed = 0
        self._last_error: dict | None = None
        self._last_open_at: float | None = None

    def _make_transport(self):
        if self.transport is not None:
            return self.transport
        from .hardware import PosixSerialTransport
        return PosixSerialTransport(self.path, baud=self.baudrate)

    def open(self) -> None:
        with self._lock:
            if self._ready:
                return
            if self.transport is None:
                self.transport = self._make_transport()
            self.transport.open()
            self._ready = True
            self._closed = False
            self._last_error = None
            self._last_open_at = time.time()

    def close(self) -> None:
        with self._lock:
            self._ready = False
            self._closed = True
            if self.transport is not None:
                try:
                    self.transport.close()
                except Exception:
                    pass

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    def send(self, command: LightingCommand) -> None:
        if not isinstance(command, LightingCommand):
            command = LightingCommand(**command)
        self.send_batch((command,))

    def send_batch(self, commands: Sequence[LightingCommand]) -> None:
        validated = tuple(
            command if isinstance(command, LightingCommand) else LightingCommand(**command)
            for command in commands
        )
        with self._lock:
            if not self.is_ready():
                raise ConnectionError("lighting serial adapter is not ready")
            try:
                for command in validated:
                    self.transport.write(command.to_wire())
                    self._sent += 1
            except Exception as exc:
                self._failed += 1
                self._ready = False
                self._last_error = {
                    "exception_type": type(exc).__name__,
                    "detail": str(exc)[:200],
                }
                raise ConnectionError("lighting serial write failed") from exc

    def reconnect(self) -> None:
        self.close()
        self.open()

    def diagnostics(self) -> dict:
        return {
            "ready": self.is_ready(),
            "backend": "serial",
            "path": self.path,
            "baudrate": self.baudrate,
            "timeout_s": self.timeout_s,
            "sent_commands": self._sent,
            "failed_commands": self._failed,
            "last_error": self._last_error,
            "last_open_at": self._last_open_at,
        }


class PhysicalLightingOutput:
    """Canonical state owner backed by ``SerialLightingAdapter``."""

    def __init__(self, adapter: SerialLightingAdapter) -> None:
        self.adapter = adapter
        self.state = LightingState()
        self._generation = 0
        self._lock = threading.Lock()

    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> None:
        self.set_elements((LightingCommand(element_id, state, transition_ms),))

    def set_elements(self, commands: Sequence[LightingCommand]) -> None:
        validated = tuple(
            command if isinstance(command, LightingCommand) else LightingCommand(**command)
            for command in commands
        )
        with self._lock:
            generation = self._generation
            if hasattr(self.adapter, "is_ready") and not self.adapter.is_ready():
                reconnect = getattr(self.adapter, "reconnect", None)
                if not callable(reconnect):
                    raise ConnectionError("lighting serial adapter is not ready")
                reconnect()
            self.adapter.send_batch(validated)
            if generation != self._generation:
                return
            for command in validated:
                self.state.apply(command)

    def clear_all(self, transition_ms: int = 0) -> None:
        self.set_elements(tuple(
            LightingCommand(element_id, 0, transition_ms)
            for element_id in LIGHTING_ELEMENT_IDS
        ))

    def invalidate_pending(self) -> None:
        with self._lock:
            self._generation += 1

    def snapshot(self) -> dict[str, int]:
        return self.state.snapshot()

    def diagnostics(self) -> dict:
        return {"backend": "serial", "state": self.snapshot(), **self.adapter.diagnostics()}
