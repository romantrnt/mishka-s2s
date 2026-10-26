from __future__ import annotations

import asyncio
import time

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import (
    BoundedConversationEngine, ConversationContext, MockPlaybackController,
    MockTTSProvider, RVCProvider, S2SConfig, TurnResult,
)
from app.foundation.stand import build_application


def event(kind, sid, sequence, payload=None, source="test"):
    return Event(kind, sid, sequence, 1.0, source, payload or {}, f"{source}:{sequence}", 0.0)


def test_bounded_dialogue_reuses_history_and_clears_on_hangup():
    class FakeConversation:
        def __init__(self):
            self.seen = []

        async def run_text_turn(self, text, context):
            self.seen.append((context, list(context.history), context.turns, context.started_at))
            context.history.append((text, "ответ"))
            context.turns += 1
            return TurnResult(("ответ",), (), False)

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-memory"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        fake = FakeConversation()
        app.bounded_s2s = fake
        for seq, text in ((2, "первый вопрос"), (3, "второй вопрос")):
            result = await app.receive_async(
                event("VoiceIntentReady", sid, seq, {"intent": "free_speech"}),
                transient_text=text,
            )
            assert result["reason"] == "dialogue_turn"
        assert fake.seen[0][1] == []
        assert fake.seen[1][1] == [("первый вопрос", "ответ")]
        assert fake.seen[1][2] == 1
        assert fake.seen[0][0] is fake.seen[1][0]
        assert fake.seen[0][3] == fake.seen[1][3]
        await app.receive_async(event("HandsetPlaced", sid, 4))
        assert sid not in app.conversation_contexts

    asyncio.run(run())


def test_bounded_engine_turn_and_whole_conversation_limits():
    class CountingLLM:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, request_id, cancelled):
            self.calls += 1
            yield "<say>Привет.</say>"

    async def run():
        llm = CountingLLM()
        playback = MockPlaybackController()
        engine = BoundedConversationEngine(
            llm, MockTTSProvider(), RVCProvider(), playback,
            S2SConfig(max_turn_seconds=1),
        )
        context = ConversationContext(
            "limit", "canonical_product", "bear_dialogue", ("__all__",),
            ("diagnosis",), 5, 120, 2,
        )
        for _ in range(5):
            result = await engine.run_text_turn("привет", context)
            assert result.fallback_reason is None
        sixth = await engine.run_text_turn("ещё", context)
        assert sixth.fallback_reason == "bounded_limit"
        assert llm.calls == 5
        assert len(playback.played) == 5

        expired = ConversationContext(
            "expired", "canonical_product", "bear_dialogue", ("__all__",),
            ("diagnosis",), 5, 120, 2,
        )
        expired.started_at = time.monotonic() - 121
        assert (await engine.run_text_turn("привет", expired)).fallback_reason == "bounded_limit"
        assert llm.calls == 5

        class SlowLLM:
            async def stream(self, messages, request_id, cancelled):
                await asyncio.Event().wait()
                yield "<say>поздний ответ.</say>"

        slow_playback = MockPlaybackController()
        slow = BoundedConversationEngine(
            SlowLLM(), MockTTSProvider(), RVCProvider(), slow_playback,
            S2SConfig(max_turn_seconds=1),
        )
        deadline = ConversationContext(
            "deadline", "canonical_product", "bear_dialogue", ("__all__",),
            ("diagnosis",), 5, 0.03, 2,
        )
        result = await asyncio.wait_for(slow.run_text_turn("привет", deadline), 0.5)
        assert result.fallback_reason == "bounded_limit"
        assert not slow_playback.played

    asyncio.run(run())


def test_dialogue_provider_failure_emits_tracked_approved_fallback():
    class Failure:
        async def run_text_turn(self, text, context):
            return TurnResult((), (), True, "provider_or_validation_failure")

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-fallback"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        app.bounded_s2s = Failure()
        result = await app.receive_async(
            event("VoiceIntentReady", sid, 2, {"intent": "free_speech"}),
            transient_text="расскажи",
        )
        spoken = [item for item in result["outputs"] if item.get("type") == "speak"]
        assert spoken and spoken[-1]["phrase_id"] == "PHRASE_104"
        assert spoken[-1].get("playback_id") or spoken[-1].get("resolution") == "recorded_phrase_unavailable_visual_only"
        assert app.canonical_runtime.sessions[sid].active_audio_id == "PHRASE_104"

    asyncio.run(run())


def test_voice_story_pause_stop_resume_pass_graph_without_opening_other_edges():
    from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer

    gate = CanonicalLangGraphAuthorizer()
    assert gate.authorize("fixed_story_playing", "story_pause_announcement", "VoiceIntentReady", intent="pause")["ok"]
    assert gate.authorize("story_speech_analysis", "fixed_story_choice_wave", "VoiceIntentReady", intent="stop")["ok"]
    assert gate.authorize("story_paused", "story_resume_announcement", "VoiceIntentReady", intent="resume")["ok"]
    assert not gate.authorize("fixed_story_playing", "game_active", "VoiceIntentReady", intent="pause")["ok"]


def test_late_bounded_result_after_hangup_cannot_speak_or_restore_context():
    class GatedConversation:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run_text_turn(self, text, context):
            self.entered.set()
            await self.release.wait()
            context.history.append((text, "поздний ответ"))
            return TurnResult((), (), True, "provider_or_validation_failure")

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-late"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        provider = GatedConversation()
        app.bounded_s2s = provider
        pending = asyncio.create_task(app.receive_async(
            event("VoiceIntentReady", sid, 2, {"intent": "free_speech"}),
            transient_text="привет",
        ))
        await asyncio.wait_for(provider.entered.wait(), 1)
        await app.receive_async(event("HandsetPlaced", sid, 3))
        commands_after_down = len(app.audio.commands)
        provider.release.set()
        old = await asyncio.wait_for(pending, 1)
        assert old["reason"] == "stale_session_generation"
        assert len(app.audio.commands) == commands_after_down
        assert sid not in app.conversation_contexts

    asyncio.run(run())


def test_limit_fallback_exits_dialogue_with_approved_recorded_phrase():
    class Limited:
        async def run_text_turn(self, text, context):
            return TurnResult((), (), True, "bounded_limit")

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-limit-fallback"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        app.bounded_s2s = Limited()
        result = await app.receive_async(
            event("VoiceIntentReady", sid, 2, {"intent": "free_speech"}),
            transient_text="ещё",
        )
        spoken = [item for item in result["outputs"] if item.get("type") == "speak"]
        assert spoken and spoken[-1]["phrase_id"] == "PHRASE_095"
        assert result["canonical_state"]["mode"] == "root_menu"
        assert sid not in app.conversation_contexts

    asyncio.run(run())


def test_voice_story_actions_pass_application_graph_and_keep_unrelated_blocked():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        for sid, mode, intent, expected in (
            ("voice-pause", "fixed_story_playing", "pause", "story_pause_requested"),
            ("voice-stop", "story_speech_analysis", "stop", "story_stopped"),
            ("voice-resume", "story_paused", "resume", "story_resume_requested"),
        ):
            await app.receive_async(event("HandsetLifted", sid, 1))
            state = app.canonical_runtime.sessions[sid]
            state.mode = mode
            state.handset = "up"
            state.active_story_id = "fixed_aibolit_v1"
            state.expected_inputs = ["speech", "handset_down"]
            result = await app.receive_async(
                event("VoiceIntentReady", sid, 2, {"intent": intent}),
                transient_text=intent,
            )
            assert result["reason"] == expected
            assert result["reason"] != "transition_rejected"
            await app.receive_async(event("HandsetPlaced", sid, 3))

    asyncio.run(run())


def test_bounded_context_refreshes_prompt_but_never_crosses_profile_or_reset():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-profile"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        state.profile_ref = "opaque-child-a"
        state.display_name = "Аня"
        first = app._bounded_dialogue_context(sid)
        first.history.append(("вопрос", "ответ"))
        first.turns = 1
        started = first.started_at
        state.display_name = "Анна"
        refreshed = app._bounded_dialogue_context(sid)
        assert refreshed is first
        assert refreshed.turns == 1 and refreshed.started_at == started
        assert refreshed.profile.display_name == "Анна"
        assert refreshed.history == [("вопрос", "ответ")]

        state.profile_ref = "opaque-child-b"
        state.display_name = "Боря"
        other = app._bounded_dialogue_context(sid)
        assert other is not first and other.history == [] and other.turns == 0
        assert other.profile.display_name == "Боря"

        await app.reset_session(sid)
        assert sid not in app.conversation_contexts

    asyncio.run(run())


def test_bounded_exception_uses_recorded_failure_fallback():
    class Broken:
        async def run_text_turn(self, text, context):
            raise RuntimeError("synthetic provider failure")

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-exception"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        app.bounded_s2s = Broken()
        result = await app.receive_async(
            event("VoiceIntentReady", sid, 2, {"intent": "free_speech"}),
            transient_text="расскажи",
        )
        assert any(e.get("type") == "speak" and e.get("phrase_id") == "PHRASE_104"
                   for e in result["outputs"])

    asyncio.run(run())


def test_missing_approved_fallback_asset_is_reported_unavailable_not_played():
    from types import SimpleNamespace

    class Failure:
        async def run_text_turn(self, text, context):
            return TurnResult((), (), True, "provider_or_validation_failure")

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "dialogue-missing-fallback"
        await app.receive_async(event("HandsetLifted", sid, 1))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "bear_dialogue"
        state.handset = "up"
        app.bounded_s2s = Failure()
        app.voice_manifest = SimpleNamespace(
            resolve=lambda *args: None,
            diagnose=lambda *args: {"status": "absent", "code": "fixture_missing_asset"},
        )
        result = await app.receive_async(
            event("VoiceIntentReady", sid, 2, {"intent": "free_speech"}),
            transient_text="расскажи",
        )
        assert not any(item.get("response_id") == "PHRASE_104" for item in app.audio.commands)
        assert not any(e.get("audio_played") for e in result.get("outputs", []))
        assert any(e.get("resolution") == "recorded_phrase_unavailable_visual_only"
                   for e in app.last_canonical_effects)

    asyncio.run(run())


def test_stt_failure_does_not_play_adult_handoff_phrase_as_generic_retry():
    from app.foundation.s2s import AudioTurnResult

    class FailedSTT:
        async def feed_audio(self, *_args, **_kwargs):
            return AudioTurnResult(
                "stt_failure", turn=TurnResult((), (), True, "stt_failure"),
            )

    async def run():
        app, _, _, _ = build_application(
            settings=Settings(profile="mock", runtime_mode="clinical"),
        )
        sid = "stt-failure-no-adult-handoff"
        await app.receive_async(event("HandsetLifted", sid, 1))
        app.s2s_audio = FailedSTT()
        before = len(app.audio.commands)
        result = await app.receive_audio_async(sid, (0.0,), 2)
        assert result["reason"] == "audio_retry"
        assert result["audio_status"] == "stt_failure"
        assert not any(
            command.get("response_id") == "PHRASE_091"
            for command in app.audio.commands[before:]
        )

    asyncio.run(run())
