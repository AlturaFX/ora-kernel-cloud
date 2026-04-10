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


def _write_event(file_path, content):
    return SimpleNamespace(
        type="agent.tool_use",
        name="Write",
        input={"file_path": file_path, "content": content},
    )


def _edit_event(file_path, old, new):
    return SimpleNamespace(
        type="agent.tool_use",
        name="Edit",
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
