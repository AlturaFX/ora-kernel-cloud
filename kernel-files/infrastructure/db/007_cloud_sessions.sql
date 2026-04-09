-- ORA Kernel Cloud: Managed Agent session tracking and file sync

-- Track Managed Agent sessions
CREATE TABLE IF NOT EXISTS cloud_sessions (
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

CREATE INDEX IF NOT EXISTS idx_cloud_sessions_status ON cloud_sessions(status);

-- Sync files for persistence across ephemeral containers
CREATE TABLE IF NOT EXISTS kernel_files_sync (
    file_path           TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    synced_from         TEXT NOT NULL DEFAULT 'container'
);
