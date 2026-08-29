"""LLM provider contract, structured output, prompts, and client facade."""

from applyuminati.llm.base import (
    LLM_REGISTRY,
    CompletionRequest,
    CompletionResponse,
    LLMCallRecord,
    LLMCapability,
    LLMProvider,
    Message,
    ModelT,
    ProviderMetadata,
    ProviderSelection,
    Role,
    TokenUsage,
    llm_plugin,
)
from applyuminati.llm.client import LLMClient
from applyuminati.llm.prompts import PROMPT_REGISTRY, PromptTemplate, get_prompt, register
from applyuminati.llm.structured import extract_json, json_schema_for, request_structured
from applyuminati.llm.usage import UsageTracker

__all__ = [
    "LLM_REGISTRY",
    "CompletionRequest",
    "CompletionResponse",
    "LLMCallRecord",
    "LLMCapability",
    "LLMClient",
    "LLMProvider",
    "Message",
    "ModelT",
    "PROMPT_REGISTRY",
    "PromptTemplate",
    "ProviderMetadata",
    "ProviderSelection",
    "Role",
    "TokenUsage",
    "UsageTracker",
    "extract_json",
    "get_prompt",
    "json_schema_for",
    "register",
    "request_structured",
]

