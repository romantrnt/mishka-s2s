"""Actual browser ingress/finally with RAM-only sockets, PCM and providers."""
import asyncio
import base64
import struct

import pytest
from starlette.websockets import WebSocketDisconnect

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import STTResult
from app.foundation.stand import create_stand_app


class Socket:
    headers = {"x-stand-token": "capture-lifecycle-test"}
    cookies = {}

    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []

    async def accept(self): pass
    async def close(self, **kwargs): pass
    async def send_json(self, payload): self.sent.append(payload)

    async def receive_json(self):
        message = await self.messages.get()
        if message is None:
            raise WebSocketDisconnect()
        return message


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 3)


class BrowserCapture:
    async def start(self):
        self.api = create_stand_app(Settings(profile="mock", stand_access_token="capture-lifecycle-test"))
        self.stand = self.api.state.stand
        self.api.state.lifecycle.admits_users = lambda: True
        self.app = self.stand.application
        self.sid = self.stand.active_session_id
        self.endpoint = next(route.endpoint for route in self.api.routes if route.path == "/stand/ws")
        self.tasks = []
        self.completed_frames = 0
        self.recognized = []
        await self.app.receive_async(Event("HandsetLifted", self.sid, 1, 1., "capture-test", {}, "lift"))
        self.app._cancel_welcome_timer(self.sid)
        self.app.sessions.get(self.sid).current_node = "bear_dialogue"
        self.app.canonical_runtime.sessions[self.sid].mode = "bear_dialogue"
        self.app._active_audio.clear()
        harness = self

        class Recognizer:
            async def transcribe(self, audio, cancelled):
                harness.recognized.append(audio.samples)
                return STTResult("", .99, 0., False)

        self.app.s2s_audio.recognizer = Recognizer()
        original = self.stand.inject_pcm

        async def observe(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.completed_frames += 1
            return result

        self.stand.inject_pcm = observe
        return self

    async def connect(self):
        socket = Socket()
        task = asyncio.create_task(self.endpoint(socket))
        self.tasks.append((socket, task))
        await until(lambda: bool(socket.sent))
        return socket, task

    async def feed(self, socket, value, count=4):
        completed = self.completed_frames + count
        frame = base64.b64encode(struct.pack("<2048f", *([value] * 2048))).decode()
        for _ in range(count):
            socket.messages.put_nowait({"type": "audio_pcm", "session_id": self.sid,
                                      "sample_rate": 16000, "data_b64": frame})
        await until(lambda: self.completed_frames >= completed)
        await asyncio.sleep(0)  # Let the endpoint discard finished collecting tasks.

    async def close(self):
        for socket, task in self.tasks:
            if not task.done():
                socket.messages.put_nowait(None)
        await asyncio.wait_for(asyncio.gather(*(task for _, task in self.tasks)), 3)
        await self.stand.aclose()


@pytest.mark.asyncio
async def test_owner_disconnect_revokes_finished_collecting_capture_and_new_turn_is_clean():
    h = await BrowserCapture().start()
    try:
        socket, task = await h.connect()
        await h.feed(socket, .25)
        ingress = h.app.s2s_audio.ingress[h.sid]
        assert ingress.active and len(ingress.buffer._samples) == 8192
        assert not h.app.s2s_audio.pending_tasks
        socket.messages.put_nowait(None)
        await asyncio.wait_for(task, 3)
        assert h.sid not in h.app.s2s_audio.ingress
        assert not ingress.active and not ingress.buffer._samples
        assert h.app.sessions.get(h.sid).handset_up  # Disconnect is not a handset event.
        replacement, _ = await h.connect()
        await h.feed(replacement, .5)
        await h.feed(replacement, 0., 2)
        await until(lambda: bool(h.recognized))
        assert h.recognized[0][0] == .5 and .25 not in h.recognized[0]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_observer_disconnect_does_not_revoke_the_write_owner_capture():
    h = await BrowserCapture().start()
    try:
        owner, _ = await h.connect()
        await h.feed(owner, .25)
        ingress = h.app.s2s_audio.ingress[h.sid]
        observer, task = await h.connect()
        assert not h.stand.hub.is_owner(observer)
        observer.messages.put_nowait(None)
        await asyncio.wait_for(task, 3)
        assert h.app.s2s_audio.ingress[h.sid] is ingress and ingress.active
        assert len(ingress.buffer._samples) == 8192
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_replaced_owner_and_its_late_finally_cannot_join_or_close_new_capture():
    h = await BrowserCapture().start()
    try:
        old, old_task = await h.connect()
        await h.feed(old, .25)
        old_ingress = h.app.s2s_audio.ingress[h.sid]
        h.stand.hub.disconnect(old)  # Write-lease revocation before old handler finally.
        new, _ = await h.connect()
        await h.feed(new, .5)
        new_ingress = h.app.s2s_audio.ingress[h.sid]
        assert new_ingress is not old_ingress
        assert new_ingress.active and len(new_ingress.buffer._samples) == 8192
        assert .25 not in new_ingress.buffer._samples
        old.messages.put_nowait(None)
        await asyncio.wait_for(old_task, 3)
        assert h.app.s2s_audio.ingress[h.sid] is new_ingress and new_ingress.active
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_old_pending_recognition_cleanup_cannot_close_replacement_capture():
    h = await BrowserCapture().start()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Recognizer:
        async def transcribe(self, audio, cancelled_check):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return STTResult("Поздняя старая реплика", .99, 0., False)

    try:
        h.app.audio_cleanup_timeout_seconds = 1.
        h.app.s2s_audio.recognizer = Recognizer()
        old, task = await h.connect()
        await h.feed(old, .25)
        await h.feed(old, 0., 2)
        await asyncio.wait_for(entered.wait(), 3)
        old.messages.put_nowait(None)
        await asyncio.wait_for(cancelled.wait(), 3)
        new, _ = await h.connect()
        await h.feed(new, .5)
        ingress = h.app.s2s_audio.ingress[h.sid]
        release.set()
        await asyncio.wait_for(task, 3)
        await asyncio.sleep(0)
        assert h.app.s2s_audio.ingress[h.sid] is ingress and ingress.active
        assert ingress.buffer._samples == [.5] * 8192
        assert h.app.canonical_runtime.sessions[h.sid].mode == "bear_dialogue"
    finally:
        release.set()
        await h.close()


@pytest.mark.asyncio
async def test_same_owner_barge_in_cancels_old_recognition_but_preserves_new_pcm():
    h = await BrowserCapture().start()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Recognizer:
        async def transcribe(self, audio, cancelled_check):
            if not entered.is_set():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            h.recognized.append(audio.samples)
            return STTResult("", .99, 0., False)

    try:
        h.app.s2s_audio.recognizer = Recognizer()
        socket, _ = await h.connect()
        await h.feed(socket, .25)
        await h.feed(socket, 0., 2)
        await asyncio.wait_for(entered.wait(), 3)
        await h.feed(socket, .5)
        await asyncio.wait_for(cancelled.wait(), 3)
        ingress = h.app.s2s_audio.ingress[h.sid]
        assert ingress.active and ingress.buffer._samples.count(.5) == 8192
        assert .25 not in ingress.buffer._samples
        await h.feed(socket, 0., 2)
        await until(lambda: bool(h.recognized))
        assert .5 in h.recognized[0] and .25 not in h.recognized[0]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_disconnect_during_barge_in_does_not_lock_out_replacement_pcm():
    h = await BrowserCapture().start()
    entered, cancelled, release, finished = (asyncio.Event() for _ in range(4))
    replacement_feed = None

    class ResistantRecognizer:
        async def transcribe(self, audio, cancelled_check):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            finished.set()
            return STTResult("Старая отменённая реплика", .99, 0., False)

    try:
        h.app.s2s_audio.recognizer = ResistantRecognizer()
        old, old_task = await h.connect()
        await h.feed(old, .25)
        await h.feed(old, 0., 2)
        await asyncio.wait_for(entered.wait(), 3)
        prior_event = dict(h.app.last_canonical_event)
        frame = base64.b64encode(struct.pack("<2048f", *([.5] * 2048))).decode()
        for _ in range(4):
            old.messages.put_nowait({"type": "audio_pcm", "session_id": h.sid,
                                     "sample_rate": 16000, "data_b64": frame})
        await asyncio.wait_for(cancelled.wait(), 3)
        assert h.stand._audio_ingress_locks[h.sid].locked()
        old.messages.put_nowait(None)
        await until(lambda: not h.stand.hub.is_owner(old))
        new, _ = await h.connect()
        assert h.stand.hub.is_owner(new)
        replacement_feed = asyncio.create_task(h.feed(new, .75))
        done, _ = await asyncio.wait({replacement_feed}, timeout=.3)
        assert done, "cancelled barge-in must release ingress before old STT returns"
        await replacement_feed
        ingress = h.app.s2s_audio.ingress[h.sid]
        assert ingress.active and ingress.buffer._samples == [.75] * 8192
        assert not release.is_set()
        release.set()
        await asyncio.wait_for(old_task, 3)
        await asyncio.wait_for(finished.wait(), 3)
        await asyncio.sleep(0)
        assert h.app.s2s_audio.ingress[h.sid] is ingress and ingress.active
        assert ingress.buffer._samples == [.75] * 8192
        assert h.app.last_canonical_event == prior_event, "late old recognition must not commit"
    finally:
        release.set()
        if replacement_feed is not None:
            await asyncio.gather(replacement_feed, return_exceptions=True)
        await h.close()
