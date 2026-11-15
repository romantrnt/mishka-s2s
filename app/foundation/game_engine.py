"""Pure, non-clinical visual games. Input time is accepted monotonic time.

No transport, speech, user profile, diagnostic norms or inferred abilities.
A round is an explicit game parameter, not a developmental assessment.
"""
from dataclasses import dataclass, replace
import math

BUTTONS = (1, 2, 3, 4)

@dataclass(frozen=True)
class GameSpec:
    game_id: str
    display_title: str
    instruction_phrase_id: str
    mechanic: str
    available: bool = True
    playable_buttons: tuple[int, ...] = BUTTONS

AVAILABLE_GAME_SPECS = (
    GameSpec('catch_light', 'Поймай огонёк', 'PHRASE_030', 'target'),
    GameSpec('visual_free_play', 'Свободный свет', 'PHRASE_037', 'free_play'),
    GameSpec('find_pair', 'Два огонька', 'PHRASE_031', 'pair'),
    GameSpec('press_together', 'Нажми вместе', 'PHRASE_032', 'simultaneous'),
    GameSpec('hold_light', 'Удержи огонёк', 'PHRASE_035', 'hold'),
    GameSpec('repeat_pattern', 'Повтори огоньки', 'PHRASE_040', 'sequence'),
)

@dataclass(frozen=True)
class GameState:
    game_id: str
    phase: str = 'playing'
    target: int = 1
    held: tuple[int, ...] = ()
    round: int = 0
    rounds: int = 8
    last_sequence: int = -1
    last_time: float = 0.0
    round_generation: int = 0
    found: tuple[int, ...] = ()
    down_times: tuple[tuple[int, float], ...] = ()
    hold_ready: bool = False
    # Explicit engineering game duration, not an age/medical norm.
    hold_seconds: float = 1.0
    run_id: str = ""
    response_window_s: float = 18.0
    first_hint_s: float = 18.0
    hint_limit: int = 2
    hints_emitted: int = 0
    response_deadline: float | None = None
    hint_deadline: float | None = None
    timer_generation: int = 0
    pattern: tuple[int, ...] = (1, 2)
    demonstration_index: int = 0
    demonstration_on: bool = True
    demonstration_deadline: float | None = None
    answer_index: int = 0

@dataclass(frozen=True)
class GameTransition:
    state: GameState
    desired_buttons: tuple[int, ...]
    status: str
    feedback_id: str | None = None
    completed: bool = False


def desired_buttons(state: GameState) -> tuple[int, ...]:
    if state.phase == 'demonstrate':
        return (state.pattern[state.demonstration_index],) if state.demonstration_on else ()
    if state.phase != 'playing':
        return ()
    if state.game_id == 'repeat_pattern':
        return BUTTONS
    if state.game_id == 'visual_free_play':
        return state.held
    if state.game_id in {'find_pair', 'press_together'}:
        pair = (state.target, BUTTONS[(BUTTONS.index(state.target) + 1) % len(BUTTONS)])
        return tuple(button for button in pair if button not in state.found)
    return (state.target,)


def start(game_id: str, *, sequence: int, now: float, selection_button: int, held_buttons: tuple[int, ...] = (), run_id: str = "",
          response_window_s: float = 18., first_hint_s: float = 18., hint_limit: int = 2) -> GameState:
    if game_id not in {spec.game_id for spec in AVAILABLE_GAME_SPECS}:
        raise ValueError('unavailable game')
    if not math.isfinite(now):
        raise ValueError('invalid game clock')
    # Entry preserves actual held keys, but never invents gameplay down times.
    # Each inherited key must be released and freshly pressed in this game.
    held = tuple(sorted({button for button in (*held_buttons, selection_button) if type(button) is int and button in BUTTONS}))
    state = GameState(game_id, held=held, last_sequence=sequence, last_time=now,
                      run_id=run_id, response_window_s=response_window_s, first_hint_s=first_hint_s,
                      hint_limit=hint_limit)
    if game_id == 'repeat_pattern':
        return replace(state, phase='instruction', rounds=3)
    return arm_wait(state, now)


def _progress(state: GameState) -> GameTransition:
    count = state.round + 1
    complete = count >= state.rounds
    state = replace(state, round=count, round_generation=state.round_generation + 1,
                    target=BUTTONS[count % len(BUTTONS)], phase='completed' if complete else 'playing',
                    found=(), down_times=(), hold_ready=False, hints_emitted=0)
    state = (begin_demonstration(state, state.last_time)
             if state.game_id == 'repeat_pattern' and not complete else arm_wait(state, state.last_time))
    return GameTransition(state, desired_buttons(state), 'completed' if complete else 'progress',
                          'PHRASE_028' if complete else None, complete)


def advance(state: GameState, *, edge: str, button: int, sequence: int, now: float, paused: bool = False) -> GameTransition:
    if (not math.isfinite(now) or now < state.last_time or sequence <= state.last_sequence
            or edge not in {'ButtonPressed', 'ButtonReleased', 'ButtonHeld', 'ButtonLongHeld'}):
        return GameTransition(state, desired_buttons(state), 'stale')
    state = replace(state, last_sequence=sequence, last_time=now)
    if state.phase == 'completed' or button not in BUTTONS:
        return GameTransition(state, desired_buttons(state), 'ignored')
    paused = paused or state.phase != 'playing'
    held, down = set(state.held), dict(state.down_times)
    if edge == 'ButtonReleased':
        started = down.pop(button, None)
        held.discard(button)
        state = replace(state, held=tuple(sorted(held)), down_times=tuple(sorted(down.items())))
        if not paused and state.game_id == 'hold_light' and button == state.target and started is not None:
            if now - started >= state.hold_seconds:
                return _progress(state)
            state = replace(state, hold_ready=False)
            return GameTransition(state, desired_buttons(state), 'early_release')
        return GameTransition(state, desired_buttons(state), 'released')
    if edge != 'ButtonPressed':
        started = down.get(button)
        if (not paused and state.game_id == 'hold_light' and button == state.target
                and started is not None and now - started >= state.hold_seconds and not state.hold_ready):
            state = replace(state, hold_ready=True)
            return GameTransition(state, desired_buttons(state), 'release_ready', 'PHRASE_036')
        return GameTransition(state, desired_buttons(state), 'held')
    if button in held:
        return GameTransition(state, desired_buttons(state), 'held')
    held.add(button)
    if not paused:
        down[button] = now
    state = replace(state, held=tuple(sorted(held)), down_times=tuple(sorted(down.items())))
    if paused:
        # Pause is not part of an active hold; no fabricated duration on resume.
        return GameTransition(replace(state, down_times=(), hold_ready=False), desired_buttons(state), 'paused')
    if state.game_id == 'visual_free_play':
        return GameTransition(state, desired_buttons(state), 'free_play')
    if state.game_id == 'repeat_pattern':
        if button != state.pattern[state.answer_index]:
            state = begin_demonstration(state, now)
            return GameTransition(state, desired_buttons(state), 'sequence_retry')
        index = state.answer_index + 1
        if index == len(state.pattern):
            return _progress(state)
        state = replace(state, answer_index=index)
        return GameTransition(state, desired_buttons(state), 'sequence_partial')
    if state.game_id == 'find_pair':
        if button not in desired_buttons(state):
            return GameTransition(state, desired_buttons(state), 'retry')
        state = replace(state, found=tuple(sorted(set(state.found) | {button})))
        return _progress(state) if len(state.found) == 2 else GameTransition(state, desired_buttons(state), 'pair_partial')
    if state.game_id == 'press_together':
        pair = set(desired_buttons(state))
        # Two observed down intervals must overlap; an extra wrong held key cannot score.
        if held == pair and pair <= down.keys():
            return _progress(state)
        return GameTransition(state, desired_buttons(state), 'waiting_overlap')
    if state.game_id == 'hold_light':
        return GameTransition(state, desired_buttons(state), 'holding' if button == state.target else 'retry')
    if button != state.target:
        return GameTransition(state, desired_buttons(state), 'retry')
    return _progress(state)


def pause(state: GameState) -> GameState:
    """Invalidate timing evidence while preserving actual down-edge guards."""
    return replace(state, down_times=(), hold_ready=False, response_deadline=None, hint_deadline=None, demonstration_deadline=None,
                   timer_generation=state.timer_generation + 1)


def arm_wait(state: GameState, now: float, *, reset_hints: bool = True) -> GameState:
    if state.game_id == 'repeat_pattern' and state.phase in {'instruction','demonstrate'}:
        return begin_demonstration(state, now)
    count = 0 if reset_hints else state.hints_emitted
    timed = state.phase == 'playing' and state.game_id != 'visual_free_play'
    return replace(state, hints_emitted=count,
        response_deadline=now + state.response_window_s if timed else None,
        hint_deadline=now + state.first_hint_s if timed and count < state.hint_limit else None,
        timer_generation=state.timer_generation + 1)


def next_deadline(state: GameState) -> float | None:
    due = [x for x in (state.response_deadline, state.hint_deadline, state.demonstration_deadline) if x is not None]
    if state.game_id == 'hold_light' and state.phase == 'playing' and not state.hold_ready:
        started = dict(state.down_times).get(state.target)
        if started is not None and state.target in state.held:
            due.append(started + state.hold_seconds)
    return min(due) if due else None


def timer_step(state: GameState, now: float) -> GameTransition:
    deadline = next_deadline(state)
    if deadline is None or now < deadline or not math.isfinite(now):
        return GameTransition(state, desired_buttons(state), 'timer_not_due')
    if state.phase == 'demonstrate':
        if state.demonstration_on:
            state = replace(state, demonstration_on=False, demonstration_deadline=now + .3,
                            timer_generation=state.timer_generation + 1)
        elif state.demonstration_index + 1 < len(state.pattern):
            state = replace(state, demonstration_index=state.demonstration_index + 1,
                            demonstration_on=True, demonstration_deadline=now + .7,
                            timer_generation=state.timer_generation + 1)
        else:
            state = arm_wait(replace(state, phase='playing', demonstration_deadline=None, answer_index=0), now, reset_hints=False)
            return GameTransition(state, desired_buttons(state), 'sequence_input', 'PHRASE_042')
        return GameTransition(state, desired_buttons(state), 'sequence_demonstration')
    if state.game_id == 'hold_light' and not state.hold_ready:
        started = dict(state.down_times).get(state.target)
        if started is not None and state.target in state.held and now >= started + state.hold_seconds:
            state = replace(state, hold_ready=True, last_time=max(state.last_time, now),
                            timer_generation=state.timer_generation + 1)
            return GameTransition(state, desired_buttons(state), 'release_ready', 'PHRASE_036')
    # Expiring a response window never scores, lowers level or ends the game.
    expired = state.response_deadline is not None and now >= state.response_deadline
    hint = state.hint_deadline is not None and now >= state.hint_deadline
    count = state.hints_emitted + int(hint)
    state = replace(state, last_time=max(state.last_time, now),
        response_deadline=None if expired else state.response_deadline,
        hints_emitted=count,
        hint_deadline=(now + state.response_window_s if count < state.hint_limit else None)
                      if hint else state.hint_deadline,
        timer_generation=state.timer_generation + 1)
    if hint and state.game_id == 'repeat_pattern':
        state = begin_demonstration(state, now)
        return GameTransition(state, desired_buttons(state), 'sequence_demonstration', 'PHRASE_041')
    phrase = next(spec.instruction_phrase_id for spec in AVAILABLE_GAME_SPECS if spec.game_id == state.game_id) if hint else None
    return GameTransition(state, desired_buttons(state), 'wait_hint' if hint else 'response_window_elapsed', phrase)


def begin_demonstration(state: GameState, now: float) -> GameState:
    """Bounded 2/3/4-step demonstration. 700/300 ms are UI timings, not norms."""
    return replace(state, phase='demonstrate', pattern=BUTTONS[:min(4, state.round + 2)],
        demonstration_index=0, demonstration_on=True, demonstration_deadline=now + .7,
        answer_index=0, response_deadline=None, hint_deadline=None,
        timer_generation=state.timer_generation + 1, down_times=(), hold_ready=False)
