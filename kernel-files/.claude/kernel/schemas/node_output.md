# Node Output Schema

Every subagent MUST return a single JSON object matching this schema as the last content in its response. Wrap it in a ```json code fence for reliable parsing.

## Required Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `target_status` | string | Always | One of: `UNVERIFIED`, `COMPLETE`, `FAILED` |
| `artifacts` | array | When files produced | List of file references |
| `inline_data` | object | When returning structured results | Free-form JSON for small results |
| `split_spec` | object | Domain Nodes only | Work decomposition instructions |
| `error` | object | When FAILED | Error details |

## Status Values

- **UNVERIFIED** — Work is done but not yet verified. The Kernel will dispatch a verifier.
- **COMPLETE** — Used only by verifier nodes to confirm work passes verification.
- **FAILED** — Execution failed. Must include `error` field. The Kernel will replan.

Task Nodes and Domain Nodes return `UNVERIFIED` on success.
Verifier Nodes return `COMPLETE` on success or `FAILED` on rejection.
Any node returns `FAILED` on error.

## Field Definitions

### artifacts
```json
"artifacts": [
  {
    "name": "research_summary",
    "uri": "file:///path/to/output.md",
    "mime_type": "text/markdown"
  }
]
```
Each artifact references a file written during execution. Use absolute paths.

### inline_data
```json
"inline_data": {
  "summary": "The analysis found three viable strategies...",
  "confidence": 0.85,
  "key_findings": ["finding1", "finding2"]
}
```
For results small enough to include directly. No size limit enforced, but prefer artifacts for large outputs.

### split_spec (Domain Nodes only)
```json
"split_spec": {
  "strategy": "parallel",
  "rationale": "The proposal has three independent sections that can be drafted concurrently.",
  "subtasks": [
    {
      "task_title": "Draft introduction section",
      "input_data": {"section": "intro", "requirements": "..."},
      "resource_hints": {"capability_tags": ["writing"]}
    },
    {
      "task_title": "Draft methodology section",
      "input_data": {"section": "methods", "requirements": "..."}
    }
  ],
  "aggregation_instructions": "Combine all sections into a single document. Ensure consistent tone and resolve any cross-references."
}
```

- `strategy`: "parallel" (independent subtasks) or "sequential" (ordered dependencies)
- `rationale`: Why this decomposition serves the mission (Axiom 7)
- `subtasks`: Each has `task_title`, `input_data`, and optional `resource_hints`
- `aggregation_instructions`: How the Kernel should reassemble results

When a Domain Node returns `split_spec`, it MUST set `target_status` to `UNVERIFIED` with `split_spec` populated. The Kernel handles dispatch and aggregation.

### error
```json
"error": {
  "code": "INVALID_ARGUMENT",
  "message": "Input field 'dataset_path' was empty but is required for this analysis.",
  "recoverable": true
}
```

Error codes:
- `INVALID_ARGUMENT` — Input validation failed
- `RESOURCE_EXHAUSTED` — Token/API/budget limit reached
- `INTERNAL` — Unexpected failure
- `DEADLINE_EXCEEDED` — Timeout
- `FAILED_PRECONDITION` — Missing dependency or prerequisite

`recoverable: true` means the Kernel may replan. `recoverable: false` means escalate to HITL.

## Complete Examples

### Example 1: Successful Task Node (Research)
```json
{
  "target_status": "UNVERIFIED",
  "artifacts": [
    {
      "name": "research_analysis",
      "uri": "file:///tmp/workspace/research_output.md",
      "mime_type": "text/markdown"
    }
  ],
  "inline_data": {
    "summary": "Identified 3 optimization approaches with measurable improvement over the baseline",
    "methods_evaluated": ["approach_a", "approach_b", "approach_c"],
    "approach_count": 3
  }
}
```

### Example 2: Failed Task Node
```json
{
  "target_status": "FAILED",
  "error": {
    "code": "FAILED_PRECONDITION",
    "message": "PostgreSQL connection refused on port 5432. Database may not be running.",
    "recoverable": true
  }
}
```

### Example 3: Domain Node Splitting Work
```json
{
  "target_status": "UNVERIFIED",
  "split_spec": {
    "strategy": "parallel",
    "rationale": "The 5-page proposal can be split into 3 independent drafting tasks plus a final assembly.",
    "subtasks": [
      {
        "task_title": "Draft executive summary and introduction",
        "input_data": {"sections": ["exec_summary", "intro"], "tone": "formal"}
      },
      {
        "task_title": "Draft technical approach",
        "input_data": {"sections": ["methodology", "architecture"], "tone": "technical"}
      },
      {
        "task_title": "Draft timeline and budget",
        "input_data": {"sections": ["timeline", "budget"], "constraints": "Q3 deadline"}
      }
    ],
    "aggregation_instructions": "Merge all sections into a single document. Add page numbers, table of contents, and ensure cross-references resolve."
  }
}
```

### Example 4: Verifier Confirming Work
```json
{
  "target_status": "COMPLETE",
  "inline_data": {
    "verification_notes": "All 3 approaches include required methodology sections. Sources verified. Calculations checked against baseline.",
    "checks_passed": ["structure", "sources", "calculations", "completeness"]
  }
}
```

### Example 5: Verifier Rejecting Work
```json
{
  "target_status": "FAILED",
  "error": {
    "code": "FAILED_PRECONDITION",
    "message": "Approach 2 is missing a methodology section. Approach 3 cites a source that does not exist.",
    "recoverable": true
  },
  "inline_data": {
    "failed_checks": ["completeness", "source_verification"],
    "details": {
      "approach_2": "Missing 'Methodology' section required by definition_of_done",
      "approach_3": "Citation 'Smith et al. 2024' not found in any indexed source"
    }
  }
}
```
