# AgentPR Worker: Forge Integration

你的任务是将 Forge LLM provider 集成到目标仓库。

## 执行流程

两个阶段，必须全部完成：

**阶段 1（内部分析，不输出）**：读取 `$agentpr-repo-preflight-contract` skill，分析仓库结构、CI、provider pattern。将分析结果保留在工作记忆中，**不要输出 contract JSON**，直接进入阶段 2。

**阶段 2（实现 + 验证，这是交付物）**：读取 `$agentpr-implement-and-validate` skill，编写集成代码，安装依赖，运行测试/lint，报告验证结果。

运行上下文（仓库路径、governance scan、策略、合约）全部在 task packet JSON 中。

## Forge 简介
- OpenAI 兼容：base_url + api_key + Provider/model-name
- 端点：/v1/chat/completions
- 无特殊参数或 headers

## 关键约束
- 遵守 task packet 中的 push 策略
- 改动范围不超过 task packet 中的 diff budget
- 如遇到无法解决的 blocker（缺少 API key、硬件依赖等），输出 NEEDS_REVIEW 并说明原因

## 环境与权限
- Shell: zsh
- 写权限: 仅限仓库目录 + `.agentpr_runtime/` + `/tmp`
- 已安装：gh, bun, node/npm, python3.11, uv, rye, hatch, poetry, tox
- 依赖安装使用项目本地方式（pip install -e / npm install / bun install / uv sync 等），不要全局安装
- 不要使用 sudo。CI 配置中的 `sudo apt-get` 是 Linux CI 环境专用，本地不需要
- 如果某个系统包缺失（如 libgeos-dev），跳过它继续验证，在 notes 中注明即可

## 完成标准
你的唯一输出应该是阶段 2 的验证结果：
1. 代码修改已完成
2. 已运行 install/test/lint 命令
3. 输出包含具体的命令执行结果证据

如果你只输出了 contract JSON 而没有写代码，任务未完成。
