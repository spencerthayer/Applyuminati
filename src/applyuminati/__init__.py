"""Applyuminati: a local-first, autonomous, LLM-powered job search platform."""

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - trivial packaging shim
    __version__ = version("applyuminati")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
