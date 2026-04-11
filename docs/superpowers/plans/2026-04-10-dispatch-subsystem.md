# Dispatch Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the cloud Kernel a working dispatch model in an environment where no subagent-dispatch tool exists: the Kernel emits structured ```DISPATCH``` fenced blocks in its messages, and the orchestrator intercepts them, spins up focused short-lived Managed Agent sub-sessions per node (reusing the shared environment), consumes their events, and returns the result to the parent session as a ```DISPATCH_RESULT``` fence.

**Architecture:** Passive intent-detection via the existing SSE message stream (same pattern as the SYNC fence protocol), with a new `DispatchManager` façade over `db` + `Anthropic` client + `SessionManager`. Per-node agents are created on demand and cached by node name in a new `dispatch_agents` postgres table, invalidated by spec-content hash so spec edits automatically rebuild the agent. Sub-session lifecycle is tracked in `dispatch_sessions` with token/cost accounting distinct from the parent. Sub-sessions run **serially** in the MVP — the call site blocks the parent event loop for the duration of the dispatch — a well-understood trade-off that can be relaxed to threaded parallel dispatch later without touching the protocol.

**Tech Stack:** Python 3.10+, anthropic SDK, psycopg2, pytest 9. No new runtime dependencies.

**Architectural constraints respected:**
- Container still cannot speak to postgres — every postgres write goes through the orchestrator (project_arch_constraint_db memory).
- `kernel-files/CLAUDE.md` is protected — the DISPATCH protocol is taught via `BOOTSTRAP_PROMPT` in `session_manager.py` and via an on-resume protocol refresh, never by editing protected files.
- Axiom 1 (Observable State): every dispatch writes `orch_activity_log` rows (start, result, failures) and a `dispatch_sessions` row with full lifecycle.
- Axiom 2 (Objective Verification): the parent Kernel is expected to dispatch the verifier node as a separate dispatch — the subsystem enforces nothing, but the node spec loader will not co-execute a task node and its verifier in the same sub-session.
- Axiom 5 (Entropy): failures are reported verbatim to the parent as `DISPATCH_RESULT status=FAILED` — the orchestrator never retries a failed dispatch blindly; the Kernel decides.

**Grounded in the Task 24 feasibility spike:**
- Sub-session setup overhead: ~0.7s agent.create + session.create
- Trivial roundtrip: ~4.8s (model-bound, not infra-bound)
- Realistic dispatch cost: ~$0.0275 for a 3000-in / 500-out Opus call
- Event lifecycle identical to parent: `user.message → running → span events → agent.message → idle`

---

## File Structure

**New files:**
- `kernel-files/infrastructure/db/008_dispatch.sql` — migration for `dispatch_agents` + `dispatch_sessions` tables
- `orchestrator/dispatch.py` — `DispatchManager` class + `parse_dispatch_fences` pure function; single responsibility: translate DISPATCH fences into sub-sessions and back
- `orchestrator/tests/test_dispatch.py` — unit tests covering fence parsing, agent caching, sub-session lifecycle (with mocked Anthropic + db), failure modes

**Modified files:**
- `orchestrator/db.py` — new methods: `get_dispatch_agent`, `upsert_dispatch_agent`, `record_dispatch_start`, `record_dispatch_complete`, `record_dispatch_failure`
- `orchestrator/session_manager.py` — new `DISPATCH_PROTOCOL` constant paralleling `SYNC_SNAPSHOT_PROTOCOL`, embedded in `BOOTSTRAP_PROMPT`; new `send_protocol_refresh` method for the on-resume path
- `orchestrator/event_consumer.py` — `_handle_message` routes DISPATCH fences to `dispatch_manager.handle_message` the same way it already routes SYNC fences to `file_sync`
- `orchestrator/__main__.py` — construct `DispatchManager`, pass it to `EventConsumer`, call `session_mgr.send_protocol_refresh()` after resuming an existing session

---

## Task 1: Migration for dispatch tables

**Files:**
- Create: `kernel-files/infrastructure/db/008_dispatch.sql`

- [ ] **Step 1: Write the migration**

Create `kernel-files/infrastructure/db/008_dispatch.sql`:

```sql
-- ORA Kernel Cloud: dispatch subsystem state (Option 3 — sub-sessions per node)

-- Per-node cached Anthropic Managed Agent IDs.
-- prompt_hash is a sha256 of the node spec file content — when the spec
-- changes, the orchestrator creates a fresh agent rather than reusing a
-- stale one.
CREATE TABLE IF NOT EXISTS dispatch_agents (
    node_name     TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- One row per dispatch. parent_session_id links the sub-session back to
-- the Kernel session that requested it. status moves RUNNING -> COMPLETE
-- or FAILED. Tokens/cost are copied out of the SSE stream at idle time.
CREATE TABLE IF NOT EXISTS dispatch_sessions (
    sub_session_id    TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    node_name         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running',
    input_data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_data       JSONB,
    input_tokens      BIGINT DEFAULT 0,
    output_tokens     BIGINT DEFAULT 0,
    cost_usd          NUMERIC(10,6) DEFAULT 0,
    duration_ms       INTEGER,
    error             TEXT,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_parent
    ON dispatch_sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_status
    ON dispatch_sessions(status);
CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_started
    ON dispatch_sessions(started_at DESC);
```

- [ ] **Step 2: Apply the migration**

Run: `psql -d ora_kernel -f kernel-files/infrastructure/db/008_dispatch.sql`
Expected: `CREATE TABLE` twice, `CREATE INDEX` three times, no errors.

- [ ] **Step 3: Verify the tables exist**

Run: `psql -d ora_kernel -c "\\d dispatch_agents" -c "\\d dispatch_sessions"`
Expected: table descriptions showing the columns above.

- [ ] **Step 4: Commit**

```bash
git add kernel-files/infrastructure/db/008_dispatch.sql
git commit -m "feat(db): dispatch_agents and dispatch_sessions tables"
```

---

## Task 2: db.py dispatch helpers — failing tests

**Files:**
- Create: `orchestrator/tests/test_db_dispatch.py`

- [ ] **Step 1: Write the failing tests**

Create `orchestrator/tests/test_db_dispatch.py`:

```python
"""Tests for dispatch-related Database helpers.

These tests hit a real postgres — they create temporary rows in
dispatch_agents and dispatch_sessions and clean up after themselves.
If POSTGRES_DSN is not reachable the tests are skipped.
"""
from __future__ import annotations

import os
import uuid

import pytest

from orchestrator.db import Database


def _dsn() -> str:
    return os.environ.get(
        "POSTGRES_DSN", "postgresql://u24@localhost:5432/ora_kernel"
    )


@pytest.fixture
def db():
    database = Database(_dsn())
    try:
        database.connect()
    except Exception as exc:
        pytest.skip(f"postgres not available: {exc}")
    yield database
    database.close()


def _random_node(prefix: str = "test_node_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def test_get_dispatch_agent_returns_none_for_unknown_node(db):
    assert db.get_dispatch_agent(_random_node()) is None


def test_upsert_dispatch_agent_then_get(db):
    node = _random_node()
    db.upsert_dispatch_agent(node, agent_id="agent_abc", prompt_hash="h1")

    row = db.get_dispatch_agent(node)
    assert row is not None
    assert row["agent_id"] == "agent_abc"
    assert row["prompt_hash"] == "h1"


def test_upsert_dispatch_agent_updates_on_hash_change(db):
    node = _random_node()
    db.upsert_dispatch_agent(node, agent_id="agent_old", prompt_hash="h1")
    db.upsert_dispatch_agent(node, agent_id="agent_new", prompt_hash="h2")

    row = db.get_dispatch_agent(node)
    assert row["agent_id"] == "agent_new"
    assert row["prompt_hash"] == "h2"


def test_record_dispatch_start_inserts_running_row(db):
    sub_id = f"test_sub_{uuid.uuid4().hex[:8]}"
    db.record_dispatch_start(
        sub_session_id=sub_id,
        parent_session_id="sesn_parent_test",
        node_name="test_node",
        input_data={"task": "demo"},
    )

    with db.cursor() as cur:
        cur.execute(
            "SELECT status, input_data FROM dispatch_sessions WHERE sub_session_id=%s",
            (sub_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["status"] == "running"
    assert row["input_data"] == {"task": "demo"}


def test_record_dispatch_complete_updates_row(db):
    sub_id = f"test_sub_{uuid.uuid4().hex[:8]}"
    db.record_dispatch_start(
        sub_session_id=sub_id,
        parent_session_id="sesn_parent_test",
        node_name="test_node",
        input_data={"task": "demo"},
    )
    db.record_dispatch_complete(
        sub_session_id=sub_id,
        output_data={"result": "ok"},
        input_tokens=100,
        output_tokens=25,
        cost_usd=0.00125,
        duration_ms=4810,
    )

    with db.cursor() as cur:
        cur.execute(
            "SELECT status, output_data, input_tokens, output_tokens, cost_usd, duration_ms "
            "FROM dispatch_sessions WHERE sub_session_id=%s",
            (sub_id,),
        )
        row = cur.fetchone()
    assert row["status"] == "complete"
    assert row["output_data"] == {"result": "ok"}
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 25
    assert float(row["cost_usd"]) == pytest.approx(0.00125)
    assert row["duration_ms"] == 4810


def test_record_dispatch_failure_updates_row(db):
    sub_id = f"test_sub_{uuid.uuid4().hex[:8]}"
    db.record_dispatch_start(
        sub_session_id=sub_id,
        parent_session_id="sesn_parent_test",
        node_name="test_node",
        input_data={},
    )
    db.record_dispatch_failure(sub_session_id=sub_id, error="sub-session terminated")

    with db.cursor() as cur:
        cur.execute(
            "SELECT status, error FROM dispatch_sessions WHERE sub_session_id=%s",
            (sub_id,),
        )
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "sub-session terminated"
```

- [ ] **Step 2: Run — expect import errors (missing methods)**

Run: `python3 -m pytest orchestrator/tests/test_db_dispatch.py -v`
Expected: multiple failures with `AttributeError: 'Database' object has no attribute 'get_dispatch_agent'` etc.

---

## Task 3: db.py dispatch helpers — implementation

**Files:**
- Modify: `orchestrator/db.py` (append methods)

- [ ] **Step 1: Append the helper methods**

At the end of the `Database` class in `orchestrator/db.py`, append:

```python
    # ── Dispatch subsystem helpers ────────────────────────────────────

    def get_dispatch_agent(self, node_name: str) -> Optional[dict]:
        """Return {'agent_id', 'prompt_hash'} for a cached node agent, or None."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT agent_id, prompt_hash FROM dispatch_agents WHERE node_name=%s",
                (node_name,),
            )
            return cur.fetchone()

    def upsert_dispatch_agent(
        self, node_name: str, agent_id: str, prompt_hash: str
    ) -> None:
        """Cache or refresh the Anthropic agent ID for a node."""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dispatch_agents (node_name, agent_id, prompt_hash)
                VALUES (%s, %s, %s)
                ON CONFLICT (node_name)
                DO UPDATE SET agent_id = %s, prompt_hash = %s, created_at = NOW()
                """,
                (node_name, agent_id, prompt_hash, agent_id, prompt_hash),
            )

    def record_dispatch_start(
        self,
        sub_session_id: str,
        parent_session_id: str,
        node_name: str,
        input_data: dict,
    ) -> None:
        """Insert a fresh dispatch_sessions row with status='running'."""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dispatch_sessions
                    (sub_session_id, parent_session_id, node_name, status, input_data)
                VALUES (%s, %s, %s, 'running', %s)
                """,
                (sub_session_id, parent_session_id, node_name, json.dumps(input_data)),
            )

    def record_dispatch_complete(
        self,
        sub_session_id: str,
        output_data: dict,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        duration_ms: int,
    ) -> None:
        """Mark a dispatch row complete with its final metrics."""
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE dispatch_sessions
                SET status       = 'complete',
                    output_data  = %s,
                    input_tokens = %s,
                    output_tokens= %s,
                    cost_usd     = %s,
                    duration_ms  = %s,
                    completed_at = NOW()
                WHERE sub_session_id = %s
                """,
                (
                    json.dumps(output_data),
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    duration_ms,
                    sub_session_id,
                ),
            )

    def record_dispatch_failure(self, sub_session_id: str, error: str) -> None:
        """Mark a dispatch row failed with an error description."""
        with self.cursor() as cur:
            cur.execute(
                """
                UPDATE dispatch_sessions
                SET status       = 'failed',
                    error        = %s,
                    completed_at = NOW()
                WHERE sub_session_id = %s
                """,
                (error, sub_session_id),
            )
```

- [ ] **Step 2: Run the tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_db_dispatch.py -v`
Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/db.py orchestrator/tests/test_db_dispatch.py
git commit -m "feat(db): dispatch_agents + dispatch_sessions CRUD helpers"
```

---

## Task 4: `parse_dispatch_fences` — failing test

**Files:**
- Create: `orchestrator/tests/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

Create `orchestrator/tests/test_dispatch.py`:

```python
"""Tests for orchestrator.dispatch."""
from __future__ import annotations

import json

import pytest

from orchestrator.dispatch import parse_dispatch_fences


def test_parse_single_dispatch_fence():
    text = (
        "Planning complete — dispatching.\n"
        "```DISPATCH node=business_analyst\n"
        '{"task": "research async patterns", "budget_size": "S"}\n'
        "```\n"
        "Awaiting result."
    )
    result = parse_dispatch_fences(text)
    assert len(result) == 1
    node, payload = result[0]
    assert node == "business_analyst"
    assert payload == {"task": "research async patterns", "budget_size": "S"}


def test_parse_multiple_dispatch_fences():
    text = (
        "```DISPATCH node=node_designer\n{\"task\": \"design researcher\"}\n```\n"
        "Then:\n"
        "```DISPATCH node=node_creator\n{\"task\": \"build researcher\"}\n```\n"
    )
    result = parse_dispatch_fences(text)
    assert len(result) == 2
    assert result[0][0] == "node_designer"
    assert result[1][0] == "node_creator"


def test_parse_ignores_non_dispatch_fences():
    text = (
        "```python\nprint('hi')\n```\n"
        '```DISPATCH node=business_analyst\n{"task": "x"}\n```\n'
    )
    result = parse_dispatch_fences(text)
    assert len(result) == 1
    assert result[0][0] == "business_analyst"


def test_parse_skips_fence_with_invalid_json():
    text = (
        "```DISPATCH node=business_analyst\n"
        "not valid json\n"
        "```\n"
    )
    # Malformed payloads are silently skipped — the orchestrator cannot
    # dispatch something it cannot parse, and will report nothing back
    # rather than guess.
    assert parse_dispatch_fences(text) == []


def test_parse_skips_fence_without_node_attr():
    text = (
        "```DISPATCH\n"
        '{"task": "x"}\n'
        "```\n"
    )
    assert parse_dispatch_fences(text) == []


def test_parse_empty_on_no_fences():
    assert parse_dispatch_fences("no fences here") == []
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v`
Expected: `ModuleNotFoundError: No module named 'orchestrator.dispatch'`.

---

## Task 5: `parse_dispatch_fences` — implementation scaffold

**Files:**
- Create: `orchestrator/dispatch.py`

- [ ] **Step 1: Write the module scaffold**

Create `orchestrator/dispatch.py`:

```python
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
```

- [ ] **Step 2: Run the tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v`
Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/dispatch.py orchestrator/tests/test_dispatch.py
git commit -m "feat(dispatch): parse_dispatch_fences for intent detection"
```

---

## Task 6: `DispatchManager` scaffold + node spec loader — failing tests

**Files:**
- Modify: `orchestrator/tests/test_dispatch.py`

- [ ] **Step 1: Append node spec loader tests**

Append to `orchestrator/tests/test_dispatch.py`:

```python
# ── Node spec loader ────────────────────────────────────────────────

from pathlib import Path
from unittest.mock import MagicMock
import tempfile

from orchestrator.dispatch import DispatchManager


def _make_manager(tmp_path: Path, **overrides):
    (tmp_path / "valid_node.md").write_text(
        "---\nname: Valid\n---\n\n## System Prompt\n\nYou are the ValidNode.\n"
    )
    db = MagicMock()
    db.get_dispatch_agent = MagicMock(return_value=None)
    db.upsert_dispatch_agent = MagicMock()
    db.record_dispatch_start = MagicMock()
    db.record_dispatch_complete = MagicMock()
    db.record_dispatch_failure = MagicMock()
    client = MagicMock()
    send_to_parent = MagicMock()
    defaults = dict(
        db=db,
        client=client,
        environment_id="env_test",
        send_to_parent=send_to_parent,
        node_spec_dir=tmp_path,
    )
    defaults.update(overrides)
    return DispatchManager(**defaults), db, client, send_to_parent


def test_load_node_spec_returns_file_contents(tmp_path):
    manager, *_ = _make_manager(tmp_path)
    spec = manager._load_node_spec("valid_node")
    assert "ValidNode" in spec
    assert "System Prompt" in spec


def test_load_node_spec_raises_for_unknown_node(tmp_path):
    manager, *_ = _make_manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        manager._load_node_spec("does_not_exist")


def test_node_spec_hash_is_content_addressed(tmp_path):
    manager, *_ = _make_manager(tmp_path)
    h1 = manager._spec_hash("valid_node")
    assert len(h1) == 64  # sha256 hex
    # Mutate the file; hash must change
    (tmp_path / "valid_node.md").write_text("different content")
    h2 = manager._spec_hash("valid_node")
    assert h1 != h2
```

- [ ] **Step 2: Run — expect ImportError for DispatchManager**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py::test_load_node_spec_returns_file_contents -v`
Expected: `ImportError: cannot import name 'DispatchManager' from 'orchestrator.dispatch'`.

---

## Task 7: `DispatchManager` scaffold + node spec loader — implementation

**Files:**
- Modify: `orchestrator/dispatch.py`

- [ ] **Step 1: Append the class scaffold**

Append to `orchestrator/dispatch.py`:

```python
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
```

- [ ] **Step 2: Run the loader tests — expect pass**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "load_node_spec or spec_hash"`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/dispatch.py orchestrator/tests/test_dispatch.py
git commit -m "feat(dispatch): DispatchManager scaffold + node spec loader"
```

---

## Task 8: Agent get-or-create — failing tests

**Files:**
- Modify: `orchestrator/tests/test_dispatch.py`

- [ ] **Step 1: Append agent-cache tests**

Append to `orchestrator/tests/test_dispatch.py`:

```python
from types import SimpleNamespace


def test_ensure_agent_creates_fresh_agent_when_cache_empty(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    db.get_dispatch_agent.return_value = None
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")

    agent_id = manager._ensure_agent("valid_node")

    assert agent_id == "agent_new"
    client.beta.agents.create.assert_called_once()
    call = client.beta.agents.create.call_args
    assert call.kwargs["name"] == "ora-dispatch-valid_node"
    assert "ValidNode" in call.kwargs["system"]
    assert call.kwargs["tools"] == [{"type": "agent_toolset_20260401"}]
    db.upsert_dispatch_agent.assert_called_once()
    upsert_kwargs = db.upsert_dispatch_agent.call_args.kwargs
    assert upsert_kwargs["node_name"] == "valid_node"
    assert upsert_kwargs["agent_id"] == "agent_new"
    assert len(upsert_kwargs["prompt_hash"]) == 64


def test_ensure_agent_reuses_cached_agent_when_hash_matches(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    current_hash = manager._spec_hash("valid_node")
    db.get_dispatch_agent.return_value = {
        "agent_id": "agent_cached",
        "prompt_hash": current_hash,
    }

    agent_id = manager._ensure_agent("valid_node")

    assert agent_id == "agent_cached"
    client.beta.agents.create.assert_not_called()
    db.upsert_dispatch_agent.assert_not_called()


def test_ensure_agent_rebuilds_when_spec_hash_drifts(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    db.get_dispatch_agent.return_value = {
        "agent_id": "agent_stale",
        "prompt_hash": "stale_hash_0000",
    }
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_rebuilt")

    agent_id = manager._ensure_agent("valid_node")

    assert agent_id == "agent_rebuilt"
    client.beta.agents.create.assert_called_once()
    db.upsert_dispatch_agent.assert_called_once()
```

- [ ] **Step 2: Run — expect AttributeError on `_ensure_agent`**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "ensure_agent"`
Expected: 3 failures (`AttributeError: 'DispatchManager' object has no attribute '_ensure_agent'`).

---

## Task 9: Agent get-or-create — implementation

**Files:**
- Modify: `orchestrator/dispatch.py`

- [ ] **Step 1: Append `_ensure_agent`**

Append inside the `DispatchManager` class in `orchestrator/dispatch.py`:

```python
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
```

- [ ] **Step 2: Run the agent-cache tests — expect pass**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "ensure_agent"`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/dispatch.py orchestrator/tests/test_dispatch.py
git commit -m "feat(dispatch): per-node agent cache with hash invalidation"
```

---

## Task 10: Sub-session lifecycle (`_run_sub_session`) — failing tests

**Files:**
- Modify: `orchestrator/tests/test_dispatch.py`

- [ ] **Step 1: Append sub-session tests**

Append to `orchestrator/tests/test_dispatch.py`:

```python
# ── Sub-session lifecycle ───────────────────────────────────────────


class _FakeStream:
    """Context manager that yields a canned list of events."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *exc):
        return False


def _model_end(input_tokens, output_tokens):
    return SimpleNamespace(
        type="span.model_request_end",
        model="claude-opus-4-6",
        model_usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _agent_message(text):
    return SimpleNamespace(
        type="agent.message",
        content=[SimpleNamespace(text=text)],
    )


def _idle():
    return SimpleNamespace(type="session.status_idle", stop_reason=None)


def _terminated(error="boom"):
    return SimpleNamespace(type="session.status_terminated", error=error)


def test_run_sub_session_returns_successful_result(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub_1")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(100, 25),
        _agent_message("Research complete. Findings: ..."),
        _idle(),
    ])

    result = manager._run_sub_session(
        parent_session_id="sesn_parent",
        agent_id="agent_abc",
        node_name="valid_node",
        input_data={"task": "do the thing"},
    )

    assert result["status"] == "complete"
    assert "Research complete" in result["output"]
    assert result["tokens"]["input"] == 100
    assert result["tokens"]["output"] == 25
    assert result["cost_usd"] > 0
    assert result["sub_session_id"] == "sesn_sub_1"
    db.record_dispatch_start.assert_called_once()
    db.record_dispatch_complete.assert_called_once()
    db.record_dispatch_failure.assert_not_called()


def test_run_sub_session_reports_termination_as_failure(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub_2")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(50, 0),
        _terminated("container restart"),
    ])

    result = manager._run_sub_session(
        parent_session_id="sesn_parent",
        agent_id="agent_abc",
        node_name="valid_node",
        input_data={},
    )

    assert result["status"] == "failed"
    assert "container restart" in result["error"]
    db.record_dispatch_failure.assert_called_once()
    db.record_dispatch_complete.assert_not_called()


def test_run_sub_session_sends_input_as_user_message(tmp_path):
    manager, db, client, _ = _make_manager(tmp_path)
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub_3")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(10, 5),
        _agent_message("ok"),
        _idle(),
    ])

    manager._run_sub_session(
        parent_session_id="sesn_parent",
        agent_id="agent_abc",
        node_name="valid_node",
        input_data={"task": "ping"},
    )

    send_call = client.beta.sessions.events.send.call_args
    assert send_call.args[0] == "sesn_sub_3"
    events = send_call.kwargs["events"]
    assert events[0]["type"] == "user.message"
    payload_text = events[0]["content"][0]["text"]
    assert "ping" in payload_text
```

- [ ] **Step 2: Run — expect failures on `_run_sub_session`**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "run_sub_session"`
Expected: 3 failures on missing `_run_sub_session`.

---

## Task 11: Sub-session lifecycle — implementation

**Files:**
- Modify: `orchestrator/dispatch.py`

- [ ] **Step 1: Append `_run_sub_session` and helpers**

Append inside the `DispatchManager` class in `orchestrator/dispatch.py`:

```python
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
```

- [ ] **Step 2: Run sub-session tests — expect pass**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "run_sub_session"`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/dispatch.py orchestrator/tests/test_dispatch.py
git commit -m "feat(dispatch): sub-session lifecycle with timeout and failure capture"
```

---

## Task 12: `handle_message` — failing tests

**Files:**
- Modify: `orchestrator/tests/test_dispatch.py`

- [ ] **Step 1: Append top-level `handle_message` tests**

Append to `orchestrator/tests/test_dispatch.py`:

```python
# ── DispatchManager.handle_message (top-level entry point) ─────────

def test_handle_message_dispatches_fence_and_sends_result(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_new")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(100, 25),
        _agent_message("result body"),
        _idle(),
    ])

    text = (
        "Dispatching.\n"
        "```DISPATCH node=valid_node\n"
        '{"task": "demo"}\n'
        "```\n"
    )
    count = manager.handle_message(parent_session_id="sesn_parent", message_text=text)

    assert count == 1
    # Result forwarded to parent as a DISPATCH_RESULT fence
    send_to_parent.assert_called_once()
    result_args = send_to_parent.call_args
    assert result_args.args[0] == "sesn_parent"
    forwarded_text = result_args.args[1]
    assert "```DISPATCH_RESULT" in forwarded_text
    assert "node=valid_node" in forwarded_text
    assert "status=complete" in forwarded_text
    assert "result body" in forwarded_text


def test_handle_message_reports_missing_node_as_failure(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    text = (
        "```DISPATCH node=nonexistent\n"
        '{"task": "x"}\n'
        "```\n"
    )
    count = manager.handle_message(parent_session_id="sesn_parent", message_text=text)

    assert count == 1
    send_to_parent.assert_called_once()
    forwarded = send_to_parent.call_args.args[1]
    assert "status=failed" in forwarded
    assert "nonexistent" in forwarded
    client.beta.agents.create.assert_not_called()


def test_handle_message_returns_zero_when_no_fences(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    count = manager.handle_message("sesn_parent", "just prose")
    assert count == 0
    send_to_parent.assert_not_called()


def test_handle_message_continues_after_per_dispatch_failure(tmp_path):
    manager, db, client, send_to_parent = _make_manager(tmp_path)
    # Build a valid second node spec
    (tmp_path / "second_node.md").write_text(
        "---\nname: Second\n---\n\n## System Prompt\n\nYou are Second.\n"
    )
    client.beta.agents.create.return_value = SimpleNamespace(id="agent_second")
    client.beta.sessions.create.return_value = SimpleNamespace(id="sesn_sub")
    client.beta.sessions.events.stream.return_value = _FakeStream([
        _model_end(5, 5),
        _agent_message("ok"),
        _idle(),
    ])

    text = (
        "```DISPATCH node=missing\n{\"task\": \"a\"}\n```\n"
        "```DISPATCH node=second_node\n{\"task\": \"b\"}\n```\n"
    )
    count = manager.handle_message("sesn_parent", text)

    assert count == 2
    # Both fences produce a DISPATCH_RESULT (one failed, one complete)
    assert send_to_parent.call_count == 2
    first, second = send_to_parent.call_args_list
    assert "status=failed" in first.args[1]
    assert "status=complete" in second.args[1]
```

- [ ] **Step 2: Run — expect failures on missing `handle_message`**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "handle_message"`
Expected: 4 failures.

---

## Task 13: `handle_message` — implementation

**Files:**
- Modify: `orchestrator/dispatch.py`

- [ ] **Step 1: Append `handle_message` + `_format_result_fence`**

Append inside the `DispatchManager` class in `orchestrator/dispatch.py`:

```python
    # ── Top-level entry point ───────────────────────────────────────

    def handle_message(self, parent_session_id: str, message_text: str) -> int:
        """Parse DISPATCH fences in *message_text*, execute each, forward results.

        Returns the number of fences processed (whether successful or
        failed). Per-dispatch exceptions are caught and converted to
        FAILED results so one bad dispatch never prevents later ones
        from running.
        """
        if not message_text:
            return 0
        fences = parse_dispatch_fences(message_text)
        if not fences:
            return 0

        logger.info(
            "dispatch: parent=%s found %d fence(s)", parent_session_id, len(fences)
        )
        for node_name, input_data in fences:
            try:
                agent_id = self._ensure_agent(node_name)
                result = self._run_sub_session(
                    parent_session_id=parent_session_id,
                    agent_id=agent_id,
                    node_name=node_name,
                    input_data=input_data,
                )
            except FileNotFoundError as exc:
                result = {
                    "status": "failed",
                    "node_name": node_name,
                    "error": f"node spec not found: {exc}",
                    "output": "",
                    "tokens": {"input": 0, "output": 0},
                    "cost_usd": 0.0,
                    "duration_ms": 0,
                    "sub_session_id": None,
                }
            except Exception as exc:  # noqa: BLE001 — top-level safety net
                logger.exception("dispatch: unexpected error for %s", node_name)
                result = {
                    "status": "failed",
                    "node_name": node_name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "output": "",
                    "tokens": {"input": 0, "output": 0},
                    "cost_usd": 0.0,
                    "duration_ms": 0,
                    "sub_session_id": None,
                }

            try:
                self.send_to_parent(
                    parent_session_id, self._format_result_fence(result)
                )
            except Exception:
                logger.exception(
                    "dispatch: failed to forward result for %s to parent", node_name
                )

        return len(fences)

    # ── Result formatting ───────────────────────────────────────────

    @staticmethod
    def _format_result_fence(result: Dict[str, Any]) -> str:
        """Render a result dict as a ```DISPATCH_RESULT``` fenced block.

        The parent Kernel parses these the same way the orchestrator
        parses its DISPATCH fences. Single fenced block, no surrounding
        prose — the Kernel is instructed to read the fence as the
        authoritative subagent return value.
        """
        header = (
            f"```DISPATCH_RESULT node={result['node_name']} "
            f"status={result['status']}"
        )
        body = {
            "output": result.get("output", ""),
            "tokens": result.get("tokens", {"input": 0, "output": 0}),
            "cost_usd": result.get("cost_usd", 0.0),
            "duration_ms": result.get("duration_ms", 0),
            "sub_session_id": result.get("sub_session_id"),
            "error": result.get("error"),
        }
        return (
            f"{header}\n{json.dumps(body, indent=2, default=str)}\n```"
        )
```

- [ ] **Step 2: Run `handle_message` tests — expect pass**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v -k "handle_message"`
Expected: 4 passed.

- [ ] **Step 3: Run the full `test_dispatch.py` suite**

Run: `python3 -m pytest orchestrator/tests/test_dispatch.py -v`
Expected: 22 passed.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/dispatch.py orchestrator/tests/test_dispatch.py
git commit -m "feat(dispatch): DispatchManager.handle_message entry point"
```

---

## Task 14: Integrate DispatchManager into EventConsumer

**Files:**
- Modify: `orchestrator/event_consumer.py`
- Modify: `orchestrator/tests/test_event_consumer_cdc.py`

- [ ] **Step 1: Add the `dispatch_manager` parameter to `EventConsumer`**

In `orchestrator/event_consumer.py`, update the `EventConsumer.__init__` signature:

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
    ):
        self.db = db
        self.client = Anthropic(api_key=api_key)
        self.agent_id = agent_id
        self.environment_id = environment_id
        self.on_event = on_event
        self.on_hitl_needed = on_hitl_needed
        self.file_sync = file_sync
        self.dispatch_manager = dispatch_manager
        self.totals = SessionTotals()
```

Add to the `TYPE_CHECKING` block near the top of the file:

```python
if TYPE_CHECKING:
    from orchestrator.file_sync import FileSync
    from orchestrator.dispatch import DispatchManager
```

- [ ] **Step 2: Route messages to the dispatch manager**

In `orchestrator/event_consumer.py`, replace the `_handle_message` body with:

```python
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
        if not full_text:
            return
        if self.file_sync is not None:
            try:
                self.file_sync.handle_snapshot_response(full_text)
            except Exception:
                logger.exception("file_sync snapshot handler failed")
        if self.dispatch_manager is not None:
            try:
                self.dispatch_manager.handle_message(session_id, full_text)
            except Exception:
                logger.exception("dispatch_manager.handle_message failed")
```

- [ ] **Step 3: Add an integration test**

Append to `orchestrator/tests/test_event_consumer_cdc.py`:

```python
def test_message_event_routes_to_dispatch_manager():
    fs = MagicMock()
    dm = MagicMock()
    db = MagicMock()
    consumer = EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        file_sync=fs,
        dispatch_manager=dm,
    )
    event = _message_event(
        "```DISPATCH node=foo\n{\"task\":\"x\"}\n```"
    )
    consumer._handle_message("sess_parent", event)

    dm.handle_message.assert_called_once()
    args = dm.handle_message.call_args
    assert args.args[0] == "sess_parent"
    assert "DISPATCH node=foo" in args.args[1]
    # file_sync should also still be called — both paths fire on every message
    fs.handle_snapshot_response.assert_called_once()


def test_dispatch_manager_is_optional():
    consumer = EventConsumer(
        db=MagicMock(),
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
    )
    # No dispatch_manager passed — should not raise
    consumer._handle_message("sess_parent", _message_event("plain message"))
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest orchestrator/tests/ -v`
Expected: all tests green (previous 45 + 22 dispatch + 2 new event_consumer).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/event_consumer.py orchestrator/tests/test_event_consumer_cdc.py
git commit -m "feat(event_consumer): route DISPATCH fences to DispatchManager"
```

---

## Task 15: `DISPATCH_PROTOCOL` constant + bootstrap embedding + resume refresh

**Files:**
- Modify: `orchestrator/session_manager.py`
- Modify: `orchestrator/tests/test_scheduler_triggers.py`

- [ ] **Step 1: Add the `DISPATCH_PROTOCOL` constant**

In `orchestrator/session_manager.py`, add after the `SYNC_SNAPSHOT_PROTOCOL` definition:

```python
# Protocol the Kernel must follow to request a subagent dispatch.
# The Managed Agent toolset does not expose an Agent/Task tool, so the
# Kernel signals dispatch intent via fenced blocks in its response. The
# orchestrator's DispatchManager parses these, creates a focused sub-
# session per dispatch, consumes events, and returns the result as a
# ```DISPATCH_RESULT``` fence sent back to the Kernel as a user.message.
DISPATCH_PROTOCOL = """=== dispatch protocol ===

You do NOT have an Agent, Task, or subagent-dispatch tool in this
environment. The CLAUDE.md dispatch instructions that reference an
"Agent tool" are obsolete in the cloud. Use this fence-based protocol
instead.

To dispatch a node, emit a single fenced block in your response:

```DISPATCH node=<node_name>
{
  "task": "<task description>",
  "input": { ... node-specific input fields ... },
  "budget_size": "S" | "M" | "L"
}
```

Rules:
- `node_name` is the base filename of the node spec (e.g. `business_analyst`,
  `node_designer`) without the `.md` suffix or directory prefix.
- The body MUST be valid JSON — the orchestrator will silently skip
  unparseable fences.
- Emit ONE dispatch per message to start. Multiple fences in one message
  will all be dispatched, but serially — they do not run in parallel.
- After emitting a DISPATCH fence, wait. The orchestrator will reply
  with a `user.message` containing a single fenced block:

```DISPATCH_RESULT node=<node_name> status=<complete|failed>
{
  "output": "<subagent's full text response>",
  "tokens": { "input": N, "output": N },
  "cost_usd": N,
  "duration_ms": N,
  "sub_session_id": "<id>",
  "error": null | "<error description>"
}
```

- Treat the DISPATCH_RESULT as the authoritative return of the node.
- On `status=failed`, apply Axiom 5 — analyze the error, do NOT retry
  the same dispatch blindly. Consider whether a different node, a
  reformulated input, or a HITL escalation is appropriate.
- Per Axiom 2 (Objective Verification), after a worker node returns
  UNVERIFIED, dispatch its paired verifier node in a SEPARATE
  DISPATCH fence — never combine worker and verifier into one call.

=== end dispatch protocol ==="""
```

- [ ] **Step 2: Embed `DISPATCH_PROTOCOL` in `BOOTSTRAP_PROMPT`**

In `orchestrator/session_manager.py`, update `BOOTSTRAP_PROMPT` — replace the `{sync_snapshot_protocol}` placeholder block with both protocols:

```python
After bootstrap, you will receive periodic triggers (/heartbeat, /briefing,
/idle-work, /consolidate, /sync-snapshot) from the user's scheduler.
Respond to them per your operating instructions in CLAUDE.md.

{sync_snapshot_protocol}

{dispatch_protocol}

DO NOT attempt to contact a PostgreSQL database — that is handled by the
orchestrator outside the container via the event stream. Your job is to
reason and act; the orchestrator records everything.

{hydration_instructions}
"""
```

- [ ] **Step 3: Update `bootstrap()` to pass the new constant**

In `orchestrator/session_manager.py`, update the `.format()` call inside `bootstrap()`:

```python
        prompt = BOOTSTRAP_PROMPT.format(
            repo_url=repo_url,
            sync_snapshot_protocol=SYNC_SNAPSHOT_PROTOCOL,
            dispatch_protocol=DISPATCH_PROTOCOL,
            hydration_instructions=hydration,
        )
```

- [ ] **Step 4: Add `send_protocol_refresh` method**

Append to the `SessionManager` class in `orchestrator/session_manager.py`:

```python
    def send_protocol_refresh(self) -> None:
        """Re-teach the current SYNC + DISPATCH protocols to the active session.

        Resumed sessions were bootstrapped with whatever protocol was
        current at creation time. On every daemon boot we send a single
        user.message with the latest protocols so the Kernel's behavior
        always matches the orchestrator's parser — no silent drift.
        """
        if not self.session_id:
            return
        message = (
            "PROTOCOL REFRESH — the orchestrator has just (re)started. "
            "Please adopt the following protocols for the rest of this "
            "session. If your bootstrap already contained them, treat "
            "this as a harmless reminder.\n\n"
            f"{SYNC_SNAPSHOT_PROTOCOL}\n\n"
            f"{DISPATCH_PROTOCOL}"
        )
        self.send_message(message)
        logger.info("Sent protocol refresh to %s", self.session_id)
```

- [ ] **Step 5: Update `test_scheduler_triggers.py` to format with the new kwarg**

In `orchestrator/tests/test_scheduler_triggers.py`, update `test_bootstrap_prompt_still_embeds_protocol`:

```python
def test_bootstrap_prompt_still_embeds_protocol():
    """Refactoring the protocol into a constant must not break the
    bootstrap prompt — new sessions still need to learn it at boot."""
    from orchestrator.session_manager import (
        BOOTSTRAP_PROMPT,
        DISPATCH_PROTOCOL,
    )

    rendered = BOOTSTRAP_PROMPT.format(
        repo_url="https://example.com/repo.git",
        sync_snapshot_protocol=SYNC_SNAPSHOT_PROTOCOL,
        dispatch_protocol=DISPATCH_PROTOCOL,
        hydration_instructions="",
    )
    assert "```SYNC path=" in rendered
    assert "/sync-snapshot" in rendered
    assert "```DISPATCH node=" in rendered
    assert "DISPATCH_RESULT" in rendered
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest orchestrator/tests/ -v`
Expected: all tests green.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/session_manager.py orchestrator/tests/test_scheduler_triggers.py
git commit -m "feat(session_manager): DISPATCH protocol constant + resume refresh"
```

---

## Task 16: Wire DispatchManager into `__main__.py` + on-resume refresh

**Files:**
- Modify: `orchestrator/__main__.py`

- [ ] **Step 1: Add imports**

Near the top of `orchestrator/__main__.py`, add:

```python
from pathlib import Path

from orchestrator.dispatch import DispatchManager
```

- [ ] **Step 2: Build `DispatchManager` and pass to `EventConsumer`**

In `orchestrator/__main__.py`, replace the block that builds the consumer:

```python
    # File sync (change-data-capture + snapshot reconciliation)
    file_sync = FileSync(db)

    # HITL handler — stdin prompt, hot-swappable for dashboard later
    hitl = StdinHitlHandler(send_response=session_mgr.send_tool_confirmation)

    # Event consumer
    consumer = EventConsumer(
        db=db,
        api_key=api_key,
        agent_id=agent_id,
        environment_id=env_id,
        on_hitl_needed=hitl.handle,
        file_sync=file_sync,
    )
```

with:

```python
    # File sync (change-data-capture + snapshot reconciliation)
    file_sync = FileSync(db)

    # HITL handler — stdin prompt, hot-swappable for dashboard later
    hitl = StdinHitlHandler(send_response=session_mgr.send_tool_confirmation)

    # Dispatch manager — translates DISPATCH fences into sub-sessions
    node_spec_dir = (
        Path(__file__).resolve().parent.parent
        / "kernel-files"
        / ".claude"
        / "kernel"
        / "nodes"
    )
    # Flat lookup helper: build a list of every .md file under node_spec_dir
    # and let DispatchManager resolve node names via the top-level dir.
    # DispatchManager.node_spec_dir accepts a root; _spec_path treats a
    # node_name as "<name>.md" directly underneath. To support nested
    # node directories we pass a flat view via a symlinked index later;
    # for the MVP we point at the system nodes dir where the bootstrap
    # node set lives.
    dispatch_manager = DispatchManager(
        db=db,
        client=Anthropic(api_key=api_key),
        environment_id=env_id,
        send_to_parent=session_mgr.send_message,
        node_spec_dir=node_spec_dir / "system",
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

Also add the `Anthropic` import near the top of `orchestrator/__main__.py` if it is not already imported:

```python
from anthropic import Anthropic
```

- [ ] **Step 3: Call `send_protocol_refresh` after resuming an existing session**

In `orchestrator/__main__.py`, in the block that resumes an existing session, update:

```python
        else:
            logger.info(f"Resuming existing session: {session_mgr.session_id}")
```

to:

```python
        else:
            logger.info(f"Resuming existing session: {session_mgr.session_id}")
            session_mgr.send_protocol_refresh()
```

- [ ] **Step 4: Update the restart-path consumer construction to pass `dispatch_manager`**

In `orchestrator/__main__.py`, inside the `while running:` loop, replace the restart-path consumer construction:

```python
                    consumer = EventConsumer(
                        db=db,
                        api_key=api_key,
                        agent_id=agent_id,
                        environment_id=env_id,
                        on_hitl_needed=hitl.handle,
                        file_sync=file_sync,
                    )
```

with:

```python
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

- [ ] **Step 5: Import-check the module and run the full suite**

Run: `python3 -c "import orchestrator.__main__" && python3 -m pytest orchestrator/tests/ -v`
Expected: clean import; all tests green.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/__main__.py
git commit -m "feat(orchestrator): wire DispatchManager + protocol refresh on resume"
```

---

## Task 17: End-to-end smoke test (semi-manual)

This task is a guided manual checklist. It cannot be a script because it exercises the live Managed Agent infrastructure.

- [ ] **Step 1: Apply the migration**

Run: `psql -d ora_kernel -f kernel-files/infrastructure/db/008_dispatch.sql`
Expected: no errors; tables exist from Task 1 but `IF NOT EXISTS` makes this idempotent.

- [ ] **Step 2: Start the orchestrator**

Run: `python3 -m orchestrator`
Expected: banner; log line "Sent protocol refresh to sesn_..." appears shortly after resume.

- [ ] **Step 3: Verify the protocol refresh landed in the activity log**

In a second shell, run:

```
psql -d ora_kernel -c "SELECT id, action, substring(details->>'text' from 1 for 80) as preview FROM orch_activity_log WHERE action='MESSAGE' ORDER BY id DESC LIMIT 3;"
```

Expected: The Kernel should acknowledge the refresh within a few seconds. Look for a message that references "DISPATCH" or "sync-snapshot" protocols — whatever the Kernel says back.

- [ ] **Step 4: Send a dispatch-requiring task**

Run:

```
python3 -m orchestrator --send "Please dispatch the business_analyst node with the following payload as input: {\"task\": \"Summarize the current dispatch protocol in one paragraph\", \"input\": {}, \"budget_size\": \"S\"}. Emit only the DISPATCH fence — no surrounding prose. Wait for my reply."
```

Expected: within ~10 seconds the Kernel emits a single `` ```DISPATCH node=business_analyst `` `` fenced block.

- [ ] **Step 5: Verify the sub-session was created**

```
psql -d ora_kernel -c "SELECT sub_session_id, parent_session_id, node_name, status, input_tokens, output_tokens, cost_usd, duration_ms FROM dispatch_sessions ORDER BY started_at DESC LIMIT 3;"
```

Expected: one row with `status='complete'` (or `'failed'` — see Step 6), non-zero token counts, non-zero cost.

- [ ] **Step 6: Verify the DISPATCH_RESULT landed back in the parent session**

```
psql -d ora_kernel -c "SELECT id, substring(details->>'text' from 1 for 200) FROM orch_activity_log WHERE action='MESSAGE' AND details->>'text' LIKE '%DISPATCH_RESULT%' ORDER BY id DESC LIMIT 2;"
```

Expected: a message containing `` ```DISPATCH_RESULT node=business_analyst status=complete `` `` with the subagent's output body.

- [ ] **Step 7: Verify the agent cache**

```
psql -d ora_kernel -c "SELECT node_name, substring(agent_id from 1 for 30) as agent, substring(prompt_hash from 1 for 10) as hash, created_at FROM dispatch_agents;"
```

Expected: one row for `business_analyst` with an `agent_id` starting `agent_`.

- [ ] **Step 8: Dispatch the same node a second time (cache hit)**

Re-run the Step 4 command. Expected: the second dispatch should reuse the same `agent_id` (no new row in `dispatch_agents`, no second `agents.create` call — you can verify the latter by grepping the orchestrator stdout for "creating fresh agent").

- [ ] **Step 9: Dispatch a non-existent node (failure path)**

Run:

```
python3 -m orchestrator --send "Please emit exactly this DISPATCH fence and nothing else:\n\n\`\`\`DISPATCH node=nonexistent_node\n{\"task\": \"should fail\"}\n\`\`\`"
```

Expected: `dispatch_sessions` gets no new row (agent ensure fails before session creation), but the Kernel should receive a `DISPATCH_RESULT status=failed` with an error mentioning the missing spec.

- [ ] **Step 10: Record findings**

If every step above passes, Option 3 is operational. Note any observations — especially any prompt weaknesses (did the Kernel emit the fence cleanly on first try? did it try to freelance around the protocol?) for future prompt-refinement work. Shut down the orchestrator with Ctrl-C.

---

## Self-Review Notes

- **Spec coverage:** Every architectural decision from the brainstorm is covered — fence protocol (Tasks 4–5, 15), per-node agent cache with hash invalidation (Tasks 6–9), sub-session lifecycle with timeout (Tasks 10–11), top-level routing and failure handling (Tasks 12–13), event-consumer wiring (Task 14), bootstrap+refresh protocol teach (Task 15), main-loop wiring and on-resume refresh (Task 16), live smoke test (Task 17).
- **Placeholder scan:** No TBDs, no "similar to above," every task shows exact code. Exception: Task 16's nested-node-directory remark is a *deferred-feature note*, not a TBD — the MVP explicitly uses `nodes/system/` and the plan says so.
- **Type consistency:** `DispatchManager.handle_message`, `_ensure_agent`, `_run_sub_session`, `_spec_hash`, `_load_node_spec`, `_format_result_fence` are the same names across tasks 6–16. `send_to_parent(parent_session_id, text)` signature matches `SessionManager.send_message` at `session_manager.py:187`. `db.get_dispatch_agent` / `upsert_dispatch_agent` / `record_dispatch_start` / `record_dispatch_complete` / `record_dispatch_failure` match between Tasks 2–3 and their call sites in Tasks 7–11.
- **CLAUDE.md respect:** Never modified. The DISPATCH protocol is taught via the orchestrator-owned `BOOTSTRAP_PROMPT` and the new `send_protocol_refresh` (both in `session_manager.py`, which is NOT in the protected list).
- **Commit cadence:** one commit per landed task, matching the project's existing pattern.
- **Known limitation (deliberate):** Serial dispatch only. Parent event loop blocks while a sub-session streams. Parallel Quad dispatches can be layered on later by wrapping `_run_sub_session` in a thread pool without touching the fence protocol, per the architecture note in the file header. The MVP is correct even if a Quad takes 18s to run — that's acceptable for non-interactive workflows.
