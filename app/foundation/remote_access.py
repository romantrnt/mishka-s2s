"""Private ASGI gateway for an explicitly synthetic remote Stand.

The canonical application still owns authorization and lifecycle admission.
This outer gate also protects HTML, assets, health and unknown routes.
"""
from __future__ import annotations

import base64
import binascii
from http.cookies import CookieError, SimpleCookie
import html
import json
import re
import secrets
from urllib.parse import parse_qsl, urlsplit


_LOGIN_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Мишка — вход на стенд</title>
<style>
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#f2f4f3; color:#182220; display:flex; min-height:100dvh; align-items:center; justify-content:center; padding:16px; box-sizing:border-box; }
  .card { width:100%; max-width:360px; background:#fff; border-radius:16px; padding:24px; box-shadow:0 8px 28px rgba(0,0,0,.08); }
  h1 { margin:0 0 8px; font-size:20px; }
  p { margin:0 0 18px; font-size:14px; color:#52615d; }
  label { display:block; font-size:13px; font-weight:600; margin-bottom:6px; }
  input { width:100%; box-sizing:border-box; padding:12px; border:1px solid #cfd8d5; border-radius:10px; font-size:16px; margin-bottom:14px; }
  button { width:100%; padding:13px; border:0; border-radius:10px; background:#147d73; color:#fff; font-size:16px; font-weight:600; cursor:pointer; }
  .err { background:#fdecec; color:#9e2222; padding:10px 12px; border-radius:10px; font-size:14px; margin-bottom:14px; }
</style>
</head>
<body>
<main class="card">
  <h1>Мягкий телефон Мишки</h1>
  <p>Введите логин и токен доступа для входа на стенд.</p>
  __ERROR__
  <form method="post" action="/stand/login" autocomplete="on">
    <label for="u">Логин</label>
    <input id="u" name="username" value="mishka" autocomplete="username" required>
    <label for="p">Токен доступа</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required autofocus>
    <button type="submit">Войти</button>
  </form>
</main>
</body>
</html>"""


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

    async def _send_login_page(self, scope, send, *, status: int = 200, error_message: str = ""):
        err_html = f'<div class="err" role="alert">{html.escape(error_message)}</div>' if error_message else ""
        body = _LOGIN_HTML.replace("__ERROR__", err_html).encode("utf-8")
        headers = [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope.get("method") == "HEAD" else body})

    def _allowed_origin(self, headers) -> bool:
        origins = [value for key, value in headers if key.lower() == b"origin"]
        if len(origins) != 1:
            return False
        try:
            return normalize_origin(origins[0].decode("ascii")) in self.allowed_origins
        except (UnicodeError, ValueError):
            return False

    async def _handle_login(self, scope, receive, send, headers):
        if not self._allowed_origin(headers):
            return await self._deny(scope, send, forbidden=True)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > 4096:
                return await self._send_login_page(scope, send, status=400, error_message="Слишком большой запрос.")
            if not message.get("more_body", False):
                break
        try:
            fields = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        except UnicodeDecodeError:
            fields = {}
        user_ok = secrets.compare_digest(fields.get("username", "").encode("utf-8"), b"mishka")
        pass_ok = secrets.compare_digest(fields.get("password", "").encode("utf-8"), self._token)
        if not (user_ok & pass_ok):
            return await self._send_login_page(scope, send, status=401, error_message="Неверный логин или токен.")
        secure = self.secure_cookie or scope.get("scheme") in {"https", "wss"}
        cookie = b"stand_token=" + self._token + b"; Path=/; HttpOnly; SameSite=Strict"
        if secure:
            cookie += b"; Secure"
        headers_out = [
            (b"location", b"/console"),
            (b"set-cookie", cookie),
            (b"cache-control", b"no-store"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"content-length", b"0"),
        ]
        await send({"type": "http.response.start", "status": 303, "headers": headers_out})
        await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path") == "/stand/login":
            return await self._handle_login(scope, receive, send, headers)
        # Auth credentials in URLs are neither accepted nor forwarded. The
        # launcher disables access logs so even a rejected query is not logged.
        try:
            query_auth = any(key.lower() in {"token", "access_token", "stand_token"}
                             for key, _ in parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        except ValueError:
            query_auth = True
        mode = None if query_auth else self._authenticate(headers)
        if mode is None:
            has_auth_header = any(key.lower() == b"authorization" for key, _ in headers)
            accepts_html = any(key.lower() == b"accept" and b"text/html" in value.lower() for key, value in headers)
            if (scope["type"] == "http" and not query_auth and not has_auth_header
                    and scope.get("method", "GET") in {"GET", "HEAD"}
                    and scope.get("path") in {"/", "/console", "/simulator"}
                    and accepts_html):
                return await self._send_login_page(scope, send, status=200)
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
