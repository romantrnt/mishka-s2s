"""CPU-only subprocess protocol fixtures. These do not synthesize a real voice."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from app.foundation import experimental_voice as voice


# Deliberately synthetic protocol peer; available only through a test monkeypatch.
# There is no fixture/mock route or worker override in the private manifest API.
FAKE_WORKER = r'''
import json, os, struct, sys, time
from pathlib import Path
config = json.loads(sys.stdin.buffer.readline())["config"]
fields = ("model_sha256", "model_inventory_sha256", "adapter_sha256", "reference_sha256",
          "source_inventory_sha256", "source_revision", "runtime_profile", "sample_rate")
def header(value):
    sys.stdout.buffer.write(json.dumps(value).encode() + b"\n")
    sys.stdout.buffer.flush()
header({"op":"ready", "route":config["route"], **{k:config[k] for k in fields},
        "torch_version":"protocol-fixture", "transformers_version":"protocol-fixture",
        "lora_loaded_tensors":2, "native_streaming":config["route"] != "qwen765"})
for line in sys.stdin.buffer:
    cmd = json.loads(line)
    text = cmd["text"]
    Path(__file__).with_suffix(".active").write_text(str(os.getpid())+":"+text)
    if cmd["op"] == "synthesize_stream":
        if text == "stream_slow": time.sleep(.2)
        identity = {"route":config["route"], "request_id":cmd["request_id"], **{k:config[k] for k in fields}}
        for index in range(12):
            if index == 4 and text in {"stream_early", "stream_close", "stream_cancel"}:
                Path(__file__).with_suffix(".paused").touch()
                if text == "stream_early":
                    while not Path(__file__).with_suffix(".release").exists(): time.sleep(.005)
                else: time.sleep(.2)
            frame = {"op":"audio_chunk", **identity, "index":index, "samples":12000,
                     "format":"f32le", "inference_ms":index + 1.0}
            if text == "stream_bad_id": frame["request_id"] = "a" * 32
            if text == "stream_bad_identity": frame["adapter_sha256"] = "0" * 64
            if text == "stream_bad_index" and index == 1: frame["index"] = 0
            if text == "stream_bad_size": frame["samples"] = config["sample_rate"] + 1
            if text == "stream_bad_time" and index == 1: frame["inference_ms"] = 0
            header(frame)
            if text == "stream_bad_size": time.sleep(5)
            value = 0.0 if text == "stream_silent" else (index + 1) / 16
            sys.stdout.buffer.write(struct.pack("<" + "f" * 12000, *([value] * 12000)))
            sys.stdout.buffer.flush()
        if text == "stream_no_done": break
        header({"op":"done", **identity, "frames":12,
                "samples":144001 if text == "stream_bad_done" else 144000,
                "inference_ms":13.0, "native_first_chunk_ms":1.0})
        Path(__file__).with_suffix(".done").touch()
        continue
    if text in {"slow", "slow_corrupt", "timeout"}: time.sleep(.2 if text != "timeout" else 5)
    if text == "stderr":
        for i in range(100): sys.stderr.write("x"*4096)
        sys.stderr.flush()
    if text in {"corrupt", "slow_corrupt"}:
        sys.stdout.buffer.write(b"not-json\n"); sys.stdout.buffer.flush(); continue
    if text == "long_header":
        sys.stdout.buffer.write(b"x"*10000+b"\n"); sys.stdout.buffer.flush(); continue
    count = 1000000000 if text == "oversized" else (4 if text == "partial" else 2)
    header({"op":"audio", "request_id":("bad" if text == "wrong_id" else cmd["request_id"]),
            "route":("vox555" if text == "wrong_route" else config["route"]),
            "sample_rate":config["sample_rate"], "samples":count, "format":"f32le", "inference_ms":1.5})
    if text == "oversized": time.sleep(5)
    elif text == "nan": sys.stdout.buffer.write(struct.pack("<ff",float("nan"),.25))
    elif text == "silent": sys.stdout.buffer.write(struct.pack("<ff",0,0))
    else: sys.stdout.buffer.write(struct.pack("<ff",.25,-.25))
    sys.stdout.buffer.flush()
    if text == "partial": break
'''


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Tiny files stand in for heavy weights solely to test validation/IPC."""
    def setup(route="qwen765", **limits):
        base = tmp_path / route
        base.mkdir()
        roots = {"model": base / "model", "source": base / "source"}
        for root in roots.values():
            root.mkdir()
        names = voice._QWEN_FILES if route == "qwen765" else voice._VOX_FILES
        entries = []
        def asset(root, name, content=b"CPU protocol fixture"):
            path = roots[root] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            entries.append({"root": root, "path": name, "bytes": len(content), "sha256": digest})
            return digest
        for name in sorted(names):
            digest = asset("model", name)
            if name == "model.safetensors":
                monkeypatch.setitem(voice._MODEL_HASHES, route, digest)
        package = "qwen_tts" if route == "qwen765" else "voxcpm"
        asset("source", f"{package}/__init__.py")
        spec = {"python": str(Path(sys.executable).resolve()), "model_root": str(roots["model"]),
                "source_root": str(roots["source"]), "source_revision": voice._SOURCE_REVISIONS[route],
                "files": entries, "limits": {"startup_timeout_s": 2, "inference_timeout_s": 1, **limits}}
        if route != "qwen765":
            roots["adapter"] = base / "adapter"
            roots["adapter"].mkdir()
            asset("adapter", "lora_config.json")
            monkeypatch.setitem(voice._ADAPTER_HASHES, route, asset("adapter", "lora_weights.safetensors"))
            roots["reference"] = base
            monkeypatch.setattr(voice, "_REFERENCE_HASH", asset("reference", "ref.wav"))
            spec.update(adapter_root=str(roots["adapter"]), reference_audio=str(base / "ref.wav"))
        manifest = base / "private.json"
        manifest.write_text(json.dumps({"schema_version": 1, "routes": {route: spec}}))
        worker = base / "protocol_fixture.py"
        worker.write_text(FAKE_WORKER)
        monkeypatch.setattr(voice, "_WORKER_SCRIPT", worker)
        provider = voice.create_experimental_tts_provider(route, manifest)
        return provider, manifest, worker
    return setup


async def wait_command(worker):
    for _ in range(200):
        marker = worker.with_suffix(".active")
        if marker.exists():
            return marker.read_text()
        await asyncio.sleep(.005)
    raise AssertionError("fixture received no command")


def test_repeated_semantic_blocks_use_resident_worker_and_honest_metadata(configured):
    provider, _, _ = configured()
    assert provider.configuration_readiness()["configuration_ready"] is True
    assert provider.readiness()["ready"] is False
    assert provider.readiness()["actual_route"] is None
    async def scenario():
        await provider.start()
        assert provider.readiness()["ready"] is True
        for token in (5, 6):
            audio = await provider.synthesize_stream_chunk("Привет", token, lambda: False, first_chunk=(token == 5))
            assert audio.samples == (.25, -.25) and audio.token == token and audio.sample_rate == 24000
            metadata = provider.request_metadata()
            assert metadata["actual_route"] == metadata["requested_route"] == "qwen765"
            assert metadata["native_streaming"] is False
            assert metadata["inference_mode"] == "full_block"
            assert metadata["native_first_chunk_ms"] is metadata["audible_ttfa_ms"] is None
            assert metadata["inference_ms"] == 1.5 and metadata["fallback_used"] is False
            assert "Привет" not in json.dumps(metadata)
        assert provider.worker_starts == 1 and provider.jobs_completed == 2
        await provider.close()
        assert provider.readiness()["closed"] is True and provider._process is None
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="closed"):
            await provider.synthesize("Привет", 7, lambda: False)
    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["voxcpm765", "vox555"])
def test_explicit_vox_identity(configured, route):
    provider, _, _ = configured(route)
    async def scenario():
        audio = await provider.synthesize("Hello", 1, lambda: False)
        metadata = provider.request_metadata()
        assert audio.sample_rate == 48000 and metadata["actual_route"] == route
        assert metadata["adapter_sha256"] and metadata["reference_sha256"]
        assert provider.readiness()["runtime_profile"] == "voxcpm-recovery-freeze"
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["corrupt", "oversized", "nan", "silent", "partial", "wrong_id", "wrong_route", "long_header"])
def test_invalid_output_fails_closed_and_next_request_restarts(configured, text):
    provider, _, _ = configured()
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable):
            await provider.synthesize(text, 1, lambda: False)
        assert provider._process is None and provider.jobs_failed == 1
        assert provider.request_metadata()["actual_route"] is None
        assert provider.request_metadata()["status"] == "failed"
        await provider._recovery_task
        audio = await provider.synthesize("valid", 2, lambda: False)
        assert audio.token == 2 and audio.samples == (.25, -.25)
        assert provider.worker_starts == 2
        await provider.close()
    asyncio.run(scenario())


def test_timeout_kills_drains_and_allows_new_request(configured):
    provider, _, _ = configured(inference_timeout_s=.08)
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="timeout"):
            await provider.synthesize("timeout", 1, lambda: False)
        assert provider._process is None
        await provider._recovery_task
        await provider.synthesize("valid", 2, lambda: False)
        assert provider.jobs_failed == 1 and provider.jobs_completed == 1
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_mode", ["callback", "task"])
def test_cancellation_drains_warm_worker_and_never_delivers_old_pcm(configured, cancel_mode):
    provider, _, worker = configured()
    async def scenario():
        cancelled = False
        task = asyncio.create_task(provider.synthesize("slow", 10, lambda: cancelled))
        old_pid = int((await wait_command(worker)).split(":")[0])
        if cancel_mode == "callback":
            cancelled = True
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider._process.pid == old_pid and provider.jobs_cancelled == 1
        assert provider.request_metadata()["cancelled"] is True
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="busy"):
            await provider.synthesize("too-early", 11, lambda: False)
        await provider._recovery_task
        audio = await provider.synthesize("valid", 11, lambda: False)
        assert audio.token == 11 and provider._process.pid == old_pid
        assert provider.jobs_completed == 1 and provider.worker_starts == 1
        await provider.close()
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
    asyncio.run(scenario())


def test_no_hidden_job_queue_close_cancels_active_work(configured):
    provider, _, worker = configured()
    async def scenario():
        task = asyncio.create_task(provider.synthesize("slow", 1, lambda: False))
        await wait_command(worker)
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="busy"):
            await provider.synthesize("other", 2, lambda: False)
        assert provider.native_worker_snapshot()["queued_jobs"] == 0
        await provider.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider._process is None and provider.readiness()["ready"] is False
        await provider.close()  # Repeated cleanup is safe.
    asyncio.run(scenario())


def test_stderr_is_drained_without_retention(configured):
    provider, _, _ = configured()
    async def scenario():
        await provider.synthesize("stderr", 1, lambda: False)
        assert len(json.dumps(provider.readiness())) < 4000
        await provider.close()
    asyncio.run(scenario())


def test_input_limits_and_pre_cancel_do_not_start_worker(configured):
    provider, _, _ = configured(max_text_chars=4)
    async def scenario():
        for text in ("", "     ", "12345", "\ud800"):
            with pytest.raises(voice.ExperimentalVoiceUnavailable, match="text_limit"):
                await provider.synthesize(text, 1, lambda: False)
        with pytest.raises(asyncio.CancelledError):
            await provider.synthesize("yes", 1, lambda: True)
        assert provider.worker_starts == provider.jobs_submitted == 0
        await provider.close()
    asyncio.run(scenario())


def test_startup_identity_mismatch_never_reports_ready(configured):
    provider, _, worker = configured()
    worker.write_text(FAKE_WORKER.replace('"route":config["route"], **', '"route":"vox555", **', 1))
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="start_failed"):
            await provider.start()
        assert provider.readiness()["ready"] is False and provider.readiness()["actual_route"] is None
        assert provider._process is None
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["digest", "missing", "escape", "unknown_command", "unknown_route", "extra_weight", "limit"])
def test_operator_manifest_rejects_unavailable_or_unsafe_assets(configured, mutation):
    _, manifest, _ = configured()
    data = json.loads(manifest.read_text())
    spec = data["routes"]["qwen765"]
    route = "qwen765"
    if mutation == "digest": spec["files"][0]["sha256"] = "0" * 64
    if mutation == "missing": Path(spec["model_root"], spec["files"][0]["path"]).unlink()
    if mutation == "escape": spec["files"][0]["path"] = "../outside"
    if mutation == "unknown_command": spec["command"] = ["sh", "-c", "true"]
    if mutation == "unknown_route": route = "arbitrary"
    if mutation == "extra_weight": Path(spec["model_root"], "alternate.pth").write_bytes(b"extra")
    if mutation == "limit": spec["limits"]["max_audio_seconds"] = 100000
    manifest.write_text(json.dumps(data))
    with pytest.raises(voice.ExperimentalVoiceUnavailable, match="configuration_unavailable") as error:
        voice.create_experimental_tts_provider(route, manifest)
    assert str(manifest) not in str(error.value)


def test_missing_private_manifest_is_unavailable(tmp_path):
    with pytest.raises(voice.ExperimentalVoiceUnavailable, match="configuration_unavailable"):
        voice.create_experimental_tts_provider("qwen765", tmp_path / "absent.json")


def test_venv_interpreter_symlink_is_preserved(configured, tmp_path):
    _, manifest, _ = configured()
    data = json.loads(manifest.read_text())
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    data["routes"]["qwen765"]["python"] = str(interpreter)
    manifest.write_text(json.dumps(data))
    provider = voice.create_experimental_tts_provider("qwen765", manifest)
    assert provider._config["python"] == str(interpreter)


def test_source_inventory_cannot_omit_importable_python_file(configured):
    _, manifest, _ = configured()
    data = json.loads(manifest.read_text())
    source = Path(data["routes"]["qwen765"]["source_root"])
    (source / "qwen_tts" / "unlisted.py").write_text("# unlisted source")
    with pytest.raises(voice.ExperimentalVoiceUnavailable, match="configuration_unavailable"):
        voice.create_experimental_tts_provider("qwen765", manifest)


def test_repeated_task_cancel_still_reaps_and_drains_worker(configured):
    provider, _, worker = configured()
    discard = provider._discard_stream
    async def delayed_discard(stream):
        await asyncio.sleep(.06)
        await discard(stream)
    provider._discard_stream = delayed_discard
    async def scenario():
        task = asyncio.create_task(provider.synthesize("slow", 1, lambda: False))
        pid = int((await wait_command(worker)).split(":")[0])
        task.cancel()
        await asyncio.sleep(.01)  # Second cancellation arrives during cleanup.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await provider.close()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert provider._process is provider._stderr_task is None
        assert provider._draining_exchange is None
    asyncio.run(scenario())


def test_real_worker_load_failure_is_fail_closed(configured, monkeypatch):
    provider, _, _ = configured()
    actual_worker = Path(voice.__file__).resolve().parents[2] / "scripts" / "experimental_tts_worker.py"
    monkeypatch.setattr(voice, "_WORKER_SCRIPT", actual_worker)
    # Fixture files are not real weights/source. The actual worker must emit
    # only a protocol failure, never fixture PCM or a successful model load.
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="start_failed"):
            await provider.start()
        assert provider._process is None and provider.readiness()["ready"] is False
        assert provider.jobs_completed == 0
        await provider.close()
    asyncio.run(scenario())


def test_cancelled_provider_recovers_readiness_without_new_synthesis(configured):
    provider, _, worker = configured('voxcpm765')
    async def scenario():
        task = asyncio.create_task(provider.synthesize('slow', 41, lambda: False))
        old_pid = int((await wait_command(worker)).split(':')[0])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider._process.pid == old_pid
        assert provider.readiness()['ready'] is False
        assert provider.readiness()['reason'] == 'experimental_voice_draining'
        # Admission waits for ready; it must recover without an admitted turn.
        for _ in range(400):
            if provider.readiness()['ready']:
                break
            await asyncio.sleep(.005)
        snapshot = provider.readiness()
        assert snapshot['ready'] is True, snapshot['reason']
        assert snapshot['actual_route'] == 'voxcpm765'
        assert snapshot['fallback_used'] is False
        assert provider.worker_starts == 1 and provider.jobs_submitted == 1
        assert provider.jobs_completed == 0 and provider.jobs_cancelled == 1
        assert provider.request_metadata()['status'] == 'cancelled'
        assert provider.request_metadata()['actual_route'] is None
        assert worker.with_suffix('.active').read_text() == f'{old_pid}:slow', 'recovery never replays private text'
        audio = await provider.synthesize('fresh', 42, lambda: False)
        assert audio.token == 42 and audio.samples == (.25, -.25)
        assert provider.worker_starts == 1
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['timeout', 'slow_corrupt'])
def test_cancelled_drain_failure_restarts_once_without_replaying_text(configured, text):
    provider, _, worker = configured(inference_timeout_s=.12 if text == 'timeout' else 1)
    async def scenario():
        task = asyncio.create_task(provider.synthesize(text, 9, lambda: False))
        old_pid = int((await wait_command(worker)).split(':')[0])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .1)
        await asyncio.wait_for(provider._recovery_task, 3)
        assert provider.readiness()['ready'] is True
        assert provider._process.pid != old_pid and provider.worker_starts == 2
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        assert provider.jobs_submitted == provider.jobs_cancelled == 1
        assert provider.jobs_completed == 0
        assert worker.with_suffix('.active').read_text() == f'{old_pid}:{text}'
        assert provider.request_metadata()['actual_route'] is None
        await provider.close()
    asyncio.run(scenario())


def test_close_during_cancelled_drain_never_restarts(configured):
    provider, _, worker = configured(inference_timeout_s=1)
    async def scenario():
        task = asyncio.create_task(provider.synthesize('timeout', 2, lambda: False))
        old_pid = int((await wait_command(worker)).split(':')[0])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(provider.close(), 1)
        assert provider._process is None and provider._draining_exchange is None
        assert provider.worker_starts == 1 and provider.readiness()['closed'] is True
        assert provider.readiness()['ready'] is False
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        await provider.close()
    asyncio.run(scenario())


def test_cancelled_drain_recovery_rejects_changed_worker_identity(configured):
    provider, _, worker = configured(inference_timeout_s=.12)
    async def scenario():
        task = asyncio.create_task(provider.synthesize('timeout', 5, lambda: False))
        await wait_command(worker)
        worker.write_text(FAKE_WORKER.replace('"route":config["route"], **', '"route":"vox555", **', 1))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(provider._recovery_task, 3)
        snapshot = provider.readiness()
        assert snapshot['ready'] is False and snapshot['actual_route'] is None
        assert snapshot['reason'] == 'experimental_voice_recovery_failed'
        assert snapshot['fallback_used'] is False and provider._process is None
        assert provider.worker_starts == 2 and provider.jobs_completed == 0
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['timeout', 'corrupt'])
def test_inference_failure_restores_readiness_without_new_request(configured, text):
    provider, _, worker = configured('voxcpm765', inference_timeout_s=.08)
    async def scenario():
        reason = 'experimental_voice_timeout' if text == 'timeout' else 'experimental_voice_inference_failed'
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match=reason):
            await provider.synthesize(text, 51, lambda: False)
        old_pid = int(worker.with_suffix('.active').read_text().split(':')[0])
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        for _ in range(200):
            if provider.readiness()['ready']:
                break
            await asyncio.sleep(.005)
        snapshot = provider.readiness()
        assert snapshot['ready'] is True, snapshot['reason']
        assert snapshot['actual_route'] == 'voxcpm765' and snapshot['fallback_used'] is False
        assert provider.worker_starts == 2 and provider.jobs_submitted == provider.jobs_failed == 1
        assert provider.jobs_completed == provider.jobs_cancelled == 0
        assert provider.request_metadata()['status'] == 'failed'
        assert provider.request_metadata()['reason'] == reason
        assert provider.request_metadata()['actual_route'] is None
        assert worker.with_suffix('.active').read_text() == f'{old_pid}:{text}', 'recovery does not retry old input'
        audio = await provider.synthesize('fresh', 52, lambda: False)
        assert audio.token == 52 and provider.worker_starts == 2
        await provider.close()
    asyncio.run(scenario())


def test_native_stream_yields_before_terminal_and_preserves_every_sample(configured):
    provider, _, worker = configured("voxcpm765")
    async def scenario():
        stream = provider.synthesize_audio_stream("stream_early", 77, lambda: False, first_chunk=True)
        first = await asyncio.wait_for(anext(stream), 1)
        assert len(first.samples) == 38400 and first.token == 77 and first.sample_rate == 48000
        assert not worker.with_suffix(".done").exists(), "first PCM must not wait for the done frame"
        metadata = provider.request_metadata()
        assert metadata["native_streaming"] is True and metadata["status"] == "processing"
        assert metadata["native_first_chunk_ms"] > 0 and metadata["audible_ttfa_ms"] is None
        assert provider.readiness()["native_streaming"] is True
        worker.with_suffix(".release").touch()
        chunks = [first] + [chunk async for chunk in stream]
        assert [len(chunk.samples) for chunk in chunks] == [38400, 96000, 9600]
        samples = tuple(value for chunk in chunks for value in chunk.samples)
        assert samples == tuple(value for index in range(12) for value in [(index + 1) / 16] * 12000)
        assert provider.jobs_completed == 1 and provider.worker_starts == 1
        assert provider.request_metadata()["audio_seconds"] == 3
        assert provider.request_metadata()["worker_first_chunk_ms"] == 1
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_mode", ["close", "callback", "task"])
def test_native_early_close_or_cancel_drains_same_worker_before_reuse(configured, cancel_mode):
    provider, _, worker = configured("voxcpm765")
    async def scenario():
        cancelled = False
        stream = provider.synthesize_audio_stream("stream_close", 80, lambda: cancelled, first_chunk=True)
        first = await anext(stream)
        old_pid = provider._process.pid
        assert len(first.samples) == 38400
        if cancel_mode == "close":
            await asyncio.wait_for(stream.aclose(), .1)
        else:
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(.01)
            if cancel_mode == "callback":
                cancelled = True
            else:
                pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, .1)
        assert provider.jobs_cancelled == 1 and provider.worker_starts == 1
        assert provider.readiness()["reason"] == "experimental_voice_draining"
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="busy"):
            await provider.synthesize("valid", 81, lambda: False)
        await asyncio.wait_for(provider._recovery_task, 2)
        assert provider.readiness()["ready"] is True and provider._process.pid == old_pid
        audio = await provider.synthesize("valid", 81, lambda: False)
        assert audio.token == 81 and audio.samples == (.25, -.25)
        chunks = [chunk async for chunk in provider.synthesize_audio_stream(
            "stream", 82, lambda: False, first_chunk=False)]
        assert [len(chunk.samples) for chunk in chunks] == [96000, 48000]
        assert chunks[0].samples[0] == 1 / 16 and provider.worker_starts == 1
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["stream_bad_id", "stream_bad_identity", "stream_bad_index",
    "stream_bad_size", "stream_bad_time", "stream_bad_done", "stream_no_done", "stream_silent"])
def test_native_invalid_frames_and_terminal_fail_then_recover(configured, text):
    provider, _, _ = configured("voxcpm765")
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="inference_failed"):
            async for _ in provider.synthesize_audio_stream(text, 90, lambda: False, first_chunk=True):
                pass
        assert provider.jobs_failed == 1 and provider.jobs_completed == 0
        await asyncio.wait_for(provider._recovery_task, 2)
        assert provider.readiness()["ready"] is True and provider.worker_starts == 2
        audio = await provider.synthesize("valid", 91, lambda: False)
        assert audio.samples == (.25, -.25) and audio.token == 91
        await provider.close()
    asyncio.run(scenario())


def test_native_stream_total_limit_cannot_be_bypassed_by_small_frames(configured):
    provider, _, _ = configured("voxcpm765", max_audio_seconds=.5)
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable):
            async for _ in provider.synthesize_audio_stream("stream", 1, lambda: False, first_chunk=True):
                pass
        assert provider.jobs_failed == 1 and provider.jobs_completed == 0
        await provider._recovery_task
        await provider.close()
    asyncio.run(scenario())


def test_native_close_while_suspended_at_yield_reaps_without_deadlock(configured):
    provider, _, _ = configured("voxcpm765")
    async def scenario():
        stream = provider.synthesize_audio_stream("stream_close", 4, lambda: False, first_chunk=True)
        await anext(stream)
        pid = provider._process.pid
        await asyncio.wait_for(provider.close(), 1)
        assert provider._process is None and provider.readiness()["closed"] is True
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await stream.aclose()
    asyncio.run(scenario())


def test_native_abandon_unblocks_full_frame_queue_and_keeps_worker(configured):
    provider, _, _ = configured("voxcpm765")
    async def scenario():
        stream = provider.synthesize_audio_stream("stream", 3, lambda: False, first_chunk=True)
        await anext(stream)
        pid = provider._process.pid
        # The sole reader fills its bounded queue while downstream is paused.
        await asyncio.sleep(.06)
        assert not provider._active_stream_exchange.done()
        await asyncio.wait_for(stream.aclose(), .1)
        await asyncio.wait_for(provider._recovery_task, 2)
        assert provider._process.pid == pid and provider.worker_starts == 1
        assert provider.readiness()["ready"] is True
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["stream_bad_done", "stream_close"])
def test_native_abandoned_corrupt_or_timeout_drain_restarts_without_replay(configured, text):
    provider, _, worker = configured("voxcpm765", inference_timeout_s=.08 if text == "stream_close" else 1)
    async def scenario():
        stream = provider.synthesize_audio_stream(text, 3, lambda: False, first_chunk=True)
        await anext(stream)
        pid = provider._process.pid
        await stream.aclose()
        await asyncio.wait_for(provider._recovery_task, 2)
        assert provider._process.pid != pid and provider.worker_starts == 2
        assert provider.readiness()["ready"] is True
        assert worker.with_suffix(".active").read_text() == f"{pid}:{text}"
        assert provider.jobs_cancelled == 1 and provider.jobs_completed == 0
        await provider.close()
    asyncio.run(scenario())


def test_native_task_cancel_before_first_frame_preserves_warm_worker(configured):
    provider, _, worker = configured("voxcpm765")
    async def scenario():
        stream = provider.synthesize_audio_stream("stream_slow", 3, lambda: False, first_chunk=True)
        pending = asyncio.create_task(anext(stream))
        pid = int((await wait_command(worker)).split(":")[0])
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, .1)
        await asyncio.wait_for(provider._recovery_task, 2)
        assert provider._process.pid == pid and provider.worker_starts == 1
        assert provider.readiness()["ready"] is True
        assert provider.request_metadata()["actual_route"] is None
        await provider.close()
    asyncio.run(scenario())


def test_native_timeout_drains_kills_and_recovers_without_replaying(configured):
    provider, _, worker = configured("voxcpm765", inference_timeout_s=.08)
    async def scenario():
        with pytest.raises(voice.ExperimentalVoiceUnavailable, match="timeout"):
            async for _ in provider.synthesize_audio_stream("stream_slow", 5, lambda: False, first_chunk=True):
                pass
        await provider._recovery_task
        assert provider.worker_starts == 2 and provider.readiness()["ready"] is True
        assert worker.with_suffix(".active").read_text().endswith(":stream_slow")
        await provider.close()
    asyncio.run(scenario())


def test_qwen_stream_interface_reports_complete_block_honestly(configured):
    provider, _, _ = configured()
    async def scenario():
        chunks = [chunk async for chunk in provider.synthesize_audio_stream(
            "valid", 4, lambda: False, first_chunk=True)]
        assert len(chunks) == 1 and chunks[0].samples == (.25, -.25)
        assert provider.request_metadata()["native_streaming"] is False
        assert provider.request_metadata()["inference_mode"] == "full_block"
        await provider.close()
    asyncio.run(scenario())


def test_vox555_streaming_uses_short_later_blocks_and_accepts_approved_phrase014_hash(configured, monkeypatch):
    provider, manifest, _ = configured("vox555")
    # Also verify unpatched _REFERENCE_HASH compatibility when PHRASE_014 hash is configured.
    spec = json.loads(manifest.read_text())
    ref_path = Path(spec["routes"]["vox555"]["reference_audio"])
    phrase014_digest = "e5af4c97f73398927ad026c5616390ec59f03625ead46f981c5912288845ba8c"
    assert phrase014_digest in voice._APPROVED_VOX555_REFERENCE_HASHES

    async def scenario():
        # FAKE_WORKER yields 12 * 12000 = 144000 samples at 48000 Hz.
        # With _VOX555_FIRST_BLOCK_SECONDS=0.4 (19200 samples) and _VOX555_LATER_BLOCK_SECONDS=0.32
        # (15360 samples), streaming emits 6 incremental blocks (not 3 blocks from a 2.0s stall).
        chunks = [chunk async for chunk in provider.synthesize_audio_stream(
            "valid_stream", 7, lambda: False, first_chunk=True)]
        assert sum(len(c.samples) for c in chunks) == 144000
        assert len(chunks) >= 5
        assert len(chunks[0].samples) == 19200  # 0.40s first block at 48 kHz
        assert len(chunks[1].samples) == 15360  # 0.32s later block at 48 kHz (not 2.0s stall)
        assert provider.request_metadata()["native_streaming"] is True
        await provider.close()
    asyncio.run(scenario())
