# Forge Integration Rules

## Hard Rules (violating = PR rejected)

1. **Read CONTRIBUTING + CI workflow FIRST** — This determines branch, toolchain, test commands, code style, and integration-specific steps.
2. **Execute ALL CONTRIBUTING steps** — If it says "Adding a New LLM: 1. code 2. tests 3. tox target", every step is mandatory. No partial compliance.
3. **CONTRIBUTING branch > default branch** — If CONTRIBUTING says "branch from master" but HEAD is main, use master.
4. **Use project's own toolchain** — rye/hatch/poetry/tox/bun, never raw `pip install -e .` unless project docs explicitly say to.
5. **`git diff --name-only` before every commit** — Lock files, CI config, unrelated files = must revert.
6. **No cross-repo mentions** — Don't reference other repos in commits, code comments, or docs.
7. **Follow existing patterns exactly** — Copy the closest provider implementation. Use the common routing path unless technically impossible.
8. **PR template checklist is a hard gate** — Every item must be satisfied BEFORE first push. Commit message format must match repo conventions.

## Important Rules (violating = wasted time)

9. **Minimal changes** — Target ≤ 4 files, prefer editing over creating new files.
10. **Format only your files** — Never run formatter on entire project.
11. **Match doc style** — Same length/format/structure as the most similar provider's docs.
12. **Stop early on env issues** — 1-2 attempts max, then mark NEEDS REVIEW and move on.
13. **Keep commit scope clean** — Never mix workflow file edits into target repo commits.

## Common Pitfalls

These are distilled from 8+ repo integrations. Each one cost significant debugging time.

- **"Copy the exception, not the rule"** — When a project has both a common path (litellm routing) and exceptions (dedicated functions for specific providers), ask WHY. If the common path supports Forge (it usually does), use the common path.
- **Dependencies in unexpected places** — Some projects have deps in `requirements.txt` but not `pyproject.toml` (e.g., podcastfy + playwright). If imports fail after install, check `requirements.txt`.
- **Doc code blocks executed as tests** — Some projects run docs code blocks as tests. Missing env vars cause CI failure. Search for `test_examples` or `test_docs` and add `FORGE_API_KEY` to their fixtures.
- **Coverage requirements** — Some CIs enforce 100% coverage. Every branch in new code must be tested, including fallback/unknown paths.
- **Aggregator `model_profile` needs delegation** — Router providers (like Forge, OpenRouter) must parse `Provider/model-name` and delegate to provider-specific profile functions, not return a generic profile.
- **Type alias registration** — Only register capabilities you've verified (e.g., Chat Completions support ≠ Responses API support).
- **False SKIP from surface-level analysis** — "Forge uses OpenAI SDK, already instrumented" is wrong if the project has a provider registry with entries for OpenRouter/Azure/etc. Always search for how similar routers are integrated before deciding SKIP.
