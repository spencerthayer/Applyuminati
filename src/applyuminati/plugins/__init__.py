"""First-party plugin implementations.

Concrete adapters live here — job sources, browser backends, agent runtimes,
email providers, LLM providers. They depend on the contract packages
(:mod:`applyuminati.sources`, :mod:`applyuminati.browser`, …); nothing in those
packages may import this one. That direction is enforced by the import-linter
contract "Contract packages never import their own concrete plugins".

This module intentionally imports none of its subpackages: importing a plugin
must never be a side effect of importing the package, because that would pull
``playwright``, ``httpx`` clients and vendor SDKs into every process. Discovery
happens through ``importlib.metadata`` entry points declared in
``pyproject.toml``, plus the idempotent ``register_builtin_*`` helpers on each
contract package.
"""

from __future__ import annotations
