#!/usr/bin/env python3
"""Synthetic-only Max HTTPS/SQLite -> MISHKA Stand JSON demonstration.

No physical capture, production data, HTTP listener for MISHKA, or model
installation is available through this launcher. --help needs only Python.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import importlib
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import stat
import sys
import tempfile
import threading
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_CODES = {"demo-1": "demo-1", "demo-2": "demo-2", "unknown": "unknown", "archive": "demo-3"}
COMMANDS = tuple(SCAN_CODES) + ("hangup", "lift", "status", "quit", "help")


class DemoError(RuntimeError):
    """A bounded diagnostic code, never a credential or patient card."""


def load_max_source(source: Path):
    """Import only from the explicitly selected, existing Max source root."""
    try:
        source = source.expanduser().resolve(strict=True)
    except OSError:
        raise DemoError("max_source_missing") from None
    if not source.is_dir() or not all((source / name).is_file() for name in (
        "phone_local_demo.py", "phone_demo_database.py", "phone_ubuntu_client.py",
    )):
        raise DemoError("max_source_invalid")
    # Never silently reuse an already imported client from a different checkout.
    for name in ("phone_local_demo", "phone_demo_database", "phone_ubuntu_client"):
        module = sys.modules.get(name)
        if module is not None and Path(module.__file__).resolve().parent != source:
            raise DemoError("max_source_already_imported_elsewhere")
    sys.path.insert(0, str(source))
    demo = importlib.import_module("phone_local_demo")
    database = importlib.import_module("phone_demo_database")
    if any(Path(module.__file__).resolve().parent != source for module in (demo, database)):
        raise DemoError("max_source_import_mismatch")
    return demo, database.demo_frame


@dataclass
class _Scan:
    code: str
    done: threading.Event = field(default_factory=threading.Event)
    duplicate: bool = False
    failed: bool = False


class SyntheticCapture:
    """Codes reach Max's own frame handler, never a MISHKA identity ingress."""

    def __init__(self, frame_factory):
        self._frame_factory = frame_factory
        self._commands: queue.Queue[_Scan] = queue.Queue()
        self.started = threading.Event()
        self.stopped = threading.Event()

    def __call__(self, handler, devices, stop):
        devices("connected")
        self.started.set()
        try:
            while not stop.is_set():
                try:
                    command = self._commands.get(timeout=0.02)
                except queue.Empty:
                    continue
                frame = None
                try:
                    frame = self._frame_factory(command.code)
                    result = handler(frame)
                    # Preserve Max's known duplicate behavior; never re-resolve.
                    command.duplicate = result == "Repeated RFID tag ignored."
                except Exception:
                    command.failed = True
                finally:
                    if isinstance(frame, bytearray):
                        frame[:] = b"\0" * len(frame)
                    command.done.set()
        finally:
            self.stopped.set()
        return 0

    async def scan(self, code: str, timeout_s: float) -> _Scan:
        if code not in SCAN_CODES.values():
            raise DemoError("synthetic_code_required")
        command = _Scan(code)
        self._commands.put(command)
        if not await asyncio.to_thread(command.done.wait, timeout_s):
            raise DemoError("synthetic_capture_timeout")
        if command.failed:
            raise DemoError("synthetic_capture_failed")
        return command


@dataclass
class DemoRuntime:
    api: Any
    capture: SyntheticCapture
    boundary: Any
    server: Any
    server_thread: threading.Thread
    assumed_control_ready: bool
    startup_lifecycle: dict[str, Any]
    timeout_s: float

    @property
    def stand(self):
        return self.api.state.stand

    async def status(self) -> dict[str, Any]:
        # This is the real read-only ASGI readiness route, without a listener.
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url="http://mishka-in-process") as client:
            response = await client.get("/ready")
        readiness = response.json()
        if readiness.get("production_ready"):
            raise DemoError("synthetic_production_readiness_invariant_failed")
        lifecycle = self.api.state.lifecycle.snapshot()
        state = self.stand.application.canonical_runtime.sessions.get(self.boundary.session_id)
        return {
            "demo_only": True,
            "production_ready": False,
            "profile": "mock",
            "hardware_mode": self.stand.hardware_mode,
            "control_readiness": "TEST-ONLY assumed" if self.assumed_control_ready else "actual",
            "control_admits_users": self.api.state.lifecycle.admits_users(),
            "startup_lifecycle": self.startup_lifecycle,
            "lifecycle": {"state": lifecycle["state"], "reason": lifecycle["reason"]},
            "readiness_http_status": response.status_code,
            "software_runtime_usable": bool(readiness.get("software_runtime_usable")),
            "mode": state.mode if state is not None else "idle",
            "handset": state.handset if state is not None else "down",
            "profile_status": state.profile_status if state is not None else "anonymous",
            "context_present": bool(self.stand.application._canonical_profiles),
        }

    async def command(self, command: str) -> dict[str, Any]:
        if command in SCAN_CODES:
            generation = self.boundary.generation
            scan = await self.capture.scan(SCAN_CODES[command], self.timeout_s)
            # Only the boundary calls poll()/patient()/clear(). Wait for the
            # resulting application context or terminal generation change.
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                changed = self.boundary.generation - generation
                context_ready = bool(self.stand.application._canonical_profiles)
                if not self.boundary._stepping and (
                    (changed >= 1 and (context_ready or scan.duplicate)) or changed >= 2
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                raise DemoError("json_boundary_scan_timeout")
            result = {"reason": "duplicate_suppressed_by_max" if scan.duplicate else "synthetic_scan_processed"}
        elif command in {"lift", "hangup"}:
            # Match admission of the real Stand transport. A cleanup hangup
            # remains safe even when the mock lifecycle does not admit users.
            if command == "lift" and not self.api.state.lifecycle.admits_users():
                result = {"reason": "runtime_unavailable"}
            else:
                injected = await self.stand.inject("handset", {"state": "up" if command == "lift" else "down"},
                                                   self.boundary.session_id, wait_for_audio=False)
                result = {"reason": injected.get("reason", "stand_input_completed")}
                if isinstance(injected.get("error"), dict):
                    result = {"error_code": injected["error"].get("code", "stand_input_failed")}
        elif command == "status":
            result = {"reason": "status"}
        else:
            raise DemoError("unknown_demo_command")
        return {"event": "command", "command": command, **result, **await self.status()}

    def shutdown_status(self) -> dict[str, Any]:
        return {
            "event": "shutdown", "demo_only": True, "production_ready": False,
            "client_closed": bool(self.boundary._close_succeeded),
            "stand_closed": bool(self.stand._closed),
            "capture_stopped": self.capture.stopped.is_set(),
            "server_stopped": not self.server_thread.is_alive(),
            "context_cleared": not bool(self.stand.application._canonical_profiles),
        }


@asynccontextmanager
async def demo_session(max_src: Path, *, hardware_mode: str = "simulator", assume_control_ready: bool = False,
                       timeout_s: float = 15.0):
    """Fresh disposable synthetic DB, real Max TLS client and Stand lifespan."""
    if hardware_mode not in {"fake", "simulator"}:
        raise DemoError("synthetic_hardware_required")
    if not 0 < timeout_s <= 60:
        raise DemoError("invalid_demo_timeout")
    demo, frame_factory = load_max_source(max_src)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from app.foundation.config import Settings
    from app.foundation.lifecycle import RuntimeState
    from app.foundation.stand import create_stand_app

    with tempfile.TemporaryDirectory(prefix="mishka-max-json-synthetic-") as temporary:
        directory = await asyncio.to_thread(demo.initialize, Path(temporary) / "max-demo")
        server = demo.DemoServer(directory, port=0)
        thread = threading.Thread(target=server.serve_forever, name="max-synthetic-https", daemon=True)
        thread.start()
        capture = SyntheticCapture(frame_factory)
        phone = api = None
        try:
            phone = await asyncio.to_thread(demo.connect_demo, directory, port=server.server_address[1], capture_runner=capture)
            # Construct settings directly: no .env, no external profile or data
            # root, no hardware selection or production settings from ambient env.
            settings = Settings(profile="mock", hardware_mode=hardware_mode, runtime_mode="exhibition",
                                identity_mode="max_json", stand_access_token=secrets.token_urlsafe(32),
                                webpanel_enabled=False)
            api = create_stand_app(settings, phone_client=phone)
            boundary = api.state.stand.application._max_client_boundary
            async with api.router.lifespan_context(api):
                lifecycle = api.state.lifecycle
                await asyncio.wait_for(asyncio.shield(lifecycle._warmup), timeout_s)
                before = lifecycle.snapshot()
                startup = {"state": before["state"], "reason": before["reason"]}
                if assume_control_ready:
                    # Explicit CLI TEST-ONLY seam. Runtime /ready providers,
                    # content reports and production readiness are untouched.
                    if api.state.stand.application.settings.profile != "mock":
                        raise DemoError("assumed_readiness_requires_mock")
                    lifecycle._state = RuntimeState.READY
                    lifecycle._reason = "test_only_assumed_control_plane_readiness"
                    lifecycle._changed_at = time.monotonic()
                if not await asyncio.to_thread(capture.started.wait, timeout_s):
                    raise DemoError("synthetic_capture_start_timeout")
                yield DemoRuntime(api, capture, boundary, server, thread, assume_control_ready, startup, timeout_s)
        finally:
            try:
                if api is None and phone is not None:
                    # Only before a boundary exists is this launcher the owner.
                    if not await asyncio.to_thread(phone.close):
                        raise DemoError("unattached_client_shutdown_incomplete")
                elif api is not None and not api.state.stand._closed:
                    await api.state.stand.aclose()
            finally:
                await asyncio.to_thread(server.shutdown)
                await asyncio.to_thread(thread.join, 5)
                server.server_close()
                if thread.is_alive():
                    raise DemoError("demo_server_shutdown_incomplete")


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


async def run_cli(args) -> int:
    runtime = None
    try:
        async with demo_session(args.max_src, hardware_mode=args.hardware_mode,
                                assume_control_ready=args.assume_control_ready) as runtime:
            emit({"event": "started", "commands": list(COMMANDS), **await runtime.status()})
            input_lines = _InputLines(sys.stdin.fileno())
            while True:
                if sys.stdin.isatty():
                    print("max-json-demo> ", end="", flush=True)
                # On POSIX this pipe reader is cancellable without leaving an
                # input() executor thread blocking Ctrl+C/SIGTERM shutdown.
                line = await input_lines.readline()
                if line is None or line.strip().lower() == "quit":
                    break
                command = line.strip().lower()
                if not command:
                    continue
                if command == "help":
                    emit({"event": "help", "commands": list(COMMANDS), "demo_only": True, "production_ready": False})
                elif command not in COMMANDS:
                    emit({"event": "command_rejected", "error_code": "unknown_demo_command", "demo_only": True, "production_ready": False})
                else:
                    emit(await runtime.command(command))
    finally:
        if runtime is not None:
            emit(runtime.shutdown_status())
    return 0


class _InputLines:
    """Cancellable POSIX pipe/terminal reader, including partial input lines."""

    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = bytearray()
        self.eof = False
        self.regular_file = stat.S_ISREG(os.fstat(fd).st_mode)

    async def readline(self) -> str | None:
        while b"\n" not in self.buffer and not self.eof:
            if self.regular_file:
                chunk = os.read(self.fd, 4096)
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_future()

                def ready():
                    if not future.done():
                        try:
                            future.set_result(os.read(self.fd, 4096))
                        except OSError as error:
                            future.set_exception(error)

                loop.add_reader(self.fd, ready)
                try:
                    chunk = await future
                finally:
                    loop.remove_reader(self.fd)
            if not chunk:
                self.eof = True
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise DemoError("demo_input_too_long")
        if not self.buffer:
            return None
        line, separator, remaining = self.buffer.partition(b"\n")
        self.buffer = bytearray(remaining) if separator else bytearray()
        return line.decode("utf-8", errors="replace")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, epilog="Commands: " + ", ".join(COMMANDS))
    result.add_argument("--max-src", type=Path, required=True, help="Explicit path to unchanged Max src directory")
    result.add_argument("--hardware-mode", choices=("fake", "simulator"), default="simulator", help="No physical hardware is supported")
    result.add_argument("--assume-control-ready", action="store_true", help="TEST-ONLY: assume control-plane admission; models/media remain unverified and production_ready=false")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if sys.platform == "win32":
        emit({"event": "error", "error_code": "posix_required_use_ubuntu_or_wsl", "production_ready": False})
        return 2
    # All dependencies and Max source imports are below parsing, so --help is
    # usable without provisioning anything. Avoid printing raw exceptions.
    try:
        with asyncio.Runner() as runner:
            loop = runner.get_loop()
            async def interruptible():
                task = asyncio.current_task()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(signum, task.cancel)
                try:
                    return await run_cli(args)
                finally:
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        loop.remove_signal_handler(signum)
            return runner.run(interruptible())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except Exception as error:
        emit({"event": "error", "error_code": str(error) if isinstance(error, DemoError) else type(error).__name__,
              "demo_only": True, "production_ready": False})
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
