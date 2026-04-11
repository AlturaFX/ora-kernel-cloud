# Dashboard WebSocket Bridge — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give ora-kernel-cloud a dashboard-facing WebSocket bridge + HTTP companion API that the forex-ml-platform dashboard's existing `OrchestratorClient` JavaScript class can consume without code changes, so operators get a real-time view of the parent Kernel session, dispatch sub-sessions, HITL prompts, activity stream, and file-sync state.

**Architecture:**
- **WebSocket bridge (port 8002).** A `WebSocketBridge` running in a background asyncio thread broadcasts `{id, event_type, payload, timestamp}` envelopes to connected clients. The envelope format and event types are deliberately identical to the ones already handled by `forex-ml-platform/src/dashboard/orchestrator-client.js` (`NODE_UPDATE`, `EDGE_UPDATE`, `TREE_CHANGE`, `HITL_NEEDED`, `CHAT_RESPONSE`, `SYSTEM_STATUS`, `ACTIVITY`, `TASKS_UPDATE`) so the dashboard side can instantiate the class twice — once against `ws://localhost:8000` for forex-ml, once against `ws://localhost:8002` for ora-kernel-cloud — with zero protocol divergence. `EventConsumer`, `DispatchManager`, and `SessionManager` all grow an optional `ws_bridge` dependency and emit events on state changes through a thread-safe broadcast helper.
- **HTTP companion API (port 8003).** A small stdlib-based `ThreadingHTTPServer` exposes five JSON endpoints the dashboard will poll for panel data the WS protocol doesn't carry naturally: parent session totals, recent dispatches, file sync state, dispatch agent cache, health. Mirrors the port 8001 pattern forex-ml already uses.
- **HITL handler swap.** A new `WebSocketHitlHandler` delegates tool_confirmation events to the WS bridge as `HITL_NEEDED` envelopes and waits for a matching `HITL_RESPONSE` inbound message. `__main__` selects between WS and stdin handlers at startup — WS when the bridge is enabled, stdin as a fallback for headless runs.
- **Phase split.** This plan is **Phase A only** — the orchestrator-side bridge + API. The dashboard tab wiring (a new "Cloud Kernel" tab in forex-ml-platform's monolithic `dashboard.html`, plus a second `OrchestratorClient` instantiation pointed at port 8002) is Phase B, implemented against the forex-ml-platform repo as a separate plan.

**Tech Stack:** Python 3.10+, `websockets>=12.0` (already in requirements.txt), stdlib `http.server.ThreadingHTTPServer`, stdlib `threading`, pytest 9. No new runtime dependencies.

**Architectural constraints respected:**
- **Invariant 1** (container never speaks to postgres) — unchanged, the bridge runs entirely on the operator's machine.
- **Invariant 3** (case-insensitive tool names) — irrelevant to this work; the bridge operates on events from the existing consumers which already handle casing.
- **Thread safety.** The SSE event loop and the WS bridge run in separate threads. All cross-thread communication uses `asyncio.run_coroutine_threadsafe` against the bridge's private event loop — no shared mutable state.
- **Protocol lock-step.** The event envelope and event type constants live in one file (`orchestrator/ws_events.py`) and are imported by both the bridge and the consumers. Adding a new event type is a one-file change.
- **Graceful degradation.** If the WS bridge fails to start, the orchestrator continues running with the stdin HITL handler and a `logger.warning`. One-way failure: bridge down does not kill orchestrator.

**Dependency on Phase B:** None. This plan produces working, testable software on its own — a Python WS client + HTTP client can fully exercise every endpoint. The live smoke test in Task 17 uses `websockets.connect` + `urllib.request` as a stand-in for the dashboard, with no dashboard repo involvement.

**Grounded in exploration (2026-04-10):**
- forex-ml's `src/dashboard/orchestrator-client.js` uses `ws://localhost:8000` (hard-coded at line 29) but the constructor already takes `graphContainerId` + `hudIds` so it's cleanly multi-instantiable. Phase B will parameterize the URL.
- Envelope format from forex-ml's `src/orchestration/events.py`: `{id: "32-char-hex-uuid", event_type: "TASKS_UPDATE", payload: {...}, timestamp: "2026-04-09T14:30:00Z"}`.
- Inbound event types from `OrchestratorClient`: `USER_MESSAGE`, `ABORT` (plus we add `HITL_RESPONSE`).
- Outbound event types: `SYSTEM_STATUS`, `NODE_UPDATE`, `EDGE_UPDATE`, `TREE_CHANGE`, `HITL_NEEDED`, `CHAT_RESPONSE`, `CHAT_ACK`, `BA_CONTEXT`, `ACTIVITY`, `TASKS_UPDATE`.

---

## File Structure

**New files:**
- `orchestrator/ws_events.py` — envelope definition, outbound event type constants, factory functions (`node_update`, `edge_update`, `system_status`, `hitl_needed`, `chat_response`, `activity`), inbound event parser. Pure functions, zero dependencies beyond stdlib.
- `orchestrator/ws_bridge.py` — `WebSocketBridge` class wrapping `websockets.serve` on a background thread; `broadcast(event)` thread-safe entry point; inbound callback registry (`on_user_message`, `on_abort`, `on_hitl_response`); client set with graceful client drop on disconnect.
- `orchestrator/http_api.py` — `PanelApiServer` class using `http.server.ThreadingHTTPServer` on a background thread with five `GET` handlers; takes a `Database` and queries live.
- `orchestrator/ws_hitl.py` — `WebSocketHitlHandler` with the same `.handle(event)` interface as `StdinHitlHandler`, delegates to WS bridge + blocking wait for response.
- `orchestrator/tests/test_ws_events.py` — envelope + parser + factory unit tests (pure).
- `orchestrator/tests/test_ws_bridge.py` — spin up a real `WebSocketBridge` on an ephemeral port, connect a real client via `websockets.connect`, assert round-trip behavior.
- `orchestrator/tests/test_http_api.py` — spin up a real `PanelApiServer` on an ephemeral port, hit endpoints via `urllib.request`, assert JSON shapes against a mocked `Database`.
- `orchestrator/tests/test_ws_hitl.py` — `WebSocketHitlHandler` with a mock WS bridge.

**Modified files:**
- `orchestrator/event_consumer.py` — accept optional `ws_bridge: WebSocketBridge` param; emit `SYSTEM_STATUS`, `ACTIVITY`, and `CHAT_RESPONSE` events on the corresponding SSE event types.
- `orchestrator/dispatch.py` — accept optional `ws_bridge: WebSocketBridge` param; emit `NODE_UPDATE` / `EDGE_UPDATE` on dispatch start, complete, fail.
- `orchestrator/db.py` — add four read-only helper methods for the HTTP API: `get_current_parent_session()`, `get_recent_dispatches(limit)`, `get_file_sync_state()`, `list_dispatch_agents()`.
- `orchestrator/__main__.py` — construct `WebSocketBridge` and `PanelApiServer`; select `WebSocketHitlHandler` when the bridge is enabled, `StdinHitlHandler` otherwise; wire both into `EventConsumer` + `DispatchManager`; call `bridge.stop()` + `api.stop()` in the shutdown handler.
- `config.yaml` — add `dashboard.http_api_port: 8003` and `dashboard.enabled: true` toggle.

---

## Task 1: Event envelope + factories — failing tests

**Files:**
- Create: `orchestrator/tests/test_ws_events.py`

- [ ] **Step 1: Write the failing tests**

Create `orchestrator/tests/test_ws_events.py`:

```python
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
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z",
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
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `python3 -m pytest orchestrator/tests/test_ws_events.py -v`
Expected: `ModuleNotFoundError: No module named 'orchestrator.ws_events'`.

---

## Task 2: Event envelope + factories — implementation

**Files:**
- Create: `orchestrator/ws_events.py`

- [ ] **Step 1: Write the module**

Create `orchestrator/ws_events.py`:

```python
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
        "parent_id": parent_id,
        "node_name": node_name,
        "status": status,
    }
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
```

- [ ] **Step 2: Run the tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_ws_events.py -v`
Expected: 18 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/ws_events.py orchestrator/tests/test_ws_events.py
git commit -m "feat(ws): event envelope + factories for dashboard bridge"
```

---

## Task 3: `WebSocketBridge` — failing tests

**Files:**
- Create: `orchestrator/tests/test_ws_bridge.py`

- [ ] **Step 1: Write the failing tests**

Create `orchestrator/tests/test_ws_bridge.py`:

```python
"""Integration tests for WebSocketBridge using real websockets.

These tests spin up a real bridge on an ephemeral port, connect real
clients via the websockets library, and assert round-trip behavior.
We use the library for both server and client because the bridge's
threading model is the main thing we want to verify — unit-mocking
websockets would test nothing useful.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets

from orchestrator.ws_bridge import WebSocketBridge
from orchestrator.ws_events import (
    INBOUND_ABORT,
    INBOUND_HITL_RESPONSE,
    INBOUND_USER_MESSAGE,
    system_status,
)


@pytest.fixture
def bridge():
    """Start a WebSocketBridge on an ephemeral port and tear it down after."""
    b = WebSocketBridge(host="127.0.0.1", port=0)  # port=0 -> OS picks
    b.start()
    # Wait up to 2s for the bridge to become ready
    deadline = time.time() + 2.0
    while b.port is None and time.time() < deadline:
        time.sleep(0.01)
    assert b.port is not None, "bridge failed to bind"
    yield b
    b.stop()


def _run(coro):
    """Run a coroutine in a fresh event loop (for use from sync tests)."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_bridge_binds_and_reports_port(bridge):
    assert bridge.port > 0


def test_client_can_connect_and_disconnect(bridge):
    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            assert ws.open
    _run(go())
    # After the 'async with' block exits, the bridge should have no clients
    # Give the bridge's event loop a moment to process the disconnect
    time.sleep(0.05)
    assert bridge.client_count == 0


def test_broadcast_reaches_connected_client(bridge):
    received = []

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            # Give the bridge a moment to register the client
            await asyncio.sleep(0.05)
            bridge.broadcast(system_status("sesn_test", "running"))
            # Wait up to 1s for the broadcast to arrive
            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
            received.append(json.loads(msg))

    _run(go())
    assert len(received) == 1
    assert received[0]["event_type"] == "SYSTEM_STATUS"
    assert received[0]["payload"]["session_id"] == "sesn_test"


def test_broadcast_with_no_clients_is_noop(bridge):
    # Should not raise, should not block
    bridge.broadcast(system_status("sesn_test", "idle"))
    assert bridge.client_count == 0


def test_broadcast_fans_out_to_multiple_clients(bridge):
    received_a = []
    received_b = []

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws_a, \
                   websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws_b:
            await asyncio.sleep(0.05)
            bridge.broadcast(system_status("sesn_test", "running"))
            received_a.append(json.loads(await asyncio.wait_for(ws_a.recv(), 1.0)))
            received_b.append(json.loads(await asyncio.wait_for(ws_b.recv(), 1.0)))

    _run(go())
    assert received_a[0]["event_type"] == "SYSTEM_STATUS"
    assert received_b[0]["event_type"] == "SYSTEM_STATUS"


def test_inbound_user_message_invokes_callback(bridge):
    received = []
    bridge.on_user_message = lambda payload: received.append(payload)

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({
                "event_type": INBOUND_USER_MESSAGE,
                "payload": {"text": "hello from test"},
            }))
            await asyncio.sleep(0.2)  # let the handler process

    _run(go())
    assert len(received) == 1
    assert received[0]["text"] == "hello from test"


def test_inbound_abort_invokes_callback(bridge):
    called = []
    bridge.on_abort = lambda: called.append(True)

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({"event_type": INBOUND_ABORT, "payload": {}}))
            await asyncio.sleep(0.2)

    _run(go())
    assert called == [True]


def test_inbound_hitl_response_invokes_callback(bridge):
    received = []
    bridge.on_hitl_response = lambda payload: received.append(payload)

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({
                "event_type": INBOUND_HITL_RESPONSE,
                "payload": {"request_id": "tu_1", "decision": "approve", "reason": ""},
            }))
            await asyncio.sleep(0.2)

    _run(go())
    assert received[0]["decision"] == "approve"


def test_inbound_invalid_json_is_ignored(bridge):
    """The bridge must not crash on malformed messages."""
    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            await asyncio.sleep(0.05)
            await ws.send("not json")
            await asyncio.sleep(0.2)
            # Still connected, can send another valid message
            assert ws.open

    _run(go())
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `python3 -m pytest orchestrator/tests/test_ws_bridge.py -v`
Expected: `ModuleNotFoundError: No module named 'orchestrator.ws_bridge'`.

---

## Task 4: `WebSocketBridge` — implementation

**Files:**
- Create: `orchestrator/ws_bridge.py`

- [ ] **Step 1: Write the module**

Create `orchestrator/ws_bridge.py`:

```python
"""WebSocket bridge between the orchestrator and the dashboard.

Architecture
------------
The bridge runs ``websockets.serve`` on a private asyncio event loop
inside a dedicated daemon thread. The orchestrator's main event loop
(SSE consumer, dispatch manager, scheduler) runs in a different thread
and broadcasts events by calling ``bridge.broadcast(envelope)``, which
schedules a send coroutine on the bridge's loop via
``asyncio.run_coroutine_threadsafe``.

This keeps the SSE loop blocking and synchronous (unchanged) while
exposing an async network surface to dashboard clients. No shared
mutable state between threads — the client set is only touched from
the bridge loop.

Callbacks
---------
``on_user_message(payload)``, ``on_abort()``, and ``on_hitl_response(payload)``
are sync callbacks the orchestrator registers. They run on the bridge
loop; callbacks that need to affect the SSE loop should do their own
thread marshalling (typically via ``SessionManager.send_message``,
which is already thread-safe because it makes an HTTP call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Dict, Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from orchestrator.ws_events import parse_inbound_event

logger = logging.getLogger(__name__)


class WebSocketBridge:
    """Thread-safe WebSocket broadcaster for dashboard clients.

    Parameters
    ----------
    host : str
        Interface to bind on. Default ``127.0.0.1``.
    port : int
        TCP port. Pass ``0`` to let the OS pick one (tests use this).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8002):
        self.host = host
        self._requested_port = port
        self.port: Optional[int] = None  # Populated once bound

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[websockets.server.Serve] = None
        self._clients: Set[WebSocketServerProtocol] = set()
        self._shutdown = threading.Event()

        # Inbound callbacks — assigned by the orchestrator at wire-up time
        self.on_user_message: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_abort: Optional[Callable[[], None]] = None
        self.on_hitl_response: Optional[Callable[[Dict[str, Any]], None]] = None

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the bridge in a background daemon thread."""
        if self._thread is not None:
            return  # already running
        self._thread = threading.Thread(
            target=self._thread_main, name="ws-bridge", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the bridge gracefully."""
        if self._loop is None or self._thread is None:
            return
        self._shutdown.set()
        asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None
        self._server = None
        self.port = None

    async def _shutdown_async(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        # Close any stragglers
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()

    def _thread_main(self) -> None:
        """Entry point for the bridge's background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve_forever())
        except Exception:
            logger.exception("ws_bridge: background loop crashed")
        finally:
            self._loop.close()

    async def _serve_forever(self) -> None:
        async with websockets.serve(self._handler, self.host, self._requested_port) as server:
            self._server = server
            # websockets.serve returns a WebSocketServer whose sockets
            # attribute tells us the bound port
            self.port = list(server.sockets)[0].getsockname()[1]
            logger.info("ws_bridge: listening on ws://%s:%d", self.host, self.port)
            # Sleep until shutdown is requested
            while not self._shutdown.is_set():
                await asyncio.sleep(0.1)

    # ── Client handler ─────────────────────────────────────────────

    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        self._clients.add(ws)
        logger.info("ws_bridge: client connected (%d total)", len(self._clients))
        try:
            async for raw in ws:
                event = parse_inbound_event(raw)
                if event is None:
                    continue
                et = event["event_type"]
                payload = event["payload"]
                try:
                    if et == "USER_MESSAGE" and self.on_user_message is not None:
                        self.on_user_message(payload)
                    elif et == "ABORT" and self.on_abort is not None:
                        self.on_abort()
                    elif et == "HITL_RESPONSE" and self.on_hitl_response is not None:
                        self.on_hitl_response(payload)
                except Exception:
                    logger.exception("ws_bridge: callback for %s failed", et)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("ws_bridge: client disconnected (%d total)", len(self._clients))

    # ── Broadcast API (thread-safe) ────────────────────────────────

    def broadcast(self, envelope: Dict[str, Any]) -> None:
        """Send an envelope to every connected client.

        Safe to call from any thread — marshalls onto the bridge loop.
        No-op if the bridge is not running or no clients are connected.
        """
        if self._loop is None or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast_async(envelope), self._loop
        )

    async def _broadcast_async(self, envelope: Dict[str, Any]) -> None:
        if not self._clients:
            return
        message = json.dumps(envelope, default=str)
        dead: Set[WebSocketServerProtocol] = set()
        for ws in self._clients:
            try:
                await ws.send(message)
            except websockets.ConnectionClosed:
                dead.add(ws)
            except Exception:
                logger.exception("ws_bridge: send failed, marking client dead")
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)
```

- [ ] **Step 2: Run tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_ws_bridge.py -v`
Expected: 9 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/ws_bridge.py orchestrator/tests/test_ws_bridge.py
git commit -m "feat(ws): WebSocketBridge with thread-safe broadcast"
```

---

## Task 5: Hook WS bridge into `EventConsumer` — failing test

**Files:**
- Modify: `orchestrator/tests/test_event_consumer_cdc.py`

- [ ] **Step 1: Append bridge-routing tests**

Append to `orchestrator/tests/test_event_consumer_cdc.py`:

```python
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
    from types import SimpleNamespace
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
    from types import SimpleNamespace
    consumer._handle_status_running("s", SimpleNamespace(type="session.status_running"))
```

- [ ] **Step 2: Run — expect failures on missing `ws_bridge` kwarg**

Run: `python3 -m pytest orchestrator/tests/test_event_consumer_cdc.py -v -k "bridge or broadcasts"`
Expected: 3 failures on `unexpected keyword argument 'ws_bridge'`.

---

## Task 6: Hook WS bridge into `EventConsumer` — implementation

**Files:**
- Modify: `orchestrator/event_consumer.py`

- [ ] **Step 1: Add `ws_bridge` param + import**

In `orchestrator/event_consumer.py`, update the `TYPE_CHECKING` block:

```python
if TYPE_CHECKING:
    from orchestrator.dispatch import DispatchManager
    from orchestrator.file_sync import FileSync
    from orchestrator.ws_bridge import WebSocketBridge
```

Add the import at module scope for the factory functions (these are lightweight, no cycle):

```python
from orchestrator import ws_events
```

Update the `EventConsumer.__init__` signature:

```python
    def __init__(
        self,
        db: Database,
        api_key: str,
        agent_id: str,
        environment_id: str,
        on_event: Optional[Callable[[Any], None]] = None,
        on_hitl_needed: Optional[Callable[[Any], None]] = None,
        file_sync: Optional["FileSync"] = None,
        dispatch_manager: Optional["DispatchManager"] = None,
        ws_bridge: Optional["WebSocketBridge"] = None,
    ):
        self.db = db
        self.client = Anthropic(api_key=api_key)
        self.agent_id = agent_id
        self.environment_id = environment_id
        self.on_event = on_event
        self.on_hitl_needed = on_hitl_needed
        self.file_sync = file_sync
        self.dispatch_manager = dispatch_manager
        self.ws_bridge = ws_bridge
        self.totals = SessionTotals()
```

- [ ] **Step 2: Broadcast from `_handle_message`**

In `_handle_message`, after the existing `log_activity` call and before the file_sync routing, add:

```python
        if self.ws_bridge is not None and full_text:
            self.ws_bridge.broadcast(
                ws_events.chat_response(session_id=session_id, text=full_text)
            )
```

- [ ] **Step 3: Broadcast from `_handle_tool_use`**

In `_handle_tool_use`, immediately after the `db.log_activity` call and before the CDC block, add:

```python
        if self.ws_bridge is not None:
            self.ws_bridge.broadcast(
                ws_events.activity(
                    session_id=session_id,
                    action="TOOL_USE",
                    details={"tool_name": tool_name, "input": input_preview},
                )
            )
```

- [ ] **Step 4: Broadcast from `_handle_status_running` and `_handle_status_idle`**

In `_handle_status_running`, after the `db.upsert_cloud_session` call, add:

```python
        if self.ws_bridge is not None:
            self.ws_bridge.broadcast(
                ws_events.system_status(
                    session_id=session_id,
                    status="running",
                    total_cost_usd=self.totals.cost_usd,
                )
            )
```

In `_handle_status_idle`, after the `db.log_activity` call, add:

```python
        if self.ws_bridge is not None:
            self.ws_bridge.broadcast(
                ws_events.system_status(
                    session_id=session_id,
                    status="idle",
                    total_cost_usd=self.totals.cost_usd,
                )
            )
```

- [ ] **Step 5: Run the full event_consumer test file**

Run: `python3 -m pytest orchestrator/tests/test_event_consumer_cdc.py -v`
Expected: all tests pass (previous tests + 4 new).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/event_consumer.py orchestrator/tests/test_event_consumer_cdc.py
git commit -m "feat(event_consumer): broadcast SYSTEM_STATUS/CHAT_RESPONSE/ACTIVITY to ws_bridge"
```

---

## Task 7: Hook WS bridge into `DispatchManager` — failing tests

**Files:**
- Modify: `orchestrator/tests/test_dispatch.py`

- [ ] **Step 1: Append bridge-routing tests**

Append to `orchestrator/tests/test_dispatch.py`:

```python
# ── ws_bridge routing ──────────────────────────────────────────────


def _make_manager_with_bridge(tmp_path, **overrides):
    manager, db, client, send_to_parent = _make_manager(tmp_path, **overrides)
    bridge = MagicMock()
    manager.ws_bridge = bridge
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
    # We expect at least: NODE_UPDATE running, EDGE_UPDATE, NODE_UPDATE complete
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
    assert any(u["payload"]["status"] == "failed" for u in node_updates)


def test_dispatch_manager_ws_bridge_is_optional(tmp_path):
    """DispatchManager with ws_bridge=None must not crash."""
    manager, *_ = _make_manager(tmp_path)
    assert manager.ws_bridge is None  # Default
    # Should not raise
    manager.handle_message("sesn_parent", "no fences here")
```

- [ ] **Step 2: Run — expect failures on missing `ws_bridge` attribute**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "bridge or broadcasts"`
Expected: 3 failures or errors.

---

## Task 8: Hook WS bridge into `DispatchManager` — implementation

**Files:**
- Modify: `orchestrator/dispatch.py`

- [ ] **Step 1: Add `ws_bridge` param to `__init__`**

In `orchestrator/dispatch.py`, update `DispatchManager.__init__` signature:

```python
    def __init__(
        self,
        db,
        client,
        environment_id: str,
        send_to_parent: SendToParent,
        node_spec_dir: Path,
        max_dispatch_seconds: float = 600.0,
        stream_read_timeout_seconds: float = 180.0,
        ws_bridge=None,
    ):
        self.db = db
        self.client = client
        self.environment_id = environment_id
        self.send_to_parent = send_to_parent
        self.node_spec_dir = Path(node_spec_dir)
        self.max_dispatch_seconds = max_dispatch_seconds
        self.stream_read_timeout_seconds = stream_read_timeout_seconds
        self.ws_bridge = ws_bridge
```

Add the import at the top of `orchestrator/dispatch.py`:

```python
from orchestrator import ws_events
```

- [ ] **Step 2: Broadcast NODE_UPDATE(running) + EDGE_UPDATE on dispatch start**

In `_run_sub_session`, immediately after the `self.db.record_dispatch_start(...)` call and before the `prompt_text = json.dumps(...)` line, add:

```python
        if self.ws_bridge is not None:
            self.ws_bridge.broadcast(
                ws_events.node_update(
                    node_id=sub_session_id,
                    parent_id=parent_session_id,
                    node_name=node_name,
                    status="running",
                )
            )
            self.ws_bridge.broadcast(
                ws_events.edge_update(
                    from_id=parent_session_id, to_id=sub_session_id
                )
            )
```

- [ ] **Step 3: Broadcast NODE_UPDATE(complete) on success**

In `_run_sub_session`, immediately before the final `return` for the success case (after `db.record_dispatch_complete`), add:

```python
        if self.ws_bridge is not None:
            self.ws_bridge.broadcast(
                ws_events.node_update(
                    node_id=sub_session_id,
                    parent_id=parent_session_id,
                    node_name=node_name,
                    status="complete",
                    tokens={"input": input_tokens, "output": output_tokens},
                    cost_usd=cost_usd,
                    duration_ms=duration_ms,
                )
            )
```

- [ ] **Step 4: Broadcast NODE_UPDATE(failed) on termination**

In `_run_sub_session`, immediately before the failure `return` (after `db.record_dispatch_failure`), add:

```python
        if self.ws_bridge is not None:
            self.ws_bridge.broadcast(
                ws_events.node_update(
                    node_id=sub_session_id,
                    parent_id=parent_session_id,
                    node_name=node_name,
                    status="failed",
                    tokens={"input": input_tokens, "output": output_tokens},
                    cost_usd=cost_usd,
                    duration_ms=duration_ms,
                    error=terminated_error,
                )
            )
```

- [ ] **Step 5: Broadcast NODE_UPDATE(failed) from `handle_message` exception paths**

In `handle_message`, inside the `except FileNotFoundError` block (and the generic `except Exception` block), after the `result = { ... }` assignment and BEFORE `self.send_to_parent(...)`, add:

```python
            if self.ws_bridge is not None:
                self.ws_bridge.broadcast(
                    ws_events.node_update(
                        node_id=result.get("sub_session_id") or f"failed:{node_name}",
                        parent_id=parent_session_id,
                        node_name=node_name,
                        status="failed",
                        error=result.get("error"),
                    )
                )
```

- [ ] **Step 6: Run full dispatch test suite**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v`
Expected: all tests pass (19 original + 3 new).

- [ ] **Step 7: Commit**

```bash
git add orchestrator/dispatch.py orchestrator/tests/test_dispatch.py
git commit -m "feat(dispatch): broadcast NODE_UPDATE/EDGE_UPDATE on dispatch lifecycle"
```

---

## Task 9: HTTP API helpers in `db.py` — failing tests

**Files:**
- Modify: `orchestrator/tests/test_db_dispatch.py`

- [ ] **Step 1: Append read-only helper tests**

Append to `orchestrator/tests/test_db_dispatch.py`:

```python
# ── HTTP API helpers ────────────────────────────────────────────────


def test_get_current_parent_session_returns_none_when_empty(db):
    # Use a fresh session_id guaranteed not to exist
    unique_sid = f"nope_{uuid.uuid4().hex[:8]}"
    # Directly query the helper — it should fall back to the latest row
    row = db.get_current_parent_session(preferred_session_id=unique_sid)
    # Either returns None (empty table) or returns whatever latest row exists
    assert row is None or "session_id" in row


def test_get_current_parent_session_finds_by_id(db):
    sid = f"sesn_test_{uuid.uuid4().hex[:8]}"
    db.upsert_cloud_session("agent_x", "env_x", sid, "running")

    row = db.get_current_parent_session(preferred_session_id=sid)
    assert row is not None
    assert row["session_id"] == sid
    assert row["status"] == "running"


def test_get_recent_dispatches_respects_limit(db):
    parent = f"sesn_rd_{uuid.uuid4().hex[:8]}"
    for i in range(5):
        sub = f"sub_{uuid.uuid4().hex[:8]}"
        db.record_dispatch_start(
            sub_session_id=sub,
            parent_session_id=parent,
            node_name=f"node_{i}",
            input_data={},
        )

    rows = db.get_recent_dispatches(limit=3, parent_session_id=parent)
    assert len(rows) == 3
    # Most recent first
    assert rows[0]["node_name"] == "node_4"


def test_get_file_sync_state_returns_all_rows(db):
    path = f".claude/kernel/journal/test_{uuid.uuid4().hex[:8]}.md"
    db.sync_file(path, "content-body", synced_from="cdc")

    rows = db.get_file_sync_state()
    paths = [r["file_path"] for r in rows]
    assert path in paths


def test_list_dispatch_agents(db):
    node = _random_node()
    db.upsert_dispatch_agent(node, agent_id="agent_list_test", prompt_hash="ha")

    rows = db.list_dispatch_agents()
    names = [r["node_name"] for r in rows]
    assert node in names
```

- [ ] **Step 2: Run — expect failures on missing methods**

Run: `python3 -m pytest orchestrator/tests/test_db_dispatch.py -v -k "get_current or get_recent or get_file_sync or list_dispatch_agents"`
Expected: failures on `AttributeError` for missing methods.

---

## Task 10: HTTP API helpers in `db.py` — implementation

**Files:**
- Modify: `orchestrator/db.py`

- [ ] **Step 1: Append four read-only helper methods**

At the end of the `Database` class in `orchestrator/db.py`, append:

```python
    # ── HTTP API read-only helpers ────────────────────────────────────

    def get_current_parent_session(
        self, preferred_session_id: Optional[str] = None
    ) -> Optional[dict]:
        """Return the current parent cloud_sessions row.

        If ``preferred_session_id`` is given, look it up directly.
        Otherwise return the most recently updated row (by last_event_at
        fallback to created_at).
        """
        with self.cursor() as cur:
            if preferred_session_id is not None:
                cur.execute(
                    """
                    SELECT agent_id, environment_id, session_id, status,
                           total_input_tokens, total_output_tokens, total_cost_usd,
                           created_at, last_event_at
                    FROM cloud_sessions
                    WHERE session_id = %s
                    """,
                    (preferred_session_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    return row
            cur.execute(
                """
                SELECT agent_id, environment_id, session_id, status,
                       total_input_tokens, total_output_tokens, total_cost_usd,
                       created_at, last_event_at
                FROM cloud_sessions
                ORDER BY COALESCE(last_event_at, created_at) DESC
                LIMIT 1
                """
            )
            return cur.fetchone()

    def get_recent_dispatches(
        self,
        limit: int = 50,
        parent_session_id: Optional[str] = None,
    ) -> list:
        """Return the N most recent dispatch_sessions rows."""
        with self.cursor() as cur:
            if parent_session_id is not None:
                cur.execute(
                    """
                    SELECT sub_session_id, parent_session_id, node_name, status,
                           input_tokens, output_tokens, cost_usd, duration_ms,
                           error, started_at, completed_at
                    FROM dispatch_sessions
                    WHERE parent_session_id = %s
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (parent_session_id, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT sub_session_id, parent_session_id, node_name, status,
                           input_tokens, output_tokens, cost_usd, duration_ms,
                           error, started_at, completed_at
                    FROM dispatch_sessions
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            return cur.fetchall()

    def get_file_sync_state(self) -> list:
        """Return all kernel_files_sync rows with lengths rather than content."""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT file_path, synced_from, length(content) AS content_length,
                       updated_at
                FROM kernel_files_sync
                ORDER BY updated_at DESC
                """
            )
            return cur.fetchall()

    def list_dispatch_agents(self) -> list:
        """Return all dispatch_agents rows."""
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT node_name, agent_id, prompt_hash, created_at
                FROM dispatch_agents
                ORDER BY created_at DESC
                """
            )
            return cur.fetchall()
```

- [ ] **Step 2: Run the db_dispatch suite**

Run: `python3 -m pytest orchestrator/tests/test_db_dispatch.py -v`
Expected: all 11 tests pass.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/db.py orchestrator/tests/test_db_dispatch.py
git commit -m "feat(db): read-only helpers for dashboard HTTP API"
```

---

## Task 11: `PanelApiServer` — failing tests

**Files:**
- Create: `orchestrator/tests/test_http_api.py`

- [ ] **Step 1: Write the failing tests**

Create `orchestrator/tests/test_http_api.py`:

```python
"""Integration tests for PanelApiServer.

Spins up a real ThreadingHTTPServer on an ephemeral port and hits it
via urllib.request, asserting JSON shapes against a mocked Database.
The server and the DB mock are both cheap, so we rebuild per test.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from orchestrator.http_api import PanelApiServer


@pytest.fixture
def server():
    db = MagicMock()
    db.get_current_parent_session = MagicMock(return_value={
        "agent_id": "agent_x",
        "environment_id": "env_x",
        "session_id": "sesn_parent",
        "status": "running",
        "total_input_tokens": 100,
        "total_output_tokens": 50,
        "total_cost_usd": 0.25,
        "created_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        "last_event_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
    })
    db.get_recent_dispatches = MagicMock(return_value=[
        {
            "sub_session_id": "sesn_sub_1",
            "parent_session_id": "sesn_parent",
            "node_name": "business_analyst",
            "status": "complete",
            "input_tokens": 3000,
            "output_tokens": 500,
            "cost_usd": 0.0275,
            "duration_ms": 4800,
            "error": None,
            "started_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
            "completed_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        }
    ])
    db.get_file_sync_state = MagicMock(return_value=[
        {
            "file_path": ".claude/kernel/journal/WISDOM.md",
            "synced_from": "cdc",
            "content_length": 1234,
            "updated_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        }
    ])
    db.list_dispatch_agents = MagicMock(return_value=[
        {
            "node_name": "business_analyst",
            "agent_id": "agent_ba",
            "prompt_hash": "deadbeef",
            "created_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        }
    ])

    api = PanelApiServer(db=db, host="127.0.0.1", port=0)
    api.start()
    deadline = time.time() + 2.0
    while api.port is None and time.time() < deadline:
        time.sleep(0.01)
    assert api.port is not None
    yield api, db
    api.stop()


def _get_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/json"
        return json.loads(resp.read())


def test_health_endpoint(server):
    api, _ = server
    body = _get_json(api.port, "/api/cloud/health")
    assert body["status"] == "ok"


def test_session_endpoint(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/session")
    assert body["session_id"] == "sesn_parent"
    assert body["status"] == "running"
    assert body["total_cost_usd"] == 0.25
    db.get_current_parent_session.assert_called_once()


def test_dispatches_endpoint_default_limit(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/dispatches")
    assert isinstance(body, list)
    assert body[0]["node_name"] == "business_analyst"
    assert body[0]["cost_usd"] == 0.0275


def test_dispatches_endpoint_honors_limit_param(server):
    api, db = server
    _get_json(api.port, "/api/cloud/dispatches?limit=5")
    call = db.get_recent_dispatches.call_args
    assert call.kwargs.get("limit", call.args[0] if call.args else None) == 5


def test_files_endpoint(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/files")
    assert body[0]["file_path"] == ".claude/kernel/journal/WISDOM.md"
    assert body[0]["synced_from"] == "cdc"


def test_agents_endpoint(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/agents")
    assert body[0]["node_name"] == "business_analyst"


def test_unknown_path_returns_404(server):
    api, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            f"http://127.0.0.1:{api.port}/api/cloud/unknown", timeout=2
        )
    assert exc_info.value.code == 404
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `python3 -m pytest orchestrator/tests/test_http_api.py -v`
Expected: `ModuleNotFoundError: No module named 'orchestrator.http_api'`.

---

## Task 12: `PanelApiServer` — implementation

**Files:**
- Create: `orchestrator/http_api.py`

- [ ] **Step 1: Write the module**

Create `orchestrator/http_api.py`:

```python
"""HTTP panel API for the ora-kernel-cloud dashboard.

Exposes five read-only JSON endpoints the dashboard polls for panel
data the WebSocket protocol doesn't carry naturally. Runs in a
background daemon thread using stdlib's ThreadingHTTPServer — no new
dependencies.

Endpoints
---------
GET /api/cloud/health        -> {"status": "ok", "port": N}
GET /api/cloud/session       -> current parent cloud_sessions row
GET /api/cloud/dispatches    -> recent dispatch_sessions rows (?limit=50)
GET /api/cloud/files         -> kernel_files_sync state (metadata, no body)
GET /api/cloud/agents        -> dispatch_agents cache
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """JSON encoder default for types psycopg2 returns (datetime, Decimal)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class _PanelRequestHandler(BaseHTTPRequestHandler):
    # The Database instance is attached to the server class below.
    server: "ThreadingHTTPServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("http_api: " + format, *args)

    def _send_json(self, body: Any, status: int = 200) -> None:
        payload = json.dumps(body, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_404(self) -> None:
        body = json.dumps({"error": "not found"}).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        db = getattr(self.server, "_db", None)
        if db is None:
            self._send_json({"error": "db not configured"}, status=500)
            return

        try:
            if path == "/api/cloud/health":
                self._send_json({"status": "ok", "port": self.server.server_port})
                return

            if path == "/api/cloud/session":
                row = db.get_current_parent_session()
                self._send_json(row or {})
                return

            if path == "/api/cloud/dispatches":
                limit = int(query.get("limit", ["50"])[0])
                parent_id = query.get("parent_session_id", [None])[0]
                rows = db.get_recent_dispatches(
                    limit=limit, parent_session_id=parent_id
                )
                self._send_json(rows or [])
                return

            if path == "/api/cloud/files":
                rows = db.get_file_sync_state()
                self._send_json(rows or [])
                return

            if path == "/api/cloud/agents":
                rows = db.list_dispatch_agents()
                self._send_json(rows or [])
                return

            self._send_404()
        except Exception:
            logger.exception("http_api: handler crashed for %s", path)
            self._send_json({"error": "internal"}, status=500)


class PanelApiServer:
    """Threaded HTTP server exposing panel data for the dashboard.

    Parameters
    ----------
    db : Database
        Orchestrator postgres wrapper. Used for read-only queries.
    host : str
        Bind interface. Default ``127.0.0.1``.
    port : int
        TCP port. Pass ``0`` for OS-assigned (tests use this).
    """

    def __init__(self, db, host: str = "127.0.0.1", port: int = 8003):
        self._db = db
        self._host = host
        self._requested_port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._server = ThreadingHTTPServer(
            (self._host, self._requested_port), _PanelRequestHandler
        )
        # Stash the db reference on the server instance so the handler
        # class can read it via self.server._db.
        self._server._db = self._db  # type: ignore[attr-defined]
        self.port = self._server.server_port
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="http-api",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "http_api: listening on http://%s:%d", self._host, self.port
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
        self.port = None
```

- [ ] **Step 2: Run tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_http_api.py -v`
Expected: 7 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/http_api.py orchestrator/tests/test_http_api.py
git commit -m "feat(http_api): read-only panel API for dashboard polling"
```

---

## Task 13: `WebSocketHitlHandler` — failing tests

**Files:**
- Create: `orchestrator/tests/test_ws_hitl.py`

- [ ] **Step 1: Write the failing tests**

Create `orchestrator/tests/test_ws_hitl.py`:

```python
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
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `python3 -m pytest orchestrator/tests/test_ws_hitl.py -v`
Expected: `ModuleNotFoundError: No module named 'orchestrator.ws_hitl'`.

---

## Task 14: `WebSocketHitlHandler` — implementation

**Files:**
- Create: `orchestrator/ws_hitl.py`

- [ ] **Step 1: Write the module**

Create `orchestrator/ws_hitl.py`:

```python
"""WebSocket-backed HITL handler.

Drop-in replacement for StdinHitlHandler when the dashboard WS bridge
is active. Sends a ``HITL_NEEDED`` envelope on the bridge, then blocks
on a thread-safe ``threading.Event`` until either a matching
``HITL_RESPONSE`` arrives (via ``WebSocketBridge.on_hitl_response``)
or the timeout fires.

If no dashboard is currently connected to the bridge, the handler
denies immediately with a ``no dashboard connected`` reason rather
than blocking — this prevents the orchestrator from wedging on a
tool_confirmation event when nobody is watching. Operators who need
to approve HITL calls without a dashboard should run without the
bridge and use ``StdinHitlHandler``.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from orchestrator import ws_events

logger = logging.getLogger(__name__)

SendResponse = Callable[[str, bool, str], None]


class WebSocketHitlHandler:
    """Dispatches HITL requests over the WebSocket bridge.

    Parameters
    ----------
    ws_bridge : WebSocketBridge
        The live bridge. Used for outbound broadcast.
    send_response : callable
        ``send_response(tool_use_id, approved, reason)`` — typically
        ``SessionManager.send_tool_confirmation``.
    timeout_seconds : float
        How long to wait for a dashboard response before denying.
        Default 120s.
    """

    def __init__(
        self,
        ws_bridge,
        send_response: SendResponse,
        timeout_seconds: float = 120.0,
    ):
        self.ws_bridge = ws_bridge
        self.send_response = send_response
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._current_request_id: Optional[str] = None
        self._response_event = threading.Event()
        self._response_payload: Optional[Dict[str, Any]] = None
        # Wire ourselves into the bridge's inbound path
        ws_bridge.on_hitl_response = self.on_response

    def handle(self, event: Any) -> None:
        tool_use_id = getattr(event, "tool_use_id", None) or getattr(event, "id", "")
        tool_name = getattr(event, "name", "unknown")
        raw_input = getattr(event, "input", {})
        if not isinstance(raw_input, dict):
            raw_input = {"raw": str(raw_input)}

        if getattr(self.ws_bridge, "client_count", 0) == 0:
            logger.warning(
                "hitl: no dashboard connected, denying tool_use_id=%s", tool_use_id
            )
            self.send_response(tool_use_id, False, "no dashboard connected")
            return

        with self._lock:
            self._current_request_id = tool_use_id
            self._response_event.clear()
            self._response_payload = None

        self.ws_bridge.broadcast(
            ws_events.hitl_needed(
                request_id=tool_use_id,
                tool_name=tool_name,
                tool_input=raw_input,
                reason="",
            )
        )

        arrived = self._response_event.wait(timeout=self.timeout_seconds)
        with self._lock:
            payload = self._response_payload
            self._current_request_id = None
            self._response_payload = None

        if not arrived or payload is None:
            logger.warning(
                "hitl: timeout waiting for dashboard response on tool_use_id=%s",
                tool_use_id,
            )
            self.send_response(
                tool_use_id,
                False,
                f"dashboard response timeout ({self.timeout_seconds}s)",
            )
            return

        decision = payload.get("decision", "deny")
        reason = payload.get("reason", "") or ""
        approved = decision == "approve"
        self.send_response(tool_use_id, approved, reason)

    def on_response(self, payload: Dict[str, Any]) -> None:
        """Callback for inbound HITL_RESPONSE events from the bridge."""
        request_id = payload.get("request_id")
        with self._lock:
            if request_id != self._current_request_id:
                logger.debug(
                    "hitl: ignoring response for %s (current=%s)",
                    request_id,
                    self._current_request_id,
                )
                return
            self._response_payload = payload
            self._response_event.set()
```

- [ ] **Step 2: Run tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_ws_hitl.py -v`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/ws_hitl.py orchestrator/tests/test_ws_hitl.py
git commit -m "feat(hitl): WebSocketHitlHandler delegating to dashboard over bridge"
```

---

## Task 15: Wire WS bridge + HTTP API + HITL swap into `__main__.py`

**Files:**
- Modify: `orchestrator/__main__.py`
- Modify: `config.yaml`

- [ ] **Step 1: Add config entries**

In `config.yaml`, add after the `scheduler:` block:

```yaml
dashboard:
  enabled: true
  websocket_port: 8002
  http_api_port: 8003
```

- [ ] **Step 2: Add imports to `__main__.py`**

In `orchestrator/__main__.py`, near the existing imports, add:

```python
from orchestrator.http_api import PanelApiServer
from orchestrator.ws_bridge import WebSocketBridge
from orchestrator.ws_hitl import WebSocketHitlHandler
```

- [ ] **Step 3: Build bridge + API + handler**

In `orchestrator/__main__.py`, replace the block that builds the HITL handler and event consumer:

```python
    # HITL handler — stdin prompt, hot-swappable for dashboard later
    hitl = StdinHitlHandler(send_response=session_mgr.send_tool_confirmation)

    # Dispatch manager — translates DISPATCH fences into sub-sessions
    node_spec_dir = (
        Path(__file__).resolve().parent.parent
        / "kernel-files"
        / ".claude"
        / "kernel"
        / "nodes"
        / "system"
    )
    dispatch_manager = DispatchManager(
        db=db,
        client=Anthropic(api_key=api_key),
        environment_id=env_id,
        send_to_parent=lambda _sid, text: session_mgr.send_message(text),
        node_spec_dir=node_spec_dir,
    )

    # Event consumer
    consumer = EventConsumer(
        db=db,
        api_key=api_key,
        agent_id=agent_id,
        environment_id=env_id,
        on_hitl_needed=hitl.handle,
        file_sync=file_sync,
        dispatch_manager=dispatch_manager,
    )
```

with:

```python
    # Dashboard bridge (WebSocket) and HTTP API — optional, controlled by config
    dashboard_cfg = config.get("dashboard", {}) or {}
    dashboard_enabled = dashboard_cfg.get("enabled", True)
    ws_bridge = None
    panel_api = None
    if dashboard_enabled:
        ws_bridge = WebSocketBridge(
            host="127.0.0.1",
            port=dashboard_cfg.get("websocket_port", 8002),
        )
        try:
            ws_bridge.start()
            logger.info("Dashboard WS bridge: ws://127.0.0.1:%d", ws_bridge.port)
        except Exception:
            logger.exception("ws_bridge failed to start — falling back to stdin HITL")
            ws_bridge = None

        if ws_bridge is not None:
            panel_api = PanelApiServer(
                db=db,
                host="127.0.0.1",
                port=dashboard_cfg.get("http_api_port", 8003),
            )
            try:
                panel_api.start()
                logger.info("Dashboard HTTP API: http://127.0.0.1:%d", panel_api.port)
            except Exception:
                logger.exception("panel_api failed to start")
                panel_api = None

    # HITL handler — WebSocket if bridge is live, stdin as fallback
    if ws_bridge is not None:
        hitl = WebSocketHitlHandler(
            ws_bridge=ws_bridge,
            send_response=session_mgr.send_tool_confirmation,
        )
        logger.info("HITL: using WebSocket handler")
    else:
        hitl = StdinHitlHandler(
            send_response=session_mgr.send_tool_confirmation
        )
        logger.info("HITL: using stdin handler")

    # Wire bridge inbound callbacks to orchestrator actions
    if ws_bridge is not None:
        ws_bridge.on_user_message = lambda payload: session_mgr.send_message(
            payload.get("text", "")
        )
        ws_bridge.on_abort = lambda: session_mgr.interrupt()

    # Dispatch manager — translates DISPATCH fences into sub-sessions
    node_spec_dir = (
        Path(__file__).resolve().parent.parent
        / "kernel-files"
        / ".claude"
        / "kernel"
        / "nodes"
        / "system"
    )
    dispatch_manager = DispatchManager(
        db=db,
        client=Anthropic(api_key=api_key),
        environment_id=env_id,
        send_to_parent=lambda _sid, text: session_mgr.send_message(text),
        node_spec_dir=node_spec_dir,
        ws_bridge=ws_bridge,
    )

    # Event consumer
    consumer = EventConsumer(
        db=db,
        api_key=api_key,
        agent_id=agent_id,
        environment_id=env_id,
        on_hitl_needed=hitl.handle,
        file_sync=file_sync,
        dispatch_manager=dispatch_manager,
        ws_bridge=ws_bridge,
    )
```

- [ ] **Step 4: Update the restart-path consumer to pass `ws_bridge`**

In `orchestrator/__main__.py`, inside the `while running:` loop, update the restart-path construction to pass `ws_bridge`:

```python
                    consumer = EventConsumer(
                        db=db,
                        api_key=api_key,
                        agent_id=agent_id,
                        environment_id=env_id,
                        on_hitl_needed=hitl.handle,
                        file_sync=file_sync,
                        dispatch_manager=dispatch_manager,
                        ws_bridge=ws_bridge,
                    )
```

- [ ] **Step 5: Shut down bridge + API in the shutdown handler**

In `orchestrator/__main__.py`, update the bottom cleanup block:

```python
    # Cleanup
    scheduler.stop()
    if panel_api is not None:
        panel_api.stop()
    if ws_bridge is not None:
        ws_bridge.stop()
    db.close()
    logger.info("Orchestrator stopped.")
```

- [ ] **Step 6: Import check + full test suite**

Run:
```
python3 -c "import orchestrator.__main__; print('ok')"
python3 -m pytest orchestrator/tests/ -v
```
Expected: clean import; all tests green (72 existing + 18 ws_events + 9 ws_bridge + 4 ws_hitl + 7 http_api + additional event_consumer and dispatch bridge tests).

- [ ] **Step 7: Commit**

```bash
git add orchestrator/__main__.py config.yaml
git commit -m "feat(orchestrator): wire dashboard WS bridge + HTTP API + HITL swap"
```

---

## Task 16: Snapshot-on-connect so late-joining dashboards see current state

**Files:**
- Modify: `orchestrator/ws_bridge.py`
- Modify: `orchestrator/tests/test_ws_bridge.py`

**Context.** When a dashboard client connects to the bridge mid-session, it has no history — only future events flow. Without a snapshot, a freshly-opened dashboard shows an empty graph until the next dispatch or status change. We fix this by having the bridge invoke a caller-supplied `snapshot_provider` function on every new connection and sending its return value (a list of envelopes) to just that client before subscribing it to the broadcast stream.

- [ ] **Step 1: Write the failing test**

Append to `orchestrator/tests/test_ws_bridge.py`:

```python
def test_new_client_receives_snapshot_on_connect(bridge):
    """A snapshot_provider set on the bridge is called on each new
    connection, and its return envelopes are sent to only that client
    before the client starts receiving broadcasts."""
    sent = []

    def provide_snapshot():
        return [
            system_status("sesn_parent", "running"),
            system_status("sesn_parent", "running"),  # two snapshot frames
        ]

    bridge.snapshot_provider = provide_snapshot

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            msg1 = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
            msg2 = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
            sent.append(msg1)
            sent.append(msg2)

    _run(go())
    assert len(sent) == 2
    assert all(m["event_type"] == "SYSTEM_STATUS" for m in sent)


def test_snapshot_provider_exception_does_not_drop_client(bridge):
    def bad_snapshot():
        raise RuntimeError("db is down")

    bridge.snapshot_provider = bad_snapshot

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            await asyncio.sleep(0.05)
            bridge.broadcast(system_status("sesn_x", "running"))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
            return msg

    result = _run(go())
    assert result["event_type"] == "SYSTEM_STATUS"
```

- [ ] **Step 2: Run — expect failure on `snapshot_provider` attribute**

Run: `python3 -m pytest orchestrator/tests/test_ws_bridge.py -v -k snapshot`
Expected: 2 failures.

- [ ] **Step 3: Add `snapshot_provider` to WebSocketBridge**

In `orchestrator/ws_bridge.py`, update `__init__` to add:

```python
        # Caller may set this to a function returning a list of envelopes
        # to send to each new client on connect (used for late-joining
        # dashboards to see current state).
        self.snapshot_provider: Optional[Callable[[], list]] = None
```

Update the `_handler` method. Replace:

```python
    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        self._clients.add(ws)
        logger.info("ws_bridge: client connected (%d total)", len(self._clients))
        try:
            async for raw in ws:
```

with:

```python
    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        # Send snapshot frames first, BEFORE adding to the broadcast set,
        # so the new client receives them in a defined order without
        # interleaving live broadcasts.
        if self.snapshot_provider is not None:
            try:
                frames = self.snapshot_provider() or []
                for envelope in frames:
                    await ws.send(json.dumps(envelope, default=str))
            except Exception:
                logger.exception("ws_bridge: snapshot_provider failed")

        self._clients.add(ws)
        logger.info("ws_bridge: client connected (%d total)", len(self._clients))
        try:
            async for raw in ws:
```

- [ ] **Step 4: Run tests — expect pass**

Run: `python3 -m pytest orchestrator/tests/test_ws_bridge.py -v`
Expected: 11 passed (9 original + 2 snapshot).

- [ ] **Step 5: Wire a snapshot provider in `__main__.py`**

In `orchestrator/__main__.py`, after `ws_bridge.start()` succeeds and after `dispatch_manager` is built, add:

```python
    if ws_bridge is not None:
        def _build_snapshot():
            """Collect current-state envelopes for newly-connected clients."""
            frames = []
            try:
                parent = db.get_current_parent_session()
                if parent is not None:
                    frames.append(ws_events.system_status(
                        session_id=parent.get("session_id", ""),
                        status=parent.get("status", "unknown"),
                        total_cost_usd=float(parent.get("total_cost_usd") or 0),
                    ))
                for row in db.get_recent_dispatches(limit=20):
                    frames.append(ws_events.node_update(
                        node_id=row["sub_session_id"],
                        parent_id=row["parent_session_id"],
                        node_name=row["node_name"],
                        status=row["status"],
                        tokens={
                            "input": row["input_tokens"] or 0,
                            "output": row["output_tokens"] or 0,
                        },
                        cost_usd=float(row["cost_usd"] or 0),
                        duration_ms=row["duration_ms"],
                        error=row.get("error"),
                    ))
                    frames.append(ws_events.edge_update(
                        from_id=row["parent_session_id"],
                        to_id=row["sub_session_id"],
                    ))
            except Exception:
                logger.exception("snapshot provider failed")
            return frames

        ws_bridge.snapshot_provider = _build_snapshot
```

Add the import near the top of `orchestrator/__main__.py`:

```python
from orchestrator import ws_events
```

- [ ] **Step 6: Import check + full test suite**

Run:
```
python3 -c "import orchestrator.__main__; print('ok')"
python3 -m pytest orchestrator/tests/ -v
```
Expected: clean import; all tests green.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/ws_bridge.py orchestrator/__main__.py orchestrator/tests/test_ws_bridge.py
git commit -m "feat(ws): snapshot_provider so late clients see current state"
```

---

## Task 17: End-to-end smoke test (semi-manual)

This task is a guided manual checklist. It exercises the live bridge and HTTP API against a running orchestrator. No dashboard is required — we use `websockets.connect` and `curl` as stand-ins.

- [ ] **Step 1: Start the orchestrator**

Run: `python3 -m orchestrator`
Expected: startup logs include:
- `ws_bridge: listening on ws://127.0.0.1:8002`
- `http_api: listening on http://127.0.0.1:8003`
- `HITL: using WebSocket handler`

- [ ] **Step 2: Hit the health endpoint**

In a second terminal:

```
curl -s http://127.0.0.1:8003/api/cloud/health | python3 -m json.tool
```

Expected: `{"status": "ok", "port": 8003}`.

- [ ] **Step 3: Hit the session endpoint**

```
curl -s http://127.0.0.1:8003/api/cloud/session | python3 -m json.tool
```

Expected: JSON describing the current parent cloud_sessions row with fields `agent_id`, `session_id`, `status`, `total_cost_usd`, etc.

- [ ] **Step 4: Hit the dispatches endpoint**

```
curl -s 'http://127.0.0.1:8003/api/cloud/dispatches?limit=5' | python3 -m json.tool
```

Expected: JSON array of the 5 most recent dispatch_sessions rows.

- [ ] **Step 5: Connect a WebSocket client and observe events**

```
python3 -c "
import asyncio, json, websockets
async def go():
    async with websockets.connect('ws://127.0.0.1:8002') as ws:
        print('connected, waiting for snapshot + events')
        for _ in range(50):
            msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
            e = json.loads(msg)
            print(f\"[{e['event_type']}] {json.dumps(e['payload'])[:120]}\")
asyncio.run(go())
"
```

Expected: on connect, you see one or more SYSTEM_STATUS + NODE_UPDATE snapshot frames. Then the client sits idle waiting for live events.

- [ ] **Step 6: Trigger a dispatch, watch events flow**

In a third terminal:

```
python3 -m orchestrator --send "Dispatch the smoke_test_node with any task. Emit only the DISPATCH fence."
```

Expected (in the WS client terminal): a `CHAT_RESPONSE` event (Kernel's response containing the DISPATCH fence), then `NODE_UPDATE` (status=running), then `EDGE_UPDATE`, then `NODE_UPDATE` (status=complete), then another `CHAT_RESPONSE` (Kernel's acknowledgment after the DISPATCH_RESULT is forwarded).

- [ ] **Step 7: Send a USER_MESSAGE over the WS to the orchestrator**

In a fourth terminal (or extending the Step 5 client):

```
python3 -c "
import asyncio, json, websockets
async def go():
    async with websockets.connect('ws://127.0.0.1:8002') as ws:
        await ws.send(json.dumps({
            'event_type': 'USER_MESSAGE',
            'payload': {'text': 'Hello kernel from the WS bridge.'}
        }))
        print('sent')
        await asyncio.sleep(5)
asyncio.run(go())
"
```

Expected: the Kernel receives the user message (verify with `psql -d ora_kernel -c \"SELECT details->>'text' FROM orch_activity_log WHERE action='MESSAGE' ORDER BY id DESC LIMIT 1;\"`) and eventually replies with an `agent.message` that you can see via the Step 5 listener as a `CHAT_RESPONSE` event.

- [ ] **Step 8: Verify HITL stub (optional, requires provoking a tool_confirmation)**

This step is skipped by default — the Managed Agent does not currently emit `tool_confirmation` events on its own. If you can provoke one (e.g., by asking the Kernel to write a protected file), verify that:
- The WS client receives a `HITL_NEEDED` envelope
- Sending an inbound `HITL_RESPONSE` with `{"request_id": ..., "decision": "approve"}` causes the orchestrator to forward the approval and the Kernel continues

- [ ] **Step 9: Shut down cleanly**

Press Ctrl+C in the orchestrator terminal. Expected log output:
- `Orchestrator stopped.`
- Clean exit with no stack traces
- Re-running `curl http://127.0.0.1:8003/api/cloud/health` should fail with connection refused

- [ ] **Step 10: Record findings**

If every step above passes, Phase A is operational and ready for Phase B (dashboard tab wiring in forex-ml-platform). Note any surprises — especially event ordering issues, HITL timeout problems, or shutdown races — as follow-up tasks.

---

## Phase B — Dashboard side (separate plan, separate repo)

These items are out of scope for this plan but are documented here so the reader understands the end-to-end story. They will be planned and executed against the `forex-ml-platform` repo as a follow-up.

1. Parameterize `src/dashboard/orchestrator-client.js` to accept a `wsUrl` constructor argument (currently hard-coded to `ws://localhost:8000` at line 29).
2. Add a new "Cloud Kernel" tab to `dashboard.html` alongside the existing Orchestrator tab. Layout: Cytoscape graph on the left, side panel on the right with Parent Session, Cost, File Sync, Dispatch Agents sub-panels. All sub-panels HTTP-poll `http://localhost:8003/api/cloud/*` on an interval.
3. Instantiate `new OrchestratorClient(...)` twice — once against port 8000 (existing forex-ml), once against port 8002 (new ora-kernel-cloud), with separate graph containers and HUD element IDs.
4. Extend the HITL widget to detect which orchestrator the active `HITL_NEEDED` came from (based on which instance delivered it) and route the Approve/Discuss response to the correct bridge as a `HITL_RESPONSE` inbound message rather than a chat message.
5. Add an indicator in the tab switcher showing Cloud Kernel status (green/running, yellow/idle, red/disconnected) polled via `/api/cloud/health`.
6. Update `scripts/start_orchestrator.sh` (or document as a separate launch) to also start `python3 -m orchestrator` against the ora-kernel-cloud repo if the operator wants both.

---

## Self-Review Notes

**Spec coverage.** Every component in the plan's Architecture section has at least one task: ws_events (Tasks 1–2), ws_bridge (Tasks 3–4, 16), EventConsumer wiring (Tasks 5–6), DispatchManager wiring (Tasks 7–8), db helpers (Tasks 9–10), http_api (Tasks 11–12), WebSocketHitlHandler (Tasks 13–14), main entry point wiring (Task 15), live smoke test (Task 17). Phase B dashboard work is out of scope and explicitly listed.

**Placeholder scan.** Every step includes exact code, exact commands, exact expected output. No "TBD", no "similar to above", no "implement later". The only place where I use "..." is inside envelope payloads where the full shape is already defined elsewhere in the same file.

**Type consistency.** `WebSocketBridge` methods: `start`, `stop`, `broadcast`, `client_count`, `snapshot_provider`, `on_user_message`, `on_abort`, `on_hitl_response`. Used consistently across Tasks 3, 4, 13, 14, 15, 16. `PanelApiServer` methods: `start`, `stop`, `port`. Used consistently across Tasks 11, 12, 15. `ws_events` factory functions: `make_envelope`, `system_status`, `node_update`, `edge_update`, `hitl_needed`, `chat_response`, `activity`, `parse_inbound_event`. Used consistently across Tasks 1, 2, 6, 8, 14, 15, 16.

**Protocol lock-step.** The outbound event type constants in `ws_events.py` are the single source of truth. Every producer (EventConsumer, DispatchManager, snapshot provider) imports from `ws_events` — no string literals scattered elsewhere. This matches the file_sync / session_manager protocol-constant pattern already established in the codebase.

**No hidden dependencies on Phase B.** Every test uses real local clients (`websockets.connect`, `urllib.request`) to verify protocol behavior. The smoke test uses the same pattern. Zero dependency on the forex-ml repo for correctness verification.

**Known limitation.** Snapshot-on-connect (Task 16) sends recent dispatches but does not replay `CHAT_RESPONSE` messages from earlier in the session. A late-joining dashboard will see the current dispatch state but not past Kernel messages — those are still reachable via an HTTP endpoint (`/api/cloud/activity`) if needed. If that gap matters, it should be a follow-up task, not an expansion of this plan.
