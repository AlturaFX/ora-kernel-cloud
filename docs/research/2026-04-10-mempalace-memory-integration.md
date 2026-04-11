# Research — Semantic Memory for ora-kernel-cloud (inspired by MemPalace, built on pgvector)

**Status:** Research complete. Architectural recommendation: **Option C** — implement a postgres+pgvector semantic store in the orchestrator, adapting specific modules from MemPalace (MIT) with attribution. This document is the input to the formal Phase 1 plan (to be written via `superpowers:writing-plans`) and to `SPEC-002-semantic-memory.md`.
**Date:** 2026-04-10
**Author:** Claude Opus 4.6 + AlturaFX
**Prior art:** https://github.com/milla-jovovich/mempalace (v3.1.0, April 2026, MIT)

---

## TL;DR

The cloud Kernel has excellent *short-term* memory (in-session context, `kernel_files_sync` WISDOM hydration, full `orch_activity_log` history) but **no semantic recall across sessions**. When a session terminates and a new one boots, the Kernel re-reads WISDOM.md (the curated snapshot) but has no way to ask *"what did I try last time when business_analyst timed out?"* or *"when did we decide to bump `max_dispatch_seconds` from 120 to 600?"* That decision context lives in the raw `agent.message` stream that gets logged to postgres and then forgotten.

**MemPalace** is a local, ChromaDB-backed memory system for AI conversations — 96.6% LongMemEval R@5 in raw verbatim mode, zero API calls, free, MIT-licensed. It stores raw conversation exchanges in a `wings → rooms → drawers` hierarchy and layers a temporal knowledge graph on top. Its architectural patterns (palace taxonomy, temporal KG, cross-wing tunnels, specialist agent diaries) are exactly what ora-kernel-cloud needs.

**But we don't want a second storage engine.** ora-kernel-cloud already runs postgres with a mature schema, operator tooling, and backup story. ChromaDB would be a parallel storage system that doubles operational surface and couples us to a young upstream. The key insight: **MemPalace's value is the architectural patterns, not the ChromaDB integration.** Vector search with metadata filtering is a capability postgres has natively via `pgvector`.

**Proposed architecture:** Build a thin semantic store on `postgres + pgvector + sentence-transformers`, adapting ~1000 lines of MemPalace's core modules (`knowledge_graph.py`, `palace_graph.py`, `dedup.py`, parts of `layers.py`) with full MIT attribution. Write ~1000 lines of new pgvector-specific code and orchestrator integration. The cloud Kernel reaches memory exclusively through a new **`RECALL` fence protocol** parallel to the existing DISPATCH and SYNC fence protocols. No new service, no new storage surface, no upstream dependency on MemPalace as a runtime package — everything in one postgres database the operator already runs.

This document captures the full research leading to this recommendation. The companion `docs/specs/SPEC-002-semantic-memory.md` is the formal spec that Phase 1's implementation plan will reference.

---

## 1. Problem Statement

### 1.1 What ora-kernel-cloud has

| Surface | Where | What it holds | Scope |
|---|---|---|---|
| `kernel_files_sync` | postgres | Current `WISDOM.md`, recent journal entries, node specs | **Current session** — entries overwritten on re-sync, no history |
| `orch_activity_log` | postgres | Every SSE event: agent messages (up to `TEXT_PREVIEW_LEN=10_000`), tool uses, session status, dispatch activity | **Every session ever** — append-only |
| `dispatch_sessions` | postgres | Every dispatch's lifecycle (tokens, cost, duration, output, errors) | **Every session ever** |
| `cloud_sessions` | postgres | Every parent session's lifecycle | **Every session ever** |
| `BOOTSTRAP_PROMPT` | `session_manager.py` | Static instructions + hydration of WISDOM + today's journal | **Loaded on bootstrap** |

### 1.2 What these surfaces cannot answer

**They CAN answer:**
- "What's the current state of WISDOM.md?" (kernel_files_sync)
- "What happened in the last hour?" (orch_activity_log, ordered by id DESC)
- "How much did the last dispatch cost?" (dispatch_sessions)

**They CANNOT answer:**
- "Have we dispatched business_analyst for this kind of task before? What was the outcome?"
- "Last week we debugged a dispatch timeout — what did we end up deciding?"
- "Find me any session where Axiom 2 was escalated to HITL."
- "When did we first add the DISPATCH fence protocol, and what was the motivation?"
- "Show me every decision about file sync that mentions CDC divergence."

These are all **semantic search** questions across an append-only corpus of conversation text. `orch_activity_log` contains the raw material; querying it requires exact-match LIKE patterns or postgres full-text search — neither of which rank by semantic similarity the way an embedding model does. You can find the needle if you already know the word; you can't find the concept.

### 1.3 The gap

`orch_activity_log` is the archive. `kernel_files_sync` is the summary. **There is no index from a question to the relevant slice of the archive.** That index is what this project proposes to build.

---

## 2. Prior Art — MemPalace

### 2.1 What it is

MemPalace is a local memory system for AI conversations. Storage is ChromaDB in **raw verbatim mode** — no LLM summarization, no lossy extraction, just the exchange text filed into a structured hierarchy. Semantic search over the raw corpus is the retrieval path.

**Benchmark claims** (from the upstream README, with their own caveats):
- 96.6% LongMemEval R@5 on 500 questions, zero API calls, raw mode — independently reproduced on M2 Ultra in under 5 minutes.
- AAAK compression mode regresses to 84.2% vs raw's 96.6%; the 96.6% headline is from **raw mode**, not AAAK.
- The +34% "palace boost" is wing+room metadata filtering — a standard ChromaDB feature, not a novel retrieval mechanism. Real and useful, but not a moat.

**Upstream honesty** (from their "Note from Milla & Ben — April 7, 2026"): they acknowledge the AAAK token example was miscalculated, the "30x lossless compression" was overstated, contradiction detection isn't yet wired into KG ops, and the ChromaDB version isn't pinned. They're iterating publicly and responsively. This matters because we're treating them as a design inspiration, not a production dependency.

### 2.2 The palace hierarchy

| Level | Meaning | Example |
|---|---|---|
| **Wing** | A project or person — the top-level container | `wing_ora_kernel_cloud`, `wing_driftwood` |
| **Hall** | A memory type — fixed set of five categories shared across all wings | `hall_facts`, `hall_events`, `hall_discoveries`, `hall_preferences`, `hall_advice` |
| **Room** | A specific topic within a wing (can repeat across wings, which creates **tunnels**) | `dispatch-subsystem`, `ws-bridge-design` |
| **Drawer** | A raw verbatim chunk — the actual exchange text | One `agent.message` or one dispatch payload |
| **Closet** | A plain-text summary pointing at a drawer | (internal; we don't plan to use this in our implementation) |
| **Hall** (corridors) | Connects rooms within a wing | |
| **Tunnel** | Connects the same room across different wings | |

**Retrieval wins from structure** (upstream's own numbers on 22,000 memories):
- Search all closets: 60.9% R@10
- Search within wing: 73.1% (+12%)
- Search wing + hall: 84.8% (+24%)
- Search wing + room: 94.8% (+34%)

The +34% isn't magic — it's ChromaDB's metadata filter doing `WHERE wing = ... AND room = ...` on an index that's computed over wing/room values. pgvector does the same with standard SQL `WHERE` clauses on JSONB metadata columns.

### 2.3 The temporal knowledge graph

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
# → chronological story of every dispatch touching this node
```

Facts have validity windows. Historical queries return what was true at time T; current queries return what's true now. This is natively aligned with how `dispatch_sessions` already tracks sub-session lifecycles — every dispatch can emit 2–3 KG triples automatically.

### 2.4 Specialist agent diaries

Each dispatched node gets its own diary, written after every completed dispatch, queryable before the next one. Gives us per-node self-improvement context without re-dispatching.

### 2.5 MCP surface — 19 tools

MemPalace exposes an MCP server with 19 tools (search, write, KG, navigation, diary). **We do not use the MCP surface.** The cloud Kernel reaches memory through a fence protocol (§ 4) that we own, parsed by the orchestrator. MCP adds no value when both sides are Python running in the same process.

### 2.6 Why we like the design

1. **Local-only, no cloud.** Matches our invariant that all operator state lives on the operator's machine.
2. **Raw verbatim storage matches our philosophy.** `orch_activity_log` is already append-only and un-summarized.
3. **Wings/rooms taxonomy maps onto our project structure.** One wing per PROJECT_DNA project, rooms per subsystem.
4. **Temporal knowledge graph.** Dispatch lifecycles are a natural source of KG triples — every dispatch creates 2–3 triples for free.
5. **Specialist agent diaries.** The self-improvement cycle we've been missing.
6. **Benchmark-validated retrieval.** 96.6% LongMemEval R@5 gives us a strong prior that vector search over raw text with metadata filtering is the right approach.

### 2.7 Why we don't want it as a runtime dependency

1. **ChromaDB is a parallel storage system.** Doubles operational surface (postgres *and* `~/.mempalace/`). Operators would manage two stores with two backup strategies.
2. **Young upstream project.** v3.1.0 at the time of this research — roughly one month old with the authors themselves flagging bugs and in-flight fixes. The API surface is still churning.
3. **Features we don't need.** CLI, mining pipelines, onboarding wizard, MCP server, AAAK compression, LLM-based extractors — ~2500+ lines of code that would come along for the ride.
4. **Features that regress.** AAAK mode scores lower than raw mode; contradiction detection isn't wired into the KG yet. We'd want raw mode only, and we'd want to skip contradiction detection until it stabilizes.
5. **Tight integration cost with existing state.** The dashboard's HTTP panel API returns JSON from SQL queries against existing tables. With MemPalace, dashboard queries would need to read from ChromaDB as well. With pgvector, memory endpoints are just more SELECTs.

The core realization: **MemPalace's value is architectural patterns, not ChromaDB plumbing.** Those patterns — wing/room/hall taxonomy, temporal KG, specialist diaries, cross-wing tunnels — are all trivially expressible on postgres. The retrieval layer (vector search with metadata filter) is a pgvector capability.

---

## 3. Decision — Option C

We considered three paths. Summary:

| Option | Description | Runtime dep | Code to write | Storage surface | Upstream burden |
|---|---|---|---|---|---|
| **A** | Use MemPalace as-is (façade in orchestrator) | `mempalace`, `chromadb`, `sentence-transformers` | ~300 lines orchestrator glue | **Two** (postgres + ChromaDB) | **High** — track MemPalace releases, patch façade on breaking changes |
| **B** | Fork MemPalace, swap its storage layer for pgvector | Fork + our orchestrator code | ~300 orchestrator + ~800 fork patches | **One** (postgres) | **Medium** — rebase against upstream, re-apply patches |
| **C** | Build our own semantic store on pgvector, adapting MemPalace modules with attribution | `pgvector`, `sentence-transformers` | ~1070 new + ~1020 adapted | **One** (postgres) | **Low** — pgvector and sentence-transformers are very stable |

**We choose Option C.** Reasons:

1. **Single storage surface.** Operators already manage postgres, already run our migrations, already know how to back up / restore / inspect the schema. Memory becomes just more tables with the same lifecycle as `cloud_sessions`, `dispatch_sessions`, `kernel_files_sync`. The dashboard's existing HTTP panel API gets new `/api/cloud/memory/*` endpoints with zero new data-plumbing — they're just more SELECTs against tables in the same database.
2. **SQL joins across existing state.** We can write queries like "which dispatches emitted drawers mentioning 'stall watchdog'" as a single JOIN across `memory_drawers` + `dispatch_sessions` + `cloud_sessions`. With ChromaDB that's two queries across two stores and manual merging in Python.
3. **Benchmark parity preserved.** The 96.6% LongMemEval score comes from vector search over raw verbatim text with metadata filtering. pgvector does this with the same embedding model (`all-MiniLM-L6-v2`). No MemPalace-specific secret sauce is being given up at the retrieval layer.
4. **Maintenance burden approaches zero.** `pgvector` is maintained by the Supabase-adjacent ecosystem, at v0.7+, widely deployed in production. Version bumps are `ALTER EXTENSION vector UPDATE;`. The last backwards-incompatible change to the HNSW format was in pre-v0.5 days. `sentence-transformers` is also very stable; pinning the model name explicitly means our existing vectors stay valid across library bumps.
5. **No features we don't need.** We don't inherit CLI, mining, MCP server, onboarding, AAAK, contradiction detection, LLM-based extractors — ~2500+ lines of MemPalace we would've come along for the ride with Option A.
6. **We keep the patterns.** MemPalace's architectural design work — temporal validity edge cases in the KG, room/wing taxonomy, tunnel detection, dedup thresholds — is all preserved because we adapt those specific modules (§ 5) with attribution.

---

## 4. Architecture

### 4.1 High-level diagram

```
                              Anthropic Cloud
                              ───────────────
  Parent Kernel session ──── agent.message (may contain RECALL fences)
                    ▲                                │
                    │ user.message (RECALL_RESULT)   │ SSE
                    │                                ▼
  ┌─────────────────┴────────────────────────────────────────────────┐
  │ Operator machine                                                 │
  │                                                                  │
  │  ┌───────────────────────────────────────────────────────────┐   │
  │  │ orchestrator                                              │   │
  │  │                                                           │   │
  │  │   EventConsumer ── routes agent.message to memory         │   │
  │  │          │               ingester and recall broker       │   │
  │  │          ▼                                                │   │
  │  │   orchestrator/memory/                                    │   │
  │  │   ┌─────────────────────────────────────────────────────┐ │   │
  │  │   │ semantic_store.py  — pgvector + sentence-transform  │ │   │
  │  │   │ knowledge_graph.py — temporal KG on postgres        │ │   │
  │  │   │   (adapted from mempalace/knowledge_graph.py, MIT)  │ │   │
  │  │   │ palace_graph.py    — room/tunnel navigation         │ │   │
  │  │   │   (adapted from mempalace/palace_graph.py, MIT)     │ │   │
  │  │   │ dedup.py           — drawer deduplication           │ │   │
  │  │   │   (adapted from mempalace/dedup.py, MIT)            │ │   │
  │  │   │ layers.py          — L0/L1/L2/L3 wake-up composer   │ │   │
  │  │   │   (parts adapted from mempalace/layers.py, MIT)     │ │   │
  │  │   │ ingester.py        — orchestrator-side write path   │ │   │
  │  │   │ recall.py          — RECALL fence broker            │ │   │
  │  │   └─────────────────────────────────────────────────────┘ │   │
  │  │          │                                                │   │
  │  │          ▼ psycopg2 (shared with rest of orchestrator)    │   │
  │  └───────────────────────────────────────────────────────────┘   │
  │                                                                  │
  │  ┌────────────────────────────────────────────────────────────┐  │
  │  │ PostgreSQL  ora_kernel                                     │  │
  │  │                                                            │  │
  │  │   (existing)                                               │  │
  │  │     cloud_sessions, dispatch_sessions, dispatch_agents,    │  │
  │  │     kernel_files_sync, orch_activity_log, otel_*           │  │
  │  │                                                            │  │
  │  │   (new — migration 009_memory.sql)                         │  │
  │  │     memory_drawers        — vector(384) + JSONB metadata   │  │
  │  │     memory_kg_triples     — temporal entity graph          │  │
  │  │     memory_agent_diaries  — per-node diary entries         │  │
  │  └────────────────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────┘
```

### 4.2 Architectural invariants (preserved)

All five invariants already documented in `docs/CLOUD_ARCHITECTURE.md` § Architectural Invariants are preserved. A sixth is proposed:

| Invariant | How preserved |
|---|---|
| **I1: Container never speaks to postgres** | Memory is operator-side. Kernel never touches `memory_drawers`, the KG tables, or any memory-related storage. All reads/writes flow through the orchestrator. |
| **I2: Protocol teaching via BOOTSTRAP_PROMPT + send_protocol_refresh** | `RECALL_PROTOCOL` becomes a new constant in `session_manager.py` next to `SYNC_SNAPSHOT_PROTOCOL` and `DISPATCH_PROTOCOL`. Same pattern. |
| **I3: Case-insensitive tool name matching** | Not affected. Memory doesn't introduce new tool names. |
| **I4: No agent ever self-certifies work** | Memory is a retrieval index, not a verification mechanism. Axiom 2 is unaffected. |
| **I5 (new): Memory ingestion is eventually consistent; recall is best-effort** | Ingestion runs in a background thread so SSE loop is never blocked. Failures are logged + dropped. Recall failures produce `RECALL_RESULT status="error"` rather than silence, so the Kernel knows the index is unavailable and can decide how to proceed (Axiom 5 — no blind retry). |

### 4.3 The RECALL fence protocol

A new protocol constant in `session_manager.py` paralleling `DISPATCH_PROTOCOL` and `SYNC_SNAPSHOT_PROTOCOL`.

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

Exactly one of `query`, `kg_entity`, or `diary_agent` must be present:

| Field | Mode | Orchestrator action |
|---|---|---|
| `query` (string) | Semantic search | `SemanticStore.search(query, wing, room, limit)` |
| `kg_entity` (string) | Knowledge-graph | `KnowledgeGraph.query_entity(kg_entity, as_of=as_of)` |
| `diary_agent` (string) | Specialist diary | `SemanticStore.diary_read(diary_agent, last_n=limit)` |
| `wing`, `room`, `limit`, `as_of` | Filters | Passed through |

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
    {...}
  ]
}
```
````

Error case:

````
```RECALL_RESULT status=error mode=search
{
  "error": "Semantic store is not available on this orchestrator — recall is unavailable",
  "query": "what did we decide about dispatch timeouts"
}
```
````

The Kernel is explicitly taught that:
- `RECALL_RESULT status=error` is not a crash condition. The index is unavailable and the Kernel should fall back to its existing behavior (read WISDOM.md, ask the operator, or proceed without the memory).
- Per Axiom 5, a failed recall does not trigger a retry storm. The Kernel can retry with a different query or a different mode, but not the exact same recall.
- Recall is not a substitute for reasoning. Use it when you don't already know the answer from current session context or WISDOM.md.

### 4.4 Taxonomy defaults

**Wings** (one per project):
- `wing_ora_kernel_cloud` — this repo's internal memory: development decisions, architectural changes, bug fixes, design discussions
- `wing_<other_project>` — per PROJECT_DNA.md when the cloud Kernel is used on other operator projects
- `wing_system` — reserved for cross-project concerns (Anthropic API changes, postgres schema migrations, base `ora-kernel` upstream syncs)

**Canonical rooms** (project-independent memory categories; operators extend via config):
- `dispatch-subsystem` — dispatch broker, node specs, fence protocol
- `file-sync` — CDC, snapshot reconciliation, WISDOM.md evolution
- `ws-bridge` — dashboard bridge, HTTP API, protocol envelope
- `session-lifecycle` — resumes, protocol refreshes, container restarts
- `self-improvement` — tuning, refinement, consolidation cycles
- `hitl` — approvals, denials, escalation patterns
- `prompt-tuning` — CLAUDE.md / BOOTSTRAP_PROMPT / node spec edits
- `incidents` — stuck dispatches, orphaned sub-sessions, quota overruns
- `cost-analysis` — running cost patterns, budget planning
- `memory` — meta-room: ingest health, query patterns, index tuning

**Per-node diary agents:**
- `diary_business_analyst`, `diary_node_designer`, `diary_node_creator`, `diary_tuning_analyst`, etc. — one per system node that's been dispatched at least once. Auto-created on first dispatch completion.

The exact taxonomy is negotiable. Wing is configured statically (from `config.yaml`); room is derived from the message content via a simple keyword-match heuristic, with `room_general` as the fallback.

---

## 5. File-by-file adaptation analysis

This section captures the concrete engineering question: **what does the pgvector rebuild actually cost?** I read the key MemPalace source files and broke them down by reusability.

### 5.1 The reusable core

| MemPalace file | Lines | ChromaDB coupling | Verdict | Effort |
|---|---|---|---|---|
| **`mempalace/knowledge_graph.py`** | 393 | **None** (SQLite only) | **Adapt to postgres** | 2–3 hours |
| **`mempalace/palace_graph.py`** | 227 | Thin (one `col.get()` batch loop) | **Adapt to postgres** | 1–2 hours |
| **`mempalace/dedup.py`** | 239 | One similarity-check call | **Adapt to postgres** | 1 hour |
| **`mempalace/layers.py`** | 515 | Indirect (via `searcher`) | **Cherry-pick ~30–50%** | 2–4 hours |

**`knowledge_graph.py` is the biggest win.** I read the first third and confirmed the design: two tables (`entities`, `triples`), temporal validity via `valid_from`/`valid_to`, point-in-time queries, clean public API (`add_entity`, `add_triple`, `query_entity`, `invalidate`, `timeline`). The ChromaDB coupling is **zero** — it's a standalone SQLite-based module that happens to live in the MemPalace package.

Adaptation to postgres is mechanical:

| SQLite → postgres translation | Effort |
|---|---|
| `sqlite3.connect()` → orchestrator's `Database.cursor()` context manager | trivial |
| `?` placeholders → `%s` | global find-replace |
| `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (id) DO UPDATE SET ...` | a few lines per occurrence |
| `executescript(...)` → split into individual `execute()` calls | trivial |
| `PRAGMA journal_mode=WAL` → remove (postgres doesn't need it) | delete |
| `TEXT` columns holding JSON → `JSONB` (optional upgrade) | optional |
| `CURRENT_TIMESTAMP` default → identical in postgres | no change |
| Foreign keys, indexes, table shape → identical syntax | no change |

**Net:** ~400 lines of working temporal KG code at ~2 hours of translation work. The design work — deciding what counts as an entity, how invalidation interacts with point-in-time queries, what the predicate vocabulary looks like, how to normalize entity IDs, how to store confidence scores and source references — is all already done. That's the real value, not the line count.

**`palace_graph.py` is almost-free reuse.** It builds a graph by iterating drawer metadata and constructing `room → {wings, halls, count}` dicts, then walks that graph for `traverse` / `find_tunnels` / `graph_stats`. The only ChromaDB-specific line in the file is:

```python
batch = col.get(limit=1000, offset=offset, include=["metadatas"])
```

That becomes one postgres query:

```sql
SELECT wing, room, hall, metadata FROM memory_drawers;
```

Everything else in the 227 lines — edge construction, BFS traversal, tunnel-finding, stats computation — is pure Python operating on in-memory dicts. Direct port.

**`dedup.py` is mostly portable.** Hash-based exact-match detection for fast paths, similarity-based near-duplicate detection for semantic duplicates, threshold tuning, edge cases for empty/whitespace/short content. The similarity check uses ChromaDB; we swap it for a pgvector similarity query. Everything else ports directly.

**`layers.py` is partial reuse.** 515 lines is substantial. Without reading it in full, my structural guess:
- L0 (identity, ~50 lines) — file reading + text assembly. Portable.
- L1 (critical facts, ~150 lines) — queries the KG and searches the palace to compose the ~120-token wake-up string. Half portable, half needs new semantic store bindings.
- L2 (room recall, ~150 lines) — calls `searcher.search()` with filters. Needs our new backend.
- L3 (deep search, ~100 lines) — same.
- Plus utility functions for AAAK formatting (skip — we don't use AAAK) and token counting (keep).

**Realistic estimate:** cherry-pick ~200 lines, rewrite ~100 against our backend, drop ~200 lines of AAAK integration. Net ~300 lines in our tree adapted from ~515 upstream.

### 5.2 Reference-only (rewrite ourselves)

**`searcher.py` (152 lines)** is the ChromaDB wrapper we replace wholesale. I read it. The file is mostly print-formatting for a CLI search result — which we don't want anyway because the orchestrator returns structured JSON to the RECALL fence handler, not pretty terminal output. The actual ChromaDB API calls are ~20 lines: `chromadb.PersistentClient(...)`, `client.get_collection(...)`, `col.query(query_texts=[...], n_results=N, where={...}, include=[...])`.

Those ~20 lines become our pgvector equivalent:

```sql
SELECT content, wing, room, hall, metadata, created_at,
       1 - (embedding <=> %s::vector) AS score
FROM memory_drawers
WHERE (%s::text IS NULL OR wing = %s)
  AND (%s::text IS NULL OR room = %s)
ORDER BY embedding <=> %s::vector
LIMIT %s
```

So `searcher.py` is **reference only** — we'd read it to confirm we're matching the retrieval parameters MemPalace uses (n_results default, metadata filter composition with `$and`, cosine-distance-to-similarity conversion) but we don't copy any of the code. The 152 lines of MemPalace's searcher become ~60 lines of ours.

### 5.3 Definitely skip

| MemPalace file | Lines | Why we skip |
|---|---|---|
| `cli.py` | various | CLI is operator-facing; orchestrator is the only ingester |
| `miner.py`, `convo_miner.py` | various | We don't mine from disk; we ingest from SSE events |
| `onboarding.py` | various | Config via `config.yaml`, not wizards |
| `mcp_server.py` | various | Not using MCP — Kernel reaches memory via RECALL fences |
| `hooks_cli.py`, `instructions_cli.py` | various | Claude-Code-plugin-specific shell hooks |
| `migrate.py`, `repair.py` | various | We own our postgres schema |
| `normalize.py`, `split_mega_files.py` | various | Chat-export-normalizing; not our use case |
| `spellcheck.py` | various | Not needed for orchestrator content |
| `general_extractor.py` | various | LLM-based classifier; we route via metadata |
| `entity_detector.py`, `entity_registry.py`, `room_detector_local.py` | various | ML-based auto-detection; we use keyword heuristics initially |
| **`dialect.py` (AAAK)** | **1075** | Regresses vs raw mode. Skip entirely. |
| `config.py` | 209 | We have `orchestrator/config.py` already |

**Total skipped: ~2500+ lines.** All of it is code we'd pay installation + maintenance cost for with Option A.

### 5.4 Honest totals for Option C

| Bucket | Lines of Python |
|---|---|
| `orchestrator/memory/semantic_store.py` (new) | ~250 |
| `orchestrator/memory/knowledge_graph.py` (adapted from MemPalace, MIT) | ~400 |
| `orchestrator/memory/palace_graph.py` (adapted from MemPalace, MIT) | ~220 |
| `orchestrator/memory/dedup.py` (adapted from MemPalace, MIT) | ~200 |
| `orchestrator/memory/layers.py` (cherry-picked from MemPalace, MIT) | ~300 |
| `orchestrator/memory/ingester.py` (new) | ~250 |
| `orchestrator/memory/recall.py` (new) | ~200 |
| `orchestrator/memory/__init__.py` + module wiring (new) | ~50 |
| Migration `kernel-files/infrastructure/db/009_memory.sql` | ~40 |
| Hooks into existing `event_consumer.py`, `dispatch.py`, `__main__.py`, `session_manager.py`, `db.py` | ~100 |
| `RECALL_PROTOCOL` constant + protocol refresh update | ~80 |
| **New code subtotal** | **~1070** |
| **Adapted code subtotal** | **~1020** |
| **Tests (unit + integration)** | **~980** |
| **Grand total Python** | **~3070** |
| Documentation updates (`CLOUD_ARCHITECTURE.md`, `CHANGELOG.md`, `SECURITY.md`, `README.md`, `docs/next_steps.md`, `CONTRIBUTING.md`) | ~400 |

**Effort estimate:** 2–3 weeks of focused work. Tests and integration hooks are the time sink, not the core modules. The adapted-code subtotal is pattern-translation (mechanical), not design work.

### 5.5 Attribution mechanics

MIT permits copying with attribution. Concrete pattern:

**1. Each adapted file gets a header:**

```python
"""
orchestrator/memory/knowledge_graph.py — Temporal knowledge graph on postgres

Adapted from MemPalace (https://github.com/milla-jovovich/mempalace)
  Original: mempalace/knowledge_graph.py
  License: MIT, Copyright (c) 2026 Milla Jovovich & Ben Sigman
  Changes from upstream:
    - SQLite backend replaced with postgres via orchestrator/db.py wrapper
    - ? placeholders -> %s
    - INSERT OR REPLACE -> ON CONFLICT upsert
    - Connection management delegated to Database class
    - Removed sqlite3-specific PRAGMAs
    - JSON columns converted to JSONB
"""
```

**2. A new `NOTICES.md` at repo root** lists third-party code:

```markdown
# Third-Party Notices

## MemPalace

Files under `orchestrator/memory/` derived in part from MemPalace v3.1.0:
- https://github.com/milla-jovovich/mempalace
- License: MIT
- Copyright (c) 2026 Milla Jovovich & Ben Sigman

Specific files adapted:
- `orchestrator/memory/knowledge_graph.py` ← `mempalace/knowledge_graph.py`
- `orchestrator/memory/palace_graph.py` ← `mempalace/palace_graph.py`
- `orchestrator/memory/dedup.py` ← `mempalace/dedup.py`
- Portions of `orchestrator/memory/layers.py` ← `mempalace/layers.py`

Original license text preserved in `licenses/mempalace-LICENSE.txt`.
```

**3. `licenses/mempalace-LICENSE.txt`** holds the verbatim MIT license text from the upstream repo.

**4. `CHANGELOG.md` entry** credits them explicitly when the memory subsystem lands:

```markdown
## [2.1.0-cloud.1] — YYYY-MM-DD — Semantic memory on pgvector

Knowledge graph, palace graph, and dedup modules adapted from MemPalace
(milla-jovovich/mempalace, MIT) — their architectural design for
temporal entity-relationship triples and cross-wing room navigation
was the basis for our postgres implementation. Their benchmark work
(96.6% LongMemEval R@5 in raw vector mode) is what made us confident
vector search over raw verbatim text would meet our recall needs
before we started.
```

**5. Courtesy (not required):**
- Open an issue on their repo saying "we've adapted your KG + palace graph modules for a postgres backend in ora-kernel-cloud, here's the link." Their authors are clearly engaged (the April 7 note shows they respond to community criticism).
- Upstream any bug fixes we find during adaptation. If we discover `query_entity` has an off-by-one on the `valid_to` check, file a PR against their SQLite version.
- Don't advertise our work as "MemPalace integration" — "semantic memory inspired by MemPalace's architecture, built on postgres" is more accurate.

---

## 6. Phased Implementation Outline

**Not a plan.** This is a research-stage outline that feeds `SPEC-002-semantic-memory.md` and, in turn, the formal Phase 1 plan (written via `superpowers:writing-plans`).

### Phase 1 — Schema + semantic store + ingest

**Goal:** Every `agent.message` and every dispatch outcome lands in `memory_drawers` as a vector-indexed row. KG triples accumulate from dispatch lifecycles. Specialist diaries accumulate from completed dispatches. **Nothing queries yet.**

**Scope:**
1. Migration `kernel-files/infrastructure/db/009_memory.sql` — schema for `memory_drawers`, `memory_kg_triples`, `memory_agent_diaries` (+ pgvector extension).
2. `orchestrator/memory/semantic_store.py` — new pgvector + sentence-transformers wrapper.
3. `orchestrator/memory/knowledge_graph.py` — adapted from MemPalace, postgres backend.
4. `orchestrator/memory/dedup.py` — adapted from MemPalace, pgvector backend.
5. `orchestrator/memory/ingester.py` — new, background thread + work queue + `EventConsumer` / `DispatchManager` hooks.
6. `orchestrator/memory/__init__.py` + module wiring.
7. `event_consumer.py` hook: file an `agent.message` drawer per message.
8. `dispatch.py` hook: file a dispatch drawer, write KG triples, write diary entry per completed dispatch.
9. Optional dep: `requirements-memory.txt` pinning `pgvector`, `sentence-transformers`, model downloader.
10. Unit tests with a real postgres + real sentence-transformers (integration style, same pattern as existing `test_db_dispatch.py`).
11. Live smoke test: run orchestrator for a minute, confirm rows in `memory_drawers` / `memory_kg_triples` / `memory_agent_diaries`.

**Non-goals:** No RECALL fence handling yet. No BOOTSTRAP_PROMPT changes. No wake-up layer. No HTTP panel API for memory.

### Phase 2 — RECALL fence protocol

**Goal:** The Kernel can ask the orchestrator to search the palace and get results back in the next turn.

**Scope:**
1. `RECALL_PROTOCOL` constant in `session_manager.py`.
2. `BOOTSTRAP_PROMPT` embeds it.
3. `send_protocol_refresh` includes it.
4. `orchestrator/memory/recall.py` — `RecallBroker` class, `parse_recall_fences` pure function.
5. `EventConsumer._handle_message` routes RECALL fences to the broker.
6. Broker calls `SemanticStore.search(...)` / `KnowledgeGraph.query_entity(...)` / `SemanticStore.diary_read(...)` depending on mode.
7. Broker formats `RECALL_RESULT` fence and calls `session_mgr.send_message(...)` to deliver it.
8. Unit tests with mocks. Live smoke test: send a dispatch task that asks the Kernel to recall something, observe the round-trip in `orch_activity_log`.

**Non-goals:** Wake-up layer (Phase 3). Dashboard integration (future).

### Phase 3 — Wake-up layer

**Goal:** Every new session starts with a compact ~170-token "here's what you already know" prelude derived from memory. Saves round-trips for basic facts.

**Scope:**
1. `orchestrator/memory/layers.py` — adapted from MemPalace, with our backend bindings.
2. `SessionManager.bootstrap()` calls `layers.build_wake_up(wing=<project>)` and injects the result into a new `{memory_wake_up}` placeholder in `BOOTSTRAP_PROMPT`.
3. Staleness guard: if the semantic store is unavailable, the placeholder is empty.

### Phase 4 — KG queries via RECALL

**Goal:** Point-in-time questions ("what was business_analyst doing 2 days ago?") and timelines ("chronological story of dispatch-subsystem changes").

**Scope:** RECALL protocol supports `kg_entity` and `as_of` modes. `RecallBroker` calls `kg.query_entity(...)` and `kg.timeline(...)`.

### Phase 5 — Diary queries + graph traversal

**Goal:** Specialist nodes' recent history is queryable before re-dispatching them.

**Scope:** `diary_agent` mode, `traverse` mode, `find_tunnels` mode. Self-improvement cycle integration: when `/self-improve` fires, the Kernel can use diary queries to see each node's recent history without re-dispatching any of them.

### Phase 6 — Dashboard memory panel (forex-ml-platform, separate repo)

**Goal:** Dashboard tab surfaces memory state: recent drawers, recent RECALL calls, KG entity timelines, ingest health.

**Scope:** Out of this repo. Documented as a follow-up for the forex-ml-platform Phase B plan. New HTTP panel endpoints in `orchestrator/http_api.py`: `/api/cloud/memory/health`, `/api/cloud/memory/recent`, `/api/cloud/memory/stats`.

---

## 7. Risks and Mitigations

### 7.1 pgvector version compatibility

**Risk:** A breaking change in pgvector could affect the `memory_drawers` index format or the `<=>` operator semantics.

**Mitigations:**
- pgvector is at v0.7+ (2024/2025), widely deployed, stable. Version bumps are `ALTER EXTENSION vector UPDATE;`. The last backwards-incompatible HNSW format change was in pre-v0.5 days.
- Pin the extension version in our migration script.
- Add a `SELECT vector_version();` health check to the orchestrator's startup path, log the version, fail gracefully on mismatch.

### 7.2 sentence-transformers model pinning

**Risk:** A library bump changes the default model or tokenizer, invalidating existing embeddings.

**Mitigations:**
- Pin the model name explicitly in `config.yaml` — `all-MiniLM-L6-v2` has been stable for years.
- Store the model name in a metadata column on `memory_drawers` so we can detect mismatches if the operator ever changes the model mid-corpus.
- If the operator ever *wants* to switch models, document a re-embedding procedure: `SELECT id, content FROM memory_drawers; UPDATE embedding = new_model.encode(content);`. Slow but tractable.

### 7.3 Embedding model footprint

**Risk:** `all-MiniLM-L6-v2` is ~90MB on first download. Plus transformers + torch runtime. First-run experience has a network dependency.

**Mitigations:**
- Make the memory subsystem **optional**. `requirements-memory.txt` is separate from `requirements.txt`. Operators who don't want it don't pay the cost.
- Ship an install check: `python3 -m orchestrator.memory.check_install` pre-downloads the model and runs a smoke test.
- Document the disk/RAM impact in `docs/API_KEY_SETUP.md` § Cost Model.

### 7.4 ChromaDB footprint (non-risk for Option C)

Worth calling out explicitly: **Option C has no ChromaDB dependency.** The ~200MB persistent directory, per-operator backup strategy, and ChromaDB version-compatibility concerns from Option A are gone.

### 7.5 SSE-loop blocking from ingest

**Risk:** pgvector writes + sentence-transformers encoding are synchronous and not instantaneous. An ingest-per-agent-message call inline on the SSE thread could slow the loop.

**Mitigations:**
- **Background ingestion thread.** One daemon thread per orchestrator boot that owns the semantic store client and reads from a bounded `queue.Queue`. The SSE loop pushes to the queue (non-blocking) and returns immediately. Overflow is logged and dropped (never blocks).
- **Failure isolation.** Every semantic store call is `try/except Exception; logger.exception(...)` — matching the established pattern for `file_sync`, `dispatch_manager`, and `ws_bridge` calls in `EventConsumer`.
- **Health endpoint.** New HTTP panel endpoint `/api/cloud/memory/health` reports ingest queue depth, last successful ingest timestamp, recent errors.

### 7.6 Content privacy and secrets

**Risk:** `agent.message` text may contain sensitive data: source code, config fragments, the operator's API key if it ever leaks into a message. `memory_drawers` stores verbatim text with embeddings.

**Mitigations:**
- Postgres database is already operator-local; memory doesn't change the trust boundary.
- **Ingest-time redaction** (should be a Phase 1 feature, not deferred): regex sweep before filing a drawer for `sk-ant-[A-Za-z0-9_\-]{80,}`, `sk-[A-Za-z0-9]{40,}`, `postgres://[^@]+:[^@]+@`, etc. Replace with `[REDACTED]`. Applied to both content and the stored embedding's source text.
- **Per-session opt-out.** Operator can set `config.memory.enabled: false` for debugging sessions with real credentials in context.
- **Write-ahead log.** Mirror MemPalace's WAL pattern — every write operation logged to a JSONL file before execution for audit/rollback.

### 7.7 Kernel over-reliance on RECALL

**Risk:** The Kernel may learn to call RECALL for every question instead of using its in-session context. Inflates token costs and latency.

**Mitigations:**
- Explicit protocol language: *"Use RECALL only when you don't already know the answer from your current session context or WISDOM.md."*
- Observable frequency: add RECALL-call count to `SYSTEM_STATUS` broadcasts. Dashboard surfaces it.
- Soft cap per session (e.g., 20 recalls) with a warning in `RECALL_RESULT` when the cap is approaching.

### 7.8 Protocol drift on resumed sessions

**Risk:** Same drift problem we already solved for SYNC and DISPATCH — resumed sessions don't re-read `BOOTSTRAP_PROMPT`, so orchestrator updates to `RECALL_PROTOCOL` would silently desync.

**Mitigation:** Reuse the existing `send_protocol_refresh()` mechanism. On every orchestrator boot against a resumed session, send the full current SYNC + DISPATCH + RECALL protocols. Pattern is already working; adding a third protocol is trivial.

### 7.9 Corpus bootstrapping

**Risk:** A fresh install has an empty corpus. The Kernel can't recall things that happened before memory was turned on.

**Mitigations:**
- **One-time backfill script** that reads existing `orch_activity_log` rows and files them as drawers under `wing_ora_kernel_cloud` with metadata-derived rooms. Converts the existing append-only archive into a searchable corpus without any loss. Not Phase 1 — a Phase 1.5 migration utility.
- **Optional backfill from `kernel_files_sync`.** Past WISDOM.md versions and journal entries become drawers under `hall_discoveries`.

### 7.10 What happens when the memory subsystem is absent

**Risk:** Operators run the orchestrator without installing `requirements-memory.txt`. Ingestion silently skips, RECALL fences silently fail.

**Mitigations:**
- `RECALL_RESULT status=error` on every recall when the subsystem is missing, with a clear explanation in the payload. The Kernel sees the failure and responds to the operator: *"I tried to recall that but long-term memory isn't installed on this orchestrator."*
- Startup log: `"Memory: semantic store enabled (pgvector)"` or `"Memory: optional dependencies not installed — ingestion and recall disabled"`.
- Dashboard memory panel (future): shows memory status so operators have single-glance visibility.

### 7.11 Upstream MemPalace maintenance

**Risk (much reduced vs Option A):** If we want to pick up upstream improvements (e.g., a better dedup algorithm), we have to manually port them.

**Mitigation:** Low-frequency manual sync. Read upstream changelogs occasionally. Port useful improvements individually. This is a pull model, not a tight coupling — we choose when to adopt changes.

---

## 8. Open Questions (for the spec phase)

### 8.1 Scope of ingestion

Do we ingest *every* `agent.message`, or only filtered content (completed dispatches, long messages, messages containing DISPATCH/SYNC fences)?

**Preliminary answer:** Every message, with aggressive deduplication. `dedup.py` (threshold 0.85–0.90) naturally suppresses routine chatter.

### 8.2 Wing-per-project vs wing-per-session

**Preliminary answer:** Wing per project, session tagged in drawer metadata. Operators can filter by session_id if they need isolation.

### 8.3 Room detection

Static keyword-match heuristic vs MemPalace's `room_detector_local.py` ML-based detector?

**Preliminary answer:** Static keyword map to start. ML-based detection is a Phase 2+ enhancement if needed.

### 8.4 KG triple vocabulary from dispatch events

**Draft:**
- `(node_name, "dispatched_for", task_id_or_sub_session_id, valid_from=start_ts)`
- `(node_name, "completed", sub_session_id, valid_from=end_ts)` — invalidates the previous triple
- `(node_name, "failed_with", error_class, valid_from=end_ts)` — failure only
- `(node_name, "cost_usd", float_value, valid_from=end_ts)` — for analytics
- `(parent_session, "dispatched", sub_session, valid_from=start_ts)`

### 8.5 Embedding model choice

`all-MiniLM-L6-v2` (384-dim, 90MB, fast) is the default and aligns with what MemPalace uses. Alternatives (e.g., `all-mpnet-base-v2` — 768-dim, 420MB, higher quality) would give better retrieval at cost.

**Preliminary answer:** Start with `all-MiniLM-L6-v2`. Benchmark after Phase 1. Swap to a larger model if recall quality is measurably insufficient.

### 8.6 Dashboard memory panel

Out of scope for this research. Noted as Phase 6 in § 6; the forex-ml-platform Phase B plan will cover it.

### 8.7 Shared vs isolated palace across operator projects

**Preliminary answer:** Single `ora_kernel` database shared across projects. Wings partition per project. Operators can filter by wing when querying across projects.

---

## 9. Out of Scope

- **Exact version pins.** Waiting for pgvector stability confirmation and sentence-transformers model benchmark.
- **Upstream contributions to MemPalace.** If we find bugs during adaptation, we'll upstream-patch as a courtesy, but that's not a project deliverable.
- **Retention / garbage collection.** Drawers live forever by default. We'll need a policy eventually, but not in Phase 1.
- **Multi-operator support.** Single-operator by design for the foreseeable future.
- **Benchmarking against LongMemEval.** MemPalace's published 96.6% is our prior; running our own LongMemEval reproduction is future validation work, not Phase 1.

---

## 10. References

- **Upstream repo:** https://github.com/milla-jovovich/mempalace (v3.1.0, MIT)
- **Upstream honesty note:** MemPalace README § "A Note from Milla & Ben — April 7, 2026"
- **LongMemEval benchmark paper:** Cited in the upstream README as the source of the 96.6% raw-mode score
- **pgvector:** https://github.com/pgvector/pgvector — the postgres extension this implementation depends on
- **sentence-transformers:** https://www.sbert.net — the library for local embedding generation
- **ora-kernel-cloud architectural invariants:** `docs/CLOUD_ARCHITECTURE.md` § Architectural Invariants
- **Fence protocols already in place:** `orchestrator/session_manager.py` (`SYNC_SNAPSHOT_PROTOCOL`, `DISPATCH_PROTOCOL`), `orchestrator/file_sync.py` (`parse_sync_fences`), `orchestrator/dispatch.py` (`parse_dispatch_fences`)
- **Formal spec (this research's output):** `docs/specs/SPEC-002-semantic-memory.md`

---

## Appendix A — Migration SQL sketch

```sql
-- kernel-files/infrastructure/db/009_memory.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Drawers: the raw verbatim memory ────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_drawers (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content        TEXT NOT NULL,
    content_hash   TEXT NOT NULL,   -- sha256 for exact-duplicate detection
    embedding      vector(384) NOT NULL,  -- all-MiniLM-L6-v2 dimension
    embedding_model TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2',

    -- Taxonomy
    wing           TEXT NOT NULL,
    room           TEXT,
    hall           TEXT,

    -- Source linkage
    session_id     TEXT,   -- cloud_sessions.session_id or NULL
    source_type    TEXT,   -- 'agent_message', 'dispatch_result', 'backfill', etc.
    source_ref     TEXT,   -- e.g. orch_activity_log id or dispatch_sessions.sub_session_id

    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX memory_drawers_embedding_idx
    ON memory_drawers
    USING hnsw (embedding vector_cosine_ops);

CREATE INDEX memory_drawers_wing_room_idx   ON memory_drawers(wing, room);
CREATE INDEX memory_drawers_session_idx     ON memory_drawers(session_id);
CREATE INDEX memory_drawers_hash_idx        ON memory_drawers(content_hash);
CREATE INDEX memory_drawers_source_ref_idx  ON memory_drawers(source_type, source_ref);
CREATE INDEX memory_drawers_created_idx     ON memory_drawers(created_at DESC);


-- ── Temporal knowledge graph (adapted from mempalace/knowledge_graph.py) ──

CREATE TABLE IF NOT EXISTS memory_kg_entities (
    id           TEXT PRIMARY KEY,   -- normalized entity ID (lowercase, underscores)
    name         TEXT NOT NULL,
    entity_type  TEXT NOT NULL DEFAULT 'unknown',
    properties   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_kg_triples (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject           TEXT NOT NULL REFERENCES memory_kg_entities(id),
    predicate         TEXT NOT NULL,
    object            TEXT NOT NULL REFERENCES memory_kg_entities(id),
    valid_from        TIMESTAMPTZ NOT NULL,
    valid_to          TIMESTAMPTZ,   -- NULL = still valid
    confidence        REAL NOT NULL DEFAULT 1.0,
    source_drawer_id  UUID REFERENCES memory_drawers(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX memory_kg_subject_idx   ON memory_kg_triples(subject);
CREATE INDEX memory_kg_object_idx    ON memory_kg_triples(object);
CREATE INDEX memory_kg_predicate_idx ON memory_kg_triples(predicate);
CREATE INDEX memory_kg_valid_idx     ON memory_kg_triples(valid_from, valid_to);


-- ── Per-agent specialist diaries ────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_agent_diaries (
    id         BIGSERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    entry      TEXT NOT NULL,
    topic      TEXT,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX memory_diary_agent_idx ON memory_agent_diaries(agent_name, created_at DESC);
CREATE INDEX memory_diary_topic_idx ON memory_agent_diaries(topic);
```

## Appendix B — `orchestrator/memory/semantic_store.py` skeleton (for discussion)

```python
"""
orchestrator/memory/semantic_store.py — pgvector-backed semantic store

Thin wrapper over pgvector + sentence-transformers. The orchestrator's
Database connection is shared (no separate client). Embedding model is
loaded once per process.

The cloud Kernel never touches this class directly. Ingestion comes
from MemoryIngester (called by EventConsumer / DispatchManager);
queries come from RecallBroker (called by EventConsumer._handle_message
when a RECALL fence is parsed).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SemanticStore:
    """pgvector + sentence-transformers wrapper.

    Thread-safety: the embedding model is thread-safe for encoding.
    The db connection is shared with the rest of the orchestrator
    (psycopg2 + autocommit), so we use the existing Database.cursor()
    context manager for every call.
    """

    def __init__(self, db, model_name: str = "all-MiniLM-L6-v2"):
        self.db = db
        self.model_name = model_name
        self._model = None  # lazy

    def _ensure_model(self):
        if self._model is None:
            # Lazy import so orchestrator-without-memory doesn't pay the cost
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _embed(self, text: str):
        return self._ensure_model().encode(text, convert_to_numpy=True)

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ── Write ────────────────────────────────────────────────────────

    def add_drawer(
        self,
        content: str,
        wing: str,
        room: Optional[str] = None,
        hall: Optional[str] = None,
        session_id: Optional[str] = None,
        source_type: Optional[str] = None,
        source_ref: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Add a drawer, with duplicate detection.

        Returns the drawer UUID, or None if duplicate detected.
        """
        embedding = self._embed(content)
        content_hash = self._hash(content)

        # Exact-duplicate check
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT id FROM memory_drawers WHERE content_hash = %s LIMIT 1",
                (content_hash,),
            )
            existing = cur.fetchone()
            if existing:
                return None

            cur.execute(
                """
                INSERT INTO memory_drawers
                    (content, content_hash, embedding, embedding_model,
                     wing, room, hall, session_id, source_type, source_ref, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    content, content_hash, embedding.tolist(), self.model_name,
                    wing, room, hall, session_id, source_type, source_ref,
                    json.dumps(metadata or {}),
                ),
            )
            return str(cur.fetchone()["id"])

    # ── Read ─────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Semantic search with optional wing/room filters."""
        query_embedding = self._embed(query).tolist()

        where_clauses = []
        params: List[Any] = [query_embedding]
        if wing is not None:
            where_clauses.append("wing = %s")
            params.append(wing)
        if room is not None:
            where_clauses.append("room = %s")
            params.append(room)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(query_embedding)
        params.append(limit)

        sql = f"""
            SELECT id, content, wing, room, hall, metadata, created_at,
                   1 - (embedding <=> %s::vector) AS score
            FROM memory_drawers
            {where_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    # ── Diary helpers (trivial; agent_diaries is a plain append-only table) ──

    def diary_write(self, agent_name: str, entry: str, topic: Optional[str] = None):
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_agent_diaries (agent_name, entry, topic) VALUES (%s, %s, %s)",
                (agent_name, entry, topic),
            )

    def diary_read(self, agent_name: str, last_n: int = 10) -> List[Dict[str, Any]]:
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT entry, topic, created_at
                FROM memory_agent_diaries
                WHERE agent_name = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (agent_name, last_n),
            )
            return list(cur.fetchall())
```

## Appendix C — Adapted-file source citations

For each adapted MemPalace file, the upstream path and our adaptation target:

| Upstream (MIT, Copyright (c) 2026 Milla Jovovich & Ben Sigman) | Adaptation | Status |
|---|---|---|
| `mempalace/knowledge_graph.py` (393 lines) | `orchestrator/memory/knowledge_graph.py` | Adapt; postgres backend |
| `mempalace/palace_graph.py` (227 lines) | `orchestrator/memory/palace_graph.py` | Adapt; postgres backend |
| `mempalace/dedup.py` (239 lines) | `orchestrator/memory/dedup.py` | Adapt; pgvector similarity |
| `mempalace/layers.py` (515 lines, partial) | `orchestrator/memory/layers.py` | Cherry-pick wake-up composition |
| `mempalace/searcher.py` (152 lines) | `orchestrator/memory/semantic_store.py` (new, reference-only) | Reference retrieval parameters |

---

*End of research document. Next: `docs/specs/SPEC-002-semantic-memory.md` — the formal spec that Phase 1's implementation plan will reference.*
