# AgentPR Master Plan (Detailed)

> 更新时间：2026-02-24  
> 文档状态：实施中（Phase A 已落地）  
> 项目目标：把现有 Forge 集成流程升级为可持续运营的 AgentPR 系统，在保证最小改动策略下提高成功率、可控性与可回放性。

---

## 0. 决策记忆（锁定项）

1. 运行时 Python 固定 `python3.11`。
2. 非交互执行器当前阶段固定 `codex exec`。
3. baseline 验证仓库固定 2 个：`mem0ai/mem0`、`virattt/dexter`。
4. 本机 codex 默认配置已确认：`model=gpt-5.3-codex`、`model_reasoning_effort=xhigh`，manager 默认不传 `--codex-model`。
5. manager MVP 运行预设：`--codex-sandbox danger-full-access`（由 preflight + 运行时隔离策略兜底）。

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
3. 非交互执行（`codex exec`）成功率未知，必须先做基线验证再扩控制面。

---

## 2. 已确认决策（实施基线）

1. 运行模式：`push_only`  
- 自动流程停在 `push`，不自动暴露对外 PR。
- `merge` 永远人工执行。

2. PR gate：`bot_approve_then_create_pr`  
- 需要人工审批后才可调用 `gh pr create`。
- 强制二次确认：`approve-open-pr --confirm`。

3. bot 平台：`telegram_first`  
- 先做 Telegram，后续可扩展 Slack。

4. 执行器：当前阶段固定 `codex exec`。

5. 存储：MVP 用 SQLite，满足升级阈值后切 Postgres。

6. 预算护栏：MVP 启用，且全部可配置。

7. baseline 仓库：固定 `mem0` 与 `dexter`。

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
- `approve-open-pr --confirm`：创建 PR 并绑定 `pr_number`
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

1. 新增命令：`python3.11 -m orchestrator.cli run-agent-step`
2. 执行器固定：`codex exec`
3. 输入：`run_id` + `--prompt` 或 `--prompt-file` + 可选 `--agent-arg`
4. 行为：
- 默认先执行 preflight（`.git` 写权限、工具链、依赖源网络）  
- preflight 不通过则直接收敛到 `NEEDS_HUMAN_REVIEW`
- 自动在 run workspace 内执行
- 支持显式指定 `codex` 参数（sandbox/model/full-auto）
- 记录 `step_attempts(step=agent)`
- 失败时记录 `worker.step.failed`
- 生成结构化 runtime report，并自动产出 verdict（`PASS/RETRYABLE/HUMAN_REVIEW`）
- verdict 元数据写入 run artifact（`grade/reason_code/next_action`）
- 仅当 verdict=`PASS` 时才应用 `--success-state`
4.1 codex 参数约束：
- `--codex-sandbox`: `read-only` / `workspace-write` / `danger-full-access`
- `--codex-model`: 透传给 codex CLI，默认沿用本地 profile
- `--no-codex-full-auto`: 关闭 full-auto（默认开启）
4.2 命令映射（当前实现）：
- 默认：`codex exec --sandbox danger-full-access --ask-for-approval on-request "<prompt>"`
- 非 `workspace-write` 但保持自动执行：`codex exec --sandbox <mode> --ask-for-approval on-request "<prompt>"`
- 设定模型：`codex exec --sandbox <mode> ... --model <model> "<prompt>"`
- 关闭 full-auto：`codex exec --sandbox <mode> [--model <model>] "<prompt>"`
4.3 preflight 参数：
- `run-preflight` 与 `run-agent-step` 都支持 `--codex-sandbox`
- 当 `--codex-sandbox read-only` 时，preflight 直接失败（阻断建环境/测试）
5. 状态约束：
- 仅允许在 `DISCOVERY/IMPLEMENTING/LOCAL_VALIDATING/ITERATING`
- `QUEUED` 调用时自动推进到 `DISCOVERY`
6. 当前形态说明：
- 目前是“单次大 prompt”执行，不是 skills 分段执行
- `run-agent-step` 已带自动分级与状态分流（失败或证据不足会自动收敛到 `FAILED_RETRYABLE` / `NEEDS_HUMAN_REVIEW`）

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
3. 当前代码尚未接入 skills 执行链；baseline 仍是单体 prompt 模式。

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
4. worker 运行时强制本地化缓存/数据目录（`<repo>/.agentpr_runtime`）。
5. Python 侧强制 `PIP_REQUIRE_VIRTUALENV=true`，避免误写全局 site-packages。
6. 可扩展隔离策略文件：`orchestrator/runtime_env_overrides.json`（新增工具优先改配置而非改代码）。

### 11.3 失败策略

1. 权限不足/环境缺失必须结构化报错。
2. 错误分流：`FAILED_RETRYABLE` 或 `NEEDS_HUMAN_REVIEW`。
3. repo 不在 `workspace_root` 范围内时 preflight 直接失败（阻断越界执行）。

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

### Phase B（进行中）

1. Telegram bot 命令层（已完成 MVP：`run-telegram-bot` + 基础命令）
2. `request-open-pr` / `approve-open-pr --confirm` 与 `gh pr create` 集成（已完成 CLI 版本）
3. GitHub 事件自动消费（已完成轮询版：`sync-github` + webhook 版：`run-github-webhook`）

### Phase C（增强）

1. 审计看板
2. 配置中心化（预算、重试、并发）
3. Postgres 迁移（按阈值）

---

## 14. 当前实现状态（2026-02-24）

### 14.1 已完成实现

新增目录：`agentpr/orchestrator/`

已实现文件：
1. `orchestrator/models.py`
2. `orchestrator/state_machine.py`
3. `orchestrator/db.py`
4. `orchestrator/service.py`
5. `orchestrator/executor.py`
6. `orchestrator/preflight.py`
7. `orchestrator/cli.py`
8. `orchestrator/__main__.py`
9. `orchestrator/README.md`
10. `orchestrator/github_sync.py`
11. `orchestrator/telegram_bot.py`
12. `orchestrator/github_webhook.py`

辅助更新：
1. `agentpr/README.md`（加入 quick start）
2. `agentpr/.gitignore`（忽略 `orchestrator/data/*.db` 和 `workspaces/`）
3. `agentpr/deploy/`（systemd / supervisord 模板）

### 14.2 当前可用方法

1. 初始化数据库
```bash
python3.11 -m orchestrator.cli init-db
python3.11 -m orchestrator.cli doctor --require-codex
```

2. 创建并推进 run
```bash
python3.11 -m orchestrator.cli create-run --owner OWNER --repo REPO --prompt-version v1
python3.11 -m orchestrator.cli start-discovery --run-id <run_id>
python3.11 -m orchestrator.cli run-prepare --run-id <run_id>
python3.11 -m orchestrator.cli mark-plan-ready --run-id <run_id> --contract-path <path>
python3.11 -m orchestrator.cli start-implementation --run-id <run_id>
python3.11 -m orchestrator.cli mark-local-validated --run-id <run_id>
python3.11 -m orchestrator.cli run-finish --run-id <run_id> --changes "..." --project REPO --commit-title "feat(scope): ..."
```

3. 开 PR（双确认 gate）与记录检查
```bash
python3.11 -m orchestrator.cli request-open-pr --run-id <run_id> --title "feat(scope): ..." --body-file forge_integration/pr_description_template.md
python3.11 -m orchestrator.cli approve-open-pr --run-id <run_id> --request-file <request.json> --confirm-token <token> --confirm
python3.11 -m orchestrator.cli record-check --run-id <run_id> --conclusion success --pr-number 123
python3.11 -m orchestrator.cli record-review --run-id <run_id> --state changes_requested
python3.11 -m orchestrator.cli mark-done --run-id <run_id>
```

3.1 手动关联 PR（备用路径）
```bash
python3.11 -m orchestrator.cli link-pr --run-id <run_id> --pr-number 123
python3.11 -m orchestrator.cli record-check --run-id <run_id> --conclusion success --pr-number 123
python3.11 -m orchestrator.cli record-review --run-id <run_id> --state changes_requested
python3.11 -m orchestrator.cli mark-done --run-id <run_id>
```

4. 非交互执行（基线验证）
```bash
python3.11 -m orchestrator.cli run-preflight --run-id <run_id>
python3.11 -m orchestrator.cli run-agent-step --run-id <run_id> --prompt-file <prompt.md>
# 或
python3.11 -m orchestrator.cli run-agent-step --run-id <run_id> --prompt "<prompt>"
```

5. 运维命令
```bash
python3.11 -m orchestrator.cli list-runs
python3.11 -m orchestrator.cli show-run --run-id <run_id>
python3.11 -m orchestrator.cli doctor
python3.11 -m orchestrator.cli pause --run-id <run_id>
python3.11 -m orchestrator.cli resume --run-id <run_id> --target-state DISCOVERY
python3.11 -m orchestrator.cli retry --run-id <run_id> --target-state IMPLEMENTING
python3.11 -m orchestrator.cli sync-github --dry-run
python3.11 -m orchestrator.cli sync-github --loop --interval-sec 120
python3.11 -m orchestrator.cli run-telegram-bot --allow-chat-id <chat_id>
python3.11 -m orchestrator.cli run-github-webhook --host 0.0.0.0 --port 8787
python3.11 -m orchestrator.cli cleanup-webhook-deliveries --keep-days 30
```
说明：可变命令默认启用 startup doctor gate；仅在调试时使用全局 `--skip-doctor` 跳过。

### 14.3 已验证情况

1. 语法编译通过：`python3.11 -m compileall agentpr/orchestrator`
2. CLI 参数解析与命令执行通过
3. 关键状态转移路径可达
4. 非法状态转移会显式报错（不再静默）
5. 脚本执行失败可被记录并进入 `FAILED_RETRYABLE`
6. 幂等重复已验证（同 payload 默认 key 下第二次事件被判定 duplicate）
7. `mark-done` 路径已验证（`PUSHED -> DONE`）
8. preflight 门禁已接入（环境不满足时快速失败并转人工）
9. preflight 已覆盖 sandbox 策略检查（`read-only` 会被明确拦截）
10. 每次 agent 执行会产出结构化 runtime report（命令样本、测试/推送信号、安全违规信号）
11. runtime report 已包含自动分级判定与原因码（`PASS/RETRYABLE/HUMAN_REVIEW`）
12. PR gate CLI 已实现：`request-open-pr`（生成 token）+ `approve-open-pr --confirm`（二次确认后创建 PR 并自动 link）
13. PR gate 已做本地 smoke 验证：token/过期/确认参数校验路径与失败分流路径可用
14. Telegram bot MVP 已实现：`/list`、`/show`、`/status`、`/pending_pr`、`/approve_pr`、`/pause`、`/resume`、`/retry`
15. GitHub 轮询同步已实现：`sync-github` 支持 dry-run/loop，把 check/review 结果映射回状态机事件
16. GitHub webhook server 已实现：签名校验 + 事件解析 + 状态机映射（check/review）
17. Webhook 重放保护已实现：按 `X-GitHub-Delivery` 去重并落库（`webhook_deliveries`）
18. Webhook 失败重试通路已打通：处理失败会释放去重锁并返回 retryable 响应
19. Telegram allowlist 默认强制：未配置 `--allow-chat-id` 时需显式 `--allow-any-chat` 才可启动
20. 部署模板已提供：`deploy/systemd/*` 与 `deploy/supervisord/agentpr-manager.conf`
21. startup doctor 已实现：`doctor` 命令支持 auth/network/tooling/secrets 可配置检查
22. mutable 命令默认启用 doctor gate，失败将快速阻断并给出修复入口（`doctor`）
23. deploy 模板已接入启动前 gate：systemd `ExecStartPre` / supervisord `doctor && main process`

### 14.4 当前未实现项

1. Telegram bot 生产加固（审计日志、限流与命令级权限）
2. GitHub webhook 生产加固（公网入口、监控告警、持久化可观测性）
3. skills 执行链接入（repo-preflight-contract / implement-and-validate / ci-review-fix）
4. agent 自动状态收敛的可配置策略（当前已实现默认分级分流，待补可调阈值）

### 14.5 Baseline 结果（`mem0` + `dexter`）

1. 结论：2/2 均完成非交互执行并产出改动，结果分类均为 `NEEDS REVIEW`。
2. `mem0`：
- agent 第 1 次失败（连接/环境），第 2 次成功
- 成功耗时约 6m20s
- 产出 4 个改动文件（代码 + 测试 + 文档）
- 明确读取了 `CONTRIBUTING.md`、`PULL_REQUEST_TEMPLATE.md`、CI workflow 并尝试执行 `make lint` / `make test`
3. `dexter`：
- agent 第 1 次成功
- 成功耗时约 4m59s
- 产出 6 个改动文件
- 明确读取了 `AGENTS.md`、CI workflow 并尝试执行 `bun run typecheck` / `bun test`
4. 共性阻塞：
- 当前运行环境对 `.git` 写入受限，`finish.sh` 无法完成 commit/push
- 部分仓库依赖安装受网络/权限限制，导致无法完整跑 lint/test
5. 工程修正：
- `finish.sh` 的 `COMMIT_TITLE` 单行校验已修复（避免误报）
6. 状态修正：
- 两个 baseline run 已手动收敛到 `NEEDS_HUMAN_REVIEW`

### 14.6 环境可执行性结论（基于 baseline）

1. “能改代码”已验证，但“能完整建环境+跑测试+提交”未完全验证。
2. 主阻塞来自运行环境而非代码逻辑：
- 依赖源网络不可达时，安装失败，测试只能部分执行或无法执行
- worker 运行时的 sandbox 策略会影响可执行能力
3. preflight 已经接入，可在 worker 启动前识别这些阻塞并快速失败。
4. 仍需在目标运行环境中验证：
- 依赖安装成功率
- repo 规定测试命令完整通过率
- commit/push 通过 manager gate 的稳定性

### 14.7 当前主要问题（实事求是）

1. 执行环境问题仍是第一阻塞：
- 外网依赖源不可达会直接导致“无法证明测试通过”
- `.git` 写权限受限会导致“无法提交/推送”
- 现已通过 startup doctor + repo preflight 双门禁将失败前置，但仍依赖真实运行环境可达性
 - 实测样例：`doctor --require-codex` 失败于 `gh.auth + net.*`，`run-preflight` 在 mem0 上 `git.write` 为通过
2. 当前仍是“单次大 prompt”：
- skills 边界已定义，但未接入自动执行链
3. manager 能力还未闭环：
- CLI/bot/轮询/webhook 已接通，且守护模板已提供；但生产部署与监控告警尚未落地
4. 质量证据结构化仍不足：
- 结构化 report + 自动分级已上线，但判定阈值仍需参数化（如 test 证据阈值、重试上限联动）

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

1. 固化 startup doctor 作为 manager 启动前置 gate（将 CI/守护进程启动脚本统一接入 `doctor`）。
2. 在“可访问依赖源 + `.git` 可写”的环境重跑 `mem0`、`dexter`，先拿到真实非交互成功率基线。
3. 将自动分级判定参数化（阈值、模式、白名单规则），并与重试策略联动。
4. 为 Telegram bot 增加部署与安全配置（强制 allowlist、systemd/pm2/supervisord）。
5. 为 webhook 增加公网接入方案与监控告警（保留 `sync-github` 作为 fallback）。
6. 增加配置文件（预算、超时、重试、并发）并将 bot/sync/webhook 参数集中化。
7. 视基线结果再决定是否引入 skills 分段执行（当前不作为阻塞项）。

---

## 17. 对话沉淀 Insights（2026-02-24）

1. 主要矛盾优先级  
- 第一位是“可执行环境”（网络、`.git` 可写、依赖安装可达），不是“再加更多编排层”。

2. manager 与 worker 职责边界  
- worker 专注读仓库、最小改动实现、验证与报告；  
- manager 专注状态管理、策略判断、审批门禁、最终 push/PR 动作。

3. skills 的正确定位  
- skills 是分段契约（输入/输出/验收标准），不是必须多次起 CLI 的机械拆分。

4. 自动化安全观  
- `danger-full-access` 可提升成功率，但必须配套边界：workspace 范围检查、局部缓存/环境、禁止全局安装、禁止 sudo。
- 若要硬隔离，仍应落到容器/VM，不应把“提示约束”当安全边界。

5. 评估方法论  
- 先拿真实 baseline（成功率/失败类型），再决定是否继续扩控制面或优化 prompt/skills。
- 没有 baseline 指标的系统设计讨论，容易过度工程化。

6. 交互方式定位  
- CLI 是 manager 的执行接口，不是最终用户主入口。  
- 最终用户应通过 Telegram/对话入口下发任务与审批，manager 再调用 CLI。

7. 未知工具链应对原则  
- 先扩展 `runtime_env_overrides.json` 做环境隔离覆盖；  
- 再通过 preflight 增加命令/网络检测；  
- 最后才改执行流程代码。
