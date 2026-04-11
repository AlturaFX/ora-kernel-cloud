"""PostgreSQL connection helpers for ORA Kernel Cloud orchestrator."""
import json
import time
from contextlib import contextmanager
from typing import Any, Optional

import psycopg2
import psycopg2.extras


class Database:
    """Simple PostgreSQL wrapper for the orchestrator."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None

    def connect(self):
        """Establish database connection."""
        self._conn = psycopg2.connect(self.dsn)
        self._conn.autocommit = True

    def close(self):
        """Close database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()

    @contextmanager
    def cursor(self):
        """Get a cursor with automatic cleanup."""
        if self._conn is None or self._conn.closed:
            self.connect()
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()

    def log_activity(self, session_id: str, agent_id: Optional[str], level: str,
                     event_source: str, action: str, node_name: Optional[str] = None,
                     details: Optional[dict] = None, rationale: Optional[str] = None,
                     task_id: Optional[str] = None):
        """Write to orch_activity_log."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO orch_activity_log
                    (task_id, session_id, agent_id, level, event_source, action, node_name, details, rationale)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (task_id, session_id, agent_id, level, event_source, action,
                  node_name, json.dumps(details or {}), rationale))

    def log_token_usage(self, session_id: str, agent_id: Optional[str], model: str,
                        input_tokens: int, output_tokens: int,
                        cache_read: int = 0, cache_creation: int = 0):
        """Write to otel_token_usage."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO otel_token_usage
                    (session_id, agent_id, model, input_tokens, output_tokens, cache_read, cache_creation)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (session_id, agent_id, model, input_tokens, output_tokens, cache_read, cache_creation))

    def log_cost(self, session_id: str, agent_id: Optional[str], model: str,
                 cost_usd: float, duration_ms: Optional[int] = None):
        """Write to otel_cost_tracking."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO otel_cost_tracking
                    (session_id, agent_id, model, cost_usd, duration_ms)
                VALUES (%s, %s, %s, %s, %s)
            """, (session_id, agent_id, model, cost_usd, duration_ms))

    def upsert_cloud_session(self, agent_id: str, environment_id: str,
                             session_id: str, status: str):
        """Track Managed Agent session in cloud_sessions table."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO cloud_sessions (agent_id, environment_id, session_id, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id)
                DO UPDATE SET status = %s, last_event_at = NOW()
            """, (agent_id, environment_id, session_id, status, status))

    def sync_file(self, file_path: str, content: str, synced_from: str = "container"):
        """Sync a file to kernel_files_sync for persistence across containers."""
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO kernel_files_sync (file_path, content, synced_from)
                VALUES (%s, %s, %s)
                ON CONFLICT (file_path)
                DO UPDATE SET content = %s, synced_from = %s, updated_at = NOW()
            """, (file_path, content, synced_from, content, synced_from))

    def get_synced_file(self, file_path: str) -> Optional[str]:
        """Retrieve a synced file's content."""
        with self.cursor() as cur:
            cur.execute("SELECT content FROM kernel_files_sync WHERE file_path = %s", (file_path,))
            row = cur.fetchone()
            return row["content"] if row else None

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
