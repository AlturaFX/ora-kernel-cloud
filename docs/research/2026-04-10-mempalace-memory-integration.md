# Research — MemPalace Memory Integration for ora-kernel-cloud

**Status:** Research only. No code written. Output of this document is intended to feed a later formal plan via `superpowers:writing-plans`.
**Date:** 2026-04-10
**Author:** Claude Opus 4.6 + AlturaFX
**Upstream repo:** https://github.com/milla-jovovich/mempalace (v3.1.0, April 2026, MIT)

---

## TL;DR

MemPalace is a local, ChromaDB-backed memory system for AI conversations — 96.6% LongMemEval R@5 in raw verbatim mode, zero API calls, free. It stores raw conversation exchanges in a `wings → rooms → drawers` hierarchy and layers a temporal knowledge graph on top, exposing 19 MCP tools for search/write/KG/diary operations.

**Why it matters to ora-kernel-cloud:** the cloud Kernel has excellent *short-term* memory (in-session context, `kernel_files_sync` for WISDOM/journal, full `orch_activity_log` history) but **no semantic recall across sessions**. When a session terminates and a new one boots, the Kernel re-reads WISDOM.md (the curated snapshot) and gets a manicured summary — but it has no way to ask *"what did I try last time when business_analyst timed out?"* or *"when did we decide to bump `max_dispatch_seconds` from 120 to 600?"* That decision context lives in the raw `agent.message` stream that gets logged to postgres and then forgotten.

MemPalace fills this gap. The proposed integration preserves every architectural invariant the cloud fork already enforces (container never touches persistence, protocol teaching via BOOTSTRAP_PROMPT, Axiom 1 observability) by running MemPalace on the **orchestrator** side and exposing it to the Kernel via a new fence protocol — ```RECALL``` / ```RECALL_RESULT``` — parallel to the existing DISPATCH and SYNC fence protocols.

This document is research only. It defines the integration surface, risks, and a phased implementation sketch. A formal implementation plan would be written separately via the `superpowers:writing-plans` skill once scope is approved.

---

## 1. What MemPalace Is

### 1.1 Core concept

From the upstream README (verbatim):

> Every conversation you have with an AI — every decision, every debugging session, every architecture debate — disappears when the session ends. Six months of work, gone. You start over every time.
>
> Other memory systems try to fix this by letting AI decide what's worth remembering. It extracts "user prefers Postgres" and throws away the conversation where you explained *why*. MemPalace takes a different approach: **store everything, then make it findable.**

The storage engine is ChromaDB in **raw verbatim mode** — no LLM summarization, no lossy extraction, just the exchange text filed into a structured hierarchy. Semantic search over the raw corpus is the retrieval path. The 96.6% LongMemEval score is from this raw mode, independently reproduced on an M2 Ultra in under 5 minutes.

The repo explicitly flags limitations upstream (their own "Note from Milla & Ben — April 7, 2026"):
- **AAAK compression mode regresses** vs raw (84.2% vs 96.6% on LongMemEval). AAAK is a separate lossy compression layer intended for packing repeated entities at scale; it is **not** the storage default. The 96.6% headline is raw mode.
- **Contradiction detection** exists as `fact_checker.py` but is not yet auto-wired into the KG operations.
- **ChromaDB version pinning is in-flight** (Issue #100) and there's a shell-injection fix for the hooks scripts (#110). The cloud integration would avoid the shell hooks entirely, but the ChromaDB unpin is a real risk we must manage.

Despite these caveats, the raw-mode architecture and the MCP tool surface are both solid.

### 1.2 The palace hierarchy

| Level | Meaning | Example |
|---|---|---|
| **Wing** | A project or person — the top-level container | `wing_ora_kernel_cloud`, `wing_kai`, `wing_driftwood` |
| **Hall** | A memory type — fixed set of five categories shared across all wings | `hall_facts`, `hall_events`, `hall_discoveries`, `hall_preferences`, `hall_advice` |
| **Room** | A specific topic within a wing (can repeat across wings, which creates **tunnels**) | `dispatch-subsystem`, `ws-bridge-design`, `business-analyst-prompt-tuning` |
| **Closet** | A plain-text summary pointing at a drawer (will be AAAK-compressed in a future release) | (internal; not usually addressed directly) |
| **Drawer** | A raw verbatim chunk — the actual exchange text | One agent.message or one dispatch payload |

**Cross-cutting links:**
- **Halls** connect rooms *within* the same wing (same memory category across different topics).
- **Tunnels** connect the same room *across* different wings (e.g., `auth-migration` in `wing_kai`, `wing_driftwood`, and `wing_priya` all tunnel to each other).

**Retrieval wins from structure** (upstream's own numbers on 22,000 real memories):
- Search all closets: 60.9% R@10
- Search within wing: 73.1% (+12%)
- Search wing + hall: 84.8% (+24%)
- Search wing + room: 94.8% (+34%)

The +34% boost from wing+room filtering is a standard ChromaDB metadata filter — not a novel retrieval mechanism — but the palace structure is what makes that filtering usable (you always know which wing/room to query because the taxonomy is projectual, not cosmetic).

### 1.3 The knowledge graph

A separate SQLite-backed temporal entity-relationship store (like Graphiti but local):

```python
kg.add_triple("business_analyst", "dispatched_for", "task_042",
              valid_from="2026-04-10T17:00")
kg.add_triple("business_analyst", "completed_with_error", "stall_watchdog",
              valid_from="2026-04-10T17:04")
kg.invalidate("business_analyst", "dispatched_for", "task_042",
              ended="2026-04-10T17:04")

# Point-in-time query
kg.query_entity("business_analyst", as_of="2026-04-10T17:02")
# → active dispatch visible

# Timeline
kg.timeline("business_analyst")
# → chronological story of every dispatch that touched this node
```

Facts have validity windows. Historical queries return what was true at time T; current queries return what's true now. This is natively aligned with how `dispatch_sessions` already tracks sub-session lifecycles — every dispatch could emit 2–3 KG triples automatically.

### 1.4 MCP surface — 19 tools

Grouped into four categories, all accessible via the MCP server (`python -m mempalace.mcp_server`):

**Read:**
- `mempalace_status` — overview + AAAK spec + memory protocol instructions (auto-taught)
- `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_get_taxonomy`
- `mempalace_search(query, wing, room, limit)` — semantic search with optional filters
- `mempalace_check_duplicate(content, threshold)`
- `mempalace_get_aaak_spec` — compression dialect reference

**Write:**
- `mempalace_add_drawer(wing, room, content, source_file)` — files verbatim content
- `mempalace_delete_drawer(drawer_id)`

**Knowledge graph:**
- `mempalace_kg_query(entity, as_of, direction)`
- `mempalace_kg_add(subject, predicate, object, valid_from, source_closet)`
- `mempalace_kg_invalidate(subject, predicate, object, ended)`
- `mempalace_kg_timeline(entity)`
- `mempalace_kg_stats`

**Navigation:**
- `mempalace_traverse(start_room, max_hops)` — walk the graph from a room across wings
- `mempalace_find_tunnels(wing_a, wing_b)`
- `mempalace_graph_stats`

**Agent diary (specialist-lens memory):**
- `mempalace_diary_write(agent_name, entry, topic)` — AAAK-compressed per-agent diary
- `mempalace_diary_read(agent_name, last_n)`

### 1.5 Memory stack (L0–L3)

MemPalace defines a 4-layer stack that maps directly onto what ora-kernel-cloud already does implicitly:

| Layer | What | Size | When |
|---|---|---|---|
| **L0** | Identity — who is this AI? | ~50 tokens | Always loaded |
| **L1** | Critical facts — team, projects, preferences | ~120 tokens (AAAK) | Always loaded |
| **L2** | Room recall — recent sessions, current project | On demand | When topic comes up |
| **L3** | Deep search — semantic query across all closets | On demand | When explicitly asked |

ora-kernel-cloud's `BOOTSTRAP_PROMPT` + `send_protocol_refresh` + `kernel_files_sync` hydration already implements L0 + L1 implicitly (CLAUDE.md = L0, WISDOM.md hydration = L1). L2 and L3 are exactly what's missing — and exactly what the RECALL fence protocol below proposes to add.

### 1.6 Python API vs MCP

MemPalace exposes both:

```python
# Direct Python API (no MCP runtime needed)
from mempalace.searcher import search_memories
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.config import MempalaceConfig

results = search_memories(
    "dispatch timeout decision",
    palace_path="~/.mempalace/palace",
    wing="wing_ora_kernel_cloud",
    limit=5,
)

kg = KnowledgeGraph()
kg.add_triple("business_analyst", "dispatched_for", "task_042",
              valid_from="2026-04-10T17:00")
```

The MCP layer is a thin wrapper around these. For ora-kernel-cloud's orchestrator integration, **we use the Python API directly** — the orchestrator is Python, MemPalace is Python, there's no benefit to round-tripping through MCP and plenty of cost (subprocess, JSON serialization, tool-name stringly typing).

The **Kernel**, running in a cloud container with no Python access to the operator's machine, cannot call the Python API directly. That's where the RECALL fence protocol comes in (§ 4).

---

## 2. What ora-kernel-cloud Currently Has (and What's Missing)

### 2.1 The memory surfaces we already have

| Surface | Where | What it holds | Scope |
|---|---|---|---|
| `kernel_files_sync` table | postgres | Current state of `WISDOM.md`, recent journal entries, node specs | **Current session** (bootstrap hydration restores it, but entries get overwritten on re-sync; no history) |
| `orch_activity_log` table | postgres | Every SSE event: agent messages (full text up to `TEXT_PREVIEW_LEN=10_000`), tool uses, session status transitions, dispatch activity | **Every session ever** — it's append-only |
| `dispatch_sessions` table | postgres | Every dispatch's lifecycle (tokens, cost, duration, output, errors) | **Every session ever** |
| `cloud_sessions` table | postgres | Every parent session's lifecycle | **Every session ever** |
| `BOOTSTRAP_PROMPT` | orchestrator/session_manager.py | Static instructions + hydration of WISDOM + today's journal | **Loaded on bootstrap** |

### 2.2 What the existing surfaces can and cannot do

**They CAN answer:**
- "What's the current state of WISDOM.md?" (kernel_files_sync)
- "What happened in the last hour?" (orch_activity_log, ordered by id DESC)
- "How much did the last dispatch cost?" (dispatch_sessions WHERE sub_session_id=...)
- "When did this parent session start?" (cloud_sessions)

**They CANNOT answer:**
- "Have we dispatched business_analyst for this kind of task before? What was the outcome?"
- "Last week we debugged a dispatch timeout — what did we end up deciding?"
- "Find me any session where Axiom 2 was escalated to HITL."
- "When did we first add the DISPATCH fence protocol, and what was the motivation?"
- "Show me every decision about file sync that mentions CDC divergence."

These are all **semantic search** questions across an append-only corpus of conversation text. `orch_activity_log` contains the raw material but querying it requires exact-match LIKE patterns or full-text search — and even postgres full-text search doesn't rank by semantic similarity the way an embedding model does. You can find the needle if you already know the word; you can't find the concept.

**Summary of the gap:** `orch_activity_log` is the archive. `kernel_files_sync` is the summary. There is no index from a *question* to the relevant slice of the archive. MemPalace is that index.

### 2.3 Why `orch_activity_log` alone is not enough

Two concrete reasons:

1. **Truncation.** `TEXT_PREVIEW_LEN=10_000` means long agent messages (notably long DISPATCH_RESULT fences, long SYNC snapshot responses, long post-failure retrospectives) get clipped. MemPalace stores drawers as whatever size chunk the ingester passes in; we'd configure the ingester to store full messages without truncation.
2. **No semantic index.** Even with full text, `SELECT ... WHERE details->>'text' LIKE '%dispatch timeout%'` is the only discovery path. You need to already know the phrase. With MemPalace, `search("how did we fix the dispatch stall problem")` finds the relevant passage regardless of exact wording.

---

## 3. Why MemPalace Is the Right Fit

A short argument, since this matters for the architectural decision:

1. **Local-only, no cloud.** This is the single most important property for ora-kernel-cloud. The cloud fork's entire premise is that the Kernel runs on Anthropic's infrastructure but *all* operator state lives on the operator's machine. A memory system that required a cloud API call would be a contradictory dependency. MemPalace is explicitly, aggressively local — ChromaDB on disk, optionally a local embedding model, zero external calls after install.
2. **MIT license, Python package.** No legal obstacles, no runtime overhead from a separate service. We can import it as a normal Python library.
3. **Raw verbatim storage matches our philosophy.** The cloud Kernel's `orch_activity_log` is already append-only and un-summarized. MemPalace's storage model is a natural extension — we're not choosing between summarization strategies, we're adding a semantic-search index over content we already keep.
4. **Wings / rooms taxonomy maps onto our project structure.** `wing_<project>` per PROJECT_DNA, rooms per subsystem or node-type. The structure is not an opinion we have to adopt; it's a thin naming convention we can fit to our own decomposition.
5. **Temporal knowledge graph.** Dispatch lifecycles are a natural source of KG triples. Every dispatch creates "node_x dispatched_for task_y at t1" and "node_x completed_with cost_z at t2" without the orchestrator needing to invent a new schema. The KG gives us **point-in-time queries** ("what was business_analyst doing when I killed the orchestrator last Tuesday?") for free.
6. **Specialist agent diaries.** The self-improvement cycle ora-kernel-cloud aspires to — "why is business_analyst slower than it used to be?" — maps directly onto MemPalace's agent diary model. Each dispatched node gets its own diary, written after every completed dispatch, queryable before the next one.
7. **The protocol extension pattern is already proven.** We have two existing fence protocols (SYNC and DISPATCH) that follow the same shape: Kernel emits a fenced block, orchestrator intercepts on `agent.message` events, acts on it, replies via `user.message`. Adding a third (RECALL) is a well-understood extension of an established pattern.

**Counter-arguments worth noting:**

- The upstream author acknowledges real bugs in the first-week release and is still iterating on the spec. We'd be picking up a young project. Mitigation: pin the version, vendor the API surface we depend on, track the known issues.
- MemPalace is tuned for conversational content (chat exports, Claude conversations). `orch_activity_log` is a mix of conversational content (agent.message) and ops metadata (tool_use, session status). We'd ingest only the former, not try to dump everything.
- ChromaDB is not trivially small (~200MB on disk for modest corpora, plus a local embedding model download on first use). The operator opting in to MemPalace accepts that cost. We make it **optional** — orchestrator falls back to no-memory mode if mempalace isn't installed.

---

## 4. Proposed Architecture

### 4.1 Architectural placement

```
                              Anthropic Cloud
                              ───────────────
  Parent Kernel session ──── agent.message (contains RECALL fences)
                    ▲                                │
                    │ user.message (RECALL_RESULT)   │ SSE
                    │                                ▼
  ┌─────────────────┴────────────────────────────────────────────────┐
  │ Operator machine                                                 │
  │                                                                  │
  │  ┌───────────────────────────────────────────────────────────┐   │
  │  │ orchestrator                                              │   │
  │  │                                                           │   │
  │  │   EventConsumer ── ingests agent.message + dispatch       │   │
  │  │          │               results into MemPalace           │   │
  │  │          ▼                                                │   │
  │  │   MemoryIngester ── writes drawers, KG triples, diary     │   │
  │  │          │               entries via Python API          │   │
  │  │          ▼                                                │   │
  │  │   RecallBroker ── parses RECALL fences from agent.message │   │
  │  │          │               calls MemPalace, formats result  │   │
  │  │          ▼                                                │   │
  │  │   SessionManager.send_message ── sends RECALL_RESULT      │   │
  │  │                                                           │   │
  │  └───────────────────────────────────────────────────────────┘   │
  │                           │                                      │
  │                           ▼ Python API (in-process)              │
  │  ┌────────────────────────────────────────────────────────────┐  │
  │  │ MemPalace library                                          │  │
  │  │   - ChromaDB persistent client (~/.mempalace/palace)       │  │
  │  │   - KnowledgeGraph SQLite (~/.mempalace/palace/kg.sqlite)  │  │
  │  │   - Specialist agent diaries                               │  │
  │  └────────────────────────────────────────────────────────────┘  │
  │                                                                  │
  │  ┌────────────────────────────────────────────────────────────┐  │
  │  │ Existing postgres (unchanged)                              │  │
  │  │   - orch_activity_log (still the canonical event archive)  │  │
  │  │   - cloud_sessions, dispatch_sessions, kernel_files_sync   │  │
  │  └────────────────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────┘
```

Two key placements:

1. **MemPalace lives entirely on the orchestrator side.** Its files (ChromaDB + SQLite) go under `~/.mempalace/palace/` by convention. The orchestrator imports the Python package directly. The Managed Agent container has no access to any of it — Invariant 1 preserved.
2. **The cloud Kernel reaches MemPalace exclusively through the RECALL fence protocol.** No other path. The Kernel cannot import the library, cannot open a network socket to the operator's machine, cannot query ChromaDB directly. It emits fences; the orchestrator answers.

### 4.2 Invariants preserved

This design respects every invariant already documented in `docs/CLOUD_ARCHITECTURE.md` § Architectural Invariants:

| Invariant | Preserved how |
|---|---|
| **I1: Container never speaks to postgres (or any operator-side storage)** | MemPalace is operator-side. Kernel never touches ChromaDB, KG SQLite, or the `~/.mempalace/` directory. All reads and writes flow through the orchestrator. |
| **I2: Protocol teaching via `BOOTSTRAP_PROMPT` + `send_protocol_refresh`, never via protected files** | `RECALL_PROTOCOL` becomes a new constant in `session_manager.py` next to `SYNC_SNAPSHOT_PROTOCOL` and `DISPATCH_PROTOCOL`. `BOOTSTRAP_PROMPT` embeds it; `send_protocol_refresh` includes it. `kernel-files/CLAUDE.md` is unchanged. |
| **I3: Case-insensitive tool name matching** | N/A — MemPalace integration doesn't add new tool names to watch for; it reads `agent.message` content. |
| **I4: No agent ever self-certifies work** | MemPalace is a retrieval index, not a verification mechanism. It cannot certify or reject. Axiom 2 is unaffected. |

A proposed new invariant:

| **I5: Memory ingestion is eventually consistent; recall is best-effort** | Ingestion runs inline with the SSE loop but failures never crash the loop (try/except around every MemPalace call, matching the existing pattern for file_sync and ws_bridge). Recall failures produce a `RECALL_RESULT status="error"` fence rather than silence — the Kernel knows the recall failed and can decide how to proceed (Axiom 5). |

### 4.3 Components

#### 4.3.1 `orchestrator/memory.py` — new module

A new module mirroring the pattern of `orchestrator/file_sync.py` and `orchestrator/dispatch.py`:

**Responsibilities:**
- Lazily import `mempalace` (optional dependency; orchestrator degrades gracefully if not installed)
- Provide a `MemoryIngester` class that the `EventConsumer` and `DispatchManager` call to file drawers / KG triples / diary entries
- Provide a `MemoryRecall` class (or a `RecallBroker` façade) that the `EventConsumer` calls from `_handle_message` when a RECALL fence is parsed from an agent message
- Expose a `parse_recall_fences(text)` pure function matching the style of `parse_dispatch_fences` and `parse_sync_fences`
- Define the wing/room taxonomy defaults (one wing per `PROJECT_DNA.md` project; a catalog of canonical rooms like `dispatch-subsystem`, `file-sync`, `ws-bridge`, `session-lifecycle`, `self-improvement`, `hitl`, `prompt-tuning`)

**Dependencies:**
- `mempalace>=3.1.0` added as an **optional** dep in `requirements.txt` (or a new `requirements-memory.txt` for operators who want it)
- Late-binds the import inside `MemoryIngester.__init__` — if the import fails, the class logs a warning and becomes a no-op, and every call on it is safe but does nothing

**Failure semantics:**
- Every write call is wrapped in `try/except Exception; logger.exception("mempalace ingest failed")` — same pattern as `ws_bridge.broadcast`
- Reads return empty results on failure, with a clear error message in the `RECALL_RESULT` fence

#### 4.3.2 Hooks into `EventConsumer`

In `orchestrator/event_consumer.py` (following the same pattern as `file_sync` and `dispatch_manager` hooks):

- `__init__` grows an optional `memory_ingester: Optional["MemoryIngester"] = None` kwarg.
- `_handle_message` ingests the full text of every `agent.message` event into a drawer, tagged with `(wing=<current_project>, room=<detected_or_default>, source="parent_session", session_id=...)`.
- `_handle_message` *also* calls `memory_ingester.handle_recall_request(session_id, full_text)` to detect RECALL fences and reply via `send_to_parent`.

#### 4.3.3 Hooks into `DispatchManager`

In `orchestrator/dispatch.py`:

- `__init__` grows an optional `memory_ingester: Optional["MemoryIngester"] = None` kwarg.
- After every successful dispatch (`status=complete`), `_run_sub_session` calls:
  - `ingester.file_dispatch_result(node_name, input_data, response_text, tokens, cost, duration, parent_session_id, sub_session_id)` — stores the dispatch as a drawer in `wing_<project>`, room `dispatch-<node_name>`, hall `hall_events`
  - `ingester.write_kg_dispatch(node_name, sub_session_id, parent_session_id, "complete", valid_from=<start>)` — adds KG triples
  - `ingester.write_diary(node_name, <concise summary>, topic="dispatch")` — per-node diary entry
- On `status=failed`, same but with `status="failed"` and the error text in both the drawer and the KG triple

#### 4.3.4 `RECALL` fence protocol

A new protocol constant in `session_manager.py` paralleling `DISPATCH_PROTOCOL` and `SYNC_SNAPSHOT_PROTOCOL`:

**Kernel → orchestrator** (in `agent.message`):

````
```RECALL
{
  "query": "what did we decide about dispatch timeouts last week",
  "wing": "wing_ora_kernel_cloud",
  "room": "dispatch-subsystem",
  "limit": 5,
  "kg_entity": null,
  "as_of": null,
  "diary_agent": null
}
```
````

All fields optional except `query` (or `kg_entity` or `diary_agent` — exactly one of the three must be present):

| Field | When it's set | What the orchestrator does |
|---|---|---|
| `query` (string) | Semantic search mode | Calls `search_memories(query, wing, room, limit)` |
| `kg_entity` (string) | Knowledge-graph mode | Calls `kg.query_entity(kg_entity, as_of=as_of)` |
| `diary_agent` (string) | Specialist diary mode | Calls `diary_read(diary_agent, last_n=limit)` |
| `wing`, `room`, `limit`, `as_of` | Filters | Passed through to the underlying call |

**Orchestrator → Kernel** (as `user.message`):

````
```RECALL_RESULT status=complete count=3 mode=search
{
  "results": [
    {
      "wing": "wing_ora_kernel_cloud",
      "room": "dispatch-subsystem",
      "hall": "hall_facts",
      "snippet": "We raised max_dispatch_seconds from 120 to 600 because business_analyst legitimately takes minutes when it uses tools. The old ceiling fired on a quiet stream that was actually still running.",
      "source": "drawer_abc123",
      "timestamp": "2026-04-10T17:04:12Z",
      "score": 0.91
    },
    ...
  ]
}
```
````

Error case:

````
```RECALL_RESULT status=error mode=search
{
  "error": "MemPalace is not installed on this orchestrator — recall is unavailable",
  "query": "what did we decide about dispatch timeouts"
}
```
````

The Kernel is explicitly taught that:
- A `RECALL_RESULT status=error` is not a crash condition; it means the index is unavailable and the Kernel should fall back to its existing behavior (read WISDOM.md, ask the operator, or proceed without the memory).
- Per Axiom 5, a failed recall does not trigger a retry storm. The Kernel can retry with a different query or a different mode, but not identical.

#### 4.3.5 Taxonomy defaults

Proposed wing/room mapping for ora-kernel-cloud's typical use:

**Wings** (one per project):
- `wing_ora_kernel_cloud` — this repo's internal memory: development decisions, architectural changes, bug fixes, design discussions
- `wing_<other_project>` — per PROJECT_DNA.md when the cloud Kernel is used on other operator projects
- `wing_system` — reserved for cross-project concerns (e.g., Anthropic API changes, postgres schema migrations)

**Canonical rooms** (project-independent memory categories):
- `dispatch-subsystem` — everything about the dispatch broker, node specs, fence protocol
- `file-sync` — CDC, snapshot, kernel_files_sync, WISDOM.md evolution
- `ws-bridge` — dashboard bridge, HTTP API, protocol envelope
- `session-lifecycle` — session resumes, protocol refreshes, container restarts
- `self-improvement` — self-improvement cycle, tuning results, refinement analyses
- `hitl` — HITL approvals, denials, escalation patterns
- `prompt-tuning` — changes to CLAUDE.md / BOOTSTRAP_PROMPT / node specs
- `incidents` — production-style issues: stuck dispatches, orphaned sub-sessions, quota overruns
- `cost-analysis` — running cost patterns, budget planning

**Per-node diary agents:**
- `diary_business_analyst`, `diary_node_designer`, `diary_node_creator`, `diary_tuning_analyst`, etc. — one per system node that's been dispatched at least once

The exact taxonomy is negotiable. MemPalace's `onboarding.py` auto-detects wings/rooms from content, so we'd bootstrap with a seed taxonomy and let it evolve.

---

## 5. Phased Implementation Outline

**Not a plan.** This is a research-stage outline intended to feed a future `superpowers:writing-plans` session. Each phase produces working, testable software on its own.

### Phase 1 — Ingest-only (foundation)

**Goal:** Every agent.message and every dispatch outcome lands in MemPalace as a drawer. Nothing queries it yet.

**Scope:**
1. `orchestrator/memory.py` — new module with `MemoryIngester` and `parse_recall_fences` (the parser is in place even though there's no reader yet, so we can unit-test it standalone)
2. Optional-dependency plumbing: `mempalace>=3.1.0` in a new `requirements-memory.txt`, lazy import inside `MemoryIngester.__init__`
3. `EventConsumer` hook: file a drawer per agent.message, try/except wrapped
4. `DispatchManager` hook: file a drawer per completed dispatch + write KG triples + write per-node diary entry
5. Unit tests with a mocked `mempalace` module (we don't install the real package in CI — we mock the three entry points: `search_memories`, `KnowledgeGraph`, and an ingester API we wrap)
6. Optional live smoke test on an operator machine where MemPalace is installed: run the orchestrator for a minute, confirm `mempalace search` returns a recent agent message from the palace

**Non-goals (explicit):**
- No RECALL fence handling yet
- No BOOTSTRAP_PROMPT changes yet
- No schema migrations (MemPalace owns its own storage)

**Verification:**
- Unit tests with a mocked mempalace module pass
- Live smoke test on an opt-in operator machine shows drawers appearing in the palace

**Risks for this phase:**
- Volume. A single live session produces dozens to hundreds of agent messages per hour. MemPalace ingests sync; we need to make sure ingestion doesn't block the SSE loop. Mitigation: run ingestion on a background thread (a thin queue + worker, reused across events) or use `asyncio.run_coroutine_threadsafe` if we can run MemPalace on the ws_bridge loop (probably not — ChromaDB is sync).

### Phase 2 — RECALL fence protocol

**Goal:** The Kernel can ask the orchestrator to search the palace and get results back in the next turn.

**Scope:**
1. `RECALL_PROTOCOL` constant in `session_manager.py`
2. `BOOTSTRAP_PROMPT` embeds it
3. `send_protocol_refresh` includes it
4. `EventConsumer._handle_message` calls `memory.handle_recall_request(session_id, full_text)` which parses RECALL fences, calls MemPalace, and emits RECALL_RESULT back via `session_mgr.send_message`
5. Format `RECALL_RESULT` as a `user.message` back to the parent session (same path as DISPATCH_RESULT)
6. Unit tests with a mocked MemPalace and a mocked `session_mgr.send_message`
7. Live smoke test: start orchestrator, send a test task that asks the Kernel to recall something, observe the fence round-trip in `orch_activity_log`

**Non-goals:**
- No wake-up layer yet (that's Phase 3)
- No KG-specific query tool yet if the first pass only supports semantic search

**Verification:**
- Unit tests green
- Smoke test shows a round-trip: Kernel emits RECALL → orchestrator parses → MemPalace returns results → orchestrator emits RECALL_RESULT → Kernel sees and processes it

**Risks for this phase:**
- Kernel may spam RECALL fences. Mitigation: the protocol explicitly discourages recall storms (Axiom 5 language) and the orchestrator logs + counts recalls per session for observability.
- Query malformation — the Kernel may send RECALL fences with invalid JSON or unknown fields. Mitigation: `parse_recall_fences` follows the existing pattern of silent-skip on malformed input, with a logger.warning.

### Phase 3 — Wake-up layer (L0 + L1 in bootstrap)

**Goal:** Every new session starts with a compact "here's what you already know" prelude derived from MemPalace. Saves round-trips for basic facts.

**Scope:**
1. Add `mempalace wake-up --wing <project>` subprocess call during `SessionManager.bootstrap()` (or a direct Python API call — whichever MemPalace exposes for programmatic access)
2. The ~170-token output is injected into `BOOTSTRAP_PROMPT` under a new `{memory_wake_up}` placeholder
3. A staleness guard: if MemPalace is not installed or returns an error, the placeholder is empty (no crash)
4. Documented via a new section in `docs/CLOUD_ARCHITECTURE.md` explaining that L0 + L1 are now provided by MemPalace rather than by `kernel_files_sync` hydration alone

**Non-goals:**
- Not replacing `kernel_files_sync` hydration. Both coexist — `kernel_files_sync` is the curated summary (WISDOM.md), the wake-up is the "last 20 things I actually said" in compressed form.

### Phase 4 — Knowledge graph queries via RECALL

**Goal:** The Kernel can ask point-in-time questions like "what was business_analyst doing 2 days ago?" or "timeline of dispatch-subsystem changes".

**Scope:**
1. Extend the RECALL protocol to support `kg_entity` and `as_of` modes (defined in § 4.3.4 already)
2. Orchestrator calls `kg.query_entity(...)` or `kg.timeline(...)` and formats results as RECALL_RESULT payloads
3. Dispatch events from Phase 1 are already generating KG triples — this phase just wires the read path

**Non-goals:**
- No graph traversal tools yet (that's Phase 5)

### Phase 5 — Diary queries + graph traversal

**Goal:** The Kernel can ask specialist nodes "what have you been learning?" before dispatching them again.

**Scope:**
1. Extend RECALL protocol with `diary_agent` mode
2. Orchestrator calls `diary_read(agent_name, last_n=limit)` and returns AAAK-compressed entries
3. Optional: add `traverse` mode for `mempalace_traverse` (walk the graph from a room across wings)
4. Optional: add `find_tunnels` mode
5. Self-improvement cycle integration: when `/self-improve` fires, the Kernel can use diary queries to see each node's recent history without re-dispatching any of them

**Non-goals:**
- Still no cloud/MCP exposure to anything outside the operator's machine

---

## 6. Risks and Mitigations

### 6.1 Upstream project maturity

**Risk:** MemPalace is v3.1.0, ~1 month old at the time of writing, with the authors themselves flagging bugs in the first-week release (ChromaDB pin missing, shell injection in hooks, macOS ARM64 segfault, AAAK/raw mode confusion in the README). The API surface may churn.

**Mitigations:**
- **Pin the version.** `mempalace==3.1.0` (or later, but strict), not `>=3.1.0` with unbounded upper.
- **Vendor the API surface we depend on** behind `orchestrator/memory.py`. Three entry points: `search_memories`, `KnowledgeGraph`, and a write-drawer helper. If MemPalace renames/refactors any of these, the damage is contained to one file.
- **Track upstream issues.** Specifically #43 (AAAK), #100 (ChromaDB pin), #110 (shell injection in hooks — NOT applicable to us since we use the Python API, but still worth knowing), #74 (macOS ARM64 segfault — relevant if the operator runs on an M-series Mac).
- **Don't use the auto-save hooks (`mempal_save_hook.sh`, `mempal_precompact_hook.sh`).** Those are Claude-Code-plugin-specific and have the shell-injection vuln #110. We use the Python API from the orchestrator directly.
- **Don't use AAAK storage mode.** The 96.6% benchmark is raw mode. AAAK regresses to 84.2%. We use raw mode for drawers and optionally AAAK for diary entries (which are the use case AAAK was actually designed for — repeated entity codes at scale).

### 6.2 ChromaDB footprint

**Risk:** ChromaDB requires a local embedding model (default: `all-MiniLM-L6-v2`, ~90MB download on first use) and persistent storage. A modest corpus is ~200MB on disk. An aggressive corpus (six months of daily use) can grow to several GB.

**Mitigations:**
- **Optional install.** MemPalace goes in `requirements-memory.txt`, not the main `requirements.txt`. Operators who don't want it don't pay the cost.
- **Bounded ingestion.** We can set a drawer-count ceiling per wing and deduplicate aggressively on ingest (MemPalace already has `check_duplicate` with a 0.85–0.90 similarity threshold). 
- **Document disk usage.** Update `docs/API_KEY_SETUP.md` § Cost Model with a new "Memory (opt-in)" line.
- **Garbage collection.** Eventually we want a retention policy — old drawers in low-traffic rooms roll off. Not a Phase-1 concern.

### 6.3 Thread safety and SSE-loop blocking

**Risk:** ChromaDB writes are synchronous and not instantaneous (tens of milliseconds for a single drawer, more under load). The existing SSE event loop is synchronous and blocking. An ingest-per-agent-message call inline on the SSE thread could slow the loop noticeably if the Kernel is chatty.

**Mitigations:**
- **Background ingestion thread.** Spawn one dedicated daemon thread per orchestrator boot that owns the MemPalace client and reads from a `queue.Queue`. The SSE loop pushes to the queue (non-blocking) and returns immediately. Queue size is bounded; overflow is logged and dropped (never blocks the SSE loop).
- **Failure isolation.** Every MemPalace call is `try/except Exception; logger.exception(...)` — matching the established pattern for `file_sync`, `dispatch_manager`, and `ws_bridge` calls in `EventConsumer`.
- **Health endpoint.** Add a new HTTP panel endpoint `/api/cloud/memory/health` that reports ingest queue depth, last successful ingest timestamp, and any recent errors. Mirrors the existing health endpoints.

### 6.4 Content privacy and secrets

**Risk:** `agent.message` text may contain sensitive data: parts of source code, config fragments, database identifiers, the operator's API key if it ever leaks into a message (which already happened once this session — the `.env` dump), internal project names. MemPalace stores verbatim. If the palace directory is ever backed up to the cloud, shared, or committed to git, secrets go with it.

**Mitigations:**
- **Palace directory is gitignored by default.** Add `~/.mempalace/` to the documented `.gitignore` advice in `SECURITY.md` — mirrors how we handle `.env`.
- **Ingest-time redaction (future work).** Before filing a drawer, run a regex sweep for obvious secret patterns: `sk-ant-[A-Za-z0-9_\-]{80,}`, `sk-[A-Za-z0-9]{40,}`, `postgres://[^@]+:[^@]+@`, etc. Replace with `[REDACTED]`. This is a nice-to-have for Phase 1 and should be documented as Phase 1.5 or as a first-order Phase 1 feature if the operator runs any kind of secret-management protocol.
- **Opt-in per session.** The operator can set `config.memory.enabled: false` to disable MemPalace ingestion for specific sessions (e.g., when debugging with real credentials in the context).
- **Write-ahead log.** MemPalace already has a WAL at `~/.mempalace/wal/write_log.jsonl` for audit / rollback. The orchestrator's ingestion path should be reviewable via that log.

### 6.5 Kernel over-reliance on RECALL

**Risk:** The Kernel may learn to call RECALL for every question instead of using its own in-session context. This would inflate token costs and add latency to every turn.

**Mitigations:**
- **Explicit protocol language.** The RECALL protocol text in `BOOTSTRAP_PROMPT` should say: *"Use RECALL only when you don't already know the answer from your current session context or WISDOM.md. Recall is not a substitute for reasoning."* Similar to how the DISPATCH protocol says *"Use the dispatch subsystem only for work that requires a focused sub-agent — not for trivial operations."*
- **Observable frequency.** Add a count of RECALL calls per session to the SYSTEM_STATUS event broadcast. The dashboard surfaces it. If we see the count climbing into the hundreds per session, that's a prompt-tuning flag.
- **Budget.** A soft cap per session (e.g., 20 recalls) with a warning message in the RECALL_RESULT when the cap is approaching. Hard-failing above the cap would be too aggressive.

### 6.6 Protocol-drift between orchestrator and Kernel

**Risk:** The same drift problem we already solved for SYNC and DISPATCH — resumed sessions don't re-read BOOTSTRAP_PROMPT, so a new orchestrator version with an updated protocol will find the old Kernel following the stale spec.

**Mitigation:** Reuse the existing `send_protocol_refresh()` mechanism. On every orchestrator boot against a resumed session, send the full current SYNC + DISPATCH + RECALL protocols. The pattern is already working; adding a third protocol is trivial.

### 6.7 Corpus bootstrapping

**Risk:** A fresh MemPalace install has an empty palace. Phase 1 starts filling it from live sessions, but the Kernel can't recall things that happened *before* MemPalace was turned on.

**Mitigation:**
- **Initial mine from `orch_activity_log`.** One-time backfill: a script that reads existing `orch_activity_log` rows and files them as drawers. Group by session, file under `wing_ora_kernel_cloud`, room `session-lifecycle` or `dispatch-subsystem` depending on the action. This converts the existing append-only archive into a searchable corpus without any loss.
- **Optional mine from `kernel_files_sync`.** Every past WISDOM.md version and journal entry also becomes a drawer under `wing_ora_kernel_cloud / hall_discoveries`.
- **`mempalace mine` on a chat export.** If the operator has Claude Code transcript exports for earlier sessions (before the cloud fork), they can pre-populate the palace with those using MemPalace's own CLI. Not part of this integration, but worth documenting as an on-ramp.

### 6.8 What happens when MemPalace is absent

**Risk:** Operators run the orchestrator without installing mempalace (the optional dep). Ingestion silently skips, RECALL fences silently fail.

**Mitigations:**
- **`RECALL_RESULT status=error`** on every recall when the library is missing, with a clear explanation in the payload. The Kernel sees the failure and can respond to the operator: *"I tried to recall that but long-term memory isn't installed on this orchestrator."*
- **Startup log.** `__main__.py` logs `"Memory: MemPalace installed, ingestion enabled"` or `"Memory: MemPalace not installed — ingestion and recall disabled"` so operators know without having to read source.
- **Dashboard memory panel** (future): shows memory status alongside the other panels so the operator has a single-glance view.

---

## 7. Open Questions

### 7.1 Scope of ingestion

**Question:** Do we ingest *every* `agent.message`, or only specific ones (e.g., completed dispatches, messages containing DISPATCH fences, messages above a length threshold)?

**Considerations:**
- Every-message ingestion is simpler (no filtering logic) but inflates the corpus with routine chatter.
- Filtered ingestion is more curated but requires us to define the filter, and the definition will drift over time.
- MemPalace's `check_duplicate` (default threshold 0.9) already deduplicates, so "spam" messages are naturally suppressed — we're not double-filing identical content.

**Preliminary answer:** Ingest every `agent.message` with deduplication enabled, but batch DISPATCH_RESULT / long decision messages into their own room (`dispatch-subsystem` or `decisions`) for higher retrieval precision. Routine chatter lands in `hall_events` and gets lower retrieval weight naturally.

### 7.2 Wing-per-project vs wing-per-session

**Question:** Should we create a wing per *project* (one `wing_ora_kernel_cloud` for all ora-kernel-cloud sessions ever) or a wing per *session* (one `wing_<session_id>` per cloud session)?

**Considerations:**
- Per-project: semantic search sees the full history, which is what makes long-term recall powerful.
- Per-session: cleaner isolation, easier garbage collection, but you can't find "what did we decide in the last session?" without knowing the last session ID.

**Preliminary answer:** Wing per project, room per subsystem. Sessions are tagged in the drawer metadata (session_id field) so you can filter by session after a broader search if needed.

### 7.3 Kernel-side protocol teaching format

**Question:** Should the RECALL_PROTOCOL block in `BOOTSTRAP_PROMPT` include example queries and example results, or just the schema?

**Considerations:**
- More examples = more tokens per bootstrap, but better Kernel compliance.
- Schema-only = fewer tokens, but potentially more confused recall attempts.

**Preliminary answer:** Schema + two examples (one `query`, one `kg_entity`). Total ~400 tokens. Same density as the existing DISPATCH_PROTOCOL.

### 7.4 Who decides wing/room for a new drawer?

**Question:** When ingesting an `agent.message`, how do we pick the wing and room?

**Considerations:**
- **Static config:** orchestrator reads `config.yaml` for a default wing and a content-pattern-to-room map. Simple, predictable, but doesn't adapt.
- **MemPalace onboarding:** MemPalace's `room_detector_local.py` can auto-detect rooms from content. Nice, but adds another model dependency and delay.
- **Hybrid:** static wing (always `wing_<project>`), room detected by simple keyword heuristics (match on tool names, fence types, well-known topic strings), fallback to `room_general`.

**Preliminary answer:** Hybrid. Static wing from `config.yaml`, room from a small keyword map in `orchestrator/memory.py`, fallback to `room_general`. Operators can override both via a `config.memory.default_wing` and a pattern list in config.

### 7.5 KG triple generation from dispatch events

**Question:** Every dispatch produces at least one KG triple. What's the vocabulary?

**Draft vocabulary:**
- `(node_name, "dispatched_for", task_id or sub_session_id, valid_from=start_ts)`
- `(node_name, "completed", sub_session_id, valid_from=end_ts)` — invalidates the previous triple
- `(node_name, "failed_with", error_class, valid_from=end_ts)` — on failure only
- `(node_name, "cost_usd", float_value, valid_from=end_ts)` — for cost analytics
- `(parent_session, "dispatched", sub_session, valid_from=start_ts)`

**Preliminary answer:** Start with these five. Add more (e.g., `tokens_consumed`, `duration_class`) in Phase 4 if analytics value them.

### 7.6 Does the dashboard show memory state?

**Question:** Should Phase B of the dashboard (separate repo) include a MemPalace panel?

**Considerations:**
- Obvious value: a view of "most recent drawers", "recent RECALL calls and their results", "KG entity timeline" would make memory behavior transparent.
- Out of scope for this integration — that's dashboard work, not orchestrator work.

**Preliminary answer:** Document as a follow-up for `forex-ml-platform`'s Phase B plan. Add a `/api/cloud/memory/*` endpoint family to the existing HTTP panel API when we get to implementation, so the dashboard has a data source.

### 7.7 Ingestion retroactively on an existing palace

**Question:** If the operator already has a MemPalace palace from using MemPalace with Claude Code directly (a plausible scenario — the reference integration is a Claude Code plugin), should the orchestrator ingest into the *same* palace or a separate one?

**Considerations:**
- Same palace: unified memory across all the operator's AI interactions. Most powerful.
- Separate palace: isolation, easier reasoning about what came from where.

**Preliminary answer:** Default to the same palace (`~/.mempalace/palace`), with a dedicated wing (`wing_ora_kernel_cloud`) that keeps ora-kernel-cloud's contributions clearly tagged. Operators can override via `config.memory.palace_path` if they want isolation.

---

## 8. Out of Scope for This Research

Things explicitly NOT decided in this document:

1. **Which exact version of MemPalace to pin.** Need to wait until 3.1.x or later stabilizes (ChromaDB pin fix, AAAK clarification).
2. **Whether to contribute upstream.** We may need small upstream patches (e.g., a programmatic `wake_up()` function that doesn't shell out). That's a separate conversation with the MemPalace maintainers.
3. **The exact wording of `RECALL_PROTOCOL`.** § 4.3.4 is a sketch. The actual prompt text needs writing and tuning once we've seen how the Kernel behaves with a draft.
4. **Retention and garbage collection policies.** Drawers live forever by default. We'll need a policy eventually, but not in Phase 1.
5. **Multi-operator support.** MemPalace is single-operator by design. If ora-kernel-cloud ever grows a multi-operator story, memory segmentation becomes a real problem. Not a 2026 concern.
6. **Benchmarking.** Before and after numbers on a concrete metric (e.g., "how often does the Kernel have to re-learn something it already knew") would be valuable but require a benchmark we don't have yet.

---

## 9. Recommended Next Steps

1. **Read this document.** (You're here.)
2. **Discuss.** Is the scope right? Is the wing/room taxonomy sensible? Are there objections to the protocol design?
3. **Approve or revise the architecture.** Specifically: is RECALL-as-fence-protocol the right mental model, or would you prefer a different integration pattern (e.g., HTTP endpoint poll, scheduler-driven digest, etc.)?
4. **If approved: write a formal plan** via `superpowers:writing-plans` targeting Phase 1 (ingest-only). Subsequent phases would each get their own plan.
5. **Before starting Phase 1**, install MemPalace on the dev machine and run `mempalace init ~/.mempalace/palace` to smoke-test the library against our environment. Confirm:
   - ChromaDB embedding model downloads and initializes
   - `mempalace_status` returns expected output
   - `search_memories()` runs against an empty palace without crashing
   - Resource footprint is acceptable (disk + memory)

---

## 10. References

- **Upstream repo:** https://github.com/milla-jovovich/mempalace (v3.1.0, MIT)
- **MemPalace README:** Comprehensive doc including palace concept, protocol teaching, 19 MCP tools, benchmarks
- **Author's honesty note (April 7, 2026):** README § "A Note from Milla & Ben" — flags the AAAK confusion, fact_checker.py gap, and ChromaDB pin issue
- **LongMemEval benchmark paper:** Cited in the upstream README as the source of the 96.6% raw-mode score
- **Upstream openclaw integration:** `integrations/openclaw/SKILL.md` — reference skill file documenting the MCP protocol teaching format that inspired the RECALL protocol design here
- **ora-kernel-cloud architectural invariants:** `docs/CLOUD_ARCHITECTURE.md` § "Architectural Invariants" — the rules this integration must respect
- **ora-kernel-cloud fence protocols already in place:** `orchestrator/session_manager.py` (`SYNC_SNAPSHOT_PROTOCOL`, `DISPATCH_PROTOCOL`), `orchestrator/file_sync.py` (`parse_sync_fences`), `orchestrator/dispatch.py` (`parse_dispatch_fences`) — the pattern this integration extends

---

## Appendix A — Minimal `orchestrator/memory.py` skeleton (for discussion, not for execution)

```python
"""Long-term memory via MemPalace (optional dependency).

This module is a THIN façade over MemPalace's Python API. If
mempalace is not installed, all operations are no-ops and the
orchestrator continues to run with short-term memory only.

The cloud Kernel reaches this module through the RECALL fence
protocol — it never imports mempalace or talks to ChromaDB directly.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_RECALL_FENCE_RE = re.compile(
    r"```RECALL\s*\n(?P<body>.*?)(?:\n)?```",
    re.DOTALL,
)


def parse_recall_fences(text: str) -> List[Dict[str, Any]]:
    """Extract RECALL payloads from an agent.message.

    Follows the same skip-on-malformed pattern as parse_sync_fences
    and parse_dispatch_fences.
    """
    # (implementation sketch, details TBD)
    ...


class MemoryIngester:
    """Background ingestion of agent.message and dispatch events into MemPalace.

    Designed to fail gracefully: if mempalace is not installed, all
    methods are no-ops. If a call into mempalace raises, it is logged
    and dropped — never propagated to the caller.
    """

    def __init__(self, palace_path: str = "~/.mempalace/palace",
                 default_wing: str = "wing_ora_kernel_cloud",
                 queue_max: int = 1000):
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._worker: Optional[threading.Thread] = None
        self._mempalace = None  # Lazy import
        self._default_wing = default_wing
        self._palace_path = palace_path
        self._try_import()
        if self._mempalace is not None:
            self._start_worker()

    def _try_import(self) -> None:
        try:
            import mempalace  # noqa: F401
            from mempalace.searcher import search_memories
            from mempalace.knowledge_graph import KnowledgeGraph
            self._mempalace = {
                "search": search_memories,
                "KG": KnowledgeGraph,
                # ... more imports as we need them
            }
            logger.info("Memory: MemPalace loaded")
        except Exception:
            logger.info("Memory: MemPalace not installed — ingestion disabled")
            self._mempalace = None

    def _start_worker(self) -> None:
        self._worker = threading.Thread(
            target=self._worker_main, name="mempalace-ingest", daemon=True
        )
        self._worker.start()

    def _worker_main(self) -> None:
        while True:
            try:
                job = self._queue.get()
            except Exception:
                logger.exception("Memory: worker queue get failed")
                continue
            if job is None:
                return
            try:
                self._process_job(job)
            except Exception:
                logger.exception("Memory: job processing failed")

    def _process_job(self, job: Dict[str, Any]) -> None:
        kind = job.get("kind")
        if kind == "drawer":
            # ... call mempalace.add_drawer
            pass
        elif kind == "kg_triple":
            # ... call self._mempalace["KG"]().add_triple(...)
            pass
        elif kind == "diary":
            # ... call mempalace.diary_write
            pass

    # ── Public ingestion API (called from EventConsumer / DispatchManager) ──

    def file_agent_message(self, session_id: str, text: str,
                           wing: Optional[str] = None,
                           room: Optional[str] = None) -> None:
        if self._mempalace is None:
            return
        try:
            self._queue.put_nowait({
                "kind": "drawer",
                "wing": wing or self._default_wing,
                "room": room or "hall_events",
                "content": text,
                "metadata": {"session_id": session_id, "source": "parent_message"},
            })
        except queue.Full:
            logger.warning("Memory: ingest queue full, dropping drawer")

    def file_dispatch_result(
        self, node_name: str, input_data: Dict[str, Any],
        response_text: str, tokens: Dict[str, int], cost_usd: float,
        duration_ms: int, parent_session_id: str, sub_session_id: str,
        status: str,
    ) -> None:
        # Files a drawer + writes KG triples + writes a diary entry
        ...

    def shutdown(self) -> None:
        if self._worker is not None:
            self._queue.put(None)  # sentinel
            self._worker.join(timeout=2.0)


class RecallBroker:
    """Handles RECALL fences from agent.message events.

    Called from EventConsumer._handle_message after the regular
    ingestion path. Parses fences, calls MemPalace search, and
    enqueues RECALL_RESULT user.messages via the injected
    send_to_parent callback.
    """

    def __init__(self, memory: MemoryIngester,
                 send_to_parent: Callable[[str, str], None]):
        self.memory = memory
        self.send_to_parent = send_to_parent

    def handle_message(self, parent_session_id: str, text: str) -> int:
        fences = parse_recall_fences(text)
        if not fences:
            return 0
        for payload in fences:
            try:
                result = self._execute_recall(payload)
            except Exception as exc:
                logger.exception("Memory: recall failed")
                result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            try:
                self.send_to_parent(
                    parent_session_id, self._format_result_fence(result, payload)
                )
            except Exception:
                logger.exception("Memory: failed to send RECALL_RESULT")
        return len(fences)

    def _execute_recall(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # query mode / kg_entity mode / diary_agent mode dispatch
        ...

    @staticmethod
    def _format_result_fence(result: Dict[str, Any],
                             request: Dict[str, Any]) -> str:
        # Return a ```RECALL_RESULT ...``` fenced block
        ...
```

This is a sketch only. Actual method signatures, error handling, and internal structure will be pinned in the formal Phase 1 plan.

---

*End of research document. Ready for discussion.*
