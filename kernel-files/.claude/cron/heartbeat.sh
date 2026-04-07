#!/usr/bin/env bash
# Heartbeat cron script — writes a /heartbeat trigger to the event inbox.
# The Kernel (via kernel-listen) picks this up and checks for anomalies.
#
# Install with crontab:
#   # Every 2 hours during work hours (8am-6pm, weekdays)
#   0 8-18/2 * * 1-5 /path/to/project/.claude/cron/heartbeat.sh /path/to/project
#
#   # Or simply every 2 hours:
#   0 */2 * * * /path/to/project/.claude/cron/heartbeat.sh /path/to/project
#
# Usage: heartbeat.sh <project_root>

PROJECT_ROOT="${1:-.}"
INBOX="$PROJECT_ROOT/.claude/events/inbox.jsonl"

if [ ! -f "$INBOX" ]; then
    echo "ERROR: Inbox not found at $INBOX" >&2
    echo "Usage: heartbeat.sh /path/to/project" >&2
    exit 1
fi

TIMESTAMP=$(date -Iseconds)
ID="heartbeat_$(date +%s)"

echo "{\"id\":\"$ID\",\"timestamp\":\"$TIMESTAMP\",\"role\":\"system\",\"content\":\"/heartbeat\"}" >> "$INBOX"
