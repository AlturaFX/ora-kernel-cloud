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


# ── Node spec loader ────────────────────────────────────────────────

from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.dispatch import DispatchManager


def _make_manager(tmp_path: Path, **overrides):
    (tmp_path / "valid_node.md").write_text(
        "---\nname: Valid\n---\n\n## System Prompt\n\nYou are the ValidNode.\n"
    )
    db = MagicMock()
    db.get_dispatch_agent = MagicMock(return_value=None)
    db.upsert_dispatch_agent = MagicMock()
    db.record_dispatch_start = MagicMock()
    db.record_dispatch_complete = MagicMock()
    db.record_dispatch_failure = MagicMock()
    client = MagicMock()
    send_to_parent = MagicMock()
    defaults = dict(
        db=db,
        client=client,
        environment_id="env_test",
        send_to_parent=send_to_parent,
        node_spec_dir=tmp_path,
    )
    defaults.update(overrides)
    return DispatchManager(**defaults), db, client, send_to_parent


def test_load_node_spec_returns_file_contents(tmp_path):
    manager, *_ = _make_manager(tmp_path)
    spec = manager._load_node_spec("valid_node")
    assert "ValidNode" in spec
    assert "System Prompt" in spec


def test_load_node_spec_raises_for_unknown_node(tmp_path):
    manager, *_ = _make_manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        manager._load_node_spec("does_not_exist")


def test_node_spec_hash_is_content_addressed(tmp_path):
    manager, *_ = _make_manager(tmp_path)
    h1 = manager._spec_hash("valid_node")
    assert len(h1) == 64  # sha256 hex
    # Mutate the file; hash must change
    (tmp_path / "valid_node.md").write_text("different content")
    h2 = manager._spec_hash("valid_node")
    assert h1 != h2
