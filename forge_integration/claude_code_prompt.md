# AgentPR Worker: Forge Integration

你的任务是将 Forge LLM provider 集成到目标仓库。

## 执行框架

你已有 AgentPR skills 可用。按以下顺序执行：
1. `$agentpr-repo-preflight-contract` — 分析仓库，输出集成合约
2. `$agentpr-implement-and-validate` — 按合约实现并验证

运行上下文（仓库路径、governance scan、策略、合约）
全部在 task packet JSON 中，不需要猜测。

## Forge 简介
- OpenAI 兼容：base_url + api_key + Provider/model-name
- 端点：/v1/chat/completions
- 无特殊参数或 headers

## 关键约束
- 遵守 task packet 中的 push 策略
- 改动范围不超过 task packet 中的 diff budget
- 如遇到无法解决的 blocker，输出 NEEDS REVIEW 并说明原因

## 环境
- Shell: zsh
- 已安装：brew, gh, bun, node/npm, python3.11, uv, rye, hatch, poetry, tox
- 不要全局安装任何包
