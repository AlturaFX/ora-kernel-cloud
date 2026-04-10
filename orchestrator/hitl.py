"""Stdin-based HITL (Human-In-The-Loop) approval handler.

Receives a tool_confirmation event, prompts the operator on stdin, and
invokes a caller-supplied response callback. Designed to be swapped for
a WebSocket/dashboard handler in Phase 2 of the cloud spec — keep this
file small and single-purpose.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

SendResponse = Callable[[str, bool, str], None]


class StdinHitlHandler:
    """Prompt the operator on stdin to approve or deny a tool call.

    The handler is intentionally blocking: it is called from the SSE
    dispatch loop in EventConsumer. While a prompt is open, new events
    will queue server-side until the operator responds. This is fine
    for a single-operator terminal bridge and will be replaced by an
    async WebSocket handler when the dashboard integration lands.

    Parameters
    ----------
    send_response : callable
        ``send_response(tool_use_id, approved, reason)`` — typically
        ``SessionManager.send_tool_confirmation``.
    """

    def __init__(self, send_response: SendResponse):
        self.send_response = send_response

    def handle(self, event: Any) -> None:
        tool_use_id = getattr(event, "tool_use_id", None) or getattr(event, "id", "")
        tool_name = getattr(event, "name", "unknown")
        raw_input = getattr(event, "input", {})

        print("\n" + "=" * 60, flush=True)
        print("HITL APPROVAL REQUESTED", flush=True)
        print(f"Tool: {tool_name}", flush=True)
        print(f"Input: {raw_input}", flush=True)
        print("=" * 60, flush=True)

        while True:
            try:
                answer = input("Approve? [y/n]: ").strip().lower()
            except EOFError:
                logger.warning("EOF on stdin — defaulting to deny")
                self.send_response(tool_use_id, False, "stdin closed")
                return

            if answer in ("y", "yes"):
                try:
                    reason = input("Reason (optional): ").strip()
                except EOFError:
                    reason = ""
                self.send_response(tool_use_id, True, reason)
                return
            if answer in ("n", "no"):
                try:
                    reason = input("Reason: ").strip()
                except EOFError:
                    reason = ""
                self.send_response(tool_use_id, False, reason)
                return
            print("Please answer y or n.", flush=True)
