# Changelog

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
