"""Private ASGI gateway for an explicitly synthetic remote Stand.

The canonical application still owns authorization and lifecycle admission.
This outer gate also protects HTML, assets, health and unknown routes.
"""
from __future__ import annotations

import base64
import binascii
from http.cookies import CookieError, SimpleCookie
import json
import re
import secrets
from urllib.parse import parse_qsl, urlsplit


def normalize_origin(value: str) -> str:
    """Only an explicit HTTP(S) origin, never a URL prefix or wildcard."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError
        port = parsed.port
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        suffix = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, TypeError):
        raise ValueError("remote_origin_invalid") from None


def validate_token(token: str) -> str:
    # Tokens are cookie-safe random secrets; do not accept short operator PINs.
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("remote_token_invalid")
    return token


class PrivateStandGateway:
    """Basic -> Strict cookie -> canonical header, with fail-closed CSRF checks.

    Cookies require a configured matching Origin for unsafe HTTP and every WS.
    Non-browser Basic clients may omit Origin; an explicit foreign Origin is
    always rejected for unsafe requests, even when Basic is valid.
    """

    def __init__(self, app, *, token: str, allowed_origins, secure_cookie: bool = True):
        self.app = app
        self._token = validate_token(token).encode("ascii")
        self.allowed_origins = frozenset(normalize_origin(origin) for origin in allowed_origins)
        if not self.allowed_origins:
            raise ValueError("remote_origin_required")
        self.secure_cookie = bool(secure_cookie)

    def _authenticate(self, headers):
        authorizations = [value for key, value in headers if key.lower() == b"authorization"]
        if authorizations:
            # Malformed/duplicate Basic never falls back to a valid cookie.
            if len(authorizations) != 1:
                return None
            try:
                scheme, value = authorizations[0].split(b" ", 1)
                if scheme.lower() != b"basic" or len(value) > 1024:
                    return None
                decoded = base64.b64decode(value, validate=True)
                username, password = decoded.split(b":", 1)
            except (ValueError, binascii.Error):
                return None
            username_ok = secrets.compare_digest(username, b"mishka")
            password_ok = secrets.compare_digest(password, self._token)
            return "basic" if username_ok & password_ok else None
        cookies = [value for key, value in headers if key.lower() == b"cookie"]
        try:
            raw = b"; ".join(cookies).decode("latin-1")
            # SimpleCookie otherwise silently accepts the last duplicate.
            if sum(part.strip().split("=", 1)[0] == "stand_token" for part in raw.split(";")) != 1:
                return None
            cookie = SimpleCookie()
            cookie.load(raw)
            supplied = cookie.get("stand_token")
            value = supplied.value.encode("utf-8") if supplied is not None else b""
        except (CookieError, UnicodeError, ValueError):
            return None
        return "cookie" if secrets.compare_digest(value, self._token) else None

    async def _deny(self, scope, send, *, forbidden=False):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "auth_failure"})
            return
        body = json.dumps({"error": {"code": "origin_rejected" if forbidden else "auth_failure",
                                     "message": "stand authentication required"}}, separators=(",", ":")).encode()
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                   (b"content-length", str(len(body)).encode())]
        if not forbidden:
            headers.append((b"www-authenticate", b'Basic realm="MISHKA private stand", charset="UTF-8"'))
        await send({"type": "http.response.start", "status": 403 if forbidden else 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        # Auth credentials in URLs are neither accepted nor forwarded. The
        # launcher disables access logs so even a rejected query is not logged.
        try:
            query_auth = any(key.lower() in {"token", "access_token", "stand_token"}
                             for key, _ in parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        except ValueError:
            query_auth = True
        mode = None if query_auth else self._authenticate(headers)
        if mode is None:
            return await self._deny(scope, send)
        unsafe = scope["type"] == "websocket" or scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"}
        if unsafe:
            origins = [value for key, value in headers if key.lower() == b"origin"]
            try:
                origin = normalize_origin(origins[0].decode("ascii")) if len(origins) == 1 else None
            except (UnicodeError, ValueError):
                origin = None
            if ((mode == "cookie" and origin not in self.allowed_origins)
                    or (origins and (len(origins) != 1 or origin not in self.allowed_origins))):
                return await self._deny(scope, send, forbidden=True)
        internal = dict(scope)
        internal["headers"] = [(key, value) for key, value in headers if key.lower() != b"x-stand-token"]
        internal["headers"].append((b"x-stand-token", self._token))

        async def private_send(message):
            if message["type"] in {"http.response.start", "websocket.accept"}:
                message = dict(message)
                outgoing = list(message.get("headers", []))
                outgoing.extend([(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                                 (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY")])
                if mode == "basic":
                    secure = self.secure_cookie or scope.get("scheme") in {"https", "wss"}
                    value = b"stand_token=" + self._token + b"; Path=/; HttpOnly; SameSite=Strict"
                    if secure:
                        value += b"; Secure"
                    outgoing.append((b"set-cookie", value))
                message["headers"] = outgoing
            await send(message)

        await self.app(internal, receive, private_send)
