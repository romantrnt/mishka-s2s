"""Canonical lighting contract: element id + binary state + transition."""

from __future__ import annotations

import asyncio
from collections import deque
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence


LIGHTING_ELEMENT_IDS: tuple[int, ...] = tuple(range(18))

# Physical channel names are flat: 18 LED channels, IDs 0..17.
BUTTON_FILL_ELEMENTS: Mapping[int, int] = {
    1: 14, 2: 13, 3: 10, 4: 9, 5: 6, 6: 3, 7: 1,
}
BUTTON_DIGIT_ELEMENTS: Mapping[int, tuple[int, int]] = {
    1: (14, 15), 2: (13, 12), 3: (10, 11), 4: (9, 8),
    5: (6, 7), 6: (3, 2), 7: (1, 0),
}
PICTOGRAM_ELEMENTS: Mapping[int, int] = {6: 4, 7: 17}
COMPENSATION_ELEMENTS: Mapping[int, int] = {6: 5, 7: 16}

BUTTON_OUTPUT_ELEMENTS: Mapping[int, tuple[int, ...]] = {
    button_id: BUTTON_DIGIT_ELEMENTS[button_id]
    + ((PICTOGRAM_ELEMENTS[button_id],) if button_id in PICTOGRAM_ELEMENTS else ())
    for button_id in BUTTON_FILL_ELEMENTS
}
BUTTON_WAVE_ELEMENTS: Mapping[int, tuple[int, ...]] = {
    button_id: BUTTON_OUTPUT_ELEMENTS[button_id]
    + ((COMPENSATION_ELEMENTS[button_id],) if button_id in COMPENSATION_ELEMENTS else ())
    for button_id in BUTTON_FILL_ELEMENTS
}

ELEMENT_TO_BUTTON: Mapping[int, int] = {
    element_id: button_id
    for button_id, element_ids in BUTTON_OUTPUT_ELEMENTS.items()
    for element_id in element_ids
}
ELEMENT_TO_BUTTON = {
    **ELEMENT_TO_BUTTON,
    **{element_id: button_id for button_id, element_id in COMPENSATION_ELEMENTS.items()},
}

READINESS_WAVE_GROUPS: tuple[tuple[int, ...], ...] = (
    BUTTON_WAVE_ELEMENTS[1], BUTTON_WAVE_ELEMENTS[2], BUTTON_WAVE_ELEMENTS[3],
    BUTTON_WAVE_ELEMENTS[4], BUTTON_WAVE_ELEMENTS[5], BUTTON_WAVE_ELEMENTS[6],
    BUTTON_WAVE_ELEMENTS[7],
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
# Age prompt: only the seven digit LEDs remain lit; fill, pictogram, and compensation channels stay dark.
EXHIBITION_DIGIT_WAVE_GROUPS: tuple[tuple[int, ...], ...] = tuple((BUTTON_DIGIT_ELEMENTS[button_id][1],) for button_id in range(1, 8))
# Menu wave: all user-facing channels only. Compensation channels 5 and 16
# are intentionally excluded; they only fill the large pictogram footprint
# while a single button is lit and must not be part of the final menu state.
EXHIBITION_MENU_WAVE_GROUPS: tuple[tuple[int, ...], ...] = tuple(
    BUTTON_OUTPUT_ELEMENTS[button_id] for button_id in range(1, 8)
)


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
        self._breathing_elements: frozenset[int] = frozenset(LIGHTING_ELEMENT_IDS)
        self._breathing_base_elements: frozenset[int] = frozenset()
        self.breathing_phase = "breathing"
        self._wave_groups: tuple[tuple[int, ...], ...] = EXHIBITION_WAVE_GROUPS
        self._follow_up_groups: tuple[tuple[int, ...], ...] | None = None
        self._wave_base_elements: frozenset[int] = frozenset()
        self._wave_loop = False
        self._wave_stage_ms = EXHIBITION_WAVE_STAGE_MS
        self.wave_phase = "idle"
        self._wave_hold_on_complete = True
        self._wave_final_commands: tuple[LightingCommand, ...] | None = None
        self._profile_ready = False
        self._profile_stage_at: float | None = None

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
                wakeup_s = EXHIBITION_BREATHING_TICK_MS / 1000.0
                if self._profile_ready and self._profile_stage_at is not None:
                    # Wake at the owned stage boundary rather than rounding
                    # every 120 ms dwell up to two 100 ms polling intervals.
                    remaining = self._profile_stage_at + self._wave_stage_ms / 1000.0 - self.monotonic_now()
                    wakeup_s = min(wakeup_s, max(0.001, remaining))
                await asyncio.sleep(wakeup_s)
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

    def _binary_commands(
        self, elements: frozenset[int], base_elements: frozenset[int], state: int, transition_ms: int,
    ) -> tuple[LightingCommand, ...]:
        active = base_elements if state == 0 else elements | base_elements
        return tuple(
            LightingCommand(element_id, 1 if element_id in active else 0, transition_ms)
            for element_id in LIGHTING_ELEMENT_IDS
        )

    def _start_generation(self, effect: str, now: float) -> int:
        self._cancel_task()
        invalidate = getattr(self.output, "invalidate_pending", None)
        if callable(invalidate):
            invalidate()
        self.generation += 1
        self.effect = effect
        self.started_at_monotonic = float(now)
        self._pwm_phase_origin = float(now)
        self.last_emit_key = None
        self.wave_stage = -1
        self._wave_groups = EXHIBITION_WAVE_GROUPS
        self._follow_up_groups = None
        self.wave_phase = "idle"
        self._wave_hold_on_complete = True
        self._wave_base_elements = frozenset()
        self._wave_loop = False
        self._wave_stage_ms = EXHIBITION_WAVE_STAGE_MS
        self._breathing_elements = frozenset(LIGHTING_ELEMENT_IDS)
        self._breathing_base_elements = frozenset()
        self.breathing_phase = "breathing"
        self._profile_ready = False
        self._profile_stage_at = None
        return self.generation

    def start_breathing(
        self, *, now: float | None = None, elements: Sequence[int] | None = None,
        base_elements: Sequence[int] | None = None, phase: str = "breathing",
        preserve_current: bool = False,
    ) -> dict[str, object]:
        now = self.monotonic_now() if now is None else float(now)
        normalized_elements = frozenset(_element_id(int(element_id)) for element_id in (elements or LIGHTING_ELEMENT_IDS))
        normalized_base = frozenset(_element_id(int(element_id)) for element_id in (base_elements or ()))
        if self.effect == "breathing" and self.started_at_monotonic is not None:
            self._breathing_elements = normalized_elements
            self._breathing_base_elements = normalized_base
            self.breathing_phase = str(phase or "breathing")
            self._schedule(self.generation)
            return self.snapshot()
        generation = self._start_generation("breathing", now)
        self._breathing_elements = normalized_elements
        self._breathing_base_elements = normalized_base
        self.breathing_phase = str(phase or "breathing")
        if not preserve_current:
            self.output.clear_all()
        self.advance(now, force=True)
        self._schedule(generation)
        return self.snapshot()

    def start_wave(
        self,
        *,
        now: float | None = None,
        groups: Sequence[Sequence[int]] | None = None,
        follow_up_groups: Sequence[Sequence[int]] | None = None,
        hold_on_complete: bool = True,
        loop: bool = False,
        base_elements: Sequence[int] | None = None,
        stage_duration_ms: int | None = None,
        preserve_current: bool = False,
    ) -> dict[str, object]:
        now = self.monotonic_now() if now is None else float(now)
        normalized = tuple(tuple(_element_id(int(element_id)) for element_id in group) for group in (groups or EXHIBITION_WAVE_GROUPS))
        if not normalized:
            raise ValueError("wave groups must not be empty")
        generation = self._start_generation("wave", now)
        self._wave_groups = normalized
        self._follow_up_groups = (
            tuple(tuple(_element_id(int(element_id)) for element_id in group) for group in follow_up_groups)
            if follow_up_groups is not None else None
        )
        self._wave_base_elements = frozenset(_element_id(int(element_id)) for element_id in (base_elements or ()))
        self._wave_loop = bool(loop)
        self._wave_stage_ms = max(20, int(stage_duration_ms or EXHIBITION_WAVE_STAGE_MS))
        self.wave_phase = "primary"
        self._wave_hold_on_complete = bool(hold_on_complete)
        if not preserve_current:
            self.output.clear_all()
        self.advance(now, force=True)
        self._schedule(generation)
        return self.snapshot()

    def start_game_ripple(self, origin_button: int, *, now: float | None = None) -> dict[str, object]:
        """A bounded two-front ripple in logical button order, never PWM."""
        if type(origin_button) is not int or origin_button not in BUTTON_FILL_ELEMENTS:
            raise ValueError("invalid ripple origin")
        rings = tuple(tuple(BUTTON_FILL_ELEMENTS[button] for button in range(1, 8)
                            if abs(button - origin_button) == distance)
                      for distance in range(max(origin_button - 1, 7 - origin_button) + 1))
        # The previous ring remains as a tail while the new front fades in.
        groups = tuple(tuple(dict.fromkeys(ring + (rings[index - 1] if index else ())))
                       for index, ring in enumerate(rings)) + (rings[-1], ())
        self.start_wave(now=now, groups=groups, hold_on_complete=False,
                                stage_duration_ms=140, preserve_current=True)
        self.wave_phase = "game_ripple"
        return self.snapshot()

    def start_game_success_wave(
        self,
        origin_button: int | None = None,
        *,
        next_commands: tuple[LightingCommand, ...] = (),
        now: float | None = None,
    ) -> dict[str, object]:
        """Run a fast celebratory burst on correct game answer, then land on next_commands."""
        origin = int(origin_button) if origin_button in BUTTON_WAVE_ELEMENTS else 4
        rings = tuple(
            tuple(
                element_id
                for button_id in (origin - distance, origin + distance)
                if 1 <= button_id <= 7
                for element_id in BUTTON_WAVE_ELEMENTS[button_id]
            )
            for distance in range(max(origin - 1, 7 - origin) + 1)
        )
        peak = tuple(LIGHTING_ELEMENT_IDS)
        groups = (
            rings[0],
            tuple(dict.fromkeys(rings[0] + (rings[1] if len(rings) > 1 else ()))),
            peak,
            rings[-1],
        )
        self.start_wave(
            now=now,
            groups=groups,
            hold_on_complete=False,
            stage_duration_ms=80,
            preserve_current=False,
        )
        self.wave_phase = "game_success"
        self._wave_final_commands = tuple(next_commands) if next_commands else None
        return self.snapshot()

    def start_profile_ready(self, *, now: float | None = None) -> dict[str, object]:
        """Own the acknowledgement wave and continuous breathing as one effect.

        Each profile-ready stage gets its full dwell even after a delayed wakeup. The
        ordinary wave keeps its existing elapsed-clock semantics.
        """
        now = self.monotonic_now() if now is None else float(now)
        generation = self._start_generation("wave", now)
        self._profile_ready = True
        self.wave_phase = "profile_ready"
        self.output.clear_all()
        self.advance(now, force=True)
        self._schedule(generation)
        return self.snapshot()

    def cancel_effects(self, *, clear: bool = True) -> dict[str, object]:
        self._cancel_task()
        invalidate = getattr(self.output, "invalidate_pending", None)
        if callable(invalidate):
            invalidate()
        self.generation += 1
        self.effect = "idle"
        self.started_at_monotonic = None
        self._pwm_phase_origin = None
        self.last_emit_key = None
        self.wave_stage = -1
        self._wave_groups = EXHIBITION_WAVE_GROUPS
        self._follow_up_groups = None
        self._wave_base_elements = frozenset()
        self._wave_loop = False
        self._wave_stage_ms = EXHIBITION_WAVE_STAGE_MS
        self._breathing_elements = frozenset(LIGHTING_ELEMENT_IDS)
        self._breathing_base_elements = frozenset()
        self.breathing_phase = "breathing"
        self.wave_phase = "idle"
        self._wave_hold_on_complete = True
        self._wave_final_commands: tuple[LightingCommand, ...] | None = None
        self._profile_ready = False
        self._profile_stage_at = None
        if clear:
            self.output.clear_all()
            self.frame_count += 1
            self.last_frame = {
                "effect": "idle", "phase": "dark_idle", "state": 0,
                "transition_ms": 0, "protocol": "binary_transition",
                "frame_index": self.frame_count, "commands": len(LIGHTING_ELEMENT_IDS),
            }
            self._notify_frame(dict(self.last_frame))
        return self.snapshot()

    def reset_to_standby(self, *, now: float | None = None) -> dict[str, object]:
        return self.cancel_effects(clear=True)

    def advance(self, now: float | None = None, *, force: bool = False) -> dict[str, object] | None:
        if self.effect in {"idle", "wave_paused"} or self.started_at_monotonic is None:
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
                self._binary_commands(
                    self._breathing_elements, self._breathing_base_elements, state, transition_ms,
                ),
                {
                    "effect": "breathing",
                    "phase": self.breathing_phase,
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
        stage = int(elapsed_ms // self._wave_stage_ms)
        if self._profile_ready:
            # Do not skip required stages when the event loop wakes late, or
            # finish the sequence while the final stage is still fading.
            if self._profile_stage_at is not None and (
                (now - self._profile_stage_at) * 1000.0 + 1e-6 < self._wave_stage_ms
            ):
                return None
            stage = self.wave_stage + 1
            self._profile_stage_at = now
        if stage >= len(groups):
            if self._profile_ready:
                generation = self.generation
                self.wave_stage = len(groups)
                self._emit(self._all_binary_commands(1, self._wave_stage_ms), {
                    "effect": "wave_complete", "phase": "profile_ready",
                    "at_monotonic": now, "wave_stage": self.wave_stage,
                    "active_elements": list(LIGHTING_ELEMENT_IDS), "held": True,
                })
                # A synchronous frame hook may replace/cancel this owner.
                if generation != self.generation or not self._profile_ready:
                    return None
                self._profile_ready = False
                self.effect = "breathing"
                self.breathing_phase = "profile_ready"
                self.started_at_monotonic = now
                self.last_emit_key = None
                return self.advance(now, force=True)
            if self._wave_loop:
                self.started_at_monotonic = now
                self.wave_stage = -1
                self.last_emit_key = None
                return self.advance(now, force=True)
            if self._follow_up_groups is not None and self.wave_phase == "primary":
                self._wave_groups = self._follow_up_groups
                self._follow_up_groups = None
                self.wave_phase = "follow_up"
                self.started_at_monotonic = now
                self.wave_stage = -1
                self.last_emit_key = None
                return self.advance(now, force=True)
            if self.wave_stage != len(groups):
                self.wave_stage = len(groups)
                active = {element_id for group in groups for element_id in group}
                if self._wave_hold_on_complete:
                    self.effect = "wave_hold"
                    commands = tuple(
                        LightingCommand(element_id, 1 if element_id in active or element_id in self._wave_base_elements else 0, self._wave_stage_ms)
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
                final_cmds = self._wave_final_commands
                self._wave_final_commands = None
                self.effect = "idle"
                return self._emit(
                    final_cmds if final_cmds is not None else self._all_binary_commands(0),
                    {"effect": "wave_complete", "at_monotonic": now, "wave_stage": self.wave_stage},
                )
            return None
        if not force and stage == self.wave_stage:
            return None
        self.wave_stage = stage
        active = set(groups[stage])
        commands = tuple(LightingCommand(element_id, 1 if element_id in active or element_id in self._wave_base_elements else 0, self._wave_stage_ms) for element_id in LIGHTING_ELEMENT_IDS)
        return self._emit(commands, {"effect": "wave", "at_monotonic": now, "wave_stage": stage + 1, "wave_total_stages": len(groups), "active_elements": sorted(active), "brightness_percent": 100})

    def pause_effect(self, *, now: float | None = None) -> dict[str, object]:
        if self.effect != "wave":
            return self.snapshot()
        self._cancel_task()
        self.effect = "wave_paused"
        return self.snapshot()

    def resume_effect(self, *, now: float | None = None) -> dict[str, object]:
        if self.effect != "wave_paused":
            return self.snapshot()
        now = self.monotonic_now() if now is None else float(now)
        self.effect = "wave"
        self.started_at_monotonic = now - max(0, self.wave_stage) * self._wave_stage_ms / 1000.0
        self.last_emit_key = None
        self.advance(now, force=True)
        self._schedule(self.generation)
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        frame = dict(self.last_frame or {})
        return {"active": self.effect != "idle", "effect": self.effect, "generation": self.generation, "clock": "monotonic_injected", "task_active": self.task_active, "scheduler": "background_wakeup" if self.background_scheduler else "manual_fake_clock", "phase": self.breathing_phase if self.effect == "breathing" else self.wave_phase, "breathing_elements": sorted(self._breathing_elements), "breathing_base_elements": sorted(self._breathing_base_elements), "wave_stage": self.wave_stage, "wave_phase": self.wave_phase, "wave_loop": self._wave_loop, "wave_stage_ms": self._wave_stage_ms, "frame_count": self.frame_count, "state": frame.get("state"), "transition_ms": frame.get("transition_ms"), "protocol": frame.get("protocol"), "brightness_percent": frame.get("brightness_percent"), "requested_brightness_percent": frame.get("requested_brightness_percent"), "on_slots": frame.get("on_slots"), "pwm_slots": frame.get("pwm_slots"), "elements": list(LIGHTING_ELEMENT_IDS), "last_frame": frame, "last_error": dict(self.last_error) if self.last_error else None}



def _lighting_failure_evidence(exc: BaseException, *, stage: str) -> dict[str, object]:
    """Fixed-shape transport evidence, never arbitrary exception messages."""
    cause = exc
    for _ in range(4):
        nested = cause.__cause__
        if nested is None or nested is cause:
            break
        cause = nested
    number = getattr(cause, "errno", None)
    known = {"OSError", "TimeoutError", "ConnectionError", "BufferError", "RuntimeError",
             "ValueError", "TypeError", "InterruptedError", "BlockingIOError",
             "FileNotFoundError", "PermissionError", "BrokenPipeError",
             "_IncompleteLightingLineError"}
    code = lambda error: type(error).__name__ if type(error).__name__ in known else "OtherError"
    return {"exception_type": code(exc), "cause_type": code(cause),
            "errno": number if type(number) is int else None,
            "stage": stage, "at_monotonic": time.monotonic()}


class _IncompleteLightingLineError(ConnectionError):
    """A partial or unconfirmed CSV line cannot safely be replayed."""


class SerialLightingAdapter:
    """Physical lighting wire adapter.

    This is separate from the legacy JSONL/CRC32 simulator transport. The
    only bytes written here are canonical CSV/LF lighting lines:
    <element_id>,<state>,<transition_ms>\n. The word "binary" in older
    notes describes the state field (0/1), not the wire framing.
    """

    def __init__(self, path: str, *, baudrate: int = 115200,
                 timeout_s: float = 1.0, transport=None,
                 inter_command_delay_s: float | None = None) -> None:
        if not path:
            raise ValueError("lighting serial path is required")
        self.path = str(path)
        self.baudrate = int(baudrate)
        self.timeout_s = max(0.01, float(timeout_s))
        self.transport = transport
        self.inter_command_delay_s = (
            max(0.0, float(inter_command_delay_s))
            if inter_command_delay_s is not None
            else (0.002 if transport is None else 0.0)
        )
        self._lock = threading.Lock()
        # RX fault observation must never wait behind a blocking TX syscall.
        self._reader_state_lock = threading.Lock()
        self._written_targets: dict[int, tuple[int, int]] = {}
        self._written_targets_epoch = 0
        self._ready = False
        self._closed = False
        self._sent = 0
        self._failed = 0
        self._last_error: dict | None = None
        self._last_open_at: float | None = None
        self._last_write_failure: dict | None = None
        self._drain_stop = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._drained_bytes = 0
        self._read_failures = 0
        self._last_read_failure: dict | None = None
        self._on_read_fault = None
        self._line_boundary_pending = False
        self._line_boundary_recoveries = 0

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
            self._invalidate_written_targets()
            self.transport.open()
            self._ready = True
            self._closed = False
            self._last_error = None
            self._last_open_at = time.time()
            self._start_reader_locked()

    def _start_reader_locked(self) -> None:
        # A timed-out old reader owns its old stop token forever. Reopening
        # must never revive that reader against the replacement descriptor.
        with self._reader_state_lock:
            self._written_targets.clear()
            self._written_targets_epoch += 1
            self._drain_stop.set()
            stop = self._drain_stop = threading.Event()
            self._ready = True
            self._closed = False
            self._last_error = None
            self._drain_thread = threading.Thread(
                target=self._drain_loop, args=(stop, self.transport),
                name="mishka-lighting-rx-drain", daemon=True,
            )
            self._drain_thread.start()

    def set_read_fault_handler(self, callback) -> None:
        with self._reader_state_lock:
            self._on_read_fault = callback
            # open() starts RX before the output owner may be constructed.
            # Replay only the currently active fault, never a recovered one.
            error = (dict(self._last_error) if not self._ready and self._last_error
                     and self._last_error.get("stage") == "serial_read" else None)
        if error is not None and callable(callback):
            callback(error)

    def _drain_loop(self, stop, transport) -> None:
        while not stop.is_set():
            try:
                read_available = getattr(transport, "read_available", None)
                if callable(read_available):
                    received = read_available() or b""
                    if stop.is_set():
                        return
                    self._drained_bytes += len(received)
                    if not received:
                        stop.wait(0.02)
                else:
                    stop.wait(0.02)
            except Exception as exc:
                with self._reader_state_lock:
                    if stop.is_set() or self._drain_stop is not stop:
                        return
                    error = _lighting_failure_evidence(exc, stage="serial_read")
                    self._ready = False
                    self._written_targets.clear()
                    self._written_targets_epoch += 1
                    self._read_failures += 1
                    self._last_read_failure = dict(error)
                    self._last_error = dict(error)
                    callback = self._on_read_fault
                # Never invoke owner cleanup while holding the serial lock,
                # and never reconnect from the reader being joined by close.
                if callable(callback):
                    callback(dict(error))
                return

    def close(self) -> None:
        with self._lock:
            with self._reader_state_lock:
                self._ready = False
                self._closed = True
                self._written_targets.clear()
                self._written_targets_epoch += 1
                self._drain_stop.set()
                thread, self._drain_thread = self._drain_thread, None
            if self.transport is not None:
                try:
                    self.transport.close()
                except Exception:
                    pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    def send(self, command: LightingCommand) -> None:
        if not isinstance(command, LightingCommand):
            command = LightingCommand(**command)
        self.send_batch((command,))

    def _invalidate_written_targets(self) -> None:
        with self._reader_state_lock:
            self._written_targets.clear()
            self._written_targets_epoch += 1

    def _write_command(self, command: LightingCommand) -> bool:
        # The desired-output snapshot may include lines not yet sent or cancelled
        # midway through a frame. Only this sole writer knows completed lines.
        target = (command.state, command.transition_ms)
        with self._reader_state_lock:
            epoch = self._written_targets_epoch
            if command.transition_ms > 0 and self._written_targets.get(command.element_id) == target:
                return False
        self._write_line(command.to_wire())
        self._sent += 1
        with self._reader_state_lock:
            # RX can revoke this owner while its native TX call is blocked.
            # Never repopulate the cache from that old in-flight line.
            if epoch == self._written_targets_epoch and self._ready and not self._closed:
                self._written_targets[command.element_id] = target
        return True

    def _write_validated_batch(self, validated: tuple[LightingCommand, ...], is_current) -> bool:
        last_index = len(validated) - 1
        for index, command in enumerate(validated):
            if callable(is_current) and not is_current():
                return False
            wrote = self._write_command(command)
            if wrote and self.inter_command_delay_s > 0 and index < last_index:
                time.sleep(self.inter_command_delay_s)
        return True

    def _write_line(self, payload: bytes) -> None:
        # PosixSerialTransport already completes partial os.write calls. Also
        # support transports that return byte counts: complete this one line
        # before checking generation again, so a reset cannot splice CSV lines.
        offset = 0
        deadline = time.monotonic() + self.timeout_s
        try:
            while offset < len(payload):
                if offset and time.monotonic() >= deadline:
                    raise TimeoutError("lighting serial line write timed out")
                remaining = payload[offset:]
                written = self.transport.write(remaining)
                if written is None:
                    written = len(remaining)
                if isinstance(written, bool) or not isinstance(written, int) or not 0 < written <= len(remaining):
                    raise IOError("invalid lighting serial write count")
                offset += written
        except Exception as exc:
            self._invalidate_written_targets()
            # The production transport completes os.write calls internally,
            # but an exception does not expose how many bytes reached the
            # device. Treat that outcome as potentially partial as well.
            from .hardware import PosixSerialTransport
            if offset or isinstance(self.transport, PosixSerialTransport):
                raise _IncompleteLightingLineError("lighting serial line interrupted after partial or unconfirmed write") from exc
            raise

    def send_batch(self, commands: Sequence[LightingCommand], *, is_current=None) -> bool:
        validated = tuple(
            command if isinstance(command, LightingCommand) else LightingCommand(**command)
            for command in commands
        )
        with self._lock:
            if callable(is_current) and not is_current():
                return False
            if not self.is_ready():
                raise ConnectionError("lighting serial adapter is not ready")
            try:
                if self._line_boundary_pending:
                    # A host reopen does not clear the MCU's partial CSV buffer.
                    # Complete/discard that old line before writing new targets.
                    try:
                        self._write_line(b"\n")
                    except Exception as exc:
                        raise _IncompleteLightingLineError("lighting serial boundary recovery failed") from exc
                    self._line_boundary_pending = False
                    self._line_boundary_recoveries += 1
                return self._write_validated_batch(validated, is_current)
            except Exception as exc:
                first_error = _lighting_failure_evidence(exc, stage="serial_batch_write")
                self._last_write_failure = dict(first_error)
                self._failed += 1
                self._ready = False
                self._last_error = {**first_error, "attempt": 1}
                if isinstance(exc, _IncompleteLightingLineError):
                    self._line_boundary_pending = True
                    raise ConnectionError("lighting serial write failed after incomplete line") from exc
                if callable(is_current) and not is_current():
                    return False
                # USB-serial CDC devices can transiently stop accepting a
                # burst while the firmware is consuming the previous line.
                # LED writes are idempotent, so reconnect and retry the full
                # batch once.  This keeps a single transient write failure
                # from killing the canonical breathing/wave task.
                try:
                    self._drain_stop.set()
                    self.transport.close()
                    if callable(is_current) and not is_current():
                        return False
                    self.transport.open()
                    self._ready = True
                    self._start_reader_locked()
                    if not self._write_validated_batch(validated, is_current):
                        return False
                    self._last_error = None
                    return True
                except Exception as retry_exc:
                    if isinstance(retry_exc, _IncompleteLightingLineError):
                        self._line_boundary_pending = True
                    self._ready = False
                    self._last_error = {
                        **_lighting_failure_evidence(retry_exc, stage="serial_batch_retry"),
                        "attempt": 2, "first_error": first_error,
                    }
                    self._last_write_failure = dict(self._last_error)
                    raise ConnectionError("lighting serial write failed") from retry_exc

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
            "last_write_failure": dict(self._last_write_failure) if self._last_write_failure else None,
            "last_open_at": self._last_open_at,
            "drained_bytes": self._drained_bytes,
            "read_failures": self._read_failures,
            "last_read_failure": dict(self._last_read_failure) if self._last_read_failure else None,
            "framing_recovery_pending": self._line_boundary_pending,
            "framing_recoveries": self._line_boundary_recoveries,
            "drain_alive": bool(self._drain_thread and self._drain_thread.is_alive()),
        }


class LightingFaultLatchedError(ConnectionError):
    """A scenario producer cannot revive a failed physical device owner."""


class PhysicalLightingOutput:
    """Desired lighting targets with one bounded, ordered serial writer.

    Generation checks fence every pending line and retry. One CSV line already
    being written can finish after invalidation; terminal dark follows
    it. Flush means host writes completed, never firmware acknowledgement.
    """

    def __init__(self, adapter: SerialLightingAdapter, *, max_pending_batches: int = 32,
                 auto_dark_recovery: bool = False, on_fault=None) -> None:
        if isinstance(max_pending_batches, bool) or not isinstance(max_pending_batches, int) or max_pending_batches < 2:
            raise ValueError("max_pending_batches must be an integer of at least two")
        self.adapter = adapter
        self.state = LightingState()
        self._generation = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mishka-lighting-io")
        self._pending: deque[tuple[int, tuple[LightingCommand, ...]]] = deque()
        self._max_pending_batches = max_pending_batches
        self._draining = False
        self._in_flight = False
        self._closing = False
        self._closed = False
        self._last_async_error: dict[str, object] | None = None
        self._first_failure: dict[str, object] | None = None
        self._generation_error: dict[str, object] | None = None
        self._submitted_batches = 0
        self._written_batches = 0
        self._dropped_batches = 0
        self._failed_batches = 0
        self._overflow_batches = 0
        self._auto_dark_recovery = bool(auto_dark_recovery)
        self._on_fault = on_fault
        self._fault_latched = False
        self._dark_recovery_attempts = 0
        self._dark_recovery: dict[str, object] | None = None
        self._pending_recovery: int | None = None
        set_handler = getattr(adapter, "set_read_fault_handler", None)
        if callable(set_handler):
            set_handler(self._external_fault)

    def _external_fault(self, evidence: dict) -> None:
        """Fence a producer/RX failure before scheduling sole-writer cleanup."""
        with self._condition:
            if self._closed or self._closing or self._fault_latched:
                return
            error = {**evidence, "generation": self._generation}
            self._last_async_error = error
            if self._first_failure is None:
                self._first_failure = dict(error)
            self._failed_batches += 1
            self._invalidate_locked()
            self._generation_error = error
            self._fault_latched = True
            if self._auto_dark_recovery and not self._dark_recovery_attempts:
                self._pending_recovery = self._generation
                if not self._draining:
                    self._draining = True
                    self._writer.submit(self._drain_pending)
            self._condition.notify_all()
        if callable(self._on_fault):
            try:
                self._on_fault(self, dict(error))
            except Exception:
                pass  # owner remains fenced even when notification fails

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _write_batch(self, generation: int, commands: tuple[LightingCommand, ...], *, deadline=None) -> bool:
        def current():
            return self._is_current(generation) and (deadline is None or time.monotonic() < deadline)
        if not current():
            return False
        if hasattr(self.adapter, "is_ready") and not self.adapter.is_ready():
            reconnect = getattr(self.adapter, "reconnect", None)
            if not callable(reconnect):
                raise ConnectionError("lighting serial adapter is not ready")
            reconnect()
        return self.adapter.send_batch(commands, is_current=current)

    def _recover_dark_once(self, generation: int) -> None:
        # Run on the same sole writer, not another executor or periodic task.
        # Never replay failed lit targets and never clear the failed-owner latch.
        with self._condition:
            if (not self._auto_dark_recovery or generation != self._generation
                    or self._closed or self._closing or self._dark_recovery_attempts):
                return
            self._dark_recovery_attempts += 1
            recovery = {"generation": generation, "stage": "terminal_dark_recovery",
                        "at_monotonic": time.monotonic(), "status": "pending",
                        "host_write_completed": False, "firmware_acknowledged": False}
            self._dark_recovery = recovery
            commands = tuple(LightingCommand(i, 0) for i in LIGHTING_ELEMENT_IDS)
            for command in commands:
                self.state.apply(command)
        # One native POSIX line may finish after this budget, but no further
        # line is admitted. No retry loop and no extra queued batch is created.
        deadline = time.monotonic() + 1.0
        try:
            written = self._write_batch(generation, commands, deadline=deadline)
            result = {"status": "host_dark_written" if written else "superseded_or_timed_out",
                      "host_write_completed": bool(written)}
        except Exception as exc:
            result = {"status": "failed", "host_write_completed": False,
                      "failure": _lighting_failure_evidence(exc, stage="terminal_dark_recovery")}
        with self._condition:
            recovery.update(result)
            recovery["completed_at_monotonic"] = time.monotonic()
            # output_ready remains false until an explicit new generation.

    def _drain_pending(self) -> None:
        while True:
            with self._condition:
                recovery = self._pending_recovery
                if recovery is not None:
                    self._pending_recovery = None
                    generation, commands = recovery, ()
                elif self._pending:
                    generation, commands = self._pending.popleft()
                else:
                    self._draining = False
                    self._condition.notify_all()
                    return
                self._in_flight = True
            try:
                if recovery is not None:
                    self._recover_dark_once(generation)
                    continue
                written = self._write_batch(generation, commands)
                with self._condition:
                    if written:
                        self._written_batches += 1
                    else:
                        self._dropped_batches += 1
            except Exception as exc:
                notify_fault = False
                with self._condition:
                    error = {**_lighting_failure_evidence(exc, stage="output_batch_write"),
                             "generation": generation}
                    self._last_async_error = error
                    if self._first_failure is None:
                        self._first_failure = dict(error)
                    self._failed_batches += 1
                    if generation == self._generation:
                        self._generation_error = error
                        notify_fault = not self._fault_latched
                        self._fault_latched = self._fault_latched or self._auto_dark_recovery or callable(self._on_fault)
                        # Stop the failed producer's queued backlog. A new
                        # generation/reset may explicitly reconcile output.
                        self._dropped_batches += len(self._pending)
                        self._pending.clear()
                if notify_fault and callable(self._on_fault):
                    try:
                        self._on_fault(self, dict(error))
                    except Exception:
                        # The output fence is already latched. Never reopen it
                        # because an owner-loop notification could not be sent.
                        pass
                self._recover_dark_once(generation)
            finally:
                with self._condition:
                    self._in_flight = False
                    self._condition.notify_all()

    def _enqueue_locked(self, commands: tuple[LightingCommand, ...]) -> None:
        if len(self._pending) + int(self._in_flight) >= self._max_pending_batches:
            self._overflow_batches += 1
            raise BufferError("lighting output queue is full")
        if self._fault_latched and any(command.state for command in commands):
            raise LightingFaultLatchedError("lighting fault requires a new hardware lifecycle")
        if self._generation_error is not None:
            raise ConnectionError("lighting output requires a new generation after failure")
        self._pending.append((self._generation, commands))
        self._submitted_batches += 1
        for command in commands:
            self.state.apply(command)
        if not self._draining:
            self._draining = True
            self._writer.submit(self._drain_pending)

    def _invalidate_locked(self) -> None:
        self._generation += 1
        self._dropped_batches += len(self._pending)
        self._pending.clear()
        self._generation_error = None

    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> None:
        self.set_elements((LightingCommand(element_id, state, transition_ms),))

    def set_elements(self, commands: Sequence[LightingCommand]) -> None:
        validated = tuple(
            command if isinstance(command, LightingCommand) else LightingCommand(**command)
            for command in commands
        )
        if len(validated) > len(LIGHTING_ELEMENT_IDS):
            raise ValueError("a lighting batch must contain at most eighteen commands")
        try:
            with self._lock:
                if self._closed or self._closing:
                    raise ConnectionError("lighting output is closed")
                self._enqueue_locked(validated)
        except BufferError as exc:
            self._external_fault(_lighting_failure_evidence(exc, stage="output_queue_overflow"))
            raise

    def clear_all(self, transition_ms: int = 0) -> None:
        commands = tuple(
            LightingCommand(element_id, 0, transition_ms)
            for element_id in LIGHTING_ELEMENT_IDS
        )
        with self._lock:
            if self._closed or self._closing:
                raise ConnectionError("lighting output is closed")
            self._invalidate_locked()
            self._enqueue_locked(commands)

    def invalidate_pending(self) -> None:
        with self._lock:
            if self._closing or self._closed or self._fault_latched:
                return
            self._invalidate_locked()

    def flush(self, timeout_s: float = 2.0) -> None:
        """Wait a bounded time for host writes; expose timeout or write failure."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while self._pending or self._in_flight or self._draining:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("lighting output drain timed out")
                self._condition.wait(remaining)
            if self._generation_error is not None:
                raise ConnectionError("lighting serial write failed during drain")

    def reset_dark(self, timeout_s: float = 2.0) -> None:
        self.clear_all()
        self.flush(timeout_s)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return self.state.snapshot()

    def close(self, timeout_s: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                # A timed-out close can be checked again after the blocked
                # transport write has returned.
                already_closed = True
            else:
                already_closed = False
                self._closing = True
                self._invalidate_locked()
                self._enqueue_locked(tuple(LightingCommand(element_id, 0) for element_id in LIGHTING_ELEMENT_IDS))
        try:
            self.flush(timeout_s)
        finally:
            if not already_closed:
                with self._lock:
                    self._closed = True
                self._writer.shutdown(wait=False)

    def diagnostics(self) -> dict:
        with self._lock:
            output = {
                "state": self.state.snapshot(), "state_kind": "desired_targets",
                "generation": self._generation, "closed": self._closed,
                "pending_batches": len(self._pending), "in_flight": self._in_flight,
                "max_pending_batches": self._max_pending_batches,
                "submitted_batches": self._submitted_batches,
                "written_batches": self._written_batches,
                "dropped_batches": self._dropped_batches,
                "failed_batches": self._failed_batches,
                "overflow_batches": self._overflow_batches,
                "async_error": dict(self._last_async_error) if self._last_async_error else None,
                "last_failure": dict(self._last_async_error) if self._last_async_error else None,
                "first_failure": dict(self._first_failure) if self._first_failure else None,
                "dark_recovery_attempts": self._dark_recovery_attempts,
                "dark_recovery": dict(self._dark_recovery) if self._dark_recovery else None,
                "output_ready": not (self._closed or self._closing or self._fault_latched or self._generation_error is not None),
                "generation_failed": self._fault_latched or self._generation_error is not None,
                "fault_latched": self._fault_latched,
                "firmware_acknowledged": False,
            }
        return {"backend": "serial", **self.adapter.diagnostics(), **output}
