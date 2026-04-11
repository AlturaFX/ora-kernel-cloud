"""HTTP panel API for the ora-kernel-cloud dashboard.

Exposes five read-only JSON endpoints the dashboard polls for panel
data the WebSocket protocol doesn't carry naturally. Runs in a
background daemon thread using stdlib's ThreadingHTTPServer — no new
dependencies.

Endpoints
---------
GET /api/cloud/health        -> {"status": "ok", "port": N}
GET /api/cloud/session       -> current parent cloud_sessions row
GET /api/cloud/dispatches    -> recent dispatch_sessions rows (?limit=50)
GET /api/cloud/files         -> kernel_files_sync state (metadata, no body)
GET /api/cloud/agents        -> dispatch_agents cache
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """JSON encoder default for types psycopg2 returns (datetime, Decimal)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class _PanelRequestHandler(BaseHTTPRequestHandler):
    # The Database instance is attached to the server class below.
    server: "ThreadingHTTPServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("http_api: " + format, *args)

    def _send_json(self, body: Any, status: int = 200) -> None:
        payload = json.dumps(body, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_404(self) -> None:
        body = json.dumps({"error": "not found"}).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        db = getattr(self.server, "_db", None)
        if db is None:
            self._send_json({"error": "db not configured"}, status=500)
            return

        try:
            if path == "/api/cloud/health":
                self._send_json({"status": "ok", "port": self.server.server_port})
                return

            if path == "/api/cloud/session":
                row = db.get_current_parent_session()
                self._send_json(row or {})
                return

            if path == "/api/cloud/dispatches":
                limit = int(query.get("limit", ["50"])[0])
                parent_id = query.get("parent_session_id", [None])[0]
                rows = db.get_recent_dispatches(
                    limit=limit, parent_session_id=parent_id
                )
                self._send_json(rows or [])
                return

            if path == "/api/cloud/files":
                rows = db.get_file_sync_state()
                self._send_json(rows or [])
                return

            if path == "/api/cloud/agents":
                rows = db.list_dispatch_agents()
                self._send_json(rows or [])
                return

            self._send_404()
        except Exception:
            logger.exception("http_api: handler crashed for %s", path)
            self._send_json({"error": "internal"}, status=500)


class PanelApiServer:
    """Threaded HTTP server exposing panel data for the dashboard.

    Parameters
    ----------
    db : Database
        Orchestrator postgres wrapper. Used for read-only queries.
    host : str
        Bind interface. Default ``127.0.0.1``.
    port : int
        TCP port. Pass ``0`` for OS-assigned (tests use this).
    """

    def __init__(self, db, host: str = "127.0.0.1", port: int = 8003):
        self._db = db
        self._host = host
        self._requested_port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._server = ThreadingHTTPServer(
            (self._host, self._requested_port), _PanelRequestHandler
        )
        # Stash the db reference on the server instance so the handler
        # class can read it via self.server._db.
        self._server._db = self._db  # type: ignore[attr-defined]
        self.port = self._server.server_port
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="http-api",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "http_api: listening on http://%s:%d", self._host, self.port
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
        self.port = None
