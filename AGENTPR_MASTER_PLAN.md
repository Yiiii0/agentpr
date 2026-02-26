# AgentPR Master Plan (Manager-Worker Final Target)

> 更新时间：2026-02-26（C1 测试完成 + 混合分级策略确认 + 文档一致性修复）
> 状态：Phase C1 首次测试完成（HKUDS/DeepCode），进入 C1 迭代 + C2 准备。
> 目标：人只通过 bot 与系统交互，manager 持续编排 worker 完成 OSS 小改动 PR 流程（默认 `push_only` + 人工 PR gate）

---

## 0. 历史沉淀归档

1. 旧版详细计划与历史实践记录已归档：`docs/AGENTPR_MASTER_PLAN_ARCHIVE_20260225_PRE_REWRITE.md`
2. 本文件聚焦“最终形态和执行路线”；历史细节不删除，只转为归档管理。

---

## 1. 最终目标（North Star）

1. 人只在 Telegram 对话：下发任务、看状态、做 approve/deny。
2. Manager（LLM）持续在线：理解自然语言、调用编排工具、推进状态机、总结反馈、提出迭代建议。
3. Worker（codex exec）专注执行：读仓库、改代码、建本地环境、跑测试、产出证据。
4. 系统默认安全：最小权限、最小改动、可回放、可审计。
5. `merge` 始终人工执行；`create PR` 受 gate 保护。

---

## 2. 实事求是：当前主矛盾

1. 不是缺”编排框架”，而是缺 Manager LLM 决策层闭环。
2. 不是缺”更多日志”，而是缺”可执行决策输入”到下一步动作的自动路由。
3. 不是缺”多 agent 并发”，而是缺”单 run 高质量稳定完成率”。
4. 当前最直接的稳定性风险是”runtime 分级误判”会把本可继续的 run 过早打到 `NEEDS_HUMAN_REVIEW`。
5. **复杂度分配错位**：12K 行 orchestrator 代码中，~70% 是确定性控制（状态机、规则决策、regex 分析），而真正需要智能的地方（失败分析、重试策略、review 处理、运营汇报）仍是规则或空白。Manager LLM 当前仅做”从 N 选 1 选择题”，增量价值极低。
6. **抽象层过多**：worker 最终收到的就是一个 prompt string，但经过 5 层构建（prompt → skills → task packet → safety contract → executor）。

结论：方向正确，不需要推翻重写；但当前优先级应从”补更多控制逻辑”转向”让 LLM 在正确的位置发挥智能”。

---

## 3. 外部对标后的架构判断

### 3.1 可借鉴（对我们有价值）

1. GitHub Copilot coding agent：后台执行 + 人审 PR + session log 可追踪。
2. OpenHands：`fix-me` / `@openhands-agent` 触发 + 评论迭代闭环。
3. LangGraph/Temporal/Inngest：强调持久化、长流程恢复、human-in-the-loop。

### 3.2 不直接照搬（对当前阶段性价比低）

1. 直接上重型多 agent 编排框架（增加复杂度，不能直接提升 worker 代码质量）。
2. 先做 swarm 并发（会放大环境/依赖/成本问题）。
3. 过早追求 Web 控制台（当前 Telegram 足够）。

### 3.3 OpenClaw 的架构启示（深入分析后更新）

**OpenClaw 核心架构原则**：”The hard problem in personal AI agents is not the agent loop itself, but everything around it.”

**对 AgentPR 的关键启示：**

| 设计点 | OpenClaw 做法 | AgentPR 当前 | AgentPR 应借鉴 |
|--------|-------------|-------------|---------------|
| 谁是大脑 | LLM agent（Pi runtime） | Rules engine + LLM 橡皮章 | Manager 应是真正的 LLM agent with tools |
| Gateway 角色 | 薄层：消息路由 + 会话管理 | 厚层：状态机 + 规则决策 + regex 分析 | Orchestrator 应退化为薄层：持久化 + gate |
| Skills | Markdown 文件，agent 自己决定何时调用 | Orchestrator 按状态注入 skill prompt | Worker 自主调用 skill（保护上下文窗口） |
| 主动性 | Heartbeat 模式：定时检查 checklist | 被动（用户查询为主） | Manager 定时巡检 + 主动通知 |
| 状态 | append-only 事件日志 + 自动压缩 | SQLite + 13 状态机 | 保留 SQLite，简化状态 |

**不照搬的部分**：OpenClaw 是”个人助理”，不是 PR 生命周期编排器。状态持久化、CI 反馈闭环、安全 gate 等能力 OpenClaw 不提供，仍由我们自建。

**核心转变**：从 `Rules (大脑) → LLM (橡皮章) → Worker (手脚)` 转为 `LLM (大脑) → Rules (安全护栏) → Worker (自主执行)`。

---

## 4. 目标架构（最终形态，基于 OpenClaw 启发修订）

```
Human (Telegram NL + /commands)
        |
        v
Bot Gateway (薄层：消息路由 + 认证 + 频率限制)
        |
        v
Manager Agent (LLM with tools — 系统大脑)
  |-- tool: create_run(repo, task)
  |-- tool: get_run_status(run_id)
  |-- tool: get_global_stats()
  |-- tool: execute_worker(run_id, instructions)
  |-- tool: analyze_worker_output(run_id, event_stream)
  |-- tool: triage_review_comment(run_id, comment_body)
  |-- tool: suggest_retry_strategy(run_id, diagnosis)
  |-- tool: notify_user(message, priority)
  |-- tool: propose_iteration(run_id, patch_draft)
        |
        v (安全约束层，不是决策层)
Orchestrator (薄层：状态持久化 + gate 执法 + 事件日志)
  |-- 硬约束：PR 创建需人工确认
  |-- 硬约束：merge 永远人工
  |-- 硬约束：diff budget / retry 上限
  |-- 硬约束：sandbox 模式
        |
        v
Worker Agent (codex exec — 自主执行，内部管理阶段)
  |-- 内部 skill-1: 分析仓库 (preflight contract)
  |-- 内部 skill-2: 实现 + 验证
  |-- 内部 skill-3: CI/review 修复 (可选，按需)
  |-- Worker 自己决定阶段流转和 skill 调用顺序
        |
        v
Repo workspace + GitHub
```

### 4.1 角色边界（修订版）

1. **Manager Agent**：真正的 LLM 大脑。做决策、分析、策略生成、主动通知。通过 tools 与 Orchestrator 交互，不直接改代码。
2. **Orchestrator**：薄层基础设施。做状态持久化、gate 执法、事件日志。不做决策。
3. **Worker**：自主执行。在一次任务中内部管理多阶段（分析→实现→验证），自行调用 skills。Orchestrator 只看最终结果。

### 4.2 与旧架构的核心差异

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| 大脑 | Rules engine (`manager_decision.py`) | Manager LLM (带 tools 的 agent) |
| LLM 角色 | 从 N 选 1 的橡皮章 | 真正的决策者（分析、策略、解释） |
| 状态机 | 13 个状态，外部微管理 worker 阶段 | 7 个状态，worker 内部管理子阶段 |
| Skills | Orchestrator 注入到 prompt | Worker 自主调用，保护上下文窗口 |
| 失败分析 | 1,400 行 regex | 混合：Rules 提取证据 + 硬护栏，LLM 做语义诊断 + 策略 |
| 通知 | 被动（用户查询） | 主动（Manager 判断何时通知） |
| 硬约束 | 分散在 rules/analysis/policy 各处 | 集中在 Orchestrator 薄层 |

### 4.3 目标状态机（简化版）

```
QUEUED → EXECUTING → PUSHED → CI_WAIT → REVIEW_WAIT → DONE
                                  ↕           ↕
                              ITERATING ← ─ ─ ┘
+ PAUSED (任何非终态可暂停)
+ NEEDS_HUMAN (升级人工)
+ FAILED (终态)
```

8 个状态（当前 13 个）。

**合并依据：**
- DISCOVERY + PLAN_READY + IMPLEMENTING + LOCAL_VALIDATING → **EXECUTING**（worker 内部管理分析→实现→验证子阶段）
- FAILED_RETRYABLE + FAILED_TERMINAL → **FAILED**（重试决策由 Manager Agent 判断，不需要两个失败状态）
- SKIPPED 合并到 FAILED（metadata 区分 `reason=skipped`）

**保留的外部生命周期状态（不能合并）：**
- PUSHED / CI_WAIT / REVIEW_WAIT / ITERATING — 对应不同的外部交互阶段
- PAUSED / NEEDS_HUMAN — 人工控制点

**迁移策略（旧 run 兼容）：**
1. Phase C3 执行时，新旧状态并存期：旧 run 继续使用 13 状态，新 run 使用 8 状态。
2. DB 层通过 `schema_version` 字段区分新旧 run。
3. 状态映射表用于旧 run 的只读查询：`DISCOVERY|PLAN_READY|IMPLEMENTING|LOCAL_VALIDATING → EXECUTING`，`FAILED_RETRYABLE|FAILED_TERMINAL|SKIPPED → FAILED`。
4. 不做旧 run 的批量迁移——旧 run 只读冻结，新 run 用新状态。

### 4.4 Contract 的定位修订

**Contract 是什么**：Skill-1（分析仓库）的输出。描述要改哪些文件、跑什么测试、遵守什么规则。

**当前问题**：
1. 实际产出大多是 `status: bootstrap` 的空壳（worker 分析阶段常遇环境问题）。
2. PLAN_READY 作为人工审核 contract 的 gate——在新架构中没有意义。

**修订后的定位**：
1. Contract 保留为 skill-1 的内部产物（指导 skill-2 执行）。
2. 不再作为 orchestrator 的外部 gate。
3. Worker 在 EXECUTING 内部：skill-1 产出 contract → 如有 blocker 则停止报告 NEEDS_HUMAN → 无 blocker 则直接进 skill-2。
4. 人不需要审核 contract，只在 worker 报告 blocker 时介入。

### 4.5 Preflight 的定位确认

**事实**：Preflight 检查完全是通用的（自动检测 Python/JS 项目类型和工具链），无 repo-specific 硬编码。

**在新架构中的位置**：
1. Preflight 仍在 worker 执行之前运行（环境健康检查）。
2. 但不需要作为独立的顶层状态——它是 EXECUTING 的前置检查。
3. Preflight 失败 → EXECUTING 状态直接报告 NEEDS_HUMAN 或 FAILED。

---

## 5. 为什么 Manager 用 API，而不是 codex exec

1. Manager 的输入是结构化状态（run snapshot / digest / policy），不是代码库全文。
2. Manager 的输出是工具调用（action），不是 diff。
3. Manager 需要低延迟、低成本、可控 schema；API function-calling 更合适。
4. Worker 继续用 codex exec，不改。

标准做法：
1. Manager 使用 Forge 或 OpenAI/Anthropic API（function calling + structured output）。
2. Worker 使用 `run-agent-step`（codex exec + sandbox/policy）。

---

## 5.1 旧版 / 当前版 / 目标版（差异与后果）

旧版（人工主驱动）：
1. 你手动敲 CLI + 手动看状态 + 手动决定下一步。
2. 优点是可控；缺点是你就是吞吐瓶颈。

当前版（已落地）：
1. 有 manager loop、state machine、gates、NL 路由、decision card。
2. 优点是稳定自动推进显著提升；缺点是 manager “智能运营感”仍偏弱（更多是安全调度）。

目标版（你要的）：
1. 人只对话与审批；manager 主动汇报“现在在哪一步、下一步是什么、你需要做什么决策”。
2. 支持评论驱动二次修复闭环、run 级统计、按收益触发迭代提案。

当前风险：
1. 若过度放权给 LLM（直接执行任意 shell），会提高误动作和不可回放风险。
2. 若完全规则化，又会失去你要的“智能经理”能力。

结论：
1. 采用中间态最优：`LLM 决策 + 硬约束执行 + 人工 gate`。

---

## 6. Bot 交互模型（命令 + 自然语言双模）

### 6.1 保留 CLI 风格命令

1. `/list`
2. `/show <run_id>`
3. `/status <run_id>`
4. `/pause <run_id>`
5. `/resume <run_id> <state>`
6. `/retry <run_id> <state>`
7. `/approve_pr <run_id> <token>`

### 6.2 增加自然语言路由

示例：
1. “帮我在 mem0 跑一轮 forge integration，并用最小改动策略。”
2. “dexter 当前卡在哪？给我下一步建议。”
3. “把这个 run 暂停，等我晚上再继续。”

NL 请求流程：
1. Bot 收到文本。
2. Manager LLM 解析意图 + 参数。
3. Manager 产出结构化 action（schema 校验）。
4. Orchestrator 执行动作并返回结果。
5. Manager 生成简明反馈给用户。

---

## 7. Manager Action Contract（当前 → 目标）

### 7.1 当前动作集（与 13 状态机对应，Phase C1/C2 期间仍在使用）

1. `list_runs(limit)`
2. `show_run(run_id)`
3. `create_run(owner, repo, prompt_version, mode)`
4. `start_discovery(run_id)`
5. `run_prepare(run_id)`
6. `mark_plan_ready(run_id, contract_path)`
7. `start_implementation(run_id)`
8. `run_agent_step(run_id, prompt_key, skills_mode)`
9. `mark_local_validated(run_id)`
10. `run_finish(run_id, changes, commit_title)`
11. `request_open_pr(run_id, title, body_file)`
12. `approve_open_pr(run_id, request_file, confirm_token, confirm=true)`
13. `pause_run(run_id)`
14. `resume_run(run_id, target_state)`
15. `retry_run(run_id, target_state)`

### 7.2 目标动作集（与 8 状态机对应，Phase C3 完成后切换）

1. `list_runs(limit)`
2. `show_run(run_id)`
3. `create_run(owner, repo, task_description)`
4. `execute_worker(run_id, instructions)` — 替代 start_discovery/run_prepare/start_implementation/run_agent_step
5. `analyze_worker_output(run_id)` — 混合分级：rules 证据提取 + LLM 语义判断（替代纯 regex 分级）
6. `run_finish(run_id, changes, commit_title)`
7. `request_open_pr(run_id, title, body_file)`
8. `approve_open_pr(run_id, request_file, confirm_token, confirm=true)`
9. `pause_run(run_id)`
10. `resume_run(run_id, target_state)`
11. `retry_run(run_id, strategy)` — strategy 由 LLM 生成，不是固定重置
12. `get_global_stats()` — 新增
13. `notify_user(message, priority)` — 新增
14. `suggest_retry_strategy(run_id, diagnosis)` — 新增

### 7.3 迁移策略

Phase C2 期间新旧动作集并行（A/B 切换）。C3 完成后旧动作集退役。

约束（始终有效）：
1. Manager 不能调用任意 shell。
2. 只能调用白名单 action。
3. action 参数必须通过 JSON schema 验证。

---

## 8. 自我迭代闭环（manager 主导）

输入源：
1. `run_digest`（机器真值）
2. `manager_insight`（解释层）
3. `skills-metrics` / `skills-feedback`
4. CI/review 反馈

输出目标：
1. prompt 迭代建议（不直接盲改）
2. skill 迭代建议
3. policy 迭代建议（timeout/diff/retry/allowlist）

落地原则：
1. 默认“建议->审批->应用”。
2. 仅允许 manager 自动改写 `orchestrator/data/prompts/*`、`orchestrator/data/contracts/*`、`skills/*` 的候选草案。
3. 核心策略文件仍需人工审批合并。
4. 迭代不是每次 run 都触发；由 manager 基于收益判断触发（失败模式重复、成本异常、回归风险上升时才提案）。

---

## 9. 安全与隔离（与真实使用场景对齐）

当前策略：
1. Worker 写权限限定在 repo + `.agentpr_runtime` + `/tmp`。
2. 仓库外写入禁用。
3. 仓库外读取仅允许白名单。
4. `PUSHED -> open PR` 必须人工双确认。

判断：
1. 这套策略对“本地单人运营 + 多 OSS 仓库”是合理的。
2. 若将来要多人共享，必须升级为主机级隔离（每用户独立 runtime/凭据/审计）。

---

## 10. 运行循环（manager 常驻）

推荐节奏：
1. 事件驱动优先：webhook 到达立即处理。
2. 定时巡检兜底：每 5-10 分钟一次（非高频轮询）。
3. LLM 调用仅在“需要决策”时触发。
4. 当前执行模型为 queue 串行（单 manager loop），先保证稳定闭环，再考虑并发 worker 池。

每轮巡检做什么：
1. 拉取 pending runs。
2. 对每个 run 读取 state + latest digest。
3. 调用 manager 决策（规则优先，可选 LLM）。
4. 执行动作或升级人工。
5. 记录 decision trace。

---

## 10.1 LLM 能力边界（细化版，基于代码审计）

### 应使用 LLM（当前严重不足的高价值场景）

| 场景 | 当前做法 | 目标做法 | 价值 |
|------|----------|----------|------|
| **失败原因分析** | runtime_analysis.py 1,400行 regex 分类 | **混合**：Rules 提取客观证据 + 硬护栏；LLM 读证据包做语义诊断和策略建议 | Rules 保证可审计，LLM 提供语义理解 |
| **重试策略生成** | 硬编码 retry→DISCOVERY 或 IMPLEMENTING | LLM 基于失败原因，生成修改后的 prompt 或建议 | 智能重试而非盲重试 |
| **Decision Card 的 why** | 机器规则文本（`reason_code` 直译） | LLM 用 2-3 句话解释”为什么到这一步，你需要做什么” | 人可读的运营建议 |
| **Review comment 处理** | webhook→ITERATING，盲目重跑 worker | LLM 读评论内容，判断：改代码？回复解释？忽略 nitpick？ | 智能分流 |
| **Contract 质量判断** | 仅检查字段是否存在 | LLM 判断 contract 与 repo 实际结构是否匹配 | 减少 bootstrap 合约导致的空跑 |
| **全局运营汇报** | 无 | LLM 综合所有 run 状态，输出优先级排序和行动建议 | 你要的”智能经理”体验 |
| **NL → action 路由** | 已有，基本可用 | 保持，可接入更丰富的上下文 | 已落地 |

### 不应使用 LLM（当前做法正确，保持）

| 场景 | 原因 |
|------|------|
| 状态转移与合法性校验 | 必须确定性、可审计、可回放 |
| Gate 执法（PR DoD、确认 token、ACL） | 安全边界不能交给概率模型 |
| 安全隔离（sandbox 模式、文件权限） | 必须硬约束 |
| 事件去重、重放、审计落盘 | 幂等性要求 |
| 重试上限、diff 预算上限 | 防止无限循环烧钱（上限值本身是硬规则） |
| Webhook 签名校验 | 密码学确定性 |

### 边界案例（需要”LLM 判断 + 硬约束兜底”）

| 场景 | 处理方式 |
|------|----------|
| runtime grading（PASS/RETRYABLE/HUMAN_REVIEW） | **混合策略**：Rules 提取证据包（test_commands, diff_stats, exit_code, has_test_infra 等），LLM 基于证据包 + 固定评分标准做语义分级。硬护栏（sandbox 违规、diff 超限）由 rules 强制执行，LLM 不可覆盖 |
| diff 合理性（语义层面） | LLM 判断改动是否符合意图，但 diff budget 上限仍硬执行 |
| 是否需要人工介入 | LLM 建议，但 PUSHED/NEEDS_HUMAN_REVIEW gate 仍硬执行 |

### Decision Card 生成原则（更新）

1. `what/decision/evidence` 必须是机器事实（deterministic）。
2. `why_explained` 应由 LLM 生成：基于 evidence 给出可操作的解释（不是复述 reason_code）。
3. `suggested_actions` 应由 LLM 提供：具体的下一步选项（不是泛化的”human review”）。
4. 对外显示双层：`why_machine`（机器事实） + `why_llm`（智能解释 + 建议）。

---

## 11. 分阶段实施计划（修订版：B 系列已完成，进入 C 系列）

### 已完成回顾

- **Phase B1** ✅：规则版 manager loop + 自动推进。
- **Phase B2** ✅：Manager LLM function-calling 适配层（rules/llm/hybrid）。
- **Phase B3** ✅：Telegram NL 路由 + 会话绑定。
- **Phase B4**：部分完成（skills-feedback 有产物，但闭环未接通）。

### Phase C 系列（架构转型）

## Phase C1：跑通第一个真实 PR（最高优先级，不改架构）

1. 用现有架构，选一个 baseline repo（mem0 或 dexter），完整跑通 QUEUED → PUSHED → PR 创建 → CI 通过。
2. 记录全流程的阻塞点、手动干预点、失败模式。
3. 基于真实数据建立基线指标：成功率、平均 attempt 数、平均耗时、常见失败 reason_code。

验收：至少 1 个 PR 被成功创建并通过 CI review。

为什么先做这个：不用真实数据验证，所有架构调整都是猜测。

## Phase C2：引入 Manager Agent（LLM with tools）

1. 新建 `manager_agent.py`：Manager 成为真正的 LLM agent，拥有结构化 tools。
2. 保留现有 rules 作为 fallback（新旧并行，A/B 切换）。
3. Manager Agent tools 最小集：
   - `create_run`、`get_run_status`、`execute_worker`
   - `analyze_worker_output`（混合分级：rules 提取证据 + LLM 做语义判断，替代纯 regex 分级）
   - `get_global_stats`（全局运营看板）
   - `notify_user`（主动通知）
4. 硬约束仍在 Orchestrator 层执行，Manager Agent 无法绕过。

验收：
1. Manager Agent 能自主推进 run 到 PUSHED，决策质量 >= rules 版。
2. 失败时能给出比 reason_code 更有价值的诊断和建议。

## Phase C3：Worker 自主 skill 调用 + 状态机简化

1. 重构 worker prompt：worker 在一次 codex exec 中自主管理多阶段（分析→实现→验证）。
2. Skills 从”orchestrator 外部注入”改为”worker 内部可调用”，每个 skill 保护独立上下文。
3. 合并 DISCOVERY/PLAN_READY/IMPLEMENTING 为 EXECUTING 状态。
4. 状态机从 13 → 7 个状态。

验收：
1. 单次 codex exec 调用能完成”分析+实现+测试”全流程。
2. 状态机简化后，现有 gate/通知/GitHub 集成不受影响。

## Phase C4：瘦身 + 运营闭环

1. 删除被 Manager Agent 替代的 rules 逻辑（`manager_decision.py` 中的确定性映射）。
2. 简化 runtime_analysis.py：保留证据提取 + 硬护栏，分级判断由 Manager Agent LLM 层承担（混合策略，非全替换）。
3. 接通迭代闭环：Manager Agent 基于失败模式生成 prompt/policy 改进草案。
4. 全局统计看板产品化。

验收：
1. orchestrator 代码量显著下降（目标 < 8K 行）。
2. 每次 run 结束，Manager 能给出可执行的改进建议。

### 11.1 Skills 设计修订

**原始设计意图（恢复）：**
Worker 在一次任务执行中，自主决定调用哪个 skill：
```
Worker 收到任务 “integrate Forge into mem0”
  → Worker 调用 skill-1: 分析仓库结构 + 生成 contract
  → 基于分析结果，Worker 调用 skill-2: 实现代码 + 跑测试
  → 如果测试失败，Worker 可选调用 skill-3: 修复
  → 每个 skill 是独立上下文，保护窗口
```

**当前实现偏差：**
Orchestrator 外部控制 skill 注入（按状态映射 skill → 构建 task packet → 注入 prompt），Worker 没有选择权。

**修订方向：**
- Skill 仍作为 markdown 定义（保留可读性和可编辑性）。
- 安装到 worker 可访问的路径（保持）。
- 但调用决策权还给 worker：worker prompt 中列出可用 skills + 使用条件，worker 自主判断何时调用。
- Orchestrator 不再按状态注入 skill，只传递任务描述 + 安全约束。

---

## 12. 当前完成度（截至 2026-02-25，含架构审计）

已完成：
1. Orchestrator 核心：状态机、事件、幂等、SQLite。
2. Worker 执行链：`run-preflight` + `run-agent-step` + runtime grading。
3. Skills 链：`agentpr` skills 已接入。
4. PR gate：`request-open-pr` + `approve-open-pr --confirm` + DoD。
5. Bot 命令：`/create /overview /list /show /status /pause /resume /retry /approve_pr`。
6. Webhook/轮询/审计与告警模板。
7. `skills-metrics` + `skills-feedback`。
8. Phase B1 规则版 manager loop 已落地：`manager-tick` / `run-manager-loop`。
9. Bot 双模路由已落地：`/` 开头按命令执行，非 `/` 文本按自然语言 intent 路由；每次回复附带固定 rules 尾注。
10. Manager 决策模式已扩展：`rules|llm|hybrid`（OpenAI-compatible function-calling，支持 Forge 网关）。
11. Bot 自然语言已接入 Manager LLM 路由（`rules|hybrid|llm`），并支持会话级 `run_id` 绑定（内存态）。
12. CLI 支持自动加载项目根目录 `.env`；新增 `.env.example` 作为标准环境模板。
13. runtime grading 已升级为“最终收敛优先”：中间测试失败转证据，不再默认硬阻塞；PR gate 同步支持该语义（保留 warning）。
14. Bot `/show|/status` 已升级为细粒度 Decision Card（why/evidence/动作建议/人工决策入口）。
15. Manager LLM 决策上下文已接入精简 `run_digest` 证据摘要（classification/validation/diff/attempt/recommendation）。
16. Bot 已具备关键状态主动通知（`PUSHED`/`NEEDS_HUMAN_REVIEW`/`DONE`/GitHub 反馈触发的 `ITERATING`），并做去重落盘。

未完成（按架构审计优先级重排）：
1. **LLM 在正确位置发挥智能**（最高优先级）：
   - runtime grading 引入 LLM 判断层（混合策略：rules 提取证据 + 硬护栏，LLM 做语义分级，替代纯 regex 分类）。
   - Decision Card `why_llm` 增强层（可操作解释 + 具体建议）。
   - review comment 智能分流（读评论内容，判断改代码/回复/忽略）。
   - 重试策略 LLM 化（基于失败诊断生成修改过的 prompt）。
2. **控制面简化**（降低维护成本）：
   - 评估状态机精简（13 → 6-7 状态）。
   - 评估 Skills 系统必要性（是否可用 prompt template 替代）。
3. **运营体验**：
   - 全局运营看板（通过数/等待PR数/阻塞分布/失败率）。
   - 主动通知策略调优。
   - bot 会话上下文持久化。
4. **闭环迭代**：
   - `skills-feedback` → prompt/policy patch 草案。
   - bot 审计日志结构化。

---

## 12.1 系统级对照（按你的目标验收）

目标能力 A：你用自然语言下发任务，manager 自动完成到 `PUSHED/PR gate`  
1. 当前状态：部分达成。NL 已可触发 `create_runs`、`manager_tick`；自动推进依赖常驻 loop。  
2. 缺口：需要把“bot 收到 create 后持续跟踪并主动汇报”固化为常驻行为（不是一次性命令响应）。

目标能力 B：你问“现在什么情况”，系统能给全局态势 + 下一步  
1. 当前状态：部分达成。`/overview` + `/show` 已有状态与决策卡。  
2. 缺口：缺全局 KPI（通过数/等待 PR 数/阻塞分布/近 24h 失败率）与“下一步优先级队列”。

目标能力 C：run 结束/失败/需审批时，系统主动通知你  
1. 当前状态：未完全达成。现在主要是你主动查询。  
2. 缺口：缺“状态变更触发通知策略”和通知去重（避免刷屏）。

目标能力 D：PR review comments 到来后，系统主动问你“人工介入还是自动修复”  
1. 当前状态：基础具备。Webhook/Sync 能把状态推进到 `ITERATING`。  
2. 缺口：缺 bot 主动决策对话层（通知 + 可选动作 + 一键执行）。

目标能力 E：manager 按收益判断是否做 prompt/skill 自我迭代，并交你审批  
1. 当前状态：基础具备。已有 `skills-metrics/skills-feedback`。  
2. 缺口：缺“自动提案 -> 审批 -> 应用”的闭环执行器与审批历史追踪。

系统结论：
1. 架构方向正确，且和你的最终目标一致。
2. 当前短板不是 worker 基础能力，而是 manager 的”主动运营层”尚未完全产品化。
3. **架构审计补充判断**：更深层的短板是”复杂度放错了地方”——12K 行中大部分在确定性控制，而真正需要智能的场景（失败分析、review 处理、运营汇报）仍是空白或规则驱动。下一阶段应优先让 LLM 在正确的位置发挥能力，而非继续堆控制逻辑。

---

## 13. 决策清单（锁定）

1. Python 固定 `3.11`。
2. Worker 固定 `codex exec`。
3. baseline 仓库固定 `mem0` 与 `dexter`。
4. 默认模式 `push_only`。
5. `merge` 永远人工。
6. `create PR` 必须二次确认。
7. Manager 默认走 API function-calling（Forge provider 优先）。

---

## 14. 对话沉淀 Insights（持续更新）

**早期实践（保留）：**
1. 先解决主矛盾：闭环决策，不是堆框架。
2. “控制面稳定 > 执行面花哨”。
3. 最小改动能力来自：prompt + policy + gate 的协同，而不是单次模型能力。
4. 运行成功率的核心是环境与规则证据，不是”更强模型名”。
5. 可观测要分层：日常看 digest，失败看 event stream。

**架构审计后新增（2026-02-25）：**
6. 系统应该简单，但不应过于简单。度的把握：安全和持久化不能简化，决策逻辑应交给 LLM。
7. “精密工厂管理聪明工人”是反模式。应该是”轻量生产线 + 自主工人 + 关键检查点”。
8. 复杂度应该投资在”智能”上（失败分析、策略生成、运营汇报），而不是在”控制”上（每个状态的合法动作列表）。
9. OpenClaw 的核心启示：LLM 应该是大脑（with tools），不是规则引擎的附属品。
10. Skills 的正确用法是 worker 自主调用（保护上下文），不是 orchestrator 外部注入。
11. 如果做的不对，再大的代价也是最小的代价。先用真实数据验证，再做架构调整。
12. 另一个 LLM 的合理反馈：不要为改架构而改架构，先跑通第一个 PR 再说。基线数据是一切决策的基础。
13. Contract（skill-1 产出的施工合同）概念正确，但不应作为人工审核 gate。Worker 内部产出、内部消费。有 blocker 时 worker 自己停止报告。
14. Preflight 检查是通用的（自动检测项目类型和工具链），对任何新 repo 都适用。
15. 状态机的复杂度来自真实问题（环境失败、异步 CI、不同失败类型），不是凭空设计。但部分状态可以内化到 worker（分析/计划/实现）或 Manager Agent（重试策略）。
16. C1 测试验证了两个关键判断：(a) `min_test_commands` 硬性要求不适用于所有项目——DeepCode 没有测试基础设施，pre-commit 已是最佳验证；(b) skill-1 作为独立外部产物无增量价值——worker 在同一次执行中做分析+实现效果更好。
17. 工具细节决定成败：rg 默认跳过 `.github/` 隐藏目录导致 worker 看不到 PR template。这类问题需要在 prompt 或 prepare 脚本中修复（加 `--hidden` 或用 `find`）。
18. runtime 分级的正确策略是混合：Rules 做证据提取 + 硬护栏（不可被 LLM 覆盖），LLM 做语义判断（基于固定评分标准）。不是全替换 regex，是分层。

---

## 15. 下一步（直接执行清单）

**立即执行（C1 迭代 — 修复已知问题后再跑）：**
1. ~~选 mem0，用现有架构完整跑通一个 PR~~（已完成首次测试：HKUDS/DeepCode，结果 NEEDS_HUMAN_REVIEW/missing_test_evidence）
2. 修复 rg 隐藏目录问题：worker prompt 或 prepare 脚本中的 `rg --files -g '.github/...'` 加 `--hidden`，或改用 `find`。
3. 选第 2 个 repo 再跑一次，验证修复效果 + 收集跨 repo 数据。
4. 基于 2+ 次真实数据建立基线。

**C1 稳定后（Phase C2 — 混合分级优先）：**
5. 构建 `analyze_worker_output` 混合分级：rules 提取证据包 + LLM 按固定评分标准做语义分级。
6. 构建 Manager Agent（LLM with tools），与现有 rules 并行运行（A/B 切换）。
7. 首批 tools：`analyze_worker_output`、`get_global_stats`、`notify_user`。
8. Decision Card 接入 `why_llm`。

**C2 验证后（Phase C3）：**
9. Worker 自主 skill 调用改造。
10. 状态机简化（13 → 8）+ 迁移策略。

**长期（Phase C4）：**
11. 瘦身：删除被 Manager Agent 替代的纯 regex 分级代码（保留证据提取 + 硬护栏）。
12. 迭代闭环产品化。
13. 全局运营看板。

---

## 16. C1 测试记录（2026-02-26）

### 16.1 测试运行

| 项 | 值 |
|-----|-----|
| Run ID | `run_2e642ed9c2f2` |
| 目标 Repo | HKUDS/DeepCode |
| 最终状态 | `NEEDS_HUMAN_REVIEW` |
| 原因 | `missing_test_evidence` |
| 持续时间 | 369s (~6min) |
| Worker 退出码 | 0 |
| 代码改动 | 4 files, +48/-10 |
| Token 消耗 | 2.48M input, 18.7K output |
| Skills 模式 | `off` |
| 执行方式 | CLI (`create-run` + `run-manager-loop`) |

### 16.2 Worker 实际表现

**做得好的：**
- Fork + clone + branch 创建 ✅
- Preflight 通过 (119ms) ✅
- 正确分析了 DeepCode 的 LLM 架构，判断走 OpenAI 兼容路径 ✅
- Forge 集成方案合理（env fallback + base_url，不新建 provider class） ✅
- 文档和配置示例都更新了 ✅
- pre-commit hooks 全部通过 ✅

**做得不好的：**
- 搜索 `.github/pull_request_template.md` 时用了 `rg --files -g '.github/pull_request_template*'`，但 rg 默认跳过隐藏目录 → 没找到 PR template → 不知道 checklist 要求
- 没有尝试运行项目测试（但 DeepCode 实际上没有测试基础设施，所以这不影响结果正确性）

### 16.3 暴露的系统问题

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **rg 不搜索 `.github/`** | 高 | Worker prompt 引导用 `rg --files -g '.github/...'`，但 rg 默认排除隐藏目录。需加 `--hidden` 或改用 `find` |
| **`min_test_commands: 1` 太刚性** | 高 | DeepCode 没有测试基础设施（无 tests/、无 pytest 依赖、CI 只跑 lint），但系统强制要求至少 1 个测试命令。正确做法：混合分级 |
| **通知未触发** | 低 | CLI 测试模式不启动 bot 守护进程，所以停在 NEEDS_HUMAN_REVIEW 时无通知。生产模式下 bot 会触发通知（已确认代码逻辑正确） |

### 16.4 Skill-1（检查阶段）对 Skill-2（实现阶段）的实际帮助评估

本次 skills_mode=off，Worker 在一次执行中自主完成分析 + 实现。观察：

1. **分析思路有价值**：Worker 按 prompt_template.md Phase 0.5 + Phase 1 做了分析，正确判断了 common path（OpenAI 兼容）vs special path，找到最相似 provider 模式。
2. **但 skill-1 作为独立产物（contract）的增量价值 ≈ 0**：之前 smoke test 的 contract 输出都是 `status: bootstrap` 空壳，没有给 skill-2 提供有用的结构化信息。
3. **上下文连续性更重要**：Worker 在同一次执行中做分析和实现，分析结果直接在上下文中被实现阶段使用，不需要外部 contract 中转。

**结论**：验证了 skill-1 → skill-2 应是 worker 内部连续调用（保护上下文窗口），不是 orchestrator 外部分阶段管理。Contract 可以是 worker 内部笔记，不需要作为外部 gate。

### 16.5 混合分级策略（C1 驱动的设计确认）

基于 C1 测试暴露的 `min_test_commands` 问题，确认分级策略：

**Rules 层（确定性，不可被 LLM 覆盖）：**
- 证据提取：test_commands、lint_commands、exit_code、diff_stats、has_test_directory、has_test_dependencies、ci_workflows
- 硬护栏：max_changed_files、max_added_lines、sandbox 违规、已知安全模式

**LLM 层（语义判断，基于固定评分标准）：**

输入：Rules 层的证据包 + worker 最终消息

固定评分标准（prompt 内置，不因 worker 输出变化）：
1. 项目是否有测试基础设施？（tests/ dir + test deps + test CI workflow）
2. 如果有 → worker 是否执行了对应测试？
3. 如果没有 → worker 是否做了合理替代验证？（lint, pre-commit, type check）
4. 改动范围与风险等级是否匹配？（文档改动 vs 核心逻辑改动）
5. PR template 要求是否满足？（需要 worker 能找到 PR template — 修复 rg 问题）
6. Worker 自评与实际证据是否一致？

输出：PASS / NEEDS_REVIEW / FAIL + 原因说明

---

## 17. 架构审计记录（2026-02-25 全量代码审查）

### 17.1 代码量分布

| 文件 | 行数 | 角色 |
|------|------|------|
| cli.py | 4,093 | CLI 命令入口（15+ 命令） |
| telegram_bot.py | 1,904 | Bot 双模控制面 |
| runtime_analysis.py | 1,423 | 运行分级与证据提取 |
| service.py | 606 | 事件与状态服务 |
| db.py | 578 | SQLite 持久化 |
| manager_loop.py | 558 | 自动推进循环 |
| github_webhook.py | 539 | Webhook 接收 |
| manager_llm.py | 512 | LLM 客户端 |
| executor.py | 488 | Worker 执行器 |
| preflight.py | 434 | 环境预检 |
| manager_policy.py | 360 | 策略加载与合并 |
| skills.py | 316 | 技能发现与 task packet |
| manager_decision.py | 176 | 规则决策引擎 |
| state_machine.py | 142 | 状态转移图 |
| github_sync.py | 113 | GitHub 状态同步 |
| models.py | 86 | 数据模型 |
| **orchestrator 合计** | **12,385** | |
| **全项目合计** | **~16,600** | 含 forge_integration、skills、deploy |

### 17.2 核心发现

**复杂度分配：**
- 确定性控制（状态机 + 规则 + regex + policy）：~70% 代码量
- LLM 智能层：~5% 代码量（manager_llm.py 仅做"从 N 选 1"）
- 基础设施（DB + CLI + Bot + webhook）：~25% 代码量

**关键判断：**

1. **Manager LLM 增量价值极低**：`manager_decision.py` 的 rules 已 100% 覆盖所有 state→action 映射。每个状态几乎只有 1 个合法动作 + `WAIT_HUMAN`。LLM 在此处的决策空间 ≈ 0。
2. **runtime_analysis.py 需要混合策略**：1,400 行 regex 做失败分类过于刚性（如 `min_test_commands` 无法处理"项目没有测试基础设施"的情况）。正确做法：Rules 保留证据提取 + 硬护栏（sandbox、diff budget），LLM 层做语义分级和策略建议。不是全替换，是分层。
3. **13 个状态过多**：DISCOVERY → PLAN_READY → IMPLEMENTING 本质是 worker 执行的子阶段，不需要顶层状态机介入。Manager 在这三个状态的决策都是"继续推进"。
4. **Skills 是一层间接但非必要的抽象**：316 行代码的核心价值 = 在不同阶段给 worker 不同 prompt 片段，直接在 prompt template 中实现更简单。
5. **与"直接让 codex 全接管"的对比**：codex 单独做不了状态持久化、PR 生命周期、CI 反馈闭环、安全 gate——这些是 orchestrator 的真正价值。但 orchestrator 当前过度微管理 worker 的判断（`min_test_commands`、`max_changed_files`），worker 自身比 regex 更能判断"改动是否合理"。

### 17.3 与外部系统对比

| 系统 | 架构模式 | 对 AgentPR 的启示 |
|------|----------|-------------------|
| GitHub Copilot coding agent | 薄编排 + 强 agent + PR gate | agent 自主度高，编排层主要管 PR 生命周期 |
| OpenHands / SWE-agent | issue→agent→PR，编排层很薄 | 证明了"LLM agent + 简单生命周期"可以工作 |
| OpenClaw | 对话网关 + 个人助理 | "LLM 在对话中心"的模式值得借鉴 |
| Devin | 厚编排 + 厚 agent | 走全包路线，不适合轻量级 OSS 场景 |

**结论**：行业趋势是"编排层做生命周期管理和安全约束，智能决策交给 LLM"。AgentPR 当前反过来了——编排层承担了太多决策逻辑。

### 17.4 整体评价

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构方向 | 正确 | manager-worker 分离、人工 gate、push_only |
| 工程量 | 偏重 | 12K 行对于未产出首个合并 PR 的系统 |
| LLM 使用 | 严重不足 | LLM 只做选择题，需要智能的地方用 regex |
| 安全设计 | 恰当 | sandbox + gate + 审计是必要的 |
| 抽象层数 | 过多 | 5 层 prompt 构建链过重 |
| 核心矛盾 | 复杂度放错地方 | 应简化控制面、强化智能面 |

**一句话**：精密的工厂生产线管理一个有自主判断力的工人——应该反过来：轻量生产线 + 更多 worker 自主权 + 关键检查点硬约束。

---

## 18. 参考资料（用于架构校准）

1. OpenAI Function Calling：<https://platform.openai.com/docs/guides/function-calling>
2. OpenAI Structured Outputs：<https://platform.openai.com/docs/guides/structured-outputs>
3. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
4. OpenHands 文档（GitHub 集成与 agent 调用）：<https://docs.all-hands.dev/modules/usage/how-to/github-action>
5. GitHub Copilot coding agent（后台任务、会话日志、PR 流程）：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
6. Temporal 文档（durable long-running workflows）：<https://temporal.io/platform>
7. LangGraph 文档（agentic workflow 图）：<https://docs.langchain.com/oss/python/langgraph/overview>
8. OpenClaw Security（one-user trusted operator 边界）：<https://raw.githubusercontent.com/openclaw/openclaw/main/SECURITY.md>
9. SWE-agent（薄编排 + 强 agent PR 工作流）：<https://github.com/SWE-agent/SWE-agent>
