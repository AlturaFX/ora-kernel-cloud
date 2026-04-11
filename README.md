# ORA Kernel Cloud

An always-on agentic orchestration system for software engineering work. The ORA Kernel runs as an Anthropic **Managed Agent** in the cloud; a thin Python orchestrator on your machine owns the event stream, runs approvals, persists state to PostgreSQL, and — since the Managed Agent toolset has no built-in subagent dispatch — brokers delegation to focused sub-agent sessions on your behalf.

This is a **separate fork** of [ora-kernel](https://github.com/AlturaFX/ora-kernel). The base ora-kernel runs inside a Claude Code TUI and uses Claude Code's `Agent` tool to dispatch subagents. ora-kernel-cloud runs in Anthropic's cloud, has no Agent tool, and has a fundamentally different runtime. If you want the TUI-based version, use base ora-kernel.

## Why a Cloud Fork?

The base ora-kernel needs a Claude Code TUI to be open. The Kernel dies when you close the terminal. Autonomous features (heartbeat, morning briefing, overnight research) only run while the TUI is active. For real "business-partner-that-works-while-you-sleep" behavior, the Kernel needs to live somewhere else.

**Managed Agents solve that** — an Anthropic-hosted, always-on cloud session that accepts events via API and emits events via SSE. But they come with one big catch: the toolset (`agent_toolset_20260401`) gives the agent `bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_search`, `web_fetch` — **and nothing else**. There is no Agent/Task/dispatch primitive. The base ora-kernel's entire orchestration model assumes the Agent tool exists.

ora-kernel-cloud reconstructs delegation at the orchestrator layer. The Kernel signals dispatch intent by emitting structured `` ```DISPATCH `` fenced blocks in its responses. The orchestrator parses them, spins up a focused Managed Agent sub-session per dispatch (with the node's spec as the sub-agent's system prompt), consumes its event stream, and forwards the result back to the parent session. Each "subagent" is a first-class cloud session. Axioms 2 (objective verification) and 9 (separation of concerns) are preserved because verifiers really are separate agents.

See `docs/CLOUD_ARCHITECTURE.md` for the full architectural writeup — that document is the source of truth.

## Status

| Component | State |
|---|---|
| Parent session lifecycle (create/resume/restart) | **Done** |
| SSE event consumer (tokens, costs, activity log) | **Done** |
| Stdin HITL handler | **Done** |
| Scheduler (`/heartbeat`, `/briefing`, `/idle-work`, `/consolidate`, `/sync-snapshot`) | **Done** |
| File sync (Write/Edit CDC + SYNC fence snapshot reconciliation) | **Done** |
| Dispatch subsystem (DISPATCH fence parser, per-node agent cache, sub-session lifecycle, result forwarding) | **Done** |
| Protocol refresh on session resume (no drift between orchestrator and Kernel) | **Done** |
| Live smoke-tested end-to-end | **Done** (2026-04-10) |
| Dashboard integration (WebSocket bridge, Cytoscape task graph, HITL widget swap) | **Pending** |
| Parallel dispatch via thread pool | **Backlog** |
| Dispatch idempotency on orchestrator restart | **Backlog** |
| Cost rollup across parent + sub-sessions | **Backlog** |

## Quick Start

### Prerequisites

- Python 3.10+
- PostgreSQL 14+ with a database named `ora_kernel` (Unix socket or TCP, both work)
- An Anthropic API key with Managed Agents beta access
- The base [ora-kernel](https://github.com/AlturaFX/ora-kernel) repo cloned somewhere the container can reach (typically just `github.com/AlturaFX/ora-kernel.git` — the bootstrap clones it inside the container)

### Setup

```bash
# 1. Clone this repo
git clone https://github.com/AlturaFX/ora-kernel-cloud.git
cd ora-kernel-cloud

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-your-key-here
POSTGRES_DSN=postgresql:///ora_kernel
EOF
chmod 600 .env

# 4. Apply the database schema
createdb ora_kernel
for f in kernel-files/infrastructure/db/*.sql; do
  psql -d ora_kernel -f "$f"
done

# 5. Launch the orchestrator
python3 -m orchestrator
```

On first run the orchestrator will create the Managed Agent, create a shared environment, create a session, and send the bootstrap prompt. On subsequent runs it resumes the existing session (IDs are persisted in `.ora-kernel-cloud.json`).

### Sending Tasks

```bash
# Send an ad-hoc message to the active session
python3 -m orchestrator --send "Please summarize recent activity"

# Trigger a specific scheduled job manually (just send the command as a message)
python3 -m orchestrator --send "/briefing"
python3 -m orchestrator --send "/sync-snapshot"
```

The orchestrator must be running (or restarted) to consume the response — send commands land as `user.message` events on the parent session and are processed on the next event loop iteration.

### Watching What It's Doing

All events land in PostgreSQL in real time. Until the dashboard integration ships (Task 21), querying `psql` directly is the authoritative way to watch:

```bash
# Recent messages (full text, no 200-char truncation)
psql -d ora_kernel -c "SELECT id, substring(details->>'text' from 1 for 300) FROM orch_activity_log WHERE action='MESSAGE' ORDER BY id DESC LIMIT 5;"

# Recent dispatches with costs
psql -d ora_kernel -c "SELECT node_name, status, input_tokens, output_tokens, cost_usd, duration_ms FROM dispatch_sessions ORDER BY started_at DESC LIMIT 10;"

# File sync state (WISDOM.md + journal)
psql -d ora_kernel -c "SELECT file_path, synced_from, length(content), updated_at FROM kernel_files_sync ORDER BY updated_at DESC;"

# Running cost for the active parent session
psql -d ora_kernel -c "SELECT status, total_input_tokens, total_output_tokens, total_cost_usd FROM cloud_sessions ORDER BY created_at DESC LIMIT 1;"

# Total cost of a task including sub-sessions (parent + dispatches)
psql -d ora_kernel -c "
SELECT
  (SELECT total_cost_usd FROM cloud_sessions WHERE session_id = 'sesn_xxx') AS parent_cost,
  (SELECT COALESCE(SUM(cost_usd), 0) FROM dispatch_sessions WHERE parent_session_id = 'sesn_xxx') AS dispatch_cost;
"
```

## The 9 Axioms

Unchanged from base ora-kernel — these are the immutable constitution the Kernel operates under:

1. **Observable State** — record and broadcast every state change
2. **Objective Verification** — no self-certification; separate verifier for every work product
3. **Finite Resources** — every task has a budget; escalate when exceeded
4. **Immutable Core** — constitution, schemas, hooks cannot be modified by agents
5. **Entropy** — never retry a failed approach blindly; analyze root cause first
6. **Isolation** — every task starts clean; no hidden dependencies
7. **Purpose** — every task must advance the mission in PROJECT_DNA.md
8. **First Principles** — decompose complex/failed tasks to fundamentals before acting
9. **Separation of Concerns** — no single agent plans AND executes AND verifies

In ora-kernel-cloud, **Axioms 2 and 9 are enforced by the dispatch subsystem** — the orchestrator never routes a task node and its verifier to the same sub-session. The Kernel is responsible for dispatching the verifier as a separate `DISPATCH` fence.

## Repository Layout

```
ora-kernel-cloud/
├── orchestrator/                        # The thin daemon
│   ├── __main__.py                      # Entry: `python3 -m orchestrator`
│   ├── agent_manager.py                 # Agent + environment CRUD
│   ├── session_manager.py               # Session lifecycle + BOOTSTRAP_PROMPT +
│   │                                    #   SYNC_SNAPSHOT_PROTOCOL + DISPATCH_PROTOCOL
│   ├── event_consumer.py                # SSE loop + event routing
│   ├── file_sync.py                     # CDC + snapshot reconciliation
│   ├── dispatch.py                      # DISPATCH fence -> sub-session broker
│   ├── hitl.py                          # Stdin-based HITL handler
│   ├── scheduler.py                     # APScheduler (5 triggers)
│   ├── db.py                            # psycopg2 wrapper
│   ├── config.py                        # .env + config.yaml loader
│   └── tests/                           # 72 unit + integration tests
├── kernel-files/                        # What the Kernel clones into /work
│   ├── CLAUDE.md                        # Kernel constitution (protected)
│   ├── PROJECT_DNA.md                   # Mission config (protected)
│   ├── .claude/
│   │   ├── kernel/
│   │   │   ├── nodes/system/            # Bootstrap node specs + smoke_test_node
│   │   │   ├── references/constitution.md
│   │   │   └── schemas/
│   │   └── hooks/                       # safety_check, protect_core, etc.
│   └── infrastructure/db/               # SQL migrations 001-008
├── docs/
│   ├── CLOUD_ARCHITECTURE.md            # Source of architectural truth
│   ├── API_KEY_SETUP.md                 # Credential handling
│   ├── next_steps.md                    # Current backlog
│   ├── specs/SPEC-001-managed-agent-cloud-fork.md
│   └── superpowers/plans/               # Executed implementation plans
├── spikes/
│   ├── subagent_feasibility.py          # Validated Option 3 dispatch model
│   └── check_sub_session.py             # Diagnostic helper for stuck sub-sessions
├── config.yaml                          # Scheduler + postgres defaults
├── requirements.txt
└── .env                                 # gitignored — your API key goes here
```

## Testing

```bash
# Full unit + integration suite (72 tests, requires local postgres for the
# db_dispatch integration tests — they auto-skip if postgres is unreachable)
python3 -m pytest orchestrator/tests/

# Dispatch subsystem only
python3 -m pytest orchestrator/tests/test_dispatch.py -v

# Live dispatch smoke test (requires running orchestrator and API access)
python3 -m orchestrator --send "Dispatch the smoke_test_node with any task payload."
```

## Cost

Rough envelope at April 2026 Opus-4.6 rates ($5/M input, $25/M output) with a mixed workload:

| Usage level | Monthly cost |
|---|---|
| Light (few /heartbeat days, occasional /briefing) | $30–80 |
| Heavy with full dispatch Quads on real tasks | $100–250 |

See `docs/API_KEY_SETUP.md` for the breakdown and `docs/CLOUD_ARCHITECTURE.md` § Cost Model for per-action numbers from the 2026-04-10 spike.

## Contributing

This fork is active development. See `CONTRIBUTING.md` (which now has a cloud-specific section) for how to test and submit changes. The authoritative planning documents are in `docs/superpowers/plans/`.

## License

Business Source License 1.1 — same as base ora-kernel. Free for personal, educational, research, and internal business use. Converts to Apache 2.0 on 2030-04-06. See `LICENSE`.

## References

- Base ora-kernel: https://github.com/AlturaFX/ora-kernel
- Architecture: `docs/CLOUD_ARCHITECTURE.md`
- Constitution: `kernel-files/.claude/kernel/references/constitution.md`
- Anthropic Managed Agents docs: (see your account's beta access docs)
