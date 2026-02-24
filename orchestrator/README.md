# Orchestrator Notes

This package provides a minimal Phase A implementation:

1. SQLite-backed run/event/state storage
2. Validated state transitions
3. Idempotent event ingestion (`run_id + idempotency_key`, deterministic default keys)
4. Integration with:
- `forge_integration/scripts/prepare.sh`
- `forge_integration/scripts/finish.sh`
5. Non-interactive agent execution hook (`codex exec`)
6. Environment preflight checks before worker execution
7. Startup doctor gate for manager/worker prerequisite validation

## Core Modules

1. `models.py`: enums and event/run data contracts
2. `state_machine.py`: allowed transitions + transition validation
3. `db.py`: schema and low-level persistence
4. `service.py`: event application and state changes
5. `executor.py`: shell script execution wrapper
6. `preflight.py`: repo preflight checks + startup doctor checks
7. `cli.py`: operator-facing command interface
8. Worker runtime isolation: local cache/data dirs under `<repo>/.agentpr_runtime`
9. Runtime env override file: `runtime_env_overrides.json` (toolchain extensibility without code changes)
10. Runtime verdict classification: `PASS` / `RETRYABLE` / `HUMAN_REVIEW`
11. PR gate commands: `request-open-pr` -> `approve-open-pr --confirm` (double confirmation)
12. `github_sync.py`: gh PR payload -> check/review sync decisions
13. `telegram_bot.py`: Telegram long-poll command loop for manager actions
14. `github_webhook.py`: webhook signature verification + event ingestion server
15. Webhook delivery dedup ledger (`webhook_deliveries`) + cleanup command
16. Automatic startup doctor gate on mutable CLI commands (`--skip-doctor` override)

Operational commands added:

1. `sync-github`
2. `run-telegram-bot`
3. `run-github-webhook`
4. `cleanup-webhook-deliveries`
5. `doctor`

## Design Constraints

1. Keep transitions explicit and fail-fast on illegal commands.
2. Keep commands idempotent-friendly by requiring unique event keys.
3. Keep failure paths observable via `step_attempts` and `events`.
4. Keep completion explicit with `mark-done` instead of implicit review auto-close.
5. Fail fast when environment cannot install dependencies or write `.git`.
6. Treat `codex --sandbox read-only` as non-executable for integration runs.
7. Fail fast when run workspace is outside configured workspace root.
8. Persist structured runtime reports for each agent attempt.
9. Persist deterministic runtime verdict with reason code and next action.
10. Keep GitHub sync idempotent-friendly by reusing state-machine event keys.
11. Keep webhook ingress replay-safe using delivery-id reservation before processing.
12. On webhook processing failure, release delivery reservation and return retryable status.
13. Keep Telegram control plane default-deny (allowlist required unless explicitly overridden).
14. Fail fast on startup prerequisites before mutating state or invoking worker actions.
