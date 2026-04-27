"""
HTTP stream proxy.

Sonos refuses to play many third-party HTTPS streams cleanly (signed
googlevideo URLs in particular fail with UPnP 701 "transition not
available"). The fix is to proxy the upstream URL through the Pi so the
speaker sees a plain HTTP LAN URL with a correct Content-Type.

We mint a short-lived token, store the upstream URL in memory, and serve
GET /sonos/stream/<token> by streaming the upstream response back to the
caller. Range requests are passed through so Sonos can seek.
"""
from __future__ import annotations

import secrets
import socket
import threading
import time

import requests


_lock = threading.Lock()
_streams: dict[str, dict] = {}        # token -> {url, expires, content_type}
_TTL_SECS = 6 * 3600                   # match yt-dlp URL lifetime


def register(upstream_url: str, content_type: str = "audio/mpeg") -> str:
    """Store an upstream URL under a fresh token; return the token."""
    token = secrets.token_urlsafe(16)
    _gc()
    with _lock:
        _streams[token] = {
            "url": upstream_url,
            "expires": time.time() + _TTL_SECS,
            "content_type": content_type,
        }
    return token


def lookup(token: str) -> dict | None:
    with _lock:
        meta = _streams.get(token)
        if meta and meta["expires"] < time.time():
            _streams.pop(token, None)
            return None
        return meta


def _gc() -> None:
    now = time.time()
    with _lock:
        dead = [t for t, m in _streams.items() if m["expires"] < now]
        for t in dead:
            _streams.pop(t, None)


def lan_ip_for(remote_ip: str) -> str:
    """LAN IP of *this* host that would route to remote_ip (no packet sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def stream_response(token: str, range_header: str | None):
    """Generator + headers for piping upstream bytes to the speaker.

    Returns (generator, headers, status_code, content_type) or
    (None, None, 404, None) if the token isn't valid.
    """
    meta = lookup(token)
    if not meta:
        return None, None, 404, None

    upstream_headers = {}
    if range_header:
        upstream_headers["Range"] = range_header
    # Some CDNs are stricter without a UA.
    upstream_headers["User-Agent"] = "Mozilla/5.0 (sonos-proxy)"

    r = requests.get(meta["url"], headers=upstream_headers, stream=True, timeout=30)
    out_headers = {
        "Content-Type": r.headers.get("Content-Type", meta["content_type"]),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in r.headers:
            out_headers[h] = r.headers[h]

    def gen():
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return gen, out_headers, r.status_code, out_headers["Content-Type"]
