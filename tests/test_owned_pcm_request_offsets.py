"""Per-request resume offsets are applied inside the native playback lock."""
import asyncio
import sys
from importlib.machinery import ModuleSpec
from types import SimpleNamespace
import pytest
from app.foundation.s2s import AudioChunk, SoundDevicePlaybackController


def native(monkeypatch):
    writes = []
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def write(self, values): writes.append(len(values))
        def stop(self): self.drained = True
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    controller = SoundDevicePlaybackController()
    controller.active_token = 1
    return controller, writes


@pytest.mark.asyncio
async def test_owned_offset_is_immutable_while_waiting_native_lock(monkeypatch):
    controller, writes = native(monkeypatch)
    await controller._play_lock.acquire()
    task = asyncio.create_task(controller.play_owned_pcm(
        AudioChunk((.1,) * 17, 16000, 1), lambda: True, start_position_samples=3200))
    await asyncio.sleep(0)
    controller.start_position_samples = 999999
    controller._play_lock.release()
    await task
    assert writes == [17]
    assert controller.position_samples == 3217
    assert controller.start_position_samples == 0
    assert controller.last_playback_completed


@pytest.mark.asyncio
async def test_legacy_asset_offset_remains_compatible(monkeypatch):
    controller, writes = native(monkeypatch)
    controller.start_position_samples = 3200
    await controller.play_owned_pcm(AudioChunk((.1,) * 17, 16000, 1), lambda: True)
    assert writes == [17]
    assert controller.position_samples == 3217


@pytest.mark.asyncio
async def test_revoked_waiter_cannot_consume_new_owner_offset(monkeypatch):
    controller, writes = native(monkeypatch)
    owner = [True]
    await controller._play_lock.acquire()
    old = asyncio.create_task(controller.play_owned_pcm(
        AudioChunk((.1,) * 100, 16000, 1), lambda: owner[0], start_position_samples=8000))
    await asyncio.sleep(0)
    owner[0] = False
    controller._play_lock.release()
    await old
    assert writes == []
    await controller.play_owned_pcm(AudioChunk((.1,) * 17, 16000, 1), lambda: True,
                                    start_position_samples=0)
    assert writes == [17]
    assert controller.position_samples == 17


@pytest.mark.asyncio
async def test_stop_mid_stream_retains_only_played_samples_after_request_offset(monkeypatch):
    controller, writes = native(monkeypatch)
    blocked, release = asyncio.Event(), asyncio.Event()
    old_run_io = controller._run_io
    calls = 0
    async def barrier(operation):
        nonlocal calls
        if operation.__name__ == 'guarded_write':
            calls += 1
            if calls == 2:
                blocked.set()
                await release.wait()
        return await old_run_io(operation)
    controller._run_io = barrier
    task = asyncio.create_task(controller.play_owned_pcm(
        AudioChunk((.1,) * 6000, 16000, 1), lambda: True, start_position_samples=3200))
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        # Block sizing is not the contract: retain precisely the first native
        # write, excluding the second dispatch that stop must revoke.
        assert len(writes) == 1 and 0 < writes[0] < 6000
        first_write = writes[0]
        assert controller.position_samples == 3200 + first_write
        await controller.stop()
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert writes == [first_write]
        assert result.written_samples == first_write
        assert result.position_samples == controller.position_samples == 3200 + first_write
        assert not result.completed and not controller.last_playback_completed
    finally:
        release.set()
        if not task.done(): task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('offset', [-1, 1.5, True, '2', float('nan'), float('inf')])
async def test_invalid_offset_rejected_before_native_io(monkeypatch, offset):
    controller, writes = native(monkeypatch)
    with pytest.raises(ValueError, match='invalid playback offset'):
        await controller.play_owned_pcm(AudioChunk((.1,), 16000, 1), lambda: True,
                                       start_position_samples=offset)
    assert writes == []


@pytest.mark.asyncio
async def test_queued_revoked_result_keeps_own_offset_not_previous_position(monkeypatch):
    controller, writes = native(monkeypatch)
    controller.position_samples = 999
    owner = [True]
    await controller._play_lock.acquire()
    task = asyncio.create_task(controller.play_owned_pcm(
        AudioChunk((.1,) * 17, 16000, 1), lambda: owner[0], start_position_samples=3200))
    await asyncio.sleep(0)
    owner[0] = False
    controller._play_lock.release()
    result = await task
    assert result.position_samples == 3200
    assert result.written_samples == 0
    assert not result.completed and not result.stream_started
    assert writes == []


@pytest.mark.asyncio
async def test_returned_result_is_immutable_after_new_owner_finishes(monkeypatch):
    from dataclasses import FrozenInstanceError
    controller, writes = native(monkeypatch)
    old = await controller.play_owned_pcm(AudioChunk((.1,) * 17, 16000, 1), lambda: True,
                                          start_position_samples=3200)
    new = await controller.play_owned_pcm(AudioChunk((.1,) * 31, 16000, 1), lambda: True,
                                          start_position_samples=0)
    assert old.position_samples == 3217 and old.completed and old.stream_started
    assert new.position_samples == controller.position_samples == 31
    with pytest.raises(FrozenInstanceError): old.written_samples = 0


@pytest.mark.asyncio
async def test_native_write_failure_never_returns_completed_result(monkeypatch):
    writes = []
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def write(self, values):
            writes.append(len(values))
            raise OSError('synthetic failure')
        def stop(self): self.drained = True
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    controller = SoundDevicePlaybackController()
    controller.active_token = 1
    with pytest.raises(RuntimeError, match='local playback unavailable'):
        await controller.play_owned_pcm(AudioChunk((.1,), 16000, 1), lambda: True,
                                        start_position_samples=3200)
    assert writes == [1]  # Reach native failure, not merely the device preflight.
    assert not controller.last_playback_completed


@pytest.mark.asyncio
async def test_cancellation_waiting_for_lock_remains_cancellation(monkeypatch):
    controller, writes = native(monkeypatch)
    await controller._play_lock.acquire()
    task = asyncio.create_task(controller.play_owned_pcm(
        AudioChunk((.1,), 16000, 1), lambda: True, start_position_samples=3200))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    controller._play_lock.release()
    assert writes == []
