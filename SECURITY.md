# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in ORA Kernel or ora-kernel-cloud, please report it responsibly:

1. **Do not** open a public GitHub issue
2. Email: fxaltura@gmail.com with subject "ORA Kernel Security"
3. Include: description, reproduction steps, and potential impact
4. **Never** include API keys, postgres credentials, or production data in the report

You will receive a response within 72 hours.

---

## ora-kernel-cloud Security Model

The cloud fork's runtime is fundamentally different from base ora-kernel's. The base-kernel hooks (`safety_check.sh`, `protect_core.py`, `loop_detector.py`, `anti_poll.py`) **do not run at runtime in the cloud** — the Managed Agent container has its own isolated filesystem and the orchestrator does not invoke them. The cloud fork's threat model is a different shape.

### Trust boundaries

```
Operator's machine          Anthropic Cloud
─────────────────           ───────────────
.env (API key) ─────────────┐
orchestrator   ─────────────┼──► Parent session (agent_toolset_20260401)
PostgreSQL     ◄────────────┘   ├── bash/read/write/edit/glob/grep
kernel-files/  (read-only)      └── web_search/web_fetch
                                     │
                                     ├──► Sub-session 1 (per dispatch)
                                     ├──► Sub-session 2
                                     └──► Sub-session N
```

The operator's machine trusts: the orchestrator code, `kernel-files/*` spec contents, `.env`, PostgreSQL credentials. The cloud trusts: the operator's `ANTHROPIC_API_KEY`. There is no trust relationship in the reverse direction — the cloud container cannot initiate traffic to the operator's machine.

### Authentication & secrets

- **`ANTHROPIC_API_KEY`** must live in `.env` (gitignored) or a shell environment variable, never in `config.yaml`, `settings.json`, or any tracked file. See `docs/API_KEY_SETUP.md`.
- **PostgreSQL credentials** should use Unix-socket peer authentication (`postgresql:///ora_kernel`) on developer machines. For remote postgres, use a strong password, TLS, and a restrictive `pg_hba.conf`.
- **If your API key is exposed** (pasted in a chat transcript, committed to a branch, leaked via a shell history), **rotate it immediately** at https://console.anthropic.com/settings/keys. The Anthropic console supports instant revocation.

### Container isolation (Invariant 1)

The Managed Agent container **never speaks directly to PostgreSQL**. This is enforced by omission — the container has no network path to postgres, no DSN in its environment, and the `BOOTSTRAP_PROMPT` explicitly tells the Kernel not to try. All state flows through the orchestrator by way of the SSE event stream.

**Why this matters security-wise:**
- Only one process has write access to `ora_kernel` (the orchestrator), so single-writer correctness is free.
- No database credentials ever leave the operator's machine.
- A compromised cloud container cannot exfiltrate postgres state or pivot to the operator's local network.

If you ever add a feature that requires the container to talk to postgres, **stop and reconsider** — you are almost certainly violating Invariant 1 and should look for an event-stream-based solution instead.

### Protected files

The file list in `protect_core.py` still represents the cloud fork's "do not modify" set, even though the hook itself does not run at cloud runtime:

- `kernel-files/CLAUDE.md` — the Kernel constitution (overridden for dispatch by `BOOTSTRAP_PROMPT`, not edited)
- `kernel-files/PROJECT_DNA.md` — the mission config
- `kernel-files/.claude/kernel/references/constitution.md` — the 9 axioms
- `kernel-files/.claude/kernel/schemas/*.md` — node output / node spec / split spec schemas
- `kernel-files/.claude/hooks/*` — the base-kernel hook scripts
- `kernel-files/.claude/settings.json` — base-kernel Claude Code settings
- `kernel-files/infrastructure/db/00[1-7]*.sql` — historical migrations (add new ones, never rewrite)

Changes to any of these files should go through PR review, not direct commit.

### Orchestrator-side threats

**Orphaned sub-sessions.** If the orchestrator crashes or is killed mid-dispatch, the Managed Agent sub-session continues running on Anthropic's side until its own idle timeout. This is a **cost concern**, not a security concern — the sub-session cannot attack anything — but runaway orphans can burn tokens. The `docs/next_steps.md` backlog has an item for a startup reconciliation sweep (item 2).

**Per-node agent growth.** Every edit to a node spec file triggers a fresh `client.beta.agents.create` call on the next dispatch (spec hash changes). Old agents are not deleted — they simply stop being referenced. Unreferenced agents accrue no cost but clutter your Anthropic console. See `docs/next_steps.md` item 7 for a cleanup sweep proposal.

**Dispatch cost caps.** `DispatchManager` enforces a wall-clock ceiling (`max_dispatch_seconds=600`) and a stall watchdog (`stream_read_timeout_seconds=180`), but no per-dispatch token budget. A runaway sub-agent could burn substantial tokens within the time budget. Add a budget wrapper (backlog item 5) for any production use.

**Credential exposure via logs.** The orchestrator logs at INFO level by default; API keys are never logged, but JSON payloads inside `DISPATCH` fences and CDC writes ARE logged verbatim to `orch_activity_log` and `kernel_files_sync`. **Do not put secrets in node input payloads.** If you need to pass secrets to a sub-agent, do so via a side channel (environment variable, file in the container, etc.) — not via the fence body.

**PostgreSQL data at rest.** `orch_activity_log` retains full text of every agent message up to `TEXT_PREVIEW_LEN=10_000` and every tool input up to `INPUT_PREVIEW_LEN=2_000`. If a sub-agent ever reads a secret file and echoes its contents, those contents land in postgres. Consider your postgres backup strategy and access controls accordingly.

### Known limitations

- **Anthropic API beta.** The Managed Agents API is in beta. Rate limits, quotas, and even endpoint shapes can change between SDK releases. The orchestrator pins `anthropic>=0.40.0` but does not pin to a specific minor version — upgrades should be tested in a sandbox first.
- **No multi-tenancy.** One operator, one `.ora-kernel-cloud.json` state file, one parent session. Running multiple projects means running multiple orchestrator processes with separate state directories. There is no isolation between dispatches within the same environment.
- **Stream stall watchdog is best-effort.** The `httpx.Timeout(read=180)` fires if no bytes arrive for 180s, but a sub-session that emits events every 170s indefinitely could run up to `max_dispatch_seconds=600` before the wall clock kills it. These defaults can be tightened per-deployment.

---

## Base ora-kernel Security Model (historical)

> The following applies to **base ora-kernel only** — the Claude Code TUI version. It is retained here for reference because `kernel-files/` is a faithful copy of the base-kernel source tree, but these hooks do not run at cloud runtime.

ORA Kernel (base) enforces safety through multiple layers:

- **safety_check.sh** — blocks `rm` and `sudo` commands (including chained/nested)
- **protect_core.py** — blocks modification of constitution, schemas, hooks, and core config
- **loop_detector.py** — prevents infinite retry loops with escalation
- **anti_poll.py** — throttles excessive status polling

These hooks are the enforcement layer. They cannot be bypassed by the LLM — they run at the Claude Code harness level.

### Base kernel known limitations

- Hooks run in the user's shell environment. A user with direct shell access can bypass hooks by editing settings.json or running commands outside Claude Code.
- The `rm` blocker uses word-boundary matching (`\brm\b`). Obfuscated commands (e.g., using eval or variable expansion) could theoretically bypass it.
- PostgreSQL credentials in docker-compose.yml use environment variable defaults. Always override with strong passwords in production.

---

## Supported Versions

| Version | Supported |
|---|---|
| `ora-kernel-cloud` 2.0.x | Yes |
| base ora-kernel 1.1.x | Yes (in the base repo) |
| base ora-kernel 1.0.x | Yes |
