"""Integration tests: EventConsumer routes file ops to FileSync."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.event_consumer import EventConsumer


def _consumer(file_sync):
    db = MagicMock()
    db.log_activity = MagicMock()
    return EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        file_sync=file_sync,
    )


def _write_event(file_path, content, tool_name="write"):
    # The Managed Agent toolset emits lowercase tool names ("write", "edit",
    # "bash", "read"). Default to lowercase; individual tests override when
    # they want to pin case behavior.
    return SimpleNamespace(
        type="agent.tool_use",
        name=tool_name,
        input={"file_path": file_path, "content": content},
    )


def _edit_event(file_path, old, new, tool_name="edit"):
    return SimpleNamespace(
        type="agent.tool_use",
        name=tool_name,
        input={"file_path": file_path, "old_string": old, "new_string": new},
    )


def _message_event(text):
    return SimpleNamespace(
        type="agent.message",
        content=[SimpleNamespace(text=text)],
    )


def test_write_event_invokes_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = _write_event("/work/.claude/kernel/journal/WISDOM.md", "body")
    consumer._handle_tool_use("sess_1", event)

    fs.handle_write.assert_called_once_with(
        "/work/.claude/kernel/journal/WISDOM.md", "body"
    )
    fs.handle_edit.assert_not_called()


def test_edit_event_invokes_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = _edit_event(
        "/work/.claude/kernel/journal/WISDOM.md", "a", "b"
    )
    consumer._handle_tool_use("sess_1", event)

    fs.handle_edit.assert_called_once_with(
        "/work/.claude/kernel/journal/WISDOM.md", "a", "b"
    )
    fs.handle_write.assert_not_called()


def test_other_tool_does_not_invoke_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = SimpleNamespace(
        type="agent.tool_use",
        name="Bash",
        input={"command": "ls"},
    )
    consumer._handle_tool_use("sess_1", event)

    fs.handle_write.assert_not_called()
    fs.handle_edit.assert_not_called()


def test_message_event_invokes_snapshot_parser():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = _message_event("```SYNC path=.claude/kernel/journal/WISDOM.md\nhi\n```")
    consumer._handle_message("sess_1", event)

    fs.handle_snapshot_response.assert_called_once()
    call_text = fs.handle_snapshot_response.call_args.args[0]
    assert "SYNC path=" in call_text


def test_file_sync_is_optional():
    # No file_sync passed — should not crash on tool_use or message.
    consumer = EventConsumer(
        db=MagicMock(),
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
    )
    consumer._handle_tool_use(
        "sess_1",
        _write_event("/work/.claude/kernel/journal/WISDOM.md", "x"),
    )
    consumer._handle_message("sess_1", _message_event("no fences"))


def test_capitalized_write_also_routes_to_file_sync():
    """Regression: routing must be case-insensitive so Claude Code-style
    tool names ('Write'/'Edit') also fire CDC — the Managed Agent toolset
    uses lowercase, but we should not break on either case."""
    fs = MagicMock()
    consumer = _consumer(fs)

    consumer._handle_tool_use(
        "sess_1",
        _write_event("/work/.claude/kernel/journal/WISDOM.md", "x", tool_name="Write"),
    )
    fs.handle_write.assert_called_once()


def test_capitalized_edit_also_routes_to_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    consumer._handle_tool_use(
        "sess_1",
        _edit_event("/work/.claude/kernel/journal/WISDOM.md", "a", "b", tool_name="Edit"),
    )
    fs.handle_edit.assert_called_once()


def test_message_event_routes_to_dispatch_manager():
    fs = MagicMock()
    dm = MagicMock()
    db = MagicMock()
    consumer = EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        file_sync=fs,
        dispatch_manager=dm,
    )
    event = _message_event(
        "```DISPATCH node=foo\n{\"task\":\"x\"}\n```"
    )
    consumer._handle_message("sess_parent", event)

    dm.handle_message.assert_called_once()
    args = dm.handle_message.call_args
    assert args.args[0] == "sess_parent"
    assert "DISPATCH node=foo" in args.args[1]
    # file_sync should also still be called — both paths fire on every message
    fs.handle_snapshot_response.assert_called_once()


def test_dispatch_manager_is_optional():
    consumer = EventConsumer(
        db=MagicMock(),
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
    )
    # No dispatch_manager passed — should not raise
    consumer._handle_message("sess_parent", _message_event("plain message"))


# ── ws_bridge routing ──────────────────────────────────────────────


def test_message_event_broadcasts_chat_response_when_bridge_present():
    bridge = MagicMock()
    db = MagicMock()
    consumer = EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        ws_bridge=bridge,
    )
    event = _message_event("hello from kernel")
    consumer._handle_message("sess_parent", event)

    # The bridge should have been broadcast to with a CHAT_RESPONSE envelope
    assert bridge.broadcast.called
    # Find the CHAT_RESPONSE call (there may also be an ACTIVITY call)
    events = [c.args[0] for c in bridge.broadcast.call_args_list]
    chat_events = [e for e in events if e["event_type"] == "CHAT_RESPONSE"]
    assert len(chat_events) == 1
    assert chat_events[0]["payload"]["session_id"] == "sess_parent"
    assert "hello from kernel" in chat_events[0]["payload"]["text"]


def test_status_running_broadcasts_system_status():
    bridge = MagicMock()
    db = MagicMock()
    consumer = EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        ws_bridge=bridge,
    )
    event = SimpleNamespace(type="session.status_running")
    consumer._handle_status_running("sess_parent", event)

    events = [c.args[0] for c in bridge.broadcast.call_args_list]
    status_events = [e for e in events if e["event_type"] == "SYSTEM_STATUS"]
    assert len(status_events) == 1
    assert status_events[0]["payload"]["status"] == "running"


def test_tool_use_broadcasts_activity():
    bridge = MagicMock()
    db = MagicMock()
    consumer = EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        ws_bridge=bridge,
    )
    event = _write_event("/work/foo.md", "body")
    consumer._handle_tool_use("sess_parent", event)

    events = [c.args[0] for c in bridge.broadcast.call_args_list]
    activity_events = [e for e in events if e["event_type"] == "ACTIVITY"]
    assert len(activity_events) == 1
    assert activity_events[0]["payload"]["action"] == "TOOL_USE"


def test_ws_bridge_is_optional():
    """EventConsumer with no ws_bridge must not crash on any handler."""
    consumer = EventConsumer(
        db=MagicMock(),
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
    )
    consumer._handle_message("s", _message_event("hi"))
    consumer._handle_tool_use("s", _write_event("/work/x.md", "y"))
    consumer._handle_status_running("s", SimpleNamespace(type="session.status_running"))
    consumer._handle_status_idle("s", SimpleNamespace(type="session.status_idle", stop_reason=None))
