"""_extract_json tolerates the wrappers different providers put around a
requested JSON object, and never invents JSON where there is none."""

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")

from phoenix.graph.llm_client import _extract_json


def test_a_bare_object_is_returned_unchanged():
    assert _extract_json('{"a": 1}') == '{"a": 1}'


def test_a_markdown_fence_is_stripped():
    assert _extract_json('```json\n{"a": {"b": 2}}\n```') == '{"a": {"b": 2}}'


def test_leading_and_trailing_prose_is_dropped():
    assert _extract_json('Here you go: {"a": "x}"} hope that helps') == '{"a": "x}"}'


def test_text_with_no_object_is_returned_untouched():
    assert _extract_json("no json here") == "no json here"


def test_an_unterminated_object_is_returned_untouched_for_the_caller_to_reject():
    text = '```json\n{"a": 1, "b": ['
    assert _extract_json(text) == text
