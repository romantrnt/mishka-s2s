"""Only synthetic child processes and loopback HTTP peers are used."""
import asyncio
from contextlib import asynccontextmanager
import json
import sys
from types import SimpleNamespace
import pytest
from app.foundation.config import Settings
from app.foundation.input_safety import OllamaInputClassifier
from app.foundation.runtime import ResourceProbe, S2SRuntimeManager
from app.foundation.schedule_weather import ProductionScheduleWeatherService, ScheduleWeatherProviderError, YandexWeatherProvider
from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator

@asynccontextmanager
async def local_http(body=None, *, declare_length=True):
    active, tasks, calls = set(), set(), []
    async def handle(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            length = next((int(v.split(b':',1)[1]) for v in headers.split(b'\r\n') if v.lower().startswith(b'content-length:')),0)
            await reader.readexactly(length)
            calls.append(headers); active.add(writer)
            if body is not None:
                length_header = b'Content-Length: '+str(len(body)).encode()+b'\r\n' if declare_length else b'Connection: close\r\n'
                writer.write(b'HTTP/1.1 200 OK\r\n'+length_header+b'\r\n'+body)
                await writer.drain(); return
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n')
            while not reader.at_eof():
                writer.write(b' '); await writer.drain(); await asyncio.sleep(.01)
        except (ConnectionError,asyncio.IncompleteReadError): pass
        finally:
            active.discard(writer);writer.close();tasks.discard(asyncio.current_task())
    server=await asyncio.start_server(handle,'127.0.0.1',0)
    try: yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}',active,calls
    finally:
        server.close()
        for writer in tuple(active): writer.close()
        pending=tuple(tasks)
        for task in pending: task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        await server.wait_closed()

@pytest.fixture
def diagnostic_children(monkeypatch):
    children=[];spawn=asyncio.create_subprocess_exec
    async def child(*args,**kwargs):
        p=await spawn(sys.executable,'-c','import time; time.sleep(30)',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        children.append(p);return p
    monkeypatch.setattr('app.foundation.runtime.shutil.which',lambda _: '/synthetic/nvidia-smi')
    monkeypatch.setattr('app.foundation.runtime.asyncio.create_subprocess_exec',child)
    monkeypatch.setattr(ResourceProbe,'VRAM_TIMEOUT_S',.08,raising=False)
    return children

async def reap(children):
    for p in children:
        if p.returncode is None:p.kill()
    await asyncio.gather(*(p.wait() for p in children))

@pytest.mark.asyncio
async def test_resource_timeout_reaps_single_shared_process(diagnostic_children):
    try:
        assert await asyncio.gather(*(ResourceProbe.available_vram_gb() for _ in range(3)))==[None]*3
        assert all(p.returncode is not None for p in diagnostic_children),'timed-out diagnostic child is still alive'
        assert len(diagnostic_children)==1,'concurrent readiness duplicated diagnostics'
    finally:await reap(diagnostic_children)

@pytest.mark.asyncio
async def test_resource_cancel_reaps_child(diagnostic_children):
    task=asyncio.create_task(ResourceProbe.available_vram_gb())
    try:
        while not diagnostic_children:await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert diagnostic_children[0].returncode is not None,'cancelled diagnostic child is still alive'
    finally:await reap(diagnostic_children)

@pytest.mark.asyncio
async def test_resource_shared_waiter_and_short_cache(monkeypatch):
    calls=[];spawn=asyncio.create_subprocess_exec
    async def child(*args,**kwargs):
        calls.append(True)
        return await spawn(sys.executable,'-c','import time;time.sleep(.05);print(8192)',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    monkeypatch.setattr('app.foundation.runtime.shutil.which',lambda _: '/synthetic/nvidia-smi')
    monkeypatch.setattr('app.foundation.runtime.asyncio.create_subprocess_exec',child)
    monkeypatch.setattr(ResourceProbe,'VRAM_CACHE_S',.08,raising=False)
    first=asyncio.create_task(ResourceProbe.available_vram_gb());second=asyncio.create_task(ResourceProbe.available_vram_gb())
    await asyncio.sleep(.02);first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    assert await second==8.
    assert await ResourceProbe.available_vram_gb()==8.
    assert len(calls)==1
    await asyncio.sleep(.1)
    assert await ResourceProbe.available_vram_gb()==8.
    assert len(calls)==2

@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[OSError('synthetic playback stop'),asyncio.CancelledError()])
async def test_manager_closes_voice_after_engine_failure(failure):
    closed=[]
    async def cancel():raise failure
    async def close():closed.append(True)
    manager=S2SRuntimeManager(Settings(),None,None,SimpleNamespace(close=close),SimpleNamespace(cancel_all_sessions=cancel))
    with pytest.raises(type(failure)):await manager.close()
    assert closed==[True]

@pytest.mark.asyncio
async def test_manager_reports_both_cleanup_failures():
    async def cancel():raise OSError('synthetic engine failure')
    async def close():raise RuntimeError('synthetic voice failure')
    manager=S2SRuntimeManager(Settings(),None,None,SimpleNamespace(close=close),SimpleNamespace(cancel_all_sessions=cancel))
    with pytest.raises(BaseExceptionGroup) as error:await manager.close()
    assert [type(v) for v in error.value.exceptions]==[OSError,RuntimeError]

@pytest.mark.asyncio
async def test_recovery_finalizes_cancelled_cleanup_group_without_swallowing_cancellation():
    from app.foundation.lifecycle import StartupLifecycle
    from app.foundation.s2s import BoundedConversationEngine, MockLLMProvider, MockTTSProvider, RVCV2Provider

    class BrokenPlayback:
        calls = 0
        async def stop(self):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.CancelledError("synthetic stop cancellation")
            raise OSError("synthetic stop failure")

    engine = BoundedConversationEngine(MockLLMProvider(), MockTTSProvider(), RVCV2Provider(),
                                       BrokenPlayback(), settings=Settings(profile="mock"))
    async def session():
        await asyncio.Event().wait()
    sessions = [asyncio.create_task(session()) for _ in range(2)]
    engine.active_tasks = dict(zip(("first", "second"), sessions))
    manager = S2SRuntimeManager(Settings(profile="local_cpu"), engine.llm, engine.tts,
                                engine.voice, engine)
    lifecycle = StartupLifecycle()
    try:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await manager.recover(lifecycle, trigger="synthetic_cleanup_failure")
        assert isinstance(cancelled.value.__cause__, BaseExceptionGroup)
        assert cancelled.value.__cause__.subgroup(OSError) is not None
        assert all(task.done() for task in sessions)
        assert manager.last_recovery["reason"] == "recovery_cancelled"
        assert manager._recovery_running is False
        snapshot = lifecycle.snapshot()
        assert snapshot["state"] == "DEGRADED"
        assert snapshot["recovery"]["state"] == "degraded"
        assert snapshot["reason"] == "recovery_cancelled"
    finally:
        for task in sessions:
            task.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['validation','input','weather'])
async def test_http_deadline_closes_trickling_response(kind):
    async with local_http() as (url,active,calls):
        for _ in range(3):
            if kind=='validation':
                gate=AsyncResponseValidator(OllamaMiniLLMValidator(url,timeout_s=.2),timeout_ms=150)
                assert not (await gate.validate('Привет!')).allowed
            elif kind=='input':
                with pytest.raises(TimeoutError):await asyncio.wait_for(OllamaInputClassifier(url,timeout_s=.2).classify('Привет!'),.15)
            else:
                provider=YandexWeatherProvider('synthetic',55.,37.,timeout_s=.2,endpoint=url)
                with pytest.raises(ScheduleWeatherProviderError):await ProductionScheduleWeatherService(provider,.15).weather({},'')
        await asyncio.sleep(.05)
        assert calls,'test must reach loopback HTTP peer'
        assert not active,'deadline returned but HTTP response is still being read'

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['validation','weather'])
@pytest.mark.parametrize('declare_length',[True,False])
async def test_http_response_bytes_are_bounded(kind, declare_length):
    value=({'message':{'content':json.dumps({'decision':'allow','confidence':1})}} if kind=='validation' else {'info':{},'forecasts':[],'fact':{'condition':'clear','temp':5}})
    value['padding']='x'*(1024*1024)
    async with local_http(json.dumps(value).encode(), declare_length=declare_length) as (url,active,calls):
        if kind=='validation':assert not (await OllamaMiniLLMValidator(url,timeout_s=2).validate('Привет!')).allowed
        else:
            with pytest.raises(ScheduleWeatherProviderError):await YandexWeatherProvider('synthetic',55.,37.,timeout_s=2,endpoint=url).current({},'')

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['validation','input','weather'])
async def test_http_adapter_accepts_valid_response(kind):
    value=({'info':{},'forecasts':[],'fact':{'condition':'clear','temp':5}} if kind=='weather' else {'message':{'content':json.dumps({'severity':'none','confidence':.99} if kind=='input' else {'decision':'allow','confidence':.99})}})
    async with local_http(json.dumps(value).encode()) as (url,active,calls):
        if kind=='validation':assert (await OllamaMiniLLMValidator(url,timeout_s=2).validate('Привет!')).allowed
        elif kind=='input':assert await OllamaInputClassifier(url,timeout_s=2).classify('Привет!')==('none',.99)
        else:assert (await YandexWeatherProvider('synthetic',55.,37.,timeout_s=2,endpoint=url).current({},''))['temperature_c']==5
        assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["validation", "weather"])
async def test_http_concurrency_is_bounded_and_cancel_drains(kind):
    async with local_http() as (url, active, calls):
        provider = (OllamaMiniLLMValidator(url, timeout_s=2) if kind == "validation"
                    else YandexWeatherProvider("synthetic", 55., 37., timeout_s=2, endpoint=url))
        tasks = [asyncio.create_task(provider.validate("Привет!") if kind == "validation"
                                     else provider.current({}, "")) for _ in range(8)]
        try:
            for _ in range(40):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(.01)
            await asyncio.sleep(.03)
            assert len(calls) == 2
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(.05)
        assert not active


@pytest.mark.asyncio
async def test_repeated_resource_cancel_still_kills_term_resistant_child(monkeypatch, tmp_path):
    marker = tmp_path / "ready"
    children = []
    spawn = asyncio.create_subprocess_exec
    async def child(*args, **kwargs):
        code = "import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(" + repr(str(marker)) + ").touch();time.sleep(30)"
        process = await spawn(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE,
                              stderr=asyncio.subprocess.DEVNULL)
        children.append(process)
        return process
    monkeypatch.setattr("app.foundation.runtime.shutil.which", lambda _: "/synthetic/nvidia-smi")
    monkeypatch.setattr("app.foundation.runtime.asyncio.create_subprocess_exec", child)
    task = asyncio.create_task(ResourceProbe.available_vram_gb())
    try:
        while not marker.exists():
            await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
    finally:
        await reap(children)


@pytest.mark.asyncio
async def test_new_probe_waiter_retries_after_previous_owner_cancellation(monkeypatch):
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def read():
        calls.append(True)
        if len(calls) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        return 8.
    monkeypatch.setattr(ResourceProbe, "_read_vram", staticmethod(read))
    first = asyncio.create_task(ResourceProbe.available_vram_gb())
    await started.wait()
    first.cancel()
    await cleaning.wait()
    second = asyncio.create_task(ResourceProbe.available_vram_gb())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == 8.
    assert len(calls) == 2


# The operator receipt remains policy authority; these peers only return metadata.
def story_contract(url, timeout_s=.15):
    import time
    from app.foundation.story_context import StoryBackendContract, StoryContextReceipt
    return StoryBackendContract(StoryContextReceipt(
        url, "synthetic-model", "a" * 64, "1.2.3", 4096, 256, time.time() + 60,
        truncate_false_verified=True, shift_false_verified=True), timeout_s=timeout_s)


@pytest.mark.asyncio
async def test_story_metadata_deadline_closes_trickling_response():
    from app.foundation.story_context import StoryContextError
    async with local_http() as (url, active, calls):
        contract = story_contract(url)
        for _ in range(3):
            with pytest.raises(StoryContextError):
                await contract.refresh()
        await asyncio.sleep(.05)
        assert len(calls) == 3
        assert not active, "metadata deadline abandoned native network reads"
        assert not contract.ready(url, contract.receipt.model)


@pytest.mark.asyncio
async def test_story_metadata_concurrency_bounded_and_cancel_drains():
    async with local_http() as (url, active, calls):
        contract = story_contract(url, timeout_s=2)
        tasks = [asyncio.create_task(contract.refresh()) for _ in range(8)]
        try:
            for _ in range(40):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(.01)
            await asyncio.sleep(.03)
            assert len(calls) == 2
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(.05)
        assert not active


@pytest.mark.asyncio
@pytest.mark.parametrize("declare_length", [True,False])
async def test_story_metadata_valid_response_and_body_limit(declare_length):
    from app.foundation.story_context import StoryContextError
    value = {"version": "1.2.3", "models": [{"name": "synthetic-model", "digest": "a" * 64,
             "runner": "llamacpp", "context_length": 4096}]}
    async with local_http(json.dumps(value).encode(), declare_length=declare_length) as (url, active, calls):
        contract = story_contract(url, timeout_s=2)
        await contract.refresh()
        assert contract.ready(url, contract.receipt.model)
        assert len(calls) == 2
    value["padding"] = "x" * (1024 * 1024)
    async with local_http(json.dumps(value).encode(), declare_length=declare_length) as (url, active, calls):
        contract = story_contract(url, timeout_s=2)
        with pytest.raises(StoryContextError):
            await contract.refresh()
        assert not contract.ready(url, contract.receipt.model)


@pytest.mark.asyncio
async def test_story_metadata_uses_one_total_deadline(monkeypatch):
    from app.foundation.story_context import StoryContextError
    contract = story_contract("http://127.0.0.1:1", timeout_s=.1)
    calls = []
    async def metadata(path):
        calls.append(path)
        await asyncio.sleep(.06)
        return {"version": "1.2.3", "models": [{"name": "synthetic-model", "digest": "a" * 64,
                "runner": "llamacpp", "context_length": 4096}]}
    monkeypatch.setattr(contract, "_metadata", metadata)
    with pytest.raises(StoryContextError):
        await contract.refresh()
    assert calls == ["/api/version", "/api/ps"]
    assert not contract.ready(contract.receipt.base_url, contract.receipt.model)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://127.0.0.1", "http://example.com", "http://u:p@localhost",
                                  "http://localhost/path", "http://localhost?query", "http://localhost#fragment"])
async def test_story_metadata_keeps_strict_local_endpoint_policy(url):
    from app.foundation.story_context import StoryContextError
    contract = story_contract(url)
    with pytest.raises(StoryContextError, match="story_backend_endpoint"):
        await contract.refresh()
    assert not contract.ready(url, contract.receipt.model)
