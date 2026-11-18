"""Synthetic RFID through the real stand/owner PCM/HTTP callback boundary."""
import asyncio
import base64
import json
import struct

import httpx
import pytest

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lifecycle import RuntimeState
from app.foundation.s2s import AudioChunk, CachedTTSProvider
from app.foundation.stand import create_stand_app
from test_profile_onboarding import Client, card
from test_private_welcome import Render


class Socket:
    def __init__(self):
        self.messages = []
        self.on_pcm = None

    async def send_json(self, payload):
        self.messages.append(payload)
        if payload.get("type") == "audio_pcm_output" and self.on_pcm:
            self.on_pcm()

    async def close(self, **kwargs):
        pass


async def setup_browser(owner=True):
    client = Client(card("Люси-Анна"))
    api = create_stand_app(Settings(profile="mock", identity_mode="max_json",
        stand_access_token="private-pcm-unit-test"), phone_client=client)
    stand = api.state.stand
    api.state.lifecycle._state = RuntimeState.READY
    clock = [10.0]
    stand.monotonic_now = stand.application.monotonic_now = lambda: clock[0]
    render = Render()
    render.voice_audio = AudioChunk((.1,) * 9000, 24000, 0)
    stand.application.bounded_s2s.tts = CachedTTSProvider(render)
    stand.application.bounded_s2s.voice = render
    boundary = stand.application._max_client_boundary
    boundary.start(background=False)
    writer, observer = Socket(), Socket()
    if owner:
        assert await stand.hub.connect(writer)
        assert not await stand.hub.connect(observer)
    client.events.append({"kind": "patient_ready"})
    await boundary.step()
    return api, stand, boundary, render, clock, writer, observer


async def event(stand, kind, sequence, payload=None):
    return await stand.application.receive_async(Event(kind, stand.active_session_id,
        sequence, 1.0, "synthetic-private-pcm-test", payload or {}, str(sequence)))


async def drain(stand):
    for _ in range(3):
        tasks = tuple(stand._notification_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0)


async def welcome(stand, clock):
    await event(stand, "HandsetLifted", 1)
    for _ in range(4):
        await asyncio.sleep(0)
    clock[0] = 15.0
    result = await event(stand, "WelcomeTimerFired", 2)
    await drain(stand)
    return result


async def cleanup(stand, boundary):
    await boundary.close()
    await stand.aclose()


@pytest.mark.asyncio
async def test_named_welcome_owner_pcm_requires_real_browser_completion():
    api, stand, boundary, render, clock, writer, observer = await setup_browser()
    try:
        assert (await welcome(stand, clock))["reason"] == "personal_welcome_started"
        frames = [x for x in writer.messages if x["type"] == "audio_pcm_output"]
        assert len(frames) == 3
        assert frames[0]["first"] and not frames[0]["final"]
        assert frames[-1]["final"] and not frames[-1]["first"]
        assert all(x["private"] for x in frames)
        assert not any(x["type"] == "audio_pcm_output" for x in observer.messages)
        samples = b"".join(base64.b64decode(x["data_b64"], validate=True) for x in frames)
        assert len(samples) == 18000 and struct.unpack_from("<h", samples)[0] == 3277
        identity = frames[-1]["playback"]
        assert stand._browser_pcm_callback_current(identity)
        assert identity["response_id"] == "PERSONAL_WELCOME"
        assert all(x["playback"] == identity for x in frames)
        assert stand.application.canonical_runtime.sessions[stand.active_session_id].mode == "voice_welcome"
        assert render.texts == ["Привет, Люси-Анна. Я Мишка."]
        assert not stand.application.bounded_s2s.tts.cache
        public = json.dumps([stand.panel_snapshot(), list(stand.commands), list(stand.errors), observer.messages], ensure_ascii=False)
        assert "Люси-Анна" not in public and "data_b64" not in public
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                base_url="http://stand", headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            for status in ("playing", "finished"):
                response = await client.post("/stand/audio/lifecycle", json={**identity, "status": status})
                assert response.status_code == 200 and response.json().get("audio_status") == status, response.json()
            response = await client.post("/stand/audio/completed", json=identity)
            assert response.status_code == 200
            assert response.json()["reason"] == "personal_welcome_menu_started"
        state = stand.application.canonical_runtime.sessions[stand.active_session_id]
        assert state.mode == "exhibition_menu_tutorial_prompt"
    finally:
        await cleanup(stand, boundary)


@pytest.mark.asyncio
async def test_no_write_owner_does_not_prepare_or_publish_private_name():
    api, stand, boundary, render, clock, writer, observer = await setup_browser(owner=False)
    try:
        assert not stand.audio_output.private_pcm_supported
        await welcome(stand, clock)
        assert not render.texts and not writer.messages
        assert any(x["type"] == "audio_play" and x["payload"].get("response_id") == "PHRASE_001"
                   for x in stand.commands)
    finally:
        await cleanup(stand, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["disconnect", "hangup", "identity"])
async def test_private_pcm_stops_at_owner_revocation_and_rejects_completion(interrupt):
    api, stand, boundary, render, clock, writer, observer = await setup_browser()
    try:
        def interrupt_after_first():
            writer.on_pcm = None
            if interrupt == "disconnect":
                stand.hub.disconnect(writer)
            elif interrupt == "hangup":
                stand.application.canonical_runtime.sessions[stand.active_session_id].handset = "down"
            else:
                stand.application.canonical_runtime.sessions[stand.active_session_id].identity_capabilities = []
        writer.on_pcm = interrupt_after_first
        await welcome(stand, clock)
        frames = [x for x in writer.messages if x["type"] == "audio_pcm_output"]
        assert len(frames) == 1 and not frames[0]["final"]
        identity = frames[0]["playback"]
        assert not stand._browser_pcm_callback_current(identity)
        if interrupt == "disconnect":
            replacement = Socket()
            assert await stand.hub.connect(replacement)
            assert not stand.audio_output.private_pcm_supported
            assert not any(x["type"] == "audio_pcm_output" for x in replacement.messages)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                base_url="http://stand", headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            reply = await client.post("/stand/audio/completed", json=identity)
            assert reply.json()["reason"] == "stale_private_pcm_callback"
        assert stand.application.canonical_runtime.sessions[stand.active_session_id].mode != "exhibition_menu_tutorial_prompt"
        assert not any(x["type"] == "audio_pcm_output" for x in observer.messages)
    finally:
        await cleanup(stand, boundary)


@pytest.mark.asyncio
async def test_game_menu_and_quiz_prompt_reach_browser_pcm_and_complete():
    api, stand, boundary, render, clock, writer, observer = await setup_browser()
    try:
        await event(stand, "HandsetLifted", 1)
        stand.application._discard_private_welcome(stand.active_session_id)
        stand.application._invalidate_audio_lease(stand.active_session_id)
        state = stand.application.canonical_runtime.sessions[stand.active_session_id]
        state.mode = "root_menu"
        state.active_audio_id = state.playback_lease = None
        assert stand.application._owned_menu_voice_capable()
        result = await event(stand, "ButtonPressed", 2, {"button_id": 1})
        await event(stand, "ButtonReleased", 3, {"button_id": 1})
        await drain(stand)
        invitation = next(x for x in reversed(stand.commands) if x["type"] == "audio_play"
                          and x["payload"].get("response_id") == "PHRASE_019")
        invitation_identity = {key: invitation["payload"][key] for key in (
            "session_id", "response_id", "playback_id", "playback_lease", "generation", "token")}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                base_url="http://stand", headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            response = await client.post("/stand/audio/completed", json=invitation_identity)
            assert response.status_code == 200
        await drain(stand)
        menu_frames = [x for x in writer.messages if x["type"] == "audio_pcm_output"]
        assert menu_frames, str({"outputs": result.get("outputs"), "texts": render.texts,
                                 "state": (state.mode, state.scene_menu_phase),
                                 "lease": stand.application._active_audio.get(stand.active_session_id)})
        assert menu_frames[-1]["playback"]["response_id"] == "MENU_GAME"
        assert state.mode == "game_menu" and state.scene_menu_phase == "narrating"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                base_url="http://stand", headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            response = await client.post("/stand/audio/completed", json=menu_frames[-1]["playback"])
            assert response.status_code == 200
            assert state.scene_menu_phase == "repeat_prompt"
            # A valid choice interrupts the repeat prompt and selects on this edge.
            await event(stand, "ButtonPressed", 4, {"button_id": 2})
            await event(stand, "ButtonReleased", 5, {"button_id": 2})
            await drain(stand)
            start = next(x for x in reversed(stand.commands) if x["type"] == "audio_play"
                         and x["payload"].get("response_id") == "PHRASE_020")
            identity = {key: start["payload"][key] for key in (
                "session_id", "response_id", "playback_id", "playback_lease", "generation", "token")}
            response = await client.post("/stand/audio/completed", json=identity)
            assert response.status_code == 200 and response.json()["reason"] == "game_prompt_started"
            await drain(stand)
            quiz_frames = [x for x in writer.messages if x["type"] == "audio_pcm_output"
                           and x["playback"]["response_id"] == "GAME_PROMPT"]
            assert quiz_frames and quiz_frames[-1]["final"]
            identity = quiz_frames[-1]["playback"]
            response = await client.post("/stand/audio/lifecycle", json={**identity, "status": "playing"})
            assert response.json().get("audio_status") == "playing", response.json()
            response = await client.post("/stand/audio/completed", json=identity)
            assert response.json()["reason"] == "game_prompt_finished"
            assert state.mode == "game_active" and state.game_audio_stage == "playing"
        assert not any(x["type"] == "audio_pcm_output" for x in observer.messages)
    finally:
        await cleanup(stand, boundary)
