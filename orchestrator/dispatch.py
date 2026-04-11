"""Orchestrator-side dispatch subsystem for ORA Kernel Cloud.

The Anthropic Managed Agent toolset (agent_toolset_20260401) does not
provide a subagent-dispatch tool. To preserve the ORA Kernel's
delegation model, the Kernel signals dispatch intent by emitting
structured ``` ```DISPATCH node=<name> ``` `` fenced blocks in its
messages, and the orchestrator — running on the user's machine —
spins up a focused Managed Agent sub-session per dispatch, consumes
its events, and returns the result to the parent Kernel session as a
``` ```DISPATCH_RESULT ``` `` fence via a user.message event.

Design:
- Sub-sessions reuse the parent's shared environment (no per-dispatch
  container provisioning — validated by spike 2026-04-10).
- Each node has a per-node Anthropic agent cached in dispatch_agents,
  invalidated by SHA256 of the node spec file so spec edits trigger
  rebuild.
- Dispatches run serially in the MVP — the parent event loop blocks
  while a dispatch is in flight. Parallel dispatches can be layered on
  later via threading without touching the fence protocol.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_DISPATCH_FENCE_RE = re.compile(
    r"```DISPATCH\s+node=(?P<node>\S+)\s*\n(?P<body>.*?)(?:\n)?```",
    re.DOTALL,
)


def parse_dispatch_fences(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Extract (node_name, payload_dict) pairs from ```DISPATCH``` fences.

    Fences with missing/invalid node attributes or unparseable JSON
    payloads are silently skipped — the orchestrator cannot dispatch
    something it cannot parse, and the Kernel will notice the missing
    DISPATCH_RESULT and decide how to proceed (Axiom 5: the orchestrator
    never guesses).
    """
    results: List[Tuple[str, Dict[str, Any]]] = []
    for match in _DISPATCH_FENCE_RE.finditer(text):
        node = (match.group("node") or "").strip()
        if not node:
            continue
        body = match.group("body")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("dispatch fence has invalid JSON body for node=%s", node)
            continue
        if not isinstance(payload, dict):
            logger.warning("dispatch payload for node=%s is not an object", node)
            continue
        results.append((node, payload))
    return results


# Callback type — the dispatcher forwards the result to the parent
# session by calling this function. Injected from __main__ so the
# dispatch module never imports SessionManager (avoids import cycle).
SendToParent = Callable[[str, str], None]  # (parent_session_id, text)


class DispatchManager:
    """Translate ```DISPATCH``` fences into Managed Agent sub-sessions.

    Parameters
    ----------
    db : Database
        Postgres wrapper. Used for the agent cache + dispatch_sessions rows.
    client : Anthropic
        Anthropic SDK client. Used for agents.create / sessions.create /
        sessions.events.{send,stream}.
    environment_id : str
        Shared Managed Agent environment ID (reused across dispatches —
        see the 2026-04-10 feasibility spike).
    send_to_parent : callable
        ``send_to_parent(parent_session_id, text)`` — typically
        ``SessionManager.send_message``-style. Called once per dispatch
        with the ``` ```DISPATCH_RESULT ``` `` fence that the parent
        Kernel will see.
    node_spec_dir : Path
        Directory containing node spec markdown files. A dispatch with
        ``node=business_analyst`` reads ``<dir>/business_analyst.md``.
    max_dispatch_seconds : float
        Hard timeout on a single dispatch. Sessions that do not reach
        idle within this window are reported as FAILED with a timeout
        error. Default 120s.
    """

    def __init__(
        self,
        db,
        client,
        environment_id: str,
        send_to_parent: SendToParent,
        node_spec_dir: Path,
        max_dispatch_seconds: float = 120.0,
    ):
        self.db = db
        self.client = client
        self.environment_id = environment_id
        self.send_to_parent = send_to_parent
        self.node_spec_dir = Path(node_spec_dir)
        self.max_dispatch_seconds = max_dispatch_seconds

    # ── Node spec loading ───────────────────────────────────────────

    def _spec_path(self, node_name: str) -> Path:
        return self.node_spec_dir / f"{node_name}.md"

    def _load_node_spec(self, node_name: str) -> str:
        path = self._spec_path(node_name)
        if not path.exists():
            raise FileNotFoundError(
                f"No node spec for {node_name!r} at {path}"
            )
        return path.read_text()

    def _spec_hash(self, node_name: str) -> str:
        content = self._load_node_spec(node_name)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ── Agent cache ─────────────────────────────────────────────────

    def _ensure_agent(self, node_name: str) -> str:
        """Return a Managed Agent ID for *node_name*, creating one if needed.

        The cache is keyed on node name with a content-hash tiebreaker:
        if the node spec file on disk has changed since the cached agent
        was created, we create a fresh agent and overwrite the cache
        entry. Stale agents are not deleted — they simply stop being
        referenced and accrue no cost.
        """
        current_hash = self._spec_hash(node_name)
        cached = self.db.get_dispatch_agent(node_name)
        if cached is not None and cached.get("prompt_hash") == current_hash:
            logger.debug("dispatch: reusing cached agent for %s", node_name)
            return cached["agent_id"]

        spec = self._load_node_spec(node_name)
        logger.info("dispatch: creating fresh agent for node %s", node_name)
        agent = self.client.beta.agents.create(
            name=f"ora-dispatch-{node_name}",
            model="claude-opus-4-6",
            system=spec,
            tools=[{"type": "agent_toolset_20260401"}],
        )
        self.db.upsert_dispatch_agent(
            node_name=node_name,
            agent_id=agent.id,
            prompt_hash=current_hash,
        )
        return agent.id

    # ── Sub-session lifecycle ───────────────────────────────────────

    # Opus pricing — must stay in sync with event_consumer.COST_RATES.
    _INPUT_USD_PER_M = 5.0
    _OUTPUT_USD_PER_M = 25.0

    @classmethod
    def _cost_usd(cls, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * cls._INPUT_USD_PER_M
            + output_tokens * cls._OUTPUT_USD_PER_M
        ) / 1_000_000.0

    def _run_sub_session(
        self,
        parent_session_id: str,
        agent_id: str,
        node_name: str,
        input_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a sub-session, send the task, consume events, return a result.

        Never raises for protocol-level failures — sub-session termination,
        timeouts, and explicit error events are captured in the returned
        result dict with ``status='failed'`` and an ``error`` field. The
        caller is responsible for forwarding the result to the parent
        session as a DISPATCH_RESULT fence.
        """
        session = self.client.beta.sessions.create(
            agent=agent_id,
            environment_id=self.environment_id,
            title=f"ora-dispatch-{node_name}",
        )
        sub_session_id = session.id

        self.db.record_dispatch_start(
            sub_session_id=sub_session_id,
            parent_session_id=parent_session_id,
            node_name=node_name,
            input_data=input_data,
        )

        prompt_text = json.dumps(input_data, indent=2, default=str)
        self.client.beta.sessions.events.send(
            sub_session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt_text}],
                }
            ],
        )

        input_tokens = 0
        output_tokens = 0
        response_text = ""
        t_start = time.time()
        terminated_error: Optional[str] = None

        with self.client.beta.sessions.events.stream(sub_session_id) as stream:
            for event in stream:
                if time.time() - t_start > self.max_dispatch_seconds:
                    terminated_error = (
                        f"dispatch exceeded max_dispatch_seconds="
                        f"{self.max_dispatch_seconds}"
                    )
                    break

                event_type = getattr(event, "type", "")

                if event_type == "span.model_request_end":
                    usage = getattr(event, "model_usage", None)
                    input_tokens += getattr(usage, "input_tokens", 0) or 0
                    output_tokens += getattr(usage, "output_tokens", 0) or 0

                elif event_type == "agent.message":
                    for block in getattr(event, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            response_text += text

                elif event_type == "session.status_idle":
                    break

                elif event_type == "session.status_terminated":
                    err = getattr(event, "error", None)
                    terminated_error = str(err) if err else "sub-session terminated"
                    break

        duration_ms = int((time.time() - t_start) * 1000)
        cost_usd = self._cost_usd(input_tokens, output_tokens)

        if terminated_error is not None:
            self.db.record_dispatch_failure(
                sub_session_id=sub_session_id, error=terminated_error
            )
            return {
                "status": "failed",
                "sub_session_id": sub_session_id,
                "node_name": node_name,
                "error": terminated_error,
                "output": response_text,
                "tokens": {"input": input_tokens, "output": output_tokens},
                "cost_usd": cost_usd,
                "duration_ms": duration_ms,
            }

        self.db.record_dispatch_complete(
            sub_session_id=sub_session_id,
            output_data={"text": response_text},
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )
        return {
            "status": "complete",
            "sub_session_id": sub_session_id,
            "node_name": node_name,
            "output": response_text,
            "tokens": {"input": input_tokens, "output": output_tokens},
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
        }
