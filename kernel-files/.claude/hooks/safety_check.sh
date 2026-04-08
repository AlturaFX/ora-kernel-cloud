#!/usr/bin/env bash
# PreToolUse hook: Safety enforcement for Bash commands.
#
# Blocks:
#   1. Commands containing rm or sudo (anywhere in the string)
#   2. Compound commands using && or ; (forces single-command-per-call)
#
# Why block compound commands:
#   - Permission patterns (e.g., Bash(git *)) only match the first command in a chain
#   - Safety checks can be bypassed by hiding dangerous commands after &&
#   - Loop detection works better with discrete, individual commands
#   - Use absolute paths instead of cd && ...; run separate Bash calls for sequences
#
# Exit 2 = block the tool call and send stderr message to the model.

# Read stdin JSON to get the full command
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)

if [ -z "$COMMAND" ]; then
    exit 0
fi

# Check for rm or sudo as whole words anywhere in the command
if echo "$COMMAND" | grep -qE '\brm\b|\bsudo\b'; then
    echo "BLOCKED: Command contains 'rm' or 'sudo'. These operations require human approval. Ask the user to perform this action manually." >&2
    exit 2
fi

# Check for compound command operators: && ; (but NOT || which is error handling)
# Also allow | (pipes) since those are single logical operations
if echo "$COMMAND" | grep -qE '&&|;\s'; then
    echo "BLOCKED: Compound commands (using && or ;) are not allowed. Run each command as a separate Bash call, or write a script file and execute it. This ensures permissions and safety checks apply to every command individually." >&2
    exit 2
fi

exit 0
