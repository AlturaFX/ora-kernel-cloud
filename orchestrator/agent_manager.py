"""Manage Anthropic Managed Agent and Environment lifecycle for ORA Kernel Cloud."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from anthropic import Anthropic

from orchestrator.config import load_config, get_api_key

logger = logging.getLogger(__name__)

STATE_FILE = Path(".ora-kernel-cloud.json")
KERNEL_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "kernel-files" / "CLAUDE.md"

DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_AGENT_NAME = "ORA Kernel"
DEFAULT_ENV_NAME = "ora-kernel-env"
AGENT_TOOLS = [{"type": "agent_toolset_20260401"}]


def _load_state() -> dict:
    """Load persisted agent/environment IDs from the state file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt state file %s — starting fresh", STATE_FILE)
    return {}


def _save_state(state: dict) -> None:
    """Persist agent/environment IDs to disk."""
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    logger.info("State saved to %s", STATE_FILE)


def _read_system_prompt() -> str:
    """Read the Kernel system prompt from kernel-files/CLAUDE.md."""
    if not KERNEL_SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Kernel system prompt not found at {KERNEL_SYSTEM_PROMPT_PATH}. "
            "Ensure the ora-kernel repo is cloned alongside ora-kernel-cloud."
        )
    return KERNEL_SYSTEM_PROMPT_PATH.read_text()


def _find_existing(items: Any, name: str) -> Optional[str]:
    """Search a paginated list response for an item matching `name`, return its ID or None."""
    for item in items:
        if getattr(item, "name", None) == name:
            return item.id
    return None


def ensure_agent(client: Anthropic, config: dict) -> str:
    """Create or retrieve the ORA Kernel managed agent. Returns the agent ID."""
    agent_cfg = config.get("agent", {})
    agent_name = agent_cfg.get("name", DEFAULT_AGENT_NAME)
    model = agent_cfg.get("model", DEFAULT_MODEL)

    # Check saved state first
    state = _load_state()
    if state.get("agent_id"):
        logger.info("Using cached agent ID: %s", state["agent_id"])
        return state["agent_id"]

    # Search existing agents
    existing_agents = client.beta.agents.list()
    existing_id = _find_existing(existing_agents, agent_name)
    if existing_id:
        logger.info("Found existing agent %r: %s", agent_name, existing_id)
        state["agent_id"] = existing_id
        _save_state(state)
        return existing_id

    # Create new agent
    system_prompt = _read_system_prompt()
    agent = client.beta.agents.create(
        name=agent_name,
        model=model,
        system=system_prompt,
        tools=AGENT_TOOLS,
    )
    logger.info("Created agent %r: %s", agent_name, agent.id)
    state["agent_id"] = agent.id
    _save_state(state)
    return agent.id


def ensure_environment(client: Anthropic, config: dict) -> str:
    """Create or retrieve the ORA Kernel managed environment. Returns the environment ID."""
    env_cfg = config.get("environment", {})
    env_name = env_cfg.get("name", DEFAULT_ENV_NAME)

    # Check saved state first
    state = _load_state()
    if state.get("environment_id"):
        logger.info("Using cached environment ID: %s", state["environment_id"])
        return state["environment_id"]

    # Search existing environments
    existing_envs = client.beta.environments.list()
    existing_id = _find_existing(existing_envs, env_name)
    if existing_id:
        logger.info("Found existing environment %r: %s", env_name, existing_id)
        state["environment_id"] = existing_id
        _save_state(state)
        return existing_id

    # Build environment config from YAML — type: "cloud" is required by the API
    env_config: Dict[str, Any] = {"type": "cloud"}
    if env_cfg.get("packages"):
        env_config["packages"] = env_cfg["packages"]
    if env_cfg.get("networking"):
        env_config["networking"] = env_cfg["networking"]
    else:
        env_config["networking"] = {"type": "unrestricted"}

    env = client.beta.environments.create(
        name=env_name,
        config=env_config,
    )
    logger.info("Created environment %r: %s", env_name, env.id)
    state["environment_id"] = env.id
    _save_state(state)
    return env.id


def setup(config: Optional[dict] = None) -> Dict[str, str]:
    """Ensure both the managed agent and environment exist. Returns their IDs."""
    if config is None:
        config = load_config()

    client = Anthropic(api_key=get_api_key(config))

    agent_id = ensure_agent(client, config)
    environment_id = ensure_environment(client, config)

    return {"agent_id": agent_id, "environment_id": environment_id}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ids = setup()
    print(f"Agent ID:       {ids['agent_id']}")
    print(f"Environment ID: {ids['environment_id']}")
