# AgentPR 测试操作手册

> 更新时间：2026-02-27
> 适用范围：C2 + C3 + P1 + P2 全部落地后的第二轮验证

---

## 0. 当前系统状态速览

| 运行 ID | Repo | 状态 | 说明 |
|---------|------|------|------|
| `run_2e642ed9c2f2` | HKUDS/DeepCode | **PAUSED** | C1 测试运行；代码已 push 到 `feature/forge-20260225-195854`；PR 请求文件已生成但 token 已过期 |
| `rerun_dexter_20260224_clean1` | virattt/dexter | **PUSHED** | 已到达 PR gate，无 pr_number（PR 未正式创建） |
| `rerun_mem0_20260224_clean1` | mem0ai/mem0 | **PUSHED** | 同上，pr_open_request 存在但 token 已过期 |

**本次测试目标**：选 1-2 个新 repo（建议先跑 mem0），全流程验证 V2 + 混合分级 + review triage + retry strategy。

---

## 1. 前置环境检查

```bash
# 1. 确认 Python 版本
python3.11 --version   # 需 3.11

# 2. 确认 codex 可用
codex --version || echo "codex not found"

# 3. 加载环境变量（项目根目录有 .env）
cd /Users/yi/Documents/Career/TensorBlcok/agentpr
source .env 2>/dev/null || true

# 4. Doctor 检查
python3.11 -m orchestrator.cli doctor
```

**关键环境变量**（参考 `.env.example`）：

| 变量 | 说明 | 当前推荐值 |
|------|------|-----------|
| `AGENTPR_MANAGER_API_KEY` | Manager LLM API key | 填 Forge/OpenAI key |
| `AGENTPR_MANAGER_MODEL` | Manager LLM 模型 | `gpt-4o-mini` |
| `AGENTPR_MANAGER_API_BASE` | Manager LLM API base | `https://api.openai.com/v1` 或 Forge URL |
| `AGENTPR_TELEGRAM_BOT_TOKEN` | Bot token（仅 bot 模式需要） | 可先不填，用 CLI 测试 |

---

## 2. 数据库与工作空间初始化

```bash
# 查看现有 runs（应看到历史 runs）
python3.11 -m orchestrator.cli list-runs

# 如需全新开始（⚠️ 会删除所有历史数据）
rm orchestrator/data/agentpr.db
python3.11 -m orchestrator.cli init-db
```

---

## 3. 场景 A：全流程新 Run（推荐先跑这个）

### 3.1 创建 Run

```bash
# 方式一：直接 CLI 创建（自动 kick，进入 QUEUED → EXECUTING）
python3.11 -m orchestrator.cli create-run \
  --owner mem0ai --repo mem0 \
  --prompt-version v1

# 查看 run_id
python3.11 -m orchestrator.cli list-runs --limit 1
# 记录 run_id，后面用 RUN_ID=... 引用
RUN_ID=<your_run_id>
```

### 3.2 启动自动推进（推荐方式）

```bash
# hybrid 模式：rules 兜底 + LLM 语义增强（需要 API key）
python3.11 -m orchestrator.cli run-manager-loop \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous \
  --codex-sandbox workspace-write \
  --interval-sec 30 \
  --max-loops 20

# 如果没有 API key，用 rules 模式
python3.11 -m orchestrator.cli run-manager-loop \
  --run-id $RUN_ID \
  --decision-mode rules \
  --skills-mode agentpr_autonomous
```

**预期状态流转**：
```
QUEUED → EXECUTING → PUSHED（成功）
                  → FAILED（失败，loop 会自动重试）
                  → NEEDS_HUMAN_REVIEW（需人工介入）
```

### 3.3 单步手动推进（调试用）

```bash
# Dry-run 查看 manager 打算做什么
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID --dry-run

# 实际执行一步
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous
```

---

## 4. 场景 B：继续 DeepCode 历史 Run（验证 resume）

DeepCode run `run_2e642ed9c2f2` 当前在 `PAUSED`，代码已 push 到远端分支 `feature/forge-20260225-195854`。

### 4.1 恢复运行

```bash
# Resume 到 EXECUTING，重新触发 finish/PR 流程
python3.11 -m orchestrator.cli resume \
  --run-id run_2e642ed9c2f2 \
  --target-state EXECUTING

# 验证状态
python3.11 -m orchestrator.cli show-run --run-id run_2e642ed9c2f2

# 触发 manager tick
python3.11 -m orchestrator.cli manager-tick \
  --run-id run_2e642ed9c2f2 \
  --decision-mode hybrid \
  --dry-run  # 先看看会做什么
```

### 4.2 直接推进到 PR gate

因为代码已完成，可以直接生成 PR 请求（跳过 worker 步骤）：

```bash
# 生成 PR 请求（会创建一个带 token 的 request 文件）
python3.11 -m orchestrator.cli run-finish \
  --run-id run_2e642ed9c2f2

# 查看生成的 request 文件 token
python3.11 -m orchestrator.cli show-run --run-id run_2e642ed9c2f2
```

### 4.3 批准 PR

```bash
# 查看 pending PR 请求
python3.11 -m orchestrator.cli pending_pr 2>/dev/null || \
  python3.11 -m orchestrator.cli list-runs | grep -A2 "PUSHED"

# 批准（需要 token，从 show-run 输出中获取）
python3.11 -m orchestrator.cli approve-open-pr \
  --run-id run_2e642ed9c2f2 \
  --confirm-token <TOKEN> \
  --confirm
```

---

## 5. 场景 C：验证混合分级（hybrid_llm）

### 5.1 注入已完成的 worker digest 测试分级

```bash
# 查看现有 digest 结构
python3.11 -m orchestrator.cli analyze-worker-output \
  --run-id run_2e642ed9c2f2

# 查看全局分级统计
python3.11 -m orchestrator.cli get-global-stats
```

### 5.2 强制 hybrid_llm 模式 manager tick

```bash
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --dry-run

# 预期：若 digest 有 PASS grade，会看到 run_finish 动作
# 预期：若 PASS 但 confidence=low，会看到 wait_human
```

---

## 6. 场景 D：验证 review triage（ITERATING 状态）

### 6.1 手动触发 ITERATING

```bash
# 模拟 review 事件（让 run 进入 ITERATING）
python3.11 -m orchestrator.cli record-review \
  --run-id $RUN_ID \
  --review-state changes_requested \
  --pr-number 1
```

### 6.2 观察 triage 决策

```bash
# Dry-run tick：预期看到 review_triage_action 影响决策
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --dry-run

# 有 LLM 时：会调用 triage_review_comment，根据 comment 内容决策
# fix_code → run_agent_step
# reply_explain / ignore → wait_human
```

---

## 7. 场景 E：验证 retry strategy（FAILED 状态）

```bash
# 把 run 手动打到 FAILED
python3.11 -m orchestrator.cli retry \
  --run-id $RUN_ID \
  --target-state FAILED

# Dry-run tick
python3.11 -m orchestrator.cli manager-tick \
  --run-id $RUN_ID \
  --decision-mode hybrid \
  --dry-run

# 有 LLM 时：会调用 suggest_retry_strategy
# should_retry=false → wait_human
# should_retry=true → retry + target_state
```

---

## 8. 场景 F：Bot 本地演练（无需 Telegram）

```bash
# 演练常用命令流程
python3.11 -m orchestrator.cli simulate-bot-session \
  --text "/overview" \
  --text "/list" \
  --text "/show $RUN_ID" \
  --nl-mode hybrid \
  --decision-why-mode hybrid

# 演练自然语言路由
python3.11 -m orchestrator.cli simulate-bot-session \
  --text "现在什么情况，有什么需要我做的" \
  --text "帮我重试最新的失败 run" \
  --nl-mode rules
```

**验证点**：
- `/overview` 输出应包含 `Pass rate: X%`、`Grades:` 和 `Top reasons:`
- `/show` 应展示 Decision Card（why_machine + why_llm）
- NL 路由 "帮我重试" 应路由到 retry 动作

---

## 9. 场景 G：通知系统验证

```bash
# 手动创建一条 manager_notification artifact
python3.11 -m orchestrator.cli notify-user \
  --run-id $RUN_ID \
  --message "测试通知：run 已完成" \
  --priority high

# 查看 artifact（应看到 manager_notification 类型）
python3.11 -c "
import sqlite3, json
conn = sqlite3.connect('orchestrator/data/agentpr.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT * FROM artifacts WHERE artifact_type='manager_notification' ORDER BY id DESC LIMIT 5\").fetchall()
for r in rows:
    print(r['run_id'], r['created_at'][:16], json.loads(r['metadata_json']).get('message'))
"

# Bot 模式下验证推送（需要启动 bot）：
# 在另一个终端启动 bot
python3.11 -m orchestrator.cli run-telegram-bot
# 30s 内应在 Telegram 收到通知消息
```

---

## 10. 关键指标与验收标准

### 10.1 功能验收检查项

| # | 检查项 | 验证方式 | 通过标准 |
|---|--------|----------|---------|
| 1 | V2 状态机 | 新 run 全流程 | QUEUED→EXECUTING→PUSHED，无 V1 状态出现 |
| 2 | 混合分级 | `analyze-worker-output` | PASS/NEEDS_REVIEW/FAIL + confidence 字段存在 |
| 3 | Confidence routing | `manager-tick --dry-run` on PASS run | high confidence → run_finish；low confidence → wait_human |
| 4 | NEEDS_REVIEW 升级 | 注入 NEEDS_REVIEW digest | manager 决策为 wait_human |
| 5 | Review triage | record-review + manager-tick --dry-run | 有 LLM：review_triage_action 影响决策 |
| 6 | Retry strategy | FAILED state + manager-tick --dry-run | 有 LLM：should_retry 影响决策 |
| 7 | 通知 artifact | notify-user CLI | `manager_notification` artifact 写入 DB |
| 8 | 通知推送 | Bot + 30s scan | 高优先通知在 Telegram 显示 `[HIGH]` 前缀 |
| 9 | `/overview` 统计 | simulate-bot-session | 输出含 `Pass rate:` + `Grades:` + `Top reasons:` |
| 10 | PR gate 完整流程 | 全流程到 PUSHED | `request-open-pr` 生成 token，`approve-open-pr --confirm` 创建 PR |

### 10.2 基线指标目标（第二轮验证后建立）

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 首次成功率 | ≥ 50% | QUEUED → PUSHED 无人工干预 |
| 平均 worker attempt 数 | ≤ 2 | 每个 run 的 agent step 次数 |
| NEEDS_HUMAN_REVIEW 中 miss 率 | ≤ 30% | 真正需要人工 vs 系统误升级 |
| Manager tick 响应时间 | ≤ 5s | 不含 worker 执行时间 |

---

## 11. 常用调试命令速查

```bash
# 查看 run 详情（状态、artifacts、events）
python3.11 -m orchestrator.cli show-run --run-id $RUN_ID

# 查看 worker 分级结果
python3.11 -m orchestrator.cli analyze-worker-output --run-id $RUN_ID

# 查看全局统计
python3.11 -m orchestrator.cli get-global-stats

# 查看 run 所有 artifacts（直接查 DB）
python3.11 -c "
import sqlite3, json
conn = sqlite3.connect('orchestrator/data/agentpr.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT artifact_type, created_at, uri FROM artifacts WHERE run_id=? ORDER BY id\", ('$RUN_ID',)).fetchall()
for r in rows: print(r['artifact_type'], r['created_at'][:16], r['uri'][-40:])
"

# 查看 run events（状态流转历史）
python3.11 -c "
import sqlite3, json
conn = sqlite3.connect('orchestrator/data/agentpr.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT event_type, created_at, substr(payload_json,1,100) FROM events WHERE run_id=? ORDER BY id\", ('$RUN_ID',)).fetchall()
for r in rows: print(r['created_at'][:16], r['event_type'], r[2])
"

# 暂停 run
python3.11 -m orchestrator.cli pause --run-id $RUN_ID

# 恢复 run 到 EXECUTING
python3.11 -m orchestrator.cli resume --run-id $RUN_ID --target-state EXECUTING

# 重试 run（从 EXECUTING 重新开始）
python3.11 -m orchestrator.cli retry --run-id $RUN_ID --target-state EXECUTING

# 手动推进 finish/push
python3.11 -m orchestrator.cli run-finish --run-id $RUN_ID
```

---

## 12. 已知问题与注意事项

### 12.1 DeepCode run 的特殊情况

- `run_2e642ed9c2f2`：代码已 push 到 `Yiiii0/DeepCode:feature/forge-20260225-195854`，工作已完成
- PR 请求文件存在但 **token 已过期**（生成于 2026-02-26，有效期 30 分钟）
- 要继续：需 `resume → EXECUTING`，再触发 `run-finish` 生成新 token，再 `approve-open-pr --confirm`

### 12.2 PUSHED 状态的 mem0/dexter run

- `rerun_mem0_20260224_clean1` 和 `rerun_dexter_20260224_clean1` 已 PUSHED 但无 pr_number
- 说明上次 approve-open-pr 被网络错误打断，PR 未实际创建
- 这两个 run 可以直接从 PUSHED 重新运行 `request-open-pr` + `approve-open-pr`

### 12.3 LLM 功能降级

- 若 `AGENTPR_MANAGER_API_KEY` 未配置，LLM 功能（triage、retry strategy、grade_worker_output）自动 fallback 到 `None`
- Triage = None → 默认走 rules（ITERATING 继续 run_agent_step）
- Retry strategy = None → 默认 retry 到 EXECUTING
- 不影响系统基本功能，只是缺少 LLM 增强

### 12.4 通知 scan 频率

- Bot loop 默认每 30 秒扫描一次（`AGENTPR_TELEGRAM_NOTIFY_SCAN_SEC=30`）
- CLI 测试不启动 bot，通知 artifact 会写入 DB 但不会推送到 Telegram
- 验证通知推送需要启动 `run-telegram-bot`

---

## 13. 第二轮验证推荐流程（5 步）

```
Step 1: 新建 run（mem0ai/mem0 或其他 repo）
Step 2: run-manager-loop --decision-mode hybrid --skills-mode agentpr_autonomous
Step 3: 等待自动推进到 PUSHED 或 NEEDS_HUMAN_REVIEW
Step 4: 记录：状态、attempt 数、耗时、reason_code（如有）
Step 5: 若 PUSHED → 人工 approve PR；若 NEEDS_HUMAN_REVIEW → 分析原因、决定 retry 或 pause
```

**验证重点**：
1. V2 状态机是否走通（无 V1 状态出现）
2. `hybrid_llm` 分级是否给出合理 confidence（需 API key）
3. `/overview` 统计是否正常更新
4. 通知是否在 bot 模式下推送

---

## 14. Telegram Bot 完整启动

```bash
# 确保 .env 中有 AGENTPR_TELEGRAM_BOT_TOKEN 和 AGENTPR_MANAGER_API_KEY
python3.11 -m orchestrator.cli run-telegram-bot

# 然后在 Telegram 中测试：
# /overview              → 应看到全局统计含 pass rate
# /list                  → 所有 runs
# /show <run_id>         → Decision Card
# 帮我创建 mem0ai/mem0 的 forge 集成   → NL create run
# 现在什么情况，需要我做什么           → NL overview
```

---

*文档由 Claude Code 基于代码审计和运行状态生成，2026-02-27*
