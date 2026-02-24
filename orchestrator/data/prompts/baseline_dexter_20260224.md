You are running in /Users/yi/Documents/Career/TensorBlcok/agentpr/workspaces/dexter.

Goal: run a full non-interactive Forge integration baseline for dexter with minimal, high-quality changes.

You must follow these local instructions in order before coding:
1) /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/workflow.md
2) /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/prompt_template.md
3) /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/examples/mem0.diff
4) /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/examples/dexter.diff
5) /Users/yi/Documents/Career/TensorBlcok/forge/README.md

Execution requirements:
- Do NOT run prepare.sh (repo is already prepared).
- Determine correct base branch and contribution rules from this repo.
- Implement Forge integration with minimal diff and follow existing project patterns.
- Run required tests/lint exactly as this repo expects.
- If status is PASS or NEEDS REVIEW, commit and push using:
  bash /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/scripts/finish.sh "integrate forge provider" "dexter" "feat(dexter): add forge model routing"
- Do not create PR.
- If blocked by pre-existing issues, still provide a clear final summary.

Final response format:
- Status: PASS / NEEDS REVIEW / FAIL / SKIP
- Files changed
- Test/lint results
- What was pushed (if any)
