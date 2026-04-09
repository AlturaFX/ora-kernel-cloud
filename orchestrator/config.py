"""Configuration loader for ORA Kernel Cloud orchestrator."""
import os
import yaml
from pathlib import Path
from typing import Any


def load_config(config_path: str = None) -> dict:
    """Load configuration from config.yaml, with .env and environment variable overrides."""
    # Try to load .env file
    try:
        from dotenv import load_dotenv
        env_file = Path(config_path or ".").parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass  # python-dotenv not installed, rely on environment variables

    # Load YAML config
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    # Environment variable overrides
    if os.environ.get("ANTHROPIC_API_KEY"):
        config.setdefault("api_key", os.environ["ANTHROPIC_API_KEY"])

    if os.environ.get("POSTGRES_DSN"):
        config.setdefault("postgres", {})["dsn"] = os.environ["POSTGRES_DSN"]

    return config


def get_api_key(config: dict) -> str:
    """Get Anthropic API key from config or environment."""
    key = config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY not found. Set it in .env file or as an environment variable.\n"
            "See docs/API_KEY_SETUP.md for instructions."
        )
    return key


def get_postgres_dsn(config: dict) -> str:
    """Get PostgreSQL connection string from config or environment."""
    return (
        os.environ.get("POSTGRES_DSN")
        or config.get("postgres", {}).get("dsn", "postgresql://u24@localhost:5432/ora_kernel")
    )
