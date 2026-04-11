# SPEC-001: ORA Kernel Cloud — Managed Agent Fork

**Status**: Partially implemented — see Implementation Status below. Source of truth for the runtime architecture is now `docs/CLOUD_ARCHITECTURE.md`. This spec is retained as a historical record of the original plan and as a register of unimplemented acceptance criteria.
**Created**: 2026-04-08
**Last updated**: 2026-04-10
**Author**: AlturaFX + Claude Opus 4.6
**Source**: docs/CLOUD_ARCHITECTURE.md
**Repo**: `ora-kernel-cloud`

---

## Implementation Status (2026-04-10)

| Phase | Status |
|---|---|
| Phase 1 — Thin Orchestrator (MVP) | ✅ Complete |
| **Phase 2.5 — Dispatch Subsystem** (not in original spec, added 2026-04-10) | ✅ Complete |
| Phase 3 — File Sync | ✅ Complete (CDC + snapshot reconciliation, not the originally-envisioned write-through) |
| Phase 2 — Dashboard Integration | ⏸ Pending (Tasks 21 + 22 in the in-session task list) |
| Phase 4 — Hybrid Mode (local TUI + cloud) | 🗂 Deferred (not an MVP priority) |

**What changed from the original spec:** During live diagnostic testing on 2026-04-10, we discovered that the Anthropic Managed Agent toolset (`agent_toolset_20260401`) does NOT expose an Agent/Task/subagent-dispatch tool — only `bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_search`, `web_fetch`. This invalidated the base ora-kernel's dispatch model in the cloud. **Phase 2.5 — the dispatch subsystem — was added as a response**: the orchestrator became a dispatch broker, and the Kernel signals delegation via structured `` ```DISPATCH `` fenced blocks. This is architecturally significant and documented in `docs/CLOUD_ARCHITECTURE.md` § The Core Constraint.

---

## Goal

Create a fork of ORA Kernel that hosts the Kernel as a Claude Managed Agent — a persistent, always-on cloud session. This enables autonomous operation (heartbeats, briefings, idle work, self-improvement) without a TUI being open, and provides real-time monitoring via the existing forex-ml-platform dashboard.

### Problem This Solves

The base ORA Kernel requires a Claude Code TUI session to be running. The Kernel dies when you close the terminal. Autonomous features (heartbeat, briefing, idle work) only work when the TUI is active and `/kernel-listen` is backgrounded. This limits the system to "working while you're watching."

### Success Criteria

- [x] A Managed Agent session stays alive indefinitely without a TUI
- [x] Cron triggers (heartbeat, briefing, idle work, consolidation) are sent via API — plus new `/sync-snapshot` trigger added for file-sync reconciliation
- [x] SSE events are consumed and written to PostgreSQL in real-time
- [x] Token costs are tracked per-session with running totals
- [x] HITL approvals work — via stdin for the MVP (dashboard handler is Phase 2)
- [ ] The dashboard's Orchestration tab shows live Managed Agent activity — **Phase 2 pending**
- [x] WISDOM.md and journal entries survive container restarts — via CDC + snapshot reconciliation rather than the originally-envisioned write-through
- [ ] A local Claude Code TUI can share the same postgres state (hybrid mode) — **deferred, not an MVP priority**
- [x] *(Added 2026-04-10)* Kernel can dispatch work to focused sub-agents despite the missing Agent tool — via the Phase 2.5 dispatch subsystem

---

## Scope

### In Scope

1. **Thin orchestrator** — Python daemon managing the Managed Agent lifecycle
2. **Dashboard integration** — Extend existing Orchestration tab for Managed Agent monitoring
3. **File sync** — WISDOM.md and journal persistence across ephemeral containers
4. **Hybrid mode** — Local TUI + cloud agent sharing postgres
5. **Cost monitoring** — Real-time token and container cost tracking

### Out of Scope

- Modifying the Constitution or axioms (identical to base ORA Kernel)
- Changing node spec format (identical)
- Multi-tenant support (single user/org for now)
- Mobile app integration (future — Remote Control exists but is separate)

---

## Constraints

- Requires Anthropic API key with Managed Agents beta access
- API billing separate from Claude Code subscription
- Container runtime: $0.05/hr beyond 50 free hours/day
- Managed Agents API is in beta (`managed-agents-2026-04-01` header required)
- Each session gets isolated container — no shared filesystem between sessions
- Python 3.8+ for thin orchestrator (stdlib + `anthropic` SDK)

---

## Dependencies

| Dependency | Status | Notes |
|---|---|---|
| `ora-kernel` base repo | Complete | Fork from this |
| `anthropic` Python SDK | Available | `pip install anthropic` |
| PostgreSQL `ora_kernel` database | Exists | Same schema, shared state |
| forex-ml-platform dashboard | Exists | Extend Orchestration tab |
| Managed Agents API access | Beta | Enabled by default for API accounts |

---

## Contracts & Interfaces

### 1. Orchestrator ↔ Anthropic API

**Agent creation:**
```python
Agent = {
    name: str,           # "ORA Kernel"
    model: str,          # "claude-opus-4-6"
    system: str,         # Contents of CLAUDE.md (with ORA-KERNEL markers)
    tools: list,         # [{"type": "agent_toolset_20260401"}]
}
```

**Session lifecycle:**
```
create_session(agent_id, environment_id) → session_id
events.send(session_id, events=[user.message]) → void
events.stream(session_id) → SSE stream
session.retrieve(session_id) → status, event_count
```

**Event types consumed:**
```
agent.message     → Log to activity_log, forward to dashboard
agent.tool_use    → Log to activity_log, forward to dashboard
agent.tool_result → Log to activity_log
session.status_*  → Update session health, forward to dashboard
span.model_request_end → Write to otel_token_usage + otel_cost_tracking
```

**Event types sent:**
```
user.message            → Task dispatch, cron triggers (/heartbeat, /briefing, etc.)
user.interrupt          → Emergency stop
user.tool_confirmation  → HITL approval/denial from dashboard
```

### 2. Orchestrator ↔ PostgreSQL

Uses existing `ora_kernel` schema. New tables needed:

```sql
-- Track Managed Agent sessions
CREATE TABLE IF NOT EXISTS cloud_sessions (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,       -- Anthropic agent ID
    environment_id  TEXT NOT NULL,       -- Anthropic environment ID
    session_id      TEXT NOT NULL UNIQUE, -- Anthropic session ID
    status          TEXT NOT NULL DEFAULT 'created',
    container_start TIMESTAMPTZ,
    container_hours NUMERIC(10,4) DEFAULT 0,
    total_input_tokens  BIGINT DEFAULT 0,
    total_output_tokens BIGINT DEFAULT 0,
    total_cost_usd  NUMERIC(10,4) DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_event_at   TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ
);

-- Sync WISDOM.md and journal entries for container persistence
CREATE TABLE IF NOT EXISTS kernel_files_sync (
    file_path       TEXT PRIMARY KEY,    -- e.g., ".claude/kernel/journal/WISDOM.md"
    content         TEXT NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    synced_from     TEXT NOT NULL        -- 'container' or 'local'
);
```

### 3. Orchestrator ↔ Dashboard

**WebSocket protocol** (extends existing `orchestrator-client.js` message types):

```json
// New message types from orchestrator to dashboard:

// Agent status update
{"type": "managed_agent_status", "status": "running|idle|terminated", "session_id": "...", "uptime_hours": 12.5}

// Live event forwarding
{"type": "managed_agent_event", "event_type": "agent.tool_use", "tool_name": "Bash", "input": "...", "timestamp": "..."}
{"type": "managed_agent_event", "event_type": "agent.message", "text": "...", "timestamp": "..."}

// Cost ticker update
{"type": "managed_agent_cost", "session_cost_usd": 1.234, "hourly_rate": 0.05, "tokens_today": {"input": 50000, "output": 12000}}

// HITL request forwarded from agent
{"type": "managed_agent_hitl", "request_id": "...", "tool_name": "...", "description": "...", "options": ["approve", "deny"]}

// From dashboard to orchestrator:

// HITL response
{"type": "managed_agent_hitl_response", "request_id": "...", "decision": "approve|deny", "note": "optional reason"}

// Direct message to agent
{"type": "managed_agent_message", "content": "user message text"}
```

### 4. Container ↔ PostgreSQL

The Managed Agent's container connects to postgres for task state. Environment networking must allow the postgres host.

Connection string passed via bootstrap event or environment variable.

---

## Task Breakdown

### Phase 1: Thin Orchestrator (MVP)

**Task 1.1: Fork repo and set up ora-kernel-cloud**
- Fork `ora-kernel` to `ora-kernel-cloud`
- Add `anthropic` SDK dependency
- Add `orchestrator/` directory for new code
- Acceptance: repo exists, installs cleanly

**Task 1.2: Agent and environment management**
- `orchestrator/agent_manager.py`: create/retrieve agent and environment
- Store IDs in local config file (`.ora-kernel-cloud.json`)
- Idempotent: reuse existing agent/environment if already created
- Acceptance: `python orchestrator/agent_manager.py setup` creates agent + env, prints IDs

**Task 1.3: Session lifecycle**
- `orchestrator/session_manager.py`: create session, send bootstrap event, handle restart on termination
- Bootstrap clones repo, runs install.py, reads CLAUDE.md
- Acceptance: session starts, agent reports ready, survives container restart

**Task 1.4: SSE event consumer**
- `orchestrator/event_consumer.py`: connect to stream, parse events by type, write to postgres
- Map `span.model_request_end` → `otel_token_usage` + `otel_cost_tracking`
- Map `agent.tool_use` / `agent.tool_result` → `orch_activity_log`
- Print events to stdout as fallback UI
- Acceptance: events flow from agent through consumer to postgres, token counts match

**Task 1.5: Cron trigger scheduler**
- `orchestrator/scheduler.py`: APScheduler or simple threading.Timer
- Sends `/heartbeat` (every 2hrs), `/briefing` (daily 8am), `/idle-work` (off-hours), `/consolidate` (weekly)
- Configurable via `config.yaml`
- Acceptance: triggers arrive at agent on schedule, agent processes them

**Task 1.6: HITL via stdin (MVP)**
- When `user.tool_confirmation` needed, print to stdout and read from stdin
- Temporary until dashboard integration (Phase 2)
- Acceptance: can approve/deny tool calls from terminal

**Task 1.7: Main entry point**
- `orchestrator/main.py`: ties together agent_manager, session_manager, event_consumer, scheduler
- `python -m orchestrator` starts the full system
- Graceful shutdown on SIGTERM/SIGINT
- Acceptance: single command starts everything, Ctrl+C shuts down cleanly

### Phase 2.5: Dispatch Subsystem (added 2026-04-10)

**Context**: The Managed Agent toolset has no Agent/Task tool. Without this phase, Axioms 2 and 9 cannot be enforced and the base ora-kernel's orchestration model is aspirational in the cloud. See `docs/CLOUD_ARCHITECTURE.md` § DispatchManager for the full design.

**Task 2.5.1: Feasibility spike** ✅
- Validate that creating a second Managed Agent session from the orchestrator is cheap and fast
- Acceptance: `spikes/subagent_feasibility.py` runs, returns real latency/cost numbers
- Result: ~0.3s agent.create, ~0.4s session.create, ~4.8s trivial roundtrip, ~$0.00014 per trivial call

**Task 2.5.2: Dispatch DB tables** ✅
- Migration `008_dispatch.sql` creates `dispatch_agents` (per-node cache with prompt hash) and `dispatch_sessions` (per-dispatch lifecycle)
- Acceptance: migration applies cleanly, `db.py` has CRUD helpers, integration tests pass

**Task 2.5.3: Fence parser** ✅
- `parse_dispatch_fences` — extracts `(node_name, payload_dict)` pairs from `` ```DISPATCH `` fenced blocks in agent messages
- Skips malformed fences (missing node attribute, invalid JSON, non-object payload)
- Acceptance: unit tests cover all valid + invalid shapes

**Task 2.5.4: Per-node agent cache** ✅
- `DispatchManager._ensure_agent` — look up cached agent by node name + prompt hash; create fresh if missing or stale
- Acceptance: spec edits trigger rebuild, unchanged specs hit cache

**Task 2.5.5: Sub-session lifecycle** ✅
- `DispatchManager._run_sub_session` — creates session, sends task, streams events, records lifecycle
- Stall watchdog via `httpx.Timeout(read=...)`; wall-clock ceiling via `max_dispatch_seconds`
- Handles `session.status_terminated` as failure; records `CDC_MISSING_BASE` / `CDC_DIVERGENCE` equivalents as `orch_activity_log` rows
- Acceptance: unit tests + live smoke test with `smoke_test_node` complete in ~7s

**Task 2.5.6: Top-level routing + result forwarding** ✅
- `DispatchManager.handle_message` — processes all fences in a message, catches per-dispatch exceptions, forwards results via `send_to_parent` callback
- `_format_result_fence` — renders results as `` ```DISPATCH_RESULT `` fenced blocks the Kernel parses
- Acceptance: unit tests cover success, missing node, no-fence, and continue-after-failure cases

**Task 2.5.7: Protocol teaching** ✅
- `DISPATCH_PROTOCOL` constant in `session_manager.py`
- Embedded in `BOOTSTRAP_PROMPT` for fresh sessions
- `SessionManager.send_protocol_refresh()` re-teaches both SYNC and DISPATCH protocols on every orchestrator boot against a resumed session
- Acceptance: fresh session works via bootstrap; resumed session works via refresh (verified live on 2026-04-10)

**Task 2.5.8: Event consumer wiring** ✅
- `EventConsumer._handle_message` routes DISPATCH fences to `DispatchManager.handle_message` (same pattern as SYNC fences → `FileSync`)
- Both routes fire on every message; exceptions are caught and logged so one handler can never wedge the SSE loop
- Acceptance: integration tests + live smoke test

**Task 2.5.9: Main entry point wiring** ✅
- `__main__.py` constructs `DispatchManager` pointing at `kernel-files/.claude/kernel/nodes/system/`, with a dedicated `Anthropic` client for sub-session calls
- `send_to_parent` is a thin lambda over `session_mgr.send_message`
- On resume, calls `send_protocol_refresh` after logging the resume

### Phase 2: Dashboard Integration

**Task 2.1: WebSocket bridge**
- `orchestrator/ws_bridge.py`: WebSocket server (port 8002)
- Consumes events from event_consumer, translates to dashboard protocol
- Acceptance: dashboard connects to ws://localhost:8002 and receives events

**Task 2.2: Dashboard agent health panel**
- New panel in Orchestration tab showing: session status, uptime, container hours
- Cytoscape node for Managed Kernel (always visible, color = status)
- Acceptance: panel shows live status, updates on status change

**Task 2.3: Dashboard cost panel**
- Real-time token cost display, hourly burn rate, daily/monthly projection
- Extends existing budget ticker HUD
- Acceptance: costs update in real-time as agent works

**Task 2.4: Dashboard event stream panel**
- Live scrolling log of agent events (tool calls, messages)
- Filter by event type
- Acceptance: events appear within 1 second of occurrence

**Task 2.5: Dashboard HITL integration**
- Extend existing HITL widget to handle Managed Agent confirmation requests
- Forward responses via WebSocket → orchestrator → API
- Acceptance: can approve/deny from dashboard, agent proceeds

### Phase 3: File Sync ✅

Implemented with a different mechanism than originally envisioned. The original "journal/WISDOM write-through via psql from the container" was ruled out by Invariant 1 (container never speaks to postgres). Instead, file sync runs entirely orchestrator-side via two complementary paths: CDC from `agent.tool_use` events, and snapshot reconciliation via the SYNC fence protocol. See `docs/CLOUD_ARCHITECTURE.md` § FileSync.

**Task 3.1: Postgres file sync tables** ✅
- Migration `007_cloud_sessions.sql` creates `kernel_files_sync` + `cloud_sessions`
- Acceptance met

**Task 3.2: Bootstrap with file hydration** ✅
- `SessionManager._build_hydration_instructions` pulls WISDOM.md and recent journal entries from `kernel_files_sync` and injects them into `BOOTSTRAP_PROMPT`
- Acceptance met

**Task 3.3: CDC write capture** ✅ *(replaces the original "write-through via psql")*
- `FileSync.handle_write` captures full content from Write tool_use events
- `FileSync.handle_edit` applies Edit diffs server-side against cached content
- Divergences logged as observable `orch_activity_log` rows
- Tracked paths: `.claude/kernel/journal/**/*.md`, `.claude/kernel/nodes/**/*.md`
- Acceptance: round-trip verified live 2026-04-10 (CDC write captured, row visible in `kernel_files_sync` with `synced_from='cdc'`)

**Task 3.4: SYNC fence snapshot reconciliation** ✅ *(new, not in original spec)*
- `SYNC_SNAPSHOT_PROTOCOL` constant taught to Kernel via bootstrap + scheduler trigger
- `FileSync.handle_snapshot_response` parses `` ```SYNC path=... ``` `` fences and writes with `synced_from='snapshot'`
- Backstop for anything CDC missed (bash-based writes, Edit divergences)
- Acceptance: verified live 2026-04-10 (snapshot row landed with correct content)

**Task 3.5 (deferred): Node spec git flow**
- New node specs from self-expansion committed to git from the container
- Status: not yet needed — the dispatch subsystem made self-expansion work again, but we have not exercised NodeDesigner → NodeCreator end-to-end yet
- Acceptance criteria preserved for future work

### Phase 4: Hybrid Mode

**Task 4.1: Shared task routing**
- Tasks created in local TUI appear in Kernel's postgres queue
- Kernel's autonomous results visible in local TUI session
- Acceptance: create task locally, see it dispatched by cloud Kernel

**Task 4.2: Unified activity log**
- Both local and cloud sources write to same `orch_activity_log`
- Self-improvement cycle sees all activity regardless of source
- Acceptance: `/self-improve` analyzes tasks from both contexts

---

## Validation Plan

### Automated Tests
```bash
# Phase 1
pytest orchestrator/tests/test_agent_manager.py     # Agent/env creation
pytest orchestrator/tests/test_session_manager.py    # Session lifecycle
pytest orchestrator/tests/test_event_consumer.py     # Event parsing + postgres writes
pytest orchestrator/tests/test_scheduler.py          # Cron trigger timing

# Phase 2
pytest orchestrator/tests/test_ws_bridge.py          # WebSocket message translation

# Phase 3
pytest orchestrator/tests/test_file_sync.py          # Postgres read/write roundtrip
```

### Integration Tests
```bash
# End-to-end: start orchestrator, send task, verify in postgres
python -m orchestrator --test-mode

# Dashboard: open browser, verify panels render
playwright-cli open http://localhost:8080/dashboard.html
playwright-cli snapshot  # Verify Orchestration tab shows Managed Agent node
```

### Manual Verification
- [ ] Start orchestrator, verify agent session created in Claude Console
- [ ] Send `/heartbeat`, verify silent when healthy
- [ ] Send a task, watch it flow through dashboard in real-time
- [ ] Trigger HITL, approve from dashboard, verify agent continues
- [ ] Kill container, verify session restarts and WISDOM.md survives
- [ ] Open Claude Code TUI, create task, verify cloud Kernel picks it up

---

## Files to Create

### New (in ora-kernel-cloud fork) — as actually built

| File | Purpose | Status |
|---|---|---|
| `orchestrator/__init__.py` | Package init | ✅ |
| `orchestrator/__main__.py` | Entry point (`python -m orchestrator`); constructs + wires FileSync, DispatchManager, StdinHitlHandler, EventConsumer, KernelScheduler | ✅ |
| `orchestrator/agent_manager.py` | Agent + environment CRUD | ✅ |
| `orchestrator/session_manager.py` | Session lifecycle + bootstrap; authoritative source for `SYNC_SNAPSHOT_PROTOCOL` and `DISPATCH_PROTOCOL`; `send_protocol_refresh` for resume drift closure | ✅ |
| `orchestrator/event_consumer.py` | SSE stream handler; routes events to FileSync, DispatchManager, HITL | ✅ |
| `orchestrator/scheduler.py` | APScheduler trigger dispatcher (`/heartbeat`, `/briefing`, `/idle-work`, `/consolidate`, `/sync-snapshot`) | ✅ |
| `orchestrator/file_sync.py` | CDC + snapshot reconciliation for operational-memory files | ✅ |
| `orchestrator/dispatch.py` | DISPATCH fence parser + DispatchManager sub-session broker | ✅ |
| `orchestrator/hitl.py` | Stdin-based HITL handler (Phase 2 will swap for WebSocket) | ✅ |
| `orchestrator/config.py` | `.env` + `config.yaml` loader | ✅ |
| `orchestrator/db.py` | psycopg2 wrapper + CDC/dispatch helpers | ✅ |
| `config.yaml` | Scheduler + postgres defaults | ✅ |
| `requirements.txt` | `anthropic`, `psycopg2-binary`, `apscheduler`, `websockets`, `python-dotenv`, `pytest` | ✅ |
| `kernel-files/infrastructure/db/007_cloud_sessions.sql` | `cloud_sessions` + `kernel_files_sync` | ✅ |
| `kernel-files/infrastructure/db/008_dispatch.sql` | `dispatch_agents` + `dispatch_sessions` | ✅ *(added for Phase 2.5)* |
| `kernel-files/.claude/kernel/nodes/system/smoke_test_node.md` | Minimal node spec for dispatch pipeline validation | ✅ *(added for Phase 2.5)* |
| `spikes/subagent_feasibility.py` | Option 3 feasibility spike | ✅ *(added for Phase 2.5)* |
| `spikes/check_sub_session.py` | Diagnostic for stuck sub-sessions | ✅ *(added for Phase 2.5)* |
| `orchestrator/ws_bridge.py` | WebSocket bridge to dashboard | ⏸ *(Phase 2 — not yet built)* |
| ~~`orchestrator/main.py`~~ | *(Originally planned; replaced by `__main__.py` pattern)* | — |

### Modified (from base ora-kernel)

| File | Change |
|---|---|
| `kernel-files/CLAUDE.md` | Add note about cloud mode + postgres connectivity instructions |
| `README.md` | Cloud-specific quickstart, cost model, dashboard setup |
| `CHANGELOG.md` | v2.0.0 entries |

### Dashboard files (in forex-ml-platform, separate PR)

| File | Change |
|---|---|
| `dashboard.html` | New panels in Orchestration tab |
| `src/dashboard/orchestrator-client.js` | New WebSocket connection to port 8002, new event handlers |

---

## Risks & Open Questions

### Risks

1. **Beta instability** — Managed Agents is in beta. API may change between releases. Mitigation: pin SDK version, abstract API calls behind our own interfaces.

2. **Container cold start** — Each session needs to clone repo and set up. Could add 30-60 seconds to restart. Mitigation: minimize bootstrap, cache packages in environment.

3. **Cost runaway** — An agent stuck in a loop burns tokens. Mitigation: our loop detector runs in the container (hooks work via filesystem), plus the orchestrator monitors `span.model_request_end` costs and can `user.interrupt` if budget exceeded.

4. **Postgres connectivity from container** — The container needs network access to your postgres. If postgres is local (not cloud-hosted), you'd need a tunnel or public endpoint. Mitigation: document this requirement clearly; suggest cloud postgres (Supabase, Railway, Neon) as an option.

### Open Questions — Resolutions

1. **Environment variable injection for POSTGRES_DSN** — **Moot.** Invariant 1 of the cloud architecture is that the container never speaks directly to postgres. All state flows through the orchestrator via the SSE event stream. The container never needs a DSN.

2. **Agent versioning on CLAUDE.md updates** — **Not a concern for the parent Kernel.** The parent agent's system prompt is `kernel-files/CLAUDE.md`, which is loaded by `agent_manager.ensure_agent` at orchestrator startup; edits to CLAUDE.md don't take effect until the next orchestrator boot. Protocol changes are delivered via `BOOTSTRAP_PROMPT` and `send_protocol_refresh`, which are updated on every startup regardless. For per-node agents in the dispatch subsystem, the `prompt_hash` column on `dispatch_agents` forces a fresh agent creation when the node spec content changes.

3. **Multi-project** — **Single-project for the MVP.** The orchestrator's state (`.ora-kernel-cloud.json`, the shared environment ID, the cached agent IDs) is all scoped to a single project directory. Running two projects means running two orchestrators with two separate state files. A multi-project extension is a backlog item, not an architectural blocker.

4. **Dashboard deployment** — **Cloud container cannot talk to the dashboard directly.** HITL always flows through the orchestrator. Invariant 1 and simplicity considerations both argue against giving the container network access to the operator's local forex-ml dashboard. The orchestrator is the hub.

### New Open Questions — from the 2026-04-10 implementation work

5. **Dispatch idempotency on orchestrator crash.** Currently: if the orchestrator crashes mid-dispatch, the sub-session continues running on Anthropic's side until its own idle timeout. The stuck `dispatch_sessions` row stays in `status='running'`. On orchestrator restart, there is no reconciliation sweep — the parent session's stream doesn't replay the triggering message (idle sessions don't replay), so no re-dispatch occurs, but the orphaned sub-session is invisible. Proposed fix: a startup sweep that queries `WHERE status='running'` and either (a) reopens the stream to consume any remaining events, or (b) marks the row failed and moves on.

6. **Parallel dispatch.** Serial is the MVP constraint. A Quad (4 nodes) takes ~4× the time of a single dispatch. Wrapping `_run_sub_session` in a thread pool is the upgrade path; the fence protocol doesn't change.

7. **Cost rollup across parent + sub-sessions.** `otel_cost_tracking` is parent-only (driven by parent SSE `span.model_request_end` events). Per-dispatch costs live in `dispatch_sessions.cost_usd`. There is no single "what did this task cost me?" query — the operator must sum both.

8. **Per-dispatch token ceiling.** No budget enforcement inside `DispatchManager` beyond the wall-clock `max_dispatch_seconds`. A runaway node could burn substantial tokens before hitting the 10-minute cutoff. A budget wrapper integrating with the base `orch_budget_limits` table is a clean extension.

9. **`orch_tasks` is never written by the cloud Kernel.** This has always been true — no cloud session has written a task row. Base ora-kernel relied on the Agent tool's dispatch events to create task rows. In the cloud, the dispatch subsystem could plausibly write `orch_tasks` rows from `DispatchManager.handle_message` on behalf of the Kernel. Not done yet — call-out for future work.

---

## Handoff Notes

This spec is ready for implementation. Recommended approach:

1. **Fork the repo** — `gh repo fork AlturaFX/ora-kernel --fork-name ora-kernel-cloud`
2. **Phase 1 first** — The thin orchestrator is the foundation. Implement tasks 1.1-1.7 before anything else.
3. **Use `/build-with-agent-team`** — Tasks 1.2-1.6 are independent Python modules that can be built in parallel by separate agents, then integrated in Task 1.7.
4. **Dashboard work is a separate PR** against forex-ml-platform, not the ora-kernel-cloud repo.
