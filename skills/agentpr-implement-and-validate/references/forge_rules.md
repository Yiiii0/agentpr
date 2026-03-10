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

## Reference Provider Selection (CRITICAL)

Before writing ANY code, find the **most similar** existing provider to use as reference:

- Forge is an aggregator/router (like OpenRouter, LiteLLM). Find OTHER aggregators in the repo first.
- Do NOT pick a random provider in the same directory. Compare architectures:
  - Aggregator → reference another aggregator (OpenRouter, LiteLLM, AiHubMix)
  - If no aggregator exists → reference the provider with the closest base class or pattern
- **Read ALL similar providers** before deciding which to copy. Do not stop after reading one.
- Once you identify the reference, copy it **exactly** — same parameter order, same types, same defaults.

## Repo Extension Mechanisms (prefer over standalone code)

Before writing new classes or functions, check if the repo provides:
- **Registry/factory pattern**: dict mapping, `@register` decorators, factory functions
- **Config-driven**: YAML/JSON/env-based provider registration
- **Subclass with base class**: `OpenAICompatibleAPI`, `BaseLLM`, etc.
- **`functools.partial`**: parameterized wrappers

If the repo has one of these → use it. Filling in a registry entry is safer than writing standalone code.
If you MUST write standalone code → line-by-line compare with the reference provider. Add NOTHING the reference doesn't have.

## Common Pitfalls

These are distilled from 17 repo integrations. Each one cost significant debugging time.

- **"Copy the exception, not the rule"** — When a project has both a common path (litellm routing) and exceptions (dedicated functions for specific providers), ask WHY. If the common path supports Forge (it usually does), use the common path.
- **Dependencies in unexpected places** — Some projects have deps in `requirements.txt` but not `pyproject.toml` (e.g., podcastfy + playwright). If imports fail after install, check `requirements.txt`.
- **Doc code blocks executed as tests** — Some projects run docs code blocks as tests. Missing env vars cause CI failure. Search for `test_examples` or `test_docs` and add `FORGE_API_KEY` to their fixtures.
- **Coverage requirements** — Some CIs enforce 100% coverage. Every branch in new code must be tested, including fallback/unknown paths.
- **Aggregator `model_profile` needs delegation** — Router providers (like Forge, OpenRouter) must parse `Provider/model-name` and delegate to provider-specific profile functions, not return a generic profile.
- **Type alias registration** — Only register capabilities you've verified (e.g., Chat Completions support ≠ Responses API support).
- **False SKIP from surface-level analysis** — "Forge uses OpenAI SDK, already instrumented" is wrong if the project has a provider registry with entries for OpenRouter/Azure/etc. Always search for how similar routers are integrated before deciding SKIP.
- **Giving up validation after install failure** — `pip install -r requirements.txt` failing does NOT mean you can skip tests. Try `pytest` directly, try `ruff check`, try `pre-commit`. The grading system requires evidence of attempted validation. Zero commands = automatic NEEDS_REVIEW.
- **Missing env.example entries** — If a repo has `env.example` or `.env.example` with API keys for each provider, you MUST add `FORGE_API_KEY=your-api-key-here`. Forgetting this is a documentation gap.
- **Unbound method calls** — Never call `OtherClass.method(self, args)` as a shortcut. Either properly subclass the provider or define the method locally. Fragile method binding is a code quality issue.
- **Missing default model** — If every existing provider in the repo defines a default model, Forge should too. Use `openai/gpt-4o-mini` as the safe default.
- **NEVER mutate `os.environ`** — Do not use `os.environ["OPENAI_API_KEY"] = forge_key` or `os.environ["OPENAI_API_BASE"] = forge_base`. This pollutes global process state and breaks other providers. Instead, pass credentials via explicit constructor kwargs (`api_key=`, `base_url=`) or module-level variables. If a framework requires env vars, use a context manager or subprocess.
- **Model detection must use prefix match** — To check if a model is a Forge model, use `model.startswith("forge/")` or equivalent prefix match. NEVER use `"/" in model` — this matches ANY model string with a slash (e.g. `org/model`, `azure/gpt-4`), not just Forge models.
- **Initialize all variables before conditional branches** — If a variable (e.g. `extra_headers`) is only set inside an `if` block but used after the block, it will be unbound when the condition is false. Always set a default value before the conditional.
- **Extend `Literal` types to accept arbitrary strings** — If the repo defines model names as `Literal["gpt-4", "claude-3"]`, adding `"forge/xxx"` to the Literal is fragile. Use `Union[Literal[...], str]` to allow any model string while preserving existing type hints.
- **elif chain mutual exclusivity** — When inserting a new `elif` branch into an existing dispatch chain, analyze ALL existing branches. Your new branch may shadow downstream branches. Example: `elif "/" in model_name` placed before `elif model_info` will intercept ALL models with `/` in their name, blocking 66% of known provider models. Always insert new branches AFTER more specific checks, not before.
- **Factory/registry must be updated together with engine code** — If you add Forge detection to an engine class (e.g. `openai.py`), check whether a factory or router (e.g. `factory.py`) dispatches to that engine. If the factory doesn't know about Forge, your detection code is unreachable for most model names.
- **Search downstream impact of all changes** — After modifying a function, type, or variable, search the entire codebase for other uses. A change to a type alias affects every function that uses it. A change to a dispatch chain affects every caller.
