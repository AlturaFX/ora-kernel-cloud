# Kernel Decision Framework

When the Kernel (main agent) receives input — whether from the user, inbox.jsonl, or a completed subagent — it reasons through these priorities. The Kernel uses Opus reasoning guided by these priorities as a framework, not a rigid sequence. Priorities inform decisions but do not dictate a fixed execution order.

## Priority Order

### P1: Acknowledge
If a task is NEW, acknowledge it by transitioning to INCOMPLETE. Log to orch_activity_log.

### P2: Check Budgets
Before any dispatch, check the task's retry_count against its budget_size limits in orch_budget_limits. If exceeded, escalate to HITL (dispatch BusinessAnalyst node).

### P3: HITL Check
If `is_awaiting_human` is true on any active task, handle the human's response before proceeding. Clear the flag when resolved.

### P4: Route
Match the task's intent to a node in the registry (`.claude/kernel/nodes/` + `.claude/agents.yaml`). If no match, trigger self-expansion (dispatch NodeDesigner).

### P5: Dispatch Work (Cycle 1)
Send the task to the matched node. Construct the subagent prompt from the node spec. Record dispatch in orch_tasks and orch_activity_log.

### P6: Create Verification
When a task reaches UNVERIFIED, identify the paired verifier node. Create a verification dispatch.

### P7: Dispatch Verification (Cycle 2)
Execute the verifier. Pass the work product and original task description.

### P8: Advance
When verification returns COMPLETE, advance the task to COMPLETE. If the task has downstream dependents in orch_task_dependencies, check if they're now unblocked.

## When to Apply First Principles (Axiom 8)

Apply first-principles decomposition before P4 (Route) when:
- The task has failed before (retry_count > 0)
- The task is Large or XL
- The intent is ambiguous (multiple possible interpretations)
- No obvious node match exists
- The task involves multiple domains or concerns

First-principles reasoning means:
1. What is the actual goal? (Strip away assumptions)
2. What are the real constraints? (Not inherited from a failed approach)
3. What is the simplest correct path to the goal?
4. Should this be decomposed into subtasks? (split_spec)

## Handling Completed Subagents

When a background subagent completes and you receive the notification:

1. Parse the result — is it a valid NodeOutput JSON?
2. Check `target_status`:
   - UNVERIFIED → proceed to P6 (Create Verification)
   - COMPLETE → proceed to P8 (Advance) — this was a verifier
   - FAILED → analyze failure, check budget, either replan or escalate
3. If `split_spec` is present → execute the decomposition protocol (see CLAUDE.md)
4. Update orch_tasks with the result
5. Log to orch_activity_log

## Handling Multiple Active Tasks

The Kernel may have several tasks in flight simultaneously. Priority order:
1. HITL responses (blocking human decisions)
2. Failed tasks needing replanning
3. UNVERIFIED tasks needing verification dispatch
4. NEW tasks needing acknowledgment and routing
5. Inbox messages needing classification

Do not poll for subagent status. Trust the notification system.
