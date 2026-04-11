"""Tests for the WebSocket event envelope and factories.

The envelope shape is pinned by the forex-ml-platform dashboard's
OrchestratorClient — see docs/CLOUD_ARCHITECTURE.md for the field
contract. Any change here MUST keep forex-ml's existing client parsing.
"""
from __future__ import annotations

import json
import re

import pytest

from orchestrator.ws_events import (
    EVENT_ACTIVITY,
    EVENT_CHAT_RESPONSE,
    EVENT_EDGE_UPDATE,
    EVENT_HITL_NEEDED,
    EVENT_NODE_UPDATE,
    EVENT_SYSTEM_STATUS,
    activity,
    chat_response,
    edge_update,
    hitl_needed,
    make_envelope,
    node_update,
    parse_inbound_event,
    system_status,
)


# ── Envelope shape ──────────────────────────────────────────────────

def test_envelope_has_all_required_fields():
    env = make_envelope("SYSTEM_STATUS", {"status": "running"})
    assert set(env.keys()) == {"id", "event_type", "payload", "timestamp"}
    assert env["event_type"] == "SYSTEM_STATUS"
    assert env["payload"] == {"status": "running"}


def test_envelope_id_is_32_hex_chars():
    env = make_envelope("SYSTEM_STATUS", {})
    assert re.fullmatch(r"[0-9a-f]{32}", env["id"]) is not None


def test_envelope_ids_are_unique():
    ids = {make_envelope("ACTIVITY", {})["id"] for _ in range(100)}
    assert len(ids) == 100


def test_envelope_timestamp_is_iso_z_format():
    env = make_envelope("SYSTEM_STATUS", {})
    # forex-ml uses ISO 8601 with trailing Z, e.g. "2026-04-09T14:30:00Z"
    assert env["timestamp"].endswith("Z")
    assert "T" in env["timestamp"]
    # Should be UTC-naive ISO format
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
        env["timestamp"],
    ) is not None


def test_envelope_is_json_serializable():
    env = make_envelope("NODE_UPDATE", {"task_id": "abc", "status": "running"})
    json.dumps(env)  # must not raise


# ── Factories ───────────────────────────────────────────────────────

def test_system_status_factory():
    env = system_status(
        session_id="sesn_parent",
        status="running",
        uptime_seconds=123.4,
        total_cost_usd=0.25,
    )
    assert env["event_type"] == EVENT_SYSTEM_STATUS
    p = env["payload"]
    assert p["session_id"] == "sesn_parent"
    assert p["status"] == "running"
    assert p["uptime_seconds"] == 123.4
    assert p["total_cost_usd"] == 0.25


def test_system_status_omits_none_optional_fields():
    env = system_status(session_id="sesn_x", status="running")
    payload = env["payload"]
    assert payload == {"session_id": "sesn_x", "status": "running"}
    assert "uptime_seconds" not in payload
    assert "total_cost_usd" not in payload


def test_node_update_factory_for_dispatch_start():
    env = node_update(
        node_id="sesn_sub_1",
        parent_id="sesn_parent",
        node_name="business_analyst",
        status="running",
    )
    assert env["event_type"] == EVENT_NODE_UPDATE
    p = env["payload"]
    assert p["node_id"] == "sesn_sub_1"
    assert p["parent_id"] == "sesn_parent"
    assert p["node_name"] == "business_analyst"
    assert p["status"] == "running"


def test_node_update_factory_for_dispatch_complete():
    env = node_update(
        node_id="sesn_sub_1",
        parent_id="sesn_parent",
        node_name="business_analyst",
        status="complete",
        tokens={"input": 3000, "output": 500},
        cost_usd=0.0275,
        duration_ms=4800,
    )
    p = env["payload"]
    assert p["status"] == "complete"
    assert p["tokens"] == {"input": 3000, "output": 500}
    assert p["cost_usd"] == 0.0275
    assert p["duration_ms"] == 4800


def test_node_update_omits_none_optional_fields():
    env = node_update(
        node_id="sesn_sub",
        parent_id="sesn_parent",
        node_name="business_analyst",
        status="running",
    )
    payload = env["payload"]
    assert set(payload.keys()) == {"node_id", "parent_id", "node_name", "status"}
    assert "tokens" not in payload
    assert "cost_usd" not in payload
    assert "duration_ms" not in payload
    assert "error" not in payload


def test_node_update_omits_parent_id_when_none():
    """Root-session nodes pass parent_id=None; the key must be absent."""
    env = node_update(
        node_id="sesn_root",
        parent_id=None,
        node_name="kernel",
        status="running",
    )
    payload = env["payload"]
    assert "parent_id" not in payload


def test_edge_update_factory():
    env = edge_update(from_id="sesn_parent", to_id="sesn_sub_1")
    assert env["event_type"] == EVENT_EDGE_UPDATE
    assert env["payload"]["from_id"] == "sesn_parent"
    assert env["payload"]["to_id"] == "sesn_sub_1"


def test_hitl_needed_factory():
    env = hitl_needed(
        request_id="tu_abc",
        tool_name="Write",
        tool_input={"file_path": "/work/x", "content": "y"},
        reason="Protected file write",
    )
    assert env["event_type"] == EVENT_HITL_NEEDED
    p = env["payload"]
    assert p["request_id"] == "tu_abc"
    assert p["tool_name"] == "Write"
    assert p["tool_input"] == {"file_path": "/work/x", "content": "y"}
    assert p["reason"] == "Protected file write"


def test_chat_response_factory():
    env = chat_response(session_id="sesn_parent", text="Hello operator.")
    assert env["event_type"] == EVENT_CHAT_RESPONSE
    assert env["payload"]["session_id"] == "sesn_parent"
    assert env["payload"]["text"] == "Hello operator."


def test_activity_factory():
    env = activity(
        session_id="sesn_parent",
        action="TOOL_USE",
        details={"tool_name": "bash", "input": "ls"},
    )
    assert env["event_type"] == EVENT_ACTIVITY
    p = env["payload"]
    assert p["session_id"] == "sesn_parent"
    assert p["action"] == "TOOL_USE"
    assert p["details"] == {"tool_name": "bash", "input": "ls"}


# ── Inbound parser ──────────────────────────────────────────────────

def test_parse_inbound_valid_user_message():
    raw = json.dumps({
        "event_type": "USER_MESSAGE",
        "payload": {"text": "Hi kernel"},
    })
    event = parse_inbound_event(raw)
    assert event is not None
    assert event["event_type"] == "USER_MESSAGE"
    assert event["payload"]["text"] == "Hi kernel"


def test_parse_inbound_valid_abort():
    event = parse_inbound_event('{"event_type": "ABORT", "payload": {}}')
    assert event["event_type"] == "ABORT"


def test_parse_inbound_valid_hitl_response():
    raw = json.dumps({
        "event_type": "HITL_RESPONSE",
        "payload": {
            "request_id": "tu_abc",
            "decision": "approve",
            "reason": "looks fine",
        },
    })
    event = parse_inbound_event(raw)
    assert event["event_type"] == "HITL_RESPONSE"
    assert event["payload"]["decision"] == "approve"


def test_parse_inbound_rejects_invalid_json():
    assert parse_inbound_event("not json") is None


def test_parse_inbound_rejects_missing_event_type():
    assert parse_inbound_event('{"payload": {}}') is None


def test_parse_inbound_rejects_non_dict_payload():
    assert parse_inbound_event(
        '{"event_type": "USER_MESSAGE", "payload": "string"}'
    ) is None


def test_parse_inbound_rejects_non_dict_toplevel():
    # Valid JSON, but top-level is not a dict
    assert parse_inbound_event("[]") is None
    assert parse_inbound_event('"bare string"') is None
    assert parse_inbound_event("42") is None


def test_parse_inbound_rejects_unknown_event_type():
    raw = json.dumps({"event_type": "UNKNOWN_THING", "payload": {}})
    assert parse_inbound_event(raw) is None
