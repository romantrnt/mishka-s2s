import json
import logging
import time
from pathlib import Path


logger = logging.getLogger("mishka.safety")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class IncidentLogger:
    """
    Пишет сработавшие правила безопасности в отдельный лог
    (logs/safety_incidents.jsonl), не блокируя основной поток -
    по аналогии с LatencyTracker (см. app/metrics/latency.py).
    """

    def __init__(self, log_path: str | Path = PROJECT_ROOT / "runtime/logs/safety_incidents.jsonl"):
        path = Path(log_path).expanduser()
        self.log_path = path if path.is_absolute() else PROJECT_ROOT / path
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

        logger.warning("safety incident direction=%s action=%s category=%s", direction, action, category)

        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
