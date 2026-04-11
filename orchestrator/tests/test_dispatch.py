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


# ── Agent get-or-create ─────────────────────────────────────────────

from types import SimpleNamespace


def test_ensure_agent_creates_fresh_agent_when_cache_empty(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    db.get_dispatch_agent.return_value = None
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")

    agent_id = manager._ensure_agent("valid_node")

    assert agent_id == "agent_new"
    client.beta.agents.create.assert_called_once()
    call = client.beta.agents.create.call_args
    assert call.kwargs["name"] == "ora-dispatch-valid_node"
    assert "ValidNode" in call.kwargs["system"]
    assert call.kwargs["tools"] == [{"type": "agent_toolset_20260401"}]
    db.upsert_dispatch_agent.assert_called_once()
    upsert_kwargs = db.upsert_dispatch_agent.call_args.kwargs
    assert upsert_kwargs["node_name"] == "valid_node"
    assert upsert_kwargs["agent_id"] == "agent_new"
    assert len(upsert_kwargs["prompt_hash"]) == 64


def test_ensure_agent_reuses_cached_agent_when_hash_matches(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    current_hash = manager._spec_hash("valid_node")
    db.get_dispatch_agent.return_value = {
        "agent_id": "agent_cached",
        "prompt_hash": current_hash,
    }

    agent_id = manager._ensure_agent("valid_node")

    assert agent_id == "agent_cached"
    client.beta.agents.create.assert_not_called()
    db.upsert_dispatch_agent.assert_not_called()


def test_ensure_agent_rebuilds_when_spec_hash_drifts(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    db.get_dispatch_agent.return_value = {
        "agent_id": "agent_stale",
        "prompt_hash": "stale_hash_0000",
    }
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_rebuilt")

    agent_id = manager._ensure_agent("valid_node")

    assert agent_id == "agent_rebuilt"
    client.beta.agents.create.assert_called_once()
    db.upsert_dispatch_agent.assert_called_once()


# ── Sub-session lifecycle ───────────────────────────────────────────


class _FakeStream:
    """Context manager that yields a canned list of events."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *exc):
        return False


def _model_end(input_tokens, output_tokens):
    return SimpleNamespace(
        type="span.model_request_end",
        model="claude-opus-4-6",
        model_usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _agent_message(text):
    return SimpleNamespace(
        type="agent.message",
        content=[SimpleNamespace(text=text)],
    )


def _idle():
    return SimpleNamespace(type="session.status_idle", stop_reason=None)


def _terminated(error="boom"):
    return SimpleNamespace(type="session.status_terminated", error=error)


def test_run_sub_session_returns_successful_result(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub_1")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(100, 25),
        _agent_message("Research complete. Findings: ..."),
        _idle(),
    ])

    result = manager._run_sub_session(
        parent_session_id="sesn_parent",
        agent_id="agent_abc",
        node_name="valid_node",
        input_data={"task": "do the thing"},
    )

    assert result["status"] == "complete"
    assert "Research complete" in result["output"]
    assert result["tokens"]["input"] == 100
    assert result["tokens"]["output"] == 25
    assert result["cost_usd"] > 0
    assert result["sub_session_id"] == "sesn_sub_1"
    db.record_dispatch_start.assert_called_once()
    db.record_dispatch_complete.assert_called_once()
    db.record_dispatch_failure.assert_not_called()


def test_run_sub_session_reports_termination_as_failure(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub_2")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(50, 0),
        _terminated("container restart"),
    ])

    result = manager._run_sub_session(
        parent_session_id="sesn_parent",
        agent_id="agent_abc",
        node_name="valid_node",
        input_data={},
    )

    assert result["status"] == "failed"
    assert "container restart" in result["error"]
    db.record_dispatch_failure.assert_called_once()
    db.record_dispatch_complete.assert_not_called()


def test_run_sub_session_sends_input_as_user_message(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub_3")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(10, 5),
        _agent_message("ok"),
        _idle(),
    ])

    manager._run_sub_session(
        parent_session_id="sesn_parent",
        agent_id="agent_abc",
        node_name="valid_node",
        input_data={"task": "ping"},
    )

    send_call = client.beta.sessions.events.send.call_args
    assert send_call.args[0] == "sesn_sub_3"
    events = send_call.kwargs["events"]
    assert events[0]["type"] == "user.message"
    payload_text = events[0]["content"][0]["text"]
    assert "ping" in payload_text
