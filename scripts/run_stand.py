import copy
import logging
import os
import sys
import asyncio
import re
import stat
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

def _resolve_env_file():
    """Resolve the declared env file without overriding process env values."""
    declared = os.getenv("MISHKA_ENV_FILE")
    if not declared:
        return PROJECT_ROOT / ".env"
    path = Path(declared).expanduser()
    if not path.is_absolute() and ".." in path.parts:
        raise RuntimeError("MISHKA_ENV_FILE path traversal is not allowed")
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _safe_config_source(env_file: Path) -> str:
    """Return a bounded provenance label, never a private absolute path."""
    try:
        return f"project:{env_file.relative_to(PROJECT_ROOT)}"
    except ValueError:
        return "external-env-file"


def _parse_env_file(env_file: Path) -> dict[str, str]:
    try:
        mode = stat.S_IMODE(env_file.stat().st_mode)
        raw_lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError("declared MISHKA_ENV_FILE does not exist") from exc
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("declared MISHKA_ENV_FILE is unreadable") from exc

    values: dict[str, str] = {}
    for line_number, raw in enumerate(raw_lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"env file is malformed at line {line_number}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise RuntimeError(f"env file has an invalid key at line {line_number}")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise RuntimeError(f"env file has an unterminated value at line {line_number}")
            value = value[1:-1]
        values[key] = value

    secret_key_pattern = re.compile(r"(?:^|_)(?:SECRET|PASSWORD|API_KEY|PRIVATE_KEY)$|(?:^|_)ACCESS_TOKEN$")
    contains_secret = any(secret_key_pattern.search(key.upper()) and value.strip() for key, value in values.items())
    if contains_secret and mode & 0o077:
        raise RuntimeError("env file containing secrets must not be group/world readable")
    return values


def _load_runtime_env():
    # Runtime secrets are injected by the process manager. A local .env is
    # optional; an explicitly declared MISHKA_ENV_FILE is mandatory. Process
    # environment wins by design, while malformed or unreadable declarations
    # fail closed before the application is constructed.
    declared = bool(os.getenv("MISHKA_ENV_FILE"))
    env_file = _resolve_env_file()
    if not env_file.is_file():
        if declared:
            raise RuntimeError("declared MISHKA_ENV_FILE does not exist")
        return
    values = _parse_env_file(env_file)
    os.environ.setdefault("MISHKA_CONFIG_SOURCE", _safe_config_source(env_file))
    for key, value in values.items():
        os.environ.setdefault(key, value)


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
