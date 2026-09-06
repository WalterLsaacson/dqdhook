#!/usr/bin/env python3
"""HTTP bind + public URL helpers for frontend boards.

Boards listen on DQD_BIND (default 0.0.0.0) so they are reachable on this
machine's public IP. Process-to-process health checks stay on 127.0.0.1.
Browser-facing URLs prefer the incoming Host header, then DQD_PUBLIC_HOST,
then a guessed IPv4.
"""

from __future__ import annotations

import os
import socket

LOOPBACK = "127.0.0.1"
DEFAULT_BIND = "0.0.0.0"


def bind_host() -> str:
    raw = (os.getenv("DQD_BIND") or DEFAULT_BIND).strip()
    return raw or DEFAULT_BIND


def _strip_hostport(value: str) -> str:
    host = (value or "").strip()
    if not host:
        return ""
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def detect_ipv4() -> str | None:
    """Best-effort IPv4 used for outbound traffic (often the public address)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None


def public_host(request_host: str | None = None) -> str:
    env = (os.getenv("DQD_PUBLIC_HOST") or "").strip()
    if env:
        return _strip_hostport(env)
    if request_host:
        stripped = _strip_hostport(request_host)
        if stripped:
            return stripped
    return detect_ipv4() or LOOPBACK


def loopback_url(port: int, path: str = "/") -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{LOOPBACK}:{int(port)}{path}"


def public_url(
    port: int, path: str = "/", *, request_host: str | None = None
) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{public_host(request_host)}:{int(port)}{path}"


def listen_banner(name: str, bind: str, port: int) -> str:
    return (
        f"{name} → bind {bind}:{port} · "
        f"local {loopback_url(port)} · "
        f"public {public_url(port)}"
    )
