"""Tests for orchestrator.dispatch."""
from __future__ import annotations

import json

import pytest

from orchestrator.dispatch import parse_dispatch_fences


def test_parse_single_dispatch_fence():
    text = (
        "Planning complete — dispatching.\n"
        "```DISPATCH node=business_analyst\n"
        '{"task": "research async patterns", "budget_size": "S"}\n'
        "```\n"
        "Awaiting result."
    )
    result = parse_dispatch_fences(text)
    assert len(result) == 1
    node, payload = result[0]
    assert node == "business_analyst"
    assert payload == {"task": "research async patterns", "budget_size": "S"}


def test_parse_multiple_dispatch_fences():
    text = (
        "```DISPATCH node=node_designer\n{\"task\": \"design researcher\"}\n```\n"
        "Then:\n"
        "```DISPATCH node=node_creator\n{\"task\": \"build researcher\"}\n```\n"
    )
    result = parse_dispatch_fences(text)
    assert len(result) == 2
    assert result[0][0] == "node_designer"
    assert result[1][0] == "node_creator"


def test_parse_ignores_non_dispatch_fences():
    text = (
        "```python\nprint('hi')\n```\n"
        '```DISPATCH node=business_analyst\n{"task": "x"}\n```\n'
    )
    result = parse_dispatch_fences(text)
    assert len(result) == 1
    assert result[0][0] == "business_analyst"


def test_parse_skips_fence_with_invalid_json():
    text = (
        "```DISPATCH node=business_analyst\n"
        "not valid json\n"
        "```\n"
    )
    # Malformed payloads are silently skipped — the orchestrator cannot
    # dispatch something it cannot parse, and will report nothing back
    # rather than guess.
    assert parse_dispatch_fences(text) == []


def test_parse_skips_fence_without_node_attr():
    text = (
        "```DISPATCH\n"
        '{"task": "x"}\n'
        "```\n"
    )
    assert parse_dispatch_fences(text) == []


def test_parse_empty_on_no_fences():
    assert parse_dispatch_fences("no fences here") == []
