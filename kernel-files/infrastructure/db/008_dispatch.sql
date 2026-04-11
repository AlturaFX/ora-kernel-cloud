-- ORA Kernel Cloud: dispatch subsystem state (Option 3 — sub-sessions per node)

-- Per-node cached Anthropic Managed Agent IDs.
-- prompt_hash is a sha256 of the node spec file content — when the spec
-- changes, the orchestrator creates a fresh agent rather than reusing a
-- stale one.
CREATE TABLE IF NOT EXISTS dispatch_agents (
    node_name     TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- One row per dispatch. parent_session_id links the sub-session back to
-- the Kernel session that requested it. status moves RUNNING -> COMPLETE
-- or FAILED. Tokens/cost are copied out of the SSE stream at idle time.
CREATE TABLE IF NOT EXISTS dispatch_sessions (
    sub_session_id    TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    node_name         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running',
    input_data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_data       JSONB,
    input_tokens      BIGINT DEFAULT 0,
    output_tokens     BIGINT DEFAULT 0,
    cost_usd          NUMERIC(10,6) DEFAULT 0,
    duration_ms       INTEGER,
    error             TEXT,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_parent
    ON dispatch_sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_status
    ON dispatch_sessions(status);
CREATE INDEX IF NOT EXISTS idx_dispatch_sessions_started
    ON dispatch_sessions(started_at DESC);
