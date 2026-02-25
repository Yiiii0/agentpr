# Deployment Templates

This folder contains production-oriented process templates for manager components:

1. `systemd/agentpr-telegram.service`
2. `systemd/agentpr-github-webhook.service`
3. `systemd/agentpr-webhook-audit-alert.service`
4. `systemd/agentpr-webhook-audit-alert.timer`
5. `supervisord/agentpr-manager.conf`
6. `nginx/agentpr-webhook.conf`
7. `cloudflare/agentpr-webhook-tunnel.yml`
8. `scripts/webhook_probe.py`

Before use:

1. Replace all `CHANGE_ME` placeholders.
2. Replace `/ABSOLUTE/PATH/TO/TensorBlcok/agentpr` with your actual path.
3. Ensure `python3.11` path is correct on host.
4. For Telegram bot, configure `allow/write/admin` chat ids (or intentionally collapse them to one chat id).
5. For GitHub webhook, set `AGENTPR_GITHUB_WEBHOOK_SECRET` and configure same secret in GitHub.
6. Keep startup doctor gate enabled in service templates (already wired by default).
7. Set `AGENTPR_CODEX_BIN` when `codex` is not in service PATH.
8. Keep webhook audit logging enabled and periodically run `webhook-audit-summary` for alert checks.
9. Keep webhook path fixed as `/github/webhook` across GitHub, proxy, and AgentPR.

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

5. Webhook ingress guard readiness (signature/replay/payload-size):
```bash
python3.11 deploy/scripts/webhook_probe.py \
  --url http://127.0.0.1:8787/github/webhook \
  --secret "$AGENTPR_GITHUB_WEBHOOK_SECRET" \
  --max-payload-bytes 1048576
```

Interpretation:

1. `doctor` failing on `net.*` means host/network/DNS problem, not workflow problem.
2. `doctor` failing on `gh.auth` means credentials problem (`gh auth login` / token scopes).
3. `run-preflight` failing on `git.write` means that repo `.git` is not writable for worker process.
4. Start manager/worker only when `doctor` and `run-preflight` are both `ok=true`.
5. If `doctor` fails only on `cmd.codex`, set `AGENTPR_CODEX_BIN` to the absolute codex binary path.
6. If webhook monitor uses thresholds, non-zero exit from `webhook-audit-summary` should trigger your external alert channel.
7. `webhook_probe.py` must pass all three checks before exposing webhook publicly.

## External Alert Loop (systemd timer)

Use this for low-cost monitoring without LLM polling:

```bash
sudo cp deploy/systemd/agentpr-webhook-audit-alert.service /etc/systemd/system/
sudo cp deploy/systemd/agentpr-webhook-audit-alert.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentpr-webhook-audit-alert.timer
sudo systemctl status agentpr-webhook-audit-alert.timer
```

The alert check runs every 10 minutes and exits non-zero when thresholds are exceeded:

- retryable failures in window `> 0`
- http 5xx rate in window `> 5%`

Tune the service `ExecStart` thresholds to your SLO.

## Public Webhook Ingress

Two supported options:

1. Nginx reverse proxy (direct public endpoint)
2. Cloudflare Tunnel (no direct inbound port exposure)

### Option A: Nginx

1. Copy `deploy/nginx/agentpr-webhook.conf`.
2. Replace `CHANGE_ME_WEBHOOK_HOST`.
3. Ensure TLS cert paths are valid.
4. Route GitHub webhook URL to `https://<host>/github/webhook`.

### Option B: Cloudflare Tunnel

1. Copy `deploy/cloudflare/agentpr-webhook-tunnel.yml` to cloudflared config.
2. Replace tunnel name/id and credentials path.
3. Replace `CHANGE_ME_WEBHOOK_HOST`.
4. Point DNS to the tunnel host.
5. Use webhook URL `https://<host>/github/webhook`.

After either option, run `webhook_probe.py` again through the public URL to verify:

1. valid signed payload accepted
2. repeated `X-GitHub-Delivery` deduplicated
3. oversized payload rejected with `413`
