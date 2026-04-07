---
description: Kernel listener — watches inbox for tasks, triggers, and messages
allowed-tools: Bash, Read, Write, Agent, Glob, Grep
---

# Kernel Listener

You are the Kernel's event loop. Your job is to watch for messages arriving in the inbox and handle them according to the Kernel Operating Instructions in CLAUDE.md.

## How This Works

1. Wait for .claude/events/inbox.jsonl to change (using inotifywait — BLOCKS until change)
2. Read the new message
3. Handle it according to message routing rules
4. Go back to step 1

**IMPORTANT**: Run inotifywait in FOREGROUND. Do NOT run it in the background or with -m flag.

## Step 1: Start Listening

Run this command directly (it will BLOCK until inbox.jsonl changes):

```bash
inotifywait -e modify -e create .claude/events/inbox.jsonl
```

## Step 2: Read the Message

After inotifywait exits, read the latest message:

```bash
tail -1 .claude/events/inbox.jsonl
```

The message is JSON:
```json
{"id": "msg_123", "timestamp": "...", "role": "user", "content": "..."}
```

## Step 3: Handle the Message

Parse the `content` field and route:

### `/self-improve`
Trigger the self-improvement cycle:
1. Read the activity log at `/tmp/claude-kernel/{session_id}/activity_log.jsonl`
2. Dispatch the tuning_analyst node to analyze metrics
3. Dispatch the refinement_analyst node to analyze patterns
4. Present all proposals to the user for HITL approval
5. Apply approved changes

### `/dispatch {json}`
Parse the task JSON and execute the full Kernel lifecycle:
1. Classify intent from the task description
2. Check `.claude/kernel/nodes/` for a matching node
3. Dispatch the appropriate subagent with the node's prompt
4. Handle verification cycle on completion
5. Log results

### `/heartbeat`
Proactive system health check. Stay SILENT if nothing is wrong (the HEARTBEAT_OK pattern).

Run these anomaly checks via the postgres MCP:

**1. Stuck tasks** — dispatched but not completed within expected time:
```sql
SELECT id, task_title, status, dispatched_at,
       EXTRACT(EPOCH FROM (NOW() - dispatched_at))/3600 AS hours_stuck
FROM orch_tasks
WHERE dispatched_at IS NOT NULL
  AND status IN ('INCOMPLETE', 'UNVERIFIED')
  AND dispatched_at < NOW() - INTERVAL '2 hours'
ORDER BY dispatched_at;
```

**2. Retry budget warnings** — tasks approaching their retry limit:
```sql
SELECT t.id, t.task_title, t.retry_count, t.max_retries,
       t.retry_count::float / t.max_retries AS exhaustion_pct
FROM orch_tasks t
WHERE t.status NOT IN ('COMPLETE', 'FAILED', 'CANCELLED')
  AND t.retry_count >= t.max_retries - 1
ORDER BY exhaustion_pct DESC;
```

**3. Node failure rate spike** — nodes failing more than 30% in recent tasks:
```sql
SELECT node_name, COUNT(*) AS total,
       COUNT(*) FILTER (WHERE action = 'FAIL') AS failures,
       ROUND(COUNT(*) FILTER (WHERE action = 'FAIL')::numeric / COUNT(*) * 100, 1) AS fail_pct
FROM orch_activity_log
WHERE created_at > NOW() - INTERVAL '24 hours'
  AND action IN ('COMPLETE', 'FAIL')
GROUP BY node_name
HAVING COUNT(*) >= 3
   AND COUNT(*) FILTER (WHERE action = 'FAIL')::numeric / COUNT(*) > 0.3;
```

**4. Orphaned subagents** — check status files for agents started but never completed:
```bash
find /tmp/claude-kernel/ -name status.json -exec grep -l '"phase": "started"' {} \;
```
For each, check if start_time is more than 30 minutes ago.

**Decision logic:**
- If ALL checks return empty results → do nothing (HEARTBEAT_OK). No output, no briefing entry.
- If ANY check returns results → write a summary to `.claude/events/pending_briefing.md` with:
  - Which checks triggered
  - Specific task IDs and names
  - Recommended actions
  - Severity (info / warning / critical)

### `/status`
Report current state:
1. List active subagents from status files
2. Count completed/failed/pending tasks from this session
3. Report recent activity log entries

### Plain text messages
Interpret as a task or question:
1. If it describes work to be done → treat as a new task, classify and dispatch
2. If it's a question → answer conversationally
3. If ambiguous → ask for clarification

### Responding
For results the user needs to see:
1. **If the user is active in the TUI** — output directly (your normal response)
2. **If the result is from a background task** — append to `.claude/events/pending_briefing.md` so it can be presented when the user next interacts

The pending briefing file format:
```markdown
## [timestamp] Task: "title"
Status: COMPLETE | FAILED
Summary: brief result

---
```

When the user sends a new message, check if `.claude/events/pending_briefing.md` has content. If so, present it first ("While you were away..."), then clear the file.

## Step 4: Listen Again

After handling the message, go back to Step 1:

```bash
inotifywait -e modify -e create .claude/events/inbox.jsonl
```

## Rules

- Run inotifywait in FOREGROUND — no background, no -m flag
- Handle ONE message per cycle, then listen again
- Use Agent tool with `run_in_background: true` for long-running dispatches
- Follow CLAUDE.md Dispatch Protocol and Verification Protocol for all task execution
- Never exit unless the user explicitly asks you to stop
- Remember: you ARE the Kernel when this command is running — you have full Agent tool access
