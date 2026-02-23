# PR Description Template

创建 PR 时复制以下内容，替换 `[Project Name]` 和 `[具体改动]` 部分。

---

## About Forge

[Forge](https://github.com/TensorBlock/forge) is an open-source middleware that routes inference across 40+ upstream providers (including OpenAI, Anthropic, Gemini, DeepSeek, and OpenRouter).

## Motivation

We have seen growing interest from users in the ecosystem who standardize on Forge for their model management and want to use it natively with [Project Name]. This integration aims to bridge that gap.

## Why Forge?

- **Unblock Users**: It enables users who already rely on Forge (for unified keys or local privacy) to onboard to [Project Name] seamlessly.
- **Future-Proofing**: It acts as a decoupling layer. Instead of this project needing to maintain individual adapters for every new model or provider (e.g., DeepSeek, or the next big model), Forge users can access them immediately through this single interface.

## Key Benefits

- **Driven by User Demand**: Addresses the need for interoperability for users who manage their keys centrally via Forge.
- **Self-Hosted & Privacy-First**: Unlike SaaS-only aggregators, Forge is open-source and designed to be self-hosted (with an optional managed service). This is critical for users who require data sovereignty and cannot send keys/logs to third-party clouds.
- **Compatibility**: Forge natively supports established aggregators (like OpenRouter) as well as direct provider connections (BYOK), offering flexibility without replacing any existing defaults in this project.
- **Non-breaking**: This change is purely additive. The existing logic for other providers remains untouched.

## Changes

- [具体改动描述，按 repo 填写]
- Environment variable: `FORGE_API_KEY`
- Model format: `Provider/model-name` (e.g., `OpenAI/gpt-4o`)

## References

- Repo: https://github.com/TensorBlock/forge
- Docs: https://www.tensorblock.co/api-docs/overview
- Main Page: https://www.tensorblock.co/
