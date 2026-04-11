"""Integration tests for PanelApiServer.

Spins up a real ThreadingHTTPServer on an ephemeral port and hits it
via urllib.request, asserting JSON shapes against a mocked Database.
The server and the DB mock are both cheap, so we rebuild per test.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from orchestrator.http_api import PanelApiServer


@pytest.fixture
def server():
    db = MagicMock()
    db.get_current_parent_session = MagicMock(return_value={
        "agent_id": "agent_x",
        "environment_id": "env_x",
        "session_id": "sesn_parent",
        "status": "running",
        "total_input_tokens": 100,
        "total_output_tokens": 50,
        "total_cost_usd": 0.25,
        "created_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        "last_event_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
    })
    db.get_recent_dispatches = MagicMock(return_value=[
        {
            "sub_session_id": "sesn_sub_1",
            "parent_session_id": "sesn_parent",
            "node_name": "business_analyst",
            "status": "complete",
            "input_tokens": 3000,
            "output_tokens": 500,
            "cost_usd": 0.0275,
            "duration_ms": 4800,
            "error": None,
            "started_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
            "completed_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        }
    ])
    db.get_file_sync_state = MagicMock(return_value=[
        {
            "file_path": ".claude/kernel/journal/WISDOM.md",
            "synced_from": "cdc",
            "content_length": 1234,
            "updated_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        }
    ])
    db.list_dispatch_agents = MagicMock(return_value=[
        {
            "node_name": "business_analyst",
            "agent_id": "agent_ba",
            "prompt_hash": "deadbeef",
            "created_at": datetime(2026, 4, 10, tzinfo=timezone.utc),
        }
    ])

    api = PanelApiServer(db=db, host="127.0.0.1", port=0)
    api.start()
    deadline = time.time() + 2.0
    while api.port is None and time.time() < deadline:
        time.sleep(0.01)
    assert api.port is not None
    yield api, db
    api.stop()


def _get_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/json"
        return json.loads(resp.read())


def test_health_endpoint(server):
    api, _ = server
    body = _get_json(api.port, "/api/cloud/health")
    assert body["status"] == "ok"


def test_session_endpoint(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/session")
    assert body["session_id"] == "sesn_parent"
    assert body["status"] == "running"
    assert body["total_cost_usd"] == 0.25
    db.get_current_parent_session.assert_called_once()


def test_dispatches_endpoint_default_limit(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/dispatches")
    assert isinstance(body, list)
    assert body[0]["node_name"] == "business_analyst"
    assert body[0]["cost_usd"] == 0.0275


def test_dispatches_endpoint_honors_limit_param(server):
    api, db = server
    _get_json(api.port, "/api/cloud/dispatches?limit=5")
    call = db.get_recent_dispatches.call_args
    assert call.kwargs.get("limit", call.args[0] if call.args else None) == 5


def test_files_endpoint(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/files")
    assert body[0]["file_path"] == ".claude/kernel/journal/WISDOM.md"
    assert body[0]["synced_from"] == "cdc"


def test_agents_endpoint(server):
    api, db = server
    body = _get_json(api.port, "/api/cloud/agents")
    assert body[0]["node_name"] == "business_analyst"


def test_unknown_path_returns_404(server):
    api, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(
            f"http://127.0.0.1:{api.port}/api/cloud/unknown", timeout=2
        )
    assert exc_info.value.code == 404
