from __future__ import annotations

import json
import logging
from pathlib import Path
import resource
from collections import Counter, deque
import os
from typing import Any

from .events import redacted_event

logger = logging.getLogger("mishka.observability")


class Metrics:
    def __init__(self, *, latency_limit: int | None = None, event_limit: int | None = None, watchdog_limit: int | None = None, settings: Any = None):
        self.latency_limit = int(latency_limit or getattr(settings, "observability_latency_limit", 500))
        self.event_limit = int(event_limit or getattr(settings, "observability_event_limit", 500))
        self.watchdog_limit = int(watchdog_limit or getattr(settings, "observability_watchdog_limit", 200))
        if min(self.latency_limit, self.event_limit, self.watchdog_limit) <= 0:
            raise ValueError("observability limits must be positive")
        self.counters: Counter[str] = Counter()
        self.latencies = deque(maxlen=self.latency_limit)
        self.latency_evicted = 0
        self.active_sessions = self.queue_depth = self.queue_high_water_mark = 0
        self.active_tasks = self.pcm_bytes = self.cancelled_tasks = 0
        self.dropped_chunks = self.cancelled_chunks = self.oom_count = 0
        self.queued_bytes = self.active_audio_bytes = 0
        self.completed_chunks = self.failed_chunks = 0
        self.watchdog_events = deque(maxlen=self.watchdog_limit)
        self.observation_errors = deque(maxlen=self.event_limit)
    def inc(self, name, amount=1): self.counters[name] += amount
    def observe(self, ms):
        if len(self.latencies) == self.latencies.maxlen:
            self.latency_evicted += 1
        self.latencies.append(float(ms))
    @staticmethod
    def metric(value, reason=None): return {"value": value, **({"reason": reason} if reason else {})}
    def memory_snapshot(self):
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            rss_current = self.metric(pages * page_size)
        except (AttributeError, OSError, ValueError, IndexError):
            rss_current = self.metric(None, "proc_rss_unavailable")
        try:
            rss_peak = self.metric(int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024)
        except (AttributeError, OSError, ValueError):
            rss_peak = self.metric(None, "resource_peak_unavailable")
        try:
            current = Path("/sys/fs/cgroup/memory.current").read_text().strip(); limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
            cgroup = {"current_bytes": self.metric(int(current)), "limit_bytes": self.metric(None, "unlimited") if limit == "max" else self.metric(int(limit))}
        except (OSError, ValueError): cgroup = {"current_bytes": self.metric(None, "cgroup_unavailable"), "limit_bytes": self.metric(None, "cgroup_unavailable")}
        try:
            import torch
            gpu = ({"allocated_bytes": self.metric(torch.cuda.memory_allocated()), "reserved_bytes": self.metric(torch.cuda.memory_reserved()), "peak_bytes": self.metric(torch.cuda.max_memory_allocated())} if torch.cuda.is_available() else {key: self.metric(None, "cuda_unavailable") for key in ("allocated_bytes", "reserved_bytes", "peak_bytes")})
        except (ImportError, AttributeError, RuntimeError): gpu = {key: self.metric(None, "cuda_unavailable") for key in ("allocated_bytes", "reserved_bytes", "peak_bytes")}
        ollama = {"rss_bytes": self.metric(None, "ollama_unavailable")}
        try:
            import psutil
            for proc in psutil.process_iter(["name", "cmdline", "memory_info"]):
                name = " ".join(proc.info.get("cmdline") or []) or proc.info.get("name") or ""
                if "ollama" in name.lower(): ollama = {"rss_bytes": self.metric(proc.info["memory_info"].rss), "pid": proc.pid}; break
        except Exception as exc:
            self.observation_errors.append({"source": "ollama_rss", "error": type(exc).__name__})
            logger.warning("optional Ollama RSS probe failed; continuing without it: %s", exc)
        return {"rss_bytes": rss_current, "rss_current_bytes": rss_current, "rss_peak_bytes": rss_peak, "cgroup": cgroup, "gpu": gpu, "ollama": ollama}
    def observe_resources(self, resources):
        self.active_sessions = int(resources.get("active_sessions", self.active_sessions)); self.active_tasks = int(resources.get("active_tasks", self.active_tasks))
        self.queue_depth = int(resources.get("queued_chunks", self.queue_depth)); self.queue_high_water_mark = max(self.queue_high_water_mark, self.queue_depth)
        self.queued_bytes = max(0, int(resources.get("queued_bytes", self.queued_bytes)))
        self.active_audio_bytes = max(0, int(resources.get("active_audio_bytes", resources.get("pcm_bytes", self.active_audio_bytes))))
        self.pcm_bytes = self.active_audio_bytes
        self.cancelled_chunks = max(self.cancelled_chunks, int(resources.get("cancelled_chunks", self.cancelled_chunks)))
        self.completed_chunks = max(self.completed_chunks, int(resources.get("completed_chunks", self.completed_chunks)))
        self.failed_chunks = max(self.failed_chunks, int(resources.get("failed_chunks", self.failed_chunks)))
    def record_watchdog_event(self, event):
        """Keep bounded, redacted watchdog diagnostics for /health."""
        self.watchdog_events.append(dict(event))
        if len(self.watchdog_events) > 200:
            del self.watchdog_events[:-200]

    def watchdog_check(self, now, tasks=None, playback=None, max_age_s=30.0, queue_generations=None, *, record=True):
        stale_tasks = [k for k, started in (tasks or {}).items() if now - started > max_age_s]
        stale_playback = [k for k, started in (playback or {}).items() if now - started > max_age_s]
        stale_generations = [k for k, pair in (queue_generations or {}).items() if pair[0] != pair[1]]
        event = {"at_monotonic": now, "stale_tasks": stale_tasks, "stale_playback": stale_playback, "stale_queue_generations": stale_generations, "degraded": bool(stale_tasks or stale_playback or stale_generations)}
        if event["degraded"] and record: self.record_watchdog_event(event)
        return event
    def snapshot(self):
        values = list(self.latencies)
        return {"counters": dict(self.counters), "active_sessions": self.active_sessions, "latency_buckets_ms": {"<100":sum(x<100 for x in values), "100-1000":sum(100<=x<1000 for x in values), ">=1000":sum(x>=1000 for x in values)}, "latency_history":{"limit":self.latency_limit,"current_sample_count":len(values),"evicted_sample_count":self.latency_evicted}, "memory": self.memory_snapshot(), "resources":{"queue_depth":self.queue_depth,"queue_high_water_mark":self.queue_high_water_mark,"active_tasks":self.active_tasks,"queued_bytes":self.queued_bytes,"active_audio_bytes":self.active_audio_bytes,"pcm_bytes":self.pcm_bytes,"cancelled_tasks":self.cancelled_tasks,"dropped_chunks":self.dropped_chunks,"cancelled_chunks":self.cancelled_chunks,"completed_chunks":self.completed_chunks,"failed_chunks":self.failed_chunks,"oom_count":self.oom_count}, "watchdog":{"events":list(self.watchdog_events),"limit":self.watchdog_limit}, "observability_errors": list(self.observation_errors)}

class StructuredEventPublisher:
    def __init__(self, logger=None): self.events = []; self.logger = logger or logging.getLogger("mishka.events")
    def publish(self, event):
        data = redacted_event(event); self.events.append(data); self.logger.info(json.dumps(data, ensure_ascii=False))
    def clear(self): self.events.clear()
