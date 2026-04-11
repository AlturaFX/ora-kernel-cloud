# ORA Kernel Cloud — Architecture

## Overview

ORA Kernel Cloud hosts the ORA Kernel as an Anthropic **Managed Agent** — a persistent, always-on cloud session that survives disconnections and does not depend on a Claude Code TUI being open. A thin Python orchestrator runs on the operator's machine, owns the SSE event stream, persists every event to PostgreSQL, sends scheduled triggers, runs human-in-the-loop approvals, mirrors the Kernel's operational-memory files (WISDOM.md, journal entries, node specs) to postgres via change-data-capture, and translates the Kernel's dispatch requests into short-lived sub-agent sessions that stand in for the missing Agent tool.

This is a **separate fork** of `ora-kernel` because it requires API billing and because its runtime model is fundamentally different from the base kernel — the Managed Agent toolset has no subagent-dispatch primitive, so the cloud fork reconstructs delegation at the orchestrator layer.

## The Core Constraint (Read This First)

**The Anthropic Managed Agent toolset `agent_toolset_20260401` does NOT include an Agent, Task, or any subagent-dispatch tool.** The Kernel has access to `bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_search`, `web_fetch` — nothing else.

This invalidates the entire ORA Kernel dispatch protocol as written in `kernel-files/CLAUDE.md`, which assumes the Kernel can invoke subagents directly via the Agent tool. Without that primitive, the Quad pattern, the NodeDesigner → NodeCreator self-expansion pipeline, the self-improvement cycle, and Axioms 2 (Objective Verification) and 9 (Separation of Concerns) all become unenforceable.

**The cloud fork solves this by making the orchestrator a dispatch broker.** The Kernel signals dispatch intent in its messages via structured fenced blocks. The orchestrator — running on the operator's machine — parses those blocks, spins up a focused Managed Agent sub-session per dispatch (with the node's system prompt as the sub-agent's system prompt), consumes the sub-session's event stream, and forwards the result back to the parent Kernel session as a `user.message` carrying a `DISPATCH_RESULT` fence. Each "subagent" is a first-class cloud session.

This design preserves the original architectural model in spirit — verifiers ARE genuinely separate agents, work IS delegated, Axiom 9 IS enforceable — at the cost of a fattened orchestrator, per-dispatch container-hours, and serial (not yet parallel) execution.

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│ Anthropic Cloud                                               │
│                                                               │
│  ┌─────────────────────────────┐                              │
│  │ Parent session              │   <── agent_toolset_20260401 │
│  │   agent = ORA Kernel         │        (no Agent tool)       │
│  │   system = CLAUDE.md         │                              │
│  │   env   = ora-kernel-env     │                              │
│  └──────┬──────────────────────┘                              │
│         │ emits ```DISPATCH fences in agent.message          │
│         │ receives ```DISPATCH_RESULT fences in user.message │
│         ▼                                                      │
│  ┌─────────────────────────────┐   ┌────────────────────────┐ │
│  │ Sub-session: node_designer  │   │ Sub-session: verifier  │ │
│  │   agent = per-node cached   │   │   agent = per-node     │ │
│  │   system = node spec .md    │   │   system = verifier .md│ │
│  │   env   = shared            │   │   env   = shared       │ │
│  └──────┬──────────────────────┘   └──────┬─────────────────┘ │
│         │  (more sub-sessions on demand, one per dispatch)   │
└─────────┼───────────────────────────────────────────────────────┘
          │ SSE event streams (parent + all sub-sessions)
          ▼
┌───────────────────────────────────────────────────────────────┐
│ Operator Machine                                              │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ Thin Orchestrator (python3 -m orchestrator)             │  │
│  │                                                         │  │
│  │  ┌──────────────┐  ┌────────────────┐  ┌────────────┐  │  │
│  │  │SessionManager│  │ EventConsumer  │  │KernelSched │  │  │
│  │  │ - bootstrap  │◄─┤ - SSE loop     │  │ - /hb      │  │  │
│  │  │ - resume     │  │ - route events │  │ - /brief   │  │  │
│  │  │ - protocol   │  │ - cost tracking│  │ - /idle    │  │  │
│  │  │   refresh    │  │                │  │ - /sync    │  │  │
│  │  └──────────────┘  └──┬─────────────┘  └────────────┘  │  │
│  │                       │                                │  │
│  │           ┌───────────┼─────────────┬──────────────┐   │  │
│  │           ▼           ▼             ▼              ▼   │  │
│  │  ┌────────────┐ ┌───────────┐ ┌────────────┐ ┌───────┐ │  │
│  │  │ FileSync   │ │DispatchMgr│ │StdinHitl   │ │ db.py │ │  │
│  │  │ - CDC Write│ │ - fence   │ │ - prompt   │ │ psql  │ │  │
│  │  │ - CDC Edit │ │   parser  │ │ - approve  │ │       │ │  │
│  │  │ - SYNC     │ │ - agent   │ │ - deny     │ │       │ │  │
│  │  │   snapshot │ │   cache   │ │            │ │       │ │  │
│  │  │            │ │ - sub-ses │ │            │ │       │ │  │
│  │  │            │ │   lifecyc │ │            │ │       │ │  │
│  │  └────────────┘ └───────────┘ └────────────┘ └───┬───┘ │  │
│  └──────────────────────────────────────────────────┼─────┘  │
│                                                     │        │
│                                                     ▼        │
│                                            ┌────────────────┐│
│                                            │ PostgreSQL     ││
│                                            │  ora_kernel    ││
│                                            │                ││
│                                            │ cloud_sessions ││
│                                            │ dispatch_*     ││
│                                            │ kernel_files   ││
│                                            │   _sync        ││
│                                            │ orch_activity  ││
│                                            │   _log         ││
│                                            │ otel_*         ││
│                                            └────────────────┘│
│                                                               │
│  ┌────────────────────────────────────────────┐               │
│  │ Future: Dashboard (Phase 2, pending)       │               │
│  │ WebSocket bridge → forex-ml Orchestration  │               │
│  └────────────────────────────────────────────┘               │
└───────────────────────────────────────────────────────────────┘
```

## Components

### SessionManager (`orchestrator/session_manager.py`)

Owns the parent Managed Agent session lifecycle:
- **Create** or **resume** the parent session (session IDs persist in `.ora-kernel-cloud.json`).
- **Bootstrap** a fresh session by sending a rich `user.message` that clones the ora-kernel repo into the container, installs the kernel files, and teaches the current **SYNC** and **DISPATCH** protocols inline (so the Kernel does not depend on its protected CLAUDE.md knowing them).
- **Resume refresh**: on every orchestrator boot against an existing session, `send_protocol_refresh()` sends a single `user.message` containing the current protocol constants — this closes the drift window where a session bootstrapped with an old orchestrator version would not know about newer protocols.
- **Send** user messages, tool confirmations, and interrupts to the parent session.
- **Restart on termination**: if the session hits `session.status_terminated`, create a new one with exponential backoff.

### EventConsumer (`orchestrator/event_consumer.py`)

Blocking SSE loop over the parent session's event stream. Dispatches each event to a handler:

| SSE event | Handler | Side effects |
|---|---|---|
| `agent.message` | `_handle_message` | Log full text (up to `TEXT_PREVIEW_LEN=10_000`) to `orch_activity_log`; scan for SYNC fences → `FileSync.handle_snapshot_response`; scan for DISPATCH fences → `DispatchManager.handle_message` |
| `agent.tool_use` | `_handle_tool_use` | Log to `orch_activity_log`; if tool is `write`/`Write` → `FileSync.handle_write`; if `edit`/`Edit` → `FileSync.handle_edit`; if `tool_confirmation` → `on_hitl_needed` callback |
| `agent.tool_result` | `_handle_tool_result` | Log to `orch_activity_log` |
| `span.model_request_end` | `_handle_model_request_end` | Write to `otel_token_usage` + `otel_cost_tracking`; update running totals |
| `session.status_running` | `_handle_status_running` | Upsert `cloud_sessions` row |
| `session.status_idle` | `_handle_status_idle` | Upsert + log |
| `session.status_terminated` | `_handle_status_terminated` | Upsert + log; returns `False` to signal restart |

**Tool name matching is case-insensitive.** The Managed Agent toolset emits lowercase tool names (`write`, `edit`, `bash`) while Claude Code-style Agent tool calls use capitalized names — both must route through the CDC paths.

### FileSync (`orchestrator/file_sync.py`)

Change-data-capture for operational-memory files so their contents survive ephemeral container restarts. Two complementary paths feed the same `kernel_files_sync` postgres table:

**CDC path (primary).** Every `write`/`edit` tool call observed on the parent stream is parsed:
- `write`: the tool payload contains `file_path` + full `content`. If the path matches a tracked pattern, `db.sync_file(path, content, synced_from="cdc")` is called.
- `edit`: the tool payload contains `file_path` + `old_string` + `new_string`. Since the full new content is not in the event, the diff is applied server-side against the cached `kernel_files_sync` row, and the result is written back. If the cached row is missing (`CDC_MISSING_BASE`) or the `old_string` is absent from cached content (`CDC_DIVERGENCE`), the divergence is logged to `orch_activity_log` and no write happens — the snapshot path will reconcile on next trigger.

**Tracked paths:**
- `.claude/kernel/journal/**/*.md` (WISDOM.md, daily entries)
- `.claude/kernel/nodes/**/*.md` (node specs)

**Snapshot path (reconciliation backstop).** A scheduled `/sync-snapshot` trigger — see KernelScheduler — asks the Kernel to emit a canonical snapshot of its operational-memory files using the SYNC fence protocol. The orchestrator parses `agent.message` events for fenced blocks of the form:

```
```SYNC path=.claude/kernel/journal/WISDOM.md
<verbatim file contents>
```
```

Each fenced block whose path is tracked is written to `kernel_files_sync` with `synced_from="snapshot"`. This reconciles anything CDC missed — most commonly, writes done via `bash` (e.g., `cat > file`) or Edit divergences.

**Hydration on bootstrap.** Fresh sessions pull WISDOM and recent journal entries from `kernel_files_sync` via `SessionManager._build_hydration_instructions`, which injects them into the bootstrap prompt as "here is your state from the last session."

### DispatchManager (`orchestrator/dispatch.py`)

The cloud fork's answer to the missing Agent tool. Translates `DISPATCH` fences in Kernel messages into Managed Agent sub-sessions.

**DISPATCH protocol (Kernel → orchestrator).** The Kernel emits:

```
```DISPATCH node=<node_name>
{
  "task": "<task description>",
  "input": { ... },
  "budget_size": "S" | "M" | "L"
}
```
```

**DISPATCH_RESULT protocol (orchestrator → Kernel).** After the sub-session reaches idle, the orchestrator sends a `user.message` to the parent containing:

```
```DISPATCH_RESULT node=<node_name> status=<complete|failed>
{
  "output": "<full text of sub-agent's response>",
  "tokens": { "input": N, "output": N },
  "cost_usd": N,
  "duration_ms": N,
  "sub_session_id": "<id>",
  "error": null | "<error description>"
}
```
```

Both protocols are taught to the Kernel via the `BOOTSTRAP_PROMPT` for new sessions and via `send_protocol_refresh` for resumed sessions. Both constants live in `session_manager.py` (`SYNC_SNAPSHOT_PROTOCOL`, `DISPATCH_PROTOCOL`) and are the authoritative source — the orchestrator's parsers must stay in lockstep with them.

**Dispatch pipeline.** For each fence parsed from a message:

1. `_ensure_agent(node_name)` — look up `dispatch_agents` by node name. If the cached row's `prompt_hash` matches the current SHA256 of the node spec file on disk, reuse the cached `agent_id`. Otherwise, call `client.beta.agents.create` with the full spec file as the system prompt, persist the new row, return the new `agent_id`. **Stale agents are not deleted** — they are simply unreferenced and accrue no cost once they have no sessions.

2. `_run_sub_session(parent_session_id, agent_id, node_name, input_data)`:
   - `client.beta.sessions.create(agent=agent_id, environment_id=shared_env)` — a new session against the **shared environment** (no per-dispatch container provisioning).
   - `db.record_dispatch_start(sub_session_id, parent_session_id, node_name, input_data)` — row in `dispatch_sessions` with `status='running'`.
   - `client.beta.sessions.events.send(sub_session_id, [user.message])` — the task payload is sent as JSON in a user message.
   - `client.beta.sessions.events.stream(sub_session_id, timeout=httpx.Timeout(read=stream_read_timeout_seconds, ...))` — the stream is iterated with a read-timeout watchdog so a quiet stream never wedges the orchestrator forever.
   - Collect `span.model_request_end` tokens, `agent.message` text, watch for `session.status_idle` (success) or `session.status_terminated` (failure).
   - A wall-clock `max_dispatch_seconds` ceiling (default 600s) is checked on every event receipt as a secondary safety net.
   - On idle: `db.record_dispatch_complete` with tokens/cost/duration/output, return success dict.
   - On terminate/timeout/stall: `db.record_dispatch_failure`, return failure dict with error string.

3. `_format_result_fence(result)` — render the result dict as a `DISPATCH_RESULT` fenced block.

4. `send_to_parent(parent_session_id, fence)` — inject the result into the parent session as a new `user.message` via the injected callback (typically `SessionManager.send_message`).

Per-dispatch exceptions are caught at the `handle_message` level and converted to FAILED results, so one bad dispatch never prevents subsequent ones from running.

**Serial dispatch.** MVP runs dispatches one at a time. While a sub-session is streaming, the parent event loop is blocked — events on the parent stream buffer server-side and are delivered when the dispatch returns. A Quad (Domain + Task + two verifiers) takes roughly `4 × (setup + model_time)` seconds sequentially. Parallel dispatch via a thread pool is a well-understood future upgrade; the fence protocol does not change.

**Cost model.** Sub-sessions produce their own span events, tracked in `dispatch_sessions` (not `otel_cost_tracking`, which is parent-session only). A "total cost of a task" query must sum both.

**Observed performance (2026-04-10 live smoke test):**
- Agent create: ~0.3s
- Session create: ~0.4s
- Trivial roundtrip (smoke_test_node: 3 in / 73 out): ~7.3s end-to-end
- Realistic dispatch (~3000 in / 500 out): ~$0.0275 per call
- Full Quad (4 nodes sequential): ~18–30s, ~$0.11 per task

### StdinHitlHandler (`orchestrator/hitl.py`)

Stdin-based human-in-the-loop approval handler. When `EventConsumer._handle_tool_use` sees a `tool_confirmation` event, it invokes the injected `on_hitl_needed` callback. `StdinHitlHandler.handle` prints the proposed tool call, reads `y`/`n` + optional reason from stdin, and calls `SessionManager.send_tool_confirmation` to return the decision to the Kernel.

The handler is intentionally blocking and isolated in its own module so the Phase 2 dashboard integration can swap it for a WebSocket handler without touching `EventConsumer`.

### KernelScheduler (`orchestrator/scheduler.py`)

APScheduler-based trigger dispatcher. Replaces the cron scripts from base ora-kernel (which wrote to `inbox.jsonl`). Registered jobs:

| Trigger | Schedule | Purpose |
|---|---|---|
| `/heartbeat` | Every 2h, weekdays 8–17 | Silent anomaly check |
| `/briefing` | Daily 08:00 | Morning status summary |
| `/idle-work` | 20:00, 00:00, 04:00 | Off-hours autonomous research |
| `/consolidate` | Weekly Sunday 03:00 | Journal → WISDOM promotion |
| `/sync-snapshot` | Every 6h | File-sync reconciliation |

The `/sync-snapshot` trigger message carries the full SYNC protocol inline — this is how resumed sessions that were bootstrapped before the protocol existed still comply. The protocol constant is imported from `session_manager.SYNC_SNAPSHOT_PROTOCOL` so scheduler and parser cannot drift.

## PostgreSQL Schema

New and cloud-specific tables layered on top of the base ora-kernel schema:

### `cloud_sessions`
```sql
CREATE TABLE cloud_sessions (
    id                  BIGSERIAL PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    environment_id      TEXT NOT NULL,
    session_id          TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'created',
    container_start     TIMESTAMPTZ,
    total_input_tokens  BIGINT DEFAULT 0,
    total_output_tokens BIGINT DEFAULT 0,
    total_cost_usd      NUMERIC(10,4) DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    last_event_at       TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ
);
```
One row per parent Managed Agent session. Migration: `007_cloud_sessions.sql`.

### `kernel_files_sync`
```sql
CREATE TABLE kernel_files_sync (
    file_path     TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    synced_from   TEXT NOT NULL DEFAULT 'container'
);
```
CDC + snapshot state for WISDOM / journal / node specs. `synced_from` tags the last write path (`cdc` vs `snapshot`). **Note**: this table stores current state only — a write-then-snapshot sequence overwrites the CDC row. If an audit trail is ever needed, a separate append-only history table would have to be added.

### `dispatch_agents`
```sql
CREATE TABLE dispatch_agents (
    node_name     TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
```
Per-node agent cache. `prompt_hash` is SHA256 of the node spec file — when the spec changes, the orchestrator creates a fresh agent rather than reusing a stale one. Migration: `008_dispatch.sql`.

### `dispatch_sessions`
```sql
CREATE TABLE dispatch_sessions (
    sub_session_id    TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    node_name         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running',
    input_data        JSONB NOT NULL DEFAULT '{}',
    output_data       JSONB,
    input_tokens      BIGINT DEFAULT 0,
    output_tokens     BIGINT DEFAULT 0,
    cost_usd          NUMERIC(10,6) DEFAULT 0,
    duration_ms       INTEGER,
    error             TEXT,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX idx_dispatch_sessions_parent ON dispatch_sessions(parent_session_id);
CREATE INDEX idx_dispatch_sessions_status ON dispatch_sessions(status);
```
One row per dispatch lifecycle. Linked to its parent Kernel session via `parent_session_id`. Migration: `008_dispatch.sql`.

## Architectural Invariants

These are contracts that any future change must respect.

**INVARIANT 1: The container never speaks directly to PostgreSQL.** All persistence flows through the orchestrator by way of the SSE event stream. The bootstrap prompt explicitly tells the Kernel this. The motivation is correctness (single writer), observability (Axiom 1 — every state change is visible in the event stream), and security (no credentials in the container).

**INVARIANT 2: Kernel-side protocol teaching goes through orchestrator-owned files.** `kernel-files/CLAUDE.md` is protected by `protect_core.py` and cannot be edited. New protocols (SYNC, DISPATCH, and any future ones) are defined as constants in `session_manager.py` and injected via `BOOTSTRAP_PROMPT` for fresh sessions and `send_protocol_refresh` for resumed sessions.

**INVARIANT 3: The dispatch subsystem is case-insensitive on tool names.** The Anthropic Managed Agent toolset emits lowercase tool names (`write`, `edit`); Claude Code-style calls use capitalized names. Both must route through CDC, HITL, and any future tool-use paths.

**INVARIANT 4: No agent ever self-certifies work in the cloud.** Even with the dispatch subsystem, the orchestrator never routes a task node and its verifier to the same sub-session. The Kernel is responsible for dispatching the verifier as a SEPARATE DISPATCH fence. This is how Axiom 2 is preserved despite the missing Agent tool.

## Event Types

### SSE events consumed on every session (parent + sub-sessions)

| Event | Fields we care about |
|---|---|
| `user.message` | (echo — usually just confirms our send) |
| `session.status_running` | — |
| `session.status_idle` | `stop_reason` |
| `session.status_terminated` | `error` |
| `span.model_request_start` | — |
| `span.model_request_end` | `model`, `model_usage.{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}` |
| `agent.message` | `content[*].text` — scanned for SYNC + DISPATCH fences |
| `agent.tool_use` | `name`, `input` — routed for CDC + HITL |
| `agent.tool_result` | — |

### Events the orchestrator sends to parent sessions

| Event | Purpose |
|---|---|
| `user.message` | Task dispatch, cron triggers (`/heartbeat`, `/briefing`, `/idle-work`, `/consolidate`, `/sync-snapshot`), protocol refresh, DISPATCH_RESULT forwarding |
| `user.tool_confirmation` | HITL response (approve/deny with reason) |
| `user.interrupt` | Emergency stop |

### Events the orchestrator sends to sub-sessions

| Event | Purpose |
|---|---|
| `user.message` | The task payload (serialized JSON from the DISPATCH fence body) |

## Implementation Status

| Component | Status | Notes |
|---|---|---|
| Agent + environment lifecycle | **Done** | `orchestrator/agent_manager.py` |
| Session bootstrap + resume | **Done** | `orchestrator/session_manager.py` |
| Protocol refresh on resume | **Done** | `SessionManager.send_protocol_refresh` |
| SSE event consumer | **Done** | Full-text logging + case-insensitive tool routing |
| HITL — stdin | **Done** | `StdinHitlHandler`, swappable for dashboard |
| Scheduler (5 triggers) | **Done** | Including `/sync-snapshot` |
| File sync — Write CDC | **Done** | `FileSync.handle_write` |
| File sync — Edit CDC with diff apply | **Done** | `FileSync.handle_edit` with divergence logging |
| File sync — SYNC fence snapshot | **Done** | `FileSync.handle_snapshot_response` |
| Dispatch subsystem — protocol | **Done** | `DISPATCH_PROTOCOL` constant |
| Dispatch subsystem — fence parser | **Done** | `parse_dispatch_fences` |
| Dispatch subsystem — agent cache | **Done** | `DispatchManager._ensure_agent` with hash invalidation |
| Dispatch subsystem — sub-session lifecycle | **Done** | `DispatchManager._run_sub_session` with stall watchdog |
| Dispatch subsystem — top-level routing | **Done** | `DispatchManager.handle_message` + result formatting |
| Dispatch subsystem — live smoke test | **Done** | Two round-trips verified 2026-04-10 |
| Dashboard — WebSocket bridge | **Pending** | Task 21 (plan) / 22 (execute) |
| Dashboard — Cytoscape task graph | **Pending** | Will surface `dispatch_sessions` rows |
| Dashboard — cost panel | **Pending** | Must sum parent + sub-session costs |
| Dispatch parallel execution | **Backlog** | Thread-pool upgrade, protocol unchanged |
| Dispatch idempotency on restart | **Backlog** | Reconcile `running` rows in `dispatch_sessions` at startup |
| Cost rollup across parent + sub-sessions | **Backlog** | Single "what did this task cost me?" query |

## Cost Model

Per-action cost envelope (Opus 4.6, April 2026 rates: $5/M input, $25/M output):

| Component | Typical cost |
|---|---|
| Parent container runtime | $0.05/hr (50 free hrs/day) |
| Parent session (idle) | $0 |
| Parent session tokens (active) | Input-dominated, varies with conversation length |
| Dispatch — trivial (smoke_test_node) | ~$0.002 (3 in / 73 out) |
| Dispatch — realistic node (~3k in / 500 out) | ~$0.0275 per call |
| Full Quad (4 dispatches) | ~$0.11 per task |
| `/heartbeat` (12/day) | ~$0.05/day |
| `/briefing` (1/day) | ~$0.15/day |
| `/idle-work` (2–3/night) | ~$1–3/day |
| `/sync-snapshot` (4/day, protocol inline ≈ 250 tokens) | ~$0.01/day |
| `/consolidate` (weekly) | ~$0.50/week |
| Self-improvement cycle (if dispatched) | ~$2–5/week |
| **Estimated monthly — light use** | **$30–80** |
| **Estimated monthly — heavy use with Quads** | **$100–250** |

## Cost Observability

- **Parent session tokens** → `otel_token_usage`, `otel_cost_tracking` (via `span.model_request_end` on the parent stream).
- **Sub-session tokens** → `dispatch_sessions.input_tokens`, `dispatch_sessions.output_tokens`, `dispatch_sessions.cost_usd` (per dispatch).
- **Running totals per parent session** → `cloud_sessions.total_cost_usd` (updated on parent events; **does not include sub-session costs** — sum `dispatch_sessions` by `parent_session_id` for that).

## Security Notes

See `SECURITY.md` for the full threat model. Cloud-specific concerns:

- **API key handling** — keep `ANTHROPIC_API_KEY` in `.env`, gitignored. Rotate immediately if exposed in logs or transcripts.
- **Orphaned sub-sessions** — if the orchestrator crashes during a dispatch, the sub-session continues running on Anthropic's side until its own idle timeout. Manual cleanup via `client.beta.sessions.retrieve` + `interrupt` is possible.
- **Per-node agent growth** — spec edits accumulate agents in `dispatch_agents` over time. Unreferenced agents cost nothing but clutter the list — a periodic cleanup sweep is a future nicety.
- **Dispatch cost caps** — the subsystem has no built-in per-dispatch token ceiling. A runaway node could burn substantial tokens before `max_dispatch_seconds` fires at 600s. A budget-enforcing wrapper is an open item.

## What Stays Identical to Base ora-kernel

- Constitution (9 axioms) — although Axiom 2 and Axiom 9 are enforced by the dispatch subsystem rather than a built-in Agent tool.
- Node spec format (YAML frontmatter + `## System Prompt` section + behavioral contracts).
- PostgreSQL base schema (`orch_tasks`, `orch_activity_log`, `otel_*`, etc.).
- The WISDOM / journal operational-memory model.
- The 9 axioms document itself (`kernel-files/.claude/kernel/references/constitution.md`).

## What Is Fundamentally Different from Base ora-kernel

| Aspect | Base ora-kernel | ora-kernel-cloud |
|---|---|---|
| Kernel host | Claude Code TUI with `/kernel-listen` | Anthropic Managed Agent (always-on) |
| Event input | `inbox.jsonl` file writes | `events.send()` API calls from scheduler + `--send` |
| Event output | `pending_briefing.md` | SSE stream → postgres (+ future dashboard) |
| Cron triggers | `.claude/cron/*.sh` shell scripts via crontab | APScheduler in the orchestrator daemon |
| Subagent dispatch | Agent tool (Claude Code) | `DISPATCH` fence → orchestrator → Managed Agent sub-session |
| File persistence | Local filesystem | CDC + snapshot to `kernel_files_sync` |
| HITL | TUI text prompts | Orchestrator stdin (MVP), dashboard WS (Phase 2) |
| Billing | Claude Code subscription | API tokens + container hours |
| kernel-files/CLAUDE.md authority | Fully authoritative | Authoritative for constitution + reasoning, **obsolete** for dispatch (overridden by `DISPATCH_PROTOCOL` in bootstrap) |

## References

- `docs/specs/SPEC-001-managed-agent-cloud-fork.md` — original implementation spec (updated with dispatch subsystem)
- `docs/superpowers/plans/2026-04-10-visibility-hitl-filesync.md` — the executed plan for the visibility / HITL / file-sync track
- `docs/superpowers/plans/2026-04-10-dispatch-subsystem.md` — the executed plan for the dispatch subsystem
- `spikes/subagent_feasibility.py` — the feasibility spike that validated sub-session dispatch latency and cost
- `spikes/check_sub_session.py` — diagnostic helper for stuck sub-sessions
- `kernel-files/infrastructure/db/007_cloud_sessions.sql` — cloud session + file sync tables
- `kernel-files/infrastructure/db/008_dispatch.sql` — dispatch subsystem tables
