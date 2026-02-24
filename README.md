# agentpr

Lightweight orchestrator for Forge OSS integration runs.

## Quick Start

```bash
cd /Users/yi/Documents/Career/TensorBlcok/agentpr
python3.11 -m orchestrator.cli init-db
python3.11 -m orchestrator.cli doctor --require-codex
```

Create and drive a run:

```bash
python3.11 -m orchestrator.cli create-run \
  --owner OWNER \
  --repo REPO \
  --prompt-version v1

python3.11 -m orchestrator.cli start-discovery --run-id <run_id>
python3.11 -m orchestrator.cli run-prepare --run-id <run_id>
python3.11 -m orchestrator.cli mark-plan-ready --run-id <run_id> --contract-path <path>
python3.11 -m orchestrator.cli start-implementation --run-id <run_id>
python3.11 -m orchestrator.cli run-preflight --run-id <run_id> --codex-sandbox danger-full-access
python3.11 -m orchestrator.cli run-agent-step --run-id <run_id> --prompt-file <prompt.md> --codex-sandbox danger-full-access --success-state NEEDS_HUMAN_REVIEW
python3.11 -m orchestrator.cli mark-local-validated --run-id <run_id>
python3.11 -m orchestrator.cli run-finish --run-id <run_id> --changes "..." --project REPO --commit-title "feat(scope): ..."
```

After manual review, open PR with forced double confirmation:

```bash
python3.11 -m orchestrator.cli request-open-pr \
  --run-id <run_id> \
  --title "feat(scope): ..." \
  --body-file forge_integration/pr_description_template.md

python3.11 -m orchestrator.cli approve-open-pr \
  --run-id <run_id> \
  --request-file <request.json> \
  --confirm-token <token> \
  --confirm
```

Or link PR number manually:

```bash
python3.11 -m orchestrator.cli link-pr --run-id <run_id> --pr-number 123
python3.11 -m orchestrator.cli record-check --run-id <run_id> --conclusion success --pr-number 123
python3.11 -m orchestrator.cli mark-done --run-id <run_id>
```

Inspect state:

```bash
python3.11 -m orchestrator.cli list-runs
python3.11 -m orchestrator.cli show-run --run-id <run_id>
```

Sync GitHub checks/reviews into run states:

```bash
python3.11 -m orchestrator.cli sync-github --dry-run
python3.11 -m orchestrator.cli sync-github --loop --interval-sec 120
```

Run GitHub webhook server:

```bash
export AGENTPR_GITHUB_WEBHOOK_SECRET=...
python3.11 -m orchestrator.cli run-github-webhook --host 0.0.0.0 --port 8787
# local dev only:
# python3.11 -m orchestrator.cli run-github-webhook --allow-unsigned
```

Run Telegram control bot:

```bash
export AGENTPR_TELEGRAM_BOT_TOKEN=...
python3.11 -m orchestrator.cli run-telegram-bot --allow-chat-id <chat_id>
# local dev only:
# python3.11 -m orchestrator.cli run-telegram-bot --allow-any-chat
```

Cleanup old webhook dedup records:

```bash
python3.11 -m orchestrator.cli cleanup-webhook-deliveries --keep-days 30
```

Telegram commands:
- `/list [N]`
- `/show <run_id>`
- `/status <run_id>`
- `/pending_pr [N]`
- `/approve_pr <run_id> <confirm_token>`
- `/pause <run_id>`
- `/resume <run_id> <target_state>`
- `/retry <run_id> <target_state>`

Notes:
- mutable commands now run a startup doctor gate by default (workspace write/tooling/auth/network profile checks).
- run `python3.11 -m orchestrator.cli doctor` for detailed readiness checks before manager loop start.
- use global `--skip-doctor` only for local debugging or controlled recovery workflows.
- `run-agent-step` runs preflight by default. Use `--skip-preflight` only for debugging.
- Use `run-preflight --skip-network-check` when you intentionally run in offline mode.
- `run-preflight` now validates the selected sandbox policy (`--codex-sandbox`).
- `approve-open-pr` requires both `--confirm-token` and `--confirm`.
- `approve-open-pr` needs authenticated GitHub CLI in the repo context (`gh auth status` should pass).
- `sync-github` needs authenticated GitHub CLI with repo read permissions.
- `run-telegram-bot` requires `--allow-chat-id` unless explicitly using `--allow-any-chat` (development only).
- `run-github-webhook` should use a secret (`AGENTPR_GITHUB_WEBHOOK_SECRET`) in production.
- `run-github-webhook` now deduplicates deliveries by `X-GitHub-Delivery` (replay-safe).
- webhook processing failures return `500` and release dedup lock so GitHub retries can re-process.
- Deployment templates are in `agentpr/deploy/systemd/` and `agentpr/deploy/supervisord/`.
- Deployment templates already include startup doctor gate (`ExecStartPre` / `doctor && process`) for Telegram/webhook manager processes.
- To confirm "real readiness" before automation:
  - global: `python3.11 -m orchestrator.cli doctor --require-codex`
  - repo-level: `python3.11 -m orchestrator.cli run-preflight --run-id <run_id> --codex-sandbox danger-full-access`
- For end-to-end git operations, set codex sandbox explicitly when needed:
  `--codex-sandbox danger-full-access`
- Override model with:
  `--codex-model gpt-5.3-codex`
- Current local default is already:
  - `model = "gpt-5.3-codex"`
  - `model_reasoning_effort = "xhigh"`
  so manager runs can omit `--codex-model` unless doing A/B tests.

## Codex Runtime Options

`run-agent-step` supports these codex runtime controls:

- `--codex-sandbox`
  - `read-only`: disallow writes. Not suitable for integration work.
  - `workspace-write`: allow writes in workspace. Suitable for code changes + local test runs.
  - `danger-full-access` (default in this project): unrestricted. Use only in trusted repos/sandboxes.
- `--codex-model`
  - Any model string accepted by codex CLI (for example `gpt-5.3-codex`).
  - If omitted, codex uses local default/profile.
- `--no-codex-full-auto`
  - Disable `--full-auto`.
  - Default behavior keeps no-prompt automation behavior enabled.

Runtime mapping:

- Default command emitted by `run-agent-step`:
  - `codex exec --sandbox danger-full-access --ask-for-approval on-request "<prompt>"`
- If sandbox is not `workspace-write` and full-auto behavior is enabled:
  - `codex exec --sandbox <mode> --ask-for-approval on-request "<prompt>"`
- If `--codex-model` is set:
  - `codex exec --sandbox <mode> ... --model <model> "<prompt>"`
- If `--no-codex-full-auto` is set:
  - `codex exec --sandbox <mode> [--model <model>] "<prompt>"`

Sandbox behavior in practice:

- `read-only`: can inspect repo, cannot reliably create env/lock files/write test artifacts.
- `workspace-write`: can modify repo files; env/test success still depends on host network and toolchain.
- `danger-full-access`: no sandbox restrictions from codex; use only in trusted local/container environments.

No-sandbox (danger-full-access) guardrails now implemented by orchestrator:

1. Runtime env isolation:
   - Tool caches/data are redirected to `<repo>/.agentpr_runtime/*`
   - Python global installs are blocked via `PIP_REQUIRE_VIRTUALENV=true`
   - npm/bun global install prefixes are redirected to repo-local dirs
2. Prompt safety contract:
   - Worker is explicitly instructed to avoid out-of-repo file operations and global installs
   - `sudo` is explicitly disallowed
3. Workspace boundary check:
   - preflight fails if run workspace is outside configured `--workspace-root`
4. Extensible runtime policy:
   - `orchestrator/runtime_env_overrides.json` controls environment overrides without code changes
   - use placeholders: `{repo_dir}`, `{runtime_dir}`, `{cache_dir}`, `{data_dir}`, `{tmp_dir}`

Important:
- This is a practical safety layer, not a formal security sandbox.
- For hard isolation guarantees, run manager/worker inside a disposable container or VM.

Recommended presets:

- Standard integration run:
  - `--codex-sandbox danger-full-access`
- Debug or constrained environments:
  - `--no-codex-full-auto`
- Tight safety fallback:
  - `--codex-sandbox workspace-write`

## Can It Build Env And Run Tests?

Yes, if preflight passes and repo commands are valid.

Minimum requirements before starting worker run:

1. Toolchain present (`git`, `python3.11` or `bun`, etc.)
2. Network reachable for dependency registries (PyPI/npm) unless dependencies are already present
3. Workspace writable under selected codex sandbox mode
4. `.git` writable if you expect commit/push in the same run

Preflight outputs are saved to:

- `agentpr/orchestrator/data/reports/<run_id>_preflight.json`

Agent runtime reports are saved to:

- `agentpr/orchestrator/data/reports/<run_id>_agent_runtime_<timestamp>.json`
- include command samples, detected test/git signals, safety-violation signals, and auto classification verdict:
  - `PASS`
  - `RETRYABLE`
  - `HUMAN_REVIEW`

Classification behavior:

1. `PASS`
   - exit code is 0, no safety violations
   - and (for implementation/validation states) test command evidence is present
2. `RETRYABLE`
   - transient/runtime failures (network/timeout/rate-limit-like signals)
3. `HUMAN_REVIEW`
   - safety violations, hard permission/auth/tooling failures, or missing test evidence

`run-agent-step` state behavior with classification:

1. non-zero exit + `HUMAN_REVIEW` -> state converges to `NEEDS_HUMAN_REVIEW`
2. non-zero exit + `RETRYABLE` -> state remains `FAILED_RETRYABLE`
3. zero exit + non-`PASS` -> command returns non-zero and state converges by verdict (`NEEDS_HUMAN_REVIEW` or `FAILED_RETRYABLE`)
4. zero exit + `PASS` -> optional `--success-state` is applied

## Current Status (2026-02-24)

1. Baseline runs (`mem0`, `dexter`) confirm codex can read rules/docs and produce minimal code changes.
2. Required repo test/lint commands were attempted, but dependency install was blocked by network in this environment.
3. Commit/push was blocked by `.git/index.lock` permission in worker sandbox context.
4. `finish.sh` commit title validation bug was fixed (single-line check + empty-title check).
5. MVP default sandbox is now `danger-full-access` with runtime guardrails and workspace-boundary preflight checks.
6. Structured runtime report is now generated for each agent attempt, with automatic verdict classification and artifact metadata (`grade/reason_code/next_action`).
7. Phase B PR gate MVP is implemented: `request-open-pr` + `approve-open-pr --confirm` (double confirmation).
8. Phase B manager loop is now available: `sync-github` for GitHub state sync and `run-telegram-bot` for remote control commands.
9. Phase B webhook ingress is available: `run-github-webhook` validates signatures and maps GitHub events to state-machine updates.
10. Webhook replay hardening is enabled via delivery-id dedup records and cleanup command.
11. Telegram bot now defaults to allowlist-only mode; deployment templates are included under `deploy/`.
12. Startup doctor + automatic gate is now implemented to fail fast on environment/auth/network prerequisites.

## Insights (Conversation)

1. Primary bottleneck is execution environment quality (network/.git write), not state-machine complexity.
2. Manager should own final push/PR gate decisions; worker should prioritize patch/report quality.
3. Non-interactive baseline must be measured first before expanding control-plane complexity.
4. "Skills" should be treated as contracts/boundaries, not necessarily separate CLI invocations.
5. `danger-full-access` is acceptable only with explicit guardrails and preferably container/VM isolation.
6. Unknown future toolchains should be handled by extending `orchestrator/runtime_env_overrides.json` first, then code only if needed.
