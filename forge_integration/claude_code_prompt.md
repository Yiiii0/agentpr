# Claude Code Prompt: Forge Integration

## 使用方式

打开 Claude Code，粘贴以下 prompt，替换 `REPO_LIST` 部分即可。

---

## Prompt

```
你的任务是将 Forge LLM provider 集成到以下 GitHub 仓库中。

## Repo List
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

1. **Repo 数量**：建议每次 3-5 个，避免超出用量限制
2. **失败处理**：AI 会自动跳过 FAIL/SKIP 的 repo
3. **结果查看**：汇总表 + GitHub review
4. **创建 PR**：人工在 GitHub 网页端创建
