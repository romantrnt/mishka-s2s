import asyncio
import io
import struct
import threading
import wave

import pytest

from app.foundation import hardware
from app.foundation.hardware import PosixSerialTransport, SerialHardwareAdapter, decode_frame, encode_frame
from app.foundation.stand import AudioInputError, HardwareIntegrationStand, decode_wav, resample


class FakeTransport:
    def __init__(self, *, block=False, fail=False):
        self.opened = False
        self.closed = False
        self.writes = []
        self.block = block
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()

    def open(self):
        self.opened = True

    def write(self, raw):
        self.started.set()
        if self.block and not self.release.wait(timeout=2):
            raise TimeoutError("fake write blocked")
        if self.fail:
            raise OSError("fake UART failure")
        self.writes.append(raw)

    def read_available(self):
        return b""

    def close(self):
        self.closed = True


def wav_bytes(channels=1, width=2, rate=16000, frames=4):
    payload = []
    for index in range(frames * channels):
        payload.append((index - 1) * 1000 if width == 2 else (index - 1) * 100000)
    raw = struct.pack("<" + ("h" if width == 2 else "i") * len(payload), *payload)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(raw)
    return output.getvalue()


def test_uart_writer_is_bounded_ordered_and_event_loop_responsive():
    async def run():
        transport = FakeTransport(block=True)
        adapter = SerialHardwareAdapter(transport, queue_limit=1, write_timeout_s=0.05)
        adapter.connect()
        adapter.send({"type": "one", "payload": {}})
        assert transport.started.wait(timeout=1)
        adapter.send({"type": "two", "payload": {}})
        with pytest.raises(BufferError, match="queue full"):
            adapter.send({"type": "three", "payload": {}})
        heartbeat = 0
        async def tick():
            nonlocal heartbeat
            for _ in range(5):
                heartbeat += 1
                await asyncio.sleep(0)
        await asyncio.wait_for(tick(), timeout=0.2)
        assert heartbeat == 5
        transport.release.set()
        await adapter.flush_async(timeout=1)
        assert [decode_frame(raw).sequence for raw in transport.writes] == [1, 2]
        assert adapter.snapshot()["dropped"] == 1
        adapter.close()
    asyncio.run(run())


def test_uart_writer_timeout_failure_reconnect_and_repeated_shutdown():
    async def run():
        transport = FakeTransport(block=True)
        adapter = SerialHardwareAdapter(transport, write_timeout_s=0.02)
        adapter.connect()
        adapter.send({"type": "blocked", "payload": {}})
        assert transport.started.wait(timeout=1)
        with pytest.raises(TimeoutError):
            await adapter.flush_async(timeout=0.01)
        transport.release.set()
        await adapter.flush_async(timeout=1)
        adapter.close()
        adapter.close()
        assert transport.closed

        failed_transport = FakeTransport(fail=True)
        failed = SerialHardwareAdapter(failed_transport)
        failed.connect()
        failed.send({"type": "fails", "payload": {}})
        with pytest.raises(ConnectionError):
            await failed.flush_async(timeout=1)
        assert failed.snapshot()["failed"] == 1
        failed.close()
    asyncio.run(run())


def test_posix_uart_retries_partial_os_write(monkeypatch):
    transport = PosixSerialTransport("/dev/does-not-open", baud=115200)
    transport.fd = 77
    calls = []
    writes = iter((2, 3))

    def partial_write(fd, view):
        assert fd == 77
        calls.append(bytes(view))
        return next(writes)

    monkeypatch.setattr(hardware.os, "write", partial_write)
    transport.write(b"hello")
    assert calls == [b"hello", b"llo"]


def test_uart_frame_crc_length_sequence_and_partial_boundary_remain_intact():
    first = encode_frame("button", 1, {"button_id": 1})
    second = encode_frame("heartbeat", 2, {})
    assert decode_frame(first).sequence == 1
    assert decode_frame(second).sequence == 2
    assert len(first) <= 4096


def test_wav_decode_is_bounded_vectorized_and_explicit_for_stereo():
    samples, rate = decode_wav(wav_bytes(channels=2, width=2, rate=48000, frames=4))
    assert rate == 48000 and len(samples) == 4
    assert all(-1.0 <= value <= 1.0 for value in samples)
    assert not list(__import__("pathlib").Path(".").glob("*.raw"))
    with pytest.raises(AudioInputError, match="sample width"):
        decode_wav(wav_bytes(width=1))
    with pytest.raises(AudioInputError, match="sample rate"):
        decode_wav(wav_bytes(rate=8000))
    with pytest.raises(AudioInputError, match="truncated"):
        decode_wav(wav_bytes()[:-2])


def test_resample_preserves_bounds_and_rejects_invalid_input():
    output = resample((0.0, 0.5, 1.0), 22050, 16000)
    assert len(output) == round(3 * 16000 / 22050)
    assert min(output) >= 0.0 and max(output) <= 1.0
    with pytest.raises(AudioInputError, match="sample rate"):
        resample((0.0,), 8000, 16000)
    with pytest.raises(AudioInputError, match="finite"):
        resample((float("nan"),), 16000, 16000)


def test_audio_codec_offload_can_be_cancelled_without_orphaning_executor():
    async def run():
        stand = HardwareIntegrationStand()
        started = asyncio.Event()
        release = asyncio.Event()
        original = stand._offload_audio

        async def slow(fn, *args):
            started.set()
            await release.wait()
            return await original(fn, *args)

        stand._offload_audio = slow
        task = asyncio.create_task(stand.inject_pcm((0.1,) * 48000, 48000, "codec-cancel", flush_silence=False))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        stand.close()
    asyncio.run(run())
