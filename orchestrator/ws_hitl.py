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
