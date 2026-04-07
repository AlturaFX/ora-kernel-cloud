# ORA Kernel — LLM Integration Guide

This document is designed for Claude (or any LLM) to follow when integrating the ORA Kernel into an existing project. The `install.py` script handles most cases automatically, but edge cases require LLM judgment.

## When to Use This Guide

- The install script's `--dry-run` output shows conflicts you want to understand
- You're integrating into a project with complex existing CLAUDE.md or hooks
- You need to reconcile project-specific rules with kernel instructions
- The automated merge produced results that need manual review

---

## 1. CLAUDE.md Reconciliation

### How It Works
The installer wraps kernel instructions in markers:
```
<!-- ORA-KERNEL:START — Do not edit this block manually. Managed by ora-kernel installer. -->
# Kernel Operating Instructions
...
<!-- ORA-KERNEL:END -->
```

This block is prepended to the existing CLAUDE.md. Both sets of instructions are loaded by Claude Code.

### Potential Conflicts

**Execution rules overlap**: If the project's CLAUDE.md says "always run tests before committing" and the kernel says "delegate testing to a verifier node," these could conflict.

**Resolution approach**: The kernel's instructions are about *orchestration* (how to dispatch, verify, escalate). The project's instructions are about *domain behavior* (coding standards, testing rules, workflow). They typically operate at different levels. When they conflict:

1. Read both instructions carefully
2. If the project rule is about *what to do* (run tests, follow style guide) — keep it. The kernel doesn't override domain rules.
3. If the project rule is about *how to organize work* (do everything in one go, don't use subagents) — the kernel's orchestration model supersedes it, but preserve the intent.
4. Add a reconciliation note at the boundary: `<!-- Project rules below may reference tasks that are now dispatched via the kernel -->`

### Example
Project says: "Before committing, run `pytest` and fix all failures."
Kernel says: "Delegate execution to Task Nodes, verify via Verifier Nodes."

Reconciled: The project rule becomes input to the verification criteria in PROJECT_DNA.md's Definition of Done: "All tests pass (`pytest` exits 0)." The kernel dispatches a testing node and verifier that checks this criterion.

---

## 2. Hook Behavior Reconciliation

### Multiple hooks on the same event

Claude Code runs all matching hooks in array order. If the project has a `PostToolUse` hook on Bash and the kernel adds `loop_detector.py` on the same event, both run independently. This is safe — they don't interfere because:
- Each hook receives stdin independently (not shared)
- Each hook reads/writes to different state files
- Exit code 2 from any hook blocks the tool call

### File protection overlap

If the project has `protect_design_doc.py` and the kernel adds `protect_core.py`, both run on `PreToolUse` for Edit/Write. A file that's protected by either hook is blocked. The protection sets are additive — this is correct and intended.

**To review**: After installation, check that no file you need to edit regularly is accidentally protected by both hooks. Run:
```bash
# List all protected paths from both hooks
grep -A 20 "PROTECTED" .claude/hooks/protect_core.py .claude/hooks/protect_*.py
```

### Hook naming conflicts

The kernel's hooks use specific names: `safety_check.sh`, `protect_core.py`, `loop_detector.py`, `subagent_lifecycle.py`, `anti_poll.py`, `session_init.sh`. If the project already has a file with one of these names in `.claude/hooks/`, the install script overwrites it (kernel-owned).

**Before installation**: Check if any of these names are used by project-specific hooks:
```bash
ls .claude/hooks/
```
If there's a conflict, rename the project's hook before installing.

---

## 3. Command Deconflicting

### The kernel-listen collision

Both the kernel and some projects use `kernel-listen.md`. The kernel's version is the core event loop — it watches `inbox.jsonl` and dispatches tasks. A project's version might do something different (e.g., listen for dashboard test requests).

The installer handles this by renaming: if `kernel-listen.md` exists, the kernel's version is installed as `ora-kernel-listen.md`.

**Should you merge them?** Consider:
- If the project's kernel-listen is a subset of what the kernel does (e.g., only handles `/test-start`) — merge the project's routing rules into the kernel's kernel-listen.md under the message routing section.
- If they serve truly different purposes — keep them separate. The kernel runs as background, the project's runs on demand.
- If the project's kernel-listen WAS the ORA kernel from a previous version — replace it with the new kernel version.

### Merging routing rules

To merge a project's command routing into the kernel's kernel-listen.md, add entries to the "Step 3: Handle the Message" section:

```markdown
### `/your-project-command`
[What this command does and how to handle it]
```

---

## 4. agents.yaml Schema Reconciliation

### Different schemas

The kernel uses:
```yaml
node_designer:
  spec_path: .claude/kernel/nodes/system/node_designer.md
  type: domain
  quad: NodeDesigner
  purpose: Analyzes capability gaps
  capability_tags: [system, architecture]
```

Some projects use:
```yaml
kernel-listen:
  type: command
  file: .claude/commands/kernel-listen.md
  purpose: Listen for messages
  invocation: /kernel-listen
  triggers: [dashboard_message]
```

### Coexistence

Both schemas can live in the same file. The kernel's entries are wrapped in section markers:
```yaml
# === ORA-KERNEL:START system_nodes ===
system_nodes:
  ...
# === ORA-KERNEL:END system_nodes ===
```

Project entries outside these markers are preserved. The kernel never touches them.

### Unified schema (optional)

If you want a single consistent format, add a `source` field to distinguish:
```yaml
  kernel-listen:
    type: command
    file: .claude/commands/kernel-listen.md
    purpose: Listen for messages
    source: project           # ← project-owned entry
  
  node_designer:
    type: node
    spec_path: .claude/kernel/nodes/system/node_designer.md
    purpose: Analyzes capability gaps
    source: ora-kernel         # ← kernel-owned entry
```

---

## 5. PROJECT_DNA.md Guidance

The installer creates PROJECT_DNA.md as a template if it doesn't exist. Here's how to fill it in for common project types:

### Research / ML Project
```markdown
## 1. Mission
> Achieve state-of-the-art forecasting accuracy on the benchmark dataset while maintaining reproducible results.

## 2. Constraints
> Never modify the core model architecture in src/. Never run training without GPU availability check. No external data sources without approval.

## 3. Definition of Done
> Model evaluation metrics match or exceed baseline. All experiments are logged with parameters and results. Code passes linting and type checks.

## 4. Autonomy Level
> Autonomous for research, data analysis, and documentation. Pause for: training runs over 1 hour, model architecture changes, publishing results.

## 5. Truth Anchor
> The benchmark evaluation script is the source of truth for model performance. Published paper results are the baseline to beat.
```

### Web Application
```markdown
## 1. Mission
> Ship features that increase user retention while maintaining 99.9% uptime.

## 2. Constraints
> Never modify production database schema without migration. Never deploy without passing CI. No dependencies with known CVEs.

## 3. Definition of Done
> All tests pass. Feature matches the spec. Lighthouse score doesn't regress. PR reviewed.

## 4. Autonomy Level
> Autonomous for code changes, test writing, documentation. Pause for: database migrations, external API integrations, dependency upgrades.

## 5. Truth Anchor
> The test suite is the source of truth for correctness. The product spec is the source of truth for requirements.
```

---

## 6. Verification After Integration

Run these checks after installation:

1. **CLAUDE.md loads**: Start a new Claude Code session. The kernel instructions should appear in the system prompt. Ask "What are the 9 axioms?" to verify.

2. **Hooks fire**: Run any Bash command. The safety hook should execute silently (check for the "Safety check..." status message).

3. **File protection works**: Try to edit CLAUDE.md via the Edit tool. It should be blocked with a message about Axiom 4.

4. **Loop detector works**: Run a failing command 3 times. The third attempt should be blocked with a replanning prompt.

5. **Subagent lifecycle tracks**: Dispatch a test subagent. Check `/tmp/claude-kernel/` for status files.

6. **PostgreSQL connected**: Query `SELECT COUNT(*) FROM orch_config;` — should return 3 (default config entries).

---

## 7. Rollback

To remove the ORA Kernel from a project:

1. Remove the kernel block from CLAUDE.md (everything between `<!-- ORA-KERNEL:START -->` and `<!-- ORA-KERNEL:END -->`)
2. Remove kernel hook entries from settings.json (entries with `.claude/hooks/safety_check.sh`, `protect_core.py`, etc.)
3. Remove kernel sections from agents.yaml (between `# === ORA-KERNEL:START` and `# === ORA-KERNEL:END` markers)
4. Delete directories: `.claude/kernel/`, `.claude/hooks/` (kernel scripts only), `infrastructure/ora-kernel/`
5. Delete files: `PROJECT_DNA.md` (if kernel-created), `.claude/commands/self-improve.md`, `.claude/commands/ora-kernel-listen.md`

The marker system makes this straightforward — kernel content is always identifiable.
