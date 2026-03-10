# AgentPR Master Plan

> 更新：2026-03-10 | 状态：D4 完成，战略转向 merge rate 优化
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

**Pipeline 技术闭环已完成。瓶颈从"能不能跑通"转移到"maintainer 会不会 merge"。**

- 22 repos 测试，19/22 PASS+PUSHED (86%)
- 17 PRs 提交，17/17 深度 review，4 代码修复，1 merged (5.9%)
- Code review gate + 双 PR 模式 + 常驻 daemon 已实装
- **Merge rate (5.9%) 才是下一步优化目标，不是 pipeline 功能**

---

## 3. 架构

```
Human (Telegram)  →  Manager (LLM + tools)  →  Worker (codex exec)  →  GitHub PR
      |                      |                        |
  NL + /commands        决策、审查、通知          读 repo、改代码、跑测试
```

**3 进程**：Telegram Bot + Manager Loop (persistent daemon) + GitHub Webhook Server
**连接**：SQLite DB + `.wake_manager` 信号文件（零依赖跨进程通知）

### 状态机（V2，10 状态）

```
QUEUED → EXECUTING → PUSHED → Code Review → PR Gate → CI_WAIT → REVIEW_WAIT → DONE
                        ↑                      |              |
                        └── ITERATING ←────────┘──────────────┘
+ PAUSED (任何非终态)  + NEEDS_HUMAN (升级)  + FAILED (终态)
```

### 角色边界

| 角色 | 职责 | 不做 |
|------|------|------|
| **Manager** | LLM 决策、分析、策略、审查、通知 | 不执行 shell、不直接改代码 |
| **Orchestrator** | 状态持久化、gate 执法、事件日志 | 不做决策 |
| **Worker** | 自主执行分析+实现+验证，自行调用 skills | 不管状态机 |

---

## 4. LLM 架构

**全部 9 个方法均为无记忆 1-shot 调用。** 无跨 tick 记忆，facts 每次从 DB 重建。

| # | 方法 | 位置 | 用途 | temp | 价值 |
|---|------|------|------|------|------|
| 1 | `decide_action` | Manager Loop | 状态机决策 | 0 | 中（rules 够用） |
| 2 | `grade_worker_output` | Runtime | 语义分级 | 0 | 中 |
| 3 | `explain_decision_card` | Telegram | 人类可读解释 | 0 | 低 |
| 4 | `decide_bot_action` | Telegram | NL→命令路由 | 0 | 低 |
| 5 | `triage_review_comment` | Manager Loop | review 评论分流 | 0 | 中 |
| 6 | `suggest_retry_strategy` | Manager Loop | 失败诊断+重试策略 | 0 | 中 |
| 7 | **`generate_pr_description`** | CLI | diff-aware PR body | 0.3 | **高** |
| 8 | **`review_code_changes`** | CLI | 深度代码审查 | 0 | **高** |
| 9 | **`assess_pr_readiness`** | Manager Loop | PR 就绪评估 | 0 | **高** |

**关键洞察**：只有 #7 #8 #9 直接影响 PR 质量（高价值）。其余是分类器，rules+heuristic 已够用。

**优点**：无状态、可重启、tick 解耦、failure-safe。
**缺点**：无跨 tick 推理连续性（"上次试了 X 不行，换 Y"做不到）。
**当前够用**：Rules 处理 90%+ 路径，LLM 做边界判断。

---

## 5. 质量链

```
Worker 执行 → Hybrid Grading (rules+LLM) → Code Review (LLM) → PR Gate → PR
    ↑                                            |
    └────────── ITERATING (fix issues) ──────────┘
```

- **Grading**：Rules 提取证据包（test/lint/diff/exit_code），LLM 做语义判断。硬护栏不可被 LLM 覆盖。
- **Code Review**：diff + changed files + sibling reference files + 7-section checklist → CLEAN/HAS_ISSUES。
- **PR Readiness**（auto mode）：code review + evidence + PR body + template + 7 principles → APPROVE/NEEDS_HUMAN。
- **三层防线**：Worker self-review → Manager code review → Human gate。

---

## 6. 战略方向：D5 Merge Rate 优化

| 优先级 | 项目 | 预期影响 |
|--------|------|---------|
| ⭐ | **收割现有 17 PRs**：签 CLA、bump 零回应 PR | 直接提升 merge 数 |
| ⭐ | **Repo targeting**：只选有 registry/factory 的 repo | 提升 PR 质量 |
| 高 | **Scale to 50 repos**：测量真实 merge rate | 获取 PMF 数据 |
| 高 | **Repo 可集成性评估**（新 LLM 方法）：preflight 评估 → 跳过不适合的 | 避免低质量 PR |
| 中 | **PR follow-up 自动化**：3 天无回应 → 礼貌 bump | 提升 respond rate |
| 中 | **CLA 签署策略** | 解除 2 个 blocked PRs |

---

## 7. 关键决策

1. Python 3.11，Worker = codex exec，默认 push_only，merge 始终人工
2. 混合策略：Rules 负责硬护栏+证据提取，LLM 负责语义判断。不做全量替换
3. Skills = Worker 自主调用（保护上下文窗口），不是 Orchestrator 注入
4. Commit 白名单：.py .pyi .ts .tsx .js .jsx .mjs .md .mdx .rst（新文件）。Lock files NEVER commit
5. Upstream default branch：始终用 `gh api repos/OWNER/REPO --jq '.default_branch'`
6. Hybrid grading 是正确中间态。AI centric ≠ all LLM
7. Manager function-calling。Forge 422 修复前用 Codex 原生 provider

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

### 验证数据

- **22 repos 测试**：19 PASS+PUSHED (86%), 3 HUMAN_REVIEW
- **17 PRs 提交**：1 merged (octotools #53), 13 clean, 2 CLA blocked, 1 maintainer positive
- **Code review 发现**：4 逻辑 bug（DAMO-ConvAI Union type, pipecat os.getenv, octotools factory, weave elif），全部修复
- **平均 worker attempt**：1.0（首次执行即 PASS）

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

57. **Manager 无记忆的 1-shot 是最大结构性限制。** 当前够用但距 North Star 有差距
58. **无状态设计优点不应被低估。** 可重启、可恢复、failure-safe。改进方向：更多 artifacts 作为上下文
59. LLM 覆盖全流程但深度不够（每个都是 1-shot）。模式化任务够用，复杂任务可能需要多步推理

### 战略洞察 (#60-#64)

60. **PASS rate ≠ merge rate，优化错误指标是最大浪费。** 86% PASS，5.9% merge
61. **代码质量和 repo 架构强相关。** Registry/factory repo → clean code。需修改已有代码 → bugs
62. **修 bug 的 PR 比加 provider 的 PR 更容易被 merge。** octotools 被 merge 因为它修了真实 bug
63. **9 个 LLM 方法中只有 3 个高价值（#7 #8 #9）。** 其余是分类器，rules 已够用
64. **"给现有方法加上下文"（低 ROI）≠ "让 Manager 做新事情"（高 ROI）**

---

## 10. 安全与隔离

1. Worker 写权限限定在 repo + `.agentpr_runtime` + `/tmp`
2. 仓库外写入禁用，读取仅允许白名单
3. PUSHED → PR 必须双确认（或 Manager assessment）
4. Merge 永远人工
5. 多人共享需升级为主机级隔离

---

## 11. 技术债

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
