"""First-party LLM provider adapters.

Registration is a function, not an import side effect: importing this package
must not pull httpx clients into the process.
"""

from __future__ import annotations


def register_llm_providers() -> None:
    """Register built-in LLM adapters. Idempotent."""
    from applyuminati.llm.base import LLM_REGISTRY

    if "openai_compatible" not in LLM_REGISTRY:
        from applyuminati.plugins.llm.openai_compatible import PLUGIN as openai

        LLM_REGISTRY.register(openai)
    if "anthropic" not in LLM_REGISTRY:
        from applyuminati.plugins.llm.anthropic import PLUGIN as anthropic

        LLM_REGISTRY.register(anthropic)
    if "gemini" not in LLM_REGISTRY:
        from applyuminati.plugins.llm.gemini import PLUGIN as gemini

        LLM_REGISTRY.register(gemini)


__all__ = ["register_llm_providers"]
