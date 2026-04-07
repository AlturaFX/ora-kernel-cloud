# ORA Kernel

A self-expanding agentic orchestration system for Claude Code. The Kernel uses Opus as the orchestrator — reasoning about priorities, delegating to specialized nodes (subagents), and enforcing a 9-axiom Constitution through programmatic hooks.

## What It Does

- **Orchestrates complex tasks** by decomposing them into subtasks, dispatching specialized subagents, and verifying results
- **Enforces safety** via hooks that block dangerous commands, prevent infinite loops, protect core files, and throttle polling
- **Self-improves** by analyzing task metrics and proposing prompt/parameter improvements after every N completed tasks
- **Persists state** in PostgreSQL (task lifecycle, activity log, metrics) and OpenTelemetry (token usage, costs)
- **Listens for events** via an inotifywait-based inbox pattern that keeps the Kernel alive between interactions

## Quick Start

### New Project

```bash
python3 ora-kernel/install.py /path/to/your/project
```

### Existing Project (with its own CLAUDE.md, hooks, etc.)

```bash
# Preview what would change
python3 ora-kernel/install.py /path/to/your/project --dry-run

# Install with merge
python3 ora-kernel/install.py /path/to/your/project
```

### After Installation

1. Fill in `PROJECT_DNA.md` with your project's mission and constraints
2. Start PostgreSQL and run migrations:
   ```bash
   createdb ora_kernel
   for f in infrastructure/ora-kernel/db/*.sql; do psql -d ora_kernel -f "$f"; done
   ```
3. Restart Claude Code from your project root
4. Run `/chat-listen` to start the Kernel event loop
5. Push the inotifywait command to background, then use the TUI normally

## Architecture

```
You (TUI) ←→ Claude Code (Kernel)
                 ├── Subagents (nodes) — dispatched via Agent tool
                 ├── Hooks — safety, loop detection, lifecycle tracking
                 ├── PostgreSQL — task state, metrics, activity log
                 ├── inbox.jsonl — event triggers (cron, webhooks, subagent completion)
                 └── Constitution (9 Axioms) — immutable rules enforced by hooks
```

## The 9 Axioms

1. **Observable State** — record and broadcast every state change
2. **Objective Verification** — no self-certification; separate verifier for every work product
3. **Finite Resources** — every task has a budget; escalate when exceeded
4. **Immutable Core** — constitution, schemas, hooks cannot be modified by agents
5. **Entropy** — never retry a failed approach blindly; analyze root cause first
6. **Isolation** — every task starts clean; no hidden dependencies
7. **Purpose** — every task must advance the mission in PROJECT_DNA.md
8. **First Principles** — decompose complex/failed tasks to fundamentals before acting
9. **Separation of Concerns** — no single agent plans AND executes AND verifies

## File Structure

```
your-project/
├── CLAUDE.md                     # Kernel instructions (merged with your existing)
├── PROJECT_DNA.md                # Your project's mission config
├── .claude/
│   ├── settings.json             # Hooks + permissions (merged)
│   ├── agents.yaml               # Node/command registry (merged)
│   ├── hooks/                    # 6 enforcement scripts
│   ├── commands/                 # chat-listen, self-improve
│   ├── chat/                     # inbox/outbox message queues
│   └── kernel/
│       ├── schemas/              # NodeOutput, NodeSpec, SplitSpec
│       ├── nodes/                # System + self-improvement node specs
│       └── references/           # Constitution, priorities, examples
└── infrastructure/ora-kernel/
    ├── docker-compose.yml        # PostgreSQL + OTel collector
    ├── db/                       # SQL migrations
    └── otel/                     # Collector config
```

## Requirements

- Claude Code 2.1.89+
- Python 3.8+ (for hooks and installer)
- PostgreSQL 14+ (for state management)
- `inotifywait` (from `inotify-tools` package) for the event loop
- Docker (optional, for OTel collector)

## License

Business Source License 1.1 (BSL)

- Free for personal, educational, research, and internal business use
- Cannot be repackaged or resold as a commercial product or service
- Converts to Apache 2.0 on 2030-04-06 (4 years from initial release)

See [LICENSE](LICENSE) for full terms.
