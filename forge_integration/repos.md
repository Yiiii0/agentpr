# Forge Integration Tracking

## Baseline Batch (2026-02-24)

| Repo | Owner | Purpose | Engine |
|------|-------|---------|--------|
| mem0 | mem0ai | Non-interactive baseline run | codex exec |
| dexter | virattt | Non-interactive baseline run | codex exec |

## Baseline Results (2026-02-24)

| Repo | Run ID | Result | State | Agent Duration | Key Blocker |
|------|--------|--------|-------|----------------|-------------|
| mem0 | baseline_mem0_20260224_033111 | NEEDS REVIEW (classification=PASS) | NEEDS_HUMAN_REVIEW | see runtime report | full test suite has pre-existing optional dependency/config issues; focused forge/openai/deepseek tests pass |
| mem0 (minimal rerun) | baseline_mem0_min_20260224_154627 | NEEDS REVIEW (classification=PASS) | NEEDS_HUMAN_REVIEW | see runtime report | no push mode; diff reduced to 2 files / +41 lines |
| dexter | baseline_dexter_20260224_033111 | NEEDS REVIEW (classification=PASS) | NEEDS_HUMAN_REVIEW | see runtime report | one pre-existing test fails due local path `~/.dexter/gateway-debug.log` missing (unrelated to Forge routing) |

## Calibration Runs (2026-02-25)

| Repo | Run ID | Prompt | Result | State | Key Insight |
|------|--------|--------|--------|-------|-------------|
| mem0 | calib_mem0_20260225_r2 | calib-v2 + skills-mode | PASS | LOCAL_VALIDATING | Contract materialization fix worked (`.agentpr_runtime/contracts/*` readable); CI-style lint/test evidence present (9 test/lint commands). |
| dexter | calib_dexter_20260225_r2 | calib-v2 + validation short prompt | HUMAN_REVIEW (`reason_code=test_command_failed`) | NEEDS_HUMAN_REVIEW | `bun run typecheck` + `bun test` failed with pre-existing issues; new classifier correctly blocks false PASS. |

## Operational Notes

1. `run-preflight` now checks selected codex sandbox policy and blocks `read-only` mode for integration runs.
2. Current environment is now green on `doctor --require-codex` and repo-level preflight (`git.write` + network checks).
3. `finish.sh` commit-title single-line validation was fixed; commit/push now succeeds in rerun baseline.

## Insights (2026-02-24)

1. Agent can follow repo rules and produce minimal diffs; after environment gates are green, primary risk shifts to repo-specific test baselines and optional dependency combinations.
2. `doctor + preflight` should remain hard gates, but transient network flakiness requires retry-tolerant orchestration.
3. Workspace hygiene is critical: rerun前必须清理历史脏改动，否则 non-interactive worker 会主动中止。
4. Each baseline attempt should include runtime report artifact for auditable test/commit evidence.
5. Manager policy must not treat "tests executed" as equivalent to "tests passed"; classification now uses failed test command detection.

## To Do

| Repo | Owner | Lang | Priority | Notes |
|------|-------|------|----------|-------|
| | | | | |

## In Progress

| Repo | Owner | Lang | Scenario | Started | Notes |
|------|-------|------|----------|---------|-------|
| | | | | | |

## Needs Review

| Repo | Owner | Lang | Scenario | Pushed | Issue |
|------|-------|------|----------|--------|-------|
| vanna | vanna-ai | Python | C (new provider class) | 2026-02-10 | Missing tox target + test marker per CONTRIBUTING "Adding a New LLM" steps 2 & 3 — needs fix |
| gpt-researcher | assafelovic | Python | B (registry entry) | 2026-02-10 | Pre-existing test failures (import errors); had to re-base on master per CONTRIBUTING |
| quivr | QuivrHQ | Python | D (config enum + elif) | 2026-02-09 | 1 pre-existing test failure (not our change); rye tooling, ruff lint |

## Skipped

| Repo | Owner | Reason | Date |
|------|-------|--------|------|
| | | | |

## Completed

| Repo | Owner | Lang | Scenario | Files | Lines | Completed | Notes |
|------|-------|------|----------|-------|-------|-----------|-------|
| dexter | virattt | TypeScript/Bun | A (prefix routing) | 3 | +12 | 2026-01-29 | langchain wrappers, bun required |
| mem0 | mem0ai | Python | A (env var detection) | 3 | +30 | 2026-01-29 | hatch tooling, ruff format |
