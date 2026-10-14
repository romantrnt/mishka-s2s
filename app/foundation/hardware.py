from __future__ import annotations

import asyncio
import json
import os
import pty
import queue
import select
import termios
import threading
import time
import tty
import zlib
from dataclasses import dataclass

from .lighting import FakeLightingOutput, LightingCommand, LightingOutput, LightingState


@dataclass(frozen=True)
class HardwareFrame:
    protocol_version: str
    type: str
    sequence: int
    payload: dict
    checksum: str


def encode_frame(message_type: str, sequence: int, payload: dict) -> bytes:
    body = {"protocol_version": "1", "type": message_type, "sequence": sequence, "payload": payload}
    canonical = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    body["checksum"] = f"{zlib.crc32(canonical):08x}"
    return (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def decode_frame(raw: bytes) -> HardwareFrame:
    if len(raw) > 4096: raise ValueError("frame exceeds 4 KiB")
    try: body = json.loads(raw.decode().strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ValueError("invalid JSON frame") from exc
    if not isinstance(body, dict): raise ValueError("invalid frame schema")
    required = {"protocol_version", "type", "sequence", "payload", "checksum"}
    if (set(body) != required or body["protocol_version"] != "1"
            or not isinstance(body["type"], str) or not isinstance(body["sequence"], int)
            or isinstance(body["sequence"], bool) or not isinstance(body["payload"], dict)
            or not isinstance(body["checksum"], str)):
        raise ValueError("invalid frame schema")
    candidate = {k: body[k] for k in required - {"checksum"}}
    canonical = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    if body["checksum"] != f"{zlib.crc32(canonical):08x}": raise ValueError("checksum mismatch")
    return HardwareFrame(**body)


class SerialHardwareAdapter:
    """Bidirectional JSONL/CRC32 adapter with a bounded single-owner writer."""

    def __init__(self, transport=None, heartbeat_s=5.0, raw_observer=None,
                 queue_limit: int = 64, write_timeout_s: float = 1.0):
        self.transport, self.heartbeat_s, self.connected, self.sequence, self.last_heartbeat = transport, heartbeat_s, False, 0, 0.0
        self.capabilities: dict = {}; self.firmware_version: str | None = None; self.reconnects = 0
        self.raw_observer=raw_observer; self._read_buffer=b""; self._last_received_sequence=0
        self.queue_limit = max(1, int(queue_limit)); self.write_timeout_s = max(0.01, float(write_timeout_s))
        self._outbound: queue.Queue[bytes | None] = queue.Queue(maxsize=self.queue_limit)
        self._writer_stop = threading.Event(); self._writer_thread: threading.Thread | None = None
        self._writer_lock = threading.Lock(); self._queued = self._sent = self._dropped = self._failed = 0
        self._last_write_error: dict | None = None

    def _ensure_writer(self) -> None:
        with self._writer_lock:
            if self._writer_thread is not None and self._writer_thread.is_alive(): return
            self._writer_stop.clear()
            self._writer_thread = threading.Thread(target=self._writer_loop, name="mishka-uart-writer", daemon=True)
            self._writer_thread.start()

    def connect(self):
        if not self.transport: raise RuntimeError("UART transport is external/TBD")
        self.transport.open(); self.connected = True; self.reconnects += 1
        self._last_write_error = None
        self._read_buffer = b""; self._last_received_sequence = 0; self._ensure_writer()

    def _writer_loop(self) -> None:
        while not self._writer_stop.is_set():
            try: raw = self._outbound.get(timeout=0.1)
            except queue.Empty: continue
            try:
                if raw is None: return
                if not self.connected: self._failed += 1; continue
                self.transport.write(raw); self._sent += 1
                if self.raw_observer: self.raw_observer("host_to_device", raw)
            except Exception as exc:
                self.connected = False; self._failed += 1
                self._last_write_error = {"exception_type": type(exc).__name__, "detail": str(exc)[:200]}
            finally: self._outbound.task_done()

    def send(self, message: dict):
        if not self.connected: raise RuntimeError("hardware disconnected")
        next_sequence = self.sequence + 1
        raw=encode_frame(message["type"], next_sequence, message.get("payload", {}))
        try:
            self._ensure_writer(); self._outbound.put_nowait(raw); self.sequence = next_sequence; self._queued += 1
        except queue.Full as exc:
            self._dropped += 1; raise BufferError("UART outbound queue full") from exc
        try: asyncio.get_running_loop()
        except RuntimeError: self.flush(timeout=self.write_timeout_s)
        return {"status": "queued", "sequence": self.sequence}

    def flush(self, timeout: float | None = None) -> dict:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while self._outbound.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline: raise TimeoutError("UART writer flush timed out")
            time.sleep(0.001)
        if self._last_write_error is not None: raise ConnectionError("UART write failed")
        return self.snapshot()

    async def flush_async(self, timeout: float | None = None) -> dict:
        return await asyncio.to_thread(self.flush, timeout)

    def snapshot(self) -> dict:
        writer = self._writer_thread
        return {"queue_size": self._outbound.qsize(), "queue_limit": self.queue_limit,
                "queued": self._queued, "sent": self._sent, "dropped": self._dropped,
                "failed": self._failed, "writer_alive": bool(writer and writer.is_alive()),
                "last_error": self._last_write_error}

    def close(self) -> None:
        with self._writer_lock:
            thread = self._writer_thread
            if thread is None:
                self.connected = False; self.transport.close(); return
            try: self.flush(timeout=self.write_timeout_s)
            except (ConnectionError, TimeoutError): pass
            self._writer_stop.set()
            try: self._outbound.put_nowait(None)
            except queue.Full: pass
            thread.join(timeout=self.write_timeout_s); self._writer_thread = None
        self.connected = False; self.transport.close()

    shutdown = close

    def receive_available(self) -> list[HardwareFrame]:
        if not self.connected: raise RuntimeError("hardware disconnected")
        try: chunk=self.transport.read_available()
        except OSError as exc:
            self.connected = False; self._read_buffer = b""; raise ConnectionError("hardware disconnected") from exc
        if chunk: self._read_buffer+=chunk
        if len(self._read_buffer)>16_384: self._read_buffer=b""; raise ValueError("UART receive buffer overflow")
        frames=[]
        while b"\n" in self._read_buffer:
            raw,self._read_buffer=self._read_buffer.split(b"\n",1); raw+=b"\n"
            if self.raw_observer: self.raw_observer("device_to_host",raw)
            frame = decode_frame(raw)
            if frame.sequence <= self._last_received_sequence: raise ValueError("duplicate or out-of-order frame sequence")
            if self._last_received_sequence and frame.sequence != self._last_received_sequence + 1: raise ValueError("frame sequence gap")
            self._last_received_sequence = frame.sequence; frames.append(frame)
        return frames

    def heartbeat(self, now=None):
        now = time.monotonic() if now is None else now
        if self.connected and now - self.last_heartbeat >= self.heartbeat_s:
            self.send({"type":"heartbeat", "payload":{}}); self.last_heartbeat = now

    def health(self):
        return {"connected": self.connected, "reconnects": self.reconnects, "firmware_version": self.firmware_version,
                "capabilities": self.capabilities, "writer": self.snapshot()}


class PosixSerialTransport:
    """Minimal POSIX serial transport; works with PTYs and real tty devices."""
    BAUD={9600:termios.B9600,19200:termios.B19200,38400:termios.B38400,57600:termios.B57600,115200:termios.B115200}
    def __init__(self, path: str, baud: int = 115200): self.path=path; self.baud=baud; self.fd: int | None=None
    def open(self):
        if self.baud not in self.BAUD: raise ValueError("unsupported UART baud")
        self.fd=os.open(self.path,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
        tty.setraw(self.fd); attrs=termios.tcgetattr(self.fd); speed=self.BAUD[self.baud]
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
        attrs[1] &= ~termios.OPOST
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~getattr(termios, "CRTSCTS", 0)
        attrs[4]=speed; attrs[5]=speed; termios.tcsetattr(self.fd,termios.TCSANOW,attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
    def write(self, raw: bytes):
        if self.fd is None: raise RuntimeError("serial transport is closed")
        view=memoryview(raw); deadline=time.monotonic()+1.0
        while view:
            if time.monotonic() >= deadline: raise TimeoutError("UART write timed out")
            try:
                written=os.write(self.fd,view); view=view[written:]
            except BlockingIOError:
                select.select([], [self.fd], [], min(0.05, max(0.0, deadline-time.monotonic())))
    def read_available(self) -> bytes:
        if self.fd is None: raise RuntimeError("serial transport is closed")
        chunks=[]
        while True:
            try:
                value=os.read(self.fd,4096)
                if not value: break
                chunks.append(value)
            except BlockingIOError: break
        return b"".join(chunks)
    def close(self):
        if self.fd is not None: os.close(self.fd); self.fd=None


class PTYUARTSimulator:
    """Firmware-side PTY endpoint. Host opens ``slave_path`` as a real tty."""
    def __init__(self):
        self.master_fd,self._slave_fd=pty.openpty(); tty.setraw(self._slave_fd)
        os.set_blocking(self.master_fd,False); self.slave_path=os.ttyname(self._slave_fd)
    def write_device(self, raw: bytes):
        view=memoryview(raw); deadline=time.monotonic()+1.0
        while view:
            if time.monotonic() >= deadline: raise TimeoutError("PTY write timed out")
            try:
                written=os.write(self.master_fd,view); view=view[written:]
            except BlockingIOError:
                select.select([], [self.master_fd], [], min(0.05, max(0.0, deadline-time.monotonic())))
    def read_device_available(self) -> bytes:
        chunks=[]
        while True:
            try:
                value=os.read(self.master_fd,4096)
                if not value: break
                chunks.append(value)
            except BlockingIOError: break
        return b"".join(chunks)
    def close(self):
        for fd in (self.master_fd,self._slave_fd):
            try: os.close(fd)
            except OSError: pass

class HardwareSimulator:
    """Same JSONL protocol as SerialHardwareAdapter, used by offline tests."""
    def __init__(self): self.outbound: list[bytes] = []; self.opened = False
    def open(self): self.opened = True
    def write(self, raw): self.outbound.append(raw)
    def inject(self, raw: bytes) -> HardwareFrame: return decode_frame(raw)
