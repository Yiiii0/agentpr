You are running in /Users/yi/Documents/Career/TensorBlcok/agentpr/workspaces/dexter.

Goal: perform validation-only calibration for current branch state.

Execution requirements:
1. Read required instructions from task packet and current repo docs.
2. Do NOT edit tracked source files.
3. Do NOT run git commit/push and do NOT create PR.
4. Run validation commands expected by this repo:
   - bun run typecheck
   - bun test
5. If command fails, report exact failure command and error summary.

Final response format:
- Status: PASS / NEEDS REVIEW / FAIL / SKIP
- Files changed
- Test/lint results
- What was pushed (if any)
