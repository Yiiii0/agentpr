# Worker Base Prompt: Forge Integration

## 用途

这是 AgentPR `worker` 的基础提示词文件。

- 生产模式：由 manager 自动调用 `run-agent-step --prompt-file <this file>`，不需要手动粘贴。
- 运行上下文（目标仓库、run_id、contract、约束）由 AgentPR 在 task packet 中注入。
- 只有 Legacy 手动批量模式才需要手工替换 `Repo List`。

---

## Prompt

```
你的任务是将 Forge LLM provider 集成到以下 GitHub 仓库中。

## Repo List（仅 Legacy 手动批量模式使用；AgentPR worker 模式忽略）
https://github.com/OWNER/REPO1
https://github.com/OWNER/REPO2

## 工作目录
/Users/yi/Documents/Career/TensorBlcok/agentpr

## 权限与边界
- 你可以直接修改 `/Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/` 下的流程文件，用于修复流程问题
- 仅在 Retrospective 中识别到明确问题且有可执行改进时才修改
- 不要把 `agentpr/forge_integration` 的改动混入目标 repo 的集成 commit

## 核心文件（必须按顺序读完再开始）
1. /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/workflow.md — 规则、常量、场景、工具链（参考手册）
2. /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/prompt_template.md — 执行清单（Phase 0.5 → 1 → 2 → 3 → 4 → 5）
3. /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/examples/mem0.diff — Python 参考
4. /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/examples/dexter.diff — TypeScript 参考
5. /Users/yi/Documents/Career/TensorBlcok/forge/README.md — Forge API 文档
6. /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/pr_description_template.md — PR 描述模板

## 对每个 repo 执行以下流程

### Step 1: Prepare
bash /Users/yi/Documents/Career/TensorBlcok/agentpr/forge_integration/scripts/prepare.sh OWNER REPO
（如果 CONTRIBUTING 指定的 base branch 和 repo 默认分支不同，用 `prepare.sh OWNER REPO BASE_BRANCH`）
（推荐传入唯一分支名用于可回放：`prepare.sh OWNER REPO BASE_BRANCH feature/forge-<run_id>`）

### Step 2: 分析（prompt_template.md Phase 0.5 + Phase 1）
按顺序回答所有分析问题。特别注意：
- 优先消费 `task_packet.repo.governance_scan` 的已发现文件，再判断是否需要二次搜索
- CONTRIBUTING 的 integration-specific 步骤清单 — 列出并全部执行
- CI 是否有 coverage 要求、doc test
- 确定用 common path 还是 special path

### Step 3: 实现（prompt_template.md Phase 2）
- 用 CI workflow 的方式装依赖
- 按最相似 provider 的模式修改代码
- 只 format/lint 你改的文件

### Step 4: 自检 + 提交（prompt_template.md Phase 3 + 4）
- git diff --name-only 硬性检查
- PR template 合规检查
- finish.sh commit + push（第三参数传 repo 约定的 commit title，如 `feat(scope): ...`）

### Step 5: Post-Push（如果 CI 失败或有 reviewer 评论）
参考 prompt_template.md Phase 5

如果 FAIL 或 SKIP，不要 commit，记录原因并继续下一个 repo。

## 最终输出
完成所有 repo 后，输出汇总表格 + Retrospective（格式见 prompt_template.md Phase 6 和 Output Templates）。

## 环境信息
- Shell: zsh
- 已安装：brew, gh, bun, node/npm, /opt/homebrew/bin/python3.11, uv, rye, hatch, poetry, tox
- 不要全局安装任何包
- 不要自动创建 PR，停在 push
```

---

## 注意事项

1. **自动模式优先**：默认由 manager 自动推进，不手工填 `Repo List`
2. **失败处理**：worker 失败后交给 manager 判定重试/升级人工
3. **结果查看**：优先看 `inspect-run`、runtime report、Telegram 状态消息
4. **创建 PR**：按当前 gate 策略执行（目前默认人工确认后再建 PR）
