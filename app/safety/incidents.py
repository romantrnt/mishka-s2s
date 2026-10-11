import json
import time
from pathlib import Path


class IncidentLogger:
    """
    Пишет сработавшие правила безопасности в отдельный лог
    (logs/safety_incidents.jsonl), не блокируя основной поток -
    по аналогии с LatencyTracker (см. app/metrics/latency.py).
    """

    def __init__(self, log_path: str = "logs/safety_incidents.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        session_id: str,
        direction: str,
        action: str,
        category: str | None,
        text: str,
    ) -> None:

        entry = {
            "time": time.time(),
            "session_id": session_id,
            "direction": direction,  # "input" или "output"
            "action": action,        # "block", "log_only" или "fallback"
            "category": category,
            "text": text,
        }

        print(
            f"[SAFETY] {direction} {action} category={category}",
            flush=True,
        )

        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
