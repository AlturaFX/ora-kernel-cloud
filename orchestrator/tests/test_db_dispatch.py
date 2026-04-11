"""Tests for dispatch-related Database helpers.

These tests hit a real postgres — they create temporary rows in
dispatch_agents and dispatch_sessions and clean up after themselves.
If POSTGRES_DSN is not reachable the tests are skipped.
"""
from __future__ import annotations

import uuid

import pytest

from orchestrator.config import get_postgres_dsn, load_config
from orchestrator.db import Database


def _dsn() -> str:
    # Use the same resolution path the orchestrator uses so we pick up
    # .env overrides (POSTGRES_DSN), config.yaml, and finally the
    # hardcoded fallback. Without this, the fixture would use a naive
    # TCP DSN and fail in environments that authenticate via Unix sockets.
    return get_postgres_dsn(load_config())


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
