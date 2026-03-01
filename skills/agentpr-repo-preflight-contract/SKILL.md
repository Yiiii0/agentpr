---
name: agentpr-repo-preflight-contract
description: Analyze a target repository before implementation and output a strict integration contract (contribution rules, CI commands, minimal-diff file plan, doc/test requirements, and blockers). Use when the run is in discovery or plan-ready stage, or whenever contract data is missing or stale.
---

# AgentPR Repo Preflight Contract

## Overview

Build a machine-checkable repo contract before any code edits. Favor objective repo evidence over assumptions.

## Inputs

- Repository workspace path.
- Integration objective (Forge/OpenAI-compatible integration).
- AgentPR task packet if present.

## Workflow

1. Read repository governance files first.
- Read `task_packet.repo.governance_scan` first when available, then open `AGENTS.md`, `CONTRIBUTING*`, `CODEOWNERS`, PR template files, README/dev/setup docs.
- If expected files are missing, run secondary search with `--hidden` or `find .github` fallback before concluding they do not exist.
- Extract branch base requirements, commit/PR message rules, and mandatory checklists.

2. Read CI as source of truth for test/lint/install.
- Inspect `.github/workflows/` and capture exact install/test/lint commands.
- Record required env vars and toolchain versions used by CI.

3. Locate integration surface and closest precedent.
- Find OpenAI/provider routing paths, model normalization, and provider registry points.
- Identify nearest existing provider implementation and reuse its pattern.

4. Decide minimal-diff plan.
- List exact files to touch (target <= 4 files unless repo constraints require more).
- Mark whether docs updates are required and where.

5. Produce contract output.
- Emit exactly one JSON object following `references/contract_schema.md`.
- Set `status=needs_review` when hard blockers exist.

## Hard Rules

- Do not edit source code in this skill.
- Do not skip CONTRIBUTING/PR template/CI workflow checks.
- Do not claim test commands without a concrete file/command source.
- Do not invent branch names or commit conventions.

## Resources

- Read `references/contract_schema.md` for required output schema.
- Read `references/source_scan.md` for deterministic scan order.
- Read `references/forge_scenarios.md` for Forge constants, quick-skip check, and integration scenarios A/B/C/D.
- Read `references/analysis_checklist.md` for the full analysis checklist (1.1–1.6) to complete before any code changes.
