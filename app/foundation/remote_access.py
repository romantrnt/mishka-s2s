"""Private browser sessions around the canonical Stand authorization boundary."""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
import html
import json
import math
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit


_LOGIN_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#ffffff">
<title>Телефон Мишки — вход</title>
<style>
  html,body { margin:0; padding:0; background:#fff; color:#000; font-family:Arial,sans-serif; }
  body { display:flex; min-height:100dvh; align-items:center; justify-content:center; padding:24px; box-sizing:border-box; }
  form { width:100%; max-width:320px; display:flex; flex-direction:column; gap:12px; }
  h1 { margin:0 0 24px; font-size:32px; line-height:1.05; font-weight:800; letter-spacing:-1px; }
  label { font-size:14px; }
  input,button { width:100%; box-sizing:border-box; height:48px; padding:0 12px; font-size:16px; border-radius:0; -webkit-appearance:none; appearance:none; }
  input { background:#fff; color:#000; border:1px solid #000; }
  button { background:#000; color:#fff; border:1px solid #000; font-weight:700; cursor:pointer; margin-top:8px; touch-action:manipulation; }
  button:hover { background:#303030; }
  :focus-visible { outline:3px solid #bd2921; outline-offset:3px; }
  .err { font-size:14px; color:#a51e17; }
</style>
</head>
<body>
<form method="post" action="/stand/login" autocomplete="on">
  <h1>Телефон Мишки</h1>
  __ERROR__
  <input name="return_to" type="hidden" value="__RETURN_TO__">
  <label for="username">Логин</label>
  <input id="username" name="username" type="text" placeholder="login" autocomplete="username" autocapitalize="none" spellcheck="false" required>
  <label for="password">Пароль</label>
  <input id="password" name="password" type="password" placeholder="pass" autocomplete="current-password" required>
  <button type="submit">enter</button>
</form>
</body>
</html>"""


def normalize_origin(value: str) -> str:
    """Only an explicit HTTP(S) origin, never a URL prefix or wildcard."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or "*" in parsed.hostname or any(c.isspace() for c in value)):
            raise ValueError
        port = parsed.port
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        suffix = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, TypeError):
        raise ValueError("remote_origin_invalid") from None


def validate_token(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("remote_token_invalid")
    return token


def _return_path(value: str) -> str:
    return value if value in {"/console", "/console/operator"} else "/console"


@dataclass
class _BrowserSession:
    expires_at: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class PrivateStandGateway:
    """Opaque RAM browser sessions, Basic, and opt-in canonical API headers.

    Every browser mutation/WS requires an explicit configured Origin. Machine
    clients may omit Origin; any supplied foreign Origin still fails closed.
    """

    def __init__(self, app, *, token: str, allowed_origins, secure_cookie: bool = True,
                 allow_api_token: bool = False, session_ttl_s: float = 3600,
                 max_sessions: int = 64):
        self.app = app
        # The physical launcher preserves its existing canonical credential
        # format; the synthetic remote launcher retains its stronger contract.
        if allow_api_token:
            if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 1024:
                raise ValueError("stand_token_required")
        else:
            validate_token(token)
        self._token = token.encode("utf-8")
        self.allowed_origins = frozenset(normalize_origin(origin) for origin in allowed_origins)
        if not self.allowed_origins:
            raise ValueError("remote_origin_required")
        if (isinstance(session_ttl_s, bool) or not isinstance(session_ttl_s, (int, float))
                or not math.isfinite(session_ttl_s) or not 1 <= session_ttl_s <= 86400):
            raise ValueError("browser_session_ttl_invalid")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 256:
            raise ValueError("browser_session_limit_invalid")
        self.secure_cookie = bool(secure_cookie)
        self.allow_api_token = allow_api_token
        self.session_ttl_s = session_ttl_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, _BrowserSession] = {}
        self._clock = time.monotonic

    def _revoke(self, sid):
        session = self._sessions.pop(sid, None)
        if session is not None:
            session.revoked.set()

    def _session_valid(self, sid, session):
        if self._sessions.get(sid) is session and self._clock() < session.expires_at:
            return True
        self._revoke(sid)
        return False

    def _new_session(self):
        for sid, session in tuple(self._sessions.items()):
            if self._clock() >= session.expires_at:
                self._revoke(sid)
        while len(self._sessions) >= self.max_sessions:
            self._revoke(next(iter(self._sessions)))
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = _BrowserSession(self._clock() + self.session_ttl_s)
        return sid

    def _cookie_sid(self, headers):
        try:
            raw = b"; ".join(value for key, value in headers if key.lower() == b"cookie").decode("latin-1")
            if sum(part.strip().split("=", 1)[0] == "stand_session" for part in raw.split(";")) != 1:
                return None
            cookie = SimpleCookie()
            cookie.load(raw)
            supplied = cookie.get("stand_session")
            return supplied.value if supplied is not None else None
        except (CookieError, UnicodeError, ValueError):
            return None

    def _authenticate(self, headers):
        authorizations = [value for key, value in headers if key.lower() == b"authorization"]
        if authorizations:
            # Malformed/duplicate Basic never falls back to another credential.
            if len(authorizations) != 1:
                return None
            try:
                scheme, value = authorizations[0].split(b" ", 1)
                if scheme.lower() != b"basic" or len(value) > 2048:
                    return None
                username, password = base64.b64decode(value, validate=True).split(b":", 1)
            except (ValueError, binascii.Error):
                return None
            valid = secrets.compare_digest(username, b"mishka") & secrets.compare_digest(password, self._token)
            return ("basic", None) if valid else None
        tokens = [value for key, value in headers if key.lower() == b"x-stand-token"]
        if self.allow_api_token and tokens:
            return ("api", None) if len(tokens) == 1 and secrets.compare_digest(tokens[0], self._token) else None
        sid = self._cookie_sid(headers)
        session = self._sessions.get(sid)
        if session is not None and self._session_valid(sid, session):
            return "cookie", sid
        return None

    def _cookie(self, scope, sid=""):
        value = f"stand_session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(self.session_ttl_s) if sid else 0}".encode("ascii")
        if self.secure_cookie or scope.get("scheme") in {"https", "wss"}:
            value += b"; Secure"
        return value

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

    async def _send_login_page(self, scope, send, *, status=200, error_message="", return_to=None):
        error = f'<div class="err" role="alert">{html.escape(error_message)}</div>' if error_message else ""
        target = _return_path(scope.get("path") if return_to is None else return_to)
        body = _LOGIN_HTML.replace("__ERROR__", error).replace("__RETURN_TO__", target).encode("utf-8")
        headers = [(b"content-type", b"text/html; charset=utf-8"),
                   (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"),
                   (b"referrer-policy", b"same-origin"), (b"x-content-type-options", b"nosniff"),
                   (b"x-frame-options", b"DENY")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})

    def _allowed_origin(self, headers):
        origins = [value for key, value in headers if key.lower() == b"origin"]
        try:
            return len(origins) == 1 and normalize_origin(origins[0].decode("ascii")) in self.allowed_origins
        except (UnicodeError, ValueError):
            return False

    async def _redirect(self, scope, send, target, sid=""):
        await send({"type": "http.response.start", "status": 303, "headers": [
            (b"location", target.encode("ascii")), (b"set-cookie", self._cookie(scope, sid)),
            (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
            (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async def _handle_login(self, scope, receive, send, headers):
        if not self._allowed_origin(headers):
            return await self._deny(scope, send, forbidden=True)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            body.extend(message.get("body", b""))
            if len(body) > 4096:
                return await self._send_login_page(scope, send, status=400, error_message="Слишком большой запрос.")
            if not message.get("more_body", False):
                break
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
            fields = dict(pairs)
            valid_fields = len(fields) == len(pairs)
        except (UnicodeDecodeError, ValueError):
            fields, valid_fields = {}, False
        user_ok = secrets.compare_digest(fields.get("username", "").encode("utf-8"), b"mishka")
        pass_ok = secrets.compare_digest(fields.get("password", "").encode("utf-8"), self._token)
        if not (valid_fields and user_ok & pass_ok):
            return await self._send_login_page(scope, send, status=401,
                error_message="Неверный логин или пароль. Попробуйте ещё раз.", return_to=fields.get("return_to"))
        self._revoke(self._cookie_sid(headers))
        return await self._redirect(scope, send, _return_path(fields.get("return_to")), self._new_session())

    async def _browser_websocket(self, internal, receive, send, sid, private_send):
        session = self._sessions[sid]

        async def guarded_receive():
            message = await receive()
            if not self._session_valid(sid, session):
                return {"type": "websocket.disconnect", "code": 1008}
            return message

        application = asyncio.create_task(self.app(internal, guarded_receive, private_send))
        revoked = asyncio.create_task(session.revoked.wait())
        try:
            done, _ = await asyncio.wait({application, revoked},
                timeout=max(0, session.expires_at - self._clock()), return_when=asyncio.FIRST_COMPLETED)
            if application in done and self._session_valid(sid, session):
                return await application
            self._revoke(sid)
            await send({"type": "websocket.close", "code": 1008, "reason": "session_expired"})
        finally:
            revoked.cancel()
            application.cancel()
            await asyncio.gather(application, revoked, return_exceptions=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        # Never allow credentials in URLs, including the login/logout endpoints.
        query_auth = any(key.lower() in {"token", "access_token", "stand_token", "stand_session"}
                         for key, _ in parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        if query_auth:
            return await self._deny(scope, send)
        if scope["type"] == "http" and scope.get("path") in {"/stand/login", "/stand/logout"}:
            if scope.get("path") == "/stand/login" and scope.get("method") in {"GET", "HEAD"}:
                return await self._send_login_page(scope, send)
            if scope.get("method") != "POST":
                await send({"type": "http.response.start", "status": 405,
                            "headers": [(b"allow", b"POST"), (b"content-length", b"0")]})
                return await send({"type": "http.response.body", "body": b""})
            if scope.get("path") == "/stand/login":
                return await self._handle_login(scope, receive, send, headers)
            if not self._allowed_origin(headers):
                return await self._deny(scope, send, forbidden=True)
            self._revoke(self._cookie_sid(headers))
            return await self._redirect(scope, send, "/console")
        auth = self._authenticate(headers)
        if auth is None:
            has_auth = any(key.lower() in {b"authorization", b"x-stand-token"} for key, _ in headers)
            accepts_html = any(key.lower() == b"accept" and b"text/html" in value.lower() for key, value in headers)
            if (scope["type"] == "http" and not has_auth and scope.get("method", "GET") in {"GET", "HEAD"}
                    and scope.get("path") in {"/", "/console", "/console/operator", "/simulator"} and accepts_html):
                return await self._send_login_page(scope, send)
            return await self._deny(scope, send)
        mode, sid = auth
        unsafe = scope["type"] == "websocket" or scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"}
        if unsafe and (mode == "cookie" or any(key.lower() == b"origin" for key, _ in headers)):
            if not self._allowed_origin(headers):
                return await self._deny(scope, send, forbidden=True)
        internal = dict(scope)
        internal["headers"] = [(key, value) for key, value in headers if key.lower() != b"x-stand-token"]
        internal["headers"].append((b"x-stand-token", self._token))
        browser_session = self._sessions.get(sid)

        async def private_send(message):
            if mode == "cookie" and scope["type"] == "websocket" and not self._session_valid(sid, browser_session):
                raise asyncio.CancelledError
            if message["type"] in {"http.response.start", "websocket.accept"}:
                message = dict(message)
                outgoing = list(message.get("headers", []))
                console_document = (scope["type"] == "http"
                    and scope.get("path") in {"/console", "/console/operator"}
                    and any(key.lower() == b"content-type" and value.split(b";", 1)[0].strip().lower() == b"text/html"
                            for key, value in outgoing))
                # no-referrer makes native form POST Origin null; keep strict Origin admission.
                policy = b"same-origin" if console_document else b"no-referrer"
                outgoing = [(key, value) for key, value in outgoing if key.lower() != b"referrer-policy"]
                outgoing.extend([(b"cache-control", b"no-store"), (b"referrer-policy", policy),
                                 (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY")])
                if mode == "basic":
                    old_sid = self._cookie_sid(headers)
                    old_session = self._sessions.get(old_sid)
                    if old_session is None or not self._session_valid(old_sid, old_session):
                        outgoing.append((b"set-cookie", self._cookie(scope, self._new_session())))
                message["headers"] = outgoing
            await send(message)

        if mode == "cookie" and scope["type"] == "websocket":
            return await self._browser_websocket(internal, receive, send, sid, private_send)
        await self.app(internal, receive, private_send)


def build_browser_gateway(app, settings, *, port: int, environ=None):
    """Protect the real launcher without changing its Stand or Max lifecycle."""
    if environ is None:
        import os
        environ = os.environ
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("browser_port_invalid")
    if settings.allow_query_token or settings.mutation_auth_policy != "token_required":
        raise ValueError("browser_requires_header_auth")
    raw = environ.get("MISHKA_BROWSER_ORIGINS")
    origins = raw.split(",") if raw is not None else [
        f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"]
    origins = [normalize_origin(value.strip()) for value in origins]
    try:
        ttl = float(environ.get("MISHKA_BROWSER_SESSION_TTL_S", "3600"))
    except (TypeError, ValueError):
        raise ValueError("browser_session_ttl_invalid") from None
    return PrivateStandGateway(app, token=settings.stand_access_token, allowed_origins=origins,
        secure_cookie=any(origin.startswith("https://") for origin in origins),
        allow_api_token=True, session_ttl_s=ttl)
