import copy
import logging
import os
import sys
import asyncio
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PORT = 8000
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

class StandAccessLogRedactionFilter(logging.Filter):
    """Redact auth query values before Uvicorn formats access records."""

    _AUTH_QUERY_KEYS = frozenset({"token", "access_token", "stand_token"})

    @classmethod
    def _redact_text(cls, value):
        if not isinstance(value, str) or "?" not in value:
            return value
        # Uvicorn formats an access request as a request-line rather than a
        # bare URL in some HTTP/WebSocket paths. Redact the value before any
        # formatter sees that request-line.
        pattern = re.compile(r"([?&](?:token|access_token|stand_token)=)[^&\s]+", re.IGNORECASE)
        value = pattern.sub(r"\1<redacted>", value)
        parts = urlsplit(value)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not any(key.lower() in cls._AUTH_QUERY_KEYS for key, _ in pairs):
            return value
        sanitized = [
            (key, "<redacted>" if key.lower() in cls._AUTH_QUERY_KEYS else item)
            for key, item in pairs
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized), parts.fragment))

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._redact_text(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(self._redact_text(value) for value in args)
        elif isinstance(args, list):
            record.args = [self._redact_text(value) for value in args]
        elif isinstance(args, dict):
            record.args = {key: self._redact_text(value) for key, value in args.items()}
        return True

def _load_runtime_env():
    env_file = PROJECT_ROOT / ".env.runpod"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_runtime_env()

def resolve_port() -> int:
    """Resolve the one production/public port without hiding misconfiguration."""
    raw = os.getenv("MISHKA_PORT", str(CANONICAL_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"MISHKA_PORT must be an integer, got {raw!r}") from exc
    if port != CANONICAL_PORT and os.getenv("MISHKA_ALLOW_NONCANONICAL_PORT") != "1":
        raise RuntimeError(
            f"canonical stand launch only supports port {CANONICAL_PORT}; "
            "set MISHKA_ALLOW_NONCANONICAL_PORT=1 only for an explicit local/test run"
        )
    return port

from app.foundation.stand import create_stand_app


if __name__ == "__main__":
    import uvicorn

    application = create_stand_app()
    host = os.getenv("MISHKA_HOST", "0.0.0.0")
    port = resolve_port()
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config.setdefault("filters", {})["stand_access_redaction"] = {"()": StandAccessLogRedactionFilter}
    log_config["handlers"]["access"].setdefault("filters", []).append("stand_access_redaction")
    # Uvicorn's WebSocket handshake access lines are emitted through the
    # uvicorn.error logger, while HTTP access lines use uvicorn.access.
    log_config["handlers"]["default"].setdefault("filters", []).append("stand_access_redaction")
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_level=os.getenv("MISHKA_LOG_LEVEL", "info"),
        log_config=log_config,
    )
