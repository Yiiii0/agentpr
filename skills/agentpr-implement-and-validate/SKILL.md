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

5. Final self-check — **with evidence**.
- Run through `references/self_review_checklist.md` — every item must pass.
- Ensure diff stays within budget and only intentional files changed.
- Ensure no commit/push when manager policy disallows push.
- **You MUST produce a `self_review` evidence block** (see Output Format). For each critical check, include the actual command output or code snippet that proves it passes — not just "checked" or "OK".

## Output Format

Return a compact structured summary with:
- `status`: `PASS | NEEDS REVIEW | FAIL | SKIP`
- `files_changed`: explicit file list
- `validation`: command/results list (command + exit_code + summary)
- `self_review`: evidence for critical checklist items (see below)
- `notes`: blockers or follow-up actions

### self_review evidence (mandatory)

For these critical items, provide **verifiable evidence** (actual command output or code snippet). Do NOT write "checked" or "OK" — show the proof.

```
self_review:
  git_diff_files: <paste output of `git diff --name-only`>
  lock_files_clean: <"no lock files in diff" or "reverted: git checkout poetry.lock">
  reference_provider: <name and path of the most similar provider you copied>
  os_environ_check: <paste output of `grep -n "os.environ\[" <your_changed_files>` — should be empty, or explain why it's safe>
  base_url_value: <the actual default base_url string in your code>
  env_var_name: <the actual env var name used for API key>
  test_evidence: <command + exit_code + pass/fail count>
```

This evidence will be verified by the Manager's code review. Fabricated evidence will be caught and the run will be sent back for iteration.

## Hard Rules

- Never run global installs.
- Never bypass repo rules with ad-hoc command substitutions.
- Never hide failed checks.

## Resources

- Read `references/self_review_checklist.md` for the mandatory pre-completion checklist.
- Read `references/validation_requirements.md` for acceptance checks.
- Read `references/forge_rules.md` for hard rules, important rules, and common pitfalls.
- Read `references/example_mem0.diff` for a Python reference integration (env var detection pattern).
- Read `references/example_dexter.diff` for a TypeScript reference integration (prefix routing pattern).
