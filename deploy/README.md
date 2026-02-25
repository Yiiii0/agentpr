# Deployment Templates

This folder contains production-oriented process templates for manager components:

1. `systemd/agentpr-telegram.service`
2. `systemd/agentpr-github-webhook.service`
3. `supervisord/agentpr-manager.conf`

Before use:

1. Replace all `CHANGE_ME` placeholders.
2. Replace `/ABSOLUTE/PATH/TO/TensorBlcok/agentpr` with your actual path.
3. Ensure `python3.11` path is correct on host.
4. For Telegram bot, configure `allow/write/admin` chat ids (or intentionally collapse them to one chat id).
5. For GitHub webhook, set `AGENTPR_GITHUB_WEBHOOK_SECRET` and configure same secret in GitHub.
6. Keep startup doctor gate enabled in service templates (already wired by default).
7. Set `AGENTPR_CODEX_BIN` when `codex` is not in service PATH.
8. Keep webhook audit logging enabled and periodically run `webhook-audit-summary` for alert checks.

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

4. Webhook observability readiness:
```bash
python3.11 -m orchestrator.cli webhook-audit-summary --since-minutes 60 --max-lines 5000
```

Interpretation:

1. `doctor` failing on `net.*` means host/network/DNS problem, not workflow problem.
2. `doctor` failing on `gh.auth` means credentials problem (`gh auth login` / token scopes).
3. `run-preflight` failing on `git.write` means that repo `.git` is not writable for worker process.
4. Start manager/worker only when `doctor` and `run-preflight` are both `ok=true`.
5. If `doctor` fails only on `cmd.codex`, set `AGENTPR_CODEX_BIN` to the absolute codex binary path.
6. If webhook monitor uses thresholds, non-zero exit from `webhook-audit-summary` should trigger your external alert channel.
