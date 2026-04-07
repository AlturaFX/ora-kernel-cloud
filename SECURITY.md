# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in ORA Kernel, please report it responsibly:

1. **Do not** open a public GitHub issue
2. Email: fxaltura@gmail.com with subject "ORA Kernel Security"
3. Include: description, reproduction steps, and potential impact

You will receive a response within 72 hours.

## Security Model

ORA Kernel enforces safety through multiple layers:

- **safety_check.sh** — blocks `rm` and `sudo` commands (including chained/nested)
- **protect_core.py** — blocks modification of constitution, schemas, hooks, and core config
- **loop_detector.py** — prevents infinite retry loops with escalation
- **anti_poll.py** — throttles excessive status polling

These hooks are the enforcement layer. They cannot be bypassed by the LLM — they run at the Claude Code harness level.

## Known Limitations

- Hooks run in the user's shell environment. A user with direct shell access can bypass hooks by editing settings.json or running commands outside Claude Code.
- The `rm` blocker uses word-boundary matching (`\brm\b`). Obfuscated commands (e.g., using eval or variable expansion) could theoretically bypass it.
- PostgreSQL credentials in docker-compose.yml use environment variable defaults. Always override with strong passwords in production.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |
