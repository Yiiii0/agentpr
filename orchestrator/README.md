# Orchestrator Notes

This package provides a minimal Phase A implementation:

1. SQLite-backed run/event/state storage
2. Validated state transitions
3. Idempotent event ingestion (`run_id + idempotency_key`, deterministic default keys)
4. Integration with:
- `forge_integration/scripts/prepare.sh`
- `forge_integration/scripts/finish.sh`
5. Non-interactive agent execution hook (`codex exec` / `claude -p`)

## Core Modules

1. `models.py`: enums and event/run data contracts
2. `state_machine.py`: allowed transitions + transition validation
3. `db.py`: schema and low-level persistence
4. `service.py`: event application and state changes
5. `executor.py`: shell script execution wrapper
6. `cli.py`: operator-facing command interface

## Design Constraints

1. Keep transitions explicit and fail-fast on illegal commands.
2. Keep commands idempotent-friendly by requiring unique event keys.
3. Keep failure paths observable via `step_attempts` and `events`.
4. Keep completion explicit with `mark-done` instead of implicit review auto-close.
