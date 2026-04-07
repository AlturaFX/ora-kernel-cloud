# ORA Kernel

A self-expanding agentic orchestration system for Claude Code. The Kernel uses Opus as the orchestrator — reasoning about priorities, delegating to specialized nodes (subagents), and enforcing a 9-axiom Constitution through programmatic hooks.

## Why ORA?

When you use Claude Code for complex, multi-step projects, you face real problems:

- **No verification** — the agent that wrote the code also judges if it's correct (self-certification)
- **Infinite loops** — failed approaches get retried without analysis
- **No state tracking** — task progress, metrics, and history are lost between sessions
- **No safety rails** — nothing prevents accidental deletion of critical files or runaway retries
- **No improvement** — the same prompt weaknesses cause the same failures repeatedly

ORA solves these by treating Claude as an **orchestrator, not a worker**. It dispatches specialized subagents for execution, requires separate verification for every work product, tracks everything in PostgreSQL, and automatically proposes system improvements based on performance data.

This is **not a chat interface** — it's a workflow orchestration engine with constitutional guardrails.

## Terminology

| Term | Meaning |
|------|---------|
| **Kernel** | The main Claude Code agent. Orchestrates work, enforces the Constitution, dispatches nodes. |
| **Node** | A markdown spec file defining a specialized subagent's system prompt, input/output contracts, and constraints. |
| **Subagent** | A Claude Code Agent tool invocation dispatched by the Kernel to execute a node's prompt. |
| **Quad** | A set of 4 nodes: Domain (planner) + Task (executor) + Domain Verifier + Task Verifier. |
| **Constitution** | 9 immutable axioms that govern all system behavior. Enforced by hooks. |
| **HITL** | Human-in-the-loop. The system pauses for human approval when required by the Constitution. |

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
4. Run `/kernel-listen` to start the Kernel event loop
5. Push the inotifywait command to background, then use the TUI normally

## Architecture

```
You (TUI) ←→ Claude Code (Kernel)
                 ├── Subagents (nodes) — dispatched via Agent tool
                 ├── Hooks — safety, loop detection, lifecycle tracking
                 ├── PostgreSQL — task state, metrics, activity log
                 ├── events/inbox.jsonl — event triggers (cron, webhooks, subagent completion)
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
│   ├── commands/                 # kernel-listen, self-improve
│   ├── events/                   # inbox/outbox event queues
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
