# agentpr

Lightweight orchestrator for Forge OSS integration runs.

## Quick Start

```bash
cd /Users/yi/Documents/Career/TensorBlcok/agentpr
python3.11 -m orchestrator.cli init-db
python3.11 -m orchestrator.cli doctor --require-codex
python3.11 -m orchestrator.cli install-skills --install-curated-ci
python3.11 -m orchestrator.cli skills-status
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
# skills-mode (worker invokes stage skills, manager injects task packet):
python3.11 -m orchestrator.cli run-agent-step --run-id <run_id> --prompt-file <prompt.md> --skills-mode agentpr --codex-sandbox danger-full-access --success-state NEEDS_HUMAN_REVIEW
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
# emergency only: add --allow-dod-bypass
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
# override defaults when needed:
# python3.11 -m orchestrator.cli run-github-webhook --max-payload-bytes 2097152 --audit-log-file orchestrator/data/reports/github_webhook_audit.jsonl
# local dev only:
# python3.11 -m orchestrator.cli run-github-webhook --allow-unsigned
```

Run Telegram control bot:

```bash
export AGENTPR_TELEGRAM_BOT_TOKEN=...
python3.11 -m orchestrator.cli run-telegram-bot --allow-chat-id <chat_id>
# optional command-tier ACL:
# python3.11 -m orchestrator.cli run-telegram-bot --allow-chat-id <read_chat> --write-chat-id <write_chat> --admin-chat-id <admin_chat>
# local dev only:
# python3.11 -m orchestrator.cli run-telegram-bot --allow-any-chat
```

Cleanup old webhook dedup records:

```bash
python3.11 -m orchestrator.cli cleanup-webhook-deliveries --keep-days 30
```

Summarize webhook audit health (manager/monitoring):

```bash
python3.11 -m orchestrator.cli webhook-audit-summary --since-minutes 60 --max-lines 5000
# fail fast for monitors:
# python3.11 -m orchestrator.cli webhook-audit-summary --fail-on-retryable-failures 0 --fail-on-http5xx-rate 5
```

Summarize skills quality metrics:

```bash
python3.11 -m orchestrator.cli skills-metrics --limit 200
# scope to one run:
python3.11 -m orchestrator.cli skills-metrics --run-id <run_id>
```

Build manager iteration feedback from metrics:

```bash
python3.11 -m orchestrator.cli skills-feedback --limit 300
# scope to one run:
python3.11 -m orchestrator.cli skills-feedback --run-id <run_id> --limit 200
```

Manager-facing run diagnostics:

```bash
python3.11 -m orchestrator.cli inspect-run --run-id <run_id>
python3.11 -m orchestrator.cli inspect-run --run-id <run_id> --include-log-tails
python3.11 -m orchestrator.cli run-bottlenecks --limit 20
```

Rule-based manager automation (Phase B1):

```bash
# single orchestration tick
python3.11 -m orchestrator.cli manager-tick \
  --prompt-file orchestrator/data/prompts/baseline_mem0_20260224.md \
  --skills-mode agentpr

# continuous manager loop (5 min interval)
python3.11 -m orchestrator.cli run-manager-loop \
  --prompt-file orchestrator/data/prompts/baseline_mem0_20260224.md \
  --skills-mode agentpr \
  --interval-sec 300
```

`inspect-run` now exposes agent black-box internals from codex JSONL events:
- `latest_agent_runtime.agent_event_summary.event_type_counts`
- `latest_agent_runtime.agent_event_summary.command_events_sample`
- `latest_agent_runtime.agent_event_summary.top_commands_by_duration` (derived from local stream timestamps)
- `latest_agent_runtime.event_stream_path` (raw JSONL)
- `latest_agent_runtime.last_message_path` / `last_message_preview`
- `latest_run_digest` (structured deterministic run summary JSON)
- `latest_manager_insight` (manager-facing markdown insight generated from run digest)

Telegram commands:
- `/list [N]`
- `/show <run_id>`
- `/status <run_id>`
- `/pending_pr [N]`
- `/approve_pr <run_id> <confirm_token>`
- `/pause <run_id>`
- `/resume <run_id> <target_state>`
- `/retry <run_id> <target_state>`

Telegram command tiers:
- `read`: `/start` `/help` `/list` `/show` `/status` `/pending_pr`
- `write`: `/pause` `/resume` `/retry`
- `admin`: `/approve_pr`

Manager interaction mode (current vs target):
- Current: Telegram is command-first (deterministic control actions).
- Target: command + natural-language dual mode.
- Planned architecture: Telegram -> manager LLM (API function-calling) -> orchestrator actions -> worker (`codex exec`).
- Manager does not directly run arbitrary shell; it calls whitelisted orchestration actions.

Notes:
- mutable commands now run a startup doctor gate by default (workspace write/tooling/auth/network profile checks).
- run `python3.11 -m orchestrator.cli doctor` for detailed readiness checks before manager loop start.
- use global `--skip-doctor` only for local debugging or controlled recovery workflows.
- `run-agent-step` runs preflight by default. Use `--skip-preflight` only for debugging.
- `run-agent-step` now runs codex with `--json` and captures event stream + final message for inspectability.
- `run-agent-step` now also emits `run_digest` + `manager_insight` artifacts for every attempt.
- `run-agent-step` now keeps raw `agent_event_stream` for non-pass runs and deterministic sampled pass runs (digest is always kept).
- Manager policy: use `run_digest` as machine-checkable truth; treat `manager_insight` as decision support, not as source of truth.
- `run-agent-step --skills-mode agentpr` means worker uses stage-specific `$agentpr-*` skills; manager only injects task packet and policy.
- In `--skills-mode agentpr`, contract artifacts are now materialized inside repo runtime path (`.agentpr_runtime/contracts/*`) so worker skills can read them without cross-repo path access.
- install/check skill readiness with `install-skills` / `skills-status`.
- `inspect-run` is the primary manager artifact for per-run timing/step/event/runtime breakdown.
- `run-bottlenecks` aggregates durations across recent runs to find slow phases before prompt tuning.
- Use `run-preflight --skip-network-check` when you intentionally run in offline mode.
- `run-preflight` now validates the selected sandbox policy (`--codex-sandbox`).
- `approve-open-pr` requires both `--confirm-token` and `--confirm`.
- `approve-open-pr` now enforces a DoD gate using latest `run_digest` + manager policy thresholds + contract artifact.
- use `--allow-dod-bypass` only for manual emergency override.
- `approve-open-pr` needs authenticated GitHub CLI in the repo context (`gh auth status` should pass).
- `sync-github` needs authenticated GitHub CLI with repo read permissions.
- `run-telegram-bot` requires `--allow-chat-id` unless explicitly using `--allow-any-chat` (development only).
- `run-telegram-bot` supports per-command ACL (`--write-chat-id`, `--admin-chat-id`), rate limiting, and JSONL audit logs.
- `run-github-webhook` should use a secret (`AGENTPR_GITHUB_WEBHOOK_SECRET`) in production.
- `run-github-webhook` now deduplicates deliveries by `X-GitHub-Delivery` (replay-safe).
- `run-github-webhook` enforces max payload size and writes JSONL audit outcomes for observability.
- webhook processing failures return `500` and release dedup lock so GitHub retries can re-process.
- `webhook-audit-summary` can be used by cron/systemd timers to emit non-zero exit code on alert thresholds.
- Deployment templates are in `agentpr/deploy/systemd/` and `agentpr/deploy/supervisord/`.
- Public ingress templates are in `agentpr/deploy/nginx/` and `agentpr/deploy/cloudflare/`.
- Webhook ingress probe is `agentpr/deploy/scripts/webhook_probe.py`.
- Deployment templates already include startup doctor gate (`ExecStartPre` / `doctor && process`) for Telegram/webhook manager processes.
- To confirm "real readiness" before automation:
  - global: `python3.11 -m orchestrator.cli doctor --require-codex`
  - repo-level: `python3.11 -m orchestrator.cli run-preflight --run-id <run_id> --codex-sandbox danger-full-access`
- If `doctor` only fails on `cmd.codex`, set `AGENTPR_CODEX_BIN` to absolute codex path.
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
- `--max-agent-seconds`
  - Hard timeout for one `run-agent-step` codex execution.
  - If omitted, uses manager policy default (`run_agent_step.max_agent_seconds`, currently `900`).
  - Set `0` to disable timeout.
- `--allow-agent-push`
  - Allow worker to run commit/push directly during `run-agent-step`.
  - Default is disabled; manager should run commit/push gate separately.
- `--allow-read-path`
  - Add external read-only context path for worker (repeatable).
  - Useful when manager prompt/task packet references files outside target repo.
- `--max-changed-files`
  - Diff budget upper bound for changed files.
  - If omitted, uses manager policy default (`run_agent_step.max_changed_files`).
- `--max-added-lines`
  - Diff budget upper bound for added lines.
  - If omitted, uses manager policy default (`run_agent_step.max_added_lines`).
- `--allow-dirty-worktree`
  - Allow agent execution with pre-existing workspace changes.
  - Default blocks dirty worktree in `DISCOVERY/IMPLEMENTING`.
- `--skills-mode`
  - `off`: legacy single-prompt mode.
  - `agentpr`: wrap prompt with task packet and stage skill chain (`$agentpr-*`).
- `--allow-missing-skills`
  - Continue even if required stage skill is not installed.
  - Default is strict fail-to-review.
- `--task-packet-file`
  - Merge operator-provided JSON/Markdown into generated task packet.
- `--max-retryable-attempts`
  - Retry cap for retryable failures; when exceeded, verdict upgrades to `HUMAN_REVIEW`.
  - If omitted, uses manager policy default.
- `--min-test-commands`
  - Minimum number of test/lint evidence commands required in implementation states for a success verdict.
  - If omitted, uses manager policy default (can be overridden per repo in manager policy).

Runtime mapping:

- Default command emitted by `run-agent-step`:
  - `codex exec --sandbox danger-full-access "<prompt>"`
- If sandbox is `workspace-write` and full-auto behavior is enabled:
  - `codex exec --sandbox workspace-write --full-auto "<prompt>"`
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
   - Worker is explicitly instructed to forbid out-of-repo writes and global installs
   - External paths can be read only when explicitly allowlisted (integration/forge/skills roots + optional `--allow-read-path`)
   - `sudo` is explicitly disallowed
3. Workspace boundary check:
   - preflight fails if run workspace is outside configured `--workspace-root`
4. Extensible runtime policy:
   - `orchestrator/runtime_env_overrides.json` controls environment overrides without code changes
   - use placeholders: `{repo_dir}`, `{runtime_dir}`, `{cache_dir}`, `{data_dir}`, `{tmp_dir}`

Manager/worker boundary:

- Worker owns code execution, environment setup, and runtime evidence generation.
- Manager owns prompt/skill/policy evolution and approval gates.
- Worker should not rewrite manager prompt assets (`forge_integration/*`) during run; it can consume them as read-only context.

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

Manager policy defaults:

- Global `--policy-file` (default `orchestrator/manager_policy.json`) controls defaults for:
  - `run_agent_step`: sandbox / skills mode / timeout / diff budgets / retry cap / test evidence threshold / known baseline-test allowlist / verdict convergence targets / repo-level overrides
  - `telegram_bot`: polling defaults / list limit / rate limits / audit log path
  - `github_webhook`: payload limit / audit log path
- CLI flags still override policy values per command.

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
    "known_test_failure_allowlist": [],
    "success_event_stream_sample_pct": 15,
    "success_state": "LOCAL_VALIDATING",
    "on_retryable_state": "FAILED_RETRYABLE",
    "on_human_review_state": "NEEDS_HUMAN_REVIEW",
    "repo_overrides": {
      "mem0ai/mem0": {
        "skills_mode": "agentpr",
        "max_agent_seconds": 780,
        "max_changed_files": 3,
        "max_added_lines": 45,
        "max_retryable_attempts": 2,
        "min_test_commands": 2,
        "success_event_stream_sample_pct": 8
      },
      "virattt/dexter": {
        "skills_mode": "agentpr",
        "max_agent_seconds": 780,
        "max_changed_files": 4,
        "max_added_lines": 70,
        "max_retryable_attempts": 2,
        "min_test_commands": 2,
        "known_test_failure_allowlist": [
          "\\.dexter/gateway-debug\\.log",
          "No such file or directory.*gateway-debug\\.log"
        ],
        "success_event_stream_sample_pct": 8
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

## Skills Chain

`run-agent-step --skills-mode agentpr` uses this split:

1. `DISCOVERY/PLAN_READY` -> `$agentpr-repo-preflight-contract`
2. `IMPLEMENTING/LOCAL_VALIDATING` -> `$agentpr-implement-and-validate`
3. `ITERATING/CI_WAIT/REVIEW_WAIT` -> `$agentpr-ci-review-fix`

Manager vs worker boundary:

1. Manager injects stage skill plan + task packet + policy.
2. Worker (`codex exec`) executes the skill logic and code changes.

Skill locations and install flow:

1. Source-of-truth skills are versioned in `agentpr/skills/`.
2. Worker-visible skills are installed to `~/.codex/skills` via:
   `python3.11 -m orchestrator.cli install-skills --install-curated-ci`
3. Check readiness with:
   `python3.11 -m orchestrator.cli skills-status`

Reference docs:

1. https://developers.openai.com/codex/advanced#tools-skills
2. https://developers.openai.com/codex/cli/#custom-skills
3. https://github.com/openai/skills/tree/main/skills/.curated

Note:
- Current local `codex` CLI build does not expose `codex create skill`; AgentPR uses the official `skill-creator` scripts (`init_skill.py`, `quick_validate.py`) to scaffold and validate skills.

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

Task packet artifacts are saved to:

- `agentpr/orchestrator/data/task_packets/<run_id>_task_packet_<timestamp>.json`

Classification behavior:

1. `PASS`
   - exit code is 0, no safety violations
   - and (for implementation/validation states) test command evidence meets `min_test_commands`
2. `RETRYABLE`
   - transient/runtime failures (network/timeout/rate-limit-like signals)
3. `HUMAN_REVIEW`
   - safety violations, hard permission/auth/tooling failures, or missing test evidence
   - test/lint/typecheck commands executed but failed (`reason_code=test_command_failed`)
4. `PASS` (allowlisted baseline test failure)
   - when failed test commands are detected but stdout/stderr matches
     `known_test_failure_allowlist`
   - reason code becomes `runtime_success_allowlisted_test_failures`

`run-agent-step` state behavior with classification:

1. non-zero exit + `HUMAN_REVIEW` -> converges to `--on-human-review-state` (or policy default)
2. non-zero exit + `RETRYABLE` -> converges to `--on-retryable-state` (or policy default)
3. zero exit + non-`PASS` -> command returns non-zero and converges by the same configurable verdict mapping
4. zero exit + `PASS` -> applies `--success-state` (or policy default)

## Current Status (2026-02-25)

1. Baseline runs (`mem0`, `dexter`) confirm codex can read rules/docs and produce minimal code changes.
2. Environment gates are now green in real host execution (`doctor --require-codex` + repo preflight pass).
3. Commit/push succeeded in rerun baselines; no `.git` permission blocker in current host mode.
4. `finish.sh` commit title validation bug was fixed (single-line check + empty-title check).
5. MVP default sandbox is now `danger-full-access` with runtime guardrails and workspace-boundary preflight checks.
6. Structured runtime report is now generated for each agent attempt, with automatic verdict classification and artifact metadata (`grade/reason_code/next_action`).
7. Phase B PR gate MVP is implemented: `request-open-pr` + `approve-open-pr --confirm` (double confirmation).
8. Phase B manager loop is now available: `sync-github` for GitHub state sync and `run-telegram-bot` for remote control commands.
9. Phase B webhook ingress is available: `run-github-webhook` validates signatures and maps GitHub events to state-machine updates.
10. Webhook replay hardening is enabled via delivery-id dedup records and cleanup command.
11. Telegram bot now defaults to allowlist-only mode; deployment templates are included under `deploy/`.
12. Startup doctor + automatic gate is now implemented to fail fast on environment/auth/network prerequisites.
13. Codex binary resolution now supports `AGENTPR_CODEX_BIN` and Cursor extension fallback for daemon/PATH stability.
14. Runtime guardrails now include dirty-worktree blocking, no-push default, and diff-budget classification for `run-agent-step`.
15. Skills chain is now integrated in `run-agent-step` (`--skills-mode agentpr`), with task packet artifact generation and stage-based required skill checks.
16. Local AgentPR skills are now versioned under `agentpr/skills/` and installable via `install-skills`; curated CI helpers (`gh-fix-ci`, `gh-address-comments`) can be installed in the same command.
17. `install-skills --install-curated-ci` is now idempotent for already-installed curated skills.
18. Retry-cap policy is now configurable and enforced in runtime classification (`retryable_limit_exceeded`).
19. `skills-metrics` command now provides per-skill aggregates (`per_skill`, `missing_required_counts`) for manager-side tuning.
20. Telegram bot production hardening is implemented (ACL + rate limits + JSONL audit).
21. GitHub webhook production hardening is implemented (payload-size guard + JSONL audit + `webhook-audit-summary`).
22. `run_digest` now includes stage-level observability (`stages.step_totals`, `stages.attempts_recent`, `stages.top_step`) and command-category shares.
23. Manager policy now supports repo-specific runtime thresholds (`run_agent_step.repo_overrides`), including `min_test_commands`.
24. Skills-mode now materializes contract artifacts into repo runtime path (`.agentpr_runtime/contracts/*`) to avoid cross-root path blockers.
25. Runtime classification now blocks false PASS when test/typecheck commands fail (`reason_code=test_command_failed`).
26. Startup doctor workspace-write probe now uses process-unique filenames to avoid parallel probe collisions.
27. `approve-open-pr` now includes DoD gate checks (digest pass + policy thresholds + contract evidence) with explicit emergency bypass.
28. Runtime analysis code was split from `cli.py` into `orchestrator/runtime_analysis.py` to reduce coupling and drift risk.
29. Manager policy now supports repo-level `skills_mode` override and event-stream sampling (`success_event_stream_sample_pct`).
30. Safety contract now supports explicit external read-only context allowlist while still forbidding out-of-repo writes.
31. `run-agent-step` timeout is now policy-driven (`run_agent_step.max_agent_seconds`) and supports repo-level overrides.
32. `mem0` and `dexter` repo overrides were tightened for minimal-diff behavior (diff budget + retry cap + timeout).
33. Deployment templates now include webhook audit alert timer (`deploy/systemd/agentpr-webhook-audit-alert.*`).
34. Public ingress templates and guard probe are added (`deploy/nginx/agentpr-webhook.conf`, `deploy/cloudflare/agentpr-webhook-tunnel.yml`, `deploy/scripts/webhook_probe.py`).
35. Legacy fresh-baseline branches were cleaned up (local + remote): `feature/forge-20260224-172250`, `feature/forge-20260224-173213`.
36. Policy-level known baseline test failure allowlist is now supported globally and per-repo (`known_test_failure_allowlist`).
37. `skills-feedback` is now available to generate deterministic prompt/skill/policy iteration actions from runtime metrics.

## Insights (Conversation)

1. Primary bottleneck shifted from environment access to repo-specific test baseline quality and workspace hygiene.
2. Manager should own final push/PR gate decisions; worker should prioritize patch/report quality.
3. Non-interactive baseline must be measured first before expanding control-plane complexity.
4. "Skills" should be treated as contracts/boundaries, not necessarily separate CLI invocations.
5. Manager does not execute skill logic itself; worker executes skills, manager enforces stage/policy and artifact tracking.
6. `danger-full-access` is acceptable only with explicit guardrails and preferably container/VM isolation.
7. Unknown future toolchains should be handled by extending `orchestrator/runtime_env_overrides.json` first, then code only if needed.
