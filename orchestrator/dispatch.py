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
