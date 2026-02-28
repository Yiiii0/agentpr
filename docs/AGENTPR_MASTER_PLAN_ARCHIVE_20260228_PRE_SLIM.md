# AgentPR Master Plan (Manager-Worker Final Target)

> 更新时间：2026-02-27（C4 瘦身完成）
> 状态：C2 + C3 + C4 完成。P1 + P2 已完成。C4 瘦身已执行：cli.py 拆分（4,628→2,768 + 4 submodules）、runtime_analysis.py 精简（1,746→1,712）、telegram_bot.py 扁平化（2,055→1,573 + helpers）。下一步：P0 第二轮真实验证。
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
5. **复杂度分配改善中**：~15.2K 行 orchestrator 代码（C4 拆分后含子模块），~60% 是确定性控制（状态机、规则决策、regex 分析），~12% 是 LLM 智能层（语义分级 + confidence routing + 解释）。Manager LLM 已有实质性决策参与，但失败诊断、重试策略等高阶智能仍待补齐。三大文件（cli/runtime/telegram_bot）已拆分为更可维护的子模块结构。
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

### 应使用 LLM（高价值场景进度表）

| 场景 | 状态 | 当前做法 | 目标做法 |
|------|------|----------|----------|
| **语义分级** | ✅ 已完成 | `hybrid_llm` 模式：rules 证据 + LLM `grade_worker_output` → PASS/NEEDS_REVIEW/FAIL + confidence | — |
| **Confidence routing** | ✅ 已完成 | `ManagerRunFacts.latest_worker_confidence` → 低信心 PASS 升级人工 | — |
| **Decision Card why_llm** | ✅ 已完成 | `explain_decision_card` → 双层展示 why_machine + why_llm + suggested_actions | — |
| **NL → action 路由** | ✅ 已完成 | bot 双模路由 + manager LLM intent 解析 | — |
| **通知** | ✅ 已完成 | `notify_user` artifact → `maybe_emit_manager_notifications()` → Telegram 推送（含优先级标记） | — |
| **失败原因分析** | ✅ 已完成 | `suggest_retry_strategy` LLM 工具：分析失败证据，判断是否值得重试 + 目标状态 + 修改指令 | — |
| **重试策略生成** | ✅ 已完成 | `_diagnose_failure()` → `RetryStrategy` → `ManagerRunFacts.retry_should_retry/retry_target_state` → FAILED 决策分流 | — |
| **Review comment 处理** | ✅ 已完成 | `triage_review_comment` LLM 工具 → `_triage_iterating_review()` → fix_code/reply_explain/ignore → ITERATING 决策分流 | — |
| **全局运营汇报** | ✅ 已完成 | `get_global_stats` 接入 `/overview`：pass_rate、grade 分布、top reason_codes | — |

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

已完成（本轮新增，2026-02-26）：
19. V1 双轨代码删除：`StateSchemaVersion`、`_resolve_target_v1`、所有 V1/V2 分支逻辑清理（~250 行）。
20. LLM 解析函数合并：4 个 `_parse_*_from_response` → 1 个 `_extract_tool_call_payload`（-86 行）。
21. LLM 语义分级接入决策循环：`grade_worker_output()` → confidence → `ManagerRunFacts` → 低信心 PASS 升级人工，NEEDS_REVIEW 升级。
22. `notify_user()` 自动触发：RUN_FINISH / RETRY / action 失败后记录 `manager_notification` artifact。
23. Telegram 清理：删除死函数 `extract_repo_ref_from_text`、`default_execution_target_state` → `DEFAULT_TARGET_STATE` 常量。

未完成（按优先级重排，反映当前真实状态）：
1. **C1 第二轮真实验证**（最高优先级）：
   - 选第 2 个 repo 跑完整 QUEUED → PUSHED → PR 流程，验证 V2 状态机 + 混合分级 + review triage + retry strategy 在真实场景的表现。
   - 收集跨 repo 基线数据（成功率、attempt 数、耗时、失败分布）。
2. **C4 瘦身**（降低维护成本）：
   - `cli.py`（4,628 行）拆分为子模块。
   - `runtime_analysis.py`（1,746 行）：保留证据提取 + 硬护栏，分级判断迁移到 Manager LLM 层。
   - `telegram_bot.py` handler 扁平化。
   - 删除被 Manager Agent 替代的纯 rules 分级代码。
   - 目标：orchestrator < 10K 行。
3. **运营闭环**：
   - bot 会话上下文持久化（当前内存态）。
   - `skills-feedback` → prompt/policy patch 草案闭环。

---

## 12.1 系统级对照（按你的目标验收）

目标能力 A：你用自然语言下发任务，manager 自动完成到 `PUSHED/PR gate`
1. 当前状态：**大部分达成**。NL 触发 `create_runs` → manager loop 自动推进 → PUSHED/gate。V2 状态机简化了推进路径。
2. 缺口：常驻 loop 仍需手动启动（`run-manager-loop`），未做 systemd/cron 常驻化。

目标能力 B：你问”现在什么情况”，系统能给全局态势 + 下一步
1. 当前状态：**部分达成**。`/overview` + `/show` + Decision Card（双层 why_machine + why_llm）。`get_global_stats` 已实现（pass_rate、state_counts、top_reason_codes）。
2. 缺口：`get_global_stats` 未接入 bot `/overview` 展示。缺”下一步优先级队列”。

目标能力 C：run 结束/失败/需审批时，系统主动通知你
1. 当前状态：**基础达成**。Bot 有关键状态主动通知（PUSHED/NEEDS_HUMAN_REVIEW/DONE/ITERATING）+ 去重落盘。Manager loop 在 RUN_FINISH/RETRY/失败后自动记录 `manager_notification` artifact。
2. 缺口：`manager_notification` artifact 产生了但尚未推送到 Telegram（缺 artifact→bot 推送桥接）。

目标能力 D：PR review comments 到来后，系统主动问你”人工介入还是自动修复”
1. 当前状态：基础具备。Webhook/Sync → `ITERATING` 状态推进。
2. 缺口：缺 LLM review comment 分流（判断改代码/回复/忽略）和 bot 主动决策对话层。

目标能力 E：manager 按收益判断是否做 prompt/skill 自我迭代，并交你审批
1. 当前状态：基础具备。已有 `skills-metrics/skills-feedback`。
2. 缺口：缺”自动提案 → 审批 → 应用”闭环执行器。

系统结论（更新）：
1. 架构方向正确，C2+C3 显著推进了 manager 智能层。
2. **关键进展**：LLM 不再只是”橡皮章”——语义分级 + confidence routing 让 manager 能做出有依据的分流决策（高信心 PASS → push，低信心 → 升级人工，NEEDS_REVIEW → 等待）。这是从”规则驱动”到”智能辅助”的实质性一步。
3. **当前短板**：(a) 缺第二轮真实验证数据——所有改进仍基于 C1 单次 DeepCode 测试；(b) 代码量从 12K 涨到 14.5K（新增 C2 模块），C4 瘦身是实际需求而非美观追求；(c) 通知 artifact 产生了但最后一公里（推到 Telegram）未连通。
4. **复杂度分配已改善**：LLM 智能层从 ~5% 上升到 ~12%（manager_llm + manager_agent + manager_tools = 1,076 行），但确定性控制仍占 ~60%。C4 的目标是让这个比例更接近目标架构（控制面薄层 + 智能面厚层）。

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

**LLM 接入后新增（2026-02-26）：**
19. Confidence routing 是"LLM 辅助决策"的正确中间态：不是让 LLM 完全替代 rules，而是让 LLM 提供 confidence，rules 根据 confidence 做分流。安全兜底在 rules，语义理解在 LLM。
20. "先删后加"比"边加边删"安全：先清理 V1 双轨 → 再接通 LLM，避免在冗余代码上叠加新逻辑导致理解困难。
21. 代码量增长不等于膨胀：C2 是新增功能，增长正常。但超过 14K 行时维护成本明显上升，C4 瘦身是实际需求。
22. 通知"最后一公里"容易被忽略：产生 artifact 只是一半，推送到用户（Telegram）才是闭环。这类"看似完成实则断链"的问题需要端到端验证发现。
23. 每次真实测试 > 10 次代码审查。C1 一次 DeepCode 测试暴露的 rg 隐藏目录问题，比任何静态审查都有效。第二轮验证是当前最高优先级。

---

## 15. 下一步（直接执行清单）

**立即执行（C1 迭代 — 修复已知问题后再跑）：**
1. ~~选 mem0，用现有架构完整跑通一个 PR~~（已完成首次测试：HKUDS/DeepCode，结果 NEEDS_HUMAN_REVIEW/missing_test_evidence）
2. ~~修复 rg 隐藏目录问题：worker prompt 或 prepare 脚本中的 `rg --files -g '.github/...'` 加 `--hidden`，或改用 `find`~~（已实现：prompt_template 二次搜索统一使用 `--hidden` + `find .github` fallback；skills-mode task packet 新增 deterministic governance scan）
3. 选第 2 个 repo 再跑一次，验证修复效果 + 收集跨 repo 数据。
4. 基于 2+ 次真实数据建立基线。

**C1 稳定后（Phase C2 — 混合分级优先）：**
5. ~~构建 `analyze_worker_output` 混合分级：rules 提取证据包 + LLM 按固定评分标准做语义分级。~~（已实现 v1：`runtime_grading_mode=rules|hybrid|hybrid_llm`；`hybrid` 支持“无测试基础设施 + 有替代验证”语义放行；`hybrid_llm` 在有 manager API key 时启用 LLM 评分）
6. ~~构建 Manager Agent（LLM with tools），与现有 rules 并行运行（A/B 切换）。~~（骨架已实现：`manager_agent.py` 接入 `manager_loop`，`rules|llm|hybrid` 决策统一由 agent 执行。已接入：LLM worker 语义分级（grade_worker_output）→ confidence 传入 ManagerRunFacts → 低信心 PASS 升级人工审核，NEEDS_REVIEW 直接升级；notify_user 在 RUN_FINISH / RETRY / 失败后自动发送通知。）
7. ~~首批 tools：`analyze_worker_output`、`get_global_stats`、`notify_user`。~~（已实现工具模块 + CLI 命令：`analyze-worker-output`、`get-global-stats`、`notify-user`；manager loop LLM facts 已注入 tool 输出）
8. ~~Decision Card 接入 `why_llm`。~~（已实现 Telegram Decision Card 双层展示：`why_machine` + `why_llm`（可选，配置 LLM 时生效），并附 `suggested_actions_llm`）
9. ~~补齐本地 bot/human 演练链路。~~（已实现 `simulate-bot-session` CLI：复用 Telegram 同一套 command + NL handler，可离线演练 `/show`、NL 路由、Decision Card 展示）
10. ~~修复 Decision Card 模式在 NL 路由不一致问题。~~（已修复：`decision_why_mode`/`decision_llm_client` 透传到 NL rules/LLM 分支，命令输入与自然语言输入展示一致）

**C2 验证后（Phase C3）：**
11. ~~Worker 自主 skill 调用改造（v1）。~~（已实现 `agentpr_autonomous`：worker 在单次 run-agent-step 内自主管理 preflight-contract/implement-validate；manager 在 `DISCOVERY|PLAN_READY` 直接调度 `run_agent_step`）
12. ~~状态机简化（13 → 8）。~~（已完成：V1 双轨代码已删除，V2 为唯一路径。`StateSchemaVersion`、`_resolve_target_v1`、所有 V1/V2 分支逻辑已清理。Legacy 状态值保留在 enum 中仅用于读旧 DB。）

**C2+C3 后续落地（2026-02-26 完成）：**
13. ~~LLM 语义分级接入决策循环。~~（已完成：`grade_worker_output` → confidence → ManagerRunFacts → 低信心 PASS 升级人工 / NEEDS_REVIEW 升级。）
14. ~~notify_user 接入 manager loop。~~（已完成：RUN_FINISH / RETRY / 失败后自动记录通知 artifact。）
15. ~~LLM 解析函数去重。~~（已完成：4→1 提取方法。）
16. ~~V1 双轨代码删除。~~（已完成：~250 行 V1 分支逻辑删除。）
17. ~~Telegram 死代码清理。~~（已完成：删除未使用函数，内联常量。）

**P1+P2 已完成（2026-02-27）：**
18. ~~`manager_notification` artifact → Telegram 推送桥接。~~（已完成：`maybe_emit_manager_notifications()` 在 bot loop 中与 state notifications 同频扫描，按优先级标记推送。）
19. ~~`get_global_stats` 接入 bot `/overview`。~~（已完成：pass_rate、grade 分布、top reason codes 展示。）
20. ~~review comment 智能分流。~~（已完成：`triage_review_comment` LLM 工具 + `_triage_iterating_review()` → ITERATING 决策分流 fix_code/reply_explain/ignore。）
21. ~~失败诊断策略生成。~~（已完成：`suggest_retry_strategy` LLM 工具 + `_diagnose_failure()` → FAILED 决策分流 should_retry + target_state。）

**接下来（按优先级排序）：**

P0 — C1 第二轮真实验证：
22. 选第 2 个 repo（建议 mem0），跑完整 QUEUED → PUSHED → PR 流程。重点验证：V2 状态机、混合分级（hybrid_llm）、confidence routing、review triage、retry strategy。
23. 基于 2 次真实数据建立基线指标。

P3 — C4 瘦身 ✅ 完成：
24. ✅ `cli.py` 拆分（4,628 → 2,768 行 + `cli_helpers.py`/`cli_pr.py`/`cli_inspect.py`/`cli_worker.py` 4 个子模块）。
25. ✅ `runtime_analysis.py` 精简（1,746 → 1,712 行）：引入 `_safe_dict` 消除冗余 dict 提取模式、合并 4 个 write 函数。分级逻辑仍为核心骨架（LLM 迁移需功能性变更，非纯重构）。
26. ✅ `telegram_bot.py` handler 扁平化（2,055 → 1,573 行 + `telegram_bot_helpers.py` 568 行）：提取常量、配置、解析器、CLI 执行等。
27. 部分达成：orchestrator 总量 15,205 行（含新增子模块）。三大文件缩减 ~1,400 行（cli.py -1,860, runtime -34, telegram -483），但子模块重复 import 抵消部分。进一步缩减需功能性迁移（runtime grading → LLM layer）。

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

### 16.6 DeepCode 当前进度标记（2026-02-26）

1. 代码已提交并推送到分支：`feature/forge-20260225-195854`
2. 最新 commit：`e38b1a1`（`Add Forge API fallback support`）
3. `request-open-pr` 已生成请求文件：`run_2e642ed9c2f2_pr_open_request_20260226T030531274170Z.json`
4. `approve-open-pr` 在严格 gate 下被拦截（`runtime_not_pass` + `insufficient_test_evidence`），与当前 C1 规则一致。
5. 使用 `--allow-dod-bypass` 的尝试触发网络错误（`error connecting to api.github.com`），随后 run 已人工冻结到 `PAUSED`。
6. 结论：该 repo 的 C1 范围（代码改动 + push + PR gate 流程验证）已完成；PR 语义生成与混合分级留给 C2 处理。

---

## 17. 架构审计记录（2026-02-25 全量代码审查）

### 17.1 代码量分布（2026-02-26 更新）

| 文件 | 行数 | 变化 | 角色 |
|------|------|------|------|
| cli.py | 4,628 | +535 | CLI 命令入口（15+ 命令） |
| telegram_bot.py | 1,977 | +73 | Bot 双模控制面 |
| runtime_analysis.py | 1,746 | +323 | 运行分级与证据提取（含 hybrid/hybrid_llm） |
| manager_llm.py | 727 | +215 | LLM 客户端（decide/grade/explain，4 工具） |
| service.py | 658 | +52 | 事件与状态服务（V2 唯一路径） |
| skills.py | 653 | +337 | 技能发现与 task packet（含 autonomous） |
| manager_loop.py | 641 | +83 | 自动推进循环（含 LLM 分级 + 通知） |
| db.py | 592 | +14 | SQLite 持久化 |
| github_webhook.py | 539 | — | Webhook 接收 |
| executor.py | 488 | — | Worker 执行器 |
| preflight.py | 434 | — | 环境预检 |
| manager_policy.py | 389 | +29 | 策略加载与合并 |
| manager_decision.py | 260 | +84 | 规则决策（含 confidence 分流） |
| manager_tools.py | 187 | 新增 | Manager 工具（analyze/stats/notify） |
| state_machine.py | 170 | +28 | 状态转移图（含 V2 转移） |
| manager_agent.py | 162 | 新增 | Manager Agent（rules/llm/hybrid 决策） |
| github_sync.py | 113 | — | GitHub 状态同步 |
| models.py | 89 | +3 | 数据模型（V2 唯一路径） |
| codex_bin.py | 51 | — | Codex 二进制发现 |
| **orchestrator 合计** | **14,510** | **+2,125** | |

**代码量变化分析**：
- 新增模块 +349 行：`manager_agent.py`（162）+ `manager_tools.py`（187）— 这是 C2 Manager Agent 的核心新增，是目标架构要求的。
- 增长最大的文件：`cli.py`（+535）、`skills.py`（+337）、`runtime_analysis.py`（+323）— 主要是 C2 功能（autonomous skills、hybrid grading、V2 状态支持）。
- 删除代码 ~400 行：V1 双轨逻辑、LLM 解析重复代码、死函数。
- **净增 +2,125 行反映了 C2 功能的实际复杂度**。C4 瘦身的目标是将总量压缩到 <10K。

### 17.2 核心发现

**复杂度分配（2026-02-26 更新）：**
- 确定性控制（状态机 + 规则 + regex + policy）：~60% 代码量（从 ~70% 下降）
- LLM 智能层：~12% 代码量（`manager_llm` 727 + `manager_agent` 162 + `manager_tools` 187 = 1,076 行）
- 基础设施（DB + CLI + Bot + webhook）：~28% 代码量

**关键判断（更新版）：**

1. ~~**Manager LLM 增量价值极低**~~ → **已改善**：LLM 现在参与三个实质性决策：(a) 语义分级（grade_worker_output → PASS/NEEDS_REVIEW/FAIL + confidence），(b) confidence routing（低信心 PASS 升级人工而非盲推），(c) Decision Card 解释（why_llm + suggested_actions）。决策空间从 ≈0 扩展到有实际价值。
2. **runtime_analysis.py 仍需简化**：混合策略已实现（`hybrid`/`hybrid_llm` 模式），但 1,746 行 regex 代码仍全部保留。C4 应将分级判断迁移到 LLM 层，保留证据提取和硬护栏。
3. ~~**13 个状态过多**~~ → **已解决**：V2 简化到 10 个状态（8 核心 + QUEUED/NEEDS_HUMAN_REVIEW）。Legacy 状态仅用于读旧 DB。
4. **Skills 系统复杂度上升**：从 316 → 653 行（autonomous 模式新增）。C4 应评估是否可进一步简化。
5. **cli.py 膨胀是最大维护债**：4,628 行单文件，占 orchestrator 32%。C4 拆分是必需。

### 17.3 与外部系统对比

| 系统 | 架构模式 | 对 AgentPR 的启示 |
|------|----------|-------------------|
| GitHub Copilot coding agent | 薄编排 + 强 agent + PR gate | agent 自主度高，编排层主要管 PR 生命周期 |
| OpenHands / SWE-agent | issue→agent→PR，编排层很薄 | 证明了"LLM agent + 简单生命周期"可以工作 |
| OpenClaw | 对话网关 + 个人助理 | "LLM 在对话中心"的模式值得借鉴 |
| Devin | 厚编排 + 厚 agent | 走全包路线，不适合轻量级 OSS 场景 |

**结论**：行业趋势是"编排层做生命周期管理和安全约束，智能决策交给 LLM"。AgentPR 当前反过来了——编排层承担了太多决策逻辑。

### 17.4 整体评价（2026-02-26 更新）

| 维度 | 评分 | 变化 | 说明 |
|------|------|------|------|
| 架构方向 | 正确 | — | manager-worker 分离、人工 gate、push_only |
| 工程量 | 偏重 | ↗ | 14.5K 行（从 12K 增长），C2 新功能驱动。C4 需瘦身 |
| LLM 使用 | 改善中 | ↑↑ | 从"只做选择题"升级到"语义分级 + confidence routing + 解释"。但失败诊断/review 分流仍缺 |
| 安全设计 | 恰当 | — | sandbox + gate + 审计持续在位 |
| 抽象层数 | 改善中 | ↑ | V1/V2 双轨已删除，但 prompt 构建链仍有 4-5 层 |
| 核心矛盾 | 部分缓解 | ↑ | LLM 智能层从 5% → 12%，控制面从 70% → 60%。方向对，但距目标仍有距离 |

**一句话（更新版）**：从"精密工厂管理聪明工人"开始向"轻量生产线 + 智能经理 + 自主工人"转型。LLM 已有实质性决策参与，但控制面仍偏厚。下一步：用真实数据验证当前改进的效果，再决定 C4 瘦身的力度和方向。

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

---

## 19. 本轮实现落地记录（2026-02-26，C2 完成 + C3 完成 + 精简 + LLM 接入）

### 19.1 本轮目标（与你确认后的执行口径）

1. 继续按本计划推进，不回退到 C1；优先完成 C2 的 manager agent 主体落地。
2. 在 C2 可用后，推进 C3：worker 自主 skill（v1）+ 状态机简化（v2）。
3. 强调”流程不断”：允许新旧状态并存，旧 run 不做强制迁移，避免运行中断。
4. **追加目标（代码审计后）**：删除 V1 双轨代码、合并 LLM 重复解析、接通 LLM 核心能力到决策循环、Telegram 清理。

### 19.2 关键设计决策（本轮锁定）

1. **混合策略继续保留**：Rules 负责硬护栏与证据提取，LLM 负责语义判断和建议，不做”全量替换 regex”的激进改造。
2. **V2 唯一路径**：已删除 `StateSchemaVersion` 双轨代码，新 run 始终使用简化 8 状态。旧 DB 中的 legacy 状态值保留在 enum 中以兼容读取，但不再有 V1/V2 分支逻辑。
3. **状态收敛**：`DISCOVERY/PLAN_READY/IMPLEMENTING/LOCAL_VALIDATING` → `EXECUTING`；`FAILED_RETRYABLE/FAILED_TERMINAL/SKIPPED` → `FAILED`。传入旧目标态时自动归一化。
4. **Confidence routing**：LLM 语义分级产出 confidence（low/medium/high），rules 层使用 confidence 做分流：高信心 PASS → RUN_FINISH，低信心 PASS → WAIT_HUMAN。这实现了”LLM 判断 + 硬约束兜底”的混合策略。

### 19.3 代码落地清单（按模块）

#### A. 状态模型与持久化

1. `orchestrator/models.py`
   - V2 状态：`QUEUED/EXECUTING/PUSHED/CI_WAIT/REVIEW_WAIT/ITERATING/PAUSED/DONE/FAILED/NEEDS_HUMAN_REVIEW`。
   - Legacy 状态保留在 enum 中仅用于读取旧 DB 行（`DISCOVERY/PLAN_READY/IMPLEMENTING/LOCAL_VALIDATING/SKIPPED/FAILED_RETRYABLE/FAILED_TERMINAL`）。
   - 已删除：`StateSchemaVersion`、`LEGACY_*` frozensets、`canonical_display_state_for_v2`、`normalize_state_schema_version`。
2. `orchestrator/db.py`
   - `runs` 表 `state_schema_version` 列保留（旧 DB 兼容），新 run 硬编码写入 `"v2"`。
3. `orchestrator/service.py`
   - 已删除 `_resolve_target_v1`，仅保留 V2 解析逻辑（`_resolve_target`）。
   - 已删除 `_normalize_target_for_schema`、`_display_state_for_schema`。
   - 保留 `_normalize_legacy_target()` 用于将旧目标态归一化到 V2。
   - `display_state` = `state`（identity，兼容下游代码）。

#### B. 状态机与转移约束

1. `orchestrator/state_machine.py`
   - 增补 `EXECUTING`、`FAILED` 合法转移图。
   - 保留 v1 旧态转移，不破坏旧 run。
   - 允许 schema 归一化后的 `retry/resume` 目标转移。

#### C. Manager 决策与 Agent 主体

1. `orchestrator/manager_agent.py`
   - C2 主体已接入：`rules|llm|hybrid` 统一入口。
   - tools 上下文：`get_run_status`、`analyze_worker_output`、`get_global_stats`。
   - LLM facts 包含 `latest_worker_confidence`。
2. `orchestrator/manager_decision.py`
   - 新增 `EXECUTING` 决策：默认 `RUN_AGENT_STEP`，若最新 worker grade=`PASS` 切到 `RUN_FINISH`。
   - 新增 `FAILED` 决策：`RETRY` 目标 `EXECUTING`。
   - **新增 confidence 分流**：`ManagerRunFacts.latest_worker_confidence` 字段；PASS + low confidence → `WAIT_HUMAN`；NEEDS_REVIEW → `WAIT_HUMAN`。
3. `orchestrator/manager_loop.py`
   - 事实注入增加 `latest_worker_grade` + `latest_worker_confidence`。
   - `_latest_worker_grade()` 返回 `(grade, confidence)` 元组，从 digest 的 `classification.semantic.confidence` 或 `classification.confidence` 提取。
   - **新增 LLM 分级调用**：当 grade 存在但 confidence 缺失且 LLM 可用时，调用 `grade_worker_output()` 获取语义评估。
   - **新增 `_notify_after_action()`**：RUN_FINISH / RETRY / 失败后通过 `notify_user()` 记录通知 artifact。
   - `RETRY` 默认目标始终 `EXECUTING`。

#### D. CLI / Policy / Runtime 分级

1. `orchestrator/cli.py`
   - 已删除 `--state-schema-version` 参数。
   - `run-agent-step` 支持 `EXECUTING`，legacy 策略默认值自动归一化到 V2。
   - `converge_agent_success_state` 与 `apply_nonpass_verdict_state` 统一使用 V2 状态。
2. `orchestrator/manager_policy.py` + `orchestrator/manager_policy.json`
   - 默认 `success_state` 调整为 `EXECUTING`。
   - 默认 `on_retryable_state` 调整为 `FAILED`。
3. `orchestrator/runtime_analysis.py`
   - 将 `EXECUTING` 纳入 test evidence / semantic override 的判定范围。

#### E. Skills / Bot / 展示层

1. `orchestrator/skills.py`
   - `STAGE_SKILLS` 使用 `EXECUTING` 映射，已删除 legacy 状态条目。
2. `orchestrator/telegram_bot.py`
   - `retry/resume` 默认目标始终 `EXECUTING`（`DEFAULT_TARGET_STATE` 常量）。
   - 已删除 `StateSchemaVersion` 导入。
   - 已删除 `default_execution_target_state()` 函数（8 处调用 → `DEFAULT_TARGET_STATE` 常量）。
   - 已删除死代码 `extract_repo_ref_from_text()`。
3. `orchestrator/manager_tools.py`
   - 全局统计优先使用 `display_state` 聚合，减少新旧状态混读歧义。

#### F. 文档同步

1. `README.md`、`orchestrator/README.md` 同步了：
   - `agentpr_autonomous`、runtime grading、state schema、策略默认值更新。
2. 本文档 `15` 节中 C3 第 12 项已标记完成（v2）。

### 19.4 本轮测试与验证证据

1. `python3.11 -m py_compile orchestrator/*.py`：通过。
2. smoke（临时 DB）：
   - `create-run` 后 `start-discovery`，状态进入 `EXECUTING`。
   - `manager-tick --dry-run` 在 `EXECUTING` 下给出 `run_agent_step`。
   - 注入 PASS digest 后，`manager-tick --dry-run` 在 `EXECUTING` 下给出 `run_finish`。
   - `retry --target-state FAILED` 后，`manager-tick --dry-run` 给出 `retry -> EXECUTING`。
3. bot 本地演练：
   - `simulate-bot-session` 中 `/list` 显示状态；
   - 自然语言”重试 run_id”默认落到 `EXECUTING`。

### 19.5 质量结论（本轮更新）

1. **达成度**：C2 Manager Agent 主体 + LLM 语义分级 + confidence routing + 通知已接入主流程。V2 唯一路径。C3 状态机简化完成。
2. **代码风险等级**：中等可控。改动涉及 12+ 文件，但每一步都通过 `py_compile` 验证。LLM 分级和通知均为 best-effort（异常不阻塞主循环）。
3. **提交可行性**：满足”可提交评审”标准。**强烈建议下一步做 C1 第二轮真实验证**——当前所有改进仍基于 1 次 DeepCode 测试，数据不足以确认稳定性。
4. **代码量趋势**：从 12,385 → 14,510 行（+17%）。增长主要是 C2 新功能（manager_agent/tools/LLM 扩展），不是膨胀。但 C4 瘦身是实际需求。

### 19.6 已知残留与后续建议

1. **缺系统化测试**：当前以 CLI smoke + `py_compile` 为主，无单元测试。risk：confidence routing 等新逻辑的边界场景未验证。
2. **通知最后一公里未连通**：`_notify_after_action()` 产生 `manager_notification` artifact，但 artifact 未推送到 Telegram。需要 artifact → bot 推送桥接。
3. **LLM 高阶智能仍缺**：失败诊断策略生成（`suggest_retry_strategy`）和 review comment 分流（`triage_review_comment`）尚为目标架构中的规划，未实现。
4. **`explain_decision_card()`** 已实现但仅在 Telegram Decision Card 场景被调用，manager_loop 主流程未直接使用。
5. **代码瘦身 backlog**：`cli.py`（4,628）、`runtime_analysis.py`（1,746）、`telegram_bot.py`（1,977）是三个最大的单文件，C4 应拆分/简化。

### 19.7 反思与认知更新

1. **”先删后加”的节奏是对的**：V1 删除 → LLM 解析合并 → 再接通新能力，避免了在冗余代码上叠加新逻辑。
2. **Confidence routing 是正确的中间态**：不是”LLM 全权决策”（太激进），也不是”LLM 只做选择题”（无价值）。而是”LLM 提供判断 + 置信度，rules 根据置信度做分流”。这既保留了安全兜底，又让 LLM 在正确的位置发挥作用。
3. **代码量增长是预期内的**：C2 是新增功能（not 重构），代码量上升正常。但净增 2,125 行提醒我们：C4 瘦身不是美观追求，是维护成本控制。
4. **真实验证是所有改进的试金石**：C1 只跑了 1 次 DeepCode，暴露了 rg 隐藏目录和 min_test_commands 两个问题。第二轮验证很可能暴露新问题。在没有更多数据前，不应继续堆新功能。
5. **目标架构（Section 4）的进展评估**：
   - “Manager 是真正的 LLM 大脑” — **部分达成**：LLM 参与语义分级和 confidence routing，但尚未参与失败诊断和 review 分流。
   - “Orchestrator 是薄层” — **未达成**：14,510 行代码说明 orchestrator 仍然很厚。需要 C4 瘦身。
   - “Worker 自主执行” — **基本达成**：`agentpr_autonomous` 模式下 worker 单次完成分析+实现+验证。
   - “安全兜底” — **已达成**：sandbox + gate + 审计 + 硬约束一直在位。
