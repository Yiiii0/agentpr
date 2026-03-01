# Forge Constants & Integration Scenarios

## Forge Constants

```
Base URL:     https://api.forge.tensorblock.co/v1
API Key Var:  FORGE_API_KEY
Base URL Var: FORGE_API_BASE (optional override)
Model Format: Provider/model-name (e.g., OpenAI/gpt-4o-mini)
Fast Model:   OpenAI/gpt-4o-mini
```

Forge is fully OpenAI-compatible. Integration means changing 3 things: `base_url`, `api_key`, and model format.

## Quick-Skip Check

Before full analysis, answer these three questions:
1. Does the project's OpenAI client already accept a custom `base_url`?
2. Search the codebase for existing router integrations (OpenRouter, LiteLLM, Together, etc.). Do they have dedicated provider entries (enum values, URL detection, config blocks)?
3. If routers DO have dedicated entries — Forge needs one too. **Do NOT skip.**

**SKIP only if**: custom base_url works AND no other router has dedicated entries AND the project has no provider registry/enum/detection logic.

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
