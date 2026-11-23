"""Pure, non-clinical visual games. Input time is accepted monotonic time.

No transport, speech, user profile, diagnostic norms or inferred abilities.
A round is an explicit game parameter, not a developmental assessment.
"""
from dataclasses import dataclass, replace
import math
import random

from .game_content import GameChallenge, build_deck, normalize_band

BUTTONS = (1, 2, 3, 4)
EXIT_RECOGNITION_S = .6
EXIT_READY_S = 5.

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

# The legacy catalog remains a compatibility seam for existing visual callers.
# The direct chooser uses this explicit five-game order.
IMMERSIVE_GAME_SPECS = (
    GameSpec('free_lights', 'Живой свет', 'PHRASE_037', 'free_play', playable_buttons=tuple(range(1, 8))),
    GameSpec('quiz', 'Вопросы Мишки', 'PHRASE_020', 'quiz', playable_buttons=tuple(range(1, 8))),
    GameSpec('repeat_pattern', 'Повтори огоньки', 'PHRASE_040', 'sequence'),
    GameSpec('odd_one_out', 'Выбери лишнее', 'PHRASE_020', 'odd_one_out', playable_buttons=(1, 2, 3, 4, 5)),
    GameSpec('word_game', 'Игра в слова', 'PHRASE_020', 'voice', playable_buttons=()),
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
    age_band: str = '3-5'
    level: int = 0
    seed: int = 0
    immersive: bool = False
    deck: tuple[GameChallenge, ...] = ()
    challenge_index: int = 0
    # Shared ordered-hint cursor; hints_emitted separately bounds timer repeats.
    challenge_attempts: int = 0
    wrong_attempts: int = 0
    narrowed_options: tuple[int, ...] = ()
    single_option_misses: int = 0
    seven_answer_generation: int | None = None
    seven_exit_down_at: float | None = None
    seven_exit_phase: str = 'idle'
    seven_exit_step: int = 0
    seven_gesture_generation: int = 0

@dataclass(frozen=True)
class GameTransition:
    state: GameState
    desired_buttons: tuple[int, ...]
    status: str
    feedback_id: str | None = None
    completed: bool = False
    speech_text: str | None = None
    wave_origin: int | None = None


def current_challenge(state: GameState) -> GameChallenge | None:
    if state.game_id not in {'quiz', 'odd_one_out'} or not state.deck:
        return None
    return state.deck[state.challenge_index % len(state.deck)]


def game_answer_buttons(state: GameState, *, include_narrowed: bool = True) -> tuple[int, ...]:
    """Answers stay contextual during their prompt, including physical 5 and 7."""
    challenge = current_challenge(state)
    if challenge is not None:
        if include_narrowed and state.narrowed_options:
            return state.narrowed_options
        return tuple(button for button, _ in challenge.options)
    if state.game_id == 'free_lights':
        return next((spec.playable_buttons for spec in IMMERSIVE_GAME_SPECS if spec.game_id == 'free_lights'), BUTTONS)
    if state.game_id == 'word_game' or state.phase == 'completed':
        return ()
    if state.game_id == 'repeat_pattern' and state.immersive:
        return BUTTONS if state.age_band == '1-3' else tuple(range(1, 8))
    return BUTTONS


def desired_buttons(state: GameState) -> tuple[int, ...]:
    challenge = current_challenge(state)
    if challenge is not None and state.phase in {'awaiting_prompt', 'playing'}:
        return game_answer_buttons(state)
    if state.game_id in {'free_lights', 'word_game'}:
        # The free-light ripple owns its frames; key releases must not clear it.
        return ()
    if state.phase == 'demonstrate':
        return (state.pattern[state.demonstration_index],) if state.demonstration_on else ()
    if state.phase != 'playing':
        return ()
    if state.game_id == 'repeat_pattern':
        return game_answer_buttons(state)
    if state.game_id == 'visual_free_play':
        return state.held
    if state.game_id in {'find_pair', 'press_together'}:
        pair = (state.target, BUTTONS[(BUTTONS.index(state.target) + 1) % len(BUTTONS)])
        return tuple(button for button in pair if button not in state.found)
    return (state.target,)


def start(game_id: str, *, sequence: int, now: float, selection_button: int, held_buttons: tuple[int, ...] = (), run_id: str = "",
          response_window_s: float = 18., first_hint_s: float = 18., hint_limit: int = 2,
          age_band: str = '3-5', level: int = 0, seed: int = 0, immersive: bool = False) -> GameState:
    if game_id not in {spec.game_id for spec in (*AVAILABLE_GAME_SPECS, *IMMERSIVE_GAME_SPECS)}:
        raise ValueError('unavailable game')
    if not math.isfinite(now):
        raise ValueError('invalid game clock')
    # Entry preserves actual held keys, but never invents gameplay down times.
    # Each inherited key must be released and freshly pressed in this game.
    immersive = immersive or game_id in {'free_lights', 'quiz', 'odd_one_out', 'word_game'}
    allowed = tuple(range(1, 8)) if immersive else BUTTONS
    held = tuple(sorted({button for button in (*held_buttons, selection_button)
                         if type(button) is int and button in allowed}))
    state = GameState(game_id, held=held, last_sequence=sequence, last_time=now,
                      run_id=run_id, response_window_s=response_window_s, first_hint_s=first_hint_s,
                      hint_limit=hint_limit, age_band=normalize_band(age_band),
                      level=max(0, int(level)), seed=int(seed), immersive=immersive)
    if game_id in {'quiz', 'odd_one_out'}:
        deck = build_deck(game_id, state.age_band, seed=state.seed)
        if not deck:
            raise ValueError('game has no challenges')
        return replace(state, phase='awaiting_prompt', deck=deck)
    if game_id == 'repeat_pattern':
        return replace(state, phase='instruction', rounds=3)
    if game_id == 'word_game':
        return replace(state, phase='awaiting_prompt')
    return arm_wait(state, now)


def _progress(state: GameState) -> GameTransition:
    count = state.round + 1
    complete = count >= state.rounds and not state.immersive
    state = replace(state, round=count, round_generation=state.round_generation + 1,
                    target=BUTTONS[count % len(BUTTONS)], phase='completed' if complete else 'playing',
                    found=(), down_times=(), hold_ready=False, hints_emitted=0,
                    level=state.level + int(state.immersive))
    state = (begin_demonstration(state, state.last_time)
             if state.game_id == 'repeat_pattern' and not complete else arm_wait(state, state.last_time))
    return GameTransition(state, desired_buttons(state), 'completed' if complete else 'progress',
                          'PHRASE_028' if complete else None, complete)


def _narrow_challenge_options(state: GameState, challenge: GameChallenge, wrong_button: int) -> tuple[int, ...]:
    """Narrow >3 quiz options to 3 on 1st miss; highlight answer after >=3 real misses."""
    options = tuple(b for b, _ in challenge.options)
    if state.game_id != 'quiz':
        return state.narrowed_options
    if state.wrong_attempts >= 2:
        return (challenge.answer,)
    if state.narrowed_options or len(options) <= 3:
        return state.narrowed_options
    wrong_candidates = tuple(b for b in options if b != challenge.answer and b != wrong_button)
    if len(wrong_candidates) < 2:
        wrong_candidates = tuple(b for b in options if b != challenge.answer)
    offset = (state.seed + state.challenge_index + wrong_button) % len(wrong_candidates)
    picked = (wrong_candidates[offset], wrong_candidates[(offset + 1) % len(wrong_candidates)])
    return tuple(sorted({challenge.answer, *picked}))


def _answer_challenge(state: GameState, button: int) -> GameTransition:
    challenge = current_challenge(state)
    if button != challenge.answer:
        narrowed = _narrow_challenge_options(state, challenge, button)
        if state.game_id == 'quiz' and state.wrong_attempts >= 2:
            hint = 'Посмотри на огонёк — попробуй нажать эту кнопку!'
        else:
            hints = challenge.hints or ('Попробуем ещё раз. ' + challenge.prompt,)
            hint = hints[min(state.challenge_attempts, len(hints) - 1)]
        state = replace(state, phase='awaiting_prompt',
                        challenge_attempts=state.challenge_attempts + 1,
                        wrong_attempts=state.wrong_attempts + 1,
                        narrowed_options=narrowed,
                        single_option_misses=0,
                        response_deadline=None, hint_deadline=None,
                        timer_generation=state.timer_generation + 1)
        return GameTransition(state, desired_buttons(state), 'retry', speech_text=hint)
    count = state.round + 1
    complete = count >= state.rounds
    if complete:
        state = replace(state, round=count, level=state.level + 1,
                        round_generation=state.round_generation + 1,
                        challenge_attempts=0, hints_emitted=0,
                        wrong_attempts=0, narrowed_options=(), single_option_misses=0,
                        phase='completed', response_deadline=None, hint_deadline=None,
                        timer_generation=state.timer_generation + 1)
        return GameTransition(state, desired_buttons(state), 'completed',
                              feedback_id='PHRASE_028', completed=True)
    index = state.challenge_index + 1
    deck = state.deck
    if index >= len(deck):
        deck = build_deck(state.game_id, state.age_band, seed=state.seed + count)
        index = 0
    state = replace(state, round=count, level=state.level + 1,
                    round_generation=state.round_generation + 1, deck=deck,
                    challenge_index=index, challenge_attempts=0, hints_emitted=0,
                    wrong_attempts=0, narrowed_options=(), single_option_misses=0,
                    phase='awaiting_prompt', response_deadline=None, hint_deadline=None,
                    timer_generation=state.timer_generation + 1)
    return GameTransition(state, desired_buttons(state), 'progress',
                          feedback_id='PHRASE_022', speech_text=current_challenge(state).prompt)


def hold_exit_policy(state: GameState) -> bool:
    return bool(state.immersive) or state.game_id == 'quiz' or 7 in game_answer_buttons(state)


def cancel_exit_gesture(state: GameState) -> GameState:
    return replace(state, seven_exit_down_at=None, seven_answer_generation=None,
        seven_exit_phase='idle', seven_exit_step=0,
        seven_gesture_generation=state.seven_gesture_generation + 1,
        timer_generation=state.timer_generation + 1)


def _apply_game_button(state: GameState, button: int, now: float,
                       held: set[int], down: dict[int, float]) -> GameTransition:
    """Apply a validated in-game button press across all supported game mechanics."""
    if button not in game_answer_buttons(state):
        return GameTransition(state, desired_buttons(state), 'ignored')
    if state.game_id == 'free_lights':
        return GameTransition(state, (), 'free_play', wave_origin=button)
    if state.game_id == 'visual_free_play':
        return GameTransition(state, desired_buttons(state), 'free_play')
    if state.game_id in {'quiz', 'odd_one_out'}:
        return _answer_challenge(state, button)
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


def advance(state: GameState, *, edge: str, button: int, sequence: int, now: float, paused: bool = False,
            cancelled: bool = False) -> GameTransition:
    if (not math.isfinite(now) or now < state.last_time or sequence <= state.last_sequence
            or edge not in {'ButtonPressed', 'ButtonReleased', 'ButtonHeld', 'ButtonLongHeld'}):
        return GameTransition(state, desired_buttons(state), 'stale')
    state = replace(state, last_sequence=sequence, last_time=now)
    physical_buttons = tuple(range(1, 8)) if state.immersive else BUTTONS
    if state.phase == 'completed' or type(button) is not int or button not in physical_buttons:
        return GameTransition(state, desired_buttons(state), 'ignored')
    paused = paused or (state.phase != 'playing' and state.game_id != 'free_lights')
    held, down = set(state.held), dict(state.down_times)
    if state.seven_exit_phase != 'idle' and button != 7 and edge == 'ButtonPressed':
        held.add(button)
        state = cancel_exit_gesture(replace(state, held=tuple(sorted(held))))
        return GameTransition(state, desired_buttons(state), 'exit_aborted')
    if button == 7 and hold_exit_policy(state):
        if edge == 'ButtonPressed':
            if button in held:
                return GameTransition(state, desired_buttons(state), 'held')
            held.add(button)
            state = replace(state, held=tuple(sorted(held)), seven_exit_down_at=now,
                seven_answer_generation=state.round_generation if not paused and 7 in game_answer_buttons(state) else None,
                seven_exit_phase='pending', seven_exit_step=0,
                seven_gesture_generation=state.seven_gesture_generation + 1,
                timer_generation=state.timer_generation + 1)
            return GameTransition(state, desired_buttons(state), 'exit_pending')
        if edge != 'ButtonReleased':
            return GameTransition(state, desired_buttons(state), 'held')
        started, answer_generation, phase = state.seven_exit_down_at, state.seven_answer_generation, state.seven_exit_phase
        held.discard(button); down.pop(button, None)
        state = replace(state, held=tuple(sorted(held)), down_times=tuple(sorted(down.items())))
        if started is None:
            return GameTransition(state, desired_buttons(state), 'released')
        state = cancel_exit_gesture(state)
        if cancelled:
            return GameTransition(state, desired_buttons(state), 'exit_aborted')
        if phase == 'ready' or now >= started + EXIT_READY_S:
            return GameTransition(state, desired_buttons(state), 'exit_requested')
        if phase == 'pending' and now < started + EXIT_RECOGNITION_S and not paused and answer_generation == state.round_generation and 7 in game_answer_buttons(state):
            return _apply_game_button(state, 7, now, held, down)
        return GameTransition(state, desired_buttons(state), 'exit_aborted')
    if edge == 'ButtonReleased':
        started = down.pop(button, None)
        held.discard(button)
        state = replace(state, held=tuple(sorted(held)), down_times=tuple(sorted(down.items())))
        if cancelled:
            return GameTransition(state, desired_buttons(state), 'released_cancelled')
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
    return _apply_game_button(state, button, now, held, down)


def pause(state: GameState) -> GameState:
    """Invalidate timing evidence while preserving actual down-edge guards."""
    return replace(state, down_times=(), hold_ready=False, response_deadline=None, hint_deadline=None, demonstration_deadline=None,
                   timer_generation=state.timer_generation + 1)


def arm_wait(state: GameState, now: float, *, reset_hints: bool = True) -> GameState:
    if state.game_id == 'repeat_pattern' and state.phase in {'instruction','demonstrate'}:
        return begin_demonstration(state, now)
    count = 0 if reset_hints else state.hints_emitted
    timed = state.phase == 'playing' and state.game_id not in {'visual_free_play', 'free_lights'}
    return replace(state, hints_emitted=count,
        response_deadline=now + state.response_window_s if timed and state.game_id != 'word_game' else None,
        hint_deadline=now + state.first_hint_s if timed and count < state.hint_limit else None,
        timer_generation=state.timer_generation + 1)


def finish_prompt(state: GameState, now: float) -> GameTransition:
    """Only the owner of a terminal prompt should call this pure boundary."""
    if not math.isfinite(now) or now < state.last_time:
        return GameTransition(state, desired_buttons(state), 'stale')
    if state.phase == 'instruction' and state.game_id == 'repeat_pattern':
        state = begin_demonstration(replace(state, last_time=now), now)
        return GameTransition(state, desired_buttons(state), 'sequence_demonstration')
    if state.phase != 'awaiting_prompt':
        return GameTransition(state, desired_buttons(state), 'prompt_not_pending')
    state = arm_wait(replace(state, phase='playing', last_time=now), now, reset_hints=False)
    return GameTransition(state, desired_buttons(state), 'prompt_finished')


def next_deadline(state: GameState) -> float | None:
    if state.seven_exit_phase != 'idle':
        if state.seven_exit_phase == 'ready':
            return None
        if state.seven_exit_step == 6:
            return state.seven_exit_down_at + EXIT_READY_S
        return state.seven_exit_down_at + EXIT_RECOGNITION_S + state.seven_exit_step * (EXIT_READY_S - EXIT_RECOGNITION_S) / 6
    due = [x for x in (state.response_deadline, state.hint_deadline, state.demonstration_deadline) if x is not None]
    if state.game_id == 'hold_light' and state.phase == 'playing' and not state.hold_ready:
        started = dict(state.down_times).get(state.target)
        if started is not None and state.target in state.held:
            due.append(started + state.hold_seconds)
    return min(due) if due else None


def timer_step(state: GameState, now: float) -> GameTransition:
    deadline = next_deadline(state)
    if deadline is None or now < deadline or now < state.last_time or not math.isfinite(now):
        return GameTransition(state, desired_buttons(state), 'timer_not_due')
    state = replace(state, last_time=max(state.last_time, now))
    if state.seven_exit_phase != 'idle':
        elapsed = now - state.seven_exit_down_at
        ready = now >= state.seven_exit_down_at + EXIT_READY_S
        step = 7 if ready else min(6, 1 + int((elapsed - EXIT_RECOGNITION_S) * 6 / (EXIT_READY_S - EXIT_RECOGNITION_S) + 1e-8))
        state = replace(state, seven_exit_phase='ready' if ready else 'progress', seven_exit_step=step,
                        timer_generation=state.timer_generation + 1)
        return GameTransition(state, (), 'exit_ready' if ready else 'exit_progress', 'PHRASE_036' if ready else None)
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
    if hint and state.game_id == 'word_game':
        state = replace(state, phase='awaiting_prompt', response_deadline=None, hint_deadline=None)
        return GameTransition(state, (), 'wait_hint', speech_text='Я рядом. Можно назвать слово или нажать семь, чтобы закончить.')
    if hint and state.game_id in {'quiz', 'odd_one_out'}:
        challenge = current_challenge(state)
        hints = challenge.hints or ('Попробуем ещё раз. ' + challenge.prompt,)
        hint_text = hints[min(state.challenge_attempts, len(hints) - 1)]
        state = replace(state, phase='awaiting_prompt', response_deadline=None, hint_deadline=None,
                        challenge_attempts=state.challenge_attempts + 1)
        return GameTransition(state, desired_buttons(state), 'wait_hint', speech_text=hint_text)
    if hint and state.game_id == 'repeat_pattern':
        state = begin_demonstration(state, now)
        return GameTransition(state, desired_buttons(state), 'sequence_demonstration', 'PHRASE_041')
    phrase = next(spec.instruction_phrase_id for spec in AVAILABLE_GAME_SPECS if spec.game_id == state.game_id) if hint else None
    return GameTransition(state, desired_buttons(state), 'wait_hint' if hint else 'response_window_elapsed', phrase)


def begin_demonstration(state: GameState, now: float) -> GameState:
    """Bounded demonstrations. The timings are UI choices, not age norms."""
    if state.immersive:
        pool = game_answer_buttons(state)
        cap = 3 if state.age_band == '1-3' else 5 if state.age_band == '4-5' else 7
        length = min(cap, 2 + state.level // 2)
        rng = random.Random(state.seed + state.level * 104729)
        pattern = []
        for _ in range(length):
            choices = tuple(b for b in pool if not pattern or b != pattern[-1])
            pattern.append(rng.choice(choices))
        pattern = tuple(pattern)
    else:
        pattern = BUTTONS[:min(4, state.round + 2)]
    return replace(state, phase='demonstrate', pattern=pattern,
        demonstration_index=0, demonstration_on=True, demonstration_deadline=now + .7,
        answer_index=0, response_deadline=None, hint_deadline=None,
        timer_generation=state.timer_generation + 1, down_times=(), hold_ready=False)
