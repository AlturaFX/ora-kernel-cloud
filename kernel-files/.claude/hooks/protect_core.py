#!/usr/bin/env python3
"""
PreToolUse hook: Blocks Edit/Write operations on protected files.
Protected files require HITL approval to modify (Axiom 4: Immutable Core).

Exit 0 = allow, Exit 2 = block (sends stderr to model).
"""
import json
import sys
import os
from typing import Optional

# Protected path patterns — these cannot be modified by any agent.
# Node prompts in .claude/kernel/nodes/ are intentionally NOT protected
# so the self-improvement cycle can refine them.
PROTECTED_PATHS = [
    "CLAUDE.md",
    "PROJECT_DNA.md",
    ".claude/kernel/references/constitution.md",
    ".claude/kernel/schemas/node_output.md",
    ".claude/kernel/schemas/node_spec.md",
    ".claude/kernel/schemas/split_spec.md",
    ".claude/hooks/",
    ".claude/settings.json",
    "infrastructure/db/",
]


def is_protected(file_path: str) -> Optional[str]:
    """Check if a file path matches any protected pattern. Returns the matching pattern or None."""
    # Normalize to relative path from project root
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir and file_path.startswith(project_dir):
        file_path = file_path[len(project_dir):].lstrip("/")

    for pattern in PROTECTED_PATHS:
        # Directory patterns (ending with /) match anything inside
        if pattern.endswith("/") and file_path.startswith(pattern):
            return pattern
        # Exact file matches
        if file_path == pattern or file_path.endswith("/" + pattern):
            return pattern

    return None


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    match = is_protected(file_path)
    if match:
        msg = (
            f"BLOCKED: '{file_path}' is a protected file (matches pattern: {match}). "
            f"Protected files require human approval to modify (Axiom 4: Immutable Core). "
            f"To propose changes, present the diff to the user for manual application."
        )
        print(msg, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
