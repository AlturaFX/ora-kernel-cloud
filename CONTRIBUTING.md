# Contributing to ORA Kernel

> **Scope note.** This repository is `ora-kernel-cloud`, the Managed Agent fork of base ora-kernel. The two forks have different runtimes, different test surfaces, and different contribution flows. Most of the sections below apply to base ora-kernel (which is tested via `install.py`); the **Contributing to ora-kernel-cloud** section below is the cloud-specific guide. Pick the one that matches what you are working on.

Thanks for your interest in improving ORA Kernel. This document covers the process for contributing.

---

## Contributing to ora-kernel-cloud (this repo)

### Reporting Issues

- Use the GitHub issue tracker for `AlturaFX/ora-kernel-cloud`
- Include the commit SHA you are running on (`git log -1 --oneline`)
- Describe what you expected vs what happened
- Include relevant orchestrator log output and any `psql` queries that show the bad state
- **Never** paste your `ANTHROPIC_API_KEY` — redact it from any command output

### Testing Changes

The orchestrator has a full unit + integration test suite. Run everything before submitting a PR:

```bash
# Full suite (72 tests as of 2026-04-10)
python3 -m pytest orchestrator/tests/ -v

# Dispatch subsystem only
python3 -m pytest orchestrator/tests/test_dispatch.py -v

# DB integration tests (auto-skip if postgres is unreachable)
python3 -m pytest orchestrator/tests/test_db_dispatch.py -v
```

For changes that touch the live dispatch pipeline, run the smoke test in `docs/superpowers/plans/2026-04-10-dispatch-subsystem.md` § Task 17 — it exercises the full fence → sub-session → result round-trip against real Anthropic infrastructure.

For changes that touch file sync, the smoke test in `docs/superpowers/plans/2026-04-10-visibility-hitl-filesync.md` § Task 18 exercises CDC + snapshot reconciliation round-trip.

### Architectural Invariants (do not break these)

See `docs/CLOUD_ARCHITECTURE.md` § Architectural Invariants for the full list. Summary:

1. **The container never speaks directly to PostgreSQL.** All persistence flows through the orchestrator.
2. **Protocol teaching goes through `BOOTSTRAP_PROMPT` + `send_protocol_refresh`**, not `kernel-files/CLAUDE.md` (which is protected).
3. **Tool name matching is case-insensitive.** Managed Agent lowercases; Claude Code style capitalizes; both route.
4. **No agent ever self-certifies work.** The orchestrator never routes a task node and its verifier to the same sub-session.

### Proposing Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-change`)
3. **Write the plan first** via `superpowers:writing-plans` for any non-trivial change. The project maintains executed plans in `docs/superpowers/plans/` as a record.
4. Make your changes using TDD — failing test first, minimal implementation, green, commit. One commit per logical step.
5. Run the full test suite. All 72+ tests must pass.
6. Update `CHANGELOG.md` with an entry describing what you added/changed/fixed.
7. Update `docs/CLOUD_ARCHITECTURE.md` if your change affects architecture.
8. Submit a pull request with a clear description of the motivation, the change, and the verification you did.

### What Can Be Changed in ora-kernel-cloud

| Area | Contribution Type | Notes |
|---|---|---|
| `orchestrator/**` | Bug fixes, new handlers, dashboard integration, performance | Must include tests |
| `kernel-files/infrastructure/db/*.sql` | New migrations with sequential numbering | Must be idempotent (`IF NOT EXISTS`) |
| `kernel-files/.claude/kernel/nodes/**` | New node specs (including verifiers) | `dispatch_agents.prompt_hash` will auto-invalidate on the next dispatch |
| `docs/CLOUD_ARCHITECTURE.md`, `README.md`, `CHANGELOG.md` | Corrections and new sections | Always welcome |
| `docs/superpowers/plans/` | New plan documents | Follow the writing-plans skill format |
| `config.yaml` | New scheduler knobs, defaults | Document in CLOUD_ARCHITECTURE.md |

### What Cannot Be Changed in ora-kernel-cloud

| Area | Reason |
|---|---|
| `kernel-files/CLAUDE.md` | Protected by `protect_core.py`. The cloud fork overrides its dispatch protocol via `BOOTSTRAP_PROMPT`; to change Kernel behavior, edit `session_manager.py` constants instead. |
| `kernel-files/.claude/kernel/references/constitution.md` | Immutable — the 9 axioms are the contract. |
| `kernel-files/.claude/kernel/schemas/*.md` | Protected. Breaking changes would invalidate every node. |
| `kernel-files/.claude/hooks/*` | Protected. Applies to base ora-kernel; cloud fork doesn't use them at runtime. |
| `kernel-files/infrastructure/db/00[1-7]*.sql` | Don't rewrite history — add new migrations instead. |

---

## Contributing to base ora-kernel

> The sections below apply to the base ora-kernel fork, not this cloud fork. If you are contributing to base ora-kernel, you should be working against `AlturaFX/ora-kernel`, not this repo.

### Reporting Issues

- Use the [GitHub issue tracker](https://github.com/AlturaFX/ora-kernel/issues)
- Include your ORA Kernel version (`cat VERSION`)
- Describe what you expected vs what happened
- Include relevant hook output or error messages

### Proposing Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-change`)
3. Make your changes
4. Test with the installer:
   ```bash
   # Clean install test
   python3 install.py /tmp/test-clean --force
   
   # Idempotency test
   python3 install.py /tmp/test-clean --force
   ```
5. Submit a pull request

### What Can Be Changed

| Area | Contribution Type | Notes |
|------|-------------------|-------|
| Node specs (.claude/kernel/nodes/) | New nodes, prompt improvements | Must include paired verifier |
| Hook scripts (.claude/hooks/) | Bug fixes, new detection patterns | Must not break existing behavior |
| Schemas (.claude/kernel/schemas/) | Clarifications, new examples | Must remain backward-compatible |
| install.py | New merge strategies, bug fixes | Must pass clean install + merge tests |
| Documentation | Corrections, new examples | Always welcome |

### What Cannot Be Changed

The Constitution (9 Axioms) is immutable by design. If you believe an axiom needs modification, open an issue to discuss — but expect a high bar for changes.

## Code Standards

- **Python hooks**: stdlib only, no external dependencies. Must handle malformed stdin gracefully (exit 0 on parse failure).
- **Node specs**: Follow the template in `.claude/kernel/schemas/node_spec.md`. Every worker needs a verifier.
- **Installer**: Must remain idempotent. `--dry-run` must never modify files.

## Testing

Before submitting a PR, verify:

1. `python3 install.py /tmp/test-project --force` completes without errors
2. Running it twice produces no duplicates (idempotency)
3. `python3 install.py /path/to/existing-project --dry-run` reports conflicts correctly
4. Any new hooks can be tested standalone: `echo '{"tool_name":"Bash","tool_input":{"command":"echo test"}}' | python3 .claude/hooks/your_hook.py`
