"""Entry point for ORA Kernel Cloud orchestrator.

Usage:
    python -m orchestrator              # Start the full orchestrator
    python -m orchestrator --setup      # Create agent + environment only
    python -m orchestrator --send MSG   # Send a message to the active session
"""
import argparse
import logging
import signal
import sys
from pathlib import Path

from anthropic import Anthropic

from orchestrator.config import load_config, get_api_key, get_postgres_dsn
from orchestrator.db import Database
from orchestrator.agent_manager import setup as agent_setup
from orchestrator.session_manager import SessionManager
from orchestrator.event_consumer import EventConsumer
from orchestrator.dispatch import DispatchManager
from orchestrator.file_sync import FileSync
from orchestrator.hitl import StdinHitlHandler
from orchestrator.scheduler import KernelScheduler
from orchestrator.http_api import PanelApiServer
from orchestrator.ws_bridge import WebSocketBridge
from orchestrator.ws_hitl import WebSocketHitlHandler
from orchestrator import ws_events  # noqa: F401 — used by snapshot provider (W9)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("orchestrator")


def main():
    parser = argparse.ArgumentParser(description="ORA Kernel Cloud Orchestrator")
    parser.add_argument("--setup", action="store_true", help="Create agent + environment only")
    parser.add_argument("--send", type=str, help="Send a message to the active session")
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)
    api_key = get_api_key(config)
    postgres_dsn = get_postgres_dsn(config)

    # Connect to database
    db = Database(postgres_dsn)
    db.connect()
    logger.info(f"Connected to PostgreSQL")

    # Setup agent + environment
    result = agent_setup(config)
    agent_id = result["agent_id"]
    env_id = result["environment_id"]
    logger.info(f"Agent: {agent_id}")
    logger.info(f"Environment: {env_id}")

    if args.setup:
        print(f"Agent ID: {agent_id}")
        print(f"Environment ID: {env_id}")
        return

    # Session manager
    session_mgr = SessionManager(config, db)
    session_mgr.set_agent_and_environment(agent_id, env_id)

    # Send a one-off message
    if args.send:
        if not session_mgr.session_id:
            print("No active session. Run without --send first to create one.")
            sys.exit(1)
        session_mgr.send_message(args.send)
        print(f"Sent: {args.send}")
        return

    # Create session if needed
    if not session_mgr.session_id:
        session_mgr.create_session()
        session_mgr.bootstrap()
    else:
        # Check if existing session is still alive
        status = session_mgr.get_status()
        if not status or status.get("status") == "terminated":
            logger.info("Previous session terminated. Creating new one.")
            session_mgr.create_session()
            session_mgr.bootstrap()
        else:
            logger.info(f"Resuming existing session: {session_mgr.session_id}")
            # Re-teach the SYNC + DISPATCH protocols to the resumed
            # session — its bootstrap may pre-date them.
            session_mgr.send_protocol_refresh()

    # File sync (change-data-capture + snapshot reconciliation)
    file_sync = FileSync(db)

    # Dashboard bridge (WebSocket) and HTTP API — optional, controlled by config
    dashboard_cfg = config.get("dashboard", {}) or {}
    dashboard_enabled = dashboard_cfg.get("enabled", True)
    ws_bridge = None
    panel_api = None
    if dashboard_enabled:
        ws_bridge = WebSocketBridge(
            host="127.0.0.1",
            port=dashboard_cfg.get("websocket_port", 8002),
        )
        try:
            ws_bridge.start()
            logger.info("Dashboard WS bridge: ws://127.0.0.1:%d", ws_bridge.port)
        except Exception:
            logger.exception("ws_bridge failed to start — falling back to stdin HITL")
            ws_bridge = None

        if ws_bridge is not None:
            panel_api = PanelApiServer(
                db=db,
                host="127.0.0.1",
                port=dashboard_cfg.get("http_api_port", 8003),
            )
            try:
                panel_api.start()
                logger.info("Dashboard HTTP API: http://127.0.0.1:%d", panel_api.port)
            except Exception:
                logger.exception("panel_api failed to start")
                panel_api = None

    # HITL handler — WebSocket if bridge is live, stdin as fallback
    if ws_bridge is not None:
        hitl = WebSocketHitlHandler(
            ws_bridge=ws_bridge,
            send_response=session_mgr.send_tool_confirmation,
        )
        logger.info("HITL: using WebSocket handler")
    else:
        hitl = StdinHitlHandler(
            send_response=session_mgr.send_tool_confirmation
        )
        logger.info("HITL: using stdin handler")

    # Wire bridge inbound callbacks to orchestrator actions
    if ws_bridge is not None:
        ws_bridge.on_user_message = lambda payload: session_mgr.send_message(
            payload.get("text", "")
        )
        ws_bridge.on_abort = lambda: session_mgr.interrupt()

    # Dispatch manager — translates DISPATCH fences into sub-sessions
    node_spec_dir = (
        Path(__file__).resolve().parent.parent
        / "kernel-files"
        / ".claude"
        / "kernel"
        / "nodes"
        / "system"
    )
    dispatch_manager = DispatchManager(
        db=db,
        client=Anthropic(api_key=api_key),
        environment_id=env_id,
        send_to_parent=lambda _sid, text: session_mgr.send_message(text),
        node_spec_dir=node_spec_dir,
        ws_bridge=ws_bridge,
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
        ws_bridge=ws_bridge,
    )

    # Scheduler
    scheduler = KernelScheduler(api_key, session_mgr.session_id, config)

    # Graceful shutdown
    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("Shutting down...")
        running = False
        scheduler.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start scheduler
    scheduler.start()
    logger.info("Scheduler started")

    # Main loop: consume events, restart on termination
    logger.info(f"Streaming events from session {session_mgr.session_id}")
    print("\n" + "=" * 50)
    print("ORA Kernel Cloud — Running")
    print(f"Session: {session_mgr.session_id}")
    print("Press Ctrl+C to stop")
    print("=" * 50 + "\n")

    while running:
        try:
            # consume() blocks until session goes idle or terminates
            # Returns False if session terminated
            should_continue = consumer.consume(session_mgr.session_id)

            if not should_continue and running:
                # Session terminated — try to restart
                logger.warning("Session terminated. Attempting restart...")
                if session_mgr.restart_if_needed():
                    # Update scheduler with new session ID
                    scheduler.stop()
                    scheduler = KernelScheduler(api_key, session_mgr.session_id, config)
                    scheduler.start()
                    consumer = EventConsumer(
                        db=db,
                        api_key=api_key,
                        agent_id=agent_id,
                        environment_id=env_id,
                        on_hitl_needed=hitl.handle,
                        file_sync=file_sync,
                        dispatch_manager=dispatch_manager,
                        ws_bridge=ws_bridge,
                    )
                else:
                    logger.critical("Could not restart session. Exiting.")
                    running = False

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Event consumer error: {e}")
            if running:
                import time
                time.sleep(5)

    # Cleanup
    scheduler.stop()
    if panel_api is not None:
        panel_api.stop()
    if ws_bridge is not None:
        ws_bridge.stop()
    db.close()
    logger.info("Orchestrator stopped.")


if __name__ == "__main__":
    main()
