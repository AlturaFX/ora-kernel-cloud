#!/usr/bin/env bash
# Memory consolidation cron script — triggers the "dreaming" cycle.
# Reviews recent journal entries and promotes insights to WISDOM.md.
#
# Install with crontab:
#   # Weekly on Sunday at 3am
#   0 3 * * 0 /path/to/project/.claude/cron/consolidate.sh /path/to/project
#
#   # Or after each self-improvement cycle (triggered by subagent_lifecycle.py)
#   # — no cron needed, /consolidate is written to inbox automatically
#
# Usage: consolidate.sh <project_root>

PROJECT_ROOT="${1:-.}"
INBOX="$PROJECT_ROOT/.claude/events/inbox.jsonl"
JOURNAL_DIR="$PROJECT_ROOT/.claude/kernel/journal"

if [ ! -f "$INBOX" ]; then
    echo "ERROR: Inbox not found at $INBOX" >&2
    echo "Usage: consolidate.sh /path/to/project" >&2
    exit 1
fi

# Verify journal directory exists
if [ ! -d "$JOURNAL_DIR" ]; then
    echo "WARNING: Journal directory not found at $JOURNAL_DIR" >&2
    exit 0
fi

# Only trigger if there are recent journal entries to consolidate
RECENT=$(find "$JOURNAL_DIR" -name "????-??-??.md" -mtime -7 | wc -l)
if [ "$RECENT" -eq 0 ]; then
    exit 0  # Nothing to consolidate
fi

TIMESTAMP=$(date -Iseconds)
ID="consolidate_$(date +%s)"

echo "{\"id\":\"$ID\",\"timestamp\":\"$TIMESTAMP\",\"role\":\"system\",\"content\":\"/consolidate\",\"metadata\":{\"journal_count\":$RECENT}}" >> "$INBOX"
