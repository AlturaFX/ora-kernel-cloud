"""Spike: validate orchestrator-side dispatch via a second Managed Agent session.

Purpose
-------
Before committing to Option 3 of the cloud dispatch architecture (the
orchestrator spins up a separate Managed Agent session for each
"subagent" dispatch), we need to ground the plan in real numbers:

1. Can we programmatically create an agent with a custom system prompt?
2. Can we create a session against the existing shared environment?
3. Can we send a user.message and consume events until idle?
4. End-to-end latency from send → first token → idle?
5. Token cost for a trivial roundtrip?
6. Any API friction we do not already know about?

This spike is intentionally NOT wired into the orchestrator. It is a
standalone script that can be run once and then ignored. Safe to delete
after findings are recorded; kept in version control as documentation
of how Option 3 was validated.

Usage
-----
    python3 spikes/subagent_feasibility.py

Reads the existing config/environment via orchestrator.config so it
reuses the shared Managed Agent environment and does not provision
another one (which would cost container hours).
"""
from __future__ import annotations

import time

from anthropic import Anthropic

from orchestrator.config import get_api_key, load_config
from orchestrator.agent_manager import _load_state

# A deliberately tiny "subagent" — one purpose, unambiguous output.
NUMBER_AGENT_SYSTEM = (
    "You are the NumberNode. When asked any question, respond with ONLY a "
    "single integer between 1 and 100. No prose, no explanation, no "
    "formatting — just the digits."
)


def _consume_until_idle(client: Anthropic, session_id: str) -> dict:
    """Stream events until the session goes idle and return a small summary."""
    summary: dict = {
        "input_tokens": 0,
        "output_tokens": 0,
        "first_token_at": None,
        "idle_at": None,
        "response_text": "",
        "event_types": [],
    }
    t_start = time.time()
    with client.beta.sessions.events.stream(session_id) as stream:
        for event in stream:
            event_type = getattr(event, "type", "")
            summary["event_types"].append(event_type)

            if event_type == "span.model_request_end":
                usage = getattr(event, "model_usage", None)
                summary["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                summary["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

            elif event_type == "agent.message":
                if summary["first_token_at"] is None:
                    summary["first_token_at"] = time.time() - t_start
                for block in getattr(event, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        summary["response_text"] += text

            elif event_type == "session.status_idle":
                summary["idle_at"] = time.time() - t_start
                return summary

            elif event_type == "session.status_terminated":
                summary["idle_at"] = time.time() - t_start
                summary["terminated"] = True
                return summary

    summary["idle_at"] = time.time() - t_start
    return summary


def main() -> None:
    config = load_config()
    api_key = get_api_key(config)
    client = Anthropic(api_key=api_key)

    state = _load_state()
    shared_env_id = state.get("environment_id")
    if not shared_env_id:
        raise SystemExit(
            "No cached environment_id in .ora-kernel-cloud.json — "
            "run the main orchestrator once first to provision it."
        )
    print(f"Reusing shared environment: {shared_env_id}\n")

    # --- 1. Create a fresh temp agent with its own system prompt ---
    print("Creating temp 'NumberNode' agent...", flush=True)
    t0 = time.time()
    agent = client.beta.agents.create(
        name=f"spike-NumberNode-{int(t0)}",
        model="claude-opus-4-6",
        system=NUMBER_AGENT_SYSTEM,
        tools=[{"type": "agent_toolset_20260401"}],
    )
    t_agent_create = time.time() - t0
    print(f"  agent_id={agent.id}  ({t_agent_create:.2f}s)\n")

    # --- 2. Create a session against the shared environment ---
    print("Creating session...", flush=True)
    t0 = time.time()
    session = client.beta.sessions.create(
        agent=agent.id,
        environment_id=shared_env_id,
        title="spike-subagent",
    )
    t_session_create = time.time() - t0
    print(f"  session_id={session.id}  ({t_session_create:.2f}s)\n")

    # --- 3. Send a trivial message ---
    print("Sending user.message: 'Give me a number.'", flush=True)
    t_send_start = time.time()
    client.beta.sessions.events.send(
        session.id,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": "Give me a number."}],
            }
        ],
    )

    # --- 4. Consume events until idle ---
    print("Consuming events...", flush=True)
    summary = _consume_until_idle(client, session.id)
    t_total = time.time() - t_send_start

    # --- 5. Report ---
    cost_usd = (
        summary["input_tokens"] * 5.0 + summary["output_tokens"] * 25.0
    ) / 1_000_000.0

    print("\n=== Spike Results ===")
    print(f"Response: {summary['response_text']!r}")
    print(f"Setup overhead:")
    print(f"  agent.create:   {t_agent_create:.2f}s")
    print(f"  session.create: {t_session_create:.2f}s")
    print(f"Roundtrip (send -> idle):  {t_total:.2f}s")
    if summary["first_token_at"] is not None:
        print(f"Time to first token:       {summary['first_token_at']:.2f}s")
    print(f"Tokens: input={summary['input_tokens']} output={summary['output_tokens']}")
    print(f"Estimated model cost: ${cost_usd:.6f}")
    print(f"Event types seen: {sorted(set(summary['event_types']))}")
    if summary.get("terminated"):
        print("!!! session terminated unexpectedly")

    print(f"\nLeaving temp agent {agent.id} in place — delete later if desired.")


if __name__ == "__main__":
    main()
