"""Prompt templates: versioned, registered, never inline.

Every prompt has an id, a version, an output schema, and a render method that
substitutes variables strictly (missing keys raise). The registry is the
single source of truth for prompt identity, so a prompt change is a versioned
event rather than a silent edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Any

from pydantic import BaseModel

from applyuminati.llm.base import Message, Role

__all__ = ["PROMPT_REGISTRY", "PromptTemplate", "get_prompt", "register"]


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    id: str
    version: str
    description: str
    output_schema: type[BaseModel] | None
    system: str
    template: str

    def render(self, **kwargs: Any) -> list[Message]:
        """Render the prompt into messages. Raises on a missing variable."""
        system_msg = (
            Template(self.system).substitute(**kwargs) if "$" in self.system else self.system
        )
        user_content = Template(self.template).substitute(**kwargs)
        return [
            Message(role=Role.SYSTEM, content=system_msg),
            Message(role=Role.USER, content=user_content),
        ]

    @property
    def full_id(self) -> str:
        return f"{self.id}/{self.version}"


PROMPT_REGISTRY: dict[str, PromptTemplate] = {}


def register(prompt: PromptTemplate) -> PromptTemplate:
    if prompt.id in PROMPT_REGISTRY:
        if PROMPT_REGISTRY[prompt.id].version != prompt.version:
            # Re-registering with a different version is allowed (it's an upgrade).
            PROMPT_REGISTRY[prompt.id] = prompt
        return prompt
    PROMPT_REGISTRY[prompt.id] = prompt
    return prompt


def get_prompt(prompt_id: str) -> PromptTemplate:
    return PROMPT_REGISTRY[prompt_id]
