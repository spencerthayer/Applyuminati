"""LLM structured output: the untrusted-output boundary.

The only sanctioned way to get a typed object out of a model. Extracts JSON,
validates against a Pydantic schema, and issues repair turns on validation
failure — never returns a half-parsed dict.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from applyuminati.core.errors import InvalidModelOutputError
from applyuminati.core.logging import get_logger
from applyuminati.llm.base import (
    CompletionRequest,
    CompletionResponse,
    LLMCapability,
    LLMProvider,
    Message,
    Role,
)

log = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

__all__ = ["extract_json", "json_schema_for", "request_structured"]


def extract_json(text: str) -> str:
    """Extract the first balanced JSON object or array from ``text``.

    Handles ```json fences, leading prose, and trailing commentary. The
    scanner is string-literal and escape aware, so a ``}`` inside a JSON
    string value does not terminate the scan prematurely.
    """
    # Strip code fences.
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Find the first { or [.
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            char = text[i]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == start_char:
                depth += 1
            elif char == end_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return text.strip()


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Return a JSON Schema with ``$defs`` inlined and ``additionalProperties: false``."""
    raw = schema.model_json_schema()
    _inline_defs(raw)
    _set_additional_properties_false(raw)
    return raw


def _inline_defs(node: dict[str, Any]) -> None:
    """Resolve ``$ref`` references inline."""
    defs = node.pop("$defs", {})
    _resolve_refs(node, defs)


def _resolve_refs(node: Any, defs: dict[str, Any]) -> None:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            # $ref looks like "#/$defs/SomeModel"
            parts = ref.split("/")
            target = defs
            for part in parts[1:]:
                target = target.get(part, {})
            node.clear()
            node.update(target)
            _resolve_refs(node, defs)
        for value in node.values():
            _resolve_refs(value, defs)
    elif isinstance(node, list):
        for item in node:
            _resolve_refs(item, defs)


def _set_additional_properties_false(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            _set_additional_properties_false(value)
    elif isinstance(node, list):
        for item in node:
            _set_additional_properties_false(item)


async def request_structured(
    provider: LLMProvider,
    request: CompletionRequest,
    schema: type[ModelT],
    *,
    max_repairs: int = 2,
) -> tuple[ModelT, CompletionResponse]:
    """Get a schema-validated object from a model.

    Tries native structured output, then JSON mode, then plain completion.
    On validation failure, issues a repair turn containing the errors. After
    exhausting repairs, raises :class:`InvalidModelOutputError`.
    """
    attempts = 0
    current_request = request

    if provider.metadata.supports(LLMCapability.STRUCTURED_OUTPUT):
        current_request = request.model_copy(
            update={"response_schema": json_schema_for(schema)}
        )

    while True:
        response = await provider.complete(current_request)
        raw_json = extract_json(response.text)
        try:
            result = schema.model_validate_json(raw_json)
            return result, response
        except ValidationError as exc:
            attempts += 1
            if attempts > max_repairs:
                log.error(
                    "structured.exhausted_repairs",
                    prompt_id=request.prompt_id,
                    attempts=attempts,
                    error=str(exc)[:500],
                )
                raise InvalidModelOutputError(
                    f"model output failed schema validation after {attempts} attempts",
                    code="llm.invalid_output",
                    details={
                        "validation_errors": exc.errors()[:10],
                        "last_raw_output": raw_json[:1000],
                    },
                ) from exc
            log.warning(
                "structured.repairing",
                prompt_id=request.prompt_id,
                attempt=attempts,
            )
            repair_message = Message(
                role=Role.USER,
                content=(
                    f"The previous response failed validation with these errors:\n"
                    f"{json.dumps(exc.errors(), default=str, indent=2)}\n\n"
                    f"Please return a corrected JSON object matching the schema."
                ),
            )
            current_request = request.model_copy(
                update={
                    "messages": [*request.messages, response, repair_message],
                }
            )
