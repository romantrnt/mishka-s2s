"""Synthetic private greeting transport: no real model, name, or recording."""
import asyncio
import os
from pathlib import Path
import stat
import sys

import pytest

from app.foundation import s2s
from app.foundation.s2s import AudioChunk, PrivateRVCUnavailable, RVCV2Provider, RVCProvider, PassthroughVoiceProvider

AUDIO = AudioChunk((.1, -.1, .2), 40000, 7)


def provider(tmp_path, mode="success"):
    project = tmp_path / "project"
    (project / "rvc/infer").mkdir(parents=True)
    (project / "rvc/infer/infer.py").write_text("# synthetic fixture")
    model, index = tmp_path / "model", tmp_path / "index"
    model.write_bytes(b"fixture"); index.write_bytes(b"fixture")
    worker = tmp_path / "worker.py"
    worker.write_text('''import json,sys,shutil,time
from pathlib import Path
print(json.dumps({'event':'ready'}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if r['op']=='shutdown': break
 if MODE=='error':
  print(json.dumps({'event':'error','id':r['id'],'message':'PRIVATE PCM NAME'}),flush=True)
  continue
 if MODE=='slow': time.sleep(10)
 Path(r['output']).parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(r['input'],r['output'])
 print(json.dumps({'event':'converted','id':r['id']}),flush=True)
'''.replace("MODE", repr(mode)))
    return RVCV2Provider(project_root=str(project), python=sys.executable, model_path=str(model),
                         index_path=str(index), worker_script=str(worker), tmp_dir=str(tmp_path / "disk-cache"))


def verified_root():
    try: return s2s._private_rvc_tmpfs_root()
    except PrivateRVCUnavailable: pytest.skip("requires verified Linux tmpfs")


@pytest.mark.parametrize("kind", [RVCProvider, PassthroughVoiceProvider])
def test_mock_private_is_exact_memory_delegate(kind):
    async def run():
        p = kind()
        assert await p.convert_private(AUDIO, lambda: False) is AUDIO
        with pytest.raises(asyncio.CancelledError):
            await p.convert_private(AUDIO, lambda: True)
    asyncio.run(run())


def test_callable_private_never_needs_filesystem(monkeypatch):
    monkeypatch.setattr(s2s, "_private_rvc_tmpfs_root", lambda: pytest.fail("in-memory must not need tmpfs"))
    p = RVCV2Provider(converter=lambda samples, rate: samples)
    result = asyncio.run(p.convert_private(AUDIO, lambda: False))
    assert result.token == AUDIO.token and result.sample_rate == 40000


def test_callable_errors_are_fixed_and_pre_cancel_does_not_run():
    def bad(*_): raise ValueError("PRIVATE NAME PCM")
    p = RVCV2Provider(converter=bad)
    async def run():
        with pytest.raises(PrivateRVCUnavailable, match="^private_rvc_unavailable$"):
            await p.convert_private(AUDIO, lambda: False)
        with pytest.raises(asyncio.CancelledError):
            await p.convert_private(AUDIO, lambda: True)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["disk", "readonly", "missing", "platform"])
def test_unverified_tmpfs_fails_closed_before_audio_or_worker(tmp_path, monkeypatch, kind):
    p = provider(tmp_path)
    if kind == "platform": monkeypatch.setattr(s2s.sys, "platform", "win32")
    elif kind == "readonly": monkeypatch.setattr(s2s.os, "access", lambda *_: False)
    else:
        original = Path.read_text
        def mounts(path, *args, **kwargs):
            if str(path) == "/proc/self/mountinfo":
                if kind == "missing": raise OSError("PRIVATE mount detail")
                info = Path('/dev/shm').stat()
                return f"1 0 {os.major(info.st_dev)}:{os.minor(info.st_dev)} / /dev/shm rw - ext4 /dev/test rw"
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", mounts)
    async def run():
        with pytest.raises(PrivateRVCUnavailable, match="^private_rvc_unavailable$"):
            await p.convert_private(AUDIO, lambda: False)
        assert p._process is None and p.jobs_submitted == 0
        assert not list(tmp_path.rglob('*.wav'))
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["success", "error", "callback_cancel", "task_cancel"])
def test_private_worker_tmpfs_permissions_cleanup_and_cancellation(tmp_path, monkeypatch, mode):
    root = verified_root()
    p = provider(tmp_path, "slow" if "cancel" in mode else mode)
    original_root = p.tmp_dir
    paths = []
    wrote = asyncio.Event()
    original_write = p._write_wav
    def write(path, audio):
        paths.append(path.parent)
        assert path.parent.parent == root
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert p.tmp_dir == original_root
        original_write(path, audio)
        wrote.set()
    monkeypatch.setattr(p, "_write_wav", write)
    async def run():
        cancelled = False
        task = asyncio.create_task(p.convert_private(AUDIO, lambda: cancelled))
        try:
            await asyncio.wait_for(wrote.wait(), 5)
            if mode == "callback_cancel": cancelled = True
            if mode == "task_cancel": task.cancel()
            if "cancel" in mode:
                with pytest.raises(asyncio.CancelledError): await task
                assert p.jobs_cancelled == 1 and p._process is None
            elif mode == "error":
                with pytest.raises(PrivateRVCUnavailable, match="^private_rvc_unavailable$"): await task
                assert p.jobs_failed == 1 and p._process is None
            else:
                assert (await task).token == AUDIO.token
                assert p.jobs_completed == 1
            assert paths and all(not path.exists() for path in paths)
            assert not list(original_root.rglob('*.wav'))
            assert p.tmp_dir == original_root and not p._active
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await p.close()
    asyncio.run(run())


def test_concurrent_private_and_normal_share_lock_not_tmp_root(tmp_path, monkeypatch):
    root = verified_root()
    p = provider(tmp_path)
    paths = []
    original_write = p._write_wav
    def write(path, audio):
        paths.append(path)
        original_write(path, audio)
    monkeypatch.setattr(p, "_write_wav", write)
    async def run():
        try:
            await asyncio.gather(p.convert_private(AUDIO, lambda: False), p.convert(AUDIO, lambda: False))
            assert len(paths) == 2
            assert paths[0].parent.parent == root
            assert paths[1].parent.parent == p.tmp_dir
            assert p.jobs_completed == 2 and p.worker_restarts == 1
            assert all(not path.parent.exists() for path in paths)
        finally: await p.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['success', 'error'])
def test_private_stderr_cannot_leak_into_next_normal_failure(tmp_path, mode, caplog):
    verified_root()
    p = provider(tmp_path, mode)
    script = p.worker_script.read_text().replace(" r=json.loads(line)", " print('PRIVATE_SYNTHETIC_MARKER',file=sys.stderr,flush=True)\n r=json.loads(line)")
    p.worker_script.write_text(script)
    async def run():
        try:
            if mode == 'error':
                with pytest.raises(PrivateRVCUnavailable): await p.convert_private(AUDIO, lambda: False)
            else:
                await p.convert_private(AUDIO, lambda: False)
            assert p._stderr_task is None and not p._stderr_tail
            # Restart only for this deliberate next-worker EOF fixture.
            await p.close()
            # A following non-private worker fails at EOF. Its exception and
            # the resilient wrapper's warning must not recover private stderr.
            p.worker_script.write_text("import json,sys\nprint(json.dumps({'event':'ready'}),flush=True)\nsys.stdin.readline()\n")
            with pytest.raises(RuntimeError) as caught:
                await p.convert(AUDIO, lambda: False)
            assert 'PRIVATE_SYNTHETIC_MARKER' not in str(caught.value)
            wrapped = s2s.ResilientVoiceProvider(p)
            assert await wrapped.convert(AUDIO, lambda: False) is AUDIO
            assert 'PRIVATE_SYNTHETIC_MARKER' not in caplog.text
            assert 'PRIVATE_SYNTHETIC_MARKER' not in str(wrapped.last_fallback_reason)
        finally: await p.close()
    asyncio.run(run())


@pytest.mark.parametrize("private", [True, False])
def test_repeated_cancel_reaps_resistant_worker_before_unlink(tmp_path, monkeypatch, private):
    if private: verified_root()
    p = provider(tmp_path)
    p.worker_script.write_text('''import json,sys,time,signal
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
print(json.dumps({'event':'ready'}),flush=True)
r=json.loads(sys.stdin.readline())
time.sleep(.8)
Path(r['output']).parent.mkdir(parents=True,exist_ok=True)
Path(r['output']).write_bytes(b'SYNTHETIC')
time.sleep(10)
''')
    paths = []
    original = p._write_wav
    wrote = asyncio.Event()
    def write(path, audio):
        paths.append(path.parent)
        original(path, audio)
        wrote.set()
    monkeypatch.setattr(p, '_write_wav', write)
    async def run():
        convert = p.convert_private if private else p.convert
        task = asyncio.create_task(convert(AUDIO, lambda: False))
        process = None
        try:
            await asyncio.wait_for(wrote.wait(), 5)
            process = p._process
            await asyncio.sleep(.05)
            task.cancel()
            await asyncio.sleep(.05)
            task.cancel()
            await asyncio.sleep(.05)
            task.cancel()
            assert p._process is process and process.returncode is None
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 6)
            assert process.returncode is not None and p._process is None
            assert not paths[0].exists()
            await asyncio.sleep(.1)
            assert not paths[0].exists()
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill(); await process.wait()
            await p.close()
    asyncio.run(run())


@pytest.mark.parametrize('cancelled', [False, True])
def test_idle_native_worker_releases_completed_job_and_value(cancelled):
    import gc
    import threading
    import weakref
    import time
    class Marker: pass
    worker = s2s._BoundedNativeWorker('synthetic-private-retention')
    entered, release = threading.Event(), threading.Event()
    text_owner, pcm_owner = Marker(), Marker()
    refs = weakref.ref(text_owner), weakref.ref(pcm_owner)
    def make_call(text, pcm):
        def call():
            entered.set()
            assert release.wait(3)
            assert text is not None
            return pcm
        return call
    future = worker.submit(1, lambda: False, make_call(text_owner, pcm_owner))
    del text_owner, pcm_owner
    try:
        assert entered.wait(3)
        if cancelled: future.cancel()
        release.set()
        if not cancelled:
            result = future.result(timeout=3)
            del result
        worker._jobs.join()
        del future
        for _ in range(100):
            gc.collect()
            if all(ref() is None for ref in refs): break
            time.sleep(.005)
        assert all(ref() is None for ref in refs)
        assert worker.snapshot()['thread_alive']
        assert worker.snapshot()['cancelled_jobs' if cancelled else 'completed_jobs'] == 1
    finally:
        release.set()
        worker._jobs.put(None)
        worker._thread.join(timeout=3)


@pytest.mark.parametrize('phase', ['startup', 'close'])
def test_lifecycle_repeated_cancel_keeps_process_until_reaped(tmp_path, phase):
    p = provider(tmp_path)
    p.worker_script.write_text('''import json,time,signal
signal.signal(signal.SIGTERM,signal.SIG_IGN)
if READY: print(json.dumps({'event':'ready'}),flush=True)
time.sleep(15)
'''.replace('READY', repr(phase == 'close')))
    async def run():
        process = None
        if phase == 'close':
            await p._ensure_worker()
            task = asyncio.create_task(p.close())
        else:
            task = asyncio.create_task(p.convert(AUDIO, lambda: False))
        try:
            for _ in range(100):
                if p._process is not None: break
                await asyncio.sleep(.01)
            process = p._process
            assert process is not None
            await asyncio.sleep(.1)
            task.cancel()
            await asyncio.sleep(.05)
            task.cancel()
            assert p._process is process
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 7)
            assert process.returncode is not None and p._process is None
            assert not list(p.tmp_dir.rglob('*.wav'))
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if process is not None and process.returncode is None:
                process.kill(); await process.wait()
            await p.close()
    asyncio.run(run())
