#!/usr/bin/env python3
"""
SubagentStart + SubagentStop hook: Tracks subagent lifecycle, logs metrics,
and triggers self-improvement after X completed tasks.

Handles both events — detects which from hook_event_name in stdin JSON.

SubagentStart: creates status file with start time and agent info.
SubagentStop: calculates duration, logs to activity_log, increments counter,
              triggers self-improvement when threshold reached.
"""
import json
import os
import sys
import time
from pathlib import Path

# Default threshold — can be overridden by orch_config table
DEFAULT_SELF_IMPROVEMENT_THRESHOLD = 10

# Paths
EVENT_INBOX = ".claude/events/inbox.jsonl"


def get_status_dir(session_id: str, agent_id: str) -> Path:
    d = Path(f"/tmp/claude-kernel/{session_id}/{agent_id}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_session_dir(session_id: str) -> Path:
    d = Path(f"/tmp/claude-kernel/{session_id}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def handle_start(hook_input: dict):
    """Create status file when a subagent starts."""
    session_id = hook_input.get("session_id", "unknown")
    agent_id = hook_input.get("agent_id", "unknown")
    agent_type = hook_input.get("agent_type", "")

    status_dir = get_status_dir(session_id, agent_id)
    status = {
        "phase": "started",
        "start_time": time.time(),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": session_id,
    }

    (status_dir / "status.json").write_text(json.dumps(status, indent=2))


def handle_stop(hook_input: dict):
    """Log metrics, increment counter, check self-improvement threshold."""
    session_id = hook_input.get("session_id", "unknown")
    agent_id = hook_input.get("agent_id", "unknown")
    agent_type = hook_input.get("agent_type", "")
    transcript_path = hook_input.get("agent_transcript_path", "")
    last_message = hook_input.get("last_assistant_message", "")

    status_dir = get_status_dir(session_id, agent_id)
    session_dir = get_session_dir(session_id)

    # Calculate duration
    duration_ms = 0
    status_file = status_dir / "status.json"
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text())
            start_time = status.get("start_time", 0)
            if start_time:
                duration_ms = int((time.time() - start_time) * 1000)
        except (json.JSONDecodeError, OSError):
            pass

    # Get transcript size as token proxy
    transcript_bytes = 0
    if transcript_path and os.path.exists(transcript_path):
        try:
            transcript_bytes = os.path.getsize(transcript_path)
        except OSError:
            pass

    # Determine outcome from last message (heuristic)
    outcome = "unknown"
    if last_message:
        lower = last_message.lower()
        if '"target_status": "complete"' in lower or '"target_status":"complete"' in lower:
            outcome = "COMPLETE"
        elif '"target_status": "unverified"' in lower or '"target_status":"unverified"' in lower:
            outcome = "UNVERIFIED"
        elif '"target_status": "failed"' in lower or '"target_status":"failed"' in lower:
            outcome = "FAILED"
        elif "error" in lower and ("failed" in lower or "cannot" in lower):
            outcome = "FAILED"
        else:
            outcome = "completed"

    # Update status file
    completed_status = {
        "phase": "completed",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "transcript_bytes": transcript_bytes,
        "outcome": outcome,
        "completed_at": time.time(),
    }
    status_file.write_text(json.dumps(completed_status, indent=2))

    # Append to activity log
    activity_entry = {
        "timestamp": time.time(),
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "duration_ms": duration_ms,
        "transcript_bytes": transcript_bytes,
        "outcome": outcome,
    }
    activity_log = session_dir / "activity_log.jsonl"
    with open(activity_log, "a") as f:
        f.write(json.dumps(activity_entry) + "\n")

    # Increment completed task counter
    counter_file = session_dir / "completed_count"
    count = 0
    if counter_file.exists():
        try:
            count = int(counter_file.read_text().strip())
        except (ValueError, OSError):
            count = 0
    count += 1
    counter_file.write_text(str(count))

    # Check self-improvement threshold
    threshold = DEFAULT_SELF_IMPROVEMENT_THRESHOLD
    # Try to read from config file if it exists
    config_file = session_dir / "config.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            threshold = config.get("self_improvement_threshold", threshold)
        except (json.JSONDecodeError, OSError):
            pass

    if count >= threshold:
        # Trigger self-improvement
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        inbox_path = os.path.join(project_dir, EVENT_INBOX) if project_dir else EVENT_INBOX

        trigger_msg = {
            "id": f"self_improve_{int(time.time())}",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "system",
            "content": "/self-improve",
            "metadata": {
                "task_count": count,
                "trigger": "automatic",
                "session_id": session_id,
            },
        }

        try:
            with open(inbox_path, "a") as f:
                f.write(json.dumps(trigger_msg) + "\n")
        except OSError:
            pass  # Non-fatal — self-improvement is optional

        # Reset counter
        counter_file.write_text("0")


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    event = hook_input.get("hook_event_name", "")

    if event == "SubagentStart":
        handle_start(hook_input)
    elif event == "SubagentStop":
        handle_stop(hook_input)

    sys.exit(0)


if __name__ == "__main__":
    main()
