"""Local-only guard (OHL-H9). There's no login, so the app must only answer this computer.

`ohl serve` refuses to bind anywhere but loopback, but the app can also be started some
other way (`uvicorn ohl.app:app --host 0.0.0.0`). This middleware holds either way:

- Host header: only localhost or a loopback address. A web page that uses DNS rebinding
  reaches 127.0.0.1 under its own name, so its requests carry that name and get 400.
- Client address: only loopback. Anyone else on the network gets 403.
- Proxy headers (Via, Forwarded, X-Forwarded-*, X-Real-IP and the like): refused with 403.
  The app never sits behind a proxy, and a server told to trust them would take a remote
  client for this computer. A proxy that adds none of them can't be told apart, so the
  README says never to put the app behind one.
- Changes (POST and other unsafe methods) from another page: refused with 403 when the
  browser says so: Origin not this app, Sec-Fetch-Site anything but same-origin, or
  Sec-Fetch-Dest anything but a top-level document (a form inside a frame).
- Framing: every response forbids being shown in a frame (X-Frame-Options and CSP
  frame-ancestors), so another page can't trick the CSO into clicking inside the app.

A page on another port of this computer shares the cookie's host, so it gets the session
cookie and could read the CSRF token from it. For that page, the Origin, Sec-Fetch and
framing rules are what stop it; the token check in app.py stops the rest.
"""

from __future__ import annotations

import ipaddress

LOCAL_NAMES = {"localhost"}
PROXY_HEADERS = {b"via", b"forwarded", b"x-real-ip", b"true-client-ip", b"cf-connecting-ip",
                 b"x-client-ip", b"x-cluster-client-ip"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
NO_FRAMING = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'")]


def proxied(raw_headers) -> bool:
    return any(k in PROXY_HEADERS or k.startswith(b"x-forwarded-") for k, _ in raw_headers)


def _loopback_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1
    return (mapped or ip).is_loopback


def host_name(header: str) -> str:
    """The name part of a Host header: 'localhost:8000' -> 'localhost', '[::1]:8000' -> '::1'."""
    h = header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    name, sep, port = h.rpartition(":")
    if sep and port.isdigit() and ":" not in name:
        return name
    return h


def host_allowed(header: str) -> bool:
    name = host_name(header)
    return name in LOCAL_NAMES or _loopback_ip(name)


def client_allowed(client) -> bool:
    return bool(client) and _loopback_ip(str(client[0]))


def same_origin(origin: str, host: str) -> bool:
    """The browser's Origin ('http://127.0.0.1:8000') names the server the request went to."""
    scheme, sep, rest = origin.strip().lower().partition("://")
    return bool(sep) and scheme in ("http", "https") and rest == host.strip().lower()


def cross_site(headers: dict[bytes, str], host: str) -> bool:
    """True when the browser says a change didn't come from a top-level page of this app."""
    origin = headers.get(b"origin")
    if origin is not None and not same_origin(origin, host):
        return True  # includes "null", sent by sandboxed frames and some redirects
    if headers.get(b"sec-fetch-site", "same-origin").strip().lower() not in ("same-origin", "none"):
        return True
    return headers.get(b"sec-fetch-dest", "document").strip().lower() != "document"  # e.g. a form in a frame


class LocalOnly:
    """Pure ASGI middleware, so it runs before sessions, routing and static files."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        raw = scope.get("headers", [])
        hosts = [v.decode("latin-1") for k, v in raw if k == b"host"]
        if len(hosts) != 1 or not host_allowed(hosts[0]):
            return await _refuse(scope, receive, send, 400, "Open Hazard Log only answers to localhost or 127.0.0.1.")
        if not client_allowed(scope.get("client")) or proxied(raw):
            return await _refuse(scope, receive, send, 403, "Open Hazard Log only answers requests from this computer.")
        headers = {k: v.decode("latin-1") for k, v in raw}
        if scope["type"] == "websocket" or scope.get("method", "GET").upper() not in SAFE_METHODS:
            if cross_site(headers, hosts[0]):
                return await _refuse(scope, receive, send, 403,
                                     "Open Hazard Log only accepts changes made from its own pages.")

        async def send_unframeable(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in dict(NO_FRAMING)]
                message = {**message, "headers": kept + NO_FRAMING}
            await send(message)
        return await self.app(scope, receive, send_unframeable)


async def _refuse(scope, receive, send, status: int, text: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *NO_FRAMING]})
    await send({"type": "http.response.body", "body": body})
