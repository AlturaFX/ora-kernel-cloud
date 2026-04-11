"""Tests for WebSocketHitlHandler.

The handler turns a tool_confirmation SSE event into a HITL_NEEDED
envelope on the WS bridge, then blocks until either a matching
HITL_RESPONSE arrives or a timeout fires. Because the handler is
invoked from the SSE loop thread, we assert behavior with a mock
bridge and a separate thread that plays the dashboard's role.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.ws_hitl import WebSocketHitlHandler


def _make_event(tool_use_id="tu_123", tool_name="Write", raw_input=None):
    return SimpleNamespace(
        tool_use_id=tool_use_id,
        name=tool_name,
        input=raw_input or {"file_path": "/work/x", "content": "y"},
    )


def test_handler_broadcasts_hitl_needed():
    bridge = MagicMock()
    bridge.client_count = 1
    send_response = MagicMock()
    handler = WebSocketHitlHandler(
        ws_bridge=bridge, send_response=send_response, timeout_seconds=0.2
    )

    # Kick off handle() in a thread because it blocks for a response
    thread = threading.Thread(target=handler.handle, args=(_make_event(),))
    thread.start()
    # Give the thread a moment to broadcast
    time.sleep(0.05)

    # Broadcast should have been called with a HITL_NEEDED envelope
    assert bridge.broadcast.called
    call = bridge.broadcast.call_args
    envelope = call.args[0]
    assert envelope["event_type"] == "HITL_NEEDED"
    assert envelope["payload"]["request_id"] == "tu_123"

    # Let the handler timeout and complete
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    # On timeout, send_response should have been called with approved=False
    send_response.assert_called_once()
    args = send_response.call_args.args
    assert args[0] == "tu_123"
    assert args[1] is False  # denied
    assert "timeout" in args[2].lower()


def test_handler_resumes_on_response():
    bridge = MagicMock()
    bridge.client_count = 1
    send_response = MagicMock()
    handler = WebSocketHitlHandler(
        ws_bridge=bridge, send_response=send_response, timeout_seconds=2.0
    )

    def trigger_response_soon():
        time.sleep(0.05)
        handler.on_response({
            "request_id": "tu_abc",
            "decision": "approve",
            "reason": "all good",
        })

    responder = threading.Thread(target=trigger_response_soon)
    responder.start()

    handler.handle(_make_event(tool_use_id="tu_abc"))
    responder.join()

    send_response.assert_called_once_with("tu_abc", True, "all good")


def test_handler_ignores_mismatched_response_id():
    bridge = MagicMock()
    bridge.client_count = 1
    send_response = MagicMock()
    handler = WebSocketHitlHandler(
        ws_bridge=bridge, send_response=send_response, timeout_seconds=0.2
    )

    def wrong_then_right():
        time.sleep(0.03)
        handler.on_response({"request_id": "tu_WRONG", "decision": "approve"})
        time.sleep(0.03)
        handler.on_response({"request_id": "tu_right", "decision": "deny", "reason": "no"})

    t = threading.Thread(target=wrong_then_right)
    t.start()
    handler.handle(_make_event(tool_use_id="tu_right"))
    t.join()

    send_response.assert_called_once_with("tu_right", False, "no")


def test_handler_with_no_clients_falls_back_to_deny():
    """If no dashboard is connected, don't block waiting — deny fast."""
    bridge = MagicMock()
    bridge.client_count = 0
    send_response = MagicMock()
    handler = WebSocketHitlHandler(
        ws_bridge=bridge, send_response=send_response, timeout_seconds=5.0
    )

    t_start = time.time()
    handler.handle(_make_event())
    elapsed = time.time() - t_start

    assert elapsed < 0.5  # Didn't wait for timeout
    send_response.assert_called_once()
    assert send_response.call_args.args[1] is False
    assert "no dashboard" in send_response.call_args.args[2].lower()
