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
