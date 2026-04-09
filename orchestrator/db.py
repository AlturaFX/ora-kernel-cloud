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
