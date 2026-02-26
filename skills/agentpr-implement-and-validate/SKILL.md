---
name: agentpr-implement-and-validate
description: Implement the approved repo contract with minimal diff and run required local validation exactly as repository docs/CI require. Use when the run is in implementing or local-validating stage.
---

# AgentPR Implement And Validate

## Overview

Execute contract-driven code changes with strict minimal-diff discipline, then run required tests/lint and return evidence.

## Required Inputs

- Valid repo contract from discovery stage.
- Current AgentPR task packet.
- Current push policy (`allow_agent_push`) and diff budget.

## Workflow

1. Reconfirm constraints.
- Recheck contract fields: target files, branch rules, required checks, docs requirements.
- Reuse `task_packet.repo.governance_scan` evidence first (CONTRIBUTING/PR template/CI/README paths) and only run secondary search when coverage is insufficient.
- Stop with `NEEDS REVIEW` if contract is missing/ambiguous.

2. Set up environment exactly as CI/docs require.
- Follow toolchain priority and install commands from repo evidence.
- Keep all artifacts local to repository runtime directories.

3. Implement minimal patch.
- Touch only contract-listed files unless a hard dependency requires one more file.
- Keep routing/model-handling changes aligned with nearest in-repo provider pattern.
- Update docs when contract says required.

4. Validate.
- Run required lint/test/typecheck commands from contract.
- Capture command + outcome clearly.

5. Final self-check.
- Ensure diff stays within budget and only intentional files changed.
- Ensure no commit/push when manager policy disallows push.

## Output Format

Return a compact structured summary with:
- `status`: `PASS | NEEDS REVIEW | FAIL | SKIP`
- `files_changed`: explicit file list
- `validation`: command/results list
- `notes`: blockers or follow-up actions

## Hard Rules

- Never run global installs.
- Never bypass repo rules with ad-hoc command substitutions.
- Never hide failed checks.

## Resources

- Read `references/validation_requirements.md` for acceptance checks.
