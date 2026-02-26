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
8. Skills-mode prompt envelope + task packet artifacts for worker execution (staged or autonomous), including deterministic governance scan coverage prioritized to repo root/`.github`/`docs`
9. Telegram dual-mode control plane (`/` command mode + NL routing `rules|hybrid|llm`, including `/create` run bootstrap)

## Core Modules

1. `models.py`: enums and event/run data contracts
2. `state_machine.py`: allowed transitions + transition validation
3. `db.py`: schema and low-level persistence
4. `service.py`: event application and state changes
5. `executor.py`: shell script execution wrapper
6. `preflight.py`: repo preflight checks + startup doctor checks
7. `cli.py`: operator-facing command interface
8. `skills.py`: codex skill installation/discovery + staged/autonomous skill plan + task packet builder
9. `runtime_analysis.py`: runtime verdicting, event-stream parsing, digest/insight rendering, PR DoD gate checks
10. Worker runtime isolation: local cache/data dirs under `<repo>/.agentpr_runtime`
11. Runtime env override file: `runtime_env_overrides.json` (toolchain extensibility without code changes)
12. Runtime verdict classification: `PASS` / `RETRYABLE` / `HUMAN_REVIEW` with retry-cap escalation and semantic no-test-infra override (`runtime_grading_mode`)
13. Manager policy file: `manager_policy.json` (sandbox/skills-mode/timeout/diff budget/retry cap/test-evidence/runtime-grading defaults + repo overrides)
14. PR gate commands: `request-open-pr` -> `approve-open-pr --confirm` (double confirmation + DoD gate), with repo PR-template-aware body composition, manager draft stub, and optional About Forge append
15. `github_sync.py`: gh PR payload -> check/review sync decisions
16. `telegram_bot.py`: Telegram long-poll dual-mode loop for manager actions
17. `github_webhook.py`: webhook signature verification + event ingestion server
18. Webhook delivery dedup ledger (`webhook_deliveries`) + cleanup command
19. Automatic startup doctor gate on mutable CLI commands (`--skip-doctor` override)
20. `manager_policy.py`: central defaults for agent/bot/webhook runtime behavior
21. Run analysis artifacts: `run_digest` (JSON) + `manager_insight` (Markdown) per agent attempt
22. Stage-level observability is persisted in `run_digest.stages` (step totals/attempt timeline/top step)
23. `manager_decision.py`: rule-based next-action decision for manager loop
24. `manager_loop.py`: manager automation runner (`manager-tick` / `run-manager-loop`)
25. `manager_llm.py`: OpenAI-compatible manager LLM function-calling client (`rules|llm|hybrid`)
26. `manager_tools.py`: manager tool primitives (`analyze_worker_output`, `get_global_stats`, `notify_user`)
27. `manager_agent.py`: tool-driven manager decision core (rules/llm/hybrid with deterministic fallback)

Operational commands added:

1. `sync-github`
2. `run-telegram-bot`
3. `run-github-webhook`
4. `cleanup-webhook-deliveries`
5. `doctor`
6. `skills-status`
7. `install-skills`
8. `skills-metrics`
9. `skills-feedback`
10. `inspect-run`
11. `run-bottlenecks`
12. `webhook-audit-summary`
13. `manager-tick`
14. `run-manager-loop`
15. `analyze-worker-output`
16. `get-global-stats`
17. `notify-user`
18. `simulate-bot-session`

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
15. Diff budget checks exclude runtime artifact paths (`.agentpr_runtime`, `.venv`, `node_modules`, test/lint caches).
16. Manager-facing diagnostics should be structured JSON first (timeline, bottlenecks, next actions), then optional LLM interpretation.
17. Telegram bot enforces command-tier permissions (`read`/`write`/`admin`) plus rate limits and JSONL audit logging.
18. GitHub webhook enforces payload-size guard and persists request outcomes for monitor-friendly summaries.
19. `run-agent-step` captures codex JSONL event stream (`--json`) and last agent message for black-box observability (including derived command durations from local stream timestamps).
20. Keep manager decisions grounded in deterministic artifacts (`run_digest`) and use LLM text summaries (`manager_insight`) only as advisory context.
21. Tune runtime thresholds by repo using `run_agent_step.repo_overrides` before changing prompt complexity.
22. Runtime grading follows final convergence semantics: intermediate failed test/typecheck commands are preserved as evidence, while converged runs can still classify as `PASS`; `runtime_grading_mode=hybrid` may semantically pass no-test-infra repos when alternative validation exists.
23. In skills-mode, materialize contract artifact under repo runtime path (`.agentpr_runtime/contracts`) for worker-readability.
24. PR creation is blocked unless latest `run_digest` satisfies DoD (PASS + runtime success reason in `{runtime_success, runtime_success_allowlisted_test_failures, runtime_success_recovered_test_failures, runtime_success_no_test_infra_with_validation}` + policy thresholds + contract evidence), unless explicitly bypassed.
25. Event stream persistence is tiered: always keep `run_digest`, keep raw `agent_event_stream` for non-pass runs and deterministic sampled pass runs.
26. Runtime verdict/report logic is centralized in `runtime_analysis.py` instead of `cli.py` to reduce coupling and behavior drift.
27. Safety contract allows explicit external read-only context roots while still forbidding out-of-repo writes.
28. `run-agent-step` supports hard timeout (`--max-agent-seconds`) to prevent silent long-running hangs.
29. In staged skills-mode, DISCOVERY success convergence defaults to `UNCHANGED`; autonomous mode can converge DISCOVERY/PLAN_READY -> `EXECUTING` (and legacy v1 runs via deterministic intermediate transitions to `LOCAL_VALIDATING`).
30. If `--max-agent-seconds` is omitted, timeout is resolved from manager policy (`run_agent_step.max_agent_seconds`).
31. Known baseline test failures can be allowlisted in policy (`known_test_failure_allowlist`) to avoid blocking on verified non-scope test noise.
32. `skills-feedback` converts skills runtime metrics to deterministic iteration actions for manager prompt/skill governance.
33. Manager LLM should consume orchestrator actions via API function-calling, while worker execution remains `codex exec`.
34. Telegram NL mode supports `rules|hybrid|llm`; `hybrid` is recommended for production-safe rollout.
35. `manager-tick`/`run-manager-loop` support `--decision-mode rules|llm|hybrid`; `llm/hybrid` needs manager API key env.
36. In `llm/hybrid`, if LLM chooses `wait_human` while rules have an executable next step, manager auto-overrides to rules to avoid stalling.
37. Dirty-workspace failures now emit compact diff summaries (counts + samples), avoiding terminal/log blowups from runtime cache file lists.
38. `manager-tick`/`run-manager-loop` auto-resolve worker prompt from `AGENTPR_WORKER_PROMPT_FILE` (fallback: `forge_integration/claude_code_prompt.md`) when `--prompt-file` is omitted.
39. Manager LLM facts include compact `run_digest` evidence (classification/validation/diff/attempt/recommendation), not only run state skeleton.
40. Telegram bot sends deduplicated proactive notifications for key run states and GitHub-feedback-triggered iterating transitions.
41. Telegram Decision Card supports dual-layer explanation: deterministic `why_machine` plus optional LLM `why_llm`/`suggested_actions_llm`.
42. `simulate-bot-session` reuses the same bot/NL routing handlers as Telegram loop for deterministic local flow rehearsal.
