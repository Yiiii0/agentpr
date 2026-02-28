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

## 2. 目标架构

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

---

## 3. 当前已完成（what works）

### 核心流程
- 状态机 + 事件 + 幂等 + SQLite 持久化
- Worker 执行链：preflight → run-agent-step → runtime grading → push
- PR gate：request-open-pr + approve-open-pr --confirm + DoD 检查
- Manager loop：manager-tick / run-manager-loop（rules/llm/hybrid 决策）
- V2 唯一路径，V1 双轨代码已删除

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

### 代码结构（C4 瘦身后）
- `cli.py` (2,768) + 4 子模块（cli_helpers/cli_pr/cli_inspect/cli_worker）
- `telegram_bot.py` (1,573) + telegram_bot_helpers (568)
- `runtime_analysis.py` (1,712)
- orchestrator 总计 ~15.2K 行（27 个 .py 文件）

---

## 4. 实事求是：当前差距

### 4.1 最大的缺口：缺真实验证数据

**只有 1 次真实测试**（C1: HKUDS/DeepCode），结果 NEEDS_HUMAN_REVIEW/missing_test_evidence。所有 C2-C4 改进都是理论上的改进，没有第二个数据点验证。

这是当前最高优先级。在没有更多真实数据前，不应继续堆新功能。

### 4.2 Orchestrator 不是"薄层"

目标：orchestrator < 8K 行。实际：15.2K 行。

原因分析：
- `runtime_analysis.py` (1,712 行)：1,700 行 regex/rules 做的事情，一个 LLM 调用（带结构化证据包）可能做得更好。但迁移是功能性变更，不是纯重构。
- `cli.py` (2,768 行)：CLI 入口本身就承载了 15+ 命令的参数解析和执行逻辑，这是必要的复杂度。
- `cli_inspect.py` (966 行)：inspection/feedback 报告生成，大量 dict 拼接。
- `manager_llm.py` (968 行)：4 个 LLM 工具的 prompt 构建 + 响应解析。

**判断**：15K 不是"膨胀"，是当前功能集的真实复杂度。要真正降到 <10K 需要：(a) runtime grading 迁移到 LLM，(b) 精简 CLI 命令集，(c) 减少报告生成代码。这些都是功能性决策，不是重构能解决的。

### 4.3 Manager LLM 角色定位

**目标**：Manager 是真正的 LLM 大脑，orchestrator 只是执行层。
**现实**：Manager LLM 参与 6 个决策点（grading、confidence routing、triage、retry strategy、explain、NL routing），但 orchestrator 仍然承担大量规则决策。

**实质进展**：从"LLM 只做选择题"升级到"LLM 参与语义判断 + confidence routing"。这是正确的中间态，不需要激进地全面替换 rules。

### 4.4 其他未完成项

| 项目 | 状态 | 优先级 |
|------|------|--------|
| C1 第二轮真实验证 | **未做** | **P0** |
| Manager loop 常驻化（systemd/cron） | 未做 | P1 |
| Bot 会话上下文持久化（当前内存态） | 未做 | P2 |
| skills-feedback → prompt/policy patch 草案闭环 | 未做 | P3 |
| runtime grading 迁移到 LLM 层 | 未做 | P3 |

---

## 5. 下一步（按优先级）

### P0：第二轮真实验证

选 1 个新 repo（建议 mem0ai/mem0），完整跑通 QUEUED → PUSHED → PR 创建。

验证重点：
1. V2 状态机走通（无 V1 状态出现）
2. hybrid_llm 分级给出合理 confidence（需 API key）
3. review triage + retry strategy 是否在真实场景有效
4. 通知是否在 bot 模式下正常推送

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

- skills-feedback + 失败模式分析 → prompt/policy 改进草案
- 默认"建议→审批→应用"，不自动盲改

---

## 6. 决策锁定

1. Python 固定 `3.11`。
2. Worker 固定 `codex exec`。
3. 默认模式 `push_only`，`merge` 永远人工。
4. `create PR` 必须二次确认。
5. Manager 默认走 API function-calling。
6. 混合策略：Rules 负责硬护栏 + 证据提取，LLM 负责语义判断 + 建议。不做全量替换。

---

## 7. 安全与隔离

1. Worker 写权限限定在 repo + `.agentpr_runtime` + `/tmp`。
2. 仓库外写入禁用，仓库外读取仅允许白名单。
3. `PUSHED -> open PR` 必须人工双确认。
4. 这套策略对"本地单人运营 + 多 OSS 仓库"是合理的。

---

## 8. 沉淀的核心认知

1. **先用真实数据验证，再做架构调整。** C1 一次 DeepCode 测试暴露的 rg 隐藏目录问题，比任何静态审查都有效。
2. **混合策略是正确的中间态。** Rules 做证据提取 + 硬护栏（不可被 LLM 覆盖），LLM 做语义判断。不是全替换 regex，是分层。
3. **Confidence routing 让 LLM 在正确位置发挥作用。** 不是"LLM 全权决策"（太激进），也不是"LLM 只做选择题"（无价值）。
4. **"精密工厂管理聪明工人"是反模式。** 应该是"轻量生产线 + 自主工人 + 关键检查点"。
5. **代码量增长不等于膨胀，但超过阈值时维护成本急升。** C4 做了结构优化但总量未降，进一步需要功能性决策。
6. **Contract（skill-1 输出）不应作为人工审核 gate。** Worker 内部产出、内部消费，有 blocker 时 worker 自己停止报告。
7. **通知"最后一公里"容易被忽略。** 产生 artifact 只是一半，推送到用户才是闭环。
8. **如果做的不对，再大的代价也是最小的代价。** 先跑通第一个 PR 再说。

---

## 9. 参考资料

1. OpenAI Codex CLI：<https://developers.openai.com/codex/cli/>
2. GitHub Copilot coding agent：<https://docs.github.com/en/copilot/concepts/about-copilot-coding-agent>
3. OpenHands：<https://docs.all-hands.dev/modules/usage/how-to/github-action>

---

## 10. C1 测试记录摘要

| 项 | 值 |
|-----|-----|
| Run ID | `run_2e642ed9c2f2` |
| Repo | HKUDS/DeepCode |
| 最终状态 | `NEEDS_HUMAN_REVIEW` |
| 原因 | `missing_test_evidence`（DeepCode 无测试基础设施） |
| 改动 | 4 files, +48/-10, pre-commit 全过 |
| 暴露问题 | (1) rg 不搜 .github/ 隐藏目录（已修复）(2) min_test_commands 太刚性（已改为混合分级）|
| 结论 | 代码 push 完成，PR gate 按预期拦截。验证了基本流程可跑通。 |

详细记录见归档文档。
