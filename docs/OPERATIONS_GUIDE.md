# agentpr

Lightweight orchestrator for Forge OSS integration runs.

## Quick Start

```bash
cd /Users/yi/Documents/Career/TensorBlcok/agentpr
cp .env.example .env          # fill in required vars
python3.11 -m orchestrator.cli init-db
python3.11 -m orchestrator.cli doctor --require-codex
python3.11 -m orchestrator.cli install-skills --install-curated-ci
python3.11 -m orchestrator.cli skills-status
```

`orchestrator.cli` auto-loads `.env` at startup (only fills env vars not already set).

### Typical run (V2 flow)

```bash
# 1. Create a run (auto-kicks into QUEUED → EXECUTING)
python3.11 -m orchestrator.cli create-run \
  --owner mem0ai --repo mem0 \
  --prompt-version v1

# 2. Record the run ID
python3.11 -m orchestrator.cli list-runs --limit 1
RUN_ID=<your_run_id>

# 3. Drive it with the manager loop
python3.11 -m orchestrator.cli run-manager-loop \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous \
  --codex-sandbox danger-full-access \
  --interval-sec 30 \
  --max-loops 20

# 4. When state reaches PUSHED, approve PR (double confirmation)
python3.11 -m orchestrator.cli request-open-pr \
  --run-id $RUN_ID \
  --title "feat(scope): ..."

python3.11 -m orchestrator.cli approve-open-pr \
  --run-id $RUN_ID \
  --confirm-token <token> \
  --confirm
```

Expected state flow: `QUEUED → EXECUTING → PUSHED → (human approve) → DONE`

---

## run-manager-loop Parameters

```
python3.11 -m orchestrator.cli run-manager-loop [options]
```

| Parameter | Values / Default | What it does |
|---|---|---|
| `--run-id` | run ID string | Drive a specific run. If omitted, loops over all active runs. |
| `--decision-mode` | `rules` \| `hybrid` \| `llm`; default `rules` | How the manager makes decisions. `rules` = deterministic only. `hybrid` = rules + LLM semantic judgment (needs API key). `llm` = LLM only. |
| `--skills-mode` | `off` \| `agentpr` \| `agentpr_autonomous`; default from policy | Worker execution mode. `off` = single prompt. `agentpr` = staged skill chain. `agentpr_autonomous` = worker self-orchestrates analyze→implement→validate in one run. |
| `--codex-sandbox` | `danger-full-access` \| `workspace-write` \| `read-only`; default `danger-full-access` | Codex execution sandbox. **Use `danger-full-access` for real integration runs** (git commit/push, install deps, run tests). `workspace-write` is too restrictive for full runs. |
| `--interval-sec` | integer; default `300` | Seconds to wait between manager ticks. |
| `--max-loops` | integer; default unlimited | Stop after this many ticks (useful for one-shot runs). |
| `--prompt-file` | file path | Worker base prompt. Defaults to `AGENTPR_WORKER_PROMPT_FILE` env or `forge_integration/claude_code_prompt.md`. |
| `--prompt-version` | `v1`, `v2`, ... | Prompt version override. Defaults to `AGENTPR_DEFAULT_PROMPT_VERSION` env. Recorded which version of prompt used |
| `--manager-api-base` | URL | Manager LLM endpoint. Defaults to `AGENTPR_MANAGER_API_BASE`. |
| `--manager-model` | model string | Manager LLM model. Defaults to `AGENTPR_MANAGER_MODEL`. |
| `--manager-timeout-sec` | integer; default `20` | Timeout for each Manager LLM call. |
| `--policy-file` | file path; default `orchestrator/manager_policy.json` | Manager policy JSON (sandbox, diff budget, retry cap, etc.). |
| `--dry-run` | flag | Print what the manager would do without executing. |
| `--skip-doctor` | flag | Skip startup environment check. Use only for debugging. |

### Single tick (debugging)

```bash
# See what the manager would do, without executing
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --dry-run

# Execute one tick
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous
```

---

## Environment Variables

### Manager LLM (`--decision-mode hybrid|llm`)

| Variable | Default | Description |
|---|---|---|
| `AGENTPR_MANAGER_API_KEY` | — | **Required** for hybrid/llm mode. |
| `AGENTPR_MANAGER_MODEL` | `gpt-4o-mini` | Manager LLM model. |
| `AGENTPR_MANAGER_API_BASE` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint (e.g. Forge gateway). |

### Worker (codex) — Forge provider

By default, codex uses `~/.codex/config.toml` (global, project-independent). To override **only for this project**, set these in `.env`:

| Variable | Default | Description |
|---|---|---|
| `AGENTPR_FORGE_BASE_URL` | — | Forge API base URL. If set together with `AGENTPR_FORGE_API_KEY`, codex uses Forge instead of its global default. |
| `AGENTPR_FORGE_API_KEY` | — | Forge API key. |
| `AGENTPR_FORGE_MODEL | — | Model name on Forge. If omitted, uses `~/.codex/config.toml` model. |
| `AGENTPR_CODEX_CONFIG_OVERRIDES` | — | A TOML-formatted string to override `codex`'s `config.toml` settings. E.g., `'model_providers.default.wire_api="responses"'` |

When either var is missing, codex falls back to `~/.codex/config.toml` (currently `gpt-5.3-codex`).

### Worker prompt

| Variable | Default | Description |
|---|---|---|
| `AGENTPR_WORKER_PROMPT_FILE` | `forge_integration/claude_code_prompt.md` | Base prompt injected into every worker run. |
| `AGENTPR_DEFAULT_PROMPT_VERSION` | `v1` | Prompt version used by `/create` in bot mode. |
| `AGENTPR_CREATE_AUTOKICK` | `1` | After `/create`, auto-run one manager tick. Set `0` to disable. |

### Telegram bot

| Variable | Default | Description |
|---|---|---|
| `AGENTPR_TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather. |
| `AGENTPR_TELEGRAM_NL_MODE` | `hybrid` | NL routing mode: `rules` \| `hybrid` \| `llm`. |
| `AGENTPR_TELEGRAM_NL_MODEL` | fallback to `AGENTPR_MANAGER_MODEL` | NL router LLM model. |
| `AGENTPR_TELEGRAM_NL_API_BASE` | fallback to `AGENTPR_MANAGER_API_BASE` | NL router LLM endpoint. |
| `AGENTPR_TELEGRAM_NL_API_KEY_ENV` | `AGENTPR_MANAGER_API_KEY` | Name of the env var holding NL router API key. |
| `AGENTPR_TELEGRAM_NL_TIMEOUT_SEC` | `20` | NL router LLM timeout. |
| `AGENTPR_TELEGRAM_NOTIFY_ENABLED` | `1` | Enable proactive state notifications. |
| `AGENTPR_TELEGRAM_NOTIFY_SCAN_SEC` | `30` | How often bot scans for new notifications (seconds). |
| `AGENTPR_TELEGRAM_NOTIFY_SCAN_LIMIT` | `200` | Max artifacts to scan per cycle. |

### Telegram Decision Card (`/show`, `/status`)

| Variable | Default | Description |
|---|---|---|
| `AGENTPR_TELEGRAM_DECISION_WHY_MODE` | `hybrid` | `off` \| `hybrid` \| `llm` — whether to show LLM explanation in Decision Card. |
| `AGENTPR_TELEGRAM_DECISION_API_KEY_ENV` | `AGENTPR_MANAGER_API_KEY` | API key env var for Decision Card LLM. |
| `AGENTPR_TELEGRAM_DECISION_MODEL` | fallback to `AGENTPR_MANAGER_MODEL` | Decision Card LLM model. |
| `AGENTPR_TELEGRAM_DECISION_API_BASE` | fallback to `AGENTPR_MANAGER_API_BASE` | Decision Card LLM endpoint. |

### Misc

| Variable | Default | Description |
|---|---|---|
| `AGENTPR_BASE_DIR` | `workspaces/` | Where repos are cloned. |
| `AGENTPR_CODEX_BIN` | auto-detected | Override codex binary path (useful when codex isn't on PATH). |
| `AGENTPR_GITHUB_WEBHOOK_SECRET` | — | HMAC secret for GitHub webhook validation. |

---

## Whole-System Runtime

Three processes for full automation:

```
process 1: run-telegram-bot       # human interaction / NL ingress
process 2: run-manager-loop       # queue progression / manager decisions
process 3: run-github-webhook     # CI/review feedback ingestion
            OR sync-github --loop # fallback (polling instead of webhook)
```

- Without manager loop: runs won't advance automatically.
- Without webhook/sync: CI and review feedback won't close the loop.

---

## Inspect & Debug

```bash
# Full run details (state, artifacts, events, Decision Card)
python3.11 -m orchestrator.cli show-run --run-id $RUN_ID

# Manager-facing diagnostics (timing, steps, events)
python3.11 -m orchestrator.cli inspect-run --run-id $RUN_ID
python3.11 -m orchestrator.cli inspect-run --run-id $RUN_ID --include-log-tails

# Worker grading result
python3.11 -m orchestrator.cli analyze-worker-output --run-id $RUN_ID

# Global stats (pass rate, grade distribution, top reason codes)
python3.11 -m orchestrator.cli get-global-stats

# Cross-run bottleneck analysis
python3.11 -m orchestrator.cli run-bottlenecks --limit 20

# All runs
python3.11 -m orchestrator.cli list-runs

# Pause / resume / retry
python3.11 -m orchestrator.cli pause --run-id $RUN_ID
python3.11 -m orchestrator.cli resume --run-id $RUN_ID --target-state EXECUTING
python3.11 -m orchestrator.cli retry --run-id $RUN_ID --target-state EXECUTING
```

---

## Codex Runtime Options

`run-agent-step` (and indirectly `run-manager-loop`) codex controls:

| Flag | Values / Default | Description |
|---|---|---|
| `--codex-sandbox` | `danger-full-access` (default) \| `workspace-write` \| `read-only` | Sandbox mode. **Use `danger-full-access` for real runs** — git commits, installs, test execution all need it. `workspace-write` blocks too much. |
| `--codex-model` | model string | Override model for this run. If omitted, uses `AGENTPR_FORGE_MODEL` (if Forge configured) or `~/.codex/config.toml`. |
| `--no-codex-full-auto` | flag | Disable `--full-auto`. Default keeps it enabled. |
| `--max-agent-seconds` | integer; default `900` | Hard timeout for one codex execution. `0` = no timeout. |
| `--allow-agent-push` | flag | Allow worker to git commit/push directly. Default off — manager handles push gate. |
| `--allow-read-path` | path (repeatable) | Extra read-only paths for worker (e.g. external context files). |
| `--max-changed-files` | integer | Diff budget: max files changed. Default from policy. |
| `--max-added-lines` | integer | Diff budget: max lines added. Default from policy. |
| `--allow-dirty-worktree` | flag | Allow running with pre-existing workspace changes. Default blocks. |
| `--skills-mode` | `off` \| `agentpr` \| `agentpr_autonomous` | Worker skills mode (see Skills Chain section). |
| `--allow-missing-skills` | flag | Continue even if a required skill is missing. Default fails. |
| `--task-packet-file` | file path | Merge extra JSON/Markdown into the generated task packet. |
| `--max-retryable-attempts` | integer | Retry cap before escalating to HUMAN_REVIEW. Default from policy. |
| `--min-test-commands` | integer | Min test/lint evidence commands required for PASS verdict. Default from policy. |
| `--runtime-grading-mode` | `rules` \| `hybrid` \| `hybrid_llm` | Grading mode. `rules` = deterministic. `hybrid` = + no-test-infra semantic override. `hybrid_llm` = + LLM semantic grading when API key available. |

Sandbox behavior summary:

- `read-only`: inspect only, cannot write files or run builds.
- `workspace-write`: write repo files, but git/network/install operations may be blocked.
- `danger-full-access`: no restrictions from codex. Orchestrator adds guardrails (cache isolation, safety contract, workspace boundary check).

---

## No-Sandbox Guardrails (danger-full-access)

Even without codex sandbox, orchestrator enforces:

1. **Runtime env isolation**: tool caches redirected to `<repo>/.agentpr_runtime/*`; `PIP_REQUIRE_VIRTUALENV=true` blocks global pip installs; npm/bun prefixes redirected.
2. **Prompt safety contract**: worker explicitly told no out-of-repo writes, no sudo, no global installs.
3. **Workspace boundary check**: preflight fails if workspace is outside configured root.
4. **Diff budget**: `max_changed_files` / `max_added_lines` caps enforced post-run.

Configurable without code changes via `orchestrator/runtime_env_overrides.json` (supports `{repo_dir}`, `{runtime_dir}`, `{cache_dir}`, `{data_dir}`, `{tmp_dir}` placeholders).

> This is a practical safety layer, not a formal security sandbox. For hard isolation, run in a container or VM.

---

## Manager Policy

`orchestrator/manager_policy.json` controls all defaults. CLI flags override per command.

Key `run_agent_step` fields:

| Field | Default | Description |
|---|---|---|
| `codex_sandbox` | `danger-full-access` | Default sandbox for worker runs. |
| `skills_mode` | `off` | Default skills mode. |
| `max_agent_seconds` | `900` | Worker execution timeout. |
| `max_changed_files` | `6` | Diff budget: files. |
| `max_added_lines` | `120` | Diff budget: lines. |
| `max_retryable_attempts` | `3` | Retries before HUMAN_REVIEW. |
| `min_test_commands` | `1` | Test evidence threshold. |
| `runtime_grading_mode` | `hybrid` | Grading mode. |
| `known_test_failure_allowlist` | `[]` | Regex patterns for acceptable baseline test failures. |
| `repo_overrides` | `{}` | Per-repo override for any field above. |

Policy file example:

```json
{
  "run_agent_step": {
    "codex_sandbox": "danger-full-access",
    "skills_mode": "off",
    "max_agent_seconds": 900,
    "max_changed_files": 6,
    "max_added_lines": 120,
    "max_retryable_attempts": 3,
    "min_test_commands": 1,
    "runtime_grading_mode": "hybrid",
    "known_test_failure_allowlist": [],
    "repo_overrides": {
      "mem0ai/mem0": {
        "skills_mode": "agentpr",
        "max_agent_seconds": 780,
        "max_changed_files": 3,
        "max_added_lines": 45,
        "max_retryable_attempts": 2,
        "min_test_commands": 2
      }
    }
  },
  "telegram_bot": {
    "poll_timeout_sec": 30,
    "idle_sleep_sec": 2,
    "list_limit": 20,
    "rate_limit_window_sec": 60,
    "rate_limit_per_chat": 12,
    "rate_limit_global": 120,
    "audit_log_file": "orchestrator/data/reports/telegram_audit.jsonl"
  },
  "github_webhook": {
    "max_payload_bytes": 1048576,
    "audit_log_file": "orchestrator/data/reports/github_webhook_audit.jsonl"
  }
}
```

---

## Skills Chain

`--skills-mode` controls how worker uses skills:

1. **`agentpr`** (staged): manager selects skill per state.
   - `EXECUTING/DISCOVERY/PLAN_READY` → `preflight-contract`
   - `IMPLEMENTING/LOCAL_VALIDATING` → `implement-and-validate`
   - `ITERATING/CI_WAIT/REVIEW_WAIT` → `ci-review-fix`

2. **`agentpr_autonomous`** (worker autonomous): manager provides skill set + task packet; worker decides invocation order for analyze→implement→validate in one run.

Skills are versioned in `agentpr/skills/`, installed to `~/.codex/skills/`:

```bash
python3.11 -m orchestrator.cli install-skills --install-curated-ci
python3.11 -m orchestrator.cli skills-status

# Per-run metrics
python3.11 -m orchestrator.cli skills-metrics --limit 200
python3.11 -m orchestrator.cli skills-feedback --limit 300
```

---

## State Machine

```
QUEUED → EXECUTING → PUSHED → CI_WAIT → REVIEW_WAIT → DONE
                                  ↕           ↕
                              ITERATING ← ─ ─ ┘
+ PAUSED       (any non-terminal state)
+ NEEDS_HUMAN  (escalation)
+ FAILED       (terminal)
```

---

## Telegram Bot

```bash
python3.11 -m orchestrator.cli run-telegram-bot \
  --allow-chat-id <chat_id>
# per-tier ACL:
# --write-chat-id <chat_id> --admin-chat-id <chat_id>
```

Commands:

| Command | Tier | Description |
|---|---|---|
| `/start` `/help` | read | Help text. |
| `/overview` | read | Global stats: pass rate, grade distribution, top reason codes. |
| `/list [N]` | read | List recent runs. |
| `/show <run_id>` | read | Run detail + Decision Card (why_machine + why_llm). |
| `/status <run_id>` | read | Short status. |
| `/pending_pr [N]` | read | Runs waiting for PR approval. |
| `/create <owner/repo> [--prompt-version vX]` | write | Create new run (supports multiple repos). |
| `/pause <run_id>` | write | Pause run. |
| `/resume <run_id> <target_state>` | write | Resume to target state. |
| `/retry <run_id> <target_state>` | write | Retry from target state. |
| `/approve_pr <run_id> <token>` | admin | Approve PR (double confirmation). |

Plain text (no `/`) → natural language routing → manager LLM → orchestrator action.

---

## GitHub Integration

```bash
# Webhook server (preferred)
export AGENTPR_GITHUB_WEBHOOK_SECRET=...
python3.11 -m orchestrator.cli run-github-webhook --host 0.0.0.0 --port 8787

# Polling fallback
python3.11 -m orchestrator.cli sync-github --loop --interval-sec 120
```

---

## PR Gate

```bash
# Generate PR request + confirmation token
python3.11 -m orchestrator.cli request-open-pr \
  --run-id $RUN_ID \
  --title "feat(scope): ..."
# optional: --body-file <path> --skip-repo-pr-template --skip-about-forge

# Approve (requires both --confirm-token and --confirm)
python3.11 -m orchestrator.cli approve-open-pr \
  --run-id $RUN_ID \
  --confirm-token <token> \
  --confirm
# emergency only: --allow-dod-bypass
```

`approve-open-pr` enforces a DoD gate: latest `run_digest` must pass + policy thresholds + contract evidence. Merge is always manual.

---

## Artifacts & Reports

All saved under `orchestrator/data/reports/`:

| Artifact | Description |
|---|---|
| `<run_id>_preflight.json` | Preflight environment check result. |
| `<run_id>_agent_runtime_<ts>.json` | Per-attempt runtime report: commands, grade, reason_code, verdict. |
| `<run_id>_run_digest_<ts>.json` | Structured deterministic run summary (machine-checkable truth). |
| `<run_id>_manager_insight_<ts>.md` | Manager-facing markdown insight (decision support, not source of truth). |
| `<run_id>_agent_events_<ts>.jsonl` | Raw codex event stream. |

Task packets saved under `orchestrator/data/task_packets/`.

---

## Deployment

Templates under `deploy/`:
- `deploy/systemd/` and `deploy/supervisord/` — process management
- `deploy/nginx/` and `deploy/cloudflare/` — public ingress for webhook
- `deploy/scripts/webhook_probe.py` — liveness probe

Each template includes startup doctor gate (`ExecStartPre` / `doctor && process`).

---

## Notes

- `doctor --require-codex` checks full env readiness before running.
- `run-preflight --run-id <id> --codex-sandbox danger-full-access` does repo-level checks.
- `--skip-doctor` and `--skip-preflight` are for debugging only.
- If `doctor` fails on `cmd.codex`, set `AGENTPR_CODEX_BIN` to the absolute codex path.
- `approve-open-pr` needs authenticated `gh` CLI (`gh auth status` must pass).
- `sync-github` needs `gh` with repo read permissions.
- `run-telegram-bot` requires `--allow-chat-id` in production (`--allow-any-chat` is dev only).
