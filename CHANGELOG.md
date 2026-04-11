# Changelog

## [2.0.0-cloud.1] - 2026-04-10

First cloud-fork release. Runs the ORA Kernel as an Anthropic Managed
Agent with a thin local orchestrator that brokers dispatch, HITL, file
sync, and schedulers. Architecture is fundamentally different from
base ora-kernel — see `docs/CLOUD_ARCHITECTURE.md`.

### Added — Orchestrator foundation

- Thin Python orchestrator daemon (`orchestrator/`):
  - `agent_manager.py` — Managed Agent + environment CRUD, ID caching in `.ora-kernel-cloud.json`
  - `session_manager.py` — session lifecycle, bootstrap prompt, resume handling, restart-on-terminate with exponential backoff
  - `event_consumer.py` — blocking SSE event loop with per-event-type handlers; writes to `orch_activity_log`, `otel_token_usage`, `otel_cost_tracking`, `cloud_sessions`
  - `scheduler.py` — APScheduler replacing base ora-kernel's cron scripts; sends `/heartbeat`, `/briefing`, `/idle-work`, `/consolidate` as `user.message` events to the managed session
  - `db.py` — psycopg2 wrapper
  - `config.py` — `.env` + `config.yaml` loader
  - `__main__.py` — single entry point: `python3 -m orchestrator`
- Cloud-session tracking table `cloud_sessions` (migration `007_cloud_sessions.sql`)
- Cost tracking: running totals per parent session, Opus 4.6 rate table ($5/M input, $25/M output)

### Added — Visibility & HITL (Track 1)

- Full-text message logging — `TEXT_PREVIEW_LEN` raised from 200 to 10,000 and `INPUT_PREVIEW_LEN` from 500 to 2,000 so `orch_activity_log` captures complete agent messages and tool inputs without truncation
- `StdinHitlHandler` (`orchestrator/hitl.py`) — stdin-based approval prompt for `tool_confirmation` events, designed as a drop-in replacement for a future WebSocket handler; wired via `EventConsumer.on_hitl_needed` callback
- Pytest added as dev dependency; first unit tests for HITL handler (4 tests)

### Added — File sync (Track 1)

- Change-data-capture mirroring of operational-memory files to postgres (`kernel_files_sync` table, from migration `007`):
  - `FileSync.handle_write` — tracked Write tool calls capture the full file content from the event payload
  - `FileSync.handle_edit` — tracked Edit tool calls apply the diff server-side against cached content; divergences and missing-base cases are logged as observable `orch_activity_log` rows (`CDC_DIVERGENCE`, `CDC_MISSING_BASE`)
- Tracked paths: `.claude/kernel/journal/**/*.md` (WISDOM + daily entries), `.claude/kernel/nodes/**/*.md`
- **SYNC fence protocol** — `SYNC_SNAPSHOT_PROTOCOL` constant in `session_manager.py`, taught to the Kernel via `BOOTSTRAP_PROMPT`. The Kernel emits `` ```SYNC path=... ``` `` fenced blocks in response to `/sync-snapshot` triggers; `FileSync.handle_snapshot_response` parses and reconciles them.
- `/sync-snapshot` scheduler job (every 6h by default) as reconciliation backstop for CDC. The trigger message carries the full SYNC protocol inline so resumed sessions (bootstrapped before the protocol existed) still comply.
- Bootstrap hydration — `_build_hydration_instructions` injects the latest `kernel_files_sync` rows into the bootstrap prompt, so fresh containers start with their previous operational memory
- 30 unit tests covering path normalization, tracking, fence parsing, Write CDC, Edit diff-apply, snapshot reconciliation

### Added — Dispatch subsystem (Track 2, the architectural centerpiece)

The Managed Agent toolset `agent_toolset_20260401` has no Agent/Task/dispatch primitive — the base ORA Kernel's entire subagent model cannot execute in the cloud environment. Discovered live on 2026-04-10 by asking the Kernel directly what tools it had. See `docs/CLOUD_ARCHITECTURE.md` § The Core Constraint for the full implications.

- **Feasibility spike** (`spikes/subagent_feasibility.py`) validated that creating a second Managed Agent session from the orchestrator is fast (~0.3s agent.create, ~0.4s session.create, ~4.8s trivial roundtrip) and that per-agent custom system prompts enforce node identity
- **`DispatchManager`** (`orchestrator/dispatch.py`) — translates `` ```DISPATCH `` fences in Kernel messages into Managed Agent sub-sessions and forwards results back:
  - `parse_dispatch_fences` — regex-based parser; skips malformed fences (missing node attribute, invalid JSON, non-object payload) with warning logs
  - `_load_node_spec` / `_spec_hash` — SHA256 content-addressed node spec loader
  - `_ensure_agent` — per-node agent cache with hash invalidation: spec edits automatically trigger a fresh `client.beta.agents.create` call; stale agents are unreferenced but not deleted
  - `_run_sub_session` — creates a sub-session against the shared environment, sends the task payload as a `user.message`, consumes the event stream, collects token counts / text / cost, records full lifecycle in `dispatch_sessions`, handles `session.status_terminated` as failure
  - `handle_message` — top-level entry point; processes all fences in a message, catches per-dispatch exceptions and converts to FAILED results so one bad dispatch never prevents later ones
  - `_format_result_fence` — renders results as `` ```DISPATCH_RESULT `` fenced blocks for the parent Kernel
- **Stall watchdog + wall-clock ceiling** — `httpx.Timeout(read=stream_read_timeout_seconds, ...)` passed to `stream()` catches `httpx.ReadTimeout` as a proper stream-stall detector; `max_dispatch_seconds=600` acts as wall-clock safety net
- **`DISPATCH_PROTOCOL` constant** in `session_manager.py` — taught to the Kernel via `BOOTSTRAP_PROMPT` for fresh sessions. Explicitly overrides the "use the Agent tool" instructions in the protected `CLAUDE.md`.
- **Dispatch tables** (migration `008_dispatch.sql`):
  - `dispatch_agents` — per-node cache with `prompt_hash` column for content-addressed invalidation
  - `dispatch_sessions` — one row per dispatch with full lifecycle (status, tokens, cost, duration, error, parent linkage)
- **`SessionManager.send_protocol_refresh`** — sends current `SYNC_SNAPSHOT_PROTOCOL` + `DISPATCH_PROTOCOL` as a single `user.message` on every orchestrator boot against a resumed session. Closes the drift window where long-lived sessions would otherwise be stuck with whatever protocol shipped at their creation time.
- **Shared-environment reuse** — sub-sessions are created against the same Managed Agent environment as the parent; no per-dispatch container provisioning cost
- **Serial dispatch (MVP)** — parent event loop blocks during a sub-session stream; parent events buffer server-side. Parallel dispatch via thread pool is a well-understood future upgrade that does not touch the fence protocol
- **`smoke_test_node`** (`kernel-files/.claude/kernel/nodes/system/smoke_test_node.md`) — minimal node spec for dispatch pipeline validation; its system prompt forbids tool use to keep roundtrips short
- 19 dispatch unit tests (fence parsing, spec loading, agent cache, sub-session lifecycle, top-level handle_message with failure paths)
- 6 postgres-integration tests for dispatch db helpers (auto-skip if postgres unreachable)

### Added — Documentation

- `docs/CLOUD_ARCHITECTURE.md` — comprehensive architectural writeup (source of truth)
- `docs/API_KEY_SETUP.md` — credential handling guide
- `docs/specs/SPEC-001-managed-agent-cloud-fork.md` — implementation spec
- `docs/superpowers/plans/2026-04-10-visibility-hitl-filesync.md` — executed plan for Track 1
- `docs/superpowers/plans/2026-04-10-dispatch-subsystem.md` — executed plan for the dispatch subsystem
- `docs/next_steps.md` — current backlog (replaces `next_steps.txt`)

### Fixed

- **Case-sensitive tool name matching** (`event_consumer.py`) — smoke test against a live Managed Agent session revealed the cloud toolset emits lowercase tool names (`write`, `edit`, `bash`) while the original CDC routing matched capitalized Claude Code-style names. Result: real Write events were logged but never mirrored. Fixed by lowercasing tool names before comparison; regression tests now default to lowercase with explicit capitalized variants to cover both cases.
- **`/sync-snapshot` drift on resumed sessions** — the original scheduler sent a bare `/sync-snapshot` string, and resumed sessions had no knowledge of the protocol. Fixed by extracting `SYNC_SNAPSHOT_PROTOCOL` as a constant, embedding it in `BOOTSTRAP_PROMPT` for fresh sessions AND inlining it in every `/sync-snapshot` trigger for resumed sessions. ~250 tokens per trigger is negligible.
- **Dispatch wall-clock timeout never firing on quiet streams** — the original 120s `max_dispatch_seconds` check ran only on event receipt. If the SSE stream went quiet, the for-loop blocked indefinitely regardless of elapsed time. Fixed by adding `stream_read_timeout_seconds` (default 180s) passed as `httpx.Timeout(read=...)` to the `stream()` call, catching `httpx.ReadTimeout` / `TimeoutException` as a proper stall watchdog. `max_dispatch_seconds` raised to 600s default as belt-and-braces.
- **Test fixture Unix-socket DSN resolution** — `test_db_dispatch.py` was hard-coding a TCP DSN that bypassed `.env` and silently skipped all tests in environments using peer authentication. Fixed by routing through `get_postgres_dsn(load_config())` so the fixture inherits the same DSN resolution as the production orchestrator.

### Changed

- **Architecture**: The cloud fork is not a subset of base ora-kernel — it is a parallel runtime. `kernel-files/CLAUDE.md` remains in place as the constitution and reasoning framework, but its Dispatch Protocol section is overridden at session context time by `DISPATCH_PROTOCOL` injected via `BOOTSTRAP_PROMPT` and `send_protocol_refresh`.
- **Event input**: base ora-kernel used `inbox.jsonl` file writes via `kernel-listen`; cloud fork uses `events.send()` API calls from the scheduler and `--send`.
- **Event output**: base wrote to `pending_briefing.md`; cloud streams events via SSE to the orchestrator which persists to postgres and (Phase 2) forwards to the dashboard.
- **File persistence**: base used the local filesystem; cloud uses CDC + snapshot to `kernel_files_sync`.
- **HITL**: base used TUI prompts; cloud uses stdin (MVP) with a WebSocket handler planned for Phase 2.

### Notes

- The orphaned sub-session from the D17 smoke test (`sesn_011CZwEgCWQMbqBEtfT9adku`, `business_analyst`) is still running on Anthropic's side and may accrue container time until its own idle timeout. Consider adding a startup reconciliation sweep in a future release (see `docs/next_steps.md`).
- `orch_tasks` is NOT written by the cloud Kernel (the `session_id` column has zero cloud-session rows). This is because task-lifecycle writes depend on dispatch events, and the dispatch subsystem is new. A follow-up task should wire `DispatchManager` to write `orch_tasks` rows so the base ora-kernel's task lifecycle is restored.

## [1.1.0] - 2026-04-07

### Added
- Proactive heartbeat monitor — cron-based anomaly detection (stuck tasks, budget exhaustion, failure spikes), silent when healthy (HEARTBEAT_OK pattern)
- Daily briefing — morning summary with completions, failures, pending work, and suggested priorities
- Context-aware suggestions — post-completion follow-up suggestions with feedback loop; learns which types are helpful over time via orch_suggestion_feedback table
- Anticipatory research (idle work) — autonomous execution of low-risk queued tasks during off-hours, gated by PROJECT_DNA.md autonomy_level
- Learning journal + wisdom consolidation — daily operational journal entries, scored promotion to WISDOM.md via "dreaming" consolidation cycle (frequency, impact, recency)
- 3 new node specs: JournalWriter, ConsolidationAnalyst + verifier
- 4 cron scripts: heartbeat.sh, daily_briefing.sh, idle_work.sh, consolidate.sh
- 006_suggestion_feedback.sql migration with v_suggestion_effectiveness view
- pending_briefing.md replaces outbox.jsonl as async results buffer

### Changed
- Renamed .claude/chat/ to .claude/events/ (event queue, not chat)
- Renamed chat-listen.md to kernel-listen.md
- Replaced forex-specific examples with generic ones in node_output.md and node_creator.md
- Docker-compose credentials use environment variable substitution
- CLAUDE.md now loads WISDOM.md and last 2 days of journal entries for context
- README restructured with "Why ORA?", terminology glossary, and proactive features section

### Fixed
- loop_detector.py: broken newline escapes in stderr noise filter caused false positives on every command with cwd-reset notice
- protect_core.py: Python 3.10+ type syntax (str | None) replaced with Optional[str] for 3.8+ compatibility
- session_init.sh: bulk directory removal replaced with safer per-file deletion and path validation
- consolidate.sh: missing journal directory now produces a warning instead of silently exiting
- install.py: kernel/ directory copy now preserves journal entries and WISDOM.md on re-install

## [1.0.0] - 2026-04-06

### Added
- 9-axiom Constitution with full definitions
- CLAUDE.md kernel operating instructions with behavioral contracts
- PROJECT_DNA.md template (5 interview questions)
- 6 hook scripts: safety_check, protect_core, loop_detector, subagent_lifecycle, anti_poll, session_init
- 10 node specs: NodeDesigner, NodeCreator, BusinessAnalyst (+ verifiers), TuningAnalyst, RefinementAnalyst (+ verifiers)
- 3 schema files: node_output, node_spec, split_spec
- 3 reference docs: constitution, kernel_priorities, node_quad_example
- 2 commands: kernel-listen (kernel event loop), self-improve (manual trigger)
- PostgreSQL schema: 5 migration files (10 tables, 2 views)
- OpenTelemetry pipeline: docker-compose + collector config
- agents.yaml node/command registry
- install.py integration script with:
  - Clean install into new projects
  - Non-destructive merge into existing projects (CLAUDE.md, settings.json, agents.yaml)
  - Section markers for idempotent updates
  - kernel-listen collision handling
  - --dry-run and --force modes
- INTEGRATION.md LLM-guided merge instructions
