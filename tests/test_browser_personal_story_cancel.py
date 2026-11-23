"""The real browser FIFO can cancel a blocked ASGI/PTY personal-story request."""
import asyncio

import httpx
import pytest

from app.foundation.personal_story import STORY_ID
from test_browser_personal_story_pause import Browser
from test_browser_private_pcm import setup_browser, cleanup, event, drain
from test_personal_story_service import engine


class QueueBrowser(Browser):
    async def call(self, **kwargs):
        if not hasattr(self, "_ipc_lock"):
            self._ipc_lock = asyncio.Lock()
        async with self._ipc_lock:
            return await super().call(**kwargs)

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "node", "tests/console_queue_regression.js", "app/foundation/console/console.js", "--bridge",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        return self


@pytest.mark.asyncio
async def test_published_generating_state_allows_browser_cancel_before_provider_finishes():
    api, stand, boundary, _render, _clock, writer, _observer = await setup_browser()
    app, sid = stand.application, stand.active_session_id
    app.bounded_s2s = engine()
    app._owned_menu_voice_capable = lambda: False
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = app.bounded_s2s.response_validator.validate_trusted
    async def blocked(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()  # Native work may return after cancellation.
        return await original(*args)
    browser, pending = await QueueBrowser().start(), None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
            base_url="http://stand", headers={"x-stand-token": "private-pcm-unit-test"}) as client:
        async def action(button, press="press"):
            result = await client.post("/stand/simulator/action", json={
                "action":"button", "button_id":button, "press":press, "epoch":stand.remote_devices.epoch})
            assert result.status_code == 200, result.text
            return result.json()
        async def deliver(request):
            reply = await client.post(request["path"], json=request["payload"])
            assert reply.status_code == 200, reply.text
            await browser.call(op="resolve", index=request["index"], value=reply.json())
            return reply.json()
        try:
            await event(stand,"HandsetLifted",1)
            app._discard_private_welcome(sid);app._invalidate_audio_lease(sid)
            state = app.canonical_runtime.sessions[sid]
            state.mode, state.active_audio_id, state.playback_lease = "root_menu", None, None
            await action(3);await action(3,"release")
            assert state.mode == "personal_story_theme"
            entered.clear()
            app.bounded_s2s.response_validator.validate_trusted = blocked
            prior_tts = list(app.bounded_s2s.tts.calls)
            await browser.call(op="snapshot", state=stand.panel_snapshot())
            first = await browser.call(op="post", path="/stand/simulator/action", payload={
                "action":"button", "button_id":1, "press":"press", "epoch":stand.remote_devices.epoch})
            pending = asyncio.create_task(deliver(first["requests"][0]))
            await asyncio.wait_for(entered.wait(),2)
            render_job = next(iter(app._personal_story_jobs))
            await drain(stand)
            published = [item["state"] for item in writer.messages if item.get("type")=="stand_state"
                         and item["state"]["session"]["node"]=="personal_story_generating"]
            assert published, "generating must reach the browser before the provider finishes"
            await browser.call(op="snapshot", state=published[-1])
            payload = {"action":"button", "button_id":1, "press":"release", "epoch":stand.remote_devices.epoch}
            await browser.call(op="post",path="/stand/simulator/action",payload=payload)
            payload = {**payload,"button_id":7,"press":"press"}
            outgoing = await browser.call(op="post",path="/stand/simulator/action",payload=payload)
            await browser.call(op="post",path="/stand/simulator/action",payload={**payload,"press":"release"})
            assert len(outgoing["requests"])==2 and outgoing["requests"][0]["aborted"], "7 bypasses the blocked authored-story POST"
            result = await asyncio.wait_for(deliver(outgoing["requests"][1]),2)
            assert result["result"]["reason"]=="activity_back"
            await asyncio.wait_for(cancelled.wait(),2)
            assert state.mode=="root_menu" and not app._personal_stories and not render_job.done()
            # Release callbacks survive the scoped cancellation and stay ordered.
            for index in (2,3):
                outgoing = await browser.call(op="inspect")
                assert outgoing["requests"][index]["payload"]["press"]=="release"
                await deliver(outgoing["requests"][index])
            assert not state.accepted_buttons_down, "the original choice and cancel keys are both released"
            release.set();await asyncio.wait_for(pending,2)
            await asyncio.wait_for(asyncio.gather(render_job, return_exceptions=True),2)
            await drain(stand)
            assert state.mode=="root_menu" and not app._personal_stories
            assert app.bounded_s2s.tts.calls==prior_tts
            assert not app.bounded_s2s.llm.calls
            assert not any(item.get("type")=="audio_pcm_output" and item.get("playback",{}).get("response_id")==STORY_ID
                           for item in writer.messages)
        finally:
            release.set()
            if pending: await asyncio.gather(pending,return_exceptions=True)
            await browser.close()
            await cleanup(stand,boundary)
