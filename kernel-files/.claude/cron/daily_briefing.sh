#!/usr/bin/env bash
# Daily briefing cron script — triggers the Kernel to generate a morning summary.
#
# Install with crontab:
#   # Every weekday at 8am
#   0 8 * * 1-5 /path/to/project/.claude/cron/daily_briefing.sh /path/to/project
#
#   # Every day at 9am
#   0 9 * * * /path/to/project/.claude/cron/daily_briefing.sh /path/to/project
#
# Usage: daily_briefing.sh <project_root>

PROJECT_ROOT="${1:-.}"
INBOX="$PROJECT_ROOT/.claude/events/inbox.jsonl"

if [ ! -f "$INBOX" ]; then
    echo "ERROR: Inbox not found at $INBOX" >&2
    echo "Usage: daily_briefing.sh /path/to/project" >&2
    exit 1
fi

TIMESTAMP=$(date -Iseconds)
ID="briefing_$(date +%Y%m%d)"

echo "{\"id\":\"$ID\",\"timestamp\":\"$TIMESTAMP\",\"role\":\"system\",\"content\":\"/briefing\"}" >> "$INBOX"
