# AgentPR Master Plan (Manager-Worker Final Target)

> 更新时间：2026-02-25（含架构审计）
> 状态：Phase B1-B3 已落地，架构审计发现"复杂度分配错位"，需重心转向 LLM 智能层
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

### 3.3 OpenClaw 的客观位置

1. OpenClaw 更像“聊天入口网关 + 个人助理控制面”。
2. 其文档明确是 one-user trusted operator 模型，不是默认多租户安全边界。
3. 可借鉴其“对话入口”体验，但 PR 生命周期编排核心仍应由我们现有 orchestrator 负责。

---

## 4. 目标架构（最终形态）

```
Human (Telegram NL + /commands)
        |
        v
Bot Adapter (command parser + NL router)
        |
        v
Manager LLM (API function-calling)
        |
        v
Orchestrator (state machine + events + policy + gates)
        |
        v
Worker Agent (codex exec + skills)
        |
        v
Repo workspace + GitHub
```

### 4.1 角色边界（必须锁定）

1. Manager LLM：做“决策与调度”，不直接改代码。
2. Worker：做“代码与验证”，不负责全局策略。
3. Orchestrator：做“唯一状态真相源 + 幂等事件 + 门禁执法”。

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

## 7. Manager Action Contract（最小集合）

MVP 仅开放下列 actions：

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

约束：
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
| **失败原因分析** | runtime_analysis.py 1,400行 regex 分类 | LLM 读 event stream 摘要 + 结构化证据，输出诊断 | 替代大量脆弱 regex，理解语义 |
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
| runtime grading（PASS/RETRYABLE/HUMAN_REVIEW） | LLM 基于证据判断分级，但安全违规一律 HUMAN_REVIEW 不交 LLM |
| diff 合理性（语义层面） | LLM 判断改动是否符合意图，但 diff budget 上限仍硬执行 |
| 是否需要人工介入 | LLM 建议，但 PUSHED/NEEDS_HUMAN_REVIEW gate 仍硬执行 |

### Decision Card 生成原则（更新）

1. `what/decision/evidence` 必须是机器事实（deterministic）。
2. `why_explained` 应由 LLM 生成：基于 evidence 给出可操作的解释（不是复述 reason_code）。
3. `suggested_actions` 应由 LLM 提供：具体的下一步选项（不是泛化的”human review”）。
4. 对外显示双层：`why_machine`（机器事实） + `why_llm`（智能解释 + 建议）。

---

## 11. 分阶段实施计划（直接通向最终形态）

## Phase B1（先做，低风险）

1. 新增 `manager_decision.py`：规则版 next_action 决策。
2. 新增 `manager_loop.py`：自动推进 run 生命周期。
3. Bot 保持命令式，先接 manager loop。

验收：
1. 单 run 可以自动从 `QUEUED` 推进到 `PUSHED/NEEDS_HUMAN_REVIEW`。
2. 全程无需人工敲 CLI。

## Phase B2（核心升级）

1. 新增 `manager_llm.py`：API function-calling 适配层（Forge provider）。
2. 在 `manager_decision` 增加 `mode=rules|llm|hybrid`。
3. 所有 manager 输出必须 schema 校验。

验收：
1. 同一条自然语言请求可稳定转为合法 action。
2. 不合法 action 被拒绝并返回可解释错误。

## Phase B3（你要的体验层）

1. Telegram 增加 NL 路由：命令优先，NL fallback 到 manager。
2. 支持“连续对话上下文 + run 绑定”。
3. 保持 `/command` 完整可用作为强控制通道。

验收：
1. 仅通过 Telegram 自然语言即可发起和跟踪 run。
2. 审批动作仍需显式确认。

## Phase B4（迭代闭环）

1. 将 `skills-feedback` 接入 manager 的迭代提案。
2. 自动产出 prompt/skill patch 草案。
3. 人工审批后再应用。

验收：
1. 每次 run 结束都有“可执行改进项”。
2. 迭代建议可追踪到具体失败模式。

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
   - runtime grading 引入 LLM 判断层（替代 ~800 行 regex 分类）。
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

## 14. 对话沉淀 Insights（保留）

1. 先解决主矛盾：闭环决策，不是堆框架。
2. “控制面稳定 > 执行面花哨”。
3. 最小改动能力来自：prompt + policy + gate 的协同，而不是单次模型能力。
4. 运行成功率的核心是环境与规则证据，不是“更强模型名”。
5. 可观测要分层：日常看 digest，失败看 event stream。

---

## 15. 下一步（基于架构审计重新排序）

### 优先级 1：让 LLM 做真正的决策（当前最大短板）

1. **runtime grading 引入 LLM 判断层**：保留结构化证据提取（event stream 解析、test 命令计数、diff 统计），但分级决策（PASS/RETRYABLE/HUMAN_REVIEW）改为 LLM 基于证据输出，安全违规保持硬覆盖。目标：替换 ~800 行 regex 分类逻辑。
2. **Decision Card 接入 `why_llm`**：LLM 基于 run_digest 证据，生成 2-3 句可操作解释和具体建议选项（不是泛化的 "human review"）。
3. **Review comment 智能分流**：webhook 收到 review 后，LLM 读评论内容，输出分流建议（改代码 / 回复解释 / nitpick 忽略），通知用户确认后执行。

### 优先级 2：简化控制面（降低维护成本）

4. **评估状态机精简**：考虑合并 DISCOVERY/PLAN_READY/IMPLEMENTING 为更粗粒度的 EXECUTING 状态，worker 内部管理子阶段。降低状态转移复杂度。
5. **评估 Skills 系统必要性**：如果 skills 只是预打包 prompt，考虑直接用 prompt template 版本管理替代 skill 安装/发现机制。

### 优先级 3：运营体验

6. 增加全局运营看板（CLI/Bot）：通过数、等待 PR 数、阻塞分布、最近 24h 失败率。
7. 主动通知策略调优：通知优先级、静默窗口、重复提醒阈值。
8. bot 会话上下文持久化（SQLite）。

### 优先级 4：闭环迭代

9. `skills-feedback` → prompt/policy patch 草案（默认人工审批）。
10. 重试策略 LLM 化：失败后 LLM 基于诊断结果，生成调整过的 prompt 或策略建议，而非盲重试。

---

## 16. 架构审计记录（2026-02-25 全量代码审查）

### 16.1 代码量分布

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

### 16.2 核心发现

**复杂度分配：**
- 确定性控制（状态机 + 规则 + regex + policy）：~70% 代码量
- LLM 智能层：~5% 代码量（manager_llm.py 仅做"从 N 选 1"）
- 基础设施（DB + CLI + Bot + webhook）：~25% 代码量

**关键判断：**

1. **Manager LLM 增量价值极低**：`manager_decision.py` 的 rules 已 100% 覆盖所有 state→action 映射。每个状态几乎只有 1 个合法动作 + `WAIT_HUMAN`。LLM 在此处的决策空间 ≈ 0。
2. **runtime_analysis.py 是最大的"用规则做 LLM 的活"**：1,400 行 regex 做失败分类，但 worker 的 event stream 包含丰富语义——LLM 能更好地理解"为什么失败、怎么修复"。
3. **13 个状态过多**：DISCOVERY → PLAN_READY → IMPLEMENTING 本质是 worker 执行的子阶段，不需要顶层状态机介入。Manager 在这三个状态的决策都是"继续推进"。
4. **Skills 是一层间接但非必要的抽象**：316 行代码的核心价值 = 在不同阶段给 worker 不同 prompt 片段，直接在 prompt template 中实现更简单。
5. **与"直接让 codex 全接管"的对比**：codex 单独做不了状态持久化、PR 生命周期、CI 反馈闭环、安全 gate——这些是 orchestrator 的真正价值。但 orchestrator 当前过度微管理 worker 的判断（`min_test_commands`、`max_changed_files`），worker 自身比 regex 更能判断"改动是否合理"。

### 16.3 与外部系统对比

| 系统 | 架构模式 | 对 AgentPR 的启示 |
|------|----------|-------------------|
| GitHub Copilot coding agent | 薄编排 + 强 agent + PR gate | agent 自主度高，编排层主要管 PR 生命周期 |
| OpenHands / SWE-agent | issue→agent→PR，编排层很薄 | 证明了"LLM agent + 简单生命周期"可以工作 |
| OpenClaw | 对话网关 + 个人助理 | "LLM 在对话中心"的模式值得借鉴 |
| Devin | 厚编排 + 厚 agent | 走全包路线，不适合轻量级 OSS 场景 |

**结论**：行业趋势是"编排层做生命周期管理和安全约束，智能决策交给 LLM"。AgentPR 当前反过来了——编排层承担了太多决策逻辑。

### 16.4 整体评价

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

## 17. 参考资料（用于架构校准）

1. OpenAI Function Calling：<https://platform.openai.com/docs/guides/function-calling>
2. OpenAI Structured Outputs：<https://platform.openai.com/docs/guides/structured-outputs>
3. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
4. OpenHands 文档（GitHub 集成与 agent 调用）：<https://docs.all-hands.dev/modules/usage/how-to/github-action>
5. GitHub Copilot coding agent（后台任务、会话日志、PR 流程）：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
6. Temporal 文档（durable long-running workflows）：<https://temporal.io/platform>
7. LangGraph 文档（agentic workflow 图）：<https://docs.langchain.com/oss/python/langgraph/overview>
8. OpenClaw Security（one-user trusted operator 边界）：<https://raw.githubusercontent.com/openclaw/openclaw/main/SECURITY.md>
9. SWE-agent（薄编排 + 强 agent PR 工作流）：<https://github.com/SWE-agent/SWE-agent>
