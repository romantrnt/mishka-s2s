"""The actual websocket finally must run inside Starlette's cancelled scope."""
import asyncio

import anyio
import pytest

from test_browser_capture_lifecycle import BrowserCapture, Socket, until


class CancelledSocket(Socket):
    async def receive_json(self):
        message = await super().receive_json()
        if message == {"cancel_scope": True}:
            self.scope.cancel()
            await anyio.lowlevel.checkpoint()
        return message


async def connect_cancelled(h):
    socket = CancelledSocket()

    async def run():
        with anyio.CancelScope() as scope:
            socket.scope = scope
            await h.endpoint(socket)

    task = asyncio.create_task(run())
    h.tasks.append((socket, task))
    await until(lambda: bool(socket.sent))
    return socket, task


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_cancelled_scope_releases_only_its_client_and_capture(owner):
    h = await BrowserCapture().start()
    try:
        if owner:
            socket, task = await connect_cancelled(h)
            current = socket
        else:
            current, _ = await h.connect()
            socket, task = await connect_cancelled(h)
        await h.feed(current, .25)
        ingress = h.app.s2s_audio.ingress[h.sid]
        binding = h.stand.hub.owner_binding()
        session = h.app.sessions.get(h.sid)
        epoch = h.app._session_epochs.get(h.sid, 0)
        socket.messages.put_nowait({"cancel_scope": True})
        await asyncio.wait_for(task, 3)
        assert socket not in h.stand.hub.clients
        assert h.app.sessions.get(h.sid) is session and session.handset_up
        assert h.app._session_epochs.get(h.sid, 0) == epoch
        if owner:
            assert not h.stand.hub.clients and h.stand.hub.owner_binding() is None
            assert not h.stand.hub.owner_binding_current(binding)
            assert h.sid not in h.app.s2s_audio.ingress
            assert h.sid not in h.stand._browser_capture_owners
            assert not ingress.active and not ingress.buffer._samples
            replacement, _ = await h.connect()
            await h.feed(replacement, .5)
            assert .25 not in h.app.s2s_audio.ingress[h.sid].buffer._samples
        else:
            assert h.stand.hub.owner_binding() == binding
            assert h.app.s2s_audio.ingress[h.sid] is ingress and ingress.active
            assert len(ingress.buffer._samples) == 8192
    finally:
        await h.close()
