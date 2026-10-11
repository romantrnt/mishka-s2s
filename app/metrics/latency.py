import json
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class TurnLatency:
    """Metrics for one child utterance and Mishka response."""

    vad_ms: float = 0.0
    stt_ms: float = 0.0
    llm_ttft_ms: float | None = None
    llm_prompt_eval_count: int | None = None
    llm_prompt_eval_ms: float | None = None
    llm_eval_count: int | None = None
    llm_eval_ms: float | None = None
    llm_load_ms: float | None = None
    tts_ttfa_ms: float | None = None
    total_ms: float | None = None
    timestamp: float = field(default_factory=time.time)
    start_time: float = field(default_factory=time.perf_counter, repr=False)


VOICE_BOUNDARIES = (
    "ingress", "vad_decision", "stt_result", "llm_request",
    "llm_first_token", "tts_request", "first_audio_chunk",
    "proxy_receive", "proxy_send", "playback_start", "turn_complete",
)


@dataclass
class VoiceTurnTrace:
    """Monotonic, one-turn trace shared by foundation runtime metrics."""

    correlation_id: str
    created_unix: float = field(default_factory=time.time)
    generation_id: object | None = field(default=None, repr=False)
    timestamps: dict[str, float] = field(default_factory=dict)
    stage_durations_ms: dict[str, float] = field(default_factory=dict)
    outcome: str = "pending"

    def mark(self, boundary: str, at: float | None = None) -> None:
        if boundary not in VOICE_BOUNDARIES:
            raise ValueError(f"unknown voice latency boundary: {boundary}")
        self.timestamps.setdefault(boundary, time.monotonic() if at is None else at)

    def observe_stage(self, stage: str, started_at: float, success: bool = True) -> None:
        elapsed = (time.monotonic() - started_at) * 1000
        self.stage_durations_ms[stage] = round(max(0.0, elapsed), 2)

    def report(self) -> dict:
        timestamps = dict(self.timestamps)
        pairs = {
            "ingress_to_vad_decision_ms": ("ingress", "vad_decision"),
            "vad_to_stt_result_ms": ("vad_decision", "stt_result"),
            "stt_to_llm_request_ms": ("stt_result", "llm_request"),
            "llm_to_first_token_ms": ("llm_request", "llm_first_token"),
            "first_token_to_tts_request_ms": ("llm_first_token", "tts_request"),
            "llm_to_tts_request_ms": ("llm_request", "tts_request"),
            "tts_to_first_audio_ms": ("tts_request", "first_audio_chunk"),
            "first_audio_to_playback_ms": ("first_audio_chunk", "playback_start"),
            "playback_to_proxy_send_ms": ("playback_start", "proxy_send"),
            "ingress_to_proxy_send_ms": ("ingress", "proxy_send"),
            "ingress_to_turn_complete_ms": ("ingress", "turn_complete"),
        }
        durations = {}
        for name, (start, end) in pairs.items():
            if start not in timestamps or end not in timestamps:
                durations[name] = None
                continue
            delta = (timestamps[end] - timestamps[start]) * 1000
            if delta < -0.001:
                raise ValueError(f"negative latency interval: {name}")
            durations[name] = round(max(0.0, delta), 2)
        return {"correlation_id": self.correlation_id, "created_unix": self.created_unix,
                "outcome": self.outcome, "timestamps_monotonic": timestamps,
                "durations_ms": durations, "stage_durations_ms": dict(self.stage_durations_ms)}


class LatencyWriterError(RuntimeError):
    """Raised only by explicit lifecycle calls when the writer failed."""


class LatencyTracker:
    """Non-blocking, bounded JSONL latency recorder.

    ``record`` only serializes and enqueues a report. Exactly one resident
    worker owns the file descriptor, preserving order and preventing concurrent
    append corruption. Overflow deterministically drops the newest record and
    increments a visible counter; latency logging remains best-effort for the
    audio pipeline while writer failures remain visible in snapshots.
    """

    def __init__(self, log_path: str = "logs/latency.jsonl", *, queue_limit: int = 128,
                 max_bytes: int = 4 * 1024 * 1024, max_rotated_files: int = 3,
                 status_limit: int = 256, writer_factory=None):
        if queue_limit <= 0 or max_bytes <= 0 or max_rotated_files < 0 or status_limit <= 0:
            raise ValueError("latency writer limits must be positive")
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_limit = int(queue_limit)
        self.max_bytes = int(max_bytes)
        self.max_rotated_files = int(max_rotated_files)
        self._queue: queue.Queue = queue.Queue(maxsize=self.queue_limit)
        self._statuses: deque[dict] = deque(maxlen=status_limit)
        self._diagnostics: deque[dict] = deque(maxlen=status_limit)
        self._lock = threading.RLock()
        self._accepting = True
        self._closed = False
        self._next_id = 0
        self._written_records = 0
        self._dropped_records = 0
        self._failed_records = 0
        self._evicted_records = 0
        self._rotation_count = 0
        self._last_error: dict | None = None
        self._writer_factory = writer_factory or threading.Thread
        self._thread = self._writer_factory(target=self._writer_loop, name="mishka-latency-writer", daemon=True)
        self._thread.start()

    def record(self, turn: TurnLatency) -> dict:
        """Queue one report without performing file I/O on the caller thread."""
        print(
            f"[LATENCY] vad={turn.vad_ms:.1f}ms stt={turn.stt_ms:.1f}ms "
            f"llm_ttft={self._fmt(turn.llm_ttft_ms)} "
            f"llm_prompt_eval={turn.llm_prompt_eval_count}tok/{self._fmt(turn.llm_prompt_eval_ms)} "
            f"tts_ttfa={self._fmt(turn.tts_ttfa_ms)} total={self._fmt(turn.total_ms)}",
            flush=True,
        )
        data = asdict(turn)
        data.pop("start_time", None)
        with self._lock:
            if not self._accepting:
                return self._failed_submission_locked("tracker_closed", "record called after close")
            self._next_id += 1
            record_id = self._next_id
            data["record_id"] = record_id
            try:
                payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            except (TypeError, ValueError) as exc:
                return self._failed_submission_locked(type(exc).__name__, str(exc))
            item = {"record_id": record_id, "data": data, "payload": payload}
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._dropped_records += 1
                status = {"record_id": record_id, "status": "dropped", "reason": "queue_full",
                          "queue_size": self._queue.qsize(), "at_unix": time.time()}
                self._statuses.append(status)
                self._diagnostics.append(dict(status))
                return status
            status = {"record_id": record_id, "status": "queued", "queue_size": self._queue.qsize()}
            self._statuses.append(status)
            return status

    def flush(self, timeout: float | None = None, *, raise_on_error: bool = False) -> dict:
        """Wait until all accepted queue items have reached a terminal state."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("latency writer flush timed out")
            time.sleep(0.001)
        if raise_on_error and self._last_error is not None:
            raise LatencyWriterError(self._last_error["message"])
        return self.snapshot()

    def close(self, timeout: float | None = None) -> dict:
        """Idempotently stop the worker after draining accepted records."""
        with self._lock:
            if self._closed:
                return self.snapshot()
            self._accepting = False
        self.flush(timeout=timeout)
        self._queue.put(None)
        join_timeout = None if timeout is None else max(0.0, float(timeout))
        self._thread.join(join_timeout)
        if self._thread.is_alive():
            raise TimeoutError("latency writer shutdown timed out")
        with self._lock:
            self._closed = True
        return self.snapshot()

    shutdown = close

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "queue_size": self._queue.qsize(),
                "queue_limit": self.queue_limit,
                "unfinished_records": self._queue.unfinished_tasks,
                "written_records": self._written_records,
                "dropped_records": self._dropped_records,
                "failed_records": self._failed_records,
                "evicted_records": self._evicted_records,
                "rotation_count": self._rotation_count,
                "retention": {"max_bytes": self.max_bytes, "max_rotated_files": self.max_rotated_files},
                "writer": {"alive": self._thread.is_alive(), "accepting": self._accepting, "closed": self._closed},
                "last_error": dict(self._last_error) if self._last_error else None,
                "recent_statuses": list(self._statuses),
                "diagnostics": list(self._diagnostics),
            }

    def _failed_submission(self, error_type: str, message: str) -> dict:
        with self._lock:
            self._next_id += 1
            return self._failed_submission_locked(error_type, message)

    def _failed_submission_locked(self, error_type: str, message: str) -> dict:
        self._failed_records += 1
        status = {"record_id": self._next_id, "status": "failed", "reason": error_type,
                  "message": message[:240], "at_unix": time.time()}
        self._statuses.append(status)
        self._diagnostics.append(dict(status))
        return status

    def _set_status(self, record_id: int, status: str, **fields) -> None:
        entry = {"record_id": record_id, "status": status, **fields}
        self._statuses.append(entry)

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            record_id = item["record_id"]
            self._set_status(record_id, "writing")
            try:
                self._rotate_if_needed(len(item["payload"]))
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(item["payload"].decode("utf-8"))
                    handle.flush()
                with self._lock:
                    self._written_records += 1
                self._set_status(record_id, "written")
            except Exception as exc:
                error = {"record_id": record_id, "status": "failed", "reason": "writer_exception",
                         "exception_type": type(exc).__name__, "message": str(exc)[:240],
                         "at_unix": time.time()}
                with self._lock:
                    self._failed_records += 1
                    self._last_error = dict(error)
                    self._diagnostics.append(dict(error))
                self._statuses.append(error)
            finally:
                self._queue.task_done()

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.log_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size == 0 or current_size + incoming_bytes <= self.max_bytes:
            return
        if self.max_rotated_files == 0:
            self.log_path.unlink(missing_ok=True)
            return
        for index in range(self.max_rotated_files, 0, -1):
            source = self.log_path.with_name(f"{self.log_path.name}.{index}")
            if index == self.max_rotated_files:
                source.unlink(missing_ok=True)
            else:
                source.replace(self.log_path.with_name(f"{self.log_path.name}.{index + 1}")) if source.exists() else None
        if self.log_path.exists():
            self.log_path.replace(self.log_path.with_name(f"{self.log_path.name}.1"))
        with self._lock:
            self._rotation_count += 1

    @staticmethod
    def _fmt(value: float | None) -> str:
        return f"{value:.1f}ms" if value is not None else "n/a"
