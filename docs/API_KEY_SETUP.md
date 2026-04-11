# API Key Setup for ORA Kernel Cloud

## Get Your API Key

1. Go to https://console.anthropic.com
2. Sign in with your Anthropic account
3. Navigate to **Settings → API Keys**
4. Click **Create Key**, name it `ora-kernel-cloud`
5. Copy the key (starts with `sk-ant-...`) — you only see it once

## Add Billing

API usage is billed separately from your Claude Pro/Max subscription.

1. In the Console: **Settings → Billing**
2. Add a payment method
3. Set a monthly spend limit (recommended: start at $50)

## Store the Key Securely

### Option A: Environment variable (recommended for development)

Add to your shell profile (`~/.bashrc` or `~/.zshrc`):

```bash
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

Then reload: `source ~/.bashrc`

The `anthropic` Python SDK reads this automatically — no code changes needed.

### Option B: `.env` file (recommended for project use)

Create a `.env` file in the `ora-kernel-cloud` project root:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-your-key-here' > .env
```

The orchestrator loads it via `python-dotenv` or reads it manually.

### Option C: System keyring (most secure)

```bash
# Store
python3 -c "import keyring; keyring.set_password('ora-kernel', 'api_key', 'sk-ant-your-key-here')"

# Retrieve (in your code)
python3 -c "import keyring; print(keyring.get_password('ora-kernel', 'api_key'))"
```

Requires `pip install keyring`. Uses your OS keychain (macOS Keychain, GNOME Keyring, Windows Credential Vault).

## Keep It Out of Git

These entries are already in the `.gitignore`:

```
# API keys and secrets
.env
*.local.json
```

If you haven't already, verify `.env` is gitignored:

```bash
echo '.env' >> .gitignore
```

### What NOT to do

- Never put the key in `config.yaml`, `settings.json`, or any tracked file
- Never pass it as a command-line argument (visible in process list)
- Never commit it to git — even in a "temporary" commit. If you accidentally commit a key, rotate it immediately at https://console.anthropic.com/settings/keys

## Verify It Works

```bash
# Quick test — should return your account info
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
```

If you get a JSON response with `"type": "message"`, your key is working.

## Cost Expectations

Updated 2026-04-10 with real numbers from the dispatch feasibility spike and D17 smoke test. Rates are Anthropic Opus 4.6 as of April 2026 ($5/M input, $25/M output).

### Per-action envelope

| Activity | Approximate Cost |
|---|---|
| Managed Agent session (idle) | Free ($0 when idle) |
| Managed Agent session (running) | $0.05/hr container runtime (50 free hrs/day) + token costs |
| `/heartbeat` (12/day, mostly silent) | ~$0.05/day |
| `/briefing` (1/day) | ~$0.15/day |
| `/idle-work` (2–3 tasks/night) | ~$1–3/day depending on task |
| `/sync-snapshot` (4/day with inline protocol ~250 tokens) | ~$0.01/day |
| `/consolidate` (weekly) | ~$0.50/week |
| **Dispatch — trivial** (`smoke_test_node`: 3 in / 73 out) | ~$0.002 per call |
| **Dispatch — realistic** (~3 000 in / 500 out) | ~$0.0275 per call |
| **Dispatch — full Quad** (4 nodes: Domain + Task + 2 verifiers) | ~$0.11 per task |
| Self-improvement cycle (if dispatched as a Quad) | ~$2–5/week |

### Monthly envelope

| Usage profile | Monthly cost |
|---|---|
| **Light** — a few `/heartbeat` days, occasional `/briefing`, no dispatches | **$30–60** |
| **Medium** — all cron triggers on + 1–2 dispatched tasks/day | **$60–120** |
| **Heavy** — all triggers + 5–10 full Quads/day + self-improvement | **$120–250** |

### What drives the numbers

- **Container runtime** is charged at $0.05/hr BEYOND 50 free hours/day. A single always-on session uses ~24 hrs/day, still well within the free envelope.
- **Parent session tokens** — dominated by input context growth as the conversation extends. Prompt caching helps significantly for repeated triggers.
- **Dispatch sub-session tokens** — each dispatch is a fresh conversation, so there is no input-context bloat. Cost is roughly linear in the complexity of the task.
- **Dispatch setup overhead** — `agents.create` + `sessions.create` ≈ 0.7s combined, negligible cost (no tokens). Creating many short-lived sub-sessions is cheaper than keeping the parent in a long multi-turn conversation.

### Cost observability

- **Parent session totals** — `cloud_sessions.total_cost_usd`, updated from `span.model_request_end` events
- **Per-dispatch costs** — `dispatch_sessions.cost_usd`, one row per sub-session
- **Rollup for a task** — no single query yet; sum `cloud_sessions.total_cost_usd` + `SUM(dispatch_sessions.cost_usd)` by `parent_session_id`. This rollup view is on the backlog (see `docs/next_steps.md`).

Runtime charges only accrue while the agent is in `running` status — not while idle waiting for the next event. An always-on session that mostly waits costs very little in runtime.
