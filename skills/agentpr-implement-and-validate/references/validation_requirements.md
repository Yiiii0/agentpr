# Validation Requirements

Required evidence before PASS:

1. Exact commands run for install, lint, and tests.
2. Exit code/result for each command.
3. `git diff --name-only` output is intentional.
4. Diff is within manager budget unless justified.
5. Docs updated if required by contract/rules.
6. At least ONE validation command was attempted (test, lint, typecheck, or pre-commit).

## Validation Resilience

If `pip install` / `npm install` / `bun install` fails:
- **Do NOT stop validation.** The grading system requires at least 1 test/lint command.
- Try running test/lint commands anyway — they may work with system packages.
- Fallback order: `pytest` → `ruff check .` → `flake8` → `pre-commit run --all-files` → `python -m py_compile <changed_files>`
- For JS/TS: `bun test` → `npm test` → `npx tsc --noEmit`
- Report what you tried and what happened — a failed validation attempt is better than no attempt.

## Change Substantiveness

- Integration MUST include at least one **non-test source file** change (provider class, config, factory registration, or env example). Test-only changes are NOT a valid integration — the grading system will flag them as NEEDS_REVIEW.
- If the repo has no clear integration point (no provider registry, no multi-provider architecture), report SKIP rather than modifying only test files.

## Classification

- `PASS`: required checks passed, non-test source files changed, and no policy violations.
- `NEEDS REVIEW`: blocked by pre-existing env/repo issues or policy ambiguity.
- `FAIL`: change introduces reproducible failures.
- `SKIP`: repo architecture does not require/allow Forge integration path.

**WARNING**: Reporting PASS with zero validation commands will be overridden to NEEDS_REVIEW by the grading system.
