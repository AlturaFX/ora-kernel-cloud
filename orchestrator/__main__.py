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

from orchestrator.config import load_config, get_api_key, get_postgres_dsn
from orchestrator.db import Database
from orchestrator.agent_manager import AgentManager
from orchestrator.session_manager import SessionManager
from orchestrator.event_consumer import EventConsumer
from orchestrator.scheduler import KernelScheduler

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
    agent_mgr = AgentManager(config)
    agent_id, env_id = agent_mgr.setup()
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

    # Event consumer
    consumer = EventConsumer(api_key, db)

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
                    consumer = EventConsumer(api_key, db)
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
    db.close()
    logger.info("Orchestrator stopped.")


if __name__ == "__main__":
    main()
