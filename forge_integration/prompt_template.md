# Forge Integration - AI Agent Prompt Template

## Context

You are integrating Forge into a target repository. See `workflow.md` for Forge constants, rules, toolchain reference, and integration scenarios. This file is your execution checklist.

**Forge in a nutshell:**
- OpenAI-compatible: `base_url` + `api_key` + `Provider/model-name`
- Endpoint: `/v1/chat/completions` (standard OpenAI)
- No special parameters, headers, or response handling

**Reference diffs:** `examples/mem0.diff` (Python, env var detection) and `examples/dexter.diff` (TypeScript, prefix routing)

---

## Phase 0.5: Quick-Skip Check

Before full analysis, answer these questions:
1. Does the project's OpenAI client already accept a custom `base_url`?
2. **Search the codebase for existing router integrations** (OpenRouter, LiteLLM, Together, etc.). Do they have dedicated provider entries (enum values, URL detection, config blocks)? Use `grep -r "openrouter\|open_router\|litellm" --include="*.py" --include="*.ts"` or equivalent.
3. If routers DO have dedicated entries — Forge needs one too. **Do NOT skip.**

**SKIP only if**: custom base_url works AND no other router has dedicated entries AND the project has no provider registry/enum/detection logic.

---

## Phase 1: Analysis (complete ALL before any code changes)

### 1.1 Project Rules & CI

**Read these in order. They determine everything else.**

1. **CONTRIBUTING / AGENTS.md / PR template:**
   - What branch to create from? Cross-check with `git remote show upstream | grep HEAD`
   - What code style rules?
   - **Integration-specific steps?** (e.g., "Adding a New LLM: 1. code 2. tests 3. tox target") — List ALL steps explicitly. Every one is mandatory.
   - PR template checklist items? List them — these become Phase 3 hard gates.

2. **CI workflows (`.github/workflows/`):**
   - What exact commands for: install deps, run tests, run lint/format?
   - What env vars does CI set? (e.g., `OPENAI_API_KEY=test-key`)
   - **Does CI enforce coverage?** What threshold? (e.g., `fail-under=100`)
   - **Does CI run doc tests?** (search for `test_examples`, `test_docs`, or doc code block execution)

3. **Environment setup docs** — Search ALL of these before guessing:
   - `tests/README.md`, `DEVELOP.md`, `DEVELOPMENT.md`, `SETUP.md`, `HACKING.md`
   - README sections: "Development", "Setup", "Contributing", "Testing"
   - `Makefile`, `docker-compose.yml`, `environment.yml`
   - **Priority: project-specific setup docs > CI workflows > pyproject.toml inference**

4. **Code style config:** `.editorconfig`, `ruff.toml`, `.eslintrc`, `.prettierrc`?

### 1.2 Project Basics
- Language? Package manager? Test runner? Formatter/linter?
- (Confirm all from CI workflow, not guessing)

### 1.3 LLM Provider Architecture
- What providers exist? How are they organized?
- **Common path vs special path:** Is there a generic routing path most providers share (litellm, OpenAI client factory)? Which providers bypass it with dedicated code?
- **For each special-case provider: WHY?** Is it because the common path technically can't support it? Or legacy/design choice?
- **Forge should use the common path** unless technically impossible. Don't copy the exception.
- Which provider is closest to Forge? (= uses the same routing mechanism Forge should use)

### 1.4 Model Name Handling
Forge format: `Provider/model-name` (e.g., `OpenAI/gpt-4o`). Case-insensitive on prefix.

- Pass-through or transform? Validation? `/` handling? Model registry? Prefix stripping?
- Decision: pass-through → OK. Prefix routing → OK. Lowercased → OK. `Provider/` stripped → problem. Whitelist → need to allow arbitrary strings.

### 1.5 Integration Approach
Which scenario (A/B/C/D) from workflow.md? Which files to modify (target ≤ 4)?

### 1.6 Docs & Tests
- Where are docs? What format? How long is a similar provider's doc?
- Does a similar provider have dedicated tests? If no → Forge doesn't need them either.
- **If CI enforces coverage:** what branches need test coverage? Plan tests for all code paths.

---

## Phase 2: Implementation

### Environment Setup
1. Copy what CI does — same commands, flags, env vars
2. Use project's toolchain (see workflow.md Toolchain Reference)
3. If install is missing deps, check `requirements.txt` alongside `pyproject.toml`
4. Stop after 1-2 failed attempts → mark NEEDS REVIEW, move on

### Code Changes
1. Follow the closest provider's pattern exactly
2. Only format/lint files you modified
3. Doc length must match similar provider (±10 lines)
4. No cross-repo mentions in code/comments/docs
5. Never modify lock files or CI config

### Validation Commands
```bash
# Run after implementation, paste output
git diff --stat           # Target: ≤ 4 files
git diff --name-only      # HARD GATE: every file must be intentional
# Project-specific test command (from CI)
# Project-specific lint command (from CI)
```

---

## Phase 3: Self-Validation (before commit)

### Objective Checks
- [ ] `git diff --name-only` — only intentional files (no lock files, no CI config)
- [ ] `git diff --stat` — ≤ 4 files, reasonable line count
- [ ] Tests pass (project's own test command)
- [ ] Lint/format pass (project's own commands)

### PR Template Compliance (HARD GATE)
If the repo has `.github/PULL_REQUEST_TEMPLATE.md`:
- Go through every checklist item
- Each must be **already satisfied**, not "will do later"
- Missing items → fix before push
- Truly N/A items → note why
- **Commit message format must match repo conventions** (e.g., conventional commits: `feat(scope): ...`). Check the template AND recent commit history for the expected format. This determines the PR title.

### Judgment Checks
- [ ] Uses the common routing path (not a separate function duplicating existing logic)?
- [ ] No unnecessary new files (if similar providers don't have dedicated tests/classes, neither should Forge)?
- [ ] Doc length matches similar provider?
- [ ] All CONTRIBUTING integration steps completed?
- [ ] Branch based on CONTRIBUTING-specified branch?
- [ ] If CI enforces coverage: all branches in new code are tested?
- [ ] If CI runs doc tests: FORGE_API_KEY added to doc test fixtures?

### Classification
- **PASS** — All checks pass → commit and push
- **NEEDS REVIEW** — Implementation correct but pre-existing env/test issues → commit and push
- **FAIL** — Our changes cause test failures → do NOT commit
- **SKIP** — No OpenAI-compatible interface → do NOT commit

---

## Phase 4: Commit and Push

Only for PASS and NEEDS REVIEW:

```bash
# Stage any NEW files first (finish.sh only auto-stages modified tracked files)
git add path/to/new_file.py

# Then run finish.sh
bash .../scripts/finish.sh "description of changes" "ProjectName" "repo-required-commit-title"
```

---

## Phase 5: Post-Push (when CI fails or reviewers comment)

After push, CI may fail or automated reviewers (Devin, etc.) may flag issues. Handle them:

### CI Failures
1. Read the CI log — identify which check failed and why
2. Common causes:
   - **Coverage below threshold** → add tests for uncovered branches (including fallback/unknown paths)
   - **Doc test failure** → add `FORGE_API_KEY` to doc test fixtures (search for `test_examples`)
   - **Lint/type check** → fix and push
3. Push fix commits (normal push, not force push — PR is already open)

### Reviewer Comments
1. Evaluate each comment for correctness
2. Fix legitimate issues, push fix commits
3. For debatable points: check if there's precedent in the codebase (e.g., "Fireworks also does this")

### What NOT to do
- Don't force push after PR is open (destroys review context)
- Don't ignore CI failures — they must all pass for merge

---

## Phase 6: Retrospective (after all repos in batch)

Evaluate whether workflow files should be improved.

1. Design-system lens: prompt design, step orchestration, checkpoints, failure recovery
2. List 0-3 issues. Each must have: Symptom, Root Cause, Improvement, Expected Impact
3. Only actionable improvements — no vague suggestions
4. If improving, directly edit files in `agentpr/forge_integration/`
5. Log changes in `Workflow Changes` section

---

## Output Templates

### Per-Repo Summary
```
## Forge Integration: [REPO_NAME]
Status: PASS / NEEDS REVIEW / FAIL / SKIP
Approach: [A/B/C/D] - [brief description]
Files Changed: [count] | Lines Added: [count]
Changes:
- [file]: [what changed]
Test/Lint Results: [pass/fail summary]
Notes: [issues or concerns]
```

### Batch Summary Table
```
| Repo | Status | Approach | Files | Lines | Notes |
|------|--------|----------|-------|-------|-------|
```
