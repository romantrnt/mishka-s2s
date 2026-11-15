import asyncio
import base64
import struct

import pytest
from starlette.websockets import WebSocketDisconnect
from app.foundation.stand import create_stand_app
from app.foundation.config import Settings


class Socket:
    headers = {"x-stand-token": "synthetic-audit-token"}
    cookies = {}
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
    async def accept(self): pass
    async def send_json(self, payload): self.sent.append(payload)
    async def close(self, **kwargs): pass
    async def receive_json(self):
        message = await self.messages.get()
        if message is None:
            raise WebSocketDisconnect()
        return message


@pytest.mark.parametrize("kind", ["audio_pcm", "flush_audio"])
def test_audio_overload_is_bounded_and_controls_remain_responsive(kind):
    async def scenario():
        api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
                                       stand_access_token="synthetic-audit-token"))
        stand = api.state.stand
        api.state.lifecycle.admits_users = lambda: True
        endpoint = next(route.endpoint for route in api.routes if getattr(route, "path", None) == "/stand/ws")
        sid = stand.active_session_id
        stand.application.start_session(sid)
        lock = stand._audio_ingress_locks.setdefault(sid, asyncio.Lock())
        await lock.acquire()
        original = stand.inject_pcm
        admitted = []
        async def record(*args, **kwargs):
            admitted.append(kind)
            return await original(*args, **kwargs)
        stand.inject_pcm = record
        controls = []
        original_inject = stand.inject
        async def control(*args, **kwargs):
            if args[0] == "audio_pcm":
                return await original_inject(*args, **kwargs)
            controls.append(args[0])
            return {}
        stand.inject = control
        socket = Socket()
        task = asyncio.create_task(endpoint(socket))
        message = {"type": kind, "session_id": sid}
        if kind == "audio_pcm":
            message.update(data_b64=base64.b64encode(struct.pack("<512f", *([0.] * 512))).decode(), sample_rate=16000)
        try:
            for _ in range(256):
                socket.messages.put_nowait(dict(message))
            socket.messages.put_nowait({"type": "handset", "state": "down", "session_id": sid})
            socket.messages.put_nowait({"type": "ping"})
            for _ in range(100):
                await asyncio.sleep(.002)
                if any(item.get("type") == "pong" for item in socket.sent):
                    break
            assert controls == ["handset"]
            assert any(item.get("type") == "pong" for item in socket.sent)
            assert len(admitted) <= 16
            errors = [item for item in socket.sent if item.get("error", {}).get("code") == "audio_backpressure"]
            assert len(errors) >= 240
        finally:
            socket.messages.put_nowait(None)
            await asyncio.wait_for(task, 3)
            lock.release()
            await stand.aclose()
    asyncio.run(scenario())
