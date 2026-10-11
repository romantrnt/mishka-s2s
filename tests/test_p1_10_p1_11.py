import asyncio
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.foundation.adapters import MockProfileProvider, RecordingAudio, RecordingLED, RuleBasedIntentClassifier
from app.foundation.application import ScenarioApplication
from app.foundation.content import FileScenarioRepository
from app.foundation.domain import Lifecycle, SessionRecord
from app.foundation.observability import StructuredEventPublisher
from app.foundation.repositories import InMemorySessionRepository
from app.metrics.latency import LatencyTracker, TurnLatency


def _latency(vad=1.0):
    return TurnLatency(vad_ms=vad, stt_ms=2.0, total_ms=3.0)


def test_latency_record_flushes_in_order_without_corrupting_jsonl(tmp_path):
    tracker = LatencyTracker(str(tmp_path / "latency.jsonl"), queue_limit=16, max_bytes=100_000)
    statuses = [tracker.record(_latency(index)) for index in range(8)]
    snapshot = tracker.flush()
    assert all(item["status"] == "queued" for item in statuses)
    assert snapshot["written_records"] == 8
    rows = [json.loads(line) for line in (tmp_path / "latency.jsonl").read_text().splitlines()]
    assert [row["record_id"] for row in rows] == list(range(1, 9))
    assert all("start_time" not in row and "timestamp" in row for row in rows)
    tracker.close()



def test_latency_record_returns_while_writer_is_slow(tmp_path):
    tracker = LatencyTracker(str(tmp_path / "latency.jsonl"), queue_limit=4)
    started = threading.Event()
    release = threading.Event()
    original_rotate = tracker._rotate_if_needed

    def slow_rotate(path):
        started.set()
        release.wait(timeout=2)
        return original_rotate(path)

    tracker._rotate_if_needed = slow_rotate
    status = tracker.record(_latency())
    assert status["status"] == "queued"
    assert started.wait(timeout=1)
    release.set()
    assert tracker.flush(timeout=2)["written_records"] == 1
    tracker.close()


def test_latency_writer_exception_is_failed_and_visible(tmp_path):
    tracker = LatencyTracker(str(tmp_path / "latency.jsonl"), queue_limit=4)

    def fail_rotate(_path):
        raise OSError("disk full")

    tracker._rotate_if_needed = fail_rotate
    status = tracker.record(_latency())
    snapshot = tracker.flush(timeout=2)
    assert status["status"] == "queued"
    assert snapshot["failed_records"] == 1
    assert snapshot["last_error"]["exception_type"] == "OSError"
    assert any(item["status"] == "failed" for item in snapshot["recent_statuses"])
    tracker.close()


def test_latency_concurrent_record_calls_have_one_bounded_writer(tmp_path):
    tracker = LatencyTracker(str(tmp_path / "latency.jsonl"), queue_limit=128)
    statuses = []
    lock = threading.Lock()

    def produce(index):
        status = tracker.record(_latency(index))
        with lock:
            statuses.append(status)

    threads = [threading.Thread(target=produce, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tracker.flush()
    tracker.flush()
    rows = [json.loads(line) for line in (tmp_path / "latency.jsonl").read_text().splitlines()]
    assert len(rows) == 40
    assert len({row["record_id"] for row in rows}) == 40
    assert tracker.snapshot()["writer"]["alive"]
    tracker.close()


def test_latency_overflow_is_deterministic_and_visible(tmp_path):
    tracker = LatencyTracker(str(tmp_path / "latency.jsonl"), queue_limit=1)
    original_put = tracker._queue.put_nowait
    tracker._queue.put_nowait = lambda _item: (_ for _ in ()).throw(queue.Full())
    status = tracker.record(_latency())
    assert status["status"] == "dropped" and status["reason"] == "queue_full"
    assert tracker.snapshot()["dropped_records"] == 1
    tracker._queue.put_nowait = original_put
    tracker.close()


def test_latency_rotation_and_repeated_close_are_bounded(tmp_path):
    path = tmp_path / "latency.jsonl"
    tracker = LatencyTracker(str(path), queue_limit=8, max_bytes=180, max_rotated_files=2)
    for index in range(20):
        tracker.record(_latency(index))
    tracker.flush()
    tracker.close()
    tracker.close()
    files = sorted(tmp_path.glob("latency.jsonl*"))
    assert len(files) <= 3
    assert tracker.snapshot()["rotation_count"] > 0


def _application():
    root = Path(__file__).parents[1] / "content"
    return ScenarioApplication(
        InMemorySessionRepository(), FileScenarioRepository(root), MockProfileProvider(),
        RuleBasedIntentClassifier(), RecordingAudio(), RecordingLED(), StructuredEventPublisher(),
        now=lambda: 100.0,
    )


def test_cleanup_classifies_completed_cancelled_failed_and_is_idempotent():
    async def run():
        application = _application()
        completed = asyncio.create_task(asyncio.sleep(0, result="ok"))
        await asyncio.sleep(0)
        cancelled = asyncio.create_task(asyncio.sleep(30))

        async def fail():
            raise ValueError("completion failed")

        failed = asyncio.create_task(fail())
        await asyncio.sleep(0)
        application._audio_completions["s"] = {completed, cancelled, failed}
        report = await application._cancel_audio_completions("s", operation="reset")
        assert report["completed"] == 1
        assert report["cancelled"] == 1
        assert report["failed"] == 1
        assert report["remaining"] == 0
        repeated = await application._cancel_audio_completions("s", operation="reset")
        assert repeated["task_count"] == 0 and repeated["remaining"] == 0
        assert application.resource_snapshot()["audio_completion_registry"] == 0

    asyncio.run(run())


def test_cleanup_reports_timeout_then_releases_remaining_task():
    async def run():
        application = _application()
        release = asyncio.Event()

        async def stubborn():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(stubborn())
        application._audio_completions["timeout"] = {task}
        await asyncio.sleep(0)
        report = await application._cancel_audio_completions("timeout", operation="disconnect", timeout_s=0.01)
        assert report["timed_out"] == 1 and report["remaining"] == 1
        release.set()
        await asyncio.sleep(0)
        repeated = await application._cancel_audio_completions("timeout", operation="disconnect")
        assert repeated["completed"] == 1 and repeated["remaining"] == 0

    asyncio.run(run())


def test_failing_audio_update_hook_is_visible_without_breaking_completion():
    async def run():
        application = _application()
        sid = "hook-failure"
        session = SessionRecord(sid, "default", "corr-hook", state=Lifecycle.ACTIVE,
                                scenario_id="fixture_core", current_node="fixture_free")
        application.sessions.save(session)
        application._session_epochs[sid] = 1
        application._apply_audio_turn_result = lambda *args: {"state": "ACTIVE", "audio": "ok"}

        async def failing_hook(_session_id, _payload):
            raise RuntimeError("transport closed")

        application.audio_update_hook = failing_hook
        pending = asyncio.create_task(asyncio.sleep(0, result=SimpleNamespace()))
        await application._complete_audio_turn(sid, 1, SimpleNamespace(), pending, 1, "corr-hook")
        diagnostic = application.resource_snapshot()["audio_completion_diagnostics"][-1]
        assert diagnostic["event"] == "audio_update_hook_error"
        assert diagnostic["exception_type"] == "RuntimeError"
        assert application.sessions.get(sid).state == Lifecycle.ACTIVE

    asyncio.run(run())
