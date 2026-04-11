# Next Steps

Living document of the current backlog. Updated as work lands.

**Last updated:** 2026-04-10

For the pre-session-2 state of this document — what was missing before visibility/HITL/file-sync and the dispatch subsystem were built — see `docs/history/2026-04-08-pre-session-state.txt`.

---

## Where We Are

As of 2026-04-10, ora-kernel-cloud is **operationally useful for single-session work**. The orchestrator runs, the parent Managed Agent session is persistent, schedulers fire, visibility into the activity log is complete, HITL works from stdin, file sync reconciles WISDOM/journal state across container restarts, and the dispatch subsystem translates Kernel `DISPATCH` fences into focused sub-agent sessions. All 72 unit + integration tests pass. The dispatch pipeline has been smoke-tested live end-to-end with two successful round-trips.

See `docs/CLOUD_ARCHITECTURE.md` for the current architectural state and `CHANGELOG.md` § 2.0.0-cloud.1 for the full list of what shipped.

---

## Immediate Priorities

### 1. Dashboard integration (Tasks 21 + 22)

**Status:** Planned. Task 21 is to write the formal plan via the `superpowers:writing-plans` skill; Task 22 is execution. Task 22 is blocked by Task 21.

**Why it matters:** The orchestrator is the data plane; the dashboard is the operator's window into it. Until the dashboard integration lands, the only way to watch what the Kernel is doing is `psql` queries. The dispatch subsystem gives the dashboard real data to surface — every sub-session is a row in `dispatch_sessions` with parent linkage, status, tokens, cost, duration.

**What the dashboard needs to surface:**
- **Live task graph** — Cytoscape nodes from `dispatch_sessions`, edges by `parent_session_id`, colors by status. Running sub-sessions animate. The "live task graph" vision from the original CLOUD_ARCHITECTURE.md is now backed by real data.
- **Agent health panel** — `cloud_sessions` row for the parent (status, uptime, running totals). Updates on every SSE event.
- **Cost panel** — parent session cost + sum of `dispatch_sessions.cost_usd`. Hourly burn rate, daily/monthly projection.
- **Event stream** — scrolling log of recent `orch_activity_log` entries, filterable by action. Replaces the current psql-query workflow.
- **HITL widget** — extends the existing forex-ml dashboard HITL widget to consume `tool_confirmation` events forwarded by the orchestrator. Replaces `StdinHitlHandler` as the frontline handler; stdin becomes the fallback.
- **DISPATCH fence visualization** — when an `agent.message` contains a DISPATCH fence, highlight it and show a spinner until the matching `DISPATCH_RESULT` arrives.
- **File sync status** — table of `kernel_files_sync` rows with `synced_from` column and last-updated timestamp, so the operator can see CDC-vs-snapshot activity.

**Architecture:** WebSocket bridge on port 8002 in the orchestrator process (`orchestrator/ws_bridge.py`). Forwards events to the forex-ml dashboard's existing Orchestration tab (`forex-ml-platform/.../orchestrator-client.js`). The existing HITL widget is extended for Managed Agent confirmations.

**Unblocked by:** the dispatch subsystem, because it gave us something worth graphing.

---

## Follow-up Work Surfaced by the Smoke Tests

### 2. Dispatch idempotency on orchestrator restart

**Problem.** If the orchestrator crashes or is killed mid-dispatch, the sub-session continues running on Anthropic's side until its own idle timeout (~several minutes). The `dispatch_sessions` row is left in `status='running'`. On restart, the parent session's SSE stream does not replay the agent.message that triggered the dispatch (idle sessions don't replay), so the orchestrator never re-dispatches. Result: orphaned sub-session, stale DB row, no visibility.

**Observed 2026-04-10.** `sesn_011CZwEgCWQMbqBEtfT9adku` (business_analyst dispatch) was left orphaned when we killed the orchestrator mid-test. Manually marked failed after the fact.

**Proposed fix.** On orchestrator startup, sweep `dispatch_sessions` for `status='running'` rows. For each:

- Option A (resume): open a fresh stream on the sub-session. If it's still live, continue consuming events. If it went idle while we were gone, read the accumulated events and finalize the row.
- Option B (reap): mark the row `failed` with an `error` noting the orchestrator restart. Let Anthropic's idle timeout clean up the container.

Option A is more correct; Option B is simpler. For an MVP, Option B is probably fine and can be upgraded later.

### 3. Parallel dispatch via thread pool

**Problem.** Serial dispatch. A Quad (Domain + Task + 2 verifiers) takes ~4× a single dispatch end-to-end because `_run_sub_session` blocks the parent event loop. Parent events buffer server-side and are delivered when the dispatch returns — correct but slow.

**Proposed fix.** Wrap `_run_sub_session` in a `concurrent.futures.ThreadPoolExecutor`. `handle_message` submits all fences from a single parent message in parallel, gathers results, and sends them back. Bound the pool (e.g., `max_workers=4`) so a Kernel emitting 20 fences in one message doesn't explode concurrency.

**Doesn't touch the protocol.** Kernel-side behavior is unchanged.

### 4. Cost rollup across parent + sub-sessions

**Problem.** `otel_cost_tracking` is parent-only (driven by parent SSE `span.model_request_end` events). Per-dispatch costs live in `dispatch_sessions.cost_usd`. To answer "what did task X cost me?" the operator must manually sum both. The `cloud_sessions.total_cost_usd` running total is parent-only.

**Proposed fix.** Either:

- **Database view** — `CREATE VIEW v_parent_total_cost AS SELECT parent_session_id, ...` that joins `cloud_sessions` and `dispatch_sessions`. Cheap, updates in real time.
- **Write-through** — on every `record_dispatch_complete`, also UPDATE `cloud_sessions.total_cost_usd`. Denormalized but avoids the join at query time.

The dashboard cost panel will force a decision here.

### 5. Per-dispatch token/budget caps

**Problem.** `DispatchManager` only enforces wall-clock limits (`max_dispatch_seconds=600`, `stream_read_timeout_seconds=180`). A runaway sub-agent could burn hundreds of thousands of tokens within the time budget before the watchdog fires. No integration with the base ora-kernel `orch_budget_limits` table.

**Proposed fix.** Check `input_tokens + output_tokens` after every `span.model_request_end` inside `_run_sub_session`. If it exceeds a per-node budget (loaded from `orch_budget_limits` or the `budget_size` field of the dispatch payload), send `user.interrupt` to the sub-session and mark the row `failed` with a budget-exceeded error.

---

## Further Out

### 6. Wire `orch_tasks` lifecycle writes from DispatchManager

**Observation.** Zero `orch_tasks` rows have a cloud-session `session_id`. The base ora-kernel relied on Agent tool dispatches to create task rows. Since the cloud fork reconstructs dispatch via the fence protocol, the natural place to write `orch_tasks` rows is `DispatchManager.handle_message`: a NEW row on dispatch start, transition to INCOMPLETE on sub-session start, UNVERIFIED on sub-session idle, COMPLETE when the Kernel's verifier dispatch returns successfully.

This restores the base ora-kernel task lifecycle (NEW → INCOMPLETE → UNVERIFIED → COMPLETE) without the Agent tool. The dashboard's task graph could then show task rows AND their dispatched sub-sessions as related entities.

### 7. Agent cache cleanup sweep

**Observation.** `dispatch_agents` accumulates rows over time. Every spec edit creates a new agent (by design, so the updated system prompt takes effect) but the old agent is never deleted — it just becomes unreferenced. Unreferenced agents cost nothing but clutter the Anthropic console.

**Proposed fix.** Periodic sweep (weekly cron trigger?) that lists all `dispatch_agents` rows, checks which `agent_id` values are NOT currently referenced in any recent `dispatch_sessions` row, and calls `client.beta.agents.delete` (if the API supports it) on the stale ones.

### 8. NodeDesigner → NodeCreator self-expansion end-to-end

**Observation.** The dispatch subsystem unblocks self-expansion in principle — the Kernel can now dispatch NodeDesigner, wait for the spec, dispatch NodeCreator, wait for the files, verify via the paired verifiers. But we haven't exercised this path yet. A good test task would be: "Design a new node that [does something specific], and add it to the workspace for immediate use."

**Unknowns to uncover by running this:**
- Does NodeCreator's output get written to the container's `/work/.claude/kernel/nodes/` directory?
- Does CDC pick up those writes and mirror them to `kernel_files_sync`?
- On next session resume, does the hydration path restore them into the new container?
- What triggers the orchestrator to notice the new node and make it available as a dispatch target? (Right now `node_spec_dir` is scanned at `_ensure_agent` time, which is fine — new specs are picked up lazily.)

### 9. Hybrid mode — local TUI + cloud agent sharing postgres

**Status.** Deferred in the original spec (Phase 4). Still not an MVP priority. Revisit after the dashboard is operational and at least one real multi-step task has run end-to-end in the cloud.

---

## How to Work Through This

The items above are ordered by a loose blend of (a) value and (b) how much later work depends on them. My suggestion:

1. **Task 21 (dashboard plan) → Task 22 (execute).** Biggest UX leap. Unblocks the operator feedback loop.
2. **Item 6 (orch_tasks wiring).** Cheap, makes the dashboard's task graph meaningful beyond dispatch state.
3. **Item 2 (dispatch idempotency).** Do this before any long-running task work; otherwise a crash leaves orphans.
4. **Item 4 (cost rollup).** The dashboard will want it.
5. **Item 3 (parallel dispatch).** Do when Quads become the common case.
6. **Items 5, 7, 8, 9.** Order as priorities shift.

Items 1–4 together would bring the project to "genuinely usable for real work with operator visibility and crash safety." That's probably the next coherent milestone.
