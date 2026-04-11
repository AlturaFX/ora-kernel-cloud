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


# ── DispatchManager.handle_message (top-level entry point) ─────────

def test_handle_message_dispatches_fence_and_sends_result(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(100, 25),
        _agent_message("result body"),
        _idle(),
    ])

    text = (
        "Dispatching.\n"
        "```DISPATCH node=valid_node\n"
        '{"task": "demo"}\n'
        "```\n"
    )
    count = manager.handle_message(parent_session_id="sesn_parent", message_text=text)

    assert count == 1
    # Result forwarded to parent as a DISPATCH_RESULT fence
    send_to_parent.assert_called_once()
    result_args = send_to_parent.call_args
    assert result_args.args[0] == "sesn_parent"
    forwarded_text = result_args.args[1]
    assert "```DISPATCH_RESULT" in forwarded_text
    assert "node=valid_node" in forwarded_text
    assert "status=complete" in forwarded_text
    assert "result body" in forwarded_text


def test_handle_message_reports_missing_node_as_failure(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    text = (
        "```DISPATCH node=nonexistent\n"
        '{"task": "x"}\n'
        "```\n"
    )
    count = manager.handle_message(parent_session_id="sesn_parent", message_text=text)

    assert count == 1
    send_to_parent.assert_called_once()
    forwarded = send_to_parent.call_args.args[1]
    assert "status=failed" in forwarded
    assert "nonexistent" in forwarded
    client.beta.agents.create.assert_not_called()


def test_handle_message_returns_zero_when_no_fences(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    count = manager.handle_message("sesn_parent", "just prose")
    assert count == 0
    send_to_parent.assert_not_called()


def test_handle_message_continues_after_per_dispatch_failure(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    # Build a valid second node spec
    (tmp_path / "second_node.md").write_text(
        "---\nname: Second\n---\n\n## System Prompt\n\nYou are Second.\n"
    )
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_second")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(5, 5),
        _agent_message("ok"),
        _idle(),
    ])

    text = (
        "```DISPATCH node=missing\n{\"task\": \"a\"}\n```\n"
        "```DISPATCH node=second_node\n{\"task\": \"b\"}\n```\n"
    )
    count = manager.handle_message("sesn_parent", text)

    assert count == 2
    # Both fences produce a DISPATCH_RESULT (one failed, one complete)
    assert send_to_parent.call_count == 2
    first, second = send_to_parent.call_args_list
    assert "status=failed" in first.args[1]
    assert "status=complete" in second.args[1]


# ── ws_bridge routing ──────────────────────────────────────────────


def _make_manager_with_bridge(tmp_path, **overrides):
    bridge = MagicMock()
    manager, db, client, send_to_parent = _make_manager(
        tmp_path, ws_bridge=bridge, **overrides
    )
    return manager, db, client, send_to_parent, bridge


def test_dispatch_start_broadcasts_node_update_running(tmp_path):
    manager, db, client, _, bridge = _make_manager_with_bridge(tmp_path)
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(50, 25),
        _agent_message("done"),
        _idle(),
    ])

    manager.handle_message(
        "sesn_parent",
        '```DISPATCH node=valid_node\n{"task": "x"}\n```',
    )

    events = [c.args[0] for c in bridge.broadcast.call_args_list]
    node_updates = [e for e in events if e["event_type"] == "NODE_UPDATE"]
    edge_updates = [e for e in events if e["event_type"] == "EDGE_UPDATE"]

    assert len(node_updates) >= 2
    assert node_updates[0]["payload"]["status"] == "running"
    assert node_updates[0]["payload"]["node_name"] == "valid_node"
    assert node_updates[0]["payload"]["parent_id"] == "sesn_parent"
    assert node_updates[-1]["payload"]["status"] == "complete"
    assert node_updates[-1]["payload"]["cost_usd"] > 0

    assert len(edge_updates) == 1
    assert edge_updates[0]["payload"]["from_id"] == "sesn_parent"
    assert edge_updates[0]["payload"]["to_id"] == "sesn_sub"


def test_dispatch_failure_broadcasts_node_update_failed(tmp_path):
    manager, db, client, _, bridge = _make_manager_with_bridge(tmp_path)

    manager.handle_message(
        "sesn_parent",
        '```DISPATCH node=nonexistent\n{"task": "x"}\n```',
    )

    events = [c.args[0] for c in bridge.broadcast.call_args_list]
    node_updates = [e for e in events if e["event_type"] == "NODE_UPDATE"]
    failed_updates = [u for u in node_updates if u["payload"]["status"] == "failed"]
    assert len(failed_updates) == 1
    assert failed_updates[0]["payload"]["node_id"] == "failed:nonexistent"
    assert failed_updates[0]["payload"]["node_name"] == "nonexistent"


def test_dispatch_manager_ws_bridge_is_optional(tmp_path):
    """DispatchManager with ws_bridge=None must exercise every broadcast
    guard without crashing — empty input, successful dispatch, and a
    failed dispatch all run cleanly with no bridge attached."""
    manager, db, client, _ = _make_manager(tmp_path)
    assert manager.ws_bridge is None  # Default

    # Path 1: empty input, no fences
    manager.handle_message("sesn_parent", "no fences here")

    # Path 2: successful dispatch — exercises running/edge/complete guards
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(5, 5),
        _agent_message("ok"),
        _idle(),
    ])
    manager.handle_message(
        "sesn_parent",
        '```DISPATCH node=valid_node\n{"task": "x"}\n```',
    )

    # Path 3: pre-sub-session failure — exercises handle_message outer guard
    manager.handle_message(
        "sesn_parent",
        '```DISPATCH node=nonexistent\n{"task": "y"}\n```',
    )


def test_internally_failed_dispatch_broadcasts_exactly_one_failed_update(tmp_path):
    """When _run_sub_session fails (terminated), the outer guard in
    handle_message must NOT fire a second failed broadcast. Exactly
    one NODE_UPDATE(status=failed) should be seen by the bridge."""
    manager, db, client, _, bridge = _make_manager_with_bridge(tmp_path)
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(10, 0),
        _terminated("container restart"),
    ])

    manager.handle_message(
        "sesn_parent",
        '```DISPATCH node=valid_node\n{"task": "x"}\n```',
    )

    events = [c.args[0] for c in bridge.broadcast.call_args_list]
    failed_updates = [
        e for e in events
        if e["event_type"] == "NODE_UPDATE"
        and e["payload"]["status"] == "failed"
    ]
    assert len(failed_updates) == 1, (
        f"expected exactly 1 failed NODE_UPDATE, got {len(failed_updates)}"
    )
    # The one that fires is _run_sub_session's internal broadcast — it has
    # the real sub_session_id, not the synthetic 'failed:...' id
    assert failed_updates[0]["payload"]["node_id"] == "sesn_sub"
    assert failed_updates[0]["payload"]["error"] is not None
