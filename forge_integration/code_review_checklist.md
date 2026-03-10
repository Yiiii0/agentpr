# Code Review Checklist for Forge Integration PRs

This checklist is used by the automated code review step (manager LLM) to evaluate
Worker-produced code BEFORE creating a PR. It encodes lessons from 17 deep PR reviews.

This file is loaded at review time and can be updated as new patterns are discovered.

---

## 1. Reference Provider Alignment

- Did the Worker choose the MOST SIMILAR existing provider as reference?
  - Forge is an aggregator → reference should be OpenRouter, LiteLLM, AiHubMix (not Groq, Together, etc.)
  - If no aggregator exists in the repo → reference the closest architectural match (e.g. nearest base class)
- Does the Forge code match the reference provider's:
  - Parameter names, types, and order?
  - Default values?
  - Return types?
  - Error handling patterns?
- Are there any additions NOT present in the reference provider? Flag each one:
  - Extra `os.getenv()` fallback? (pipecat lesson: OpenRouter doesn't do this)
  - Extra validation (`ValueError`, type checks)? (DAMO-ConvAI lesson)
  - Extra helper methods? (py-gpt `_apply_auth()` — acceptable if DRY, flag if changes semantics)

## 2. Dispatch Chain / Routing Integrity

- If the code inserts into an `if/elif` chain:
  - Does the new branch shadow any existing branches? (weave lesson: `elif "/" in model_name` before `elif model_info` broke 937 models)
  - Is the insertion point AFTER more specific checks, not before?
  - Does the new branch handle ALL cases (including the "no match within branch" fallthrough)?
- If the code adds detection logic in an engine/handler:
  - Is the factory/router ALSO updated to dispatch to that engine? (octotools lesson: forgot factory.py)
  - Are ALL model name patterns routed correctly? (e.g. `forge/Anthropic/claude` shouldn't route to ChatAnthropic)

## 3. Environment Variable Handling

- No `os.environ["KEY"] = value` mutation (pollutes global state)
- API key passed via explicit kwargs (`api_key=`, not env var injection)
- `os.getenv()` usage matches the reference provider's pattern
- If the reference provider reads from config system → Forge should too (not env var fallback)

## 4. Type Safety

- No modification of existing `Literal[...]` types (use `Union[Literal[...], str]` if needed)
- No parameter type changes that break existing callers
- All variables initialized before conditional branches (no unbound variable risk)
- Model detection uses `startswith("forge/")`, NOT `"/" in model_string`

## 5. Downstream Impact

- Search for all callers of modified functions — are they affected?
- Search for all users of modified types — are they compatible?
- If a type alias is changed, check every file that imports it
- If a dispatch chain is modified, check every function that calls the dispatch

## 6. Completeness

- If the repo has a factory/registry → Forge is registered
- If the repo has env.example → `FORGE_API_KEY` is added
- If the repo has a README providers list → Forge is added in matching format
- If the repo has CONTRIBUTING requirements → all steps are followed
- If similar providers have docs/tests → Forge has equivalent docs/tests

## 7. Minimal Diff Discipline

- Only necessary files are modified (no unrelated formatting, no unnecessary refactors)
- No lock files modified
- No CI/workflow files modified unless explicitly required
- Code additions are proportional to what the reference provider has

---

## Severity Classification

When reporting issues, classify each as:

### HIGH (blocks PR, must fix)
- Shadows existing dispatch branches (weave elif bug)
- Missing factory/router update (octotools bug)
- `os.environ` mutation
- Type changes that break existing code
- Unbound variable in conditional path

### MEDIUM (should fix, Worker can likely handle)
- Extra validation not in reference provider
- Extra `os.getenv()` fallback not in reference
- Missing env.example entry
- Missing factory registration
- Parameter order mismatch with reference

### LOW (note for review, may keep)
- Extra helper method (DRY improvement)
- Minor naming difference
- Extra env var alias (e.g. two env var names for base_url)
- Default parameter values slightly different from reference

---

## Review Output Format

The review should produce:
1. **verdict**: CLEAN | HAS_ISSUES
2. **issues**: list of {severity, file, description, suggested_fix}
3. **reference_provider**: which existing provider was used as reference
4. **summary**: 1-2 sentence overall assessment
