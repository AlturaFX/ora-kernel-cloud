#!/usr/bin/env bash
# SessionStart hook: Clears temporary state for a fresh session.
# Preserves cross-session data (postgres, node specs) but clears
# runtime state (loop detector logs, status files, counters).
#
# Note: This hook uses directory removal which is outside the safety_check.sh
# scope (SessionStart hooks run before tool-use hooks). The path is hardcoded
# and validated to prevent accidental deletion of non-temp data.

TEMP_ROOT="/tmp/claude-kernel"

# Safety: only clean if the path is exactly what we expect
if [ -d "$TEMP_ROOT" ] && [[ "$TEMP_ROOT" == /tmp/claude-kernel ]]; then
    # Remove session directories older than 60 minutes
    # Uses -mindepth 1 -maxdepth 1 to only target top-level session dirs
    for dir in "$TEMP_ROOT"/*/; do
        if [ -d "$dir" ]; then
            # Check age: only remove if modified >60 min ago
            if find "$dir" -maxdepth 0 -mmin +60 -print -quit | grep -q .; then
                # Remove contents individually instead of rm -rf for safer deletion
                find "$dir" -type f -delete 2>/dev/null
                find "$dir" -type d -mindepth 1 -delete 2>/dev/null
                rmdir "$dir" 2>/dev/null
            fi
        fi
    done
fi

exit 0
