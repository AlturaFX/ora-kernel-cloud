# SPEC-002: Semantic Memory Subsystem

**Status:** Draft, approved scope. Ready for Phase 1 implementation plan via `superpowers:writing-plans`.
**Created:** 2026-04-10
**Author:** AlturaFX + Claude Opus 4.6
**Sources:**
- `docs/research/2026-04-10-mempalace-memory-integration.md` — full research that fed this spec
- Upstream prior art: https://github.com/milla-jovovich/mempalace (v3.1.0, MIT)

**Repo:** `ora-kernel-cloud` (this repo)
**Related:** `SPEC-001-managed-agent-cloud-fork.md` (the base fork spec); `docs/CLOUD_ARCHITECTURE.md` (current architectural state)

---

## Goal

Give the cloud Kernel **semantic recall across sessions** by adding a postgres-backed semantic memory subsystem on the orchestrator side. The Kernel reaches memory exclusively through a new `RECALL` fence protocol parallel to the existing `SYNC` and `DISPATCH` protocols. Everything runs in the existing `ora_kernel` postgres database — no new storage engine, no new runtime service, no cloud dependency.

### Problem this solves

The cloud Kernel cannot answer:
- "Have we dispatched business_analyst for this kind of task before? What was the outcome?"
- "Last week we debugged a dispatch timeout — what did we end up deciding?"
- "When did we first add the DISPATCH fence protocol, and what was the motivation?"
- "Find me any session where Axiom 2 was escalated to HITL."

These are semantic-search questions across the append-only conversation archive in `orch_activity_log`. The archive has the raw material; there's no index from a question to the relevant slice. This spec builds that index.

### Inspired by, not dependent on

MemPalace (milla-jovovich/mempalace, MIT, v3.1.0, April 2026) pioneered the architectural patterns — wings/rooms/halls taxonomy, temporal knowledge graph, specialist agent diaries, cross-wing tunnels — and achieved 96.6% LongMemEval R@5 in raw vector mode on 500 questions. We adapt specific modules from their codebase (`knowledge_graph.py`, `palace_graph.py`, `dedup.py`, parts of `layers.py`) with full MIT attribution. We **do not** take ChromaDB as a runtime dependency: the vector search + metadata filter capability we need is native to `pgvector`, and everything else (the palace taxonomy, temporal KG, diaries, tunnels) is trivially expressible on postgres tables.

---

## Success Criteria

Phase 1 is complete when all of the following are true:

- [ ] `kernel-files/infrastructure/db/009_memory.sql` creates `memory_drawers`, `memory_kg_entities`, `memory_kg_triples`, `memory_agent_diaries` with the schemas defined in § Contracts below.
- [ ] `pgvector` extension is installed and the `memory_drawers` HNSW index is built.
- [ ] `orchestrator/memory/semantic_store.py` exposes `add_drawer`, `search`, `diary_write`, `diary_read` methods tested against a real postgres.
- [ ] `orchestrator/memory/knowledge_graph.py` (adapted from MemPalace) exposes `add_entity`, `add_triple`, `query_entity`, `invalidate`, `timeline` — tested against a real postgres.
- [ ] `orchestrator/memory/dedup.py` (adapted from MemPalace) exposes deduplication helpers — tested against a real postgres.
- [ ] `orchestrator/memory/ingester.py` provides a `MemoryIngester` class that runs a background worker thread consuming a bounded `queue.Queue` and calls `semantic_store.add_drawer(...)` / `knowledge_graph.add_triple(...)` / `semantic_store.diary_write(...)` based on the job type.
- [ ] `EventConsumer` accepts an optional `memory: Optional["MemoryIngester"] = None` kwarg and enqueues a drawer job per `agent.message` event. Optional-dependency path is tested — `memory=None` works unchanged.
- [ ] `DispatchManager` accepts the same kwarg and enqueues a drawer + KG triples + diary entry per completed dispatch.
- [ ] Optional-dependency plumbing: `requirements-memory.txt` pins `pgvector`, `sentence-transformers`, and the embedding-model version. The orchestrator starts cleanly when the deps are missing and logs `Memory: optional dependencies not installed — ingestion and recall disabled`.
- [ ] `NOTICES.md` at repo root + `licenses/mempalace-LICENSE.txt` + per-file attribution headers on every adapted file.
- [ ] All unit and integration tests pass. Target: **+30 tests** on top of the current 130-test baseline.
- [ ] A live smoke test against a running orchestrator shows drawers appearing in `memory_drawers` within seconds of their `agent.message` events and KG triples appearing per dispatch lifecycle.

Phase 2 is complete when:

- [ ] `RECALL_PROTOCOL` constant in `session_manager.py`.
- [ ] `BOOTSTRAP_PROMPT` embeds it and `send_protocol_refresh` includes it.
- [ ] `orchestrator/memory/recall.py` with `parse_recall_fences` (pure function) + `RecallBroker` class.
- [ ] `EventConsumer._handle_message` routes RECALL fences to the broker, same pattern as SYNC and DISPATCH.
- [ ] RECALL fences with `query`, `kg_entity`, and `diary_agent` modes all work.
- [ ] Errors are surfaced as `RECALL_RESULT status=error` fences — never silent.
- [ ] Live smoke test against a running orchestrator shows round-trip: Kernel emits `RECALL` → orchestrator returns `RECALL_RESULT` in the next turn.

Phases 3–6 each get their own success criteria when we plan them individually.

---

## Scope

### In scope (Phases 1–2, this spec)

1. **Schema migration** (`009_memory.sql`) — three new tables + pgvector extension + HNSW index
2. **Semantic store** (`orchestrator/memory/semantic_store.py`) — pgvector + sentence-transformers wrapper
3. **Temporal knowledge graph** (`orchestrator/memory/knowledge_graph.py`) — adapted from MemPalace, postgres backend
4. **Deduplication** (`orchestrator/memory/dedup.py`) — adapted from MemPalace
5. **Ingest pipeline** (`orchestrator/memory/ingester.py`) — background thread, bounded queue, EventConsumer + DispatchManager hooks
6. **RECALL fence protocol** (Phase 2) — new constant in `session_manager.py`, new broker module, `EventConsumer._handle_message` routing
7. **Attribution** — `NOTICES.md`, `licenses/mempalace-LICENSE.txt`, per-file headers

### Out of scope (deferred to later phases)

- **Wake-up layer** (L0+L1 in `BOOTSTRAP_PROMPT`) — Phase 3
- **KG query modes** (`kg_entity`, `as_of`) beyond the structural pieces — Phase 4
- **Diary reads + graph traversal** via RECALL — Phase 5
- **Dashboard memory panel** (forex-ml-platform) — Phase 6
- **Corpus bootstrapping** (backfill from `orch_activity_log`) — Phase 1.5 utility, not core Phase 1
- **Retention / garbage collection** — not needed until memory grows
- **Multi-operator support** — single-operator by design for the foreseeable future
- **Benchmarking against LongMemEval** — MemPalace's 96.6% is our prior; we don't reproduce it as a Phase 1 deliverable

### Explicitly NOT doing

- Using `mempalace` as a runtime package. We adapt specific source files with attribution.
- Taking ChromaDB as a dependency. pgvector does the same job in the database we already run.
- Using MCP to talk to memory. The Kernel reaches memory via RECALL fences; the orchestrator implements the query path in Python.
- Using AAAK compression. Regresses vs raw mode per upstream benchmarks.
- LLM-based extraction of facts/decisions/preferences from content. Ingestion is raw + metadata-tagged only.
- Auto-detecting entities and rooms from message content via ML. Keyword heuristics only in Phase 1.

---

## Constraints

- **Python 3.10+** — already a project requirement
- **PostgreSQL 14+** with `pgvector` extension installable (v0.7+ recommended)
- **`sentence-transformers` >= 2.0** for local embedding generation. Default model: `all-MiniLM-L6-v2` (384-dim, ~90MB download, MIT).
- **Optional dependency pattern** — the memory subsystem is opt-in via `pip install -r requirements-memory.txt`. The orchestrator must continue to run with zero behavioral changes when memory deps are absent.
- **No new background services** — everything runs inside the existing orchestrator process. No Redis, no separate worker daemon, no message broker.
- **Invariant I1 preserved** — Managed Agent container never touches memory storage. All reads and writes flow through the orchestrator.
- **Invariant I2 preserved** — protocol teaching via `BOOTSTRAP_PROMPT` + `send_protocol_refresh`. No edits to protected `kernel-files/CLAUDE.md`.
- **Axiom 1 (Observable State)** — every write to memory produces an `orch_activity_log` entry (action `MEMORY_INGEST`) or a metric that the HTTP panel API surfaces. The operator can see what landed.
- **Axiom 5 (Entropy)** — a failed recall is surfaced as `RECALL_RESULT status=error` so the Kernel can adapt; recall failures never trigger silent retries.

---

## Dependencies

| Dependency | Version | Notes |
|---|---|---|
| `pgvector` (postgres extension) | >= 0.7 | Installed via `CREATE EXTENSION vector;` in the migration |
| `pgvector` (Python client bindings for psycopg2) | >= 0.3 | PyPI package for vector type registration |
| `sentence-transformers` | >= 2.7 | Local embedding generation |
| `all-MiniLM-L6-v2` | fixed | Embedding model, downloaded on first use |
| `psycopg2-binary` | already installed | Existing orchestrator dependency |
| `mempalace` | **not a runtime dep** | Source-code reference only, MIT, adapted with attribution |

All new deps go in `requirements-memory.txt`, not `requirements.txt`. The main `requirements.txt` stays clean so operators who don't want memory don't pay the installation cost.

### Postgres version check at startup

Orchestrator logs on startup:

```
Memory: pgvector <version> detected in database 'ora_kernel'
Memory: embedding model all-MiniLM-L6-v2 loaded (384-dim)
Memory: ingest queue ready, worker thread started
```

or, if deps are missing:

```
Memory: optional dependencies not installed — ingestion and recall disabled
        Install with: pip install -r requirements-memory.txt
```

---

## Contracts & Interfaces

### 1. Database schema (`009_memory.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Drawers: raw verbatim memory content ────────────────────────────

CREATE TABLE IF NOT EXISTS memory_drawers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,              -- sha256 for exact-duplicate detection
    embedding       vector(384) NOT NULL,       -- all-MiniLM-L6-v2 dimension
    embedding_model TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2',

    -- Taxonomy
    wing            TEXT NOT NULL,
    room            TEXT,
    hall            TEXT,

    -- Source linkage
    session_id      TEXT,                       -- cloud_sessions.session_id or NULL
    source_type     TEXT,                       -- 'agent_message' | 'dispatch_result' | 'backfill' | etc.
    source_ref      TEXT,                       -- orch_activity_log id / dispatch_sessions.sub_session_id

    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX memory_drawers_embedding_idx
    ON memory_drawers
    USING hnsw (embedding vector_cosine_ops);

CREATE INDEX memory_drawers_wing_room_idx   ON memory_drawers(wing, room);
CREATE INDEX memory_drawers_session_idx     ON memory_drawers(session_id);
CREATE INDEX memory_drawers_hash_idx        ON memory_drawers(content_hash);
CREATE INDEX memory_drawers_source_ref_idx  ON memory_drawers(source_type, source_ref);
CREATE INDEX memory_drawers_created_idx     ON memory_drawers(created_at DESC);


-- ── Temporal knowledge graph ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_kg_entities (
    id           TEXT PRIMARY KEY,              -- lowercase, underscore-separated
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
    valid_to          TIMESTAMPTZ,              -- NULL = still valid
    confidence        REAL NOT NULL DEFAULT 1.0,
    source_drawer_id  UUID REFERENCES memory_drawers(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX memory_kg_subject_idx    ON memory_kg_triples(subject);
CREATE INDEX memory_kg_object_idx     ON memory_kg_triples(object);
CREATE INDEX memory_kg_predicate_idx  ON memory_kg_triples(predicate);
CREATE INDEX memory_kg_valid_idx      ON memory_kg_triples(valid_from, valid_to);


-- ── Per-agent specialist diaries ────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_agent_diaries (
    id         BIGSERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    entry      TEXT NOT NULL,
    topic      TEXT,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX memory_diary_agent_idx  ON memory_agent_diaries(agent_name, created_at DESC);
CREATE INDEX memory_diary_topic_idx  ON memory_agent_diaries(topic);
```

**Schema invariants:**
- `embedding_model` on `memory_drawers` tracks which model was used so we can detect cross-model mixing later.
- `content_hash` enables O(1) exact-duplicate rejection before computing an embedding.
- `source_type` + `source_ref` give a traceable link back to the originating orch_activity_log row or dispatch_session row.
- `memory_kg_triples.source_drawer_id` creates bidirectional provenance: every fact has a text witness.

### 2. `SemanticStore` public API (Python)

```python
class SemanticStore:
    def __init__(self, db: Database, model_name: str = "all-MiniLM-L6-v2"): ...

    # Write
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
        """Insert a drawer with exact-duplicate detection. Returns UUID or None if dup."""

    # Read
    def search(
        self,
        query: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]: ...

    # Diaries
    def diary_write(self, agent_name: str, entry: str, topic: Optional[str] = None) -> None: ...
    def diary_read(self, agent_name: str, last_n: int = 10) -> List[Dict[str, Any]]: ...
```

### 3. `KnowledgeGraph` public API (Python, adapted from MemPalace)

```python
class KnowledgeGraph:
    def __init__(self, db: Database): ...

    # Write
    def add_entity(
        self,
        name: str,
        entity_type: str = "unknown",
        properties: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Returns the normalized entity ID."""

    def add_triple(
        self,
        subject: str,
        predicate: str,
        object: str,
        valid_from: datetime,
        source_drawer_id: Optional[str] = None,
        confidence: float = 1.0,
    ) -> str:
        """Auto-creates entities if they don't exist. Returns triple UUID."""

    def invalidate(
        self,
        subject: str,
        predicate: str,
        object: str,
        ended: datetime,
    ) -> int:
        """Sets valid_to on matching triples. Returns count affected."""

    # Read
    def query_entity(
        self,
        entity: str,
        as_of: Optional[datetime] = None,
        direction: str = "both",  # "outgoing" | "incoming" | "both"
    ) -> List[Dict[str, Any]]: ...

    def timeline(self, entity: str) -> List[Dict[str, Any]]:
        """Chronological list of all triples where entity is subject or object."""
```

### 4. `MemoryIngester` public API

```python
class MemoryIngester:
    def __init__(
        self,
        semantic_store: SemanticStore,
        knowledge_graph: KnowledgeGraph,
        default_wing: str = "wing_ora_kernel_cloud",
        queue_maxsize: int = 1000,
    ): ...

    def start(self) -> None:
        """Spin up the background worker thread."""

    def stop(self, timeout: float = 2.0) -> None:
        """Graceful shutdown — drain queue then join."""

    # Called from EventConsumer._handle_message
    def ingest_agent_message(
        self,
        session_id: str,
        text: str,
        wing: Optional[str] = None,
        room: Optional[str] = None,
    ) -> None: ...

    # Called from DispatchManager._run_sub_session on successful completion
    def ingest_dispatch_result(
        self,
        node_name: str,
        input_data: Dict[str, Any],
        response_text: str,
        tokens: Dict[str, int],
        cost_usd: float,
        duration_ms: int,
        parent_session_id: str,
        sub_session_id: str,
        status: str,
    ) -> None: ...

    @property
    def queue_depth(self) -> int: ...

    @property
    def is_healthy(self) -> bool:
        """True if the worker thread is alive and the queue is below its high-water mark."""
```

All ingestion calls from `EventConsumer` and `DispatchManager` are **non-blocking** — they enqueue a job and return. Queue overflow is logged + dropped (never blocks). Worker errors are logged per-job (never crash the worker).

### 5. `RECALL` fence protocol (Phase 2)

**Kernel → orchestrator** (embedded in `agent.message`):

````
```RECALL
{
  "query": "<natural-language search query>",
  "wing": "<optional wing filter>",
  "room": "<optional room filter>",
  "limit": 5,
  "kg_entity": "<optional entity for KG mode>",
  "as_of": "<optional ISO8601 timestamp for temporal queries>",
  "diary_agent": "<optional agent name for diary mode>"
}
```
````

Exactly one of `query` / `kg_entity` / `diary_agent` must be present. The orchestrator's broker picks the mode by which field is non-null.

**Orchestrator → Kernel** (as `user.message`):

````
```RECALL_RESULT status=<complete|error> count=<N> mode=<search|kg|diary>
{
  "results": [ ... ],        // schema varies by mode (see below)
  "error": "<message>",      // only on status=error
  "query": "<echo of input query>"
}
```
````

**`mode=search` result schema:**

```json
[
  {
    "wing": "wing_ora_kernel_cloud",
    "room": "dispatch-subsystem",
    "hall": "hall_facts",
    "snippet": "<content>",
    "source": "drawer_<uuid>",
    "timestamp": "2026-04-10T17:04:12Z",
    "score": 0.91
  }
]
```

**`mode=kg` result schema:**

```json
[
  {
    "subject": "business_analyst",
    "predicate": "dispatched_for",
    "object": "task_042",
    "valid_from": "2026-04-10T17:00:00Z",
    "valid_to": null,
    "source_drawer_id": "drawer_<uuid>"
  }
]
```

**`mode=diary` result schema:**

```json
[
  {
    "entry": "<AAAK-style condensed entry>",
    "topic": "dispatch",
    "created_at": "2026-04-10T17:04:12Z"
  }
]
```

**Error cases** (as `status=error`):
- `memory subsystem not installed`
- `invalid RECALL fence body (JSON parse error)`
- `unknown mode — must supply query, kg_entity, or diary_agent`
- `embedding model not loaded`
- `semantic search failed: <reason>`

The Kernel is taught (via the `RECALL_PROTOCOL` constant in `session_manager.py`):
1. RECALL is a last resort. Use in-session context and WISDOM.md first.
2. An error result is not a crash condition. Fall back to existing behavior.
3. Do not retry an identical RECALL. Reformulate or ask the operator.

---

## Task Breakdown

**Note:** This section is the spec's phase outline. The formal per-task implementation plan will be written separately via `superpowers:writing-plans` when we start Phase 1.

### Phase 1 — Schema + semantic store + ingest

**Phase 1.1 — Migration and dep plumbing**
- `kernel-files/infrastructure/db/009_memory.sql` (new)
- `requirements-memory.txt` (new) with pinned versions of `pgvector`, `sentence-transformers`, etc.
- Document in `docs/API_KEY_SETUP.md` § Cost Model the disk/RAM footprint of the embedding model.

**Phase 1.2 — `SemanticStore` implementation**
- `orchestrator/memory/__init__.py` (new)
- `orchestrator/memory/semantic_store.py` (new, ~250 lines)
- Lazy-load the `sentence-transformers` model on first use so import cost doesn't hit the orchestrator cold start.
- Unit + integration tests against a real postgres. Reuse the existing `db` fixture pattern from `test_db_dispatch.py`.

**Phase 1.3 — Adapt `knowledge_graph.py` from MemPalace**
- Copy `mempalace/knowledge_graph.py` → `orchestrator/memory/knowledge_graph.py`
- Add provenance header per § Attribution below.
- Replace SQLite backend with the existing `Database` wrapper.
- Global `?` → `%s`. `INSERT OR REPLACE` → `ON CONFLICT` upsert. Remove SQLite PRAGMAs.
- Convert JSON-in-TEXT columns to JSONB where the tests don't assume exact string round-trip.
- Integration tests against real postgres for temporal correctness — point-in-time queries, invalidation edge cases, timeline ordering.

**Phase 1.4 — Adapt `dedup.py` from MemPalace**
- Copy `mempalace/dedup.py` → `orchestrator/memory/dedup.py`
- Add provenance header.
- Replace the ChromaDB similarity call with `SemanticStore.search(...)` + similarity threshold check.
- Unit tests against the semantic store.

**Phase 1.5 — `MemoryIngester` and worker thread**
- `orchestrator/memory/ingester.py` (new, ~250 lines)
- Bounded `queue.Queue`; daemon worker thread; graceful `start` / `stop`.
- `ingest_agent_message` and `ingest_dispatch_result` entry points.
- Health property; queue-depth gauge.
- Background failure isolation — worker catches all exceptions and keeps running.
- Unit tests: enqueue → verify drawer / KG triple / diary row in postgres; stop drains queue; overflow is logged not raised.

**Phase 1.6 — Hook into `EventConsumer` + `DispatchManager`**
- Both classes grow an optional `memory: Optional["MemoryIngester"] = None` kwarg.
- `_handle_message` calls `memory.ingest_agent_message(...)` after the existing `db.log_activity` / `file_sync` / `ws_bridge` calls. Wrapped in `try/except Exception; logger.exception(...)` matching the established pattern.
- `_run_sub_session` calls `memory.ingest_dispatch_result(...)` on successful completion, enqueueing:
  1. A drawer with the response text tagged `hall_events`, room `dispatch-<node_name>`
  2. KG triples: `(node_name, dispatched_for, sub_session_id, start_ts)`, `(node_name, completed, sub_session_id, end_ts)`, `(parent_session, dispatched, sub_session, start_ts)`
  3. A diary entry under `agent_name=node_name`, topic=`dispatch`
- Regression tests: verify `memory=None` path works unchanged.

**Phase 1.7 — `__main__.py` wiring**
- Build `SemanticStore`, `KnowledgeGraph`, `MemoryIngester` when optional deps are present.
- Pass `MemoryIngester` to `EventConsumer` and `DispatchManager` in both initial construction and restart path.
- Graceful fallback when deps missing — log the fact and proceed.
- Add `memory.stop()` to the shutdown handler.

**Phase 1.8 — Live smoke test**
- Start orchestrator with memory enabled.
- Send a few dispatched tasks; confirm drawers appear in `memory_drawers`, triples appear in `memory_kg_triples`, diary entries appear in `memory_agent_diaries`.
- Verify queue depth stays bounded (no runaway).
- Verify orchestrator still shuts down cleanly.

**Phase 1.9 — Attribution mechanics**
- `licenses/mempalace-LICENSE.txt` — verbatim upstream MIT license
- `NOTICES.md` at repo root — lists adapted files with source links
- Per-file headers on every adapted file
- `CHANGELOG.md` entry explicitly citing MemPalace as prior art when Phase 1 ships

### Phase 2 — RECALL fence protocol

**Phase 2.1 — `RECALL_PROTOCOL` constant**
- New constant in `session_manager.py` following the pattern of `SYNC_SNAPSHOT_PROTOCOL` and `DISPATCH_PROTOCOL`.
- Document modes (query / kg / diary), schemas, error semantics, Axiom 5 note on retry discipline.

**Phase 2.2 — `BOOTSTRAP_PROMPT` + `send_protocol_refresh`**
- Embed `RECALL_PROTOCOL` in `BOOTSTRAP_PROMPT` next to the existing two protocols.
- Include in `send_protocol_refresh` so resumed sessions get the refresh.
- Regression tests for the existing `test_scheduler_triggers.py` that verify new `BOOTSTRAP_PROMPT` formatting.

**Phase 2.3 — `parse_recall_fences` pure function**
- `orchestrator/memory/recall.py` (new)
- Fence regex parallel to `parse_sync_fences` and `parse_dispatch_fences`.
- Silent-skip on malformed JSON, missing required fields, non-dict body.
- Unit tests covering all valid modes + all reject paths.

**Phase 2.4 — `RecallBroker` class**
- Holds references to `SemanticStore`, `KnowledgeGraph`, `send_to_parent` callback.
- `handle_message(parent_session_id, text)` — parses fences, dispatches each to the right mode handler, formats `RECALL_RESULT` fence, calls `send_to_parent(...)`.
- Mode handlers: `_handle_query_mode`, `_handle_kg_mode`, `_handle_diary_mode`.
- Error handling: catches every exception, returns `status=error` fence with a useful message.

**Phase 2.5 — `EventConsumer` wiring**
- Add optional `recall_broker: Optional["RecallBroker"] = None` kwarg.
- `_handle_message` routes RECALL fences to the broker after the existing file_sync / dispatch_manager / ws_bridge paths.
- Wrapped in `try/except Exception; logger.exception(...)`.
- Regression tests.

**Phase 2.6 — `__main__.py` wiring (Phase 2 slice)**
- Build `RecallBroker` if memory subsystem is active.
- Pass to `EventConsumer` alongside the other broker callbacks.

**Phase 2.7 — Live smoke test**
- Send a task that asks the Kernel to recall something (e.g., "Using the RECALL protocol, search for any previous discussions about file sync divergence").
- Observe the Kernel emit a `RECALL` fence.
- Observe the orchestrator broker return a `RECALL_RESULT` as the next user message.
- Observe the Kernel process the result and respond with a synthesis.

### Phases 3–6

Covered at higher level in `docs/research/2026-04-10-mempalace-memory-integration.md` § 6. Each will get its own spec section or standalone SPEC-003 / SPEC-004 when we approach them.

---

## Files to Create or Modify

### New files

| File | Purpose |
|---|---|
| `kernel-files/infrastructure/db/009_memory.sql` | Schema migration |
| `orchestrator/memory/__init__.py` | Package init |
| `orchestrator/memory/semantic_store.py` | pgvector + sentence-transformers wrapper |
| `orchestrator/memory/knowledge_graph.py` | Adapted from MemPalace, postgres backend |
| `orchestrator/memory/dedup.py` | Adapted from MemPalace, pgvector similarity |
| `orchestrator/memory/ingester.py` | Background-thread ingest pipeline |
| `orchestrator/memory/recall.py` | Phase 2: RECALL fence broker |
| `orchestrator/memory/layers.py` | Phase 3: wake-up composition (adapted) |
| `orchestrator/memory/palace_graph.py` | Phase 5: room navigation (adapted) |
| `orchestrator/tests/test_semantic_store.py` | Integration tests against real postgres |
| `orchestrator/tests/test_knowledge_graph.py` | Temporal correctness tests |
| `orchestrator/tests/test_dedup.py` | Deduplication tests |
| `orchestrator/tests/test_ingester.py` | Queue + worker tests |
| `orchestrator/tests/test_recall.py` | Phase 2: RECALL fence parser + broker tests |
| `orchestrator/tests/test_memory_integration.py` | Cross-component integration tests |
| `requirements-memory.txt` | Optional deps (`pgvector` Python bindings, `sentence-transformers`) |
| `NOTICES.md` | Third-party attributions |
| `licenses/mempalace-LICENSE.txt` | Upstream MIT license verbatim |

### Modified files

| File | Change |
|---|---|
| `orchestrator/event_consumer.py` | Add `memory` + `recall_broker` kwargs; hook `_handle_message` + `_handle_tool_use` to enqueue ingest jobs; route RECALL fences |
| `orchestrator/dispatch.py` | Add `memory` kwarg; hook `_run_sub_session` success path to enqueue dispatch drawer + KG triples + diary entry |
| `orchestrator/session_manager.py` | Add `RECALL_PROTOCOL` constant (Phase 2); embed in `BOOTSTRAP_PROMPT`; include in `send_protocol_refresh` |
| `orchestrator/__main__.py` | Optionally construct memory subsystem; pass to EventConsumer + DispatchManager; add `memory.stop()` to shutdown path |
| `orchestrator/db.py` | Small helpers if needed (e.g., `register_vector_type()` for pgvector bindings) |
| `config.yaml` | New `memory:` section with `enabled`, `default_wing`, `palace_wings` keyword→wing map, `model_name`, `queue_maxsize`, `recall_timeout_seconds` |
| `docs/CLOUD_ARCHITECTURE.md` | New Components section for Semantic Memory; new invariant I5; updated Implementation Status table |
| `CHANGELOG.md` | New `[2.1.0-cloud.1]` entry citing MemPalace as prior art |
| `docs/next_steps.md` | Update Phase 1 status when shipped; move "long-term recall" out of backlog |
| `docs/specs/SPEC-001-managed-agent-cloud-fork.md` | Mark SPEC-002 as related spec |
| `CONTRIBUTING.md` | Add guidance on touching `orchestrator/memory/*` files with attribution |
| `SECURITY.md` | Add secrets-redaction + content-privacy section for memory |

---

## Validation Plan

### Automated tests

**Unit-level:**
- `test_semantic_store.py` — add / duplicate-reject / search / diary round-trip against a real postgres. Target: 15+ tests.
- `test_knowledge_graph.py` — add_entity / add_triple / query_entity / invalidate / timeline, with emphasis on temporal correctness. Target: 12+ tests.
- `test_dedup.py` — exact match / near-duplicate / below-threshold cases. Target: 6+ tests.
- `test_ingester.py` — enqueue non-blocking / worker drains queue / overflow logs-not-raises / stop drains / worker survives per-job exceptions. Target: 8+ tests.
- `test_recall.py` — fence parsing (valid + all reject paths), broker mode dispatch, error fence formatting. Target: 10+ tests.

**Integration-level:**
- `test_memory_integration.py` — full round-trip: EventConsumer receives an agent.message → MemoryIngester enqueues → worker files drawer → SemanticStore.search finds it. Plus the DispatchManager success path: _run_sub_session → dispatch drawer + 3 KG triples + diary entry. Target: 6+ tests.
- `test_event_consumer_memory.py` (extension of existing `test_event_consumer_cdc.py`) — confirm `memory=None` path works, confirm RECALL fence routing.

**Target totals:** 50+ new tests, bringing the orchestrator suite from 130 to 180+.

### Manual smoke tests

**Phase 1 — ingest smoke:**
1. Apply migration.
2. Install `requirements-memory.txt`.
3. Start orchestrator. Verify log line `Memory: pgvector <version> detected`, `Memory: embedding model all-MiniLM-L6-v2 loaded (384-dim)`, `Memory: ingest queue ready`.
4. Send a few dispatch tasks via `python3 -m orchestrator --send ...`.
5. Verify rows in `memory_drawers`, `memory_kg_triples`, `memory_agent_diaries` via psql.
6. Verify `memory_drawers.wing = 'wing_ora_kernel_cloud'` and rooms are non-empty.
7. Run `SELECT content, wing, room, 1 - (embedding <=> (SELECT embedding FROM memory_drawers ORDER BY created_at DESC LIMIT 1)) AS score FROM memory_drawers ORDER BY embedding <=> (SELECT embedding FROM memory_drawers ORDER BY created_at DESC LIMIT 1) LIMIT 5;` and confirm the most recent drawer is its own nearest neighbor with score=1.0.

**Phase 2 — RECALL round-trip smoke:**
1. With Phase 1 ingest having populated some drawers, restart the orchestrator.
2. Send: `python3 -m orchestrator --send "Using the RECALL protocol, search for any previous discussions about dispatch timeouts. Summarize what you find."`
3. Watch `orch_activity_log` for the Kernel's response containing a `RECALL` fence.
4. Watch for the orchestrator's `user.message` carrying `RECALL_RESULT`.
5. Watch for the Kernel's follow-up response that cites the recalled drawers.

### Cross-repo verification

When Phase B of the dashboard lands (in forex-ml-platform), the memory subsystem should be visible as a new panel surface. Not a Phase 1 deliverable.

---

## Risks & Mitigations

See `docs/research/2026-04-10-mempalace-memory-integration.md` § 7 for the full risk register with mitigations. Summary:

| Risk | Mitigation | Phase |
|---|---|---|
| `pgvector` version incompat | Pin extension version, log at startup, fail gracefully | 1 |
| `sentence-transformers` model drift | Pin model name explicitly, record in every drawer row | 1 |
| Embedding model footprint (~90MB) | Optional dependency, documented disk/RAM impact | 1 |
| SSE-loop blocking from ingest | Background thread + bounded queue | 1 |
| Content privacy / secrets in drawers | Ingest-time regex redaction for known API-key patterns | 1 |
| Kernel over-reliance on RECALL | Explicit protocol language + observable call counts + soft cap | 2 |
| Protocol drift on resume | Reuse existing `send_protocol_refresh` mechanism | 2 |
| Corpus bootstrapping | Phase 1.5 utility reads `orch_activity_log` | Future |
| Memory subsystem absent | Optional dep with clear startup log, `RECALL_RESULT status=error` on recall | 1 |
| Upstream MemPalace maintenance | Low-frequency manual sync; we chose Option C to avoid this | n/a |

---

## Attribution

Per MIT license, the following upstream files are adapted with attribution:

| Upstream file (MemPalace, MIT, Copyright (c) 2026 Milla Jovovich & Ben Sigman) | Adaptation target |
|---|---|
| `mempalace/knowledge_graph.py` (393 lines) | `orchestrator/memory/knowledge_graph.py` |
| `mempalace/palace_graph.py` (227 lines) | `orchestrator/memory/palace_graph.py` (Phase 5) |
| `mempalace/dedup.py` (239 lines) | `orchestrator/memory/dedup.py` |
| `mempalace/layers.py` (515 lines, partial) | `orchestrator/memory/layers.py` (Phase 3) |
| `mempalace/searcher.py` (152 lines, reference only) | Informs `orchestrator/memory/semantic_store.py` design |

**Required deliverables:**

1. **Per-file header** on every adapted file:

```python
"""
orchestrator/memory/<file>.py — <brief description>

Adapted from MemPalace (https://github.com/milla-jovovich/mempalace)
  Original: mempalace/<file>.py
  License: MIT, Copyright (c) 2026 Milla Jovovich & Ben Sigman
  Changes from upstream:
    - <list of concrete adaptations>
"""
```

2. **`NOTICES.md`** at repo root — lists adapted files with upstream source links and the license

3. **`licenses/mempalace-LICENSE.txt`** — upstream MIT license verbatim

4. **CHANGELOG.md entry** on first Phase 1 release:

```markdown
## [2.1.0-cloud.1] — <date> — Semantic memory on pgvector

Knowledge graph, dedup, and (Phase 3+) wake-up / palace-graph modules
adapted from MemPalace (milla-jovovich/mempalace, MIT) — their
architectural design for temporal entity-relationship triples,
cross-wing room navigation, and specialist agent diaries is the
basis for our postgres implementation. Their benchmark work
(96.6% LongMemEval R@5 in raw vector mode) is what made us confident
vector search over raw verbatim text would meet our recall needs
before we started.
```

5. **Courtesy (not required):** open an issue on their repo noting our adaptation, upstream any bug fixes we find during adaptation, don't describe our work as "MemPalace integration" — describe it as "semantic memory inspired by MemPalace's architecture, built on postgres".

---

## Open Questions

Captured at § 8 of the research doc. Quick summary of spec-level decisions we need:

1. **Ingest scope:** every `agent.message` vs filtered. **Proposed: every, with dedup.**
2. **Wing-per-project vs wing-per-session:** **Proposed: wing-per-project, session_id in metadata.**
3. **Room detection:** keyword heuristic vs ML. **Proposed: keyword heuristic in Phase 1, ML later if needed.**
4. **Embedding model:** `all-MiniLM-L6-v2` vs larger. **Proposed: all-MiniLM-L6-v2, benchmark after Phase 1.**
5. **Single-project palace vs per-project palace:** **Proposed: single `ora_kernel` database, wings partition per project.**
6. **Redaction patterns:** `sk-ant-*`, `sk-*`, `postgres://.*:.*@*`, `Bearer *`, others? **Proposed: seed list + operator-extensible.**

These are resolved to a default in § Contracts and become authoritative once Phase 1 ships. Operators can override via `config.yaml` where applicable.

---

## Handoff Notes

This spec is ready for the Phase 1 implementation plan. Recommended approach:

1. **Read the research doc first** (`docs/research/2026-04-10-mempalace-memory-integration.md`) for the full context — especially § 5 (file-by-file adaptation analysis) and § 7 (risks).
2. **Write the Phase 1 plan** via `superpowers:writing-plans`, targeting Phase 1.1 through Phase 1.9 from § Task Breakdown above. The plan should produce one commit per bite-sized TDD unit (same cadence as the W1-W10 dashboard bridge work and D1-D17 dispatch subsystem work from 2026-04-10).
3. **Execute via `superpowers:subagent-driven-development`** following the proven pattern: implementer subagent per task, spec reviewer, code quality reviewer, two-stage review loop.
4. **Phase 2 is a separate plan.** Don't merge Phase 1 and Phase 2 — they're different conceptual slices (ingest vs recall) and each deserves its own formal plan.
5. **Before starting implementation,** install `pgvector` and `sentence-transformers` on the dev machine and run a smoke test:
   - Apply the migration manually against a throwaway test database
   - Run `CREATE EXTENSION vector; SELECT version();` to confirm pgvector version
   - Run `python3 -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('all-MiniLM-L6-v2'); print(m.encode('hello'))"` to confirm model loads and embeds
   - Confirm the disk/RAM footprint is acceptable on the operator's machine

The research doc and this spec are the two source-of-truth documents. Any architectural question not answered here or there should be added as an Open Question in one of them, not guessed at during implementation.

---

*End of SPEC-002. Ready for Phase 1 planning.*
