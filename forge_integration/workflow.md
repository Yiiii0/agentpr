# Forge OSS Integration Workflow

## What is Forge
Forge is an OpenAI API compatible LLM router. Integration into any project that uses OpenAI SDK means changing 3 things: `base_url`, `api_key`, and model format (`Provider/model-name`). Nothing else.

## Forge Constants
```
Base URL:     https://api.forge.tensorblock.co/v1
API Key Var:  FORGE_API_KEY
Base URL Var: FORGE_API_BASE (optional override)
Model Format: Provider/model-name (e.g., OpenAI/gpt-4o-mini)
Fast Model:   OpenAI/gpt-4o-mini
Forge Repo:   /Users/yi/Documents/Career/TensorBlcok/forge
```

## Workspace
```
/Users/yi/Documents/Career/TensorBlcok/agentpr/
├── forge_integration/
    ├── workflow.md             # This file — reference manual (constants, rules, scenarios)
    ├── prompt_template.md      # AI execution guide (phases, checklists, validation)
    ├── claude_code_prompt.md   # Entry point prompt (paste into Claude Code)
    ├── pr_description_template.md  # PR description (for GitHub PR creation)
    ├── repos.md                # Tracking table
    ├── examples/
    │   ├── mem0.diff           # Reference: Python, env var detection
    │   └── dexter.diff         # Reference: TypeScript, prefix routing
    └── scripts/
        ├── prepare.sh          # Fork/clone/branch
        └── finish.sh           # Commit/push
└── workspaces/
    └── [repo-name]/            # Forked repos (created by prepare.sh)
```

## Per-Repo Flow

### Human Does
1. **Before**: Paste prompt from `claude_code_prompt.md` into Claude Code with repo list
2. **After**: Review pushes on GitHub, create PRs, handle post-push CI fixes

### Claude Code Does
```
Per repo:
  1. prepare.sh OWNER REPO        → fork/clone/branch
  2. Phase 1: Analyze              → CONTRIBUTING, CI, provider architecture
  3. Phase 2: Implement            → code + env setup + test + lint
  4. Phase 3: Self-validate        → git diff check, PR template compliance
  5. finish.sh                     → commit + push (only for PASS/NEEDS REVIEW)
  6. Phase 5: Post-push fixes      → CI failures, reviewer feedback (if applicable)

After all repos:
  7. Retrospective + workflow improvements
```

## Integration Scenarios

### A: Has router/aggregator (OpenRouter, LiteLLM)
Use the router's existing pattern. Forge goes through the common routing path.
- If the project uses litellm → `openai/` prefix + `api_base` (Forge is OpenAI-compatible, litellm supports this natively)
- If the project has its own provider registry → add Forge entry following the closest router's pattern
- **Files**: 2-3

### B: Multiple providers, no router
Add Forge alongside existing providers using the project's own pattern.
- Separate classes per provider → new Forge class (copy OpenAI, change base_url)
- Config-based providers → add Forge config entry
- **Files**: 2-4

### C: OpenAI only, no multi-provider support
Add env var detection in OpenAI initialization.
- If `FORGE_API_KEY` is set → use Forge base_url
- Otherwise → standard OpenAI
- **Files**: 1-2

### D: No OpenAI-compatible interface
**Skip.** Flag for human review.

## Rules

### Hard Rules (violating = PR rejected)
1. **Read CONTRIBUTING + CI workflow FIRST** — This determines branch, toolchain, test commands, code style, and integration-specific steps
2. **Execute ALL CONTRIBUTING steps** — If it says "Adding a New LLM: 1. code 2. tests 3. tox target", every step is mandatory. No partial compliance
3. **CONTRIBUTING branch > default branch** — If CONTRIBUTING says "branch from master" but HEAD is main, use master
4. **Use project's own toolchain** — rye/hatch/poetry/tox/bun, never raw `pip install -e .` unless project docs explicitly say to
5. **`git diff --name-only` before every commit** — Lock files, CI config, unrelated files = must revert
6. **No cross-repo mentions** — Don't reference other repos in commits, code comments, or docs
7. **Follow existing patterns exactly** — Copy the closest provider implementation. Use the common routing path unless technically impossible
8. **PR template checklist is a hard gate** — Every item must be satisfied BEFORE first push. No "fix in follow-up". Commit message format must match repo conventions (e.g., `feat(instrumentation): ...` for conventional commits repos)

### Important Rules (violating = wasted time)
9. **Minimal changes** — Target ≤ 4 files, prefer editing over creating new files
10. **Format only your files** — Never run formatter on entire project
11. **Match doc style** — Same length/format/structure as the most similar provider's docs
12. **Stop early on env issues** — 1-2 attempts max, then mark NEEDS REVIEW and move on
13. **Keep commit scope clean** — Never mix workflow file edits into target repo commits

### Environment
14. **macOS/zsh** — Use `source ~/.zshrc`, not `~/.bashrc`
15. **Python path** — `which python3.11` first; if wrong venv, use `/opt/homebrew/bin/python3.11`
16. **Isolated deps** — Use project-local venv/node_modules, never install globally
17. **Stop at push** — Do not create PRs automatically

## Toolchain Reference

**Pre-installed:** uv, rye, hatch, poetry, tox, bun, node/npm, brew, gh, `/opt/homebrew/bin/python3.11`

**Python toolchain priority:**
1. CI workflow (`.github/workflows/`) — copy exactly what CI does
2. `pyproject.toml` tool section (`[tool.rye]`, `[tool.poetry]`, `[tool.hatch]`, `[tool.uv]`)
3. Use that tool: rye → `rye sync --no-lock`; poetry → `poetry install`; hatch → `hatch run`; uv → `uv sync`
4. Plain `requirements.txt` → create venv, `pip install -r requirements.txt`
5. If `pyproject.toml` install is missing deps, check `requirements.txt` (some projects split deps across both)
6. **Never** raw `pip install -e .` unless project docs explicitly say to
7. **Never** `rye sync` without `--no-lock` (regenerates lock files, breaks diff)

**Manager runtime isolation (AgentPR):**
1. All tool caches/data are redirected to `<repo>/.agentpr_runtime/*` via env vars.
2. If a new tool appears, extend `orchestrator/runtime_env_overrides.json` first.
3. Treat global-install commands (`brew install`, `npm -g`, `uv tool install`, `poetry self`) as violations.

## Common Pitfalls

These are distilled from 8+ repo integrations. Each one cost significant debugging time.

- **"Copy the exception, not the rule"** — When a project has both a common path (litellm routing) and exceptions (dedicated functions for specific providers), ask WHY. If the common path supports Forge (it usually does), use the common path
- **Dependencies in unexpected places** — Some projects have deps in `requirements.txt` but not `pyproject.toml` (e.g., podcastfy + playwright). If imports fail after install, check `requirements.txt`
- **Doc code blocks executed as tests** — Some projects (e.g., pydantic-ai) run docs code blocks as tests. Missing env vars cause CI failure. Search for `test_examples` or `test_docs` and add `FORGE_API_KEY` to their fixtures
- **Coverage requirements** — Some CIs enforce 100% coverage. Every branch in new code must be tested, including fallback/unknown paths
- **Aggregator `model_profile` needs delegation** — Router providers (like Forge, OpenRouter) must parse `Provider/model-name` and delegate to provider-specific profile functions, not return a generic profile
- **Type alias registration** — Only register capabilities you've verified (e.g., Chat Completions support ≠ Responses API support)
- **False SKIP from surface-level analysis** — "Forge uses OpenAI SDK, already instrumented" is wrong if the project has a provider registry with entries for OpenRouter/Azure/etc. Always search for how similar routers are integrated before deciding SKIP. If OpenRouter has an enum value and URL detection, Forge needs the same
