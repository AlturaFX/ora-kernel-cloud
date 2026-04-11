"""WebSocket event envelope and factories for the dashboard bridge.

The envelope format (``{id, event_type, payload, timestamp}``) and the
outbound event type constants are deliberately identical to the protocol
already spoken by forex-ml-platform's ``src/dashboard/orchestrator-client.js``
so that client can be instantiated a second time against this
orchestrator's bridge (ws://localhost:8002 by default) with zero code
changes. Any edit to the envelope shape or event type names is a
cross-repo breaking change.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Outbound event type constants (server -> client) ───────────────
# Mirrored from forex-ml/src/orchestration/events.py. Do not rename.
EVENT_SYSTEM_STATUS = "SYSTEM_STATUS"
EVENT_NODE_UPDATE = "NODE_UPDATE"
EVENT_EDGE_UPDATE = "EDGE_UPDATE"
EVENT_TREE_CHANGE = "TREE_CHANGE"
EVENT_HITL_NEEDED = "HITL_NEEDED"
EVENT_CHAT_RESPONSE = "CHAT_RESPONSE"
EVENT_CHAT_ACK = "CHAT_ACK"
EVENT_ACTIVITY = "ACTIVITY"
EVENT_TASKS_UPDATE = "TASKS_UPDATE"
# NOTE: forex-ml emits BA_CONTEXT for its business-analyst node responses.
# The cloud Kernel has no equivalent concept — agent.message events are
# forwarded as plain CHAT_RESPONSE — so BA_CONTEXT is intentionally omitted.

# ── Inbound event types (client -> server) ─────────────────────────
INBOUND_USER_MESSAGE = "USER_MESSAGE"
INBOUND_ABORT = "ABORT"
INBOUND_HITL_RESPONSE = "HITL_RESPONSE"

_VALID_INBOUND = {INBOUND_USER_MESSAGE, INBOUND_ABORT, INBOUND_HITL_RESPONSE}


# ── Envelope ────────────────────────────────────────────────────────

def make_envelope(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a ``{id, event_type, payload, timestamp}`` envelope."""
    return {
        "id": uuid.uuid4().hex,
        "event_type": event_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


# ── Outbound factories ──────────────────────────────────────────────

def system_status(
    session_id: str,
    status: str,
    uptime_seconds: Optional[float] = None,
    total_cost_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Parent Kernel session status changed (running / idle / terminated)."""
    payload: Dict[str, Any] = {"session_id": session_id, "status": status}
    if uptime_seconds is not None:
        payload["uptime_seconds"] = uptime_seconds
    if total_cost_usd is not None:
        payload["total_cost_usd"] = total_cost_usd
    return make_envelope(EVENT_SYSTEM_STATUS, payload)


def node_update(
    node_id: str,
    parent_id: Optional[str],
    node_name: str,
    status: str,
    tokens: Optional[Dict[str, int]] = None,
    cost_usd: Optional[float] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """A dispatch sub-session node changed state in the task DAG."""
    payload: Dict[str, Any] = {
        "node_id": node_id,
        "node_name": node_name,
        "status": status,
    }
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if tokens is not None:
        payload["tokens"] = tokens
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error is not None:
        payload["error"] = error
    return make_envelope(EVENT_NODE_UPDATE, payload)


def edge_update(from_id: str, to_id: str) -> Dict[str, Any]:
    """A dependency edge in the task DAG changed (usually created)."""
    return make_envelope(EVENT_EDGE_UPDATE, {"from_id": from_id, "to_id": to_id})


def hitl_needed(
    request_id: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    reason: str = "",
) -> Dict[str, Any]:
    """Human-in-the-loop approval requested for a tool call."""
    return make_envelope(
        EVENT_HITL_NEEDED,
        {
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "reason": reason,
        },
    )


def chat_response(session_id: str, text: str) -> Dict[str, Any]:
    """An agent.message from the parent Kernel, forwarded verbatim."""
    return make_envelope(
        EVENT_CHAT_RESPONSE,
        {"session_id": session_id, "text": text},
    )


def activity(
    session_id: str,
    action: str,
    details: Dict[str, Any],
) -> Dict[str, Any]:
    """A generic activity-log entry (tool use, tool result, etc.)."""
    return make_envelope(
        EVENT_ACTIVITY,
        {"session_id": session_id, "action": action, "details": details},
    )


# ── Inbound parser ──────────────────────────────────────────────────

def parse_inbound_event(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a ``USER_MESSAGE`` / ``ABORT`` / ``HITL_RESPONSE`` message.

    Returns ``None`` for anything malformed — unknown types, invalid
    JSON, missing fields, or a non-dict payload. The caller is expected
    to log and move on; the WS bridge never raises on a bad message.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ws inbound: invalid json")
        return None
    if not isinstance(obj, dict):
        return None
    event_type = obj.get("event_type")
    if event_type not in _VALID_INBOUND:
        logger.warning("ws inbound: unknown event_type=%r", event_type)
        return None
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        logger.warning("ws inbound: non-dict payload for %s", event_type)
        return None
    return {"event_type": event_type, "payload": payload}
