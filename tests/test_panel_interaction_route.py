"""Panel DOWN follows real voice/owned-media paths, with mocked providers only."""
import json
import pytest

from app.foundation.domain import Event
from app.foundation.events import EventValidationError, parse_event
from test_contextual_game_flow import Model
from test_game_audio_reminder_race import DelayedRecognizer, SPEECH, SILENCE
from test_panel_session_actions import PanelStand


async def dialogue(h):
    await h.control("start")
    await h.menu()
    await h.control("enable_audio", {**h.scope(), "state_version": h.state.state_version})
    await h.key(6)
    while "stand" in h.app._active_audio:
        await h.done()
    assert h.state.mode == "bear_dialogue" and h.state.handset == "down"


@pytest.mark.asyncio
async def test_actual_browser_pcm_vad_stt_semantic_dialogue_works_at_down_without_physical_mic(tmp_path):
    h = PanelStand(tmp_path)
    recognizer = DelayedRecognizer("Как дела?")
    recognizer.release.set()
    model = Model("<say>Я рад тебя слышать.</say>")
    h.app.bounded_s2s.llm = model
    h.app.s2s_audio.recognizer = recognizer
    try:
        await dialogue(h)
        assert h.stand._interaction_audio_accepting()
        assert not h.stand._headless_audio_accepting()
        assert h.stand.panel_snapshot()["simulator"]["microphone"]["eligible"]
        checked = []
        original = h.app.safety_classifier.classify
        async def classify(text, **kwargs):
            checked.append(text)
            return await original(text, **kwargs)
        h.app.safety_classifier.classify = classify
        result = await h.stand.inject_pcm(SPEECH + SILENCE, 16000, flush_silence=False)
        assert result["reason"] == "dialogue_turn", result
        assert recognizer.calls == 1 and checked == ["Как дела?"] and len(model.calls) == 1
        prompt = model.calls[0][0]["content"]
        context = json.loads(next(line.removeprefix("RUNTIME_CONTEXT_DATA=")
                                  for line in prompt.splitlines() if line.startswith("RUNTIME_CONTEXT_DATA=")))
        assert context["handset"] == "down" and context["interaction_active"] is True
        assert context["interaction_route"] == "panel"
        assert h.state.handset == "down" and not h.app.sessions.get("stand").handset_up
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_word_game_natural_finish_restores_one_caller_at_down(tmp_path):
    h = PanelStand(tmp_path)
    h.app.bounded_s2s.llm = Model('<action type="offer_game" game_id="word_game"/>',
        *("<say>Лиса, теперь твоя очередь.</say>" for _ in range(4)),
        "<say>Спасибо, мы закончили играть.</say>")
    try:
        await dialogue(h)
        await h.stand.inject("text", {"text": "Давай играть в слова"})
        assert h.state.pending_game_offer == "word_game"
        await h.done()
        await h.key(1)
        while "stand" in h.app._active_audio:
            await h.done()
        assert h.state.active_game_id == "word_game" and h.state.game_audio_stage == "playing"
        caller = h.app._game_callers["stand"]["context"]
        for word in ("Кот", "Лиса", "Слон", "Заяц", "Белка"):
            await h.stand.inject("text", {"text": word})
            if h.state.mode == "bear_dialogue":
                break
        assert h.state.mode == "bear_dialogue" and not h.app._game_callers
        assert h.app.conversation_contexts["stand"] is caller
        assert h.state.handset == "down" and h.state.interaction_route == "panel"
        await h.key(7)
        assert h.state.mode == "root_menu"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_fixed_story_menu_selection_and_back_use_current_owned_media_at_down(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        await h.menu()
        await h.control("enable_audio", {**h.scope(), "state_version": h.state.state_version})
        await h.key(2)
        old = dict(h.app._active_audio["stand"])
        selected = h.state.scene_menu_descriptor["items"][0]
        await h.key(selected["slot"])
        assert h.state.mode == "fixed_story_playing" and h.state.active_story_id == selected["id"]
        assert h.state.handset == "down"
        await h.key(7)
        current = dict(h.app._active_audio["stand"])
        await h.done(old)
        assert h.state.mode == "fixed_story_choice_wave"
        assert h.state.active_audio_id == "PHRASE_066"
        assert h.app._active_audio["stand"] == current
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_untrusted_session_and_skip_events_cannot_bypass_scoped_actions(tmp_path):
    h = PanelStand(tmp_path)
    try:
        for kind in ("SessionStarted", "InterviewSkipped"):
            with pytest.raises(EventValidationError):
                parse_event({"type": kind}, "stand", 1)
            result = await h.app.receive_async(Event(kind, "stand", 1, 1., "forged", {}, kind))
            assert result["reason"] == "session_action_untrusted"
        assert h.state is None and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_start_and_skip_are_real_activity_but_rejected_replays_do_not_extend_120(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        owner = h.app._engagement_owners["stand"]
        nonce = owner["nonce"]
        h.harness.clock.advance(1)
        await h.control("start")
        assert h.app._engagement_owners["stand"] is owner and owner["nonce"] == nonce
        await h.menu()
        await h.key(6)
        while "stand" in h.app._active_audio:
            await h.done()
        owner = h.app._engagement_owners["stand"]
        deadline, nonce = owner["deadline"], owner["nonce"]
        assert (await h.control("skip_interview"))["reason"] == "interview_skip_unavailable"
        assert (await h.control("start"))["reason"] == "interaction_already_started"
        assert (owner["deadline"], owner["nonce"]) == (deadline, nonce)
        h.harness.clock.advance(deadline - h.app.monotonic_now() - .01)
        assert await h.app.expire_engagement_sessions() == []
        h.harness.clock.advance(.02)
        assert (await h.app.expire_engagement_sessions())[0]["reason"] == "interaction_idle"
        assert h.state.interaction_idle and h.state.mode == "root_menu"
        assert not h.app._active_audio and not h.stand._interaction_audio_accepting()
        assert not any(h.stand.lighting_output.snapshot().values())
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_playing_public_story_is_busy_and_pause_has_its_own_120_wait(tmp_path):
    h = PanelStand(tmp_path)
    try:
        await h.control("start")
        await h.menu()
        await h.control("enable_audio", {**h.scope(), "state_version": h.state.state_version})
        await h.key(2)
        await h.key(h.state.scene_menu_descriptor["items"][0]["slot"])
        assert h.state.mode == "fixed_story_playing"
        h.harness.clock.advance(400)
        assert await h.app.expire_engagement_sessions() == []
        assert h.state.mode == "fixed_story_playing"
        result = await h.key(6)
        assert result["reason"] == "story_pause_requested"
        assert h.state.active_audio_id == "PHRASE_062"
        await h.done()
        assert h.state.mode == "story_paused"
        deadline = h.app._engagement_owners["stand"]["deadline"]
        h.harness.clock.advance(deadline - h.app.monotonic_now())
        assert (await h.app.expire_engagement_sessions())[0]["reason"] == "interaction_idle"
        assert h.state.mode == "root_menu" and h.state.handset == "down"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_max_absolute_authorization_still_expires_during_panel_down(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    try:
        await h.ready()
        deadline = h.state.identity_expires_monotonic
        await h.control("start")
        assert h.state.identity_ready_deadline is None
        assert h.state.identity_expires_monotonic == deadline
        h.harness.clock.advance(deadline - h.app.monotonic_now() + .01)
        result = await h.app.expire_identity_leases()
        assert result[0]["reason"] == "identity_expired"
        assert not h.state.interaction_open and not h.app._canonical_profiles
        assert not h.app._active_audio and h.state.handset == "down"
    finally:
        await h.close()
