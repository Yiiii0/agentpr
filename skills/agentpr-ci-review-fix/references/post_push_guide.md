# Post-Push Guide

## CI Failures

1. Read the CI log — identify which check failed and why.
2. Common causes:
   - **Coverage below threshold** → add tests for uncovered branches (including fallback/unknown paths).
   - **Doc test failure** → add `FORGE_API_KEY` to doc test fixtures (search for `test_examples`).
   - **Lint/type check** → fix and push.
3. Push fix commits (normal push, not force push — PR is already open).

## Reviewer Comments

1. Evaluate each comment for correctness.
2. Fix legitimate issues, push fix commits.
3. For debatable points: check if there's precedent in the codebase (e.g., "Fireworks also does this").

## What NOT to Do

- Don't force push after PR is open (destroys review context).
- Don't ignore CI failures — they must all pass for merge.
- Don't rewrite large parts of the implementation during CI fix stage.
