import pytest

from app.foundation.stand import HardwareIntegrationStand


@pytest.mark.asyncio
@pytest.mark.parametrize("in_flush", [False, True])
@pytest.mark.parametrize("status", ["stt_busy", "stt_unknown"])
@pytest.mark.parametrize("wait_for_turn", [False, True])
async def test_upload_returns_first_terminal_retry_without_feeding_a_new_utterance(in_flush, status, wait_for_turn):
    stand = object.__new__(HardwareIntegrationStand)
    calls = []
    published = []
    retry = {"reason": "audio_retry", "audio_status": status, "pending": False,
             "outputs": [{"action": "audio_retry", "reason": status}]}
    terminal_at = 3 if in_flush else 1

    async def inject(*args, **kwargs):
        calls.append(args)
        return retry if len(calls) == terminal_at else {"reason": "collecting", "pending": False}

    stand.inject = inject
    async def publish(sid, result):
        published.append((sid, result))
    stand._publish_audio_completion = publish
    result = await stand.inject_pcm((0.1,) * 1024, 16000, "synthetic", wait_for_turn=wait_for_turn, _ingress_guard=lambda: True)
    assert result == retry
    assert len(calls) == terminal_at
    assert published == ([("synthetic", retry)] if wait_for_turn else [])
    calls.clear()
    # A retry does not disable the next independent upload.
    result = await stand.inject_pcm((0.1,) * 1024, 16000, "synthetic", wait_for_turn=wait_for_turn, _ingress_guard=lambda: True)
    assert result == retry
    assert len(calls) == terminal_at
    assert published == ([("synthetic", retry), ("synthetic", retry)] if wait_for_turn else [])


@pytest.mark.asyncio
async def test_old_upload_guard_overrides_a_terminal_retry():
    stand = object.__new__(HardwareIntegrationStand)
    live = True

    async def inject(*args, **kwargs):
        nonlocal live
        live = False
        return {"reason": "audio_retry", "audio_status": "stt_busy"}

    stand.inject = inject
    assert await stand.inject_pcm((0.1,) * 1024, 16000, "synthetic", _ingress_guard=lambda: live) == {
        "reason": "stale_session_generation", "outputs": []}
