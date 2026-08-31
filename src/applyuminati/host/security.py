"""Trust boundary for the outbound Browser Host connection."""

from __future__ import annotations

from urllib.parse import urlparse

from applyuminati.core.errors import ConfigurationError
from applyuminati.core.settings import LOOPBACK_HOSTS

__all__ = ["require_secure_server"]


def require_secure_server(url: str, *, allow_insecure: bool) -> None:
    """Refuse a remote plaintext WebSocket.

    A Browser Host drives a real signed-in browser. Remote connections require
    TLS. Loopback may use ``ws://``. ``--allow-insecure`` is an explicit
    operator override, not a default.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "wss":
        return
    if scheme != "ws":
        raise ConfigurationError(
            f"browser host server must be ws:// or wss://, not {scheme or 'missing scheme'}",
            code="configuration.browser_host_url",
        )
    if host in LOOPBACK_HOSTS or allow_insecure:
        return
    raise ConfigurationError(
        "remote Browser Hosts require wss://. Pass --allow-insecure only for a "
        "trusted network you have already wrapped in TLS at a proxy.",
        code="configuration.browser_host_requires_tls",
        details={"host": host},
    )
