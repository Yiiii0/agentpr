# AgentPR Master Plan

> 更新时间：2026-03-07
> 状态：D3 边界修复完成。Test Run 7: 4/5 PASS+PUSHED tick 1。**下一步：D3.5 自我改进 + D4 运维化。**
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

1. **管线端到端已验证且改进中。** D3 Test Run 7: 4/5 PASS+PUSHED tick 1（vs D2 Run 6: 3/5 tick 1 + 2/5 tick 2）。不再需要 auto-recovery。
2. **主矛盾从"grading 架构"变为"Worker 自我改进"。** Grading 架构迁移已确认不需要（hybrid 是正确中间态）。真正的差距是 Worker 行为一致性：Paper2Poster 在 pip 失败后放弃验证，dexter/mem0 文档更新缺失。
3. **自我改进机制是核心。** 每次测试发现的问题应该编码回 skill instructions，让 Worker 下次不犯同样错误。D3.5 完成了这一闭环：self_review_checklist 加强、validation_requirements 加强、forge_rules 增加新 pitfalls。
4. **基础设施修复完成。** skills 自动升级（不再需要手动 --force），test infra 检测 ≥2 信号（防止 false positive）。Manager skill improvement proposal 系统已就位。
5. **Forge 422 仍阻塞**：使用 Codex 原生 provider，不影响核心管线。

**结论：自我改进闭环初步建立。下一步是验证改进效果 + D4 运维化。**

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
  |-- tool: notify_user
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
12. `retry_run(run_id, target_state)` — target_state 已约束为 enum：QUEUED/EXECUTING/ITERATING/DISCOVERY/IMPLEMENTING（D1 完成）
13. `analyze_worker_output(run_id)` — 混合分级
14. `get_global_stats()` — 全局运营统计
15. `notify_user(message, priority)` — 主动通知
16. `suggest_retry_strategy(run_id, diagnosis)` — LLM 失败诊断
17. `triage_review_comment(run_id, comment_body)` — LLM review 分流

### 4.2 目标动作集（延期 — 当前 17 动作集工作正常）

> **状态**：D1 已完成 ACI 优化（enum 约束、自动推导、显式反馈）。当前 17 动作集在 D2/D3 验证中工作良好，进一步简化延期到需求驱动时再做。

原设想的 7 动作简化版（备忘）：
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

### D1.6 Pipeline 修复 + 边界安全网（2026-03-04）
- **Decision audit logging**：rules/llm/guardrail 触发全记录 + facts_snapshot 字段
- **加宽 guardrail**：rules 和 LLM 都选 active action 但不一致时，rules 优先（防止 LLM 把 RUN_FINISH 改成 RUN_AGENT_STEP）
- **Smart dirty workspace**：PASS grade workspace 返回可恢复错误（不直接升级 NEEDS_HUMAN_REVIEW）
- **Dirty workspace auto-recovery**：自动执行 run-finish 恢复 PASS grade 的 dirty workspace
- **Stale state detection**：CI_WAIT >24h / REVIEW_WAIT >48h 自动升级 WAIT_HUMAN
- **Run-level mutex**：fcntl.flock per-run 锁，防止并发 manager-tick 竞争
- **CI/review 竞态修复**：GITHUB_CHECK_COMPLETED 仅从 CI_WAIT 触发转移，避免 review 先到时吃掉 CI 结果
- **No-changes commit 处理**：finish.sh 检测无 staged changes（exit 2）+ cli.py 返回 `no_staged_changes` reason_code
- **Post-push 验证**：finish.sh push 后验证 local HEAD == remote HEAD
- **Token scope 验证**：preflight 额外调用 `gh api user` 验证 token 有效性

### Bot 交互
- CLI 命令：/create /overview /list /show /status /pause /resume /retry /approve_pr
- NL 路由：rules/hybrid/llm 三模，会话级 run_id 绑定
- 主动通知：PUSHED/NEEDS_HUMAN_REVIEW/DONE/ITERATING 状态变更 + manager 通知

### Worker
- agentpr_autonomous 模式：worker 单次完成分析+实现+验证
- Skills 系统可用（markdown 定义，worker 可访问）
- Codex 支持 Forge provider（`.env` 配置 `AGENTPR_FORGE_BASE_URL` + `AGENTPR_FORGE_API_KEY`，不设则用默认 provider）
- **当前临时使用 Codex 原生 provider**：Forge `/v1/responses` 端点存在 422 bug（reasoning item `id` 必填但 OpenAI 规范中可选），已反馈给 Forge 团队，等修复后切回。详见 `docs/forge_422_bug_report.md`

### 代码结构（D3.5 后）
- `cli.py` (~2,845) + 4 子模块（cli_helpers/cli_pr/cli_inspect/cli_worker）
- `telegram_bot.py` (1,573) + telegram_bot_helpers (568)
- `runtime_analysis.py` (~1,764)
- `manager_tools.py` (~354) — 含 skill improvement proposal 系统
- `manager_loop.py` (~580) — 含 post-processing 自我改进检测
- orchestrator 总计 ~16.1K 行（26 个 .py 文件）
- skills 总计 ~438 行（3 skills × entry + references）

---

## 7. 系统级验收对照（距 North Star 差距分析）

### 目标能力 A：NL 下发任务 → 自动推进到 PUSHED/PR gate

**代码侧：✅ 已达成。** NL → create_run → auto-prepare → manager loop → PUSHED/gate 全通路。D1.6 修复了 dirty workspace 误升级和 guardrail 问题。
**运维侧：✅ 已验证。** D2 Test Run 6: 5/5 PASS+PUSHED。D3 Test Run 7: 4/5 PASS+PUSHED tick 1。涵盖 Python + TypeScript + 混合项目。
**常驻化：❌ 未做。** loop 仍需手动启动 `run-manager-loop`。→ D4 运维化。

### 目标能力 B：问"现在什么情况"→ 全局态势 + 下一步

**当前状态：部分达成。** `/overview` + `/show` + Decision Card 双层展示。
**缺口**：缺"下一步优先级队列"——自动告诉用户"最值得关注的 3 件事"。

### 目标能力 C：状态变更时主动通知

**当前状态：✅ 已达成。** Bot 关键状态通知 + manager_notification 推送。D1.6 加了 stale state 检测（CI_WAIT >24h / REVIEW_WAIT >48h 自动升级）。

### 目标能力 D：PR review comments → 主动问"人工还是自动修复"

**当前状态：基础具备。** CI/review 竞态已修复（D1.6 EC3）。triage_review_comment 分流已实现。
**缺口**：triage 结果直接影响 manager 决策，不经过用户确认对话。

### 目标能力 E：manager 自我迭代提案 → 人审批

**当前状态：✅ 初步闭环。** D3.5 完成：`propose_skill_improvement` 系统自动检测失败模式 → 生成 proposal artifact → `list-skill-proposals` CLI 可查。Skill instructions 已编码 Test Run 7 全部教训。
**缺口**：proposal → 自动 apply skill patch 尚未实现（当前需人工编辑 skill files）。Telegram 审批流程待 D4。

---

## 8. 实事求是：当前差距

### 8.1 ~~缺真实验证数据~~ → ✅ 已解决（D2 完成）

**D2 Test Run 6 结果：5/5 PASS + PUSHED。** 5 个仓库（pipecat/DeepCode/mem0/Paper2Poster/dexter），涵盖 Python + TypeScript + 混合项目：
- pipecat: 6 files, pytest 703 passed → PUSHED tick 1
- DeepCode: 3 files, pre-commit → PUSHED tick 1
- mem0: 3 files, make test 537 passed → PUSHED tick 1
- Paper2Poster: 2 files → PUSHED tick 2 (auto_recovery)
- dexter: 4 files, bun test → PUSHED tick 2 (auto_recovery)

**D2.5 修复了 commit 质量问题：** finish.sh 新文件未 stage（pipecat/mem0 PR 实际不完整）、lock files 被 commit、test command false positive。

**D3 Test Run 7 结果：4/5 PASS+PUSHED tick 1。**
- pipecat: 3 files (all new), pytest 874 passed → PUSHED tick 1. 优秀代码质量。
- DeepCode: 3 files, pre-commit ran → PUSHED tick 1. env_key 沿用 repo 既有模式。
- dexter: 4 files, bun test 23 passed + typecheck → PUSHED tick 1. 缺 env.example 条目。
- mem0: 3 files, pytest 537 passed → PUSHED tick 1. auto-stage 修复验证！forge.py 被正确 staged。
- Paper2Poster: NEEDS_HUMAN_REVIEW (missing_test_evidence). Worker 在 pip 失败后放弃验证。

**D3.5 自我改进修复完成：** skills 自动升级、test infra ≥2 信号、skill instructions 加强、manager skill improvement proposal 系统。

**当前最大缺口：Worker 行为一致性（验证不可跳过）和运维化（D4）。**

### 8.2 Orchestrator 不是"薄层"（不变，接受现实）

目标：orchestrator < 8K 行。实际：~15.5K 行。

**判断不变**：15.5K 是当前功能集的真实复杂度。D1.6 增加了 ~200 行（audit logging + guardrails + mutex + edge cases），但每行都有明确的 bug fix / safety net 价值。要降到 <10K 需要功能性决策（runtime grading → LLM），不是重构。

### 8.3 Manager LLM 角色定位（进展：guardrail 加宽）

**目标**：Manager 是真正的 LLM 大脑，orchestrator 只是执行层。
**现实**：D1.6 加宽了 guardrail——rules 和 LLM 都选 active action 时 rules 优先。这意味着当前 LLM 在 hybrid 模式下的自由度是：
- ✅ 可以把 rules 的 active action 降级为 WAIT_HUMAN（保守路线，由 LLM 判断）
- ❌ 不可以把 rules 的 RUN_FINISH 改成 RUN_AGENT_STEP（激进路线，被 guardrail 阻止）

这是正确的中间态。进一步松绑需要 D2 数据证明 LLM 判断可靠。

### 8.4 进度总览

| 项目 | 状态 | 优先级 |
|------|------|--------|
| D1：Manager 工具接口 ACI 优化 | **✅ 已完成** | — |
| D1.6：Pipeline 修复 + 边界安全网 | **✅ 已完成** | — |
| D2：真实验证（5 repos × 6 runs） | **✅ 已完成（5/5 PASS + PUSHED）** | — |
| D2.5：Commit 质量修复 | **✅ 已完成** | — |
| D3：边界修复 + Test Run 7 验证 | **✅ 已完成（4/5 PASS+PUSHED tick 1）** | — |
| D3.5：自我改进闭环 | **✅ 已完成** | — |
| **D4：运维化（常驻 + 持久化 + UX）** | **未做** | **⭐ 最高** |
| D-forge：Forge 切回 | 等外部修复 | 独立 |

---

## 9. 下一步（按优先级）

> 详细行动计划见 Section 18。以下是优先级摘要。

### D3 + D3.5（✅ 已完成）：边界修复 + 自我改进

**D3.1** — 边界修复（✅）：timeout partial changes → HUMAN_REVIEW, failure count 持久化
**D3.2** — Test Run 7 验证（✅）：4/5 PASS+PUSHED tick 1. finish.sh auto-stage 验证通过。
**D3.5** — 自我改进闭环（✅）：
- Skills 自动升级：`install_local_skills` 比较源/目标 mtime，源更新自动覆盖（不再需要 --force）
- Test infra 检测：≥2/4 信号才算有 test infrastructure（防止 pytest 作为 app 依赖的 false positive）
- manager skill improvement proposal 系统（`propose_skill_improvement` + `detect_skill_improvement_proposals` + `list-skill-proposals` CLI）
- Skill instructions 加强：validation 不可跳过、env.example 必须更新、unbound method call 警告、default model 要求

### D4（⭐ 最高优先）：运维化

| 优先级 | 项目 | North Star |
|--------|------|------------|
| ⭐ | Manager loop daemonization (systemd) | #2: Manager always online |
| 高 | Bot session persistence (SQLite) | #2: Manager always online |
| 高 | Review triage user confirmation | #1: Human in Telegram |
| 中 | Priority queue ("top 3 focus items") | #1: Human in Telegram |
| 中 | Self-iteration closed loop | #3: Worker autonomous |
| 低 | Grading → LLM (only if data shows need) | Infrastructure optimization |

**Grading 架构迁移延期**：当前 hybrid grading 对 5/5 repos 判断正确。"混合策略是正确的中间态"（insight #18）。LLM grading 引入非确定性 + 延迟 + 故障模式。仅在数据证明当前 grading 不足时再迁移。

### D4：运维化（D3 之后）

1. **Manager loop 常驻化**（systemd/daemon + 自恢复 + 日志轮转 + 健康检查）
2. **Bot 会话持久化**（run_id 绑定从内存态改为 SQLite）
3. **Review triage 用户确认**（triage 结果先问用户，再执行）
4. **优先级队列**（"你最应该关注的 3 件事"）
5. **自我迭代闭环**（skills-feedback → prompt/policy patch 草案 → 人审批）

### D-forge：Forge 切回（等外部修复）

Forge 422 修复后验证并切回。独立于 D3/D4。

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
- Dirty workspace + PASS grade → 自动 run-finish 恢复（D1.6），不误升级 NEEDS_HUMAN。
- CI_WAIT >24h / REVIEW_WAIT >48h → 自动升级 WAIT_HUMAN（stale state detection，D1.6）。
- 并发 manager-tick → per-run fcntl 文件锁互斥（D1.6），重复处理时 skip。
- CI/review 事件竞态 → 状态感知转移（D1.6），避免 review 先到时吃掉 CI 结果。
- Post-push 验证 → finish.sh 比对 local/remote HEAD，防止 push 失败但状态误报 PUSHED（D1.6）。

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

| 设计点 | OpenClaw 做法 | AgentPR 当前（D3.5） | 状态 |
|--------|-------------|---------------------|------|
| 谁是大脑 | LLM agent（Pi runtime） | Manager LLM agent with tools（hybrid 决策） | ✅ 已转变（D1） |
| Gateway 角色 | 薄层：消息路由 + 会话管理 | Orchestrator 薄层化进行中（~16K 行仍含 grading 逻辑） | ⚠️ 部分达成 |
| Skills | Markdown 文件，agent 自己决定调用 | Worker 自主调用 + skills 自动升级 + 自我改进 | ✅ 已对齐 |
| 主动性 | Heartbeat 模式：定时检查 | 定时巡检 + 状态通知 + manager 主动通知 | ✅ 已对齐 |
| 状态 | append-only 事件日志 | SQLite + 10 状态机 + decision audit logging | ✅ 保留 SQLite |
| 自我改进 | — | propose_skill_improvement → skill patch（D3.5） | ✅ 新增 |

**当前定位**：`LLM (大脑 + hybrid 决策) → Rules (安全护栏 + 证据提取) → Worker (自主执行 + skills)`。Hybrid 是验证后的正确中间态（#18 + #46）。

---

## 12. 决策锁定

1. Python 固定 `3.11`。
2. Worker 固定 `codex exec`。
3. 默认模式 `push_only`，`merge` 永远人工。
4. `create PR` 必须二次确认。
5. Manager 默认走 API function-calling。Provider：Forge 422 修复前用 Codex 原生，修复后切回 Forge。
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
9. OpenClaw 的核心启示：LLM 应该是大脑（with tools），不是规则引擎的附属品。**（注：#46 进一步明确——"大脑"不等于"所有东西都用 LLM"。基础设施应确定性，LLM 做语义决策。）**
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

**文档重构 + Forge 调试 + ACI 对标后：**
24. **工具接口设计 > 模型能力。** SWE-agent 论文核心结论：ACI 设计对 agent 性能的影响大于模型选择。`retry --target-state retry` 不是"LLM 太蠢"，是工具参数类型太松。用 enum + 自动推导 + 显式反馈修工具，不要加"意图抽象层"。
25. **不要过度设计分层。** 初始的"CEO+COO"模型是过度架构化。OpenHands、SWE-agent、Claude Code、GitHub Copilot Agent——没有一个用"意图层"隔离 LLM 和工具。正确做法：单 agent + 好的工具 + 安全拦截器。
26. **给 LLM 精简的信息比给它更多信息效果好。** Worker 文档重构（8 文件 → 3 skills 按需读）验证了这一点。SWE-agent 的"信息窗口化"原则一致。
27. **Skills 即单一来源。** Worker 的执行指南应收敛到 skills（按需检索），不应散布在多个重叠文件中。slim entry + skill references 优于 everything-in-one-prompt。
28. **外部依赖的 bug 要尽早隔离，不阻塞核心流程。** Forge 422 调试有价值（暴露了 Responses API schema 细节），但立刻切到原生 provider 继续前进。
29. **先纠偏认知再写代码。** Section 17 初版的"CEO+COO"看着合理但经不起对标检验。花时间对标 OpenHands/SWE-agent 比直接实现"意图层"省了更多时间。

**D1.6 Pipeline 修复后：**
30. **一次真实测试胜过十次代码审查（再次验证）。** dexter run 暴露的 dirty workspace bug 是纯静态分析不可能发现的——Worker PASS 了但 Manager 决定再跑一次 RUN_AGENT_STEP，这是 guardrail 设计缺陷 + dirty workspace 处理过于激进的组合问题。
31. **Guardrail 的正确策略是"rules 优先但 LLM 可降级"。** 不是"rules 全覆盖"也不是"LLM 全权"。LLM 可以把 active action 降为 WAIT_HUMAN（保守），但不可以把 RUN_FINISH 改为 RUN_AGENT_STEP（激进覆盖）。松紧度需要数据验证后调整。
32. **边界情况的修复成本远低于出问题的代价。** CI/review 竞态、concurrent tick、push 验证——每个修复只有 10-20 行代码，但对应的 bug 一旦触发就是"Worker 工作白费"或"状态污染"。
33. **审计日志不是可选项。** facts_snapshot 让 post-mortem 从"猜测发生了什么"变成"直接读 action_record"。decision_source 字段让每个决策可追溯。这 20 行代码的 ROI 是最高的。
34. **不要继续堆代码。去跑真实测试。** D1.6 之后管线代码已经足够健壮（5 个 pipeline fix + 5 个边界 fix = 10 个安全网）。继续加 feature 是逃避验证。

**D2 真实验证后（5/5 PASS + PUSHED）：**
35. **codex 的 turn.completed 是隐形断点。** Worker 输出 contract JSON 后 codex 视为完成——skill-1 SKILL.md 说"输出 JSON"，worker 忠实执行后停止。修复：2-phase prompt（Phase 1 分析=内部不输出，Phase 2 实现=唯一交付物）。"不输出"比"输出后继续"更可靠。
36. **LLM 对 error 字段过度反应。** `analyze_worker_output` 返回 `{"error": "missing_artifact"}` 时 LLM 立即判断 WAIT_HUMAN，即使 hint 说"正常情况"。修复：改 error 为语义更中性的 `"no_prior_worker_execution"` + 显式 hint。**给 LLM 的错误信息的措辞直接影响决策质量。**
37. **`git add -u` 是新文件的隐形杀手。** Worker 创建新 .py 文件但 finish.sh 的 `git add -u` 只 stage 已 tracked 文件修改。PR 看起来 PUSHED 成功但实际缺少核心文件。修复：白名单 auto-stage 新源码/文档文件 + 黑名单排除 lock files。
38. **test command 检测需要排除 read commands。** `rg -n "pytest|test" README.md` 被 `\bpytest\b` 匹配为 test execution。修复：先检查 read command patterns 再匹配 test patterns。**分类顺序即优先级。**
39. **Self-review checklist 是提升 worker 质量的最低成本手段。** DeepCode 用了 OPENAI_API_KEY 而非 FORGE_API_KEY，base_url 为空。一个 checklist 文件（20 行）让 worker 在输出前自检。效果待 D3 验证。
40. **Commit 质量审计必须检查实际 pushed 内容，不只看 grade。** Grade=PASS 但 PR 缺文件、包含 lock files、commit message 硬编码——这些都是 PASS 之后的质量问题。管线成功 ≠ 产出质量达标。

**D3 Test Run 7 + D3.5 自我改进后：**
41. **Skills 部署 ≠ Skills 更新。** D2.5 创建了 self_review_checklist.md 但未重新部署。Worker 从未看到。系统必须自动检测源比部署新并自动升级（mtime 比较），不依赖人工 `--force`。
42. **单信号 false positive 是分类问题的通病。** Paper2Poster 的 pytest 在 requirements.txt 里是应用依赖（评估海报质量），不是测试基础设施。≥2/4 信号阈值消除了这类误判。**分类器要求多重佐证，不要单点判断。**
43. **Worker 在失败后放弃是最危险的行为模式。** pip install 失败 → worker 直接报告 NEEDS_REVIEW 而不尝试任何验证。对比 DeepCode：worker 发现没有 test runner → 主动找 pre-commit 作为替代验证 → PASS。**Instructions 必须编码"永不放弃验证"作为硬规则。**
44. **Metrics 有局限但可接受。** `git diff --numstat HEAD` 不包含新 untracked 文件（added_lines=0），但 changed_files_count 和 changed_files 列表已足够支撑 grading 决策。不需要额外计入 untracked 文件行数。
45. **自我改进闭环 = 测试发现问题 → 编码回 skill instructions → 下次不犯。** 这是 AI centric 的核心：不是人写更多规则，而是每次运行的经验自动改善下一次的 worker 行为。Skills 是经验的持久化载体。
46. **Grading 架构迁移不需要做（至少现在不需要）。** Hybrid grading（rules 提取证据 + heuristic 语义覆盖）在 5/5 repos 上正确工作。AI centric ≠ 所有东西都用 LLM。基础设施应该是确定性的，Agent 应该是智能的。

---

## 15. 参考资料

1. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
2. GitHub Copilot coding agent：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
3. OpenHands：<https://docs.all-hands.dev/modules/usage/how-to/github-action>
4. OpenAI Function Calling：<https://platform.openai.com/docs/guides/function-calling>
5. SWE-agent：<https://github.com/SWE-agent/SWE-agent>
6. SWE-agent ACI 论文（ICLR 2025）：<https://arxiv.org/abs/2405.15793> — "Agent-Computer Interfaces Enable Automated Software Engineering"
7. OpenHands V1 SDK 论文：<https://arxiv.org/abs/2511.03690> — "A Composable and Extensible Foundation for Production Agents"
8. SWE-agent ACI 设计原则：<https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md>

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

## 16.1 Worker 文档重构记录（2026-03-01）

### 问题

Worker 接触到的文档体系存在三个核心问题：
1. **大量冗余**：同一条规则在 4 个不同文件中重复出现
2. **claude_code_prompt.md 过时**：包含 Legacy 手动批量模式、硬编码绝对路径
3. **Skills 与 prompt_template.md 职责不清**：autonomous 模式下 worker 自主调用 skills，但 prompt_template.md 又包含了 skills 已覆盖的全部内容

### 重构结果

| 维度 | 重构前 | 重构后 |
|------|--------|--------|
| Worker 需读文件数 | 8+ (prompt + workflow + prompt_template + 2 diffs + 3 skills + refs) | 3 skills + 各自的 refs（按需读） |
| 内嵌 prompt 大小 | 84 行 | 27 行 |
| 冗余指令 | 同一规则 3-4 处重复 | 每条规则只在一个地方 |
| Forge 上下文 | 散布在 4 个文件 | 集中在 skills 的 references 中 |

### 具体变更

- `claude_code_prompt.md`：84 → 27 行精简入口（角色 + 核心指令 + 指向 skills）
- Skill-1 新增：`forge_scenarios.md`（常量+场景）、`analysis_checklist.md`（分析清单 1.1-1.6）
- Skill-2 新增：`forge_rules.md`（8 hard + 5 important rules + pitfalls）、移入 `example_mem0.diff` + `example_dexter.diff`
- Skill-3 新增：`post_push_guide.md`（CI failure + reviewer comment 处理）
- `prompt_template.md` → `archive/prompt_template_v1.md`
- `workflow.md` → `archive/workflow_v1.md`
- `orchestrator/skills.py`：task_packet docs 字段移除 workflow/prompt_template 引用

### 16.2 Forge 422 Bug 记录

详见 `docs/forge_422_bug_report.md`。核心问题：Forge `/v1/responses` 端点在多轮工具调用时返回 422，因为 `ResponsesItemReasoning.id` 被定义为必填但 OpenAI 规范中可选。一行修复（`id: str` → `id: str | None = None`）。已反馈 Forge 团队，等修复后切回。临时方案：使用 Codex 原生 provider。

---

## 17. 附录：Agent-Computer Interface 设计原则（C2 测试 + 行业对标驱动）

### 17.1 原始事件

`run_4a2896afee3b` 手动测试中的两个失败：
1. Manager 生成了无效命令 `retry --target-state retry`（`target_state` 是自由文本，LLM 填了无效值）
2. 人工修正后，幂等性机制拦截连续 retry 请求

### 17.2 初始诊断（已修正）

最初我们将此归因于”LLM 不应直接操作工具”，提出了”CEO+COO”模式和”意图表达层”（`propose_remediation(intent, justification)`）。经过与行业最佳实践对标后，**发现初始诊断偏了**。

**偏了的部分：**

- **”CEO+COO” 双层分离是过度设计。** 看看表现最好的 agent 系统：OpenHands V1（单 agent + event loop + SecurityAnalyzer）、SWE-agent（单 agent + ACI）、Claude Code（单 agent + tools + permission mode）、GitHub Copilot Agent（单 agent + sandbox）。**没有一个用 “意图抽象层” 隔离 LLM 和工具**。
- **`propose_remediation` 本质就是 function calling。** 它和一个参数设计合理的 `retry_run(run_id)` 没有区别，多了一层不必要的间接。
- **”三重门”低估了当前 LLM 能力。** 128K+ 上下文窗口足够容纳操作历史；function calling 已经成熟；Haiku 级模型做 routing 决策成本很低。

### 17.3 正确的诊断：工具接口设计差

`retry --target-state retry` 的根因不是”LLM 不该直接调工具”，而是 **`retry` 工具的接口设计有问题**：

```python
# 当时：target_state 是自由文本，LLM 可以填任何值
retry_run(run_id: str, target_state: str)  # → LLM 写了 “retry”

# 应该：target_state 由 orchestrator 自动推导，或用 enum 约束
retry_run(run_id: str)  # orchestrator 根据当前状态自动决定
# 或
retry_run(run_id: str, strategy: Literal[“rerun”, “iterate”, “escalate”])
```

这正是 SWE-agent 的核心发现——**Agent-Computer Interface (ACI) 设计对 agent 性能的影响，大于模型选择本身**。

### 17.4 ACI 设计原则（来自 SWE-agent + OpenHands V1）

SWE-agent 论文（ICLR 2025）的核心假设：”We assume a fixed LM and focus on designing the ACI to improve its performance.” 四个原则：

1. **语法验证**：工具执行前先 validate，不让错误传播。
   - AgentPR 应用：Manager 工具参数用 JSON schema + enum 约束，无效调用在执行前就被拒绝并返回清晰错误。
2. **信息窗口化**：一次只给 agent 它需要的信息量，不做信息洪流。
   - AgentPR 应用：Worker 文档重构（slim entry + skill references 按需读取）已经做了这一步。Manager 工具返回值也应精简——返回”下一步建议”而非裸 JSON dump。
3. **搜索结果精简**：只返回关键信息，不返回完整上下文。
   - AgentPR 应用：`show_run` 返回结构化摘要 + 建议动作，不是整个 run 的所有事件。
4. **显式反馈**：空输出、成功、失败都要有明确反馈。
   - AgentPR 应用：幂等拦截不应只返回 `duplicate: true`，应返回 “This run was already retried 2 minutes ago, current state is EXECUTING. No action needed.”

OpenHands V1 SDK（2025）的架构选择：
- Agent 是**无状态的 event processor**
- 所有状态在 ConversationState（event log）
- 安全层是**拦截器**（execute 前检查），不是独立决策层
- **没有 “意图层” vs “执行层” 的分离——只有好的工具设计 + 安全拦截**

### 17.5 AgentPR 的正确定位

对比行业实践，AgentPR 当前架构其实没有根本性错误：

| 组件 | 行业做法 | AgentPR 当前 | 差距 |
|------|---------|-------------|------|
| Agent loop | 单 agent + structured tools | Manager LLM + action contract | ✅ 方向对 |
| 安全层 | 拦截器（execute 前检查） | safety contract + gate 执法 | ✅ 已有 |
| 工具接口 | 严格类型 + enum + 显式反馈 | 部分自由文本 + 裸错误返回 | **← 这是要改的** |
| 信息管理 | 精简上下文 + 按需检索 | Worker 已做（skills），Manager 未做 | **← 这也是要改的** |

**结论：不需要新的架构层，需要打磨现有工具接口。**

### 17.6 保留的正确认知

初始思考中有几点是对的，应保留：

1. **Orchestrator 负责确定性执行**（幂等性、状态转换、diff budget）——这和 OpenHands 的 SecurityAnalyzer + ConversationState 角色一致。
2. **LLM 做语义判断，rules 做硬约束**——混合策略是行业共识。
3. **信息架构影响 agent 质量**——Worker 文档重构已验证，给 LLM 精简、无冗余的信息比给它更多信息效果更好。

---

## 18. 下一步行动计划（2026-03-04 更新）

### Phase D1：Manager 工具接口 ACI 优化 — ✅ 已完成

**完成内容**：
- target_state enum 约束 + 自动推导
- 幂等拦截返回上下文（duplicate action 错误信息）
- retry target_state 校验 + 人话错误消息

### Phase D1.6：Pipeline 修复 + 边界安全网 — ✅ 已完成（2026-03-04）

**修复的 Pipeline Bug（D2 测试暴露）：**
1. Decision audit logging（rules_action + llm_action + guardrail + facts_snapshot）
2. 加宽 guardrail（LLM active action 与 rules 不一致时 → rules 优先）
3. Smart dirty workspace（PASS grade → 可恢复错误，不直接 NEEDS_HUMAN_REVIEW）
4. Dirty workspace auto-recovery（自动执行 run-finish）
5. Stale state detection（CI_WAIT >24h / REVIEW_WAIT >48h → WAIT_HUMAN）

**修复的边界情况：**
6. No-changes commit 处理（finish.sh exit 2 + cli.py reason_code）
7. Run-level mutex（fcntl.flock per-run 锁）
8. CI/review 竞态（状态感知 event 转移）
9. Token scope 验证（preflight `gh api user`）
10. Post-push 验证（local HEAD == remote HEAD）

### Phase D2：真实验证 — ✅ 已完成（2026-03-05）

**结果：5/5 PASS + PUSHED。** 6 轮测试迭代，5 个仓库全部成功。

| Repo | Grade | Files Changed | Tests | Push Tick | Notes |
|------|-------|---------------|-------|-----------|-------|
| pipecat | PASS | 6 | pytest 703 passed | tick 1 | PR 不完整：新 .py 文件未 stage（D2.5 修复） |
| DeepCode | PASS | 3 | pre-commit | tick 1 | env_key=OPENAI_API_KEY（质量问题，D2.5 checklist 覆盖） |
| mem0 | PASS | 3 | make test 537 passed | tick 1 | PR 不完整：forge.py 未 stage（D2.5 修复） |
| Paper2Poster | PASS | 2 | — | tick 2 auto_recovery | Clean |
| dexter | PASS | 4 | bun test | tick 2 auto_recovery | Clean |

**D2 关键发现与修复：**
- Test Run 1-4：Worker 停在 skill-1 contract JSON 输出 → codex 视为 turn.completed。修复：2-phase prompt（分析=内部，实现=交付物）
- Test Run 5：stale runs（Paper2Poster/DeepCode/dexter）LLM 误判 WAIT_HUMAN。修复：error message + system prompt
- Test Run 6：5/5 PASS。auto_recovery 正确触发（Paper2Poster/dexter dirty workspace → auto run-finish）

**基线指标：**
- 首次成功率：60%（3/5 tick 1 PUSHED）
- Auto-recovery 成功率：100%（2/2 dirty workspace → run-finish → PUSHED）
- 平均 worker attempt：1.0（每个 repo 仅 1 次 worker execution）
- Guardrail 触发率：0%（Test Run 6 无 guardrail override，所有 decision 由 rules 直接产生）

### Phase D2.5：Commit 质量修复 — ✅ 已完成（2026-03-06）

**修复内容：**
1. `finish.sh`：auto-stage 白名单新文件（.py .ts .md 等）+ 黑名单排除 lock files + 参数化 commit message
2. `runtime_analysis.py`：READ_COMMAND_PATTERNS 过滤（`rg -n “pytest”` 不再误判为 test execution）
3. `self_review_checklist.md`（NEW）：worker 完成前必查清单（git/代码质量/docs/验证）
4. `analysis_checklist.md`：must_update_docs CRITICAL 指令（CONTRIBUTING 要求 doc → 必须 true）
5. `manager_tools.py`：enriched evidence（test_command_samples + changed_files）

### Phase D3 + D3.5：边界修复 + 自我改进 — ✅ 已完成

**D3.1 — 边界修复** ✅：timeout partial changes → HUMAN_REVIEW, failure count artifact 持久化。
**D3.2 — Test Run 7 验证** ✅：4/5 PASS+PUSHED tick 1。finish.sh auto-stage 验证通过。
**D3.5 — 自我改进闭环** ✅：
- `skills.py`：`install_local_skills` 比较 mtime，源更新自动覆盖（解决 self_review_checklist 未部署问题）
- `runtime_analysis.py`：test infra 检测从 OR（任一信号）改为 ≥2/4 信号（解决 Paper2Poster pytest-as-app-dep false positive）
- `manager_tools.py`：skill improvement proposal 系统（pattern detection → artifact storage → CLI review）
- `skills.py` 入口 prompt：CRITICAL 规则"zero validation = NEEDS_REVIEW"
- Skill-2 references 全面加强：self_review_checklist（+8 items）、validation_requirements（+resilience section）、forge_rules（+5 pitfalls）
- Skill-1 analysis_checklist：env.example + changelog convention 检查

**Grading 架构迁移延期**：hybrid grading 是正确中间态。AI centric ≠ all LLM，基础设施应确定性。

### Phase D4：运维化（D3 之后）

1. **Manager loop 常驻化**：systemd unit + 自恢复 + 日志轮转 + 健康检查
2. **Bot 会话持久化**：run_id 绑定从内存态改为 SQLite
3. **Review triage 用户确认对话**：triage 结果先问用户，再执行（当前直接走 manager 决策）
4. **优先级队列**：`/overview` 加”最值得关注的 3 件事”
5. **自我迭代闭环**：skills-feedback → prompt/policy patch 草案 → 人审批

### Phase D-forge：Forge 切回（等外部修复）

**触发条件**：Forge 团队确认 `/v1/responses` 422 bug 已修复。
