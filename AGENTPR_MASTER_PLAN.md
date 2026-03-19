# AgentPR Master Plan

> 更新：2026-03-16 | 状态：E3 验证基本完成，进入 D5 Merge Rate 优化
> 归档：`docs/AGENTPR_MASTER_PLAN_ARCHIVE_20260310_PRE_LEAN.md`（详细历史）

---

## 1. North Star

1. **人只观察。** 给 20 repos → 系统自主运行 → 人在 Telegram 看通知。
2. **Manager (LLM) 是大脑。** 理解上下文、做决策、审代码和 PR、主动通知。
3. **Worker (Codex) 自主执行。** 读 repo、写代码、跑测试、产出证据。
4. **安全默认。** Merge 始终人工。PR 创建需 human-confirm 或 Manager assessment。
5. **自我改进。** 每次运行的失败经验编码回 skill instructions。

---

## 2. 当前主矛盾

**Manager Agent 重构 + 验证完成（E0-E3）。旧代码已清理，Agent 端到端验证通过。**

- Manager 从 ~7.3K 行 plumbing 替换为 ~2K 行 agent loop + tools，旧 manager 代码已删除
- E3 验证：7 个 bug 修复，3 新 repo 端到端测试（worker → commit+push → code review → PR body 生成）
- 22+3 repos 测试，Worker 86% PASS，Agent 正确处理全流程
- **主矛盾转移：PASS rate (86%) → Merge rate (5.9%)。技术债已清，产品优化是下一步**

---

## 3. 架构

```
Human (Telegram)  →  Manager Agent (LLM + 10 tools)  →  Worker (codex exec)  →  GitHub PR
      |                        |                              |
  NL + /commands          多轮推理、审查、通知            读 repo、改代码、跑测试
```

**3 进程**：Telegram Bot + Agent Loop (persistent daemon) + GitHub Webhook Server
**连接**：SQLite DB + `.wake_manager` 信号文件（零依赖跨进程通知）
**Agent Loop**：每 tick 从 DB 重建 context → Agent session (max 15 turns) → tools 执行副作用 → 等待下一事件

### 状态机（V2，10 状态）

```
QUEUED → EXECUTING → PUSHED → Code Review → PR Gate → CI_WAIT → REVIEW_WAIT → DONE
                        ↑          |                     |              |
                        └── ITERATING ←──────────────────┘──────────────┘
+ PAUSED (任何非终态)  + NEEDS_HUMAN (升级)  + FAILED (终态)
```

注：E3 新增 PUSHED → ITERATING 转移（代码审查发现问题时可迭代修复后重新 push）。

### 角色边界

| 角色 | 职责 | 不做 |
|------|------|------|
| **Manager** | LLM 决策、分析、策略、审查、通知 | 不执行 shell、不直接改代码 |
| **Orchestrator** | 状态持久化、gate 执法、事件日志 | 不做决策 |
| **Worker** | 自主执行分析+实现+验证，自行调用 skills | 不管状态机 |

---

## 4. LLM 架构

### 新架构：Manager Agent（Phase E, 2026-03-11）

**单 Agent session with tools，替代 9 x 1-shot。** 通过 Forge `/chat/completions` 路由，模型无关。

```
Event → build context from DB → Agent session (max 15 turns) → tools side effects → done
```

**10 个 Agent tools**（安全检查嵌入 tool 实现）：

| Tool | 用途 | 安全检查 |
|------|------|---------|
| `query_runs` | 查询 run 状态/详情 | — |
| `update_state` | 状态转移 | state_machine.can_transition() |
| `read_evidence` | 读 worker 产出/grade/review（从 digest 文件读取完整证据） | — |
| `execute_worker` | 启动 codex exec + 自动 run-finish commit+push | retry_count < 3, 状态必须是 EXECUTING/ITERATING |
| `github_api` | create_pr/read_ci/read_reviews/post_comment/read_pr_template | PR gate + DoD via CLI pipeline |
| `review_code` | 深度代码审查（CLEAN → PR / HAS_ISSUES → 迭代） | 委托 run-code-review CLI（保留专门 prompt） |
| `generate_pr_body` | diff-aware PR body（从 digest 文件读取完整证据） | 委托 ManagerLLMClient（保留专门 prompt） |
| `notify_human` | Telegram 通知 | — |
| `reply_human` | Telegram 回复 | — |
| `update_skill` | 更新 skill 文件 | 文件白名单（低风险 only） |

**9 → 2 specialized tools + Agent 内在推理。** #7（PR body）和 #8（code review）保留专门 prompt，因为需要丰富的 context（diff、sibling files、checklist）。其余 7 个 1-shot 方法被 Agent 的多轮推理能力替代。

E3 关键改进：
- `execute_worker` 自动调用 `run-finish` 完成 commit+push（关闭 EXECUTING 卡住的 gap）
- `generate_pr_body` 从 digest 文件读取完整证据（不再依赖稀疏 DB metadata）
- `review_code` 返回 HAS_ISSUES 时，Agent prompt 明确要求迭代而非直接创建 PR
- PR body 的维护声明只通过 About Forge 模板出现一次（不再由 LLM 重复生成）

---

## 5. 质量链

```
Worker 执行 → run-finish (commit+push) → Hybrid Grading → Code Review → PR Gate → PR
    ↑                                                          |
    └──────────────── ITERATING (fix issues) ──────────────────┘
```

- **Grading**：Rules 提取证据包（test/lint/diff/exit_code），LLM 做语义判断。硬护栏不可被 LLM 覆盖。**E3 新增**：test-only changes 检测 → HUMAN_REVIEW（纯测试文件改动不是有效 integration）。
- **Code Review**：diff + changed files + sibling reference files + 7-section checklist → CLEAN/HAS_ISSUES。**E3 修复**：HAS_ISSUES 时 Agent 必须迭代（PUSHED → ITERATING），不可跳过直接创建 PR。
- **PR Body**：LLM 基于 commit diff + digest evidence 生成（Summary + Changes + Usage + Test Evidence），About Forge 固定模板拼接。
- **PR Readiness**（auto mode）：code review + evidence + PR body + template + 7 principles → APPROVE/NEEDS_HUMAN。
- **三层防线**：Worker self-review → Manager code review → Human gate。

---

## 6. 战略方向

### 近期：E3 收尾 + D5 Merge Rate 优化

| 优先级 | 项目 | 状态 | 预期影响 |
|--------|------|------|---------|
| ✅ | **E3 旧代码清理**：删除 manager_loop.py, manager_decision.py, manager_agent.py + 死代码 | 完成（-2100 行） | 净减代码 |
| ✅ | **E3 Bug 修复**：7 个 bug（证据读取、commit+push、迭代循环、PR body 等） | 完成 | Agent 端到端可用 |
| ✅ | **E3 验证**：3 新 repo 端到端测试（magentic, ExtractThinker, promptulate） | 完成 | Worker→Push→Review→PR body 全通 |
| 进行中 | **E3 PR 创建端到端**：approve-open-pr 实际创建 GitHub PR | 被 gh auth 阻塞 | 验证最后一环 |

### 中期：D5 Merge Rate 优化（主矛盾）

| 优先级 | 项目 | 预期影响 |
|--------|------|---------|
| ⭐ | **收割现有 PRs**：签 CLA、bump 零回应 PR | 直接提升 merge 数 |
| ⭐ | **Repo targeting**：只选有 registry/factory 的 repo | 提升 PR 质量 |
| ⭐ | **Agent 迭代循环**：HAS_ISSUES 时自动修复+重新 review（已有基础设施） | 提升 PR 首次质量 |
| 高 | **Scale to 50 repos**：测量真实 merge rate | 获取 PMF 数据 |
| 中 | **PR follow-up 自动化**：Agent 自动 bump | 提升 respond rate |
| 中 | **Worker 规则合规强化**：部分 worker 不遵守 forge_rules（如 os.environ 污染） | 减少 HAS_ISSUES |

---

## 7. 关键决策

1. Python 3.11，Worker = codex exec，默认 push_only，merge 始终人工
2. 混合策略：Rules 负责硬护栏+证据提取，LLM 负责语义判断。不做全量替换
3. Skills = Worker 自主调用（保护上下文窗口），不是 Orchestrator 注入
4. Commit 白名单：.py .pyi .ts .tsx .js .jsx .mjs .md .mdx .rst（新文件）。Lock files NEVER commit
5. Upstream default branch：始终用 `gh api repos/OWNER/REPO --jq '.default_branch'`
6. Hybrid grading 是正确中间态。AI centric ≠ all LLM
7. **Manager Agent via Forge `/chat/completions`**，模型无关（GPT-4o / Claude / Gemini 均通过验证）
8. **安全在 tools 层，不在 agent 层**。Agent 自由行动，tools 拒绝非法操作
9. **不引入框架**。30 行 agent loop + OpenAI function-calling format 足够
10. **Agent 工具输出必须包含下一步建议**。`_suggest_action()` 对两种路径（CLEAN vs HAS_ISSUES）都给出明确指引
11. **固定内容只在模板层出现，不在 LLM prompt 中重复**。避免分层拼接导致重复

---

## 8. 已完成阶段

| 阶段 | 日期 | 核心产出 |
|------|------|---------|
| D1 ACI 优化 | 03-02 | target_state enum、自动推导、显式反馈 |
| D1.6 Pipeline 修复 | 03-04 | 10 个安全网（audit logging、guardrail、mutex、stale detection、push 验证） |
| D2 真实验证 | 03-05 | 5/5 PASS+PUSHED，2-phase prompt、auto-stage 白名单 |
| D2.5 Commit 质量 | 03-06 | finish.sh 白名单 stage、self-review checklist、test command 过滤 |
| D3 边界修复 | 03-07 | 4/5 PASS tick 1，timeout 处理、failure count 持久化 |
| D3.5 自我改进 | 03-07 | Skills 自动升级（mtime）、≥2/4 test infra 信号、skill proposal 系统 |
| D3.6 LLM PR Body | 03-07 | `generate_pr_description()` diff-aware 生成，template-aware 填充 |
| D3.7 批量验证 | 03-08 | 19/22 PASS (86%)，17 PRs 提交，4 代码质量 fix |
| D3.8 Review→Skill | 03-08 | forge_rules +4、self_review +4、ITERATING prompt fix |
| D3.9 Code Review Gate | 03-09 | `review_code_changes()` + checklist + PUSHED→CLEAN/HAS_ISSUES 流程 |
| D4 运维化 | 03-10 | persistent daemon、wake file、进度通知、双 PR 模式、auto-title |
| E0 Agent Spike | 03-10 | agent loop + 9 tools + system prompt。5 turns / 18 tool calls 验证通过 |
| E1 Full Tool Layer | 03-10 | 10 tools + safety fixes + LLM body gen + PR flow + context builder |
| E2 Integration | 03-11 | agent_loop.py daemon + Telegram 通知 + webhook 集成 + audit。26 tool calls 生产验证 |
| E3 Cleanup | 03-16 | 删除 3 旧 manager 文件 + 死代码清理（-2100 行） |
| E3 Bug Fixes | 03-16 | 7 个 bug 修复：证据读取(A)、confirm_token(B+C)、test-only 检测(D+E)、auto run-finish(F)、迭代循环(G) |
| E3 Validation | 03-16 | 3 新 repo 端到端测试。Worker→Push→Review→PR body 全通。LLM body 证据正确 |

### 验证数据

- **25 repos 测试**：22 PASS+PUSHED (88%), 3 HUMAN_REVIEW（含 E3 新增 magentic, ExtractThinker, promptulate）
- **17 PRs 提交**：1 merged (octotools #53), 13 clean, 2 CLA blocked, 1 maintainer positive
- **Code review 发现**：4 逻辑 bug（DAMO-ConvAI Union type, pipecat os.getenv, octotools factory, weave elif），全部修复
- **平均 worker attempt**：1.0（首次执行即 PASS）
- **Agent 生产验证**：26 tool calls/tick, 15K input + 1.5K output tokens, 正确处理 20 active runs
- **E3 端到端验证**：3 新 repo 均完成 worker + commit+push + code review + LLM PR body 生成

---

## 9. 沉淀的核心认知（64 条）

### 基本原则 (#1-#10)

1. 先解决主矛盾：闭环决策，不是堆框架
2. 控制面稳定 > 执行面花哨
3. 最小改动能力来自 prompt + policy + gate 的协同
4. 运行成功率的核心是环境与规则证据，不是模型名
5. 可观测分层：日常看 digest，失败看 event stream
6. 系统应该简单，但不应过于简单。安全和持久化不能简化，决策逻辑应交给 LLM
7. **"精密工厂管理聪明工人"是反模式。** 应该是"轻量生产线 + 自主工人 + 关键检查点"
8. 复杂度应投资在"智能"上（失败分析、策略生成），不是"控制"上
9. LLM 应该是大脑 with tools，不是规则引擎附属品。但"大脑" ≠ "所有东西都用 LLM"
10. Skills 的正确用法是 worker 自主调用，不是 orchestrator 外部注入

### 验证驱动 (#11-#17)

11. **先用真实数据验证，再做架构调整。** 每次真实测试 > 10 次代码审查
12. 不要为改架构而改架构，先跑通第一个 PR 再说
13. Contract 不应作为人工审核 gate。Worker 内部产出、内部消费
14. Preflight 检查是通用的（自动检测项目类型和工具链）
15. 状态机的复杂度来自真实问题，但部分状态可内化到 worker
16. `min_test_commands` 硬性要求不适用所有项目；skill-1 作为独立外部产物无增量价值
17. 工具细节决定成败：rg 默认跳过 `.github/` 隐藏目录

### LLM 接入 (#18-#23)

18. **混合策略是正确的中间态。** Rules 做证据提取+硬护栏，LLM 做语义判断。不是全替换
19. **Confidence routing 让 LLM 在正确位置发挥作用。** 安全兜底在 rules，语义理解在 LLM
20. "先删后加"比"边加边删"安全
21. 代码量增长不等于膨胀，但超过阈值时维护成本急升
22. 通知"最后一公里"容易被忽略。产生 artifact 只是一半，推送到用户才是闭环
23. **如果做的不对，再大的代价也是最小的代价。** 先跑通第一个 PR 再说

### ACI 设计 (#24-#29)

24. **工具接口设计 > 模型能力（SWE-agent ICLR 2025）。** 用 enum + 自动推导 + 显式反馈修工具
25. **不要过度设计分层。** 没有成功的 agent 系统用"意图抽象层"。单 agent + 好工具 + 安全拦截器
26. **给 LLM 精简的信息比给它更多信息效果好**
27. **Skills 即单一来源。** slim entry + skill references 优于 everything-in-one-prompt
28. 外部依赖的 bug 要尽早隔离，不阻塞核心流程
29. **先纠偏认知再写代码。** 花时间对标行业 > 直接实现

### Pipeline 修复 (#30-#34)

30. **一次真实测试胜过十次代码审查。** dirty workspace bug 纯静态分析不可能发现
31. **Guardrail：rules 优先但 LLM 可降级。** 不可升级（激进覆盖被阻止）
32. 边界情况的修复成本远低于出问题的代价（10-20 行 vs Worker 白费）
33. **审计日志不是可选项。** facts_snapshot 20 行代码 ROI 最高
34. **不要继续堆代码。去跑真实测试。**

### D2 验证 (#35-#40)

35. **codex turn.completed 是隐形断点。** 修复：2-phase prompt（分析=内部不输出，实现=唯一交付物）
36. **LLM 对 error 字段过度反应。** 给 LLM 的错误信息措辞直接影响决策质量
37. **`git add -u` 是新文件隐形杀手。** 修复：白名单 auto-stage + 黑名单排除 lock files
38. test command 检测需排除 read commands。分类顺序即优先级
39. Self-review checklist 是提升 worker 质量的最低成本手段
40. **Commit 质量审计必须检查实际 pushed 内容，不只看 grade。** Grade=PASS ≠ 产出质量达标

### 自我改进 (#41-#47)

41. **Skills 部署 ≠ Skills 更新。** 系统必须自动检测并升级（mtime 比较）
42. **单信号 false positive 是分类问题通病。** ≥2/4 信号阈值消除误判
43. **Worker 在失败后放弃是最危险的行为模式。** Instructions 必须编码"永不放弃验证"
44. Metrics 有局限但可接受（diff numstat 不含 untracked，但 changed_files 足够）
45. **自我改进闭环 = 测试发现 → 编码回 skill instructions → 下次不犯**
46. **Grading 架构迁移不需要做。** AI centric ≠ all LLM。基础设施确定性，Agent 智能
47. PR Description 应 LLM 智能填充 template，不是傻拼接

### 批量验证 (#48-#52)

48. **量化 grading ≠ 代码质量。** Pipeline 需要 code review gate（PUSHED → PR 之间）
49. **os.environ 全局污染是 Worker 常犯反模式。** 正确做法：显式 kwargs
50. **模型检测必须用 prefix match。** `"/" in model` → `startswith("forge/")`
51. Preflight 对混合语言项目应宽容（JS 工具降级为 warning）
52. **Skill-3 prompt 必须区分 integration 和 iteration**

### 深度 Review (#53-#56)

53. **Pipeline 需要 code review gate（定量 grading ≠ 定性审查）。** 17/17 review 发现 4 逻辑 bug
54. **Worker 无法执行代码结构重排。** 擅长"添加新代码"，不擅长"重组已有逻辑"
55. **Review 经验必须编码到可持久化外部文件。** 三层防线：Worker 自检 → Manager review → Human gate
56. **PR body 不应包含营销文本。** 被 maintainer 视为低质量贡献信号

### 架构审视 (#57-#59)

57. **Manager 无记忆的 1-shot 是最大结构性限制。** ~~当前够用但距 North Star 有差距~~ → Phase E 已解决：Agent session 内多轮推理
58. **无状态设计优点不应被低估。** 可重启、可恢复、failure-safe。→ Phase E 保留：session 间无状态，session 内有推理
59. ~~LLM 覆盖全流程但深度不够（每个都是 1-shot）~~ → Phase E 已解决：Agent 自主多步决策

### 战略洞察 (#60-#64)

60. **PASS rate ≠ merge rate，优化错误指标是最大浪费。** 86% PASS，5.9% merge
61. **代码质量和 repo 架构强相关。** Registry/factory repo → clean code。需修改已有代码 → bugs
62. **修 bug 的 PR 比加 provider 的 PR 更容易被 merge。** octotools 被 merge 因为它修了真实 bug
63. **9 个 LLM 方法中只有 3 个高价值（#7 #8 #9）。** → Phase E：9 → 2 specialized tools + Agent 推理
64. **"给现有方法加上下文"（低 ROI）≠ "让 Manager 做新事情"（高 ROI）**

### Manager Agent 重构 (#65-#69)

65. **30 行 agent loop 替代 7.3K 行 plumbing。** 不需要框架——`/chat/completions` + for 循环 + tool_executor 就是 agent 框架
66. **Forge 多轮 tool-calling 模型无关。** GPT-4o / Claude Sonnet 4 / Gemini 2.5 Flash 均通过验证。通过 `AGENTPR_MANAGER_MODEL` 切换，无需改代码
67. **Agent 的错误处理比规则引擎更聪明。** Agent 读到 ERROR 后自行调整策略（换工具、升级人工），规则引擎只能走预设分支
68. **工具输出包含下一步建议是关键 ACI 设计。** `_suggest_action()` 让 Agent 知道该做什么，不只是返回 raw data
69. **并行运行是安全的迁移策略。** `run-agent-loop` 和 `run-manager-loop` 共存，新旧对比后再删旧代码

### E3 验证 (#70-#76)

70. **状态机缺失转移是隐形 bug。** PUSHED→ITERATING 不在允许列表 → Agent 无法迭代修复代码。状态机必须覆盖所有合理的业务流程，不只是正向流程
71. **Tool 输出截断会导致级联失败。** stdout 2000 字符截断 → JSON 被截断 → confirm_token 丢失 → PR 创建失败。任何 tool 的输出上限必须覆盖最大合理输出
72. **DB metadata 和 artifact 文件的信息密度差距巨大。** Artifact metadata 只存 4 个字段，文件里有完整证据。Agent 工具必须从文件（而非 metadata）读取证据
73. **Worker commit+push 不能假设由外部驱动。** `allow_agent_push=False` 时 worker 不 commit，但 Manager Agent 的 `execute_worker` 也没调 `run-finish` → 改动永远不 push。关键步骤必须有明确 owner
74. **纯测试文件改动不是有效 integration。** 部分 worker 只加测试不改源码 → grading 判 PASS 但实际无集成价值。质量检查必须区分"有改动"和"有效改动"
75. **LLM 生成内容与模板拼接必须去重。** PR body 中维护声明出现两次：LLM prompt 要求生成一次 + About Forge 模板包含一次。分层系统中固定内容应只在一层出现
76. **Prompt 中的流程描述必须覆盖异常路径。** Agent prompt 只写了 "CLEAN → create PR"，没有明确写 "HAS_ISSUES → iterate"。Agent 会忽略未明确指示的路径

---

## 10. 安全与隔离

1. Worker 写权限限定在 repo + `.agentpr_runtime` + `/tmp`
2. 仓库外写入禁用，读取仅允许白名单
3. PUSHED → PR 必须双确认（或 Manager assessment）
4. Merge 永远人工
5. 多人共享需升级为主机级隔离

---

## 11. 技术债

- ~~旧 Manager 代码清理~~ ✅ E3 已完成：删除 manager_loop.py (1313), manager_decision.py (403), manager_agent.py (219) + 死代码清理
- **Telegram inbound → Agent session**：当前只有 outbound 通知，inbound 消息仍走旧 Bot 路由
- **Worker 规则合规**：部分 worker 不遵守 forge_rules.md（如 ExtractThinker 直接修改 os.environ），需强化 worker prompt 或增加 post-execution 检查
- **manager_llm.py 瘦身**：旧 9 x 1-shot 方法仍在（generate_pr_description 和 review_code_changes 被 Agent tools 复用，其余 7 个待清理）
- Forge 422 修复后切回（独立于主线）
- Bot 会话持久化 ✅（SQLite `bot_sessions` 表）
- Review triage 用户确认 ✅（`/approve_triage` + artifact gate）
- 自我迭代安全分层 ✅（低风险 auto-apply + git commit，高风险 `/approve_skill`）
- PR follow-up 自动化 ✅（`BUMP_PR_COMMENT` action，3 天无回应自动 bump）

---

## 12. 参考

1. SWE-agent ACI 论文（ICLR 2025）：<https://arxiv.org/abs/2405.15793>
2. OpenHands V1 SDK：<https://arxiv.org/abs/2511.03690>
3. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
4. GitHub Copilot coding agent：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
