"""Repeated cards through the real JSON boundary and application timer producers."""
import asyncio

import pytest

from app.foundation import application
from app.foundation.domain import Event
from test_private_welcome import setup, start
from test_profile_onboarding import card


async def end_profile(h, cause):
    if cause == "authorization_expired":
        h.clock[0] = h.state.identity_expires_monotonic
        await h.app.expire_identity_leases()
    elif cause != "hangup":
        h.clock[0] += 120
        h.client.value = None
        h.client.events.append({"kind": "clear", "reason_code": cause})
        assert (await h.boundary.step())["reason"] == "profile_cleared"
    if cause != "hangup":
        assert h.state.mode == "session_closed"
        assert h.state.profile_status == "anonymous" and h.state.display_name is None
    await h.send("HandsetPlaced")
    h.clock[0] += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["session_expired", "scan_started", "authorization_expired", "hangup"])
@pytest.mark.parametrize("private_pcm", [True, False])
async def test_repeated_profiles_start_welcome_once_at_each_real_timer_deadline(cause, private_pcm):
    h, render = setup()
    h.app.audio.private_pcm_supported = private_pcm
    old_deadline = old_callback = old_sequence = None
    try:
        for name, age in (("Аня", 4), ("Борис", 7), ("Вера", 6)):
            h.client.value = card(name, age)
            assert (await h.ready())["reason"] == "profile_ready"
            await h.send("HandsetLifted")
            for _ in range(3):
                await asyncio.sleep(0)
            deadline = h.state.welcome_deadline_monotonic
            assert h.state.display_name == name and h.state.age_years == age
            assert h.state.age_band_source == "data14_age_years"
            if old_callback is not None:
                assert (await h.send("AudioCompleted", old_callback))["reason"] == "stale_exhibition_audio_completion"
                assert await h.app._fire_exhibition_welcome(h.boundary.session_id, old_deadline) is None
            assert (await h.complete())["reason"] == "ringtone_finished"
            h.clock[0] = deadline - .000001
            assert await h.app.flush_welcome_timers() == []
            h.clock[0] = deadline
            fired = await h.app.flush_welcome_timers()
            assert len(fired) == 1
            assert fired[0]["reason"] == ("personal_welcome_started" if private_pcm else "welcome_started")
            if old_sequence is not None and cause != "hangup":
                # Retained canonical admission still rejects an old source
                # sequence, even with a different correlation ID.
                stale = await h.app.receive_async(Event(
                    "WelcomeTimerFired", h.boundary.session_id, old_sequence, 1.,
                    "canonical_welcome_timer", {"deadline_monotonic": old_deadline},
                    f"late-welcome:{old_sequence}", h.clock[0],
                ))
                assert stale["reason"] == "stale_sequence" and not stale["outputs"]
            old_sequence = h.state.last_sequence_by_source["canonical_welcome_timer"]
            assert await h.app.flush_welcome_timers() == []
            old_deadline, old_callback = deadline, h.callback()
            assert (await h.complete())["reason"] == (
                "personal_welcome_menu_started" if private_pcm else "authorized_profile_menu_started")
            assert h.state.mode == "exhibition_menu_tutorial_prompt"
            assert not h.state.onboarding_name_collected
            await end_profile(h, cause)
        assert not set(h.played) & {"PHRASE_078", "PHRASE_099", "PHRASE_108", "PHRASE_075"}
        assert render.texts == ([f"Привет, {name}. Я Мишка." for name in ("Аня", "Борис", "Вера")]
                                if private_pcm else [])
        assert len(h.pcm) == (3 if private_pcm else 0)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_repeated_profile_menu_clock_preserves_sequences_and_rejects_old_token(monkeypatch):
    # Exercise the actual clock task without waiting for the recorded WAV.
    monkeypatch.setattr(application, "EXHIBITION_MENU_PHASE_TIMINGS_MS", tuple(
        (button, phase, 0) for button, phase, _ in application.EXHIBITION_MENU_PHASE_TIMINGS_MS))
    h, _ = setup()
    old_token = old_sequence = None
    try:
        for name in ("Аня", "Борис", "Вера"):
            h.client.value = card(name)
            await start(h)
            h.clock[0] = h.state.welcome_deadline_monotonic
            # This test isolates the independent menu producer; the real
            # welcome timer and its admission fence are covered above.
            assert (await h.send("WelcomeTimerFired"))["reason"] == "personal_welcome_started"
            await h.complete()
            h.app.background_scheduler = True
            try:
                await h.complete()  # PHRASE_007 starts PHRASE_008 and its clock.
            finally:
                h.app.background_scheduler = False
            task = h.app._menu_tutorial_tasks[h.boundary.session_id]
            await task
            assert h.state.menu_tutorial_step == 7 and h.state.menu_tutorial_phase == "full"
            assert h.state.menu_lit_buttons == list(range(1, 8))
            if old_token is not None:
                late = await h.send("MenuTutorialStep", {"button": 1, "phase": "digit", "token": old_token})
                assert late["reason"] == "stale_exhibition_menu_tutorial_step" and not late["outputs"]
                assert h.state.last_sequence_by_source["canonical_menu_tutorial_clock"] > old_sequence
            old_token = h.app.sessions.get(h.boundary.session_id).cancellation_token
            old_sequence = h.state.last_sequence_by_source["canonical_menu_tutorial_clock"]
            assert (await h.complete())["reason"] == "exhibition_menu_audio_completed"
            await end_profile(h, "session_expired")
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pending", "playing"])
async def test_hangup_fences_old_welcome_before_next_guest(stage):
    h, _ = setup("Аня")
    try:
        await start(h)
        deadline = h.state.welcome_deadline_monotonic
        if stage == "playing":
            h.clock[0] = deadline
            assert (await h.app.flush_welcome_timers())[0]["reason"] == "personal_welcome_started"
        old_callback = h.callback()
        await h.send("HandsetPlaced")
        assert h.boundary.session_id not in h.app.canonical_runtime.sessions
        h.clock[0] = deadline + 1
        assert await h.app.flush_welcome_timers() == []
        assert await h.app._fire_exhibition_welcome(h.boundary.session_id, deadline) is None
        await h.send("HandsetLifted")
        assert h.state.profile_status == "anonymous" and h.state.display_name is None
        assert (await h.send("AudioCompleted", old_callback))["reason"] == "stale_exhibition_audio_completion"
        h.clock[0] = h.state.welcome_deadline_monotonic
        assert (await h.app.flush_welcome_timers())[0]["reason"] == "welcome_started"
        assert (await h.complete())["reason"] == "exhibition_introduction_started"
        assert len(h.pcm) == (1 if stage == "playing" else 0)
    finally:
        await h.close()
