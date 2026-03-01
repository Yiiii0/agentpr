# AgentPR Master Plan

> 更新时间：2026-02-28
> 状态：C1-C4 + P1-P2 全部完成。缺第二轮真实验证。
> 归档：旧版详细记录在 `docs/AGENTPR_MASTER_PLAN_ARCHIVE_20260228_PRE_SLIM.md` 和 `docs/AGENTPR_MASTER_PLAN_ARCHIVE_20260225_PRE_REWRITE.md`

---

## 1. North Star

1. 人只在 Telegram 对话：下发任务、看状态、做 approve/deny。
2. Manager（LLM）持续在线：理解 NL、调用工具、推进状态机、主动通知、提出改进建议。
3. Worker（codex exec）专注执行：读仓库、改代码、建环境、跑测试、产出证据。
4. 安全默认：最小权限、最小改动、可回放、可审计。
5. `merge` 始终人工；`create PR` 受 gate 保护。

---

## 2. 当前主矛盾

1. 不是缺"编排框架"，而是缺 Manager LLM 决策层闭环。
2. 不是缺"更多日志"，而是缺"可执行决策输入"到下一步动作的自动路由。
3. 不是缺"多 agent 并发"，而是缺"单 run 高质量稳定完成率"。
4. 当前最直接的稳定性风险是"runtime 分级误判"会把本可继续的 run 过早打到 `NEEDS_HUMAN_REVIEW`。
5. **复杂度分配改善中**：~15.2K 行 orchestrator 代码（C4 拆分后含子模块），~60% 是确定性控制，~12% 是 LLM 智能层。三大文件已拆分为子模块结构。
6. **抽象层过多**：worker 最终收到的就是一个 prompt string，但经过 5 层构建（prompt → skills → task packet → safety contract → executor）。

结论：方向正确，不需要推翻重写；但当前优先级应从"补更多控制逻辑"转向"让 LLM 在正确的位置发挥智能"。

---

## 3. 目标架构

```
Human (Telegram NL + /commands)
        |
Bot Gateway (薄层：消息路由 + 认证 + 频率限制)
        |
Manager Agent (LLM with tools — 系统大脑)
  |-- tool: create_run / get_run_status / get_global_stats
  |-- tool: execute_worker / analyze_worker_output
  |-- tool: triage_review_comment / suggest_retry_strategy
  |-- tool: notify_user / propose_iteration
        |
Orchestrator (薄层：状态持久化 + gate 执法 + 事件日志)
  |-- 硬约束：PR 创建需人工确认 / merge 永远人工
  |-- 硬约束：diff budget / retry 上限 / sandbox
        |
Worker Agent (codex exec — 自主执行)
  |-- 内部管理分析→实现→验证
  |-- 自主调用 skills
        |
Repo workspace + GitHub
```

### 角色边界

1. **Manager Agent**：LLM 大脑。决策、分析、策略生成、主动通知。通过 tools 与 Orchestrator 交互。
2. **Orchestrator**：薄层基础设施。状态持久化、gate 执法、事件日志。不做决策。
3. **Worker**：自主执行。单次 codex exec 完成分析+实现+验证，自行调用 skills。

### 与旧架构的核心差异

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| 大脑 | Rules engine (`manager_decision.py`) | Manager LLM (带 tools 的 agent) |
| LLM 角色 | 从 N 选 1 的橡皮章 | 真正的决策者（分析、策略、解释） |
| 状态机 | 13 个状态，外部微管理 worker 阶段 | 10 个状态（V2），worker 内部管理子阶段 |
| Skills | Orchestrator 注入到 prompt | Worker 自主调用，保护上下文窗口 |
| 失败分析 | 1,400 行 regex | 混合：Rules 提取证据 + 硬护栏，LLM 做语义诊断 + 策略 |
| 通知 | 被动（用户查询） | 主动（Manager 判断何时通知） |
| 硬约束 | 分散在 rules/analysis/policy 各处 | 集中在 Orchestrator 薄层 |

### 目标状态机

```
QUEUED → EXECUTING → PUSHED → CI_WAIT → REVIEW_WAIT → DONE
                                  ↕           ↕
                              ITERATING ← ─ ─ ┘
+ PAUSED (任何非终态可暂停)
+ NEEDS_HUMAN (升级人工)
+ FAILED (终态)
```

当前 V2 实际使用 10 个状态（上述 + QUEUED）。Legacy 状态值保留在 enum 中仅用于读旧 DB。

**合并依据**（从 13 到 10）：
- DISCOVERY + PLAN_READY + IMPLEMENTING + LOCAL_VALIDATING → **EXECUTING**（worker 内部管理子阶段）
- FAILED_RETRYABLE + FAILED_TERMINAL → **FAILED**（重试决策由 Manager Agent 判断）
- SKIPPED 合并到 FAILED（metadata 区分 `reason=skipped`）

---

## 4. Manager Action Contract

### 4.1 当前动作集

1. `list_runs(limit)`
2. `show_run(run_id)`
3. `create_run(owner, repo, prompt_version, mode)`
4. `start_discovery(run_id)` — QUEUED → EXECUTING
5. `run_prepare(run_id)` — fork + clone workspace（auto-prepare：manager loop 自动在 RUN_AGENT_STEP 前检测并调用）
6. `run_agent_step(run_id, prompt_key, skills_mode)`
7. `run_finish(run_id, changes, commit_title)`
8. `request_open_pr(run_id, title, body_file)`
9. `approve_open_pr(run_id, request_file, confirm_token, confirm=true)`
10. `pause_run(run_id)`
11. `resume_run(run_id, target_state)`
12. `retry_run(run_id, target_state)`
13. `analyze_worker_output(run_id)` — 混合分级
14. `get_global_stats()` — 全局运营统计
15. `notify_user(message, priority)` — 主动通知
16. `suggest_retry_strategy(run_id, diagnosis)` — LLM 失败诊断
17. `triage_review_comment(run_id, comment_body)` — LLM review 分流

### 4.2 目标动作集（进一步简化后）

1. `create_run(owner, repo, task_description)`
2. `execute_worker(run_id, instructions)` — 替代 start_discovery/run_prepare/run_agent_step
3. `analyze_worker_output(run_id)` — 混合分级
4. `run_finish(run_id, changes, commit_title)`
5. `request_open_pr / approve_open_pr`
6. `pause_run / resume_run / retry_run(strategy)`
7. `get_global_stats() / notify_user(message, priority)`

### 4.3 约束（始终有效）

1. Manager 不能调用任意 shell。
2. 只能调用白名单 action。
3. action 参数必须通过 JSON schema 验证。

---

## 5. LLM 能力边界

### 5.1 应使用 LLM（已完成）

| 场景 | 当前做法 |
|------|----------|
| **语义分级** | `hybrid_llm` 模式：rules 证据 + LLM `grade_worker_output` → PASS/NEEDS_REVIEW/FAIL + confidence |
| **Confidence routing** | `ManagerRunFacts.latest_worker_confidence` → 低信心 PASS 升级人工 |
| **Decision Card why_llm** | `explain_decision_card` → 双层展示 why_machine + why_llm + suggested_actions |
| **NL → action 路由** | bot 双模路由 + manager LLM intent 解析（rules/hybrid/llm 三模） |
| **通知** | `notify_user` artifact → `maybe_emit_manager_notifications()` → Telegram 推送 |
| **失败原因分析** | `suggest_retry_strategy` → should_retry + target_state + 修改指令 |
| **重试策略生成** | `_diagnose_failure()` → `RetryStrategy` → FAILED 决策分流 |
| **Review comment 处理** | `triage_review_comment` → fix_code/reply_explain/ignore → ITERATING 决策分流 |
| **全局运营汇报** | `get_global_stats` 接入 `/overview`：pass_rate、grade 分布、top reason_codes |

### 5.2 不应使用 LLM（当前做法正确，保持）

| 场景 | 原因 |
|------|------|
| 状态转移与合法性校验 | 必须确定性、可审计、可回放 |
| Gate 执法（PR DoD、确认 token、ACL） | 安全边界不能交给概率模型 |
| 安全隔离（sandbox 模式、文件权限） | 必须硬约束 |
| 事件去重、重放、审计落盘 | 幂等性要求 |
| 重试上限、diff 预算上限 | 防止无限循环烧钱（上限值本身是硬规则） |
| Webhook 签名校验 | 密码学确定性 |

### 5.3 边界案例（LLM 判断 + 硬约束兜底）

| 场景 | 处理方式 |
|------|----------|
| runtime grading | **混合策略**：Rules 提取证据包（test_commands, diff_stats, exit_code 等），LLM 基于证据包做语义分级。硬护栏（sandbox 违规、diff 超限）由 rules 强制执行，LLM 不可覆盖 |
| diff 合理性 | LLM 判断改动是否符合意图，但 diff budget 上限仍硬执行 |
| 是否需要人工介入 | LLM 建议，但 PUSHED/NEEDS_HUMAN gate 仍硬执行 |

### 5.4 Decision Card 生成原则

1. `what/decision/evidence` 必须是机器事实（deterministic）。
2. `why_explained` 由 LLM 生成：基于 evidence 给出可操作的解释。
3. `suggested_actions` 由 LLM 提供：具体的下一步选项。
4. 对外显示双层：`why_machine`（机器事实） + `why_llm`（智能解释 + 建议）。

---

## 6. 当前已完成（what works）

### 核心流程
- 状态机 + 事件 + 幂等 + SQLite 持久化
- Worker 执行链：auto-prepare（workspace 自动 fork/clone）→ preflight → run-agent-step → runtime grading → push
- PR gate：request-open-pr + approve-open-pr --confirm + DoD 检查
- Manager loop：manager-tick / run-manager-loop（rules/llm/hybrid 决策）
- V2 唯一路径，V1 双轨代码已删除
- 连续失败保护：同一 run 连续 3 次 action 失败自动 PAUSE + 通知

### Manager LLM 能力
- 语义分级：hybrid_llm 模式（rules 证据 + LLM grade_worker_output → PASS/NEEDS_REVIEW/FAIL + confidence）
- Confidence routing：低信心 PASS 升级人工审核
- Decision Card：why_machine + why_llm + suggested_actions 双层展示
- Review triage：triage_review_comment → fix_code/reply_explain/ignore
- 失败诊断：suggest_retry_strategy → should_retry + target_state
- 全局统计：get_global_stats 接入 /overview（pass_rate、grade 分布、top reason_codes）
- 通知：manager_notification artifact → Telegram 推送（含优先级标记）

### Bot 交互
- CLI 命令：/create /overview /list /show /status /pause /resume /retry /approve_pr
- NL 路由：rules/hybrid/llm 三模，会话级 run_id 绑定
- 主动通知：PUSHED/NEEDS_HUMAN_REVIEW/DONE/ITERATING 状态变更 + manager 通知

### Worker
- agentpr_autonomous 模式：worker 单次完成分析+实现+验证
- Skills 系统可用（markdown 定义，worker 可访问）
- Codex 支持 Forge provider（`.env` 配置 `AGENTPR_FORGE_BASE_URL` + `AGENTPR_FORGE_API_KEY`，不设则用默认 provider）

### 代码结构（C4 瘦身后）
- `cli.py` (2,768) + 4 子模块（cli_helpers/cli_pr/cli_inspect/cli_worker）
- `telegram_bot.py` (1,573) + telegram_bot_helpers (568)
- `runtime_analysis.py` (1,712)
- orchestrator 总计 ~15.2K 行（27 个 .py 文件）

---

## 7. 系统级验收对照

### 目标能力 A：NL 下发任务 → 自动推进到 PUSHED/PR gate

**当前状态：大部分达成。** NL 触发 `create_runs` → auto-prepare → manager loop 自动推进 → PUSHED/gate。V2 状态机简化了推进路径。
**缺口**：常驻 loop 仍需手动启动（`run-manager-loop`），未做 systemd/cron 常驻化。

### 目标能力 B：问"现在什么情况"→ 全局态势 + 下一步

**当前状态：部分达成。** `/overview` + `/show` + Decision Card 双层展示。`get_global_stats` 已实现。
**缺口**：缺"下一步优先级队列"。

### 目标能力 C：状态变更时主动通知

**当前状态：已达成。** Bot 有关键状态主动通知 + `manager_notification` artifact 推送到 Telegram（含优先级标记）。

### 目标能力 D：PR review comments → 主动问"人工还是自动修复"

**当前状态：基础具备。** Webhook/Sync → `ITERATING`。`triage_review_comment` LLM 分流（fix_code/reply_explain/ignore）。
**缺口**：bot 端尚未做主动决策对话层（目前 triage 结果直接影响 manager 决策，不经过用户确认）。

### 目标能力 E：manager 自我迭代提案 → 人审批

**当前状态：基础具备。** 已有 `skills-metrics/skills-feedback`。
**缺口**：缺"自动提案 → 审批 → 应用"闭环执行器。

---

## 8. 实事求是：当前差距

### 8.1 最大的缺口：缺真实验证数据

**只有 1 次真实测试**（C1: HKUDS/DeepCode），结果 NEEDS_HUMAN_REVIEW/missing_test_evidence。所有 C2-C4 改进都是理论上的改进，没有第二个数据点验证。

这是当前最高优先级。在没有更多真实数据前，不应继续堆新功能。

### 8.2 Orchestrator 不是"薄层"

目标：orchestrator < 8K 行。实际：15.2K 行。

原因分析：
- `runtime_analysis.py` (1,712 行)：1,700 行 regex/rules 做的事情，一个 LLM 调用（带结构化证据包）可能做得更好。但迁移是功能性变更，不是纯重构。
- `cli.py` (2,768 行)：CLI 入口本身就承载了 15+ 命令的参数解析和执行逻辑，这是必要的复杂度。
- `cli_inspect.py` (966 行)：inspection/feedback 报告生成，大量 dict 拼接。
- `manager_llm.py` (968 行)：4 个 LLM 工具的 prompt 构建 + 响应解析。

**判断**：15K 不是"膨胀"，是当前功能集的真实复杂度。要真正降到 <10K 需要：(a) runtime grading 迁移到 LLM，(b) 精简 CLI 命令集，(c) 减少报告生成代码。这些都是功能性决策，不是重构能解决的。

### 8.3 Manager LLM 角色定位

**目标**：Manager 是真正的 LLM 大脑，orchestrator 只是执行层。
**现实**：Manager LLM 参与 6 个决策点（grading、confidence routing、triage、retry strategy、explain、NL routing），但 orchestrator 仍然承担大量规则决策。

**实质进展**：从"LLM 只做选择题"升级到"LLM 参与语义判断 + confidence routing"。这是正确的中间态，不需要激进地全面替换 rules。

### 8.4 其他未完成项

| 项目 | 状态 | 优先级 |
|------|------|--------|
| C1 第二轮真实验证 | **未做** | **P0** |
| Manager loop 常驻化（systemd/cron） | 未做 | P1 |
| Bot 会话上下文持久化（当前内存态） | 未做 | P2 |
| skills-feedback → prompt/policy patch 草案闭环 | 未做 | P3 |
| runtime grading 迁移到 LLM 层 | 未做 | P3 |

---

## 9. 下一步（按优先级）

### P0：第二轮真实验证

选 1 个新 repo（建议 mem0ai/mem0），完整跑通 QUEUED → PUSHED → PR 创建。

验证重点：
1. V2 状态机走通（无 V1 状态出现）
2. auto-prepare 自动 fork/clone workspace
3. hybrid_llm 分级给出合理 confidence（需 API key）
4. review triage + retry strategy 是否在真实场景有效
5. 通知是否在 bot 模式下正常推送
6. 连续失败保护是否正常触发

基于 2+ 次真实数据建立基线指标：
- 首次成功率（目标 ≥ 50%）
- 平均 worker attempt 数（目标 ≤ 2）
- NEEDS_HUMAN_REVIEW 中误升级率（目标 ≤ 30%）

### P1：Manager loop 运营化

- 常驻进程（systemd service 或后台 daemon）
- 异常自恢复（crash → 自动重启）
- 日志轮转

### P2：Bot 会话持久化

当前 run_id 绑定是内存态（bot 重启丢失）。改为 SQLite 或 JSON 文件持久化。

### P3：自我迭代闭环

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
1. 默认"建议→审批→应用"。
2. 仅允许 manager 自动改写候选草案（`data/prompts/*`、`data/contracts/*`、`skills/*`）。
3. 核心策略文件仍需人工审批合并。
4. 迭代不是每次 run 都触发；由 manager 基于收益判断触发（失败模式重复、成本异常时才提案）。

---

## 10. 运行循环（manager 常驻）

推荐节奏：
1. 事件驱动优先：webhook 到达立即处理。
2. 定时巡检兜底：每 5-10 分钟一次（非高频轮询）。
3. LLM 调用仅在"需要决策"时触发。
4. 当前执行模型为 queue 串行（单 manager loop），先保证稳定闭环，再考虑并发 worker 池。

每轮巡检做什么：
1. 拉取 pending runs。
2. 对每个 run 读取 state + latest digest。
3. 调用 manager 决策（规则优先，可选 LLM）。
4. 执行动作或升级人工。
5. 记录 decision trace。

保护机制：
- 同一 run 连续 3 次 action 失败 → 自动 PAUSE + 高优先级通知。
- workspace 不存在 → 自动 run-prepare（fork + clone），prepare 失败则计入连续失败。

---

## 11. 外部对标

### 可借鉴

1. **GitHub Copilot coding agent**：后台执行 + 人审 PR + session log 可追踪。
2. **OpenHands**：`fix-me` / `@openhands-agent` 触发 + 评论迭代闭环。
3. **LangGraph/Temporal/Inngest**：强调持久化、长流程恢复、human-in-the-loop。

### 不直接照搬

1. 直接上重型多 agent 编排框架（增加复杂度，不能直接提升 worker 代码质量）。
2. 先做 swarm 并发（会放大环境/依赖/成本问题）。
3. 过早追求 Web 控制台（当前 Telegram 足够）。

### OpenClaw 的架构启示

核心原则："The hard problem in personal AI agents is not the agent loop itself, but everything around it."

| 设计点 | OpenClaw 做法 | AgentPR 当前 | AgentPR 应借鉴 |
|--------|-------------|-------------|---------------|
| 谁是大脑 | LLM agent（Pi runtime） | Rules engine + LLM 辅助 | Manager 应是真正的 LLM agent with tools |
| Gateway 角色 | 薄层：消息路由 + 会话管理 | 厚层：状态机 + 规则决策 + regex 分析 | Orchestrator 应退化为薄层 |
| Skills | Markdown 文件，agent 自己决定调用 | Worker 自主调用（autonomous 模式已实现） | ✅ 已对齐 |
| 主动性 | Heartbeat 模式：定时检查 | 定时巡检 + 状态通知（已实现） | ✅ 已对齐 |
| 状态 | append-only 事件日志 | SQLite + 10 状态机 | 保留 SQLite，状态已简化 |

**核心转变**：从 `Rules (大脑) → LLM (橡皮章) → Worker (手脚)` 转为 `LLM (大脑) → Rules (安全护栏) → Worker (自主执行)`。当前处于中间态——LLM 已有实质性决策参与，但 rules 仍承担大量决策。

---

## 12. 决策锁定

1. Python 固定 `3.11`。
2. Worker 固定 `codex exec`。
3. 默认模式 `push_only`，`merge` 永远人工。
4. `create PR` 必须二次确认。
5. Manager 默认走 API function-calling（Forge provider 优先）。
6. 混合策略：Rules 负责硬护栏 + 证据提取，LLM 负责语义判断 + 建议。不做全量替换。
7. baseline 仓库固定 `mem0` 与 `dexter`。

---

## 13. 安全与隔离

1. Worker 写权限限定在 repo + `.agentpr_runtime` + `/tmp`。
2. 仓库外写入禁用，仓库外读取仅允许白名单。
3. `PUSHED -> open PR` 必须人工双确认。
4. 这套策略对"本地单人运营 + 多 OSS 仓库"是合理的。
5. 若将来多人共享，必须升级为主机级隔离（每用户独立 runtime/凭据/审计）。

---

## 14. 沉淀的核心认知

**早期实践：**
1. 先解决主矛盾：闭环决策，不是堆框架。
2. "控制面稳定 > 执行面花哨"。
3. 最小改动能力来自：prompt + policy + gate 的协同，而不是单次模型能力。
4. 运行成功率的核心是环境与规则证据，不是"更强模型名"。
5. 可观测要分层：日常看 digest，失败看 event stream。

**架构审计后：**
6. 系统应该简单，但不应过于简单。度的把握：安全和持久化不能简化，决策逻辑应交给 LLM。
7. **"精密工厂管理聪明工人"是反模式。** 应该是"轻量生产线 + 自主工人 + 关键检查点"。
8. 复杂度应该投资在"智能"上（失败分析、策略生成、运营汇报），而不是在"控制"上（每个状态的合法动作列表）。
9. OpenClaw 的核心启示：LLM 应该是大脑（with tools），不是规则引擎的附属品。
10. Skills 的正确用法是 worker 自主调用（保护上下文），不是 orchestrator 外部注入。

**C1 测试后：**
11. **先用真实数据验证，再做架构调整。** C1 一次 DeepCode 测试暴露的 rg 隐藏目录问题，比任何静态审查都有效。每次真实测试 > 10 次代码审查。
12. 另一个 LLM 的合理反馈：不要为改架构而改架构，先跑通第一个 PR 再说。基线数据是一切决策的基础。
13. **Contract（skill-1 输出）不应作为人工审核 gate。** Worker 内部产出、内部消费，有 blocker 时 worker 自己停止报告。
14. Preflight 检查是通用的（自动检测项目类型和工具链），对任何新 repo 都适用。
15. 状态机的复杂度来自真实问题（环境失败、异步 CI、不同失败类型），不是凭空设计。但部分状态可以内化到 worker 或 Manager Agent。
16. C1 验证了两个判断：(a) `min_test_commands` 硬性要求不适用所有项目；(b) skill-1 作为独立外部产物无增量价值——worker 同次执行中做分析+实现效果更好。
17. 工具细节决定成败：rg 默认跳过 `.github/` 隐藏目录。这类问题需要在 prompt 或 prepare 脚本中修复。

**LLM 接入后：**
18. **混合策略是正确的中间态。** Rules 做证据提取 + 硬护栏（不可被 LLM 覆盖），LLM 做语义判断。不是全替换 regex，是分层。
19. **Confidence routing 让 LLM 在正确位置发挥作用。** 不是"LLM 全权决策"（太激进），也不是"LLM 只做选择题"（无价值）。安全兜底在 rules，语义理解在 LLM。
20. "先删后加"比"边加边删"安全：先清理 V1 双轨 → 再接通 LLM，避免在冗余代码上叠加新逻辑。
21. **代码量增长不等于膨胀，但超过阈值时维护成本急升。** C4 做了结构优化但总量未降，进一步需要功能性决策。
22. **通知"最后一公里"容易被忽略。** 产生 artifact 只是一半，推送到用户才是闭环。这类"看似完成实则断链"的问题需要端到端验证发现。
23. **如果做的不对，再大的代价也是最小的代价。** 先跑通第一个 PR 再说。

---

## 15. 参考资料

1. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
2. GitHub Copilot coding agent：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
3. OpenHands：<https://docs.all-hands.dev/modules/usage/how-to/github-action>
4. OpenAI Function Calling：<https://platform.openai.com/docs/guides/function-calling>
5. SWE-agent：<https://github.com/SWE-agent/SWE-agent>

---

## 16. C1 测试记录摘要

| 项 | 值 |
|-----|-----|
| Run ID | `run_2e642ed9c2f2` |
| Repo | HKUDS/DeepCode |
| 最终状态 | `NEEDS_HUMAN_REVIEW` |
| 原因 | `missing_test_evidence`（DeepCode 无测试基础设施） |
| 改动 | 4 files, +48/-10, pre-commit 全过 |
| 暴露问题 | (1) rg 不搜 .github/ 隐藏目录（已修复）(2) min_test_commands 太刚性（已改为混合分级）|
| 结论 | 代码 push 完成，PR gate 按预期拦截。验证了基本流程可跑通。 |

### 混合分级策略（C1 驱动的设计确认）

**Rules 层（确定性，不可被 LLM 覆盖）：**
- 证据提取：test_commands、lint_commands、exit_code、diff_stats、has_test_directory、has_test_dependencies、ci_workflows
- 硬护栏：max_changed_files、max_added_lines、sandbox 违规、已知安全模式

**LLM 层（语义判断，基于固定评分标准）：**
- 项目是否有测试基础设施？（tests/ dir + test deps + test CI workflow）
- 如果有 → worker 是否执行了对应测试？
- 如果没有 → worker 是否做了合理替代验证？（lint, pre-commit, type check）
- 改动范围与风险等级是否匹配？
- PR template 要求是否满足？
- Worker 自评与实际证据是否一致？

详细记录见归档文档。

---

## 17. 附录：关于 AI Manager 智能边界的思考（C2 轮测试驱动）

在对 `run_4a2896afee3b` 进行手动测试的过程中，我们遇到了两个关键的失败场景：
1.  **LLM 决策错误**：Manager 在判断需要重试后，生成了无效的命令 `retry --target-state retry`。
2.  **幂等性机制介入**：人类专家在修正了上述错误命令后，连续的 `retry` 请求被系统的幂等性保护机制拦截，返回 `duplicate: true`。

这两个场景暴露了让 AI Manager 直接操作低阶工具的内在风险和复杂性，并引发了关于其“智能边界”的深入思考。

### AI Manager 面临的“三重门”

一个纯粹的 LLM 在尝试进行自我诊断和修复时，面临着三个难以逾越的障碍：

1.  **上下文窗口的“窄门”**：LLM 的记忆力仅限于当前 Prompt。它不知道自己之前的操作历史，因此难以理解“重复请求”这类需要时间线上下文的状态。要使其理解，就必须在 Prompt 中构建复杂的历史摘要，这会极大地消耗 Token 和成本。
2.  **工具抽象的“幻门”**：对 LLM 而言，`retry` 只是一个被告知可以使用的符号。它不理解其背后连接的状态机、数据库和幂等性校验逻辑。让它直接生成完整的、带所有参数的 CLI 命令，无异于让它“猜测”一个复杂工具的所有内部规则。
3.  **经济成本的“铁门”**：让 LLM 自由“试错”的成本是高昂的。每一次无效的尝试都是一次昂贵的 API 调用。相比之下，一个确定性的规则（“凡是重试，状态就是 EXECUTING”）成本极低。

### 设计模式反思：从“万能的 CEO”到“CEO + COO”模式

这次实践证明，将 AI Manager 设计成一个无所不能、直接操作一切的“万能 CEO”是脆弱且昂贵的。一个更健壮、更实事求是的设计模式是**“CEO + COO”的职责分离模式**：

*   **AI Manager (CEO - 首席执行官)**: 负责**战略和意图**。它的核心价值在于理解模糊的自然语言、分析非结构化的错误日志、并做出**高级别的决策**（例如：“看起来是临时故障，我们应该重试”或“这个问题我没见过，需要人类专家介入”）。
*   **Orchestrator (COO - 首席运营官)**: 负责**战术和执行**。它的职责是接收 CEO 的战略意图，并将其翻译成**绝对安全、100% 有效**的具体动作。它负责处理幂等性、校验状态转换、执行命令、保障安全“护栏”。

### 未来改进方向：从“命令生成”到“意图表达”

为了更好地赋能 AI Manager，我们不应期望它能完美生成每一个 CLI 命令的参数。未来的改进方向应该是，将提供给 AI Manager 的工具从**低阶的、命令式的**（如 `retry`）升级为**高阶的、意图式的**。

例如，设计一个新工具 `propose_remediation(intent: str, justification: str)`。

在未来的工作流中：
1.  AI Manager 在分析失败后，不再生成 `retry` 命令，而是调用 `propose_remediation(intent="RETRY", justification="The failure seems temporary.")`。
2.  Orchestrator (COO) 收到这个结构化的**意图**。
3.  Orchestrator 内部的**确定性规则**开始工作：它根据 `RETRY` 的意图，自行构造并执行一个 100% 正确的命令，包括处理 `idempotency-key` 的逻辑。
4.  Orchestrator 甚至可以在执行前，将这个“意图和理由”通过 Bot 呈报给人类“董事长”进行最终审批。

这种模式既给了 AI **思考和形成意图的空间**，又用**确定性的规则**保证了最终执行的**绝对可靠**，是实现高级别人机协同智能的必由之路。
