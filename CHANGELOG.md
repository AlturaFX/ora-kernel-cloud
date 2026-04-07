# Changelog

## [1.1.0] - 2026-04-07

### Added
- Proactive heartbeat monitor — cron-based anomaly detection (stuck tasks, budget exhaustion, failure spikes), silent when healthy (HEARTBEAT_OK pattern)
- Daily briefing — morning summary with completions, failures, pending work, and suggested priorities
- Context-aware suggestions — post-completion follow-up suggestions with feedback loop; learns which types are helpful over time via orch_suggestion_feedback table
- Anticipatory research (idle work) — autonomous execution of low-risk queued tasks during off-hours, gated by PROJECT_DNA.md autonomy_level
- Learning journal + wisdom consolidation — daily operational journal entries, scored promotion to WISDOM.md via "dreaming" consolidation cycle (frequency, impact, recency)
- 3 new node specs: JournalWriter, ConsolidationAnalyst + verifier
- 4 cron scripts: heartbeat.sh, daily_briefing.sh, idle_work.sh, consolidate.sh
- 006_suggestion_feedback.sql migration with v_suggestion_effectiveness view
- pending_briefing.md replaces outbox.jsonl as async results buffer

### Changed
- Renamed .claude/chat/ to .claude/events/ (event queue, not chat)
- Renamed chat-listen.md to kernel-listen.md
- Replaced forex-specific examples with generic ones in node_output.md and node_creator.md
- Docker-compose credentials use environment variable substitution
- CLAUDE.md now loads WISDOM.md and last 2 days of journal entries for context
- README restructured with "Why ORA?", terminology glossary, and proactive features section

### Fixed
- loop_detector.py: broken newline escapes in stderr noise filter caused false positives on every command with cwd-reset notice
- protect_core.py: Python 3.10+ type syntax (str | None) replaced with Optional[str] for 3.8+ compatibility
- session_init.sh: bulk directory removal replaced with safer per-file deletion and path validation
- consolidate.sh: missing journal directory now produces a warning instead of silently exiting
- install.py: kernel/ directory copy now preserves journal entries and WISDOM.md on re-install

## [1.0.0] - 2026-04-06

### Added
- 9-axiom Constitution with full definitions
- CLAUDE.md kernel operating instructions with behavioral contracts
- PROJECT_DNA.md template (5 interview questions)
- 6 hook scripts: safety_check, protect_core, loop_detector, subagent_lifecycle, anti_poll, session_init
- 10 node specs: NodeDesigner, NodeCreator, BusinessAnalyst (+ verifiers), TuningAnalyst, RefinementAnalyst (+ verifiers)
- 3 schema files: node_output, node_spec, split_spec
- 3 reference docs: constitution, kernel_priorities, node_quad_example
- 2 commands: kernel-listen (kernel event loop), self-improve (manual trigger)
- PostgreSQL schema: 5 migration files (10 tables, 2 views)
- OpenTelemetry pipeline: docker-compose + collector config
- agents.yaml node/command registry
- install.py integration script with:
  - Clean install into new projects
  - Non-destructive merge into existing projects (CLAUDE.md, settings.json, agents.yaml)
  - Section markers for idempotent updates
  - kernel-listen collision handling
  - --dry-run and --force modes
- INTEGRATION.md LLM-guided merge instructions
