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
from websockets.protocol import OPEN

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
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_bridge_binds_and_reports_port(bridge):
    assert bridge.port > 0


def test_client_can_connect_and_disconnect(bridge):
    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{bridge.port}") as ws:
            # websockets 15.x uses ws.state == OPEN instead of ws.open
            assert ws.state == OPEN
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
            assert ws.state == OPEN

    _run(go())
