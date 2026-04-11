"""WebSocket bridge between the orchestrator and the dashboard.

Architecture
------------
The bridge runs ``websockets.serve`` on a private asyncio event loop
inside a dedicated daemon thread. The orchestrator's main event loop
(SSE consumer, dispatch manager, scheduler) runs in a different thread
and broadcasts events by calling ``bridge.broadcast(envelope)``, which
schedules a send coroutine on the bridge's loop via
``asyncio.run_coroutine_threadsafe``.

This keeps the SSE loop blocking and synchronous (unchanged) while
exposing an async network surface to dashboard clients. No shared
mutable state between threads — the client set is only touched from
the bridge loop.

Callbacks
---------
``on_user_message(payload)``, ``on_abort()``, and ``on_hitl_response(payload)``
are sync callbacks the orchestrator registers. They run on the bridge
loop; callbacks that need to affect the SSE loop should do their own
thread marshalling (typically via ``SessionManager.send_message``,
which is already thread-safe because it makes an HTTP call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Dict, Optional, Set

import websockets
import websockets.asyncio.server as _ws_server
from websockets.asyncio.server import ServerConnection

from orchestrator.ws_events import parse_inbound_event

logger = logging.getLogger(__name__)


class WebSocketBridge:
    """Thread-safe WebSocket broadcaster for dashboard clients.

    Parameters
    ----------
    host : str
        Interface to bind on. Default ``127.0.0.1``.
    port : int
        TCP port. Pass ``0`` to let the OS pick one (tests use this).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8002):
        self.host = host
        self._requested_port = port
        self.port: Optional[int] = None  # Populated once bound

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[_ws_server.Server] = None
        self._clients: Set[ServerConnection] = set()
        self._shutdown = threading.Event()

        # Inbound callbacks — assigned by the orchestrator at wire-up time
        self.on_user_message: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_abort: Optional[Callable[[], None]] = None
        self.on_hitl_response: Optional[Callable[[Dict[str, Any]], None]] = None

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the bridge in a background daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return  # already running
        self._thread = threading.Thread(
            target=self._thread_main, name="ws-bridge", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the bridge gracefully."""
        if self._loop is None or self._thread is None:
            return
        # Signal the poll loop in _serve_forever to exit. _serve_forever
        # itself awaits _shutdown_async() after the poll loop breaks, so
        # by the time the thread exits, all clients are cleanly closed.
        self._shutdown.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning(
                "ws_bridge: background thread did not exit within 2s — leaking it"
            )
        self._thread = None
        self._loop = None
        self._server = None
        self.port = None

    async def _shutdown_async(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        # Close any stragglers
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()

    def _thread_main(self) -> None:
        """Entry point for the bridge's background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve_forever())
        except Exception:
            logger.exception("ws_bridge: background loop crashed")
        finally:
            self._loop.close()

    async def _serve_forever(self) -> None:
        async with websockets.serve(self._handler, self.host, self._requested_port) as server:
            self._server = server
            # websockets.serve returns a Server whose sockets attribute
            # tells us the bound port
            self.port = list(server.sockets)[0].getsockname()[1]
            logger.info("ws_bridge: listening on ws://%s:%d", self.host, self.port)
            # Sleep until shutdown is requested
            while not self._shutdown.is_set():
                await asyncio.sleep(0.1)
            # Drain shutdown work (close server + all clients) BEFORE the
            # `async with` block exits and the loop closes, so client close
            # handshakes actually complete.
            await self._shutdown_async()

    # ── Client handler ─────────────────────────────────────────────

    async def _handler(self, ws) -> None:
        self._clients.add(ws)
        logger.info("ws_bridge: client connected (%d total)", len(self._clients))
        loop = asyncio.get_running_loop()
        try:
            async for raw in ws:
                event = parse_inbound_event(raw)
                if event is None:
                    continue
                et = event["event_type"]
                payload = event["payload"]
                try:
                    # Callbacks are sync and may do I/O (e.g. session.send_message
                    # makes an HTTP call). Offload to the default executor so
                    # the bridge loop stays responsive to other clients.
                    if et == "USER_MESSAGE" and self.on_user_message is not None:
                        await loop.run_in_executor(None, self.on_user_message, payload)
                    elif et == "ABORT" and self.on_abort is not None:
                        await loop.run_in_executor(None, self.on_abort)
                    elif et == "HITL_RESPONSE" and self.on_hitl_response is not None:
                        await loop.run_in_executor(None, self.on_hitl_response, payload)
                except Exception:
                    logger.exception("ws_bridge: callback for %s failed", et)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("ws_bridge: client disconnected (%d total)", len(self._clients))

    # ── Broadcast API (thread-safe) ────────────────────────────────

    def broadcast(self, envelope: Dict[str, Any]) -> None:
        """Send an envelope to every connected client.

        Safe to call from any thread — marshalls onto the bridge loop.
        No-op if the bridge is not running or no clients are connected.
        """
        # Snapshot the loop reference once. Between the guard and the
        # run_coroutine_threadsafe call, another thread could nil self._loop
        # via stop() — we'd hit AttributeError. The snapshot eliminates that
        # TOCTOU window regardless of which thread is calling.
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast_async(envelope), loop
        )

    async def _broadcast_async(self, envelope: Dict[str, Any]) -> None:
        if not self._clients:
            return
        message = json.dumps(envelope, default=str)
        dead: Set[ServerConnection] = set()
        for ws in self._clients:
            try:
                await ws.send(message)
            except websockets.ConnectionClosed:
                dead.add(ws)
            except Exception:
                logger.exception("ws_bridge: send failed, marking client dead")
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)
