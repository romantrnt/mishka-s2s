"""Real private PCM/UART/ASGI checkpoint with the actual browser audio functions."""
import asyncio
import json

import httpx
import pytest

from app.foundation.personal_story import STORY_ID, StoryOwner
from app.foundation.s2s import AudioChunk
from test_browser_private_pcm import setup_browser, cleanup, event, drain


class Browser:
    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "node", "tests/console_personal_story_regression.js", "--bridge",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        return self

    async def call(self, **value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()
        line = await asyncio.wait_for(self.process.stdout.readline(), 3)
        result = json.loads(line)
        assert "error" not in result, result.get("error")
        return result

    async def close(self):
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 3)


async def start_story():
    api, stand, boundary, render, clock, writer, observer = await setup_browser()
    app, sid = stand.application, stand.active_session_id
    await event(stand, "HandsetLifted", 1)
    app._discard_private_welcome(sid)
    app._invalidate_audio_lease(sid)
    state = app.canonical_runtime.sessions[sid]
    state.mode, state.handset = "personal_story_playing", "up"
    state.active_story_id = state.active_audio_id = STORY_ID
    state.playback_lease = "initial-personal-story-lease"
    state.expected_inputs = ["button_6", "button_7", "handset_down"]
    owner = StoryOwner(app._session_epochs.get(sid, 0), "synthetic-story-owner",
        (state.profile_ref, state.identity_request_id), state.age_band, 100.,
        audio=AudioChunk((.1,) * 80000, 16000, 0))
    app._personal_stories[sid] = owner
    assert stand._browser_private_pcm_available()
    assert app._play_personal_story(sid, {"playback_lease": state.playback_lease},
                                    app.sessions.get(sid).cancellation_token)
    await drain(stand)
    frames = [item for item in writer.messages if item.get("type") == "audio_pcm_output"
              and item["playback"]["response_id"] == STORY_ID]
    assert frames
    return api, stand, boundary, writer, observer, owner, frames


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at_end", [False, True])
async def test_actual_browser_personal_story_pause_checkpoint_new_lease_and_completion(pause_at_end):
    api, stand, boundary, writer, observer, owner, frames = await start_story()
    browser = await Browser().start()
    app, sid = stand.application, stand.active_session_id
    state = app.canonical_runtime.sessions[sid]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
            base_url="http://stand", headers={"x-stand-token": "private-pcm-unit-test"}) as client:
        async def endpoint(path, payload):
            response = await client.post(path, json=payload)
            assert response.status_code == 200
            return response.json()
        async def complete_current():
            current = dict(app._active_audio[sid])
            return await endpoint("/stand/audio/completed", {**current, "session_id": sid, "response_id": current["content_id"]})
        try:
            played = await browser.call(op="frames", frames=frames)
            assert played["active"] == len(frames) and not played["publicCached"]
            for post in played["posts"]:
                assert (await endpoint(post["path"], post["payload"]))["audio_status"] == "playing"
            old = dict(frames[0]["playback"])
            assert (await event(stand, "ButtonPressed", 2, {"button_id": 6}))["reason"] == "story_pause_requested"
            expected_offset = 5000 if pause_at_end else 250
            if pause_at_end:
                ended = await browser.call(op="end", indices=list(range(len(frames))), time=15.)
                for post in ended["posts"]:
                    await endpoint(post["path"], post["payload"])
            async with stand._transport_lock:
                await stand._flush_device_commands(timeout=1.)
            await drain(stand)
            assert any(item["type"] == "audio_pause" for item in writer.messages)
            paused = await browser.call(op="pause", node="story_pause_announcement", time=15.01 if pause_at_end else 10.25)
            assert paused["active"] == 0
            if not pause_at_end:
                assert all(paused["stopped"])
            checkpoints = [post["payload"] for post in paused["posts"] if post["path"] == "/stand/audio/checkpoint"]
            assert len(checkpoints) == 1, "even a just-ended owned stream retains one position receipt for an admitted pause"
            checkpoint = checkpoints[0]
            assert checkpoint["offset_ms"] == expected_offset
            assert stand.panel_snapshot()["canonical_state"]["personal_story_checkpoint_pending"]
            await complete_current()
            assert state.mode == "story_paused"
            await event(stand, "ButtonReleased", 3, {"button_id": 6})
            result = await event(stand, "ButtonPressed", 4, {"button_id": 6})
            assert result["reason"] == "personal_story_checkpoint_pending" and state.mode == "story_paused"
            for patch in ({"token": old["token"] + 1}, {"playback_id": "foreign"},
                          {"playback_lease": "foreign"}, {"generation": old["generation"] + 1},
                          {"offset_ms": 5001}, {"offset_ms": -1}, {"offset_ms": 1.5}):
                assert (await endpoint("/stand/audio/checkpoint", {**checkpoint, **patch}))["reason"] == "stale_story_checkpoint"
                assert owner.checkpoint_ms is None
            assert (await endpoint("/stand/audio/checkpoint", checkpoint))["reason"] == "story_checkpoint_updated"
            assert owner.checkpoint_ms == expected_offset and state.story_checkpoint_confirmed
            assert not stand.panel_snapshot()["canonical_state"]["personal_story_checkpoint_pending"]
            assert (await endpoint("/stand/audio/checkpoint", checkpoint))["reason"] == "stale_story_checkpoint"
            assert (await endpoint("/stand/audio/completed", old))["reason"] == "stale_private_pcm_callback"
            await event(stand, "ButtonPressed", 5, {"button_id": 6})
            assert state.mode == "story_resume_announcement"
            resumed_result = await complete_current()
            if pause_at_end:
                assert resumed_result["reason"] == "personal_story_finished" and state.mode == "root_menu"
                assert owner.closed and owner.audio is None
                leases = [item for item in resumed_result["outputs"] if item.get("type") == "play_personal_story"]
                assert leases and leases[0]["playback_id"] != old["playback_id"]
                assert leases[0]["playback_lease"] != old["playback_lease"]
                assert not (await browser.call(op="cleanup"))["personalRetained"]
                return
            assert resumed_result["reason"] == "personal_story_resumed"
            await drain(stand)
            resumed = [item for item in writer.messages if item.get("type") == "audio_pcm_output"
                and item["playback"]["response_id"] == STORY_ID and item["playback"]["playback_id"] != old["playback_id"]]
            assert resumed and state.mode == "personal_story_playing"
            assert resumed[0]["playback"]["playback_lease"] != old["playback_lease"]
            assert all(item["story_offset_ms"] == 250 and item["story_duration_ms"] == 5000 for item in resumed)
            assert owner.offset_ms == 250 and owner.checkpoint_ms is None
            again = await browser.call(op="frames", node="personal_story_playing", frames=resumed)
            assert again["active"] == len(resumed) and not again["publicCached"]
            for post in again["posts"]:
                assert (await endpoint(post["path"], post["payload"]))["audio_status"] == "playing"
            late = await browser.call(op="end", indices=list(range(len(frames))))
            assert not late["posts"], "old scheduled callbacks cannot complete resumed PCM"
            done = await browser.call(op="end", indices=list(range(len(frames), len(frames) + len(resumed))))
            for post in done["posts"]:
                await endpoint(post["path"], post["payload"])
            assert state.mode == "root_menu" and sid not in app._personal_stories
            assert owner.closed and owner.audio is None and owner.offset_ms == 0 and owner.checkpoint_ms is None
            assert not any(item.get("type") == "audio_pcm_output" for item in observer.messages)
        finally:
            await browser.close()
            await cleanup(stand, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["hangup", "reset", "card", "fault", "replacement", "story_owner"])
async def test_personal_checkpoint_cannot_survive_terminal_owner_loss(revoke):
    api, stand, boundary, writer, observer, owner, frames = await start_story()
    app, sid = stand.application, stand.active_session_id
    old = frames[0]["playback"]
    try:
        await event(stand, "ButtonPressed", 2, {"button_id": 6})
        if revoke == "replacement":
            from test_browser_private_pcm import Socket
            stand.hub.disconnect(writer)
            assert await stand.hub.connect(Socket())
        elif revoke == "story_owner":
            app._personal_stories[sid] = StoryOwner(owner.epoch, "replacement", owner.identity, owner.band,
                                                   owner.deadline, audio=owner.audio)
        elif revoke == "card":
            boundary.client.events.append({"kind": "clear", "reason_code": "scan_started"})
            await boundary.step()
        else:
            await event(stand, {"hangup": "HandsetPlaced", "reset": "SessionReset", "fault": "HardwareDisconnected"}[revoke], 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://stand",
                headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            reply = await client.post("/stand/audio/checkpoint", json={**old, "story_id": STORY_ID, "offset_ms": 250})
            assert reply.json()["reason"] == "stale_story_checkpoint"
        assert owner.checkpoint_ms is None
    finally:
        await cleanup(stand, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", ["story_owner", "socket"])
async def test_checkpoint_rechecks_exact_paused_owner_after_identity_await(replace):
    api, stand, boundary, writer, observer, owner, frames = await start_story()
    app, sid = stand.application, stand.active_session_id
    original = app.expire_identity_leases
    blocked, release = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        await event(stand, "ButtonPressed", 2, {"button_id": 6})
        state = app.canonical_runtime.sessions[sid]
        async def delayed_expiry():
            blocked.set()
            await release.wait()
            return []
        app.expire_identity_leases = delayed_expiry
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://stand",
                headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            pending = asyncio.create_task(client.post("/stand/audio/checkpoint", json={
                **frames[0]["playback"], "story_id": STORY_ID, "offset_ms": 250}))
            await asyncio.wait_for(blocked.wait(), 2)
            if replace == "socket":
                from test_browser_private_pcm import Socket
                stand.hub.disconnect(writer)
                assert await stand.hub.connect(Socket())
            else:
                replacement = StoryOwner(owner.epoch, "new-story-owner", owner.identity, owner.band,
                    owner.deadline, audio=owner.audio, checkpoint_ms=250)
                app._personal_stories[sid] = replacement
                app._paused_story_playback[sid] = {**app._paused_story_playback[sid], "playback_id": "new-paused-playback"}
                owner.close()
            release.set()
            reply = await pending
            assert reply.json()["reason"] == "stale_story_checkpoint"
            assert state.dialogue_memory.story_checkpoint["offset_ms"] == 0
            assert not state.story_checkpoint_confirmed
            if replace == "socket":
                assert owner.checkpoint_ms is None
            else:
                assert replacement.checkpoint_ms == 250, "old receipt cannot mutate a replacement owner"
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        app.expire_identity_leases = original
        await cleanup(stand, boundary)


@pytest.mark.asyncio
async def test_concurrent_duplicate_cannot_erase_committed_checkpoint_or_break_resume():
    api, stand, boundary, writer, observer, owner, frames = await start_story()
    app, sid = stand.application, stand.active_session_id
    blocked, release = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        await event(stand, "ButtonPressed", 2, {"button_id": 6})
        async def hook(_sid, result):
            if result.get("reason") == "story_checkpoint_updated":
                blocked.set()
                await release.wait()
        app.state_update_hook = hook
        payload = {**frames[0]["playback"], "story_id": STORY_ID, "offset_ms": 250}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://stand",
                headers={"x-stand-token": "private-pcm-unit-test"}) as client:
            pending = asyncio.create_task(client.post("/stand/audio/checkpoint", json=payload))
            await asyncio.wait_for(blocked.wait(), 2)
            duplicate = await client.post("/stand/audio/checkpoint", json=payload)
            assert duplicate.json()["reason"] in {"stale_story_checkpoint", "idempotent_duplicate"}
            assert owner.checkpoint_ms == 250, "a duplicate must never erase the committed exact position"
            release.set()
            assert (await pending).json()["reason"] == "story_checkpoint_updated"
        app.state_update_hook = None
        state = app.canonical_runtime.sessions[sid]
        async def complete(seq):
            current = dict(app._active_audio[sid])
            return await event(stand, "AudioCompleted", seq, {**current, "response_id": current["content_id"]})
        await complete(3)
        assert state.mode == "story_paused" and state.story_checkpoint_confirmed
        await event(stand, "ButtonReleased", 4, {"button_id": 6})
        await event(stand, "ButtonPressed", 5, {"button_id": 6})
        assert (await complete(6))["reason"] == "personal_story_resumed"
        await drain(stand)
        resumed = [item for item in writer.messages if item.get("type") == "audio_pcm_output"
            and item["playback"]["response_id"] == STORY_ID and item["playback"]["playback_id"] != payload["playback_id"]]
        assert resumed and resumed[0]["story_offset_ms"] == 250
        assert resumed[0]["playback"]["playback_lease"] != payload["playback_lease"]
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        app.state_update_hook = None
        await cleanup(stand, boundary)
