---
name: agentpr-ci-review-fix
description: Triage and fix CI failures or review feedback after push/PR with focused incremental patches, then revalidate and summarize. Use when the run is in iterating, ci-wait, or review-wait stage.
---

# AgentPR CI Review Fix

## Overview

Handle post-push failures incrementally: identify root cause, patch minimally, rerun targeted checks, and report whether run should retry or escalate.

## Workflow

1. Collect failure evidence.
- Read failing check/review context from task packet and local run artifacts.
- If `gh-fix-ci` is installed, use it for GitHub Actions failure triage.
- If `gh-address-comments` is installed, use it to enumerate actionable review comments.

2. Create a narrow fix plan.
- Bind each change to one failing signal or reviewer request.
- Prefer smallest reversible patch.

3. Implement and validate targeted fixes.
- Run only relevant commands first, then broader required checks if needed.
- Keep diff small and avoid unrelated cleanup.

4. Emit routing decision.
- `PASS`: checks now pass and no unresolved requested changes.
- `RETRYABLE`: transient infra/network issue.
- `HUMAN_REVIEW`: ambiguous failure, policy conflict, or missing evidence.

## Hard Rules

- Do not rewrite large parts of the implementation during CI fix stage.
- Do not force-push unless explicitly required by repo policy and approved.
- Do not mark success without command evidence.

## Resources

- Read `references/ci_triage_playbook.md` for triage order.
- Read `references/post_push_guide.md` for CI failure handling, reviewer comment handling, and what NOT to do.
