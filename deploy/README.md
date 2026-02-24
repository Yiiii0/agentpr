# Deployment Templates

This folder contains production-oriented process templates for manager components:

1. `systemd/agentpr-telegram.service`
2. `systemd/agentpr-github-webhook.service`
3. `supervisord/agentpr-manager.conf`

Before use:

1. Replace all `CHANGE_ME` placeholders.
2. Replace `/ABSOLUTE/PATH/TO/TensorBlcok/agentpr` with your actual path.
3. Ensure `python3.11` path is correct on host.
4. For Telegram bot, configure at least one allowed chat id.
5. For GitHub webhook, set `AGENTPR_GITHUB_WEBHOOK_SECRET` and configure same secret in GitHub.
6. Keep startup doctor gate enabled in service templates (already wired by default).

## How To Confirm Environment Is Really Ready

Run these checks in order:

1. Global manager/worker readiness (network + auth + codex):
```bash
python3.11 -m orchestrator.cli doctor --require-codex
```

2. If manager-only components are starting (Telegram/webhook), use profile checks:
```bash
python3.11 -m orchestrator.cli doctor --skip-network-check --no-require-gh-auth --require-telegram-token
python3.11 -m orchestrator.cli doctor --skip-network-check --no-require-gh-auth --require-webhook-secret
```

3. Repo-level execution readiness (`.git` writable + toolchain + package source reachability):
```bash
python3.11 -m orchestrator.cli run-preflight --run-id <run_id> --codex-sandbox danger-full-access
```

Interpretation:

1. `doctor` failing on `net.*` means host/network/DNS problem, not workflow problem.
2. `doctor` failing on `gh.auth` means credentials problem (`gh auth login` / token scopes).
3. `run-preflight` failing on `git.write` means that repo `.git` is not writable for worker process.
4. Start manager/worker only when `doctor` and `run-preflight` are both `ok=true`.
