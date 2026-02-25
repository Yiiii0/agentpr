# Validation Requirements

Required evidence before PASS:

1. Exact commands run for install, lint, and tests.
2. Exit code/result for each command.
3. `git diff --name-only` output is intentional.
4. Diff is within manager budget unless justified.
5. Docs updated if required by contract/rules.

Classify as:
- `PASS`: required checks passed and no policy violations.
- `NEEDS REVIEW`: blocked by pre-existing env/repo issues or policy ambiguity.
- `FAIL`: change introduces reproducible failures.
- `SKIP`: repo architecture does not require/allow Forge integration path.
