#!/usr/bin/env python3
"""Explicit private remote simulator; --help imports no runtime/models."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import fcntl
import importlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
import threading
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class RemoteLaunchError(RuntimeError):
    """A bounded diagnostic, never raw upstream output or credentials."""


def _private_directory(value: Path) -> Path:
    path = value.expanduser().absolute()
    if path.resolve() != path:
        raise RemoteLaunchError("remote_state_symlink_forbidden")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RemoteLaunchError("remote_state_permissions")
    return path


def _private_file(path: Path):
    try:
        info = path.lstat()
    except OSError:
        raise RemoteLaunchError("remote_private_file_unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RemoteLaunchError("remote_private_file_permissions")


def load_or_create_token(directory: Path) -> str:
    from app.foundation.remote_access import validate_token
    destination = directory / "stand-token"
    supplied = os.environ.get("STAND_ACCESS_TOKEN")
    if destination.exists() or destination.is_symlink():
        _private_file(destination)
        try:
            token = validate_token(destination.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            raise RemoteLaunchError("remote_private_token_invalid") from None
        if supplied is not None and not secrets.compare_digest(supplied.encode(), token.encode()):
            raise RemoteLaunchError("remote_token_source_conflict")
        return token
    try:
        token = validate_token(supplied if supplied is not None else secrets.token_urlsafe(48))
    except ValueError:
        raise RemoteLaunchError("remote_private_token_invalid") from None
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(token + "\n")
    return token


def _manifest(directory: Path, value: dict):
    destination = directory / "launch.json"
    if destination.exists() or destination.is_symlink():
        _private_file(destination)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _load_max(source: Path | None, bundle: Path | None):
    if source is not None:
        from scripts.run_max_json_demo import load_max_source
        demo, frame_factory = load_max_source(source)
        root = source.expanduser().resolve(strict=True)
        hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.glob("*.py"))}
    else:
        from app.foundation.max_client_runtime import _load_demo_bundle
        demo = _load_demo_bundle(str(bundle))
        frame_factory = importlib.import_module("phone_demo_database").demo_frame
        hashes = {"reviewed_bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}
    return demo, frame_factory, hashes


def _max_state(demo, directory: Path) -> Path:
    from app.foundation.max_client_runtime import _private_state
    path = directory / "max-demo"
    if not path.exists() and not path.is_symlink():
        # initialize() refuses an existing root; never reinitialize or repair it.
        demo.initialize(path)
        for child in path.rglob("*"):
            if child.is_file():
                child.chmod(0o600)
            elif child.is_dir():
                child.chmod(0o700)
    _private_state(str(path))
    # Validate every persisted file/directory including TLS certificate paths.
    for child in path.rglob("*"):
        if child.is_symlink():
            raise RemoteLaunchError("max_demo_state_symlink_forbidden")
        info = child.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RemoteLaunchError("max_demo_state_permissions")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RemoteLaunchError("max_demo_state_file_type")
    demo.load_state(path)
    return path


@dataclass
class RemoteRuntime:
    api: object
    gateway: object
    capture: object
    state_dir: Path
    server_thread: threading.Thread
    manifest: dict


@asynccontextmanager
async def _remote_session(*, max_source: Path | None = None, max_bundle: Path | None = None,
                         state_dir: Path, profile: str = "mock", origins=("http://127.0.0.1:8000",),
                         host: str = "127.0.0.1", port: int = 8000, max_port: int = 0,
                         whisper_model: Path | None = None, voice_route: str = "baseline_silero",
                         voice_manifest: Path | None = None, rvc_root: Path | None = None,
                         rvc_python: Path | None = None, rvc_model: Path | None = None,
                         rvc_index: Path | None = None, minimum_free_vram_gb: float | None = None):
    from app.foundation.config import Settings
    from app.foundation.max_client_runtime import _OwnedDemoClient
    from app.foundation.remote_access import PrivateStandGateway, normalize_origin
    from app.foundation.stand import create_stand_app
    from scripts.run_max_json_demo import SyntheticCapture
    if profile not in {"mock", "local_gpu"} or (max_source is None) == (max_bundle is None):
        raise RemoteLaunchError("remote_explicit_profile_and_max_source_required")
    trained = voice_route in {"voxcpm765_rvc100", "silero_rvc100"}
    experimental = voice_route == "voxcpm765_rvc100"
    if voice_route not in {"baseline_silero", "voxcpm765_rvc100", "silero_rvc100"}:
        raise RemoteLaunchError("remote_voice_route_unsupported")
    if trained and (profile != "local_gpu" or not all((rvc_root, rvc_python, rvc_model, rvc_index)) or (experimental and not voice_manifest)):
        raise RemoteLaunchError("remote_trained_voice_requires_explicit_assets")
    if not trained and any((voice_manifest, rvc_root, rvc_python, rvc_model, rvc_index)):
        raise RemoteLaunchError("remote_baseline_rejects_trained_assets")
    if minimum_free_vram_gb is not None and not trained:
        raise RemoteLaunchError("remote_baseline_rejects_vram_override")
    resource_overrides = {}
    if minimum_free_vram_gb is not None:
        Settings(min_available_vram_gb=minimum_free_vram_gb).validate()
        resource_overrides["min_available_vram_gb"] = minimum_free_vram_gb
    expected_voice = "rvc_v2" if trained else "off"
    if os.getenv("MISHKA_VOICE_CONVERSION", expected_voice).strip().lower() != expected_voice:
        raise RemoteLaunchError("remote_baseline_requires_voice_conversion_off")
    allowed = tuple(normalize_origin(value) for value in origins)
    if not allowed:
        raise RemoteLaunchError("remote_origin_required")
    if any(urlsplit(value).scheme == "http" and urlsplit(value).hostname not in
           {"localhost", "127.0.0.1", "::1"} for value in allowed):
        raise RemoteLaunchError("remote_external_origin_requires_https")
    model_reference = "large-v3-turbo"
    if whisper_model is not None:
        try:
            local_model = whisper_model.expanduser().resolve(strict=True)
            if not local_model.is_dir() or not all((local_model / name).is_file()
                                                  for name in ("model.bin", "config.json", "tokenizer.json")):
                raise OSError
        except OSError:
            raise RemoteLaunchError("remote_whisper_model_directory_invalid") from None
        model_reference = str(local_model)
    directory = _private_directory(state_dir)
    token = load_or_create_token(directory)
    demo, frame_factory, hashes = _load_max(max_source, max_bundle)
    state = _max_state(demo, directory)
    server = demo.DemoServer(state, port=max_port)
    thread = threading.Thread(target=server.serve_forever, name="max-remote-synthetic-https", daemon=True)
    thread.start()
    capture = SyntheticCapture(frame_factory)
    owned = api = None
    manifest = {"event": "starting", "pid": os.getpid(), "profile": profile, "hardware_mode": "simulator",
                "synthetic_data": True, "production_ready": False, "voice_conversion": expected_voice,
                "voice_route": voice_route, "tts_provider": "voxcpm765" if experimental else "silero",
                "min_available_vram_gb": minimum_free_vram_gb if minimum_free_vram_gb is not None else Settings().min_available_vram_gb,
                "whisper_model": model_reference, "whisper_local_assets": whisper_model is not None,
                "started_at": datetime.now(timezone.utc).isoformat(), "host": host, "port": port,
                "origins": list(allowed), "max_loopback_port": server.server_address[1],
                "max_source_hashes": hashes, "token_file": "stand-token", "state": "max-demo"}
    try:
        _manifest(directory, manifest)
        phone = await asyncio.to_thread(demo.connect_demo, state, port=server.server_address[1], capture_runner=capture)
        owned = _OwnedDemoClient(phone, server, thread)
        settings = Settings(profile=profile, runtime_mode="exhibition", hardware_mode="simulator",
                            identity_mode="max_json", max_client_source="local_demo", max_synthetic_data=True,
                            max_client_bundle=str((max_bundle or max_source).expanduser().resolve()),
                            max_demo_state=str(state), max_demo_port=server.server_address[1],
                            host=host, port=port, stand_access_token=token, allow_query_token=False,
                            mutation_auth_policy="token_required", voice_conversion=expected_voice,
                            stt_model=model_reference,
                            llm_provider="mock" if profile == "mock" else "ollama",
                            story_backend_receipt_path=os.environ.get("MISHKA_STORY_BACKEND_RECEIPT_PATH", "").strip(),
                            weather_provider="demo", history_db=str(directory / "session_history.sqlite3"),
                            silero_cache_path=str(directory / "cache/silero/v5_5_ru.pt"))
        if trained:
            from dataclasses import replace
            settings = replace(settings, tts_provider="voxcpm765" if experimental else "silero",
                               experimental_voice_manifest=str(voice_manifest.expanduser().absolute()) if experimental else "",
                               rvc_project_root=str(rvc_root.expanduser().absolute()),
                               rvc_python=str(rvc_python.expanduser().absolute()),
                               rvc_model=str(rvc_model.expanduser().absolute()),
                               rvc_index=str(rvc_index.expanduser().absolute()),
                               rvc_worker_script=str(PROJECT_ROOT / "scripts/rvc_trained_worker.py"),
                               rvc_index_rate=.75, rvc_fallback_mode="fail_closed",
                               rvc_tmp_dir=str(directory / "cache/rvc-tmp"),
                               rvc_startup_timeout_s=180, rvc_timeout_s=180,
                               tts_timeout_s=180, e2e_stage_timeout_s=600, e2e_warmup_timeout_s=600,
                               watchdog_stale_after_s=max(settings.watchdog_stale_after_s,
                                   settings.max_turn_seconds + settings.audio_cleanup_timeout_s + 5),
                               **resource_overrides)
        api = create_stand_app(settings, phone_client=owned, simulator_scan=capture.scan)
        if experimental:
            from app.foundation.public_menu_audio import enable_public_menu_audio_cache, public_menu_audio_readiness
            application = api.state.stand.application
            enable_public_menu_audio_cache(application)
            async def warm_public_menus():
                def progress(status):
                    print(json.dumps({"event": "public_menu_warmup_progress", **status}), flush=True)
                return await application.warm_public_menu_audio(progress=progress)
            application.runtime_manager.public_menu_warmup = warm_public_menus
            application.runtime_manager.public_menu_status = lambda: public_menu_audio_readiness(application)
        gateway = PrivateStandGateway(api, token=token, allowed_origins=allowed,
                                     secure_cookie=any(value.startswith("https://") for value in allowed))
        async with api.router.lifespan_context(api):
            if not await asyncio.to_thread(capture.started.wait, 15):
                raise RemoteLaunchError("remote_capture_start_timeout")
            manifest.update(event="running", lifecycle=api.state.lifecycle.snapshot()["state"])
            _manifest(directory, manifest)
            yield RemoteRuntime(api, gateway, capture, directory, thread, manifest)
    finally:
        try:
            if api is not None and not api.state.stand._closed:
                await api.state.stand.aclose()
            elif api is None and owned is not None:
                await asyncio.to_thread(owned.close)
        finally:
            # Owned client normally stopped this server; also close on an
            # early connect/factory failure. Never stop an external listener.
            if thread.is_alive():
                await asyncio.to_thread(server.shutdown)
                await asyncio.to_thread(thread.join, 5)
            server.server_close()
            manifest.update(event="stopped", server_stopped=not thread.is_alive(),
                            capture_stopped=capture.stopped.is_set(),
                            stand_closed=bool(api is not None and api.state.stand._closed))
            _manifest(directory, manifest)
            if thread.is_alive():
                raise RemoteLaunchError("remote_max_service_shutdown_incomplete")


@asynccontextmanager
async def remote_session(*, state_dir: Path, **options):
    """One live owner per private runtime, including startup and cleanup."""
    directory = _private_directory(state_dir)
    destination = directory / ".launcher.lock"
    descriptor = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RemoteLaunchError("remote_lock_permissions")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RemoteLaunchError("remote_state_already_in_use") from None
        async with _remote_session(state_dir=directory, **options) as runtime:
            yield runtime
    finally:
        os.close(descriptor)


def parser():
    result = argparse.ArgumentParser(description="Private synthetic Max HTTPS/SQLite + browser simulator; production_ready=false.")
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--max-source", type=Path, help="Explicit unchanged reviewed Max source directory")
    source.add_argument("--max-bundle", type=Path, help="Absolute reviewed Max module ZIP")
    result.add_argument("--state-dir", type=Path, required=True, help="New or existing owner-only runtime directory")
    result.add_argument("--profile", choices=("mock", "local_gpu"), default="mock")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8000)
    result.add_argument("--external-origin", action="append", required=True, help="Exact allowed browser origin; repeat explicitly for localhost")
    result.add_argument("--max-port", type=int, default=0, help="Owned loopback Max HTTPS listener; 0 allocates a fresh port")
    result.add_argument("--whisper-model", type=Path, help="Existing pinned faster-whisper model directory; avoids mutable model lookup")
    result.add_argument("--voice-route", choices=("baseline_silero", "voxcpm765_rvc100", "silero_rvc100"), default="baseline_silero")
    result.add_argument("--voice-manifest", type=Path)
    result.add_argument("--rvc-root", type=Path)
    result.add_argument("--rvc-python", type=Path)
    result.add_argument("--rvc-model", type=Path)
    result.add_argument("--rvc-index", type=Path)
    result.add_argument("--minimum-free-vram-gb", type=float, help="Explicit trained-route free-VRAM reserve; omitted keeps the Settings default")
    return result


async def _serve(arguments):
    import uvicorn

    class OwnedStandServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # The launcher owns cleanup outside Server.serve(). Uvicorn's
            # default signal replay would terminate before that cleanup runs.
            yield

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    server = None
    stopping = False
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def stop(sig):
        nonlocal stopping
        stopping = True
        if server is None:
            task.cancel()
        else:
            server.handle_exit(sig, None)

    for sig in original:
        loop.add_signal_handler(sig, stop, sig)
    try:
        async with remote_session(max_source=arguments.max_source, max_bundle=arguments.max_bundle,
                                  state_dir=arguments.state_dir, profile=arguments.profile,
                                  origins=arguments.external_origin, host=arguments.host, port=arguments.port,
                                  max_port=arguments.max_port, whisper_model=arguments.whisper_model,
                                  voice_route=arguments.voice_route, voice_manifest=arguments.voice_manifest,
                                  rvc_root=arguments.rvc_root, rvc_python=arguments.rvc_python,
                                  rvc_model=arguments.rvc_model, rvc_index=arguments.rvc_index,
                                  minimum_free_vram_gb=arguments.minimum_free_vram_gb) as runtime:
            # Secrets, rejected query strings and private cards must never
            # reach access logs. Forwarded headers are deliberately untrusted.
            config = uvicorn.Config(runtime.gateway, host=arguments.host, port=arguments.port,
                                    lifespan="off", access_log=False, proxy_headers=False,
                                    timeout_graceful_shutdown=10, log_level="warning")
            server = OwnedStandServer(config)
            print(json.dumps({"event": "remote_listener_starting", "profile": arguments.profile,
                              "production_ready": False, "hardware_mode": "simulator"}), flush=True)
            await server.serve()
    except asyncio.CancelledError:
        if not stopping:
            raise
    finally:
        for sig, handler in original.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def main(argv=None):
    arguments = parser().parse_args(argv)
    if not 1024 <= arguments.port <= 65535 or not 0 <= arguments.max_port <= 65535:
        parser().error("listener ports are outside the supported range")
    try:
        asyncio.run(_serve(arguments))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # Upstream errors can include private paths/cards; diagnostics are in
        # the protected local manifest, never echo arbitrary exception text.
        print(json.dumps({"event": "remote_launcher_failed",
                          "code": str(error) if isinstance(error, RemoteLaunchError) else "startup_or_runtime_failed",
                          "error_type": type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
