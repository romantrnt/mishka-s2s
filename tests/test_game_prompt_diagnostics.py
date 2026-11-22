"""A failed owned game prompt exposes a bounded cause without speech or PCM."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.foundation.game_engine import current_challenge
from app.foundation.config import Settings
from app.foundation.s2s import AudioChunk, SileroTTSProvider
from app.foundation.stand import create_stand_app
from app.foundation.tts_engine import SileroTTSEngine
from test_scene_menu_runtime import Harness


async def start_odd_game(h):
    await h.button(1)
    await h.release(1)
    h.state.game_entry_seed = 0
    assert (await h.button(4))["reason"] == "game_started"
    await h.release(4)
    assert (await h.complete())["reason"] == "game_prompt_started"
    assert (await h.complete())["reason"] == "game_prompt_finished"


def diagnostics(h):
    return [item for item in h.app._audio_completion_diagnostics
            if item["event"] == "game_prompt_render_failed"]


@pytest.mark.asyncio
async def test_actual_odd_answer_retry_then_next_question_keeps_one_game_owner():
    h = Harness("4-5")
    try:
        await start_odd_game(h)
        engine = h.state.game_engine_state
        run_id, challenge_id = engine.run_id, current_challenge(engine).id
        answer = current_challenge(engine).answer
        wrong = next(button for button, _ in current_challenge(engine).options if button != answer)
        retry = await h.button(wrong)
        await h.release(wrong)
        assert retry["reason"] == "game_retry"
        assert h.played[-1].response_id == "GAME_PROMPT"
        assert (await h.complete())["reason"] == "game_prompt_finished"
        progress = await h.button(answer)
        await h.release(answer)
        assert progress["reason"] == "game_progress"
        assert h.state.game_engine_state.run_id == run_id
        assert h.state.game_engine_state.round == 1
        assert current_challenge(h.state.game_engine_state).id != challenge_id
        assert h.played[-1].response_id == "GAME_PROMPT"
        assert (await h.complete())["reason"] == "game_prompt_finished"
        assert h.state.mode == "game_active" and not diagnostics(h)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_odd_prompt_and_retry_accept_finite_silero_model_overshoot():
    import torch
    class Model:
        def apply_tts(self, **kwargs):
            return torch.tensor((1.0080755949020386, -.5040377974510193), dtype=torch.float32)
    h = Harness("4-5")
    try:
        engine = SileroTTSEngine()
        engine._model = Model()
        h.app.bounded_s2s.tts = SileroTTSProvider(engine)
        await start_odd_game(h)
        challenge = current_challenge(h.state.game_engine_state)
        wrong = next(button for button, _ in challenge.options if button != challenge.answer)
        assert (await h.button(wrong))["reason"] == "game_retry"
        await h.release(wrong)
        assert h.played[-1].response_id == "GAME_PROMPT"
        assert (await h.complete())["reason"] == "game_prompt_finished"
        assert h.state.mode == "game_active" and not diagnostics(h)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason,exception_class", [
    ("timeout", "render_timeout", None),
    ("invalid_pcm", "invalid_tts_pcm", None),
    ("native_queue_full", "native_queue_full", "RuntimeError"),
    ("exception", "render_exception", "RuntimeError"),
])
async def test_actual_immediate_odd_answer_failure_records_fixed_cause(fault, reason, exception_class):
    h = Harness("4-5")
    try:
        await start_odd_game(h)
        if fault == "timeout":
            h.app.audio.owned_render_timeout_s = .01
            h.render.block = asyncio.Event()
        else:
            async def synthesize(text, token, cancelled):
                if fault == "invalid_pcm":
                    return AudioChunk((1.01,), 16000, token)
                if fault == "native_queue_full":
                    raise RuntimeError("Silero TTS worker queue is full")
                raise RuntimeError("private phrase Bearer secret token=private-pcm")
            h.render.synthesize = synthesize
        challenge = current_challenge(h.state.game_engine_state)
        wrong = next(button for button, _ in challenge.options if button != challenge.answer)
        result = await h.button(wrong)
        assert result["reason"] == "game_prompt_audio_failed"
        assert h.state.mode == "root_menu" and h.sid not in h.app._active_audio
        [diagnostic] = diagnostics(h)
        assert diagnostic["reason"] == reason and diagnostic["stage"] == "synthesis"
        assert diagnostic["chunk_index"] == 0 and diagnostic["elapsed_ms"] >= 0
        assert diagnostic.get("exception_class") == exception_class
        assert set(diagnostic) <= {"event", "event_type", "timestamp", "session_id", "generation",
            "operation", "reason", "stage", "chunk_index", "elapsed_ms", "exception_type", "exception_class"}
        assert "private phrase" not in str(diagnostic) and "secret" not in str(diagnostic)
        public = h.app.canonical_diagnostics[-1]
        assert public["kind"] == "game_prompt_render_failed" and public["code"] == reason
        assert set(public) == {"kind", "code", "stage", "generation", "at_monotonic"}
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason", [
    ("capacity", "render_capacity"),
    ("validation", "validation_blocked"),
    ("text", "invalid_text"),
    ("baseline", "baseline_unavailable"),
])
async def test_game_preparation_rejection_has_a_distinct_bounded_cause(fault, reason):
    h = Harness("4-5")
    extra_jobs = set()
    try:
        if fault == "capacity":
            extra_jobs = {asyncio.create_task(asyncio.Event().wait()) for _ in range(2)}
            h.app._owned_text_jobs.update(extra_jobs)
        elif fault == "validation":
            async def reject(text):
                return SimpleNamespace(allowed=False)
            h.app.bounded_s2s.response_validator.validate_trusted = reject
        elif fault == "text":
            h.app.bounded_s2s.normalizer.normalize = lambda text: ""
        elif fault == "baseline":
            async def unavailable(*args):
                return None
            h.app._assemble_game_audio = unavailable
            h.app.bounded_s2s.voice = object()
        rendered = await h.app._render_game_prompt(h.sid, {"prompt": "Найди лишнее."}, 1, lambda: True)
        assert rendered is None
        assert diagnostics(h)[-1]["reason"] == reason
    finally:
        h.app._owned_text_jobs.difference_update(extra_jobs)
        for task in extra_jobs:
            task.cancel()
        await asyncio.gather(*extra_jobs, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_game_pcm_dispatch_exception_is_redacted_and_diagnosable():
    h = Harness("4-5")
    try:
        await h.button(1)
        await h.release(1)
        await h.button(4)
        await h.release(4)
        def unavailable(handle, audio):
            raise OSError("private PCM and private phrase")
        h.app.audio.play_pcm_owned = unavailable
        assert (await h.complete())["reason"] == "game_prompt_audio_failed"
        item = h.app._audio_completion_diagnostics[-1]
        assert item["event"] == "game_prompt_dispatch_failed"
        assert item["reason"] == "pcm_dispatch_exception" and item["exception_class"] == "OSError"
        assert "private" not in str(item)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_render_game_prompt_retries_experimental_voice_busy():
    from app.foundation.experimental_voice import ExperimentalVoiceUnavailable

    h = Harness("4-5")
    attempts = 0
    try:
        async def flaky_synthesize(text, token, cancelled):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ExperimentalVoiceUnavailable("experimental_voice_busy")
            return AudioChunk((0.1,) * 240, 24000, token)

        tts = SimpleNamespace(provider_name="vox555", synthesize=flaky_synthesize)
        h.app.bounded_s2s.tts = tts
        h.app.bounded_s2s.voice = object()
        rendered = await h.app._render_game_prompt(h.sid, {"prompt": "Найди лишнее."}, 1, lambda: True)
        assert attempts == 3
        assert rendered is not None and len(rendered[0].samples) > 0
        assert not diagnostics(h)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_stand_diagnostics_endpoint_preserves_fixed_game_failure():
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator"))
    stand = api.state.stand
    try:
        stand.application.bounded_s2s = None
        assert await stand.application._render_game_prompt(
            stand.active_session_id, {"prompt": "private phrase"}, 1, lambda: True) is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://stand") as client:
            response = await client.get("/stand/diagnostics")
        assert response.status_code == 200
        [item] = [item for item in response.json()["diagnostics"]
                  if item.get("kind") == "game_prompt_render_failed"]
        assert item["code"] == "engine_unavailable" and item["stage"] == "admission"
        assert type(item["generation"]) is int and type(item["at_monotonic"]) in (int, float)
        assert set(item) == {"kind", "code", "stage", "generation", "at_monotonic"}
        assert "private phrase" not in str(item)
    finally:
        await stand.aclose()
