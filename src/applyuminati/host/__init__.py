"""Native ``applyuminati-browser-host`` companion.

A small desktop process. Its job is browser execution, not job-search
orchestration. It dials out to Applyuminati over the Browser Host protocol
and runs semantic commands against local ego lite or Playwright backends.
"""

from applyuminati.host.discovery import advertise_backends, loopback_url
from applyuminati.host.security import require_secure_server

__all__ = ["advertise_backends", "loopback_url", "require_secure_server"]
