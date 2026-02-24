# AgentPR Master Plan (Detailed)

> 更新时间：2026-02-23  
> 文档状态：实施中（Phase A 已落地）  
> 项目目标：把现有 Forge 集成流程升级为可持续运营的 AgentPR 系统，在保证最小改动策略下提高成功率、可控性与可回放性。

---

## 1. 核心目标与主要矛盾

### 1.1 核心目标

1. 让 “多 repo 小改动集成” 从人工驱动转为系统驱动。
2. 保持最小改动原则，尽量减少无关变更。
3. 在自动化执行中保留关键人工门禁（尤其 PR 创建与 merge）。
4. 对失败有明确分流：可重试、需人工、终止。

### 1.2 主要矛盾（已确认）

1. 不是“改代码能力不足”，而是“首轮读仓库 + 规则合规 + 状态追踪”成本高。
2. 需要统一的状态事实源与事件闭环，不再依赖手工表格维护。
3. 非交互执行（`codex exec`/`claude -p`）成功率未知，必须先做基线验证再扩控制面。

---

## 2. 已确认决策（实施基线）

1. 运行模式：`push_only`  
- 自动流程停在 `push`，不自动暴露对外 PR。
- `merge` 永远人工执行。

2. PR gate：`bot_approve_then_create_pr`  
- 需要人工审批后才可调用 `gh pr create`。
- 强制二次确认：`approve_open_pr --confirm`。

3. bot 平台：`telegram_first`  
- 先做 Telegram，后续可扩展 Slack。

4. 执行器：`codex exec` 默认，`claude -p` 备选。

5. 存储：MVP 用 SQLite，满足升级阈值后切 Postgres。

6. 预算护栏：MVP 启用，且全部可配置。

默认阈值（可调）：
1. `max_run_minutes = 90`
2. `max_iteration_count = 3`
3. `max_parallel_runs_per_day = 3`

---

## 3. 范围与非目标

### 3.1 当前范围

1. repo 级状态编排
2. CI/review 事件闭环
3. 人工可控 PR gate
4. prompt/skills 可测迭代

### 3.2 非目标（当前阶段不做）

1. 自动 merge
2. 初期上重型工作流基础设施（Temporal 级）
3. 初期替换执行引擎

---

## 4. 总体架构与方法

### 4.1 三层架构

1. Control Plane
- Telegram bot + 命令入口
- 自然语言解释 + 结构化动作执行

2. Orchestration Plane
- 状态机、事件去重、重试、超时、审计
- 统一管控 run 生命周期

3. Execution Plane
- 复用 `agentpr/forge_integration` 已有流程资产
- 非交互执行脚本/agent

### 4.2 方法论

1. 事实优先：push 后一切以 GitHub 事件为准。
2. 幂等优先：所有动作尽量可重复执行且无副作用放大。
3. 门禁优先：关键动作（开 PR）必须人工确认。
4. 最小改动优先：严格控制变更表面积。

---

## 5. 端到端流程（push_only + human gate）

1. 用户提交任务（repo、目标、prompt_version）。
2. orchestrator 创建 `run_id`，写入 `QUEUED`。
3. 进入 discovery，产出 `repo_contract`。
4. 通过 gate 后进入实现与本地验证。
5. 合规通过后 commit + push，状态到 `PUSHED`。
6. 人工 review：
- `deny`：终止或转人工修正
- `approve_open_pr --confirm`：创建 PR 并绑定 `pr_number`
7. 进入 CI/review 迭代（`CI_WAIT/REVIEW_WAIT/ITERATING`）。
8. 满足完成条件后转 `DONE`，merge 人工执行。

---

## 6. 状态机设计（MVP）

状态集合：

`QUEUED`  
`DISCOVERY`  
`PLAN_READY`  
`IMPLEMENTING`  
`LOCAL_VALIDATING`  
`PUSHED`  
`CI_WAIT`  
`REVIEW_WAIT`  
`ITERATING`  
`PAUSED`  
`DONE`  
`SKIPPED`  
`NEEDS_HUMAN_REVIEW`  
`FAILED_RETRYABLE`  
`FAILED_TERMINAL`

核心约束：
1. 非法状态转移必须直接报错。
2. 终态（`DONE/SKIPPED/FAILED_TERMINAL`）不允许继续动作。
3. 超过重试阈值必须分流到人工，不允许无限循环。

---

## 7. 事件模型（MVP）

| event_type | source | 关键字段 | 作用 |
|---|---|---|---|
| `command.run.create` | bot/api | run_id, repo | 初始化 run |
| `command.start.discovery` | bot/api | run_id | `QUEUED -> DISCOVERY` |
| `worker.discovery.completed` | worker | contract_path | `DISCOVERY -> PLAN_READY` |
| `command.start.implementation` | bot/api | run_id | `PLAN_READY -> IMPLEMENTING` |
| `command.local.validation.passed` | bot/api | run_id | `IMPLEMENTING -> LOCAL_VALIDATING` |
| `worker.push.completed` | worker | branch | `LOCAL_VALIDATING -> PUSHED` |
| `command.pr.create` | bot/api | run_id | 人工批准后触发开 PR |
| `command.pr.linked` | bot/api | run_id, pr_number | `PUSHED -> CI_WAIT` |
| `command.mark.done` | bot/api | run_id | 人工确认完成后转 `DONE` |
| `github.check.completed` | github | pr_number, conclusion | `CI_WAIT -> REVIEW_WAIT/ITERATING` |
| `github.review.submitted` | github | state | `changes_requested -> ITERATING` |
| `command.retry` | bot/api | run_id, target_state | 指定目标重试 |
| `command.pause` | bot/api | run_id | 转 `PAUSED` |
| `command.resume` | bot/api | run_id, target_state | 从 `PAUSED` 恢复 |
| `timer.timeout` | scheduler | run_id, step | 超时分流 |

硬规则：
1. 每事件必须有 `idempotency_key`。
2. `(run_id, idempotency_key)` 唯一约束。
3. 默认 key 必须是确定性的（event_type + run_id + payload hash）。
4. 重复事件可接受，但必须被判定为幂等重复，不重复执行状态副作用。

---

## 8. 数据模型（当前实现）

### 8.1 数据表

1. `runs`
- run_id, owner, repo, prompt_version, mode, budget_json, workspace_dir, pr_number, created_at, updated_at

2. `run_states`
- run_id, current_state, last_error, updated_at

3. `events`
- event_id, run_id, event_type, idempotency_key, payload_json, created_at  
- 唯一约束：`UNIQUE(run_id, idempotency_key)`

4. `step_attempts`
- run_id, step, attempt_no, exit_code, stdout_log, stderr_log, duration_ms, created_at

5. `artifacts`
- run_id, artifact_type, uri, metadata_json, created_at

### 8.2 存储策略

1. 当前：SQLite（单机单进程）。
2. 升级阈值：多 worker 并发、跨机部署、事件量明显增长。
3. 迁移策略：保留 DB 抽象，后续可切 Postgres。

---

## 9. 执行层契约（已落地）

### 9.1 prepare.sh

文件：`agentpr/forge_integration/scripts/prepare.sh`

参数：
`prepare.sh OWNER REPO [BASE_BRANCH] [FEATURE_BRANCH]`

行为：
1. 默认 workspace 在 `agentpr/workspaces`
2. 默认分支名唯一（避免历史叠加）
3. 支持传入显式分支（用于可回放）

### 9.2 finish.sh

文件：`agentpr/forge_integration/scripts/finish.sh`

参数：
`finish.sh "CHANGES" [PROJECT] [COMMIT_TITLE]`

行为：
1. 支持 repo 规范化 commit 标题传入
2. 避免固定标题导致规则冲突

### 9.3 分支命名规则

1. 默认：`feature/forge-<run_id>`
2. PR 页面会显示分支名，因此需遵守目标仓库规则
3. `repo_contract` 应包含 `branch_naming_rule`
4. push 若因分支命名被拒：自动重命名重试 1 次，仍失败转 `NEEDS_HUMAN_REVIEW`

### 9.4 非交互执行器契约（新增）

1. 新增命令：`python3 -m orchestrator.cli run-agent-step`
2. 支持执行器：`codex`、`claude`
3. 输入：`run_id` + `--prompt` 或 `--prompt-file` + 可选 `--agent-arg`
4. 行为：
- 自动在 run workspace 内执行
- 记录 `step_attempts(step=agent)`
- 失败时记录 `worker.step.failed`
5. 状态约束：
- 仅允许在 `DISCOVERY/IMPLEMENTING/LOCAL_VALIDATING/ITERATING`
- `QUEUED` 调用时自动推进到 `DISCOVERY`

---

## 10. Skills 设计（当前认知）

当前采用 3-skill 切片：

1. `repo-preflight-contract`
- 覆盖 discovery、贡献规则读取、PR checklist、风险标记、最小改动计划

2. `implement-and-validate`
- 覆盖实现、本地验证、合规复核

3. `ci-review-fix`
- 覆盖 CI 失败处理、review 评论迭代修复
- 可复用 GH Fix CI 的工作流思想

说明：
1. 3-skill 是最小可运营切片。
2. 后续可按复杂度再拆。

---

## 11. 安全、权限与环境隔离

### 11.1 权限策略

1. 默认仅允许写当前 run 目录。
2. 禁止越权写 `agentpr` 外目录。
3. 关键命令必须显式确认（开 PR 二次确认）。

### 11.2 依赖策略

1. 全局工具预装（brew/gh/python/bun/uv/rye/hatch/poetry/tox）。
2. 自动执行中禁止新增全局安装。
3. repo 依赖按仓库规范在局部环境安装（venv/node_modules）。

### 11.3 失败策略

1. 权限不足/环境缺失必须结构化报错。
2. 错误分流：`FAILED_RETRYABLE` 或 `NEEDS_HUMAN_REVIEW`。

---

## 12. 质量与可观测性

核心指标：
1. 首次 CI 通过率
2. 平均迭代轮次
3. `NEEDS_HUMAN_REVIEW` 比例
4. run.create 到 PR ready 中位耗时

审计要求：
1. 所有状态变化必须有事件记录
2. 所有脚本执行必须有 attempt 记录
3. 关键产物（contract/branch/log）必须可追踪

---

## 13. 实施路线图

### Phase A（已实现）

1. orchestrator 核心包
2. SQLite schema 与服务层
3. 状态机转移校验
4. prepare/finish 脚本执行集成
5. 非交互执行入口（`run-agent-step`）
6. CLI 命令集（含 `mark-done`、`idempotency-key`）

### Phase B（下一阶段）

1. Telegram bot 命令层
2. `approve_open_pr --confirm` 与 `gh pr create` 集成
3. GitHub 事件自动消费（check/review/comment）

### Phase C（增强）

1. 审计看板
2. 配置中心化（预算、重试、并发）
3. Postgres 迁移（按阈值）

---

## 14. 当前实现状态（2026-02-23）

### 14.1 已完成实现

新增目录：`agentpr/orchestrator/`

已实现文件：
1. `orchestrator/models.py`
2. `orchestrator/state_machine.py`
3. `orchestrator/db.py`
4. `orchestrator/service.py`
5. `orchestrator/executor.py`
6. `orchestrator/cli.py`
7. `orchestrator/__main__.py`
8. `orchestrator/README.md`

辅助更新：
1. `agentpr/README.md`（加入 quick start）
2. `agentpr/.gitignore`（忽略 `orchestrator/data/*.db` 和 `workspaces/`）

### 14.2 当前可用方法

1. 初始化数据库
```bash
python3 -m orchestrator.cli init-db
```

2. 创建并推进 run
```bash
python3 -m orchestrator.cli create-run --owner OWNER --repo REPO --prompt-version v1
python3 -m orchestrator.cli start-discovery --run-id <run_id>
python3 -m orchestrator.cli run-prepare --run-id <run_id>
python3 -m orchestrator.cli mark-plan-ready --run-id <run_id> --contract-path <path>
python3 -m orchestrator.cli start-implementation --run-id <run_id>
python3 -m orchestrator.cli mark-local-validated --run-id <run_id>
python3 -m orchestrator.cli run-finish --run-id <run_id> --changes "..." --project REPO --commit-title "feat(scope): ..."
```

3. 关联 PR 与记录检查
```bash
python3 -m orchestrator.cli link-pr --run-id <run_id> --pr-number 123
python3 -m orchestrator.cli record-check --run-id <run_id> --conclusion success --pr-number 123
python3 -m orchestrator.cli record-review --run-id <run_id> --state changes_requested
python3 -m orchestrator.cli mark-done --run-id <run_id>
```

4. 非交互执行（基线验证）
```bash
python3 -m orchestrator.cli run-agent-step --run-id <run_id> --engine codex --prompt-file <prompt.md>
# 或
python3 -m orchestrator.cli run-agent-step --run-id <run_id> --engine claude --prompt "<prompt>"
```

5. 运维命令
```bash
python3 -m orchestrator.cli list-runs
python3 -m orchestrator.cli show-run --run-id <run_id>
python3 -m orchestrator.cli pause --run-id <run_id>
python3 -m orchestrator.cli resume --run-id <run_id> --target-state DISCOVERY
python3 -m orchestrator.cli retry --run-id <run_id> --target-state IMPLEMENTING
```

### 14.3 已验证情况

1. 语法编译通过：`python3 -m compileall agentpr/orchestrator`
2. CLI 参数解析与命令执行通过
3. 关键状态转移路径可达
4. 非法状态转移会显式报错（不再静默）
5. 脚本执行失败可被记录并进入 `FAILED_RETRYABLE`
6. 幂等重复已验证（同 payload 默认 key 下第二次事件被判定 duplicate）
7. `mark-done` 路径已验证（`PUSHED -> DONE`）

### 14.4 当前未实现项

1. Telegram bot（命令层）
2. 自动 PR 创建命令链（含二次确认）
3. GitHub webhook/server 事件接入
4. 真实 repo 非交互成功率基线（3-5 个样本）尚未完成

---

## 15. 逻辑与框架总结（对外说明版）

### 15.1 大体逻辑

1. 把一个 repo 的生命周期建模为有穷状态机。
2. 所有动作先变成事件，再由服务层决定是否转状态。
3. 所有状态变化和执行结果都落库，保证可追踪。
4. push 后进入人工 gate，防止自动化直接暴露 PR。

### 15.2 方法框架

1. 设计层：状态机 + 事件契约 + 数据契约
2. 实现层：CLI 先行，bot/webhook 后接
3. 运行层：预算护栏 + 人工门禁 + 错误分流

### 15.3 为什么可用

1. 可操作：现在就能本地跑通核心链路
2. 可扩展：后续可接 bot/webhook/postgres
3. 可治理：有事件、状态、attempt、artifact 四类审计数据

---

## 16. 下一步执行清单（从现在开始）

1. 先做首轮 3-5 repo 非交互基线（`run-agent-step` + 统一 prompt），统计成功率与失败类型。
2. 根据基线结果调 prompt/分步策略，再进入 Telegram bot 开发。
3. 实现 Telegram bot 命令入口（status/detail/retry/pause/approve_open_pr）。
4. 实现 `approve_open_pr --confirm` 二次确认 + `gh pr create`。
5. 增加 GitHub 事件消费器（check/review/comment）。
6. 加入配置文件（预算、超时、重试、并发）。
