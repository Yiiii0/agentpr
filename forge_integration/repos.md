# Forge Integration Tracking

## Baseline Batch (2026-02-24)

| Repo | Owner | Purpose | Engine |
|------|-------|---------|--------|
| mem0 | mem0ai | Non-interactive baseline run | codex exec |
| dexter | virattt | Non-interactive baseline run | codex exec |

## Baseline Results (2026-02-24)

| Repo | Run ID | Result | State | Agent Duration | Key Blocker |
|------|--------|--------|-------|----------------|-------------|
| mem0 | baseline_mem0_20260224 | NEEDS REVIEW | NEEDS_HUMAN_REVIEW | 6m20s (attempt 2) | commit/push blocked in current sandbox (`.git/index.lock` permission) |
| dexter | baseline_dexter_20260224 | NEEDS REVIEW | NEEDS_HUMAN_REVIEW | 4m59s | dependency install/typecheck blocked by environment/network; commit/push blocked by `.git` permission |

## Operational Notes

1. `run-preflight` now checks selected codex sandbox policy and blocks `read-only` mode for integration runs.
2. Current environment still blocks dependency download (PyPI/npm), so required repo tests cannot be fully validated.
3. `finish.sh` commit-title single-line validation was fixed; remaining push blocker is `.git` write permission.

## Insights (2026-02-24)

1. Agent can follow repo rules and produce minimal diffs, but execution environment determines whether validation/commit can complete.
2. Network and `.git` writability are first-order gates; prompt quality is second-order once those fail.
3. Preflight should be treated as hard gate, not optional diagnostics, for non-interactive manager-driven runs.
4. Each future baseline attempt should include the new agent runtime report artifact for auditable command/test evidence.

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
