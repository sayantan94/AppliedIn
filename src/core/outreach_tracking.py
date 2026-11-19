"""Optional isolated 1x1 pixel intake; never mounted in the dashboard.

Operator-owned stable HTTPS and sanitized ingress/proxy/tunnel logs are required
for activation. Otherwise mail remains token-free and not_tracked.
"""
from __future__ import annotations
import ipaddress
import re
import secrets
from urllib.parse import urlsplit
from core.outreach_ledger import Ledger, OutreachHold

PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
         b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def pixel_base(settings) -> str | None:
    if not all((getattr(settings, "mode", None) == "local",
                getattr(settings, "outreach_enabled", False),
                getattr(settings, "outreach_pixel_enabled", False),
                getattr(settings, "outreach_pixel_healthy", False),
                getattr(settings, "outreach_pixel_ingress_logs_safe", False))):
        return None
    value = getattr(settings, "outreach_pixel_base", "")
    if not isinstance(value, str) or len(value) > 254:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.port not in (None, 443) or url.path not in ("", "/")
                or url.query or url.fragment or not url.hostname.isascii()
                or not re.fullmatch(r"[a-z0-9.-]+", url.hostname)
                or url.hostname in {"localhost", "127.0.0.1"}
                or "." not in url.hostname or ".." in url.hostname):
            return None
        return "https://" + url.hostname.lower()
    except ValueError:
        return None


def register_pixel(settings, ledger: Ledger, attempt_id: str) -> tuple[str, str]:
    base = pixel_base(settings)
    if base is None:
        return "", "not_tracked"
    token = secrets.token_hex(32)
    ledger.register_pixel(attempt_id, token)
    return f"{base}/o/{token}.gif", "pixel_enabled"


def classify_open(headers: list[tuple[bytes, bytes]]) -> str:
    """Scanner/unknown opens are retained as diagnostics, not human reads."""
    agents = [v.decode("latin1", "replace") for k, v in headers
              if k.lower() == b"user-agent"]
    if len(agents) != 1 or not agents[0] or len(agents[0]) > 256:
        return "unknown"
    agent = agents[0].casefold()
    if any(marker in agent for marker in (
        "bot", "spider", "scanner", "prefetch", "preview", "googleimageproxy",
        "google image proxy", "security", "proofpoint", "mimecast", "safelinks")):
        return "scanner"
    return "qualified"


def pixel_app(settings):
    """Standalone ASGI endpoint. No token in responses, errors or access logs."""
    async def app(scope, receive, send):
        if scope.get("type") != "http":
            return

        async def respond(status, *, image=False):
            headers = [(b"cache-control", b"no-store, private"),
                       (b"referrer-policy", b"no-referrer"),
                       (b"x-content-type-options", b"nosniff"),
                       (b"content-type", b"image/gif" if image else b"text/plain")]
            await send({"type": "http.response.start", "status": status,
                        "headers": headers})
            await send({"type": "http.response.body", "body": PIXEL if image else b""})

        base = pixel_base(settings)
        if base is None:
            await respond(404)
            return
        expected_host = urlsplit(base).hostname
        host = [value.decode("ascii", "ignore").lower() for name, value
                in scope.get("headers", []) if name.lower() == b"host"]
        peer = (scope.get("client") or ("", 0))[0]
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except (ValueError, TypeError):
            loopback = False
        if (not loopback or len(host) != 1 or host[0] != expected_host
                or scope.get("method") != "GET" or scope.get("query_string")
                or not isinstance(scope.get("path"), str)):
            await respond(404)
            return
        match = re.fullmatch(r"/o/([a-f0-9]{64})\.gif", scope["path"])
        if match is None:
            await respond(404)
            return
        try:
            found = Ledger(settings).record_pixel_open(
                match.group(1), classify_open(scope.get("headers", [])))
        except Exception:
            # Failed SQLite write cannot acknowledge an open.
            await respond(503)
            return
        if found:
            await respond(200, image=True)
        else:
            await respond(404)

    return app


def serve_pixel(settings) -> None:
    """Only the separate 127.0.0.1 listener, never src/server.py."""
    if pixel_base(settings) is None:
        return
    port = getattr(settings, "outreach_pixel_loopback_port", 0)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise OutreachHold("pixel_loopback_port_required")
    import uvicorn
    # Uvicorn's ordinary access logs contain URI/token: disable entirely.
    uvicorn.run(pixel_app(settings), host="127.0.0.1", port=port,
                access_log=False, log_level="error")
