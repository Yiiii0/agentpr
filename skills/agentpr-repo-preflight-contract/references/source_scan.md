# Source Scan Order

1. Governance deterministic pass: `AGENTS.md`, `CONTRIBUTING*`, PR templates, `CODEOWNERS`, `CODE_OF_CONDUCT*`, README/dev/setup docs.
2. CI truth: `.github/workflows/*.yml|yaml` (install/test/lint/env).
3. Env docs: setup/development/testing sections and docs.
4. Integration surface: provider registry, OpenAI path, model normalization.
5. Existing provider precedent for minimal-diff implementation pattern.

Secondary pass (when deterministic pass is incomplete):
- Use `rg --files --hidden` so `.github/` files are not skipped.
- Fallback to `find .github ... pull_request_template ...` for path variant coverage.
- Search README/docs section headings for Contributing/Development/Testing keywords.
