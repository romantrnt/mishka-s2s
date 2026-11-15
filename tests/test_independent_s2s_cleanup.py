import asyncio
import pytest
from app.foundation.config import Settings
from app.foundation.runtime import S2SRuntimeManager
from app.foundation.s2s import BoundedConversationEngine, MockLLMProvider, MockTTSProvider, RVCV2Provider

@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, asyncio.CancelledError])
async def test_one_playback_failure_does_not_leave_another_session_holding_voice_lock(error_type):
    class BrokenPlayback:
        async def stop(self):
            raise error_type('synthetic device stop failure')
    voice = RVCV2Provider()
    closed = asyncio.Event()
    async def stop_worker(**kwargs):
        closed.set()
    voice._stop_worker_drained = stop_worker
    engine = BoundedConversationEngine(MockLLMProvider(), MockTTSProvider(), voice, BrokenPlayback())
    async def run_session():
        async with voice._job_lock:
            await asyncio.Event().wait()
    sessions = [asyncio.create_task(run_session()) for _ in range(2)]
    engine.active_tasks = dict(zip(['first', 'second'], sessions))
    await asyncio.sleep(0)
    manager = S2SRuntimeManager(Settings(profile='mock'), engine.llm, engine.tts, voice, engine)
    closing = asyncio.create_task(manager.close())
    try:
        done, _ = await asyncio.wait({closing}, timeout=.2)
        assert done, 'shutdown stopped after one session failure and waits on remaining voice job'
        assert all(task.done() for task in sessions)
        assert closed.is_set()
        with pytest.raises(BaseExceptionGroup) as error:
            closing.result()
        assert len(error.value.exceptions) == 3
        assert all(isinstance(item, error_type) for item in error.value.exceptions)
    finally:
        for task in sessions:
            task.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)
        await asyncio.gather(closing, return_exceptions=True)
