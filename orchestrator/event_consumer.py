"""SSE event consumer for Anthropic Managed Agent sessions.

Consumes the event stream for a session, parses events by type, writes
telemetry to PostgreSQL, and provides callback hooks for real-time
forwarding (e.g. WebSocket bridge).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from anthropic import Anthropic

from orchestrator.db import Database

if TYPE_CHECKING:
    from orchestrator.file_sync import FileSync

logger = logging.getLogger(__name__)

# ── Cost rates (USD per million tokens) ──────────────────────────────
COST_RATES: Dict[str, Dict[str, float]] = {
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
}
DEFAULT_RATES = COST_RATES["claude-opus-4-6"]

TEXT_PREVIEW_LEN = 10_000
INPUT_PREVIEW_LEN = 2_000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _cost_for_model(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost given a model name and token counts."""
    rates = DEFAULT_RATES
    for key, r in COST_RATES.items():
        if key in model:
            rates = r
            break
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


@dataclass
class SessionTotals:
    """Running totals for a single streaming session."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class EventConsumer:
    """Consumes an Anthropic Managed Agent SSE stream and records events.

    Parameters
    ----------
    db : Database
        Connected database helper.
    api_key : str
        Anthropic API key.
    agent_id : str
        Identifier for the managed agent (written to activity log / sessions).
    environment_id : str
        Environment tied to the cloud session.
    on_event : callable, optional
        Called for every event (useful for WebSocket bridge forwarding).
    on_hitl_needed : callable, optional
        Called when a tool_confirmation event arrives (Human-In-The-Loop).
    """

    def __init__(
        self,
        db: Database,
        api_key: str,
        agent_id: str,
        environment_id: str,
        on_event: Optional[Callable[[Any], None]] = None,
        on_hitl_needed: Optional[Callable[[Any], None]] = None,
        file_sync: Optional["FileSync"] = None,
    ):
        self.db = db
        self.client = Anthropic(api_key=api_key)
        self.agent_id = agent_id
        self.environment_id = environment_id
        self.on_event = on_event
        self.on_hitl_needed = on_hitl_needed
        self.file_sync = file_sync
        self.totals = SessionTotals()

    # ── Public API ────────────────────────────────────────────────────

    def consume(self, session_id: str) -> bool:
        """Block while consuming the SSE stream for *session_id*.

        Returns
        -------
        bool
            ``True`` if the session ended normally (idle).
            ``False`` if the session was terminated (caller should restart).
        """
        logger.info("Starting event consumer for session %s", session_id)
        self.totals = SessionTotals()

        with self.client.beta.sessions.events.stream(session_id) as stream:
            for event in stream:
                try:
                    if self.on_event is not None:
                        self.on_event(event)

                    result = self._dispatch(session_id, event)
                    if result is False:
                        return False
                except Exception:
                    logger.exception(
                        "Error processing event type=%s for session=%s",
                        getattr(event, "type", "unknown"),
                        session_id,
                    )

        logger.info(
            "Stream closed for session %s — totals: in=%d out=%d cost=$%.4f",
            session_id, self.totals.input_tokens, self.totals.output_tokens,
            self.totals.cost_usd,
        )
        return True

    # ── Internal dispatch ─────────────────────────────────────────────

    def _dispatch(self, session_id: str, event: Any) -> Optional[bool]:
        """Route a single SSE event to the appropriate handler.

        Returns ``False`` only when the session has terminated and the caller
        should treat this as a signal to restart.
        """
        event_type: str = getattr(event, "type", "")
        handler = self._handlers.get(event_type)
        if handler is not None:
            return handler(self, session_id, event)
        else:
            logger.debug("Unhandled event type: %s", event_type)
        return None

    # ── Handlers (one per event type) ─────────────────────────────────

    def _handle_message(self, session_id: str, event: Any) -> None:
        text_parts = []
        for block in getattr(event, "content", []):
            if hasattr(block, "text"):
                text_parts.append(block.text)
        full_text = " ".join(text_parts)
        preview = _truncate(full_text, TEXT_PREVIEW_LEN) if full_text else ""
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="INFO",
            event_source="sse",
            action="MESSAGE",
            details={"text": preview},
        )
        if self.file_sync is not None and full_text:
            try:
                self.file_sync.handle_snapshot_response(full_text)
            except Exception:
                logger.exception("file_sync snapshot handler failed")

    def _handle_tool_use(self, session_id: str, event: Any) -> None:
        tool_name = getattr(event, "name", "unknown")
        raw_input = getattr(event, "input", {})
        input_preview = _truncate(
            json.dumps(raw_input, default=str) if isinstance(raw_input, dict) else str(raw_input),
            INPUT_PREVIEW_LEN,
        )
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="INFO",
            event_source="sse",
            action="TOOL_USE",
            details={"tool_name": tool_name, "input": input_preview},
        )

        # CDC file sync from tool_use events
        if self.file_sync is not None and isinstance(raw_input, dict):
            if tool_name == "Write":
                try:
                    self.file_sync.handle_write(
                        raw_input.get("file_path", ""),
                        raw_input.get("content", ""),
                    )
                except Exception:
                    logger.exception("file_sync.handle_write failed")
            elif tool_name == "Edit":
                try:
                    self.file_sync.handle_edit(
                        raw_input.get("file_path", ""),
                        raw_input.get("old_string", ""),
                        raw_input.get("new_string", ""),
                    )
                except Exception:
                    logger.exception("file_sync.handle_edit failed")

        # Detect HITL confirmation requests
        if tool_name == "tool_confirmation" and self.on_hitl_needed is not None:
            self.on_hitl_needed(event)

    def _handle_tool_result(self, session_id: str, event: Any) -> None:
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="INFO",
            event_source="sse",
            action="TOOL_RESULT",
        )

    def _handle_model_request_end(self, session_id: str, event: Any) -> None:
        usage = getattr(event, "model_usage", None)
        # model_usage is a Pydantic object — use getattr, not .get()
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        model = getattr(event, "model", "claude-opus-4-6")

        cost = _cost_for_model(model, input_tokens, output_tokens)

        # Update running totals
        self.totals.input_tokens += input_tokens
        self.totals.output_tokens += output_tokens
        self.totals.cost_usd += cost

        self.db.log_token_usage(
            session_id=session_id,
            agent_id=self.agent_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation,
        )
        self.db.log_cost(
            session_id=session_id,
            agent_id=self.agent_id,
            model=model,
            cost_usd=cost,
        )

    def _handle_status_running(self, session_id: str, event: Any) -> None:
        self.db.upsert_cloud_session(
            agent_id=self.agent_id,
            environment_id=self.environment_id,
            session_id=session_id,
            status="running",
        )

    def _handle_status_idle(self, session_id: str, event: Any) -> None:
        stop_reason = getattr(event, "stop_reason", None)
        # stop_reason may be a Pydantic object — convert to string
        stop_reason_str = self._serialize_pydantic(stop_reason)
        self.db.upsert_cloud_session(
            agent_id=self.agent_id,
            environment_id=self.environment_id,
            session_id=session_id,
            status="idle",
        )
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="INFO",
            event_source="sse",
            action="SESSION_IDLE",
            details={"stop_reason": stop_reason_str},
        )

    @staticmethod
    def _serialize_pydantic(obj: Any) -> Any:
        """Convert a Pydantic model to a JSON-serializable form, or return as-is."""
        if obj is None:
            return None
        # Pydantic v2 model
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()
            except Exception:
                pass
        # Pydantic v1 fallback
        if hasattr(obj, "dict"):
            try:
                return obj.dict()
            except Exception:
                pass
        # Last resort: string representation
        return str(obj)

    def _handle_status_terminated(self, session_id: str, event: Any) -> bool:
        error = getattr(event, "error", None)
        error_detail = str(error) if error else "unknown"
        self.db.upsert_cloud_session(
            agent_id=self.agent_id,
            environment_id=self.environment_id,
            session_id=session_id,
            status="terminated",
        )
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="ERROR",
            event_source="sse",
            action="SESSION_TERMINATED",
            details={"error": error_detail},
        )
        logger.error("Session %s terminated: %s", session_id, error_detail)
        return False

    # Class-level handler dispatch table (avoids per-event if/elif chains)
    _handlers: Dict[str, Callable[..., Any]] = {
        "agent.message": _handle_message,
        "agent.tool_use": _handle_tool_use,
        "agent.tool_result": _handle_tool_result,
        "span.model_request_end": _handle_model_request_end,
        "session.status_running": _handle_status_running,
        "session.status_idle": _handle_status_idle,
        "session.status_terminated": _handle_status_terminated,
    }
