"""Session lifecycle management for ORA Kernel Cloud.

Handles creating sessions, sending the bootstrap event, monitoring health,
and restarting on termination.
"""
import json
import time
import logging
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

from orchestrator.config import load_config, get_api_key
from orchestrator.db import Database

logger = logging.getLogger(__name__)

BOOTSTRAP_PROMPT = """Bootstrap: Set up the ORA Kernel workspace.

1. Clone the ORA Kernel repo: git clone {repo_url} /work/ora-kernel
2. Run the installer: python3 /work/ora-kernel/install.py /work --force
3. Read CLAUDE.md to confirm your operating instructions are loaded
4. Read .claude/kernel/journal/WISDOM.md for operational context
5. Check postgres connectivity: psql "{postgres_dsn}" -c "SELECT COUNT(*) FROM orch_config"
6. Report ready status with a summary of:
   - Number of node specs available
   - WISDOM.md entries loaded
   - Postgres connection status

{hydration_instructions}
"""

HYDRATION_TEMPLATE = """
Additionally, restore these files from the previous session:

{files}

Write each file to the specified path.
"""


class SessionManager:
    """Manages the lifecycle of a Managed Agent session."""

    def __init__(self, config: dict, db: Database):
        self.config = config
        self.db = db
        self.client = Anthropic(api_key=get_api_key(config))
        self.agent_id: Optional[str] = None
        self.environment_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self._state_file = Path(".ora-kernel-cloud.json")
        self._load_state()

    def _load_state(self):
        """Load persisted agent/environment/session IDs."""
        if self._state_file.exists():
            state = json.loads(self._state_file.read_text())
            self.agent_id = state.get("agent_id")
            self.environment_id = state.get("environment_id")
            self.session_id = state.get("session_id")

    def _save_state(self):
        """Persist IDs for reuse across runs."""
        state = {
            "agent_id": self.agent_id,
            "environment_id": self.environment_id,
            "session_id": self.session_id,
        }
        self._state_file.write_text(json.dumps(state, indent=2))

    def set_agent_and_environment(self, agent_id: str, environment_id: str):
        """Set agent and environment IDs (from agent_manager.setup())."""
        self.agent_id = agent_id
        self.environment_id = environment_id
        self._save_state()

    def create_session(self) -> str:
        """Create a new Managed Agent session."""
        if not self.agent_id or not self.environment_id:
            raise ValueError("Agent and environment must be set before creating a session")

        session = self.client.beta.sessions.create(
            agent=self.agent_id,
            environment_id=self.environment_id,
            title="ORA Kernel — persistent session",
        )
        self.session_id = session.id
        self._save_state()

        # Track in postgres
        self.db.upsert_cloud_session(
            self.agent_id, self.environment_id, self.session_id, "created"
        )

        logger.info(f"Session created: {self.session_id}")
        return self.session_id

    def bootstrap(self):
        """Send the bootstrap event to set up the workspace."""
        if not self.session_id:
            raise ValueError("Session must be created before bootstrapping")

        # Build hydration instructions from synced files
        hydration = self._build_hydration_instructions()

        repo_url = self.config.get("session", {}).get(
            "bootstrap_repo", "https://github.com/AlturaFX/ora-kernel.git"
        )
        postgres_dsn = self.config.get("postgres", {}).get(
            "dsn", "postgresql://u24@localhost:5432/ora_kernel"
        )

        prompt = BOOTSTRAP_PROMPT.format(
            repo_url=repo_url,
            postgres_dsn=postgres_dsn,
            hydration_instructions=hydration,
        )

        self.client.beta.sessions.events.send(
            self.session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": prompt}],
            }],
        )

        logger.info("Bootstrap event sent")

    def _build_hydration_instructions(self) -> str:
        """Build file hydration instructions from postgres sync table."""
        files_to_restore = []

        # Get WISDOM.md
        wisdom = self.db.get_synced_file(".claude/kernel/journal/WISDOM.md")
        if wisdom:
            files_to_restore.append(
                f"File: .claude/kernel/journal/WISDOM.md\nContent:\n```\n{wisdom}\n```"
            )

        # Get recent journal entries
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT file_path, content FROM kernel_files_sync
                WHERE file_path LIKE '.claude/kernel/journal/____-__-__.md'
                ORDER BY updated_at DESC LIMIT 2
            """)
            for row in cur.fetchall():
                files_to_restore.append(
                    f"File: {row['file_path']}\nContent:\n```\n{row['content']}\n```"
                )

        if not files_to_restore:
            return ""

        return HYDRATION_TEMPLATE.format(files="\n\n".join(files_to_restore))

    def send_message(self, content: str):
        """Send a user message to the active session."""
        if not self.session_id:
            raise ValueError("No active session")

        self.client.beta.sessions.events.send(
            self.session_id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": content}],
            }],
        )

    def send_tool_confirmation(self, tool_use_id: str, approved: bool, reason: str = ""):
        """Send HITL approval/denial for a tool call."""
        if not self.session_id:
            raise ValueError("No active session")

        self.client.beta.sessions.events.send(
            self.session_id,
            events=[{
                "type": "user.tool_confirmation",
                "tool_use_id": tool_use_id,
                "decision": "approve" if approved else "deny",
                **({"reason": reason} if reason else {}),
            }],
        )

    def interrupt(self):
        """Interrupt the agent mid-execution."""
        if not self.session_id:
            return

        self.client.beta.sessions.events.send(
            self.session_id,
            events=[{"type": "user.interrupt"}],
        )
        logger.warning("Session interrupted")

    def get_status(self) -> Optional[dict]:
        """Retrieve current session status."""
        if not self.session_id:
            return None

        try:
            session = self.client.beta.sessions.retrieve(self.session_id)
            return {
                "id": session.id,
                "status": session.status,
            }
        except Exception as e:
            logger.error(f"Failed to retrieve session status: {e}")
            return None

    def restart_if_needed(self) -> bool:
        """Check session health and restart if terminated. Returns True if restarted."""
        status = self.get_status()
        if status and status.get("status") == "terminated":
            max_attempts = self.config.get("session", {}).get("max_restart_attempts", 3)
            logger.warning(f"Session terminated. Restarting (max {max_attempts} attempts)...")

            for attempt in range(1, max_attempts + 1):
                try:
                    self.create_session()
                    self.bootstrap()
                    logger.info(f"Session restarted successfully (attempt {attempt})")
                    return True
                except Exception as e:
                    logger.error(f"Restart attempt {attempt} failed: {e}")
                    if attempt < max_attempts:
                        time.sleep(5 * attempt)  # Exponential backoff

            logger.critical("All restart attempts failed. Manual intervention needed.")
            return False

        return False
