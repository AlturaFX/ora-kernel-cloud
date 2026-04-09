# ORA Kernel Cloud — Managed Agent Architecture

## Overview

ORA Kernel Cloud extends the base ORA Kernel by hosting the Kernel as a Claude Managed Agent — a persistent, always-on cloud session that survives disconnections and doesn't depend on a TUI being open. It adds a thin Python orchestrator for session management and a dashboard integration for real-time monitoring.

This is a separate fork (`ora-kernel-cloud`) because it requires API billing, unlike the base ORA Kernel which runs entirely within Claude Code's subscription.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ Anthropic Cloud                                          │
│                                                          │
│  ┌────────────────────────────────────────┐              │
│  │ Managed Agent: "ORA Kernel"            │              │
│  │                                        │              │
│  │ Agent:                                 │              │
│  │   model: claude-opus-4-6               │              │
│  │   system: CLAUDE.md content            │              │
│  │   tools: agent_toolset_20260401        │              │
│  │                                        │              │
│  │ Environment:                           │              │
│  │   packages: python3, psql client       │              │
│  │   networking: limited (postgres host,  │              │
│  │     github.com for repo clone)         │              │
│  │                                        │              │
│  │ Container filesystem:                  │              │
│  │   /work/.claude/kernel/ (cloned)       │              │
│  │   /work/.claude/hooks/  (cloned)       │              │
│  │   /work/CLAUDE.md       (via system)   │              │
│  └──────────────┬─────────────────────────┘              │
│                 │ SSE event stream                        │
└─────────────────┼────────────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────────────────┐
│ Your Machine                                             │
│                                                          │
│  ┌────────────────────────────────────────┐              │
│  │ Thin Orchestrator (Python)             │              │
│  │                                        │              │
│  │ - Manages agent/environment/session    │              │
│  │ - Consumes SSE stream                  │              │
│  │ - Writes events to PostgreSQL          │              │
│  │ - Sends cron triggers as user.message  │              │
│  │ - Forwards HITL to dashboard           │              │
│  │ - Calculates costs from span events    │              │
│  └────┬───────────────┬──────────────────┘              │
│       │               │                                  │
│       ▼               ▼                                  │
│  ┌─────────┐   ┌──────────────────┐                     │
│  │PostgreSQL│   │ Dashboard (WS)   │                     │
│  │ora_kernel│   │                  │                     │
│  │          │   │ Orchestration tab│                     │
│  │ - tasks  │   │ - Live task graph│                     │
│  │ - logs   │   │ - Agent status   │                     │
│  │ - metrics│   │ - Token costs    │                     │
│  │ - wisdom │   │ - HITL approvals │                     │
│  └─────────┘   │ - Activity feed  │                     │
│                 └──────────────────┘                     │
│                                                          │
│  ┌────────────────────────────────────────┐              │
│  │ Claude Code TUI (optional)             │              │
│  │ Interactive work, same postgres state  │              │
│  └────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────┘
```

## Components

### 1. Managed Agent Configuration

**Agent** — Created once via API, referenced by ID:
```python
agent = client.beta.agents.create(
    name="ORA Kernel",
    model="claude-opus-4-6",
    system=open("kernel-files/CLAUDE.md").read(),
    tools=[{"type": "agent_toolset_20260401"}],
)
```

**Environment** — Cloud container with limited networking:
```python
environment = client.beta.environments.create(
    name="ora-kernel-env",
    config={
        "type": "cloud",
        "packages": {
            "pip": ["psycopg2-binary"],
            "apt": ["inotify-tools", "postgresql-client"],
        },
        "networking": {
            "type": "limited",
            "allowed_hosts": [
                "your-postgres-host:5432",
                "github.com",
            ],
        },
    },
)
```

**Session bootstrap** — First event clones the repo and sets up the workspace:
```python
session = client.beta.sessions.create(
    agent=agent.id,
    environment_id=environment.id,
    title="ORA Kernel — persistent session",
)

client.beta.sessions.events.send(session.id, events=[{
    "type": "user.message",
    "content": [{
        "type": "text",
        "text": """Bootstrap: Clone the ORA Kernel repo and set up your workspace.
        
1. git clone https://github.com/AlturaFX/ora-kernel.git /work/ora-kernel
2. python3 /work/ora-kernel/install.py /work --force
3. Read CLAUDE.md to confirm your operating instructions are loaded
4. Read .claude/kernel/journal/WISDOM.md for operational context
5. Report ready status"""
    }]
}])
```

### 2. Thin Orchestrator (`orchestrator.py`)

A Python daemon running locally that:

**Session management:**
- Creates/resumes the Managed Agent session
- Handles session.status_terminated by restarting
- Graceful shutdown on SIGTERM

**Event consumption:**
- Connects to SSE stream
- Parses every event by type
- Writes to PostgreSQL:
  - `agent.tool_use` / `agent.tool_result` → `orch_activity_log`
  - `span.model_request_end` → `otel_token_usage` + `otel_cost_tracking`
  - `session.status_*` → session health tracking
- Calculates running cost from `model_usage` token counts

**Cron trigger forwarding:**
- Instead of cron scripts writing to inbox.jsonl, they call the orchestrator
- Orchestrator sends `user.message` events with `/heartbeat`, `/briefing`, `/idle-work`, `/consolidate`
- Or: orchestrator has its own scheduler (APScheduler) and sends directly

**HITL forwarding:**
- When the agent needs approval (`user.tool_confirmation` events), forward to dashboard via WebSocket
- Dashboard shows approval UI (already has HITL widget with Approve/Discuss buttons)
- User's response sent back as `user.tool_confirmation` event

**Dashboard WebSocket bridge:**
- Consumes SSE events from Anthropic
- Translates to WebSocket messages the dashboard already understands
- Extends the existing message types with Managed Agent-specific events

### 3. Dashboard Integration

Extend the existing Orchestration tab (`orchestrator-client.js`) with:

**New data source:**
- Connect to thin orchestrator's WebSocket endpoint (new port, e.g., 8002)
- Receive translated Managed Agent events

**New Cytoscape node types:**
- "Managed Kernel" node (always present, shows running/idle status)
- "Cloud Subagent" nodes (when Kernel dispatches work)
- Color: green = running, blue = idle, red = error

**New panels:**
- **Agent Health Panel**: session status, uptime, container runtime hours
- **Cost Panel**: real-time token costs, hourly burn rate, daily/monthly projection
- **Event Stream Panel**: live scroll of agent events (tool calls, messages, decisions)

**Enhanced HITL widget:**
- Already has Approve/Discuss buttons
- Extend to handle `user.tool_confirmation` events from Managed Agent
- Show what the agent wants to do and why (from the event payload)

### 4. Shared PostgreSQL State

Both the Managed Agent (via psql in container) and the local Claude Code TUI connect to the same `ora_kernel` database. This means:

- Tasks created interactively appear in the Kernel's queue
- Kernel's autonomous work (heartbeat, briefing, idle work) is visible locally
- WISDOM.md could be synced to/from a postgres table for cross-environment sharing
- Activity logs from both sources feed the self-improvement cycle

### 5. File Sync Strategy

The Managed Agent's container filesystem is ephemeral — files don't persist across sessions. Strategy:

**On session start (bootstrap):**
1. Clone ora-kernel repo → get latest node specs, schemas, hooks
2. Pull WISDOM.md and recent journal entries from postgres (new sync table)
3. Read PROJECT_DNA.md from repo or postgres

**During session:**
- Journal entries written to container filesystem AND postgres
- WISDOM.md updates written to container AND postgres
- New node specs (from self-expansion) written to container AND committed to git

**On session end/restart:**
- Final journal entry captured
- Any uncommitted node specs pushed to git

## Cost Model

| Component | Cost | Notes |
|---|---|---|
| Container runtime | $0.05/hr (50 free hrs/day) | ~$36/month for 24/7 |
| Opus tokens (input) | $5/M tokens | Only when agent is active |
| Opus tokens (output) | $25/M tokens | Only when agent is active |
| Heartbeat (12/day) | ~$0.10/day | Minimal queries, often silent |
| Daily briefing (1/day) | ~$0.15/day | Moderate query + formatting |
| Idle work (3/night) | ~$1-3/day | Depends on task complexity |
| Self-improvement (weekly) | ~$2-5/week | Full analysis cycle |
| **Estimated monthly** | **$50-150** | Varies with workload |

## What Changes vs Base ORA Kernel

| Component | Base (Claude Code) | Cloud (Managed Agent) |
|---|---|---|
| Kernel host | TUI session + inotifywait | Managed Agent cloud session |
| Event input | inbox.jsonl file writes | `events.send()` API calls |
| Event output | pending_briefing.md | SSE stream → orchestrator → dashboard |
| Cron triggers | Shell scripts → file writes | Orchestrator scheduler → API calls |
| Hooks | settings.json filesystem hooks | Container filesystem hooks (same scripts) |
| HITL | TUI text prompts | Dashboard approval widget |
| Monitoring | Manual / OTel pipeline | SSE events → postgres + dashboard |
| Persistence | TUI must stay open | Always-on cloud session |
| Billing | Claude Code subscription | API tokens + container hours |

## What Stays Identical

- Constitution (9 axioms)
- CLAUDE.md content (loaded as system prompt)
- Node spec files (cloned into container)
- PostgreSQL schema (same tables)
- WISDOM.md + journal system
- Self-improvement cycle logic
- Suggestion feedback loop
- All node behavioral contracts

## Implementation Phases

### Phase 1: Thin Orchestrator (MVP)
- Create/resume Managed Agent session
- Consume SSE stream, write to postgres
- Forward cron triggers as user.message events
- Basic HITL: print to stdout, read from stdin

### Phase 2: Dashboard Integration
- WebSocket bridge from orchestrator to dashboard
- New Managed Agent panels in Orchestration tab
- HITL approval via dashboard
- Cost tracking display

### Phase 3: File Sync
- WISDOM.md + journal postgres sync tables
- Bootstrap script that hydrates container from postgres
- Git commit flow for new node specs

### Phase 4: Hybrid Mode
- Local Claude Code TUI and cloud Managed Agent sharing postgres
- Task routing: interactive work → TUI, autonomous work → cloud
- Unified activity log across both
