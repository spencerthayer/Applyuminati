"""Tests for LLM structured-output parsing."""

from __future__ import annotations

from applyuminati.llm.structured import extract_json


def test_extract_json_from_fence() -> None:
    text = 'Some prose\n```json\n{"key": "value"}\n```\nmore prose'
    result = extract_json(text)
    assert '"key"' in result


def test_extract_json_with_braces_in_string() -> None:
    text = '{"path": "C:\\}\\test", "ok": true}'
    result = extract_json(text)
    assert "C:" in result


def test_extract_json_leading_prose() -> None:
    text = 'Here is the result:\n{"answer": 42}'
    result = extract_json(text)
    assert "42" in result


def test_extract_json_array_root() -> None:
    text = "[1, 2, 3]"
    result = extract_json(text)
    assert result == "[1, 2, 3]"


def test_extract_json_trailing_commentary() -> None:
    text = '{"a": 1}\n\nI hope this helps!'
    result = extract_json(text)
    assert result == '{"a": 1}'
