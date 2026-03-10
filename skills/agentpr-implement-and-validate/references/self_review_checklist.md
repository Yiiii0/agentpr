# Pre-Completion Self-Review Checklist

Run through EVERY item before reporting status. If any item fails, fix it before proceeding.

## Files & Git
- [ ] `git diff --name-only` shows ONLY intentional files
- [ ] `git add` all NEW files you created (they won't be committed otherwise!)
- [ ] No lock files modified (uv.lock, package-lock.json, bun.lockb, yarn.lock, poetry.lock, pnpm-lock.yaml) — `git checkout` to revert if so
- [ ] No CI/workflow files modified unless explicitly required by contract

## Reference Provider Check
- [ ] Identified the **most similar** existing provider (aggregator → aggregator like OpenRouter, not random provider)
- [ ] Read ALL similar providers before deciding which to copy
- [ ] Used repo's extension mechanism (registry, factory, partial, config) if one exists — NOT standalone implementation

## Code Quality
- [ ] Variable/class/function naming matches the nearest existing provider pattern in this repo
- [ ] `base_url` default is `https://api.forge.tensorblock.co/v1` (not empty, not placeholder)
- [ ] Environment variable is `FORGE_API_KEY` (not OPENAI_API_KEY, not FORGE_KEY, not API_KEY)
- [ ] Model format `Provider/model-name` is documented or handled where relevant
- [ ] If subclassing or reusing another provider class, use proper inheritance (NOT unbound method calls like `OtherClass.method(self, ...)`)
- [ ] If similar providers define a default model, Forge should too (e.g. `openai/gpt-4o-mini`)
- [ ] No hardcoded strings that couple to specific providers where the code should be generic
- [ ] NOT mutating `os.environ` — pass API keys/base URLs via explicit kwargs, not environment variable assignment
- [ ] All variables are initialized before use in ALL branches (no unbound variable risk in conditional paths)
- [ ] Model detection uses `startswith("forge/")` prefix match, NOT `"/" in model_string`
- [ ] If the repo uses `Literal[...]` for model names, extended to `Union[Literal[...], str]` (not just appending to Literal)

## Downstream Impact Check
- [ ] Searched for ALL callers/users of modified functions, types, and variables
- [ ] If inserting into an `elif`/`if` dispatch chain: verified new branch does NOT shadow existing branches
- [ ] If modifying an engine/handler class: verified the factory/router that dispatches to it is also updated
- [ ] No parameter type or default value changes that break existing callers

## Docs & Contribution Rules
- [ ] If CONTRIBUTING/PR template says "update docs" → docs ARE updated (README provider list, config docs, etc.)
- [ ] If PR template has a checklist → ALL items are satisfied
- [ ] Doc additions match format/length/structure of the nearest similar provider's docs
- [ ] If the repo has a providers list in README → Forge is added in the same format
- [ ] If the repo has an env.example / .env.example → `FORGE_API_KEY` is added
- [ ] If the repo has a changelog convention (towncrier, CHANGELOG.md, etc.) → entry added

## Validation — CRITICAL: NEVER SKIP
- [ ] Install command ran (or noted specific blocker)
- [ ] **Even if install fails**: still attempt test/lint commands — they may work with partial deps or system packages
- [ ] Test command ran if test infrastructure exists — report actual exit code, not assumption
- [ ] Lint/format check ran — report actual exit code
- [ ] If no test runner found: try `pytest`, `ruff check`, `flake8`, or `pre-commit run --all-files` as fallback validation
- [ ] Do NOT give up on validation after a single failure — try alternative commands
- [ ] If tests fail, determine if failures are pre-existing vs caused by your changes
- [ ] **Zero validation commands = automatic NEEDS_REVIEW from grading system**. Always run something.
