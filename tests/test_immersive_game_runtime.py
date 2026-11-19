"""Focused runtime boundaries: real owned PCM, cues, hardware guards and S2S context."""
import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.foundation.api import build_application
from app.foundation.age_policy import policy_for_band
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_presentation import game_light_commands
from app.foundation.lighting import BUTTON_FILL_ELEMENTS, FakeLightingOutput, ExhibitionLightingEngine
from app.foundation.s2s import AudioChunk, CachedTTSProvider, SoundDevicePlaybackController


class GameRuntime:
    def __init__(self, band="4-5"):
        self.clock = [10.]
        self.app, *_ = build_application(settings=Settings(profile="mock"),
            monotonic_now=lambda:self.clock[0], background_scheduler=False)
        self.state = CanonicalState("game-runtime", mode="root_menu", handset="up", age_band=band)
        self.state.age_years = {"1-3":2, "4-5":4, "6-7":6}[band]
        self.state.age_band_source = "exhibition_button"
        self.state.age_policy = policy_for_band(band, source="exhibition_button").as_dict()
        self.app.canonical_runtime.sessions[self.state.session_id] = self.state
        self.sequence = 0
        self.pcm = []
        original = self.app.audio.play_pcm_owned
        def play(handle, audio):
            self.pcm.append((handle, audio))
            original(handle, audio)
        self.app.audio.play_pcm_owned = play
    async def send(self, kind, payload=None, text=None):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, self.state.session_id, self.sequence, 1., "test",
            payload or {}, str(self.sequence), self.clock[0]), text)
    async def key(self, button):
        result = await self.send("ButtonPressed", {"button_id":button})
        await self.send("ButtonReleased", {"button_id":button})
        return result
    async def complete(self):
        lease = self.app._active_audio[self.state.session_id]
        return await self.send("AudioCompleted", {**lease, "response_id":lease["content_id"]})
    async def start(self, slot):
        await self.key(1)
        for _ in range(8):
            if self.state.scene_menu_phase == "ready":
                break
            if self.app._active_audio.get(self.state.session_id):
                await self.complete()
            elif self.state.scene_menu_phase == "repeat_listen":
                await self.send("SpeechReceived", text="нет")
            else:
                break
        assert self.state.scene_menu_phase == "ready"
        await self.key(slot)
        assert self.state.active_audio_id == "PHRASE_020"
        await self.complete()
        assert self.state.active_audio_id == "GAME_PROMPT"
        return self.pcm[-1]
    async def close(self):
        await self.app._cleanup_session_resources(self.state.session_id)


@pytest.mark.asyncio
async def test_odd_original_list_is_real_pcm_and_one_terminal_opens_answers():
    h = GameRuntime("1-3")
    texts = []
    tts = h.app.bounded_s2s.tts
    while isinstance(tts, CachedTTSProvider):
        tts = tts.delegate
    original = tts.synthesize
    async def synthesize(text, token, cancelled):
        texts.append(text)
        return await original(text, token, cancelled)
    tts.synthesize = synthesize
    try:
        handle, audio = await h.start(4)
        assert handle.response_id == "GAME_PROMPT" and audio.samples
        assert h.state.game_audio_stage == "prompt"
        assert "Что из этого лишнее?" in texts[-1] and "Чтобы выйти, держи семь. Когда скажу «Теперь отпускай», отпусти." in texts[-1]
        assert all(c in texts[-1] for c in
            ("Красный:", "Оранжевый:", "Жёлтый:", "Зелёный:", "Голубой:"))
        cue_owner = h.app._game_prompt_cues[h.state.session_id]
        assert len(cue_owner[3]) == 8
        assert [buttons for _,buttons in cue_owner[3] if buttons] == [(1,), (2,), (3,), (4,), (5,)]
        await h.complete()
        assert h.state.game_audio_stage == "playing"
        assert h.state.active_audio_id is None
        assert not h.app._game_prompt_cues
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_spam_during_tts_cannot_revoke_prompt_and_hangup_fences_late_pcm():
    h = GameRuntime()
    entered, release = asyncio.Event(), asyncio.Event()
    tts = h.app.bounded_s2s.tts
    while isinstance(tts, CachedTTSProvider):
        tts = tts.delegate
    original = tts.synthesize
    async def synthesize(text, token, cancelled):
        if h.state.mode == "game_active" and "лишнее" in text:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # native adapter may complete late
        return await original(text, token, cancelled)
    tts.synthesize = synthesize
    task = asyncio.create_task(h.start(4))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        version, lease = h.state.state_version, h.state.playback_lease
        for button in (1,2,3,4,5,6,1,4):
            await h.key(button)
        assert h.state.state_version == version and h.state.playback_lease == lease
        await h.send("HandsetPlaced")
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not any(handle.response_id == "GAME_PROMPT" for handle,_ in h.pcm)
        assert not h.app._game_prompt_cues and not h.app._game_cue_tasks
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_game_cues_start_at_playback_and_stale_duplicate_cannot_relight():
    h = GameRuntime()
    try:
        handle, _ = await h.start(4)
        sid = h.state.session_id
        owner = h.app._game_prompt_cues[sid]
        # A tiny deterministic cue schedule isolates ownership from TTS speed.
        h.app._game_prompt_cues[sid] = (*owner[:3], ((0., (2,)), (.03, (4,))), "colors")
        h.app._game_audio_started(sid, "stale", handle.playback_lease, handle.generation)
        assert not h.app._game_cue_tasks
        h.app._game_audio_started(sid, handle.playback_id, handle.playback_lease, handle.generation)
        await asyncio.sleep(0)
        assert h.app.led.output.snapshot()[str(BUTTON_FILL_ELEMENTS[2])] == 1
        await h.send("SpeechReceived", text="не хочу играть")
        frame = h.app.led.output.snapshot()
        h.clock[0] += 1.
        await asyncio.sleep(.04)
        h.app._game_audio_started(sid, handle.playback_id, handle.playback_lease, handle.generation)
        assert h.app.led.output.snapshot() == frame
        assert h.state.mode == "game_menu" and not h.app._game_cue_tasks
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_original_question_uses_clean_validated_tts_if_rvc_fails():
    h = GameRuntime()
    calls = []
    original = h.app.bounded_s2s.voice.convert_private
    async def fail(audio, cancelled):
        if h.state.mode == "game_active":
            calls.append("private")
            raise RuntimeError("synthetic")
        return await original(audio, cancelled)
    h.app.bounded_s2s.voice.convert_private = fail
    try:
        handle, audio = await h.start(4)
        assert calls == ["private"]
        assert handle.response_id == "GAME_PROMPT" and audio.samples
        await h.complete()
        assert h.state.game_audio_stage == "playing"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_recorded_quiz_asset_is_used_before_tts():
    h = GameRuntime()
    effect = {"game_id":"quiz", "prompt":"Сколько дней в неделе?", "phrase_asset_id":"PHRASE_049"}
    try:
        segments = h.app._game_prompt_segments(effect)
        assert segments == [(effect["prompt"], (), "PHRASE_049")]
        audio = await asyncio.to_thread(h.app._load_game_asset, "PHRASE_049", 0)
        assert audio.samples and audio.sample_rate > 0
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_word_game_context_retains_own_turns_and_drops_on_exit():
    h = GameRuntime("1-3")
    try:
        await h.start(5)
        await h.complete()
        word = h.app._bounded_dialogue_context(h.state.session_id, game_id="word_game")
        assert "Не требуй букв" in word.system_prompt
        word.history.append(("кот", "собака"))
        assert h.app._bounded_dialogue_context(h.state.session_id, game_id="word_game").history == word.history
        ordinary = h.app._bounded_dialogue_context(h.state.session_id)
        assert ordinary.history == [] and not getattr(ordinary, "_game_id", None)
        h.app._bounded_dialogue_context(h.state.session_id, game_id="word_game")
        await h.send("SpeechReceived", text="не хочу играть")
        assert h.state.session_id not in h.app.conversation_contexts
    finally:
        await h.close()


@pytest.mark.parametrize("display,buttons,expected", [
    ("colors", (5,7), {6,1}), ("pictograms", (6,7), {4,17}),
    ("digits", (5,7), {6,7,1,0}), ("pictograms", (1,3), set())])
def test_game_frame_uses_exact_requested_channel_roles(display, buttons, expected):
    assert {element for element,_ in game_light_commands(buttons, display)} == expected


def test_ripple_two_fronts_replaces_owner_fades_out_and_never_uses_pwm():
    clock = [0.]
    output = FakeLightingOutput()
    engine = ExhibitionLightingEngine(output, monotonic_now=lambda:clock[0], background_scheduler=False)
    engine.start_game_ripple(4)
    first = engine.generation
    assert {int(k) for k,v in output.snapshot().items() if v} == {BUTTON_FILL_ELEMENTS[4]}
    clock[0] = .141
    engine.advance()
    assert {int(k) for k,v in output.snapshot().items() if v} == {BUTTON_FILL_ELEMENTS[b] for b in (3,4,5)}
    engine.start_game_ripple(2)
    assert engine.generation == first + 1
    assert all(command.state in (0,1) and command.transition_ms >= 0 for command in output.commands)
    clock[0] += 2.
    engine.advance()
    assert not any(output.snapshot().values()) and engine.effect == "idle"
    engine.cancel_effects()
    assert engine.advance(clock[0] + 5.) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "failed", "revoked"])
async def test_native_start_callback_follows_stream_start_and_owner(monkeypatch, mode):
    order = []
    owner = [True]
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self):
            order.append("start")
            if mode == "failed": raise OSError("synthetic")
            if mode == "revoked": owner[0] = False
        def write(self, values): order.append("write")
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=Stream))
    controller = SoundDevicePlaybackController()
    controller.active_token = 1
    if mode == "failed":
        with pytest.raises(RuntimeError):
            await controller.play_owned_pcm(AudioChunk((.1,)*20, 24000, 1), lambda:owner[0],
                on_started=lambda:order.append("cue"))
    else:
        await controller.play_owned_pcm(AudioChunk((.1,)*20, 24000, 1), lambda:owner[0],
            on_started=lambda:order.append("cue"))
    assert order == (["start", "cue", "write"] if mode == "ok" else ["start"])


def test_arbitrary_quiz_options_are_spoken_with_their_real_button_mapping():
    from app.foundation.game_presentation import GamePresentationMixin
    effect = {"game_id":"quiz", "prompt":"Кто говорит мяу?", "display":"digits",
              "options":((1,"кошка"),(2,"собака"),(3,"корова"))}
    segments = GamePresentationMixin()._game_prompt_segments(effect)
    assert [text for text,_,_ in segments] == ["Кто говорит мяу?", "Один: кошка.",
        "Два: собака.", "Три: корова.", "Нажми свой ответ."]
    assert [buttons for _,buttons,_ in segments] == [(), (1,), (2,), (3,), ()]


@pytest.mark.asyncio
async def test_physical_game_pcm_uses_native_start_cue_and_exact_terminal():
    from test_owned_private_pcm import setup, install, start, HANDLE, AUDIO
    from app.foundation.s2s import PCMPlaybackResult
    stand = await setup()
    handle = replace(HANDLE, response_id="GAME_PROMPT")
    state = stand.application.canonical_runtime.sessions[handle.session_id]
    state.mode, state.active_game_id, state.game_audio_stage = "game_active", "odd_one_out", "prompt"
    install(stand, handle)
    started = []
    class Playback:
        active_token = 0
        async def play_owned_pcm(self, audio, current, *, on_started=None):
            assert current()
            if on_started: on_started()
            await asyncio.sleep(0)
            return PCMPlaybackResult(0, len(audio.samples), True, True)
        async def stop(self): pass
    stand.playback = Playback()
    stand.application._game_audio_started = lambda *args:started.append(args)
    try:
        await asyncio.gather(*start(stand,handle,AUDIO))
        assert started == [(handle.session_id,handle.playback_id,handle.playback_lease,handle.generation)]
        assert len(stand.events) == 1 and stand.events[0].event_type == "AudioCompleted"
        assert stand.events[0].payload["response_id"] == "GAME_PROMPT"
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_turntaking_native_wrapper_forwards_start_callback(monkeypatch):
    from app.foundation.stand import _HeadlessTurnTakingPlaybackController
    order = []
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self): order.append("stream")
        def write(self, values): order.append("write")
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules,"sounddevice",SimpleNamespace(OutputStream=Stream))
    controller = _HeadlessTurnTakingPlaybackController(None,
        lambda sid:order.append("closed-mic"), lambda owner:order.append("resume-mic"))
    controller.active_token = 1
    await controller.play_owned_pcm(AudioChunk((.1,)*20,24000,1,"word"), lambda:True,
        on_started=lambda:order.append("cue"))
    assert order == ["closed-mic","stream","cue","write","resume-mic"]


@pytest.mark.asyncio
async def test_word_game_runs_actual_bounded_text_path_with_own_history():
    h = GameRuntime("6-7")
    try:
        await h.start(5)
        await h.complete()
        result = await h.send("SpeechReceived", text="Кот")
        effect = next(e for e in result["outputs"] if e.get("type") == "bounded_dialogue_turn")
        assert effect["response_policy"] == "bounded_validated_dialogue"
        context = h.app.conversation_contexts[h.state.session_id]
        assert context._game_id == "word_game" and context.turns == 1
        assert context.history[0][0] == "Кот"
        assert "Ь, Ъ и Ы пропускайте" in context.system_prompt
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_physical_capture_admits_ready_game_menu_and_between_game_audio():
    from test_owned_private_pcm import setup, HANDLE
    stand = await setup()
    original = stand.application.sessions.get
    record = SimpleNamespace(handset_up=True,current_node="game_menu")
    stand.application.sessions.get = lambda sid:record
    state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
    try:
        stand.application._active_audio.clear()
        state.mode, state.scene_menu_phase = "game_menu", "ready"
        state.scene_menu_descriptor = {"items":[{"id":"free_lights"}]}
        assert stand._headless_audio_accepting()
        stand.application._active_audio[HANDLE.session_id] = {"content_id":"MENU_GAME"}
        assert not stand._headless_audio_accepting()
        stand.application._active_audio.clear()
        record.current_node = state.mode = "game_active"
        state.game_audio_stage = "prompt"
        assert not stand._headless_audio_accepting()
        state.game_audio_stage = "playing"
        assert stand._headless_audio_accepting()
        stand.application._active_audio[HANDLE.session_id] = {"content_id":"GAME_PROMPT"}
        assert not stand._headless_audio_accepting()
        record.handset_up = False
        stand.application._active_audio.clear()
        assert not stand._headless_audio_accepting()
    finally:
        stand.application.sessions.get = original
        await stand.aclose()


async def game_voice(h, text, guard=None):
    h.sequence += 1
    return await h.app._receive_canonical_event(Event("SpeechReceived", h.state.session_id,
        h.sequence, 1., "voice-test", {}, str(h.sequence), h.clock[0]), text,
        _speech_ingress_guard=guard, _speech_lifecycle_guard=getattr(guard,"lifecycle_current",guard))


@pytest.mark.asyncio
async def test_game_utterance_survives_button_ripple_but_not_a_different_run():
    from app.foundation.stand import HardwareIntegrationStand
    h = GameRuntime()
    stand = HardwareIntegrationStand(Settings(profile="mock"),background_scheduler=False)
    original_app = stand.application
    stand.application, stand.active_session_id = h.app, h.state.session_id
    try:
        await h.start(1)
        await h.complete()
        _, guard = stand._headless_frame_owner()
        assert guard()
        await h.key(3)
        assert guard(), "ordinary ripple must not revoke the child's speech"
        result = await game_voice(h,"не хочу играть",guard)
        assert result["reason"] == "game_exited_by_voice"
        assert h.state.mode == "game_menu"
        await h.key(2)
        await h.complete()
        assert not guard(), "an old utterance cannot own a different game run"
        result = await game_voice(h,"не хочу играть",guard)
        assert result["reason"] == "stale_session_generation"
        assert h.state.mode == "game_active" and h.state.active_game_id == "quiz"
    finally:
        stand.application = original_app
        await stand.aclose()
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["button", "new-run", "hangup"])
async def test_game_exit_safety_await_allows_same_run_buttons_and_fences_new_owner(change):
    from app.foundation.stand import HardwareIntegrationStand
    h = GameRuntime()
    stand = HardwareIntegrationStand(Settings(profile="mock"),background_scheduler=False)
    original_app = stand.application
    stand.application, stand.active_session_id = h.app, h.state.session_id
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        await h.start(1)
        await h.complete()
        _, guard = stand._headless_frame_owner()
        original = h.app.safety_classifier.classify
        async def delayed(text):
            entered.set()
            await release.wait()
            return await original(text)
        h.app.safety_classifier.classify = delayed
        pending = asyncio.create_task(game_voice(h,"не хочу играть",guard))
        await asyncio.wait_for(entered.wait(), 1.)
        if change == "button":
            await h.key(3)
        elif change == "new-run":
            await h.send("ButtonPressed", {"button_id": 7})
            h.state.game_engine_state = h.state.game_engine_state.__class__(
                **{**h.state.game_engine_state.__dict__, "seven_exit_phase": "ready", "seven_exit_step": 7}
            )
            await h.send("ButtonReleased", {"button_id": 7})
            h.app.safety_classifier.classify = original
            await h.key(2)
            await h.complete()
        else:
            await h.send("HandsetPlaced")
        release.set()
        result = await pending
        if change == "button":
            assert result["reason"] == "game_exited_by_voice"
            assert h.state.mode == "game_menu"
        else:
            assert result["reason"] == "stale_session_generation"
            if change == "new-run":
                assert h.state.mode == "game_active" and h.state.active_game_id == "quiz"
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        stand.application = original_app
        await stand.aclose()
        await h.close()


@pytest.mark.asyncio
async def test_pcm_exit_utterance_keeps_application_phase_owner_across_ripple():
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.s2s import STTResult
    h = GameRuntime()
    stand = HardwareIntegrationStand(Settings(profile="mock"),background_scheduler=False)
    original_app = stand.application
    stand.application, stand.active_session_id = h.app, h.state.session_id
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        await h.start(1)
        await h.complete()
        _, guard = stand._headless_frame_owner()
        async def recognize(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(stt=STTResult("не хочу играть", .99, 1.), turn=None,
                pending=None, barge_in=False, ingress_status="transcribed")
        h.app.s2s_audio.feed_audio = recognize
        h.sequence += 1
        pending = asyncio.create_task(h.app.receive_audio_async(h.state.session_id,
            (.1,)*512, h.sequence, _ingress_guard=guard))
        await asyncio.wait_for(entered.wait(), 1.)
        await h.key(4)
        release.set()
        result = await pending
        assert result["reason"] == "game_exited_by_voice"
        assert h.state.mode == "game_menu"
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        stand.application = original_app
        await stand.aclose()
        await h.close()
