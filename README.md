# agentpr

Lightweight orchestrator for Forge OSS integration runs.

## Quick Start

```bash
cd /Users/yi/Documents/Career/TensorBlcok/agentpr
python3 -m orchestrator.cli init-db
```

Create and drive a run:

```bash
python3 -m orchestrator.cli create-run \
  --owner OWNER \
  --repo REPO \
  --prompt-version v1

python3 -m orchestrator.cli start-discovery --run-id <run_id>
python3 -m orchestrator.cli run-prepare --run-id <run_id>
python3 -m orchestrator.cli mark-plan-ready --run-id <run_id> --contract-path <path>
python3 -m orchestrator.cli start-implementation --run-id <run_id>
python3 -m orchestrator.cli run-agent-step --run-id <run_id> --engine codex --prompt-file <prompt.md>
python3 -m orchestrator.cli mark-local-validated --run-id <run_id>
python3 -m orchestrator.cli run-finish --run-id <run_id> --changes "..." --project REPO --commit-title "feat(scope): ..."
```

After manual review, link PR number:

```bash
python3 -m orchestrator.cli link-pr --run-id <run_id> --pr-number 123
python3 -m orchestrator.cli record-check --run-id <run_id> --conclusion success --pr-number 123
python3 -m orchestrator.cli mark-done --run-id <run_id>
```

Inspect state:

```bash
python3 -m orchestrator.cli list-runs
python3 -m orchestrator.cli show-run --run-id <run_id>
```
