# AgentPR Master Plan (Manager-Worker Final Target)

> 更新时间：2026-02-25  
> 状态：Phase A 已落地，正在进入 Manager LLM 闭环阶段  
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

1. 不是缺“编排框架”，而是缺 Manager LLM 决策层闭环。
2. 不是缺“更多日志”，而是缺“可执行决策输入”到下一步动作的自动路由。
3. 不是缺“多 agent 并发”，而是缺“单 run 高质量稳定完成率”。

结论：当前方向正确，不需要推翻重写；应在现有 orchestrator 上补 Manager LLM 层。

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

每轮巡检做什么：
1. 拉取 pending runs。
2. 对每个 run 读取 state + latest digest。
3. 调用 manager 决策（规则优先，可选 LLM）。
4. 执行动作或升级人工。
5. 记录 decision trace。

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

## 12. 当前完成度（截至 2026-02-25）

已完成：
1. Orchestrator 核心：状态机、事件、幂等、SQLite。
2. Worker 执行链：`run-preflight` + `run-agent-step` + runtime grading。
3. Skills 链：`agentpr` skills 已接入。
4. PR gate：`request-open-pr` + `approve-open-pr --confirm` + DoD。
5. Bot 命令：`/list /show /status /pause /resume /retry /approve_pr`。
6. Webhook/轮询/审计与告警模板。
7. `skills-metrics` + `skills-feedback`。
8. Phase B1 规则版 manager loop 已落地：`manager-tick` / `run-manager-loop`。
9. Bot 双模路由已落地：`/` 开头按命令执行，非 `/` 文本按自然语言 intent 路由；每次回复附带固定 rules 尾注。
10. Manager 决策模式已扩展：`rules|llm|hybrid`（OpenAI-compatible function-calling，支持 Forge 网关）。
11. Bot 自然语言已接入 Manager LLM 路由（`rules|hybrid|llm`），并支持会话级 `run_id` 绑定（内存态）。
12. CLI 支持自动加载项目根目录 `.env`；新增 `.env.example` 作为标准环境模板。

未完成（关键）：
1. manager 与 bot 的 NL 对话上下文持久化（当前为进程内内存态）。
2. bot 侧 tool-calling 决策轨迹的结构化审计字段（当前主要体现在响应文本/audit response）。
3. LLM 决策模式在线上环境的策略校准（action 命中率、fallback 率、误路由率）。

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

## 15. 下一步（直接执行）

1. 将 bot 会话上下文从内存态升级为可恢复存储（SQLite 表或 run artifact）。
2. 扩展 bot 审计日志：单独记录 NL 决策输入摘要、action、fallback 原因。
3. 接通 B4：把 `skills-feedback` 自动转为 prompt/skill patch 草案（默认人工审批）。
4. 完善 bot-only runbook：从 `.env` 到上线检查的全链路脚本化。

---

## 16. 参考资料（用于架构校准）

1. OpenAI Function Calling：<https://platform.openai.com/docs/guides/function-calling>
2. OpenAI Structured Outputs：<https://platform.openai.com/docs/guides/structured-outputs>
3. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
4. OpenHands 文档（GitHub 集成与 agent 调用）：<https://docs.all-hands.dev/modules/usage/how-to/github-action>
5. GitHub Copilot coding agent（后台任务、会话日志、PR 流程）：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
6. Temporal 文档（durable long-running workflows）：<https://temporal.io/platform>
7. LangGraph 文档（agentic workflow 图）：<https://docs.langchain.com/oss/python/langgraph/overview>
8. OpenClaw Security（one-user trusted operator 边界）：<https://raw.githubusercontent.com/openclaw/openclaw/main/SECURITY.md>
