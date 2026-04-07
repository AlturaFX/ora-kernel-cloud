# Contributing to ORA Kernel

Thanks for your interest in improving ORA Kernel. This document covers the process for contributing.

## How to Contribute

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
