# Analysis Checklist

Complete ALL items before any code changes.

## 1.1 Project Rules & CI

**Read these in order. They determine everything else.**

0. **Use manager-provided governance scan first (if present in task packet):**
   - Read `task_packet.repo.governance_scan.groups` and open those files first.
   - Treat this as deterministic seed evidence (reusable across skill-1/skill-2).
   - Then decide whether second-pass search is needed for naming variants.

1. **CONTRIBUTING / AGENTS.md / PR template:**
   - What branch to create from? Cross-check with `git remote show upstream | grep HEAD`
   - What code style rules?
   - **Integration-specific steps?** (e.g., "Adding a New LLM: 1. code 2. tests 3. tox target") — List ALL steps explicitly. Every one is mandatory.
   - PR template checklist items? List them — these become hard gates.

2. **CI workflows (`.github/workflows/`):**
   - Exact commands for: install deps, run tests, run lint/format?
   - Env vars CI sets? (e.g., `OPENAI_API_KEY=test-key`)
   - **Coverage enforcement?** What threshold? (e.g., `fail-under=100`)
   - **Doc tests?** (search for `test_examples`, `test_docs`, or doc code block execution)

3. **Environment setup docs** — Search ALL before guessing:
   - `tests/README.md`, `DEVELOP.md`, `DEVELOPMENT.md`, `SETUP.md`, `HACKING.md`
   - README sections: "Development", "Setup", "Contributing", "Testing"
   - `Makefile`, `docker-compose.yml`, `environment.yml`
   - **Priority: project-specific setup docs > CI workflows > pyproject.toml inference**

4. **Code style config:** `.editorconfig`, `ruff.toml`, `.eslintrc`, `.prettierrc`?

## 1.2 Project Basics
- Language? Package manager? Test runner? Formatter/linter?
- (Confirm all from CI workflow, not guessing)

## 1.3 LLM Provider Architecture
- What providers exist? How are they organized?
- **Common path vs special path:** Is there a generic routing path most providers share (litellm, OpenAI client factory)? Which providers bypass it?
- **For each special-case provider: WHY?** Technically necessary or legacy/design choice?
- **Forge should use the common path** unless technically impossible.
- Which provider is closest to Forge?

## 1.4 Model Name Handling
Forge format: `Provider/model-name` (e.g., `OpenAI/gpt-4o`). Case-insensitive on prefix.
- Pass-through or transform? Validation? `/` handling? Model registry? Prefix stripping?

## 1.5 Integration Approach
Which scenario (A/B/C/D) from `forge_scenarios.md`? Which files to modify (target ≤ 4)?

## 1.6 Docs & Tests
- Where are docs? What format? How long is a similar provider's doc?
- Does a similar provider have dedicated tests? If no → Forge doesn't need them either.
- **If CI enforces coverage:** what branches need test coverage? Plan tests for all code paths.
